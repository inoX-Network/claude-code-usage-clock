#!/usr/bin/env python3
"""PreToolUse hook for Agent, Workflow and forking Skills (matcher "Agent|Workflow|Skill").

Applies to spawns of the main session too (no agent_id filter) unless `apply_to_main_session` is off. That is
intended: otherwise nobody would be slowed down. The main session's other tool calls are never blocked.

Model part (`model_effort_action`): `deny` fails closed, without a determinable model and effort the start is
denied; `warn` starts and reports the same finding as a warning; `off` does not judge, it only counts agents. Every internal error,
including a failed import, also denies via deny JSON with exit 0, because a hook error with exit 1 does NOT
block. Empty or broken input or a missing tool_name → deny, because the hook is only registered for
Agent|Workflow|Skill.
Skill: only skills with context: fork start an agent and are checked (the usage thresholds apply). A fork
without a complete agent definition is reported, not denied (`skill_fork_missing_agent`). Inline, unknown and
plugin skills: not responsible. An internal error in the Skill branch only warns (otherwise it would block
every skill, including plain knowledge skills).
Usage part fails open: at `start_block_5h` (5h) or `start_block_week` (week) no new start; with an unknown or
stale measurement only a warning, unless the block has more than `unknown_usage_max_agents` agents. Approval
for the week: --week-ok-until=YYYY-MM-DD (up to and including that day only a warning; it lives in the hook
command in settings.json, where the model cannot set it if settings.json is protected).
In shadow mode everything is only logged.
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EVENT = 'PreToolUse'


def _mode(argv: list[str]) -> str:
    """The mode without _hookio, for the case that exactly its import fails. Same rule as _hookio.parse_args."""
    mode = 'shadow'
    for arg in argv:
        if arg.startswith('--mode='):
            mode = arg[len('--mode='):]
    return mode if mode == 'enforce' else 'shadow'


def _deny_without_hookio(reason: str) -> None:
    """Deny JSON without _hookio, for the case that exactly its import fails."""
    print(json.dumps({'hookSpecificOutput': {'hookEventName': EVENT, 'permissionDecision': 'deny',
                                             'permissionDecisionReason': reason}}, ensure_ascii=False))


def decide_start(usage, agent_count: int | None, thresholds: dict, week_ok_until: date | None = None,
                 today: date | None = None) -> tuple[str, str]:
    """Usage part: ('allow' | 'warn' | 'deny', reason).

    agent_count: agents in the block (Agent tool and Skill fork = 1, Workflow = counted agent() calls,
    None = could not be counted). week_ok_until: approval for the 7-day window, valid up to and including
    that day (only a warning then).
    """
    if usage.state != 'ok':
        limit = thresholds['unknown_usage_max_agents']
        if agent_count is None or agent_count > limit:
            agents = 'an unknown number of' if agent_count is None else str(agent_count)
            return 'deny', (f'Usage {usage.state} and {agents} agents in the block: fresh measurement first, then the '
                            f'block. Up to {limit} agent(s) may start while the usage is {usage.state}.')
        return 'warn', f'Usage {usage.state}: start allowed (fail open), keep blocks small.'
    note = ''
    if usage.week is not None and usage.week >= thresholds['start_block_week']:
        today = today or date.today()
        if week_ok_until is None or today > week_ok_until:
            return 'deny', (f'7-day window at {usage.week:.0f} %: no new start without approval '
                            '(--week-ok-until=YYYY-MM-DD on the spawn gate command in settings.json).')
        note = f'7-day window at {usage.week:.0f} %: start approved until {week_ok_until.isoformat()}.'
    if usage.five_hour is not None and usage.five_hour >= thresholds['start_block_5h']:
        return 'deny', f'5h window at {usage.five_hour:.0f} %: no new block until the window resets.'
    return ('warn', note) if note else ('allow', '')


def decide(hook_input: dict, cfg: dict, week_ok_until: date | None = None, today: date | None = None,
           now=None) -> tuple[str, str]:
    """('allow' | 'warn' | 'deny' | 'not_responsible', reason) for one hook input."""
    import _hookio
    import _usage
    from _spawn_check import Check, Rules, check_agent, check_skill, check_workflow

    tool = hook_input.get('tool_name')
    if not isinstance(tool, str) or not tool:
        return 'deny', 'Spawn gate: input empty, broken or without tool_name; denied to be safe.'
    if tool not in ('Agent', 'Workflow', 'Skill'):
        return 'not_responsible', ''
    spawn_rules = cfg['spawn_rules']
    if not spawn_rules['apply_to_main_session'] and not hook_input.get('agent_id'):
        return 'not_responsible', ''
    rules = Rules.from_config(spawn_rules)
    raw = hook_input.get('tool_input')
    tool_input = raw if isinstance(raw, dict) else {}
    directories = _hookio.agent_dirs(hook_input)
    if tool == 'Agent':
        agent_type = tool_input.get('subagent_type') or 'general-purpose'
        if not rules.require:
            check = Check(True, agent_count=1)
        elif agent_type in rules.exceptions:
            check = Check(True, notes=[f'"{agent_type}" allowed as an exception.'], agent_count=1)
        else:
            check = check_agent(tool_input, directories, rules)
    elif tool == 'Workflow':
        check = check_workflow(tool_input, directories, cwd=hook_input.get('cwd'), rules=rules)
    else:
        skill_dirs, command_dirs = _hookio.skill_dirs(hook_input)
        check = check_skill(tool_input, skill_dirs, command_dirs, directories, rules, cwd=hook_input.get('cwd'))
        if check is None:
            return 'not_responsible', ''
    if not check.allowed:
        if rules.model_effort_action == 'deny':
            return 'deny', check.reason
        check = Check(True, notes=[check.reason] + check.notes, agent_count=check.agent_count)
    usage = _usage.read_usage(None, now, cfg['thresholds']['max_age_min'])
    action, text = decide_start(usage, check.agent_count, cfg['thresholds'], week_ok_until, today)
    if action == 'deny':
        return 'deny', ' '.join([text] + check.notes)
    notes = check.notes + ([text] if action == 'warn' else [])
    return ('warn', ' '.join(notes)) if notes else ('allow', '')


def main() -> int:
    mode = _mode(sys.argv[1:])
    try:
        import _config
        import _hookio
    except Exception as exc:  # fail closed even without the base modules
        reason = f'Spawn gate cannot be loaded ({type(exc).__name__}); denied to be safe.'
        if mode == 'enforce':
            _deny_without_hookio(reason)
        else:
            print(reason, file=sys.stderr)
        return 0
    args = _hookio.parse_args(sys.argv[1:])
    hook_input = _hookio.read_input()
    try:
        decision, reason = decide(hook_input, _config.load(log_problems=True),
                                  _hookio.parse_date(args['week-ok-until']))
    except Exception as exc:  # fail closed: every unexpected error, including an import, denies; except for skills
        if hook_input.get('tool_name') == 'Skill':
            decision, reason = 'warn', f'Skill check failed internally ({type(exc).__name__}); only warned.'
        else:
            decision, reason = 'deny', f'Spawn gate failed internally ({type(exc).__name__}); denied to be safe.'
    if decision == 'not_responsible':
        return 0
    _hookio.log_decision('spawn_gate', args['mode'], hook_input, decision, reason)
    if args['mode'] != 'enforce':
        return 0
    if decision == 'deny':
        _hookio.emit_deny(EVENT, reason)
    elif decision == 'warn':
        _hookio.emit_context(EVENT, 'Spawn gate: ' + reason)
    return 0


if __name__ == '__main__':
    sys.exit(main())
