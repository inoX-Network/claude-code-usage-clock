"""Tests for hooks/spawn_gate.py. Most run the real script as a subprocess: JSON in, JSON and exit code out.

Every test checks the exact output keys: a wrong key would stay silently without effect in Claude Code.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import _config
import _spawn_check
import _usage
import spawn_gate
from conftest import HOOKS, WINDOWS_CLI

SCRIPT = os.path.join(HOOKS, 'spawn_gate.py')
THRESHOLDS = dict(_config.DEFAULTS['thresholds'], week_action='deny')   # the strict level; warn and ask have own tests
DAY = date(2026, 1, 14)


@pytest.fixture
def env(tmp_path):
    agents = tmp_path / 'agents'
    for name, head in {'full-def': 'model: opus\neffort: high', 'half-def': 'model: sonnet',
                       'worker-fast': 'model: haiku\neffort: low'}.items():
        (agents / name).mkdir(parents=True)
        (agents / name / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Test\n{head}\n---\nText\n',
                                                encoding='utf-8')
    # Most tests check the strict level; the default level `warn` has its own tests.
    strict = tmp_path / 'strict-config.json'
    strict.write_text(json.dumps({'spawn_rules': {'model_effort_action': 'deny'}, 'thresholds': {'week_action': 'deny'}}),
                      encoding='utf-8')
    return dict(os.environ, CCUC_AGENT_DIRS=str(agents), CCUC_CONFIG=str(strict))


def usage(tmp_path, five, week=20, age_min=1):
    now = time.time()
    measured = datetime.fromtimestamp(now - age_min * 60, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    (tmp_path / 'usage.json').write_text(json.dumps({
        'five_hour': None if five is None else {'used_percentage': five, 'resets_at': now + 3600},
        'seven_day': {'used_percentage': week, 'resets_at': now + 86400}, 'measured_at': measured}), encoding='utf-8')


def config(tmp_path, env, spawn_rules=None, **sections):
    path = tmp_path / 'config.json'
    sections['thresholds'] = {'week_action': 'deny', **sections.get('thresholds', {})}
    path.write_text(json.dumps({'spawn_rules': {'model_effort_action': 'deny', **(spawn_rules or {})}, **sections}),
                    encoding='utf-8')
    return dict(env, CCUC_CONFIG=str(path))


def run(hook_input, env, *args):
    r = subprocess.run([sys.executable, SCRIPT, *args], input=json.dumps(hook_input), capture_output=True, text=True, encoding='utf-8',
                       env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def run_raw(path, text, env, *args):
    """Like run, but with any stdin and without an assumption about the exit code."""
    r = subprocess.run([sys.executable, str(path), *args], input=text, capture_output=True, text=True, encoding='utf-8', env=env,
                       timeout=30)
    return r.returncode, (json.loads(r.stdout) if r.stdout.strip() else None), r.stderr


def log(tmp_path):
    path = tmp_path / 'log' / 'log.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def gate_log(tmp_path):
    return [line for line in log(tmp_path) if line['hook'] == 'spawn_gate']


def agent(agent_type=None, cwd='.', **extra):
    tool_input = {'prompt': 'secret task', 'description': 'x', **extra}
    if agent_type:
        tool_input['subagent_type'] = agent_type
    return {'hook_event_name': 'PreToolUse', 'tool_name': 'Agent', 'tool_input': tool_input, 'cwd': str(cwd)}


def workflow(source, cwd='.'):
    return {'hook_event_name': 'PreToolUse', 'tool_name': 'Workflow', 'tool_input': {'script': source}, 'cwd': str(cwd)}


def is_deny(out):
    return (out is not None and list(out) == ['hookSpecificOutput']
            and set(out['hookSpecificOutput']) == {'hookEventName', 'permissionDecision', 'permissionDecisionReason'}
            and out['hookSpecificOutput']['hookEventName'] == 'PreToolUse'
            and out['hookSpecificOutput']['permissionDecision'] == 'deny'
            and bool(out['hookSpecificOutput']['permissionDecisionReason']))


def is_warning(out):
    return (out is not None and list(out) == ['hookSpecificOutput']
            and set(out['hookSpecificOutput']) == {'hookEventName', 'additionalContext'}
            and out['hookSpecificOutput']['hookEventName'] == 'PreToolUse'
            and out['hookSpecificOutput']['additionalContext'].startswith('Spawn gate: '))


def is_shadow_note(out, phrase):
    """The hint of shadow mode: context only, never a decision, and it says what enforce would have done."""
    return (out is not None and list(out) == ['hookSpecificOutput']
            and set(out['hookSpecificOutput']) == {'hookEventName', 'additionalContext'}
            and out['hookSpecificOutput']['hookEventName'] == 'PreToolUse'
            and out['hookSpecificOutput']['additionalContext'].startswith(
                f'Spawn gate (shadow mode, information only): in enforce mode this start {phrase}. Reason: ')
            and out['hookSpecificOutput']['additionalContext'].endswith(
                'Nothing was blocked. Tell the user in one sentence. The gates stay in shadow mode until the user '
                'runs tools/install.py --mode enforce.'))


def reason(out):
    return out['hookSpecificOutput']['permissionDecisionReason']


def context(out):
    return out['hookSpecificOutput']['additionalContext']


# --- Agent tool ---------------------------------------------------------------------------------------------

def test_shadow_mode_never_blocks_but_logs_and_tells_what_enforce_would_have_done(tmp_path, env):
    usage(tmp_path, 10)
    enforced = reason(run(agent(), env, '--mode=enforce'))
    assert run(agent(), env) == {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': (
        'Spawn gate (shadow mode, information only): in enforce mode this start would have been denied. Reason: '
        + enforced + ' Nothing was blocked. Tell the user in one sentence. The gates stay in shadow mode until the '
        'user runs tools/install.py --mode enforce.')}}
    line = gate_log(tmp_path)[-1]
    assert line['decision'] == 'deny' and line['mode'] == 'shadow'
    assert 'secret task' not in json.dumps(line)  # never log prompt content
    assert line['tool_input_keys'] == ['description', 'prompt']


def test_general_purpose_without_definition_denied_in_enforce(tmp_path, env):
    usage(tmp_path, 10)
    out = run(agent(), env, '--mode=enforce')
    assert is_deny(out) and 'Define an agent with model: and effort:' in reason(out)
    env = config(tmp_path, env, {'suggested_agents': ['worker-fast', 'reviewer']})
    out = run(agent(), env, '--mode=enforce')
    assert is_deny(out) and 'worker-fast' in reason(out) and 'reviewer' in reason(out)


def test_complete_definition_allowed(tmp_path, env):
    usage(tmp_path, 10)
    assert run(agent('full-def'), env, '--mode=enforce') is None
    assert gate_log(tmp_path)[-1]['decision'] == 'allow'


def test_definition_without_effort_denied(tmp_path, env):
    usage(tmp_path, 10)
    assert is_deny(run(agent('half-def'), env, '--mode=enforce'))


def test_model_lines_inside_a_multi_line_value_do_not_count(tmp_path, env):
    # YAML reads model: and effort: here as part of the description; Claude Code starts the agent without both
    usage(tmp_path, 10)
    for name, head in (('sneaky', 'description: "does things\nmodel: haiku\neffort: low\n"'),
                       ('dashy', 'description: a---b\nmodel: haiku\neffort: low')):
        folder = tmp_path / 'agents' / name
        folder.mkdir()
        (folder / 'SKILL.md').write_text(f'---\nname: {name}\n{head}\n---\nText\n', encoding='utf-8')
        out = run(agent(name), env, '--mode=enforce')
        assert is_deny(out) and 'definition without model: and/or effort:' in reason(out), name


def test_block_scalar_before_model_and_effort_is_allowed(tmp_path, env):
    usage(tmp_path, 10)
    folder = tmp_path / 'agents' / 'block'
    folder.mkdir()
    (folder / 'SKILL.md').write_text('---\nname: block\ndescription: !!str >-\n  text\nmodel: haiku\neffort: low\n'
                                     '---\nText\n', encoding='utf-8')
    assert run(agent('block'), env, '--mode=enforce') is None


def test_usage_at_the_5h_threshold_no_new_start(tmp_path, env):
    usage(tmp_path, 80)
    out = run(agent('full-def'), env, '--mode=enforce')
    assert is_deny(out) and '5h' in reason(out)


def test_unknown_usage_only_warns(tmp_path, env):
    usage(tmp_path, None)
    assert is_warning(run(agent('full-def'), env, '--mode=enforce'))


def test_model_in_the_call_overriding_is_reported(tmp_path, env):
    usage(tmp_path, 10)
    out = run(agent('full-def', model='haiku'), env, '--mode=enforce')
    assert is_warning(out) and 'overrides' in context(out)


def test_model_in_the_call_by_configuration(tmp_path, env):
    usage(tmp_path, 10)
    out = run(agent('full-def', model='haiku'), config(tmp_path, env, {'model_override_in_call': 'deny'}), '--mode=enforce')
    assert is_deny(out) and 'model_override_in_call' in reason(out)
    assert run(agent('full-def', model='haiku'), config(tmp_path, env, {'model_override_in_call': 'allow'}),
               '--mode=enforce') is None


def test_exception_is_allowed_and_reported(tmp_path, env):
    usage(tmp_path, 10)
    env = config(tmp_path, env, {'exceptions': ['Explore']})
    out = run(agent('Explore'), env, '--mode=enforce')
    assert is_warning(out) and 'exception' in context(out)
    assert is_deny(run(agent('Other'), env, '--mode=enforce'))


def test_default_exceptions_come_from_the_configuration(tmp_path, env):
    usage(tmp_path, 10)
    assert is_warning(run(agent('claude-code-guide'), env, '--mode=enforce'))
    assert is_deny(run(agent('claude-code-guide'), config(tmp_path, env, {'exceptions': []}), '--mode=enforce'))


def test_exceptions_argument_is_no_longer_read(tmp_path, env):
    usage(tmp_path, 10)
    assert is_deny(run(agent('Explore'), env, '--mode=enforce', '--exceptions=Explore'))


def test_other_tools_untouched(tmp_path, env):
    usage(tmp_path, 99)
    out = run({'tool_name': 'Bash', 'tool_input': {'command': 'ls'}}, env, '--mode=enforce')
    assert out is None and gate_log(tmp_path) == []


# --- Workflow tool ------------------------------------------------------------------------------------------

WF_FULL = """export const meta = { name: 'x', description: 'y' }
const a = await agent(`Count ${list.length} files, no agent( here`, { label: 'a', model: 'haiku', effort: 'low' })
const b = await agent('Verdict', { model: 'opus', effort: 'high', schema: S })
const c = await agent('Type', { agentType: 'full-def' })
"""

WF_HEAD = "export const meta = { name: 'x', description: 'y' }\n"


def test_complete_workflow_allowed(tmp_path, env):
    usage(tmp_path, 10)
    assert run(workflow(WF_FULL, tmp_path), env, '--mode=enforce') is None


@pytest.mark.parametrize('options', [
    "{ label: 'p', agentType: p.high ? 'full-def' : 'worker-fast', schema: S }",
    "{ agentType: (a && b) ? 'full-def' : `worker-fast` }",
    "{ model: big ? 'opus' : 'haiku', effort: 'low' }",
    "{ model: 'sonnet', effort: deep ? 'high' : 'low' }",
    "{ agentType: x ? 'full-def' : 'full-def', model: y ? 'opus' : 'sonnet' }",
])
def test_ternary_with_two_valid_literals_allowed(tmp_path, env, options):
    usage(tmp_path, 10)
    source = WF_HEAD + f"const r = await agent('Task', {options})\n"
    assert run(workflow(source, tmp_path), env, '--mode=enforce') is None, options


@pytest.mark.parametrize('options,expected', [
    ("{ agentType: p.high ? 'full-def' : 'half-def' }", 'effort missing'),                # one side incomplete
    ("{ agentType: p.high ? 'full-def' : kind }", 'not a string literal'),                # variable
    ("{ agentType: a ? 'full-def' : b ? 'full-def' : 'worker-fast' }", 'not a string literal'),  # nested
    ("{ agentType: kind ?? 'full-def' }", 'not a string literal'),                       # nullish
    ("{ agentType: ? 'full-def' : 'full-def' }", 'not a string literal'),                # no condition
    ("{ agentType: a ? 'full-def' }", 'not a string literal'),                           # no colon
    ("{ model: g ? 'opus' : 'gpt-4', effort: 'low' }", 'unknown'),                        # invalid model
    ("{ model: 'opus', effort: t ? 'high' : 'extreme' }", 'invalid'),                    # invalid effort
    ("{ agentType: a ? 'full-def' : 'does-not-exist' }", 'missing'),                     # unknown agent
])
def test_ternary_with_an_invalid_side_denied(tmp_path, env, options, expected):
    usage(tmp_path, 10)
    source = WF_HEAD + f"const r = await agent('Task', {options})\n"
    out = run(workflow(source, tmp_path), env, '--mode=enforce')
    assert is_deny(out) and expected in reason(out), options


def test_workflow_with_missing_effort_denied(tmp_path, env):
    usage(tmp_path, 10)
    source = WF_FULL + "const d = await agent('without', { label: 'd', model: 'sonnet' })\n"
    out = run(workflow(source, tmp_path), env, '--mode=enforce')
    assert is_deny(out) and 'agent call 4: effort missing' in reason(out)


def test_workflow_agent_in_template_expression_is_checked(tmp_path, env):
    usage(tmp_path, 10)
    source = "log(`Result: ${await agent('inner', { label: 'i' })}`)\n"
    assert is_deny(run(workflow(source, tmp_path), env, '--mode=enforce'))


def test_workflow_script_path_is_read(tmp_path, env):
    usage(tmp_path, 10)
    path = tmp_path / 'wf.js'
    path.write_text(WF_FULL, encoding='utf-8')
    hook_input = {'tool_name': 'Workflow', 'tool_input': {'scriptPath': str(path), 'resumeFromRunId': 'wf_x'},
                  'cwd': str(tmp_path)}
    assert run(hook_input, env, '--mode=enforce') is None


def test_workflow_without_agent_calls_warns(tmp_path, env):
    usage(tmp_path, 10)
    out = run(workflow(WF_HEAD + 'log(1)\n', tmp_path), env, '--mode=enforce')
    assert is_warning(out) and 'No agent(...) call found.' in context(out)


def test_workflow_with_several_agents_denied_while_usage_unknown(tmp_path, env):
    source = "".join(f"await agent('p{i}', {{ model: 'haiku', effort: 'low' }})\n" for i in range(6))
    for age in (60, None):
        if age is None:
            (tmp_path / 'usage.json').unlink()
        else:
            usage(tmp_path, 10, age_min=age)
        out = run(workflow(source, tmp_path), env, '--mode=enforce')
        assert is_deny(out) and 'fresh measurement first' in reason(out) and '6 agents' in reason(out)
    single = "await agent('p', { model: 'haiku', effort: 'low' })\n"
    assert is_warning(run(workflow(single, tmp_path), env, '--mode=enforce'))


def test_unknown_usage_agent_limit_from_the_configuration(tmp_path, env):
    two = "await agent('a', { model: 'haiku', effort: 'low' })\nawait agent('b', { model: 'haiku', effort: 'low' })\n"
    assert is_deny(run(workflow(two, tmp_path), env, '--mode=enforce'))
    out = run(workflow(two, tmp_path), config(tmp_path, env, thresholds={'unknown_usage_max_agents': 2}), '--mode=enforce')
    assert is_warning(out) and 'Usage unknown' in context(out)


def test_invisible_character_before_the_bracket_is_denied(tmp_path, env):
    # JavaScript reads U+FEFF as a space: without the rule the call is invisible and "no agent" is allowed
    source = 'await agent\ufeff("do everything", {model: "opus"})\n'
    out = run(workflow(source, tmp_path), env, '--mode=enforce')
    assert is_deny(out) and 'U+FEFF' in reason(out)


def test_log_masks_script_parts_model_literals_and_paths(tmp_path, env):
    usage(tmp_path, 10)
    hidden = tmp_path / 'SECRET-DIR'
    cases = [workflow("await agent('a', {model: 'SECRET-MODEL', effort: 'low'})\n", tmp_path),
             workflow("const r = /SECRET-REGEX agent/\n", tmp_path),
             workflow("await agent('a', {'SECRET-ENTRY'})\n", tmp_path),
             workflow(f"await workflow({{scriptPath: '{hidden.as_posix()}/child.js'}})\n", tmp_path),
             workflow("await workflow('SECRET-NAME')\n", hidden),
             {'tool_name': 'Workflow', 'tool_input': {'scriptPath': str(hidden / 'wf.js')}, 'cwd': str(tmp_path)}]
    for hook_input in cases:
        out = run(hook_input, env, '--mode=enforce')
        assert is_deny(out) and 'SECRET' in reason(out), hook_input     # the model still sees the value
        shadow = run(hook_input, env)                                   # shadow: logged, the model is told
        assert is_shadow_note(shadow, 'would have been denied') and 'SECRET' in context(shadow), hook_input
    lines = gate_log(tmp_path)
    assert len(lines) == 2 * len(cases) and all(line['decision'] == 'deny' and '…' in line['reason'] for line in lines)
    assert 'SECRET' not in (tmp_path / 'log' / 'log.jsonl').read_text(encoding='utf-8')


def test_synthetic_multi_step_script_through_the_hook(tmp_path, env):
    usage(tmp_path, 10)
    child = tmp_path / 'child.js'
    child.write_text("await agent('Child', { agentType: 'full-def' })\n", encoding='utf-8')
    source = (WF_HEAD + "const found = await parallel(files.map(f => () => agent(`Check ${f}`, "
              "{ agentType: 'worker-fast' })))\n"
              "await pipeline([() => agent('Merge', { model: 'sonnet', effort: big ? 'max' : 'high' })])\n"
              f"await workflow({{ scriptPath: '{child.as_posix()}' }})\n")      # backslashes in a literal are refused
    assert run(workflow(source, tmp_path), env, '--mode=enforce') is None
    usage(tmp_path, 10, age_min=60)
    out = run(workflow(source, tmp_path), env, '--mode=enforce')
    assert is_deny(out) and '3 agents' in reason(out)


# --- Robustness: fail closed ---------------------------------------------------------------------------------

def copy_hooks(tmp_path, *names, folder='copy'):
    """Hook scripts in a foreign folder, to recreate missing neighbours."""
    target = tmp_path / folder
    target.mkdir(exist_ok=True)
    for name in names:
        shutil.copy(os.path.join(WINDOWS_CLI if name == '_win.py' else HOOKS, name), target / name)
    return target


def test_spawn_gate_without_the_check_module_denies(tmp_path, env):
    # a ModuleNotFoundError must not end with exit 1 and no deny: the spawn would run anyway
    usage(tmp_path, 10)
    without = copy_hooks(tmp_path, 'spawn_gate.py', '_hookio.py', '_config.py', '_usage.py', '_plat.py',
                         *(['_win.py'] if os.name == 'nt' else []))
    text = json.dumps({'tool_name': 'Agent', 'tool_input': {}})
    code, out, _ = run_raw(without / 'spawn_gate.py', text, env, '--mode=enforce')
    assert code == 0 and is_deny(out) and 'ModuleNotFoundError' in reason(out)
    code, out, _ = run_raw(without / 'spawn_gate.py', text, env)          # shadow: let through, but said
    assert code == 0 and is_shadow_note(out, 'would have been denied') and 'ModuleNotFoundError' in context(out)
    assert gate_log(tmp_path)[-1]['decision'] == 'deny'


def test_spawn_gate_without_base_modules_denies(tmp_path, env):
    alone = copy_hooks(tmp_path, 'spawn_gate.py')
    code, out, _ = run_raw(alone / 'spawn_gate.py', json.dumps(agent('full-def')), env, '--mode=enforce')
    assert code == 0 and is_deny(out) and 'cannot be loaded (ModuleNotFoundError)' in reason(out)
    code, out, err = run_raw(alone / 'spawn_gate.py', json.dumps(agent('full-def')), env)
    assert (code, out) == (0, None) and 'cannot be loaded' in err
    code, out, _ = run_raw(alone / 'spawn_gate.py', json.dumps(agent('full-def')), env, '--mode=enforce', '--mode=x')
    assert (code, out) == (0, None)        # the last --mode wins, as in _hookio.parse_args


@pytest.mark.parametrize('text', ['', 'xyz', '[]', json.dumps({'tool_input': {'subagent_type': 'full-def'}}),
                                  json.dumps({'tool_name': 5})])
def test_broken_input_denies(tmp_path, env, text):
    usage(tmp_path, 10)
    code, out, _ = run_raw(SCRIPT, text, env, '--mode=enforce')
    assert code == 0 and is_deny(out) and 'without tool_name' in reason(out), text


def test_broken_config_falls_back_and_is_logged(tmp_path, env):
    usage(tmp_path, 10)
    path = tmp_path / 'config.json'
    path.write_text('{broken', encoding='utf-8')
    assert run(agent('full-def'), dict(env, CCUC_CONFIG=str(path)), '--mode=enforce') is None
    assert [line['hook'] for line in log(tmp_path)] == ['config', 'spawn_gate']


def test_week_ok_until_today_only_warns(tmp_path, env):
    usage(tmp_path, 10, week=85)
    today = datetime.now().date()
    out = run(agent('full-def'), env, '--mode=enforce', f'--week-ok-until={today.isoformat()}')
    assert is_warning(out) and 'approved until' in context(out)
    yesterday = (today - timedelta(days=1)).isoformat()
    for arg in (f'--week-ok-until={yesterday}', '--week-ok-until=broken', '--week-ok-until='):
        out = run(agent('full-def'), env, '--mode=enforce', arg)
        assert is_deny(out) and 'week-ok-until' in reason(out), arg


def test_spawn_gate_applies_to_the_main_session_too(tmp_path, env):
    usage(tmp_path, 80)
    hook_input = agent('full-def')
    assert 'agent_id' not in hook_input
    assert is_deny(run(hook_input, env, '--mode=enforce'))


def test_main_session_can_be_left_out_by_configuration(tmp_path, env):
    usage(tmp_path, 80)
    env = config(tmp_path, env, {'apply_to_main_session': False})
    assert run(agent(), env, '--mode=enforce') is None
    assert gate_log(tmp_path) == []
    sub = dict(agent('full-def'), agent_id='a1', agent_type='worker-fast')
    assert is_deny(run(sub, env, '--mode=enforce'))
    assert is_deny(run({'tool_name': 5}, env, '--mode=enforce'))       # broken input still denies


def test_the_default_level_warns_instead_of_denying(tmp_path, env):
    usage(tmp_path, 10)
    env = dict(env, CCUC_CONFIG=str(tmp_path / 'no-config.json'))
    out = run(agent(), env, '--mode=enforce')
    assert is_warning(out) and 'Agent "general-purpose": no definition' in context(out)
    assert run(agent('full-def'), env, '--mode=enforce') is None


def test_warn_level_reports_every_model_finding_and_starts(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, personal={'bare': 'name: bare\ncontext: fork'})
    env = config(tmp_path, env, {'model_effort_action': 'warn', 'skill_fork_missing_agent': 'deny',
                                 'model_override_in_call': 'deny'})
    out = run(agent('half-def'), env, '--mode=enforce')
    assert is_warning(out) and 'definition without model: and/or effort:' in context(out)
    out = run(agent('full-def', model='haiku'), env, '--mode=enforce')
    assert is_warning(out) and 'not allowed here (model_override_in_call)' in context(out)
    out = run(workflow("await agent('a', {model: 'opus'})\n", tmp_path), env, '--mode=enforce')
    assert is_warning(out) and 'agent call 1' in context(out)
    out = run(skill('bare', tmp_path), env, '--mode=enforce')
    assert is_warning(out) and 'Skill "bare" (context: fork)' in context(out)
    assert [line['decision'] for line in gate_log(tmp_path)] == ['warn'] * 4


def test_warn_level_keeps_the_usage_thresholds(tmp_path, env):
    env = config(tmp_path, env, {'model_effort_action': 'warn'})
    usage(tmp_path, 80)
    out = run(agent(), env, '--mode=enforce')
    assert is_deny(out) and 'no new block' in reason(out)
    usage(tmp_path, 10)
    script = "await agent('a', {model: 'opus'}); await agent('b', {model: 'opus'})\n"
    assert is_warning(run(workflow(script, tmp_path), env, '--mode=enforce'))
    usage(tmp_path, 10, age_min=60)                      # stale: two agents are more than one may start
    assert is_deny(run(workflow(script, tmp_path), env, '--mode=enforce'))


def test_warn_level_counts_an_unanalysable_script_as_unknown(tmp_path, env):
    env = config(tmp_path, env, {'model_effort_action': 'warn'})
    script = "const a = agent; await a('x')\n"
    usage(tmp_path, 10)
    out = run(workflow(script, tmp_path), env, '--mode=enforce')
    assert is_warning(out) and 'only agent(...) can be checked' in context(out)
    usage(tmp_path, 10, age_min=60)                      # an unknown number of agents must not count as zero
    out = run(workflow(script, tmp_path), env, '--mode=enforce')
    assert is_deny(out) and 'an unknown number of agents' in reason(out)


def test_model_check_off_keeps_the_usage_thresholds(tmp_path, env):
    usage(tmp_path, 10)
    env = config(tmp_path, env, {'model_effort_action': 'off'})
    assert run(agent(), env, '--mode=enforce') is None
    assert run(workflow("await agent('a')\n", tmp_path), env, '--mode=enforce') is None
    usage(tmp_path, 80)
    assert is_deny(run(agent(), env, '--mode=enforce'))
    usage(tmp_path, 10, age_min=60)
    out = run(workflow("const a = agent; await a('x')\n", tmp_path), env, '--mode=enforce')
    assert is_deny(out) and 'an unknown number of agents' in reason(out)
    assert is_warning(run(agent(), env, '--mode=enforce'))


def test_project_agent_found_when_cwd_is_a_subfolder(tmp_path, env):
    # The cwd was in a subfolder and the agent in the project root's agents folder. Claude Code found it,
    # the gate did not ("no definition"). The root comes from CLAUDE_PROJECT_DIR; cwd moves with every cd.
    usage(tmp_path, 10)
    root = tmp_path / 'project'
    folder = root / '.claude' / 'agents'
    folder.mkdir(parents=True)
    (folder / 'probe.md').write_text('---\nname: probe\ndescription: Test\nmodel: haiku\neffort: low\n---\nText\n',
                                     encoding='utf-8')
    sub = root / 'packages' / 'x'
    sub.mkdir(parents=True)
    env = {k: v for k, v in env.items() if k != 'CCUC_AGENT_DIRS'}
    env.update(HOME=str(tmp_path / 'home'), CLAUDE_PROJECT_DIR=str(root))
    hook_input = agent('probe', cwd=sub)
    assert run(hook_input, env, '--mode=enforce') is None
    env.pop('CLAUDE_PROJECT_DIR')
    assert run(hook_input, env, '--mode=enforce') is None  # without the root: the cwd and its parents


# --- The gate reads the agents Claude Code starts (C-7) -------------------------------------------------------------
# Claude Code reads project agents from the project root and its parents up to the repo root, never from the cwd,
# the deepest folder winning, and it follows linked folders (rg --follow). A definition the gate reads but Claude
# Code does not, or the other way round, lets an agent through that would otherwise be denied.

def agent_def(path, name, head):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: Test\n{head}\n---\nText\n', encoding='utf-8')


def repo_world(tmp_path, env):
    """A repo with .git and a session in repo/app; the user's agents in the test home."""
    usage(tmp_path, 10)
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    (repo / 'app' / 'sub').mkdir(parents=True)
    env = {k: v for k, v in env.items() if k != 'CCUC_AGENT_DIRS'}
    env.update(HOME=str(tmp_path / 'home'), USERPROFILE=str(tmp_path / 'home'), CLAUDE_PROJECT_DIR=str(repo / 'app'))
    return repo, tmp_path / 'home' / '.claude' / 'agents', env


def test_a_definition_planted_in_the_cwd_is_not_what_claude_code_starts(tmp_path, env):
    repo, _, env = repo_world(tmp_path, env)
    for cwd in (repo / 'app' / 'sub', tmp_path / 'elsewhere'):      # 2g: a folder in the project, 2b: outside it
        agent_def(cwd / '.claude' / 'agents' / 'x.md', 'general-purpose', 'model: sonnet\neffort: low')
        out = run(agent('general-purpose', cwd=cwd), env, '--mode=enforce')
        assert is_deny(out) and 'no definition' in reason(out), cwd


def test_a_complete_copy_in_the_cwd_does_not_cover_an_incomplete_user_definition(tmp_path, env):
    repo, user, env = repo_world(tmp_path, env)      # 2d
    agent_def(user / 'helper.md', 'helper', 'model: opus')
    agent_def(repo / 'app' / 'sub' / '.claude' / 'agents' / 'helper.md', 'helper', 'model: opus\neffort: high')
    assert is_deny(run(agent('helper', cwd=repo / 'app' / 'sub'), env, '--mode=enforce'))


def test_a_definition_in_a_parent_of_the_project_root_is_read(tmp_path, env):
    repo, user, env = repo_world(tmp_path, env)      # case 10: Claude Code starts repo's, ahead of the user's
    agent_def(repo / '.claude' / 'agents' / 'helper2.md', 'helper2', 'model: opus')
    agent_def(user / 'helper2.md', 'helper2', 'model: opus\neffort: high')
    assert is_deny(run(agent('helper2', cwd=repo / 'app'), env, '--mode=enforce'))


@pytest.mark.parametrize('deep,upper,allowed', [('model: opus', 'model: opus\neffort: high', False),
                                                ('model: opus\neffort: high', 'model: opus', True)],
                         ids=['deep-incomplete', 'deep-complete'])
def test_the_deepest_project_folder_wins(tmp_path, env, deep, upper, allowed):
    repo, _, env = repo_world(tmp_path, env)
    agent_def(repo / 'app' / '.claude' / 'agents' / 'h.md', 'h', deep)
    agent_def(repo / '.claude' / 'agents' / 'h.md', 'h', upper)
    out = run(agent('h', cwd=repo / 'app'), env, '--mode=enforce')
    assert (out is None) if allowed else is_deny(out)


@pytest.mark.posix_only
def test_a_definition_in_a_linked_folder_is_read(tmp_path, env):
    repo, user, env = repo_world(tmp_path, env)
    agent_def(tmp_path / 'outside' / 'helper3.md', 'helper3', 'model: opus')
    (repo / 'app' / '.claude' / 'agents').mkdir(parents=True)
    (repo / 'app' / '.claude' / 'agents' / 'linked').symlink_to(tmp_path / 'outside')
    agent_def(user / 'helper3.md', 'helper3', 'model: opus\neffort: high')
    assert is_deny(run(agent('helper3', cwd=repo / 'app'), env, '--mode=enforce'))


@pytest.mark.posix_only
def test_a_loop_of_linked_folders_ends_the_search_without_hanging(tmp_path, env):
    # The walk stops where the system refuses the path (ELOOP), as ripgrep skips a loop: the loop adds nothing
    repo, _, env = repo_world(tmp_path, env)
    folder = repo / 'app' / '.claude' / 'agents'
    agent_def(folder / 'helper4.md', 'helper4', 'model: opus')
    (folder / 'loop').symlink_to(folder)
    started = time.monotonic()
    assert is_deny(run(agent('helper4', cwd=repo / 'app'), env, '--mode=enforce'))
    assert time.monotonic() - started < 5


# --- Skills with context: fork ----------------------------------------------------------------------------------

def setup_skills(tmp_path, env, personal=None, project=None, commands=None):
    """Creates skill folders (precedence: personal before project) and sets the search variables."""
    folders = []
    for kind, skills in (('personal', personal or {}), ('project', project or {})):
        base = tmp_path / 'skills' / kind
        base.mkdir(parents=True, exist_ok=True)
        for folder, head in skills.items():
            (base / folder).mkdir()
            (base / folder / 'SKILL.md').write_text(f'---\n{head}\n---\nText\n', encoding='utf-8')
        folders.append(str(base))
    cmd = tmp_path / 'commands'
    cmd.mkdir(exist_ok=True)
    for name, head in (commands or {}).items():
        (cmd / f'{name}.md').write_text(f'---\n{head}\n---\nText\n', encoding='utf-8')
    return dict(env, CCUC_SKILL_DIRS=os.pathsep.join(folders), CCUC_COMMAND_DIRS=str(cmd))


def skill(name, cwd='.'):
    return {'hook_event_name': 'PreToolUse', 'tool_name': 'Skill', 'tool_input': {'skill': name}, 'cwd': str(cwd)}


def skill_file(path, head, raw=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(f'---\n{head}\n---\nText\n', encoding='utf-8')


def without_skill_env(env):
    env = dict(env)
    for key in ('CCUC_SKILL_DIRS', 'CCUC_COMMAND_DIRS', 'CLAUDE_PROJECT_DIR'):
        env.pop(key, None)
    return env


def test_fork_skill_with_complete_agent_allowed_and_logged(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, personal={'review': 'name: review\ncontext: fork\nagent: full-def'})
    assert run(skill('review', tmp_path), env, '--mode=enforce') is None
    line = gate_log(tmp_path)[-1]
    assert line['tool_name'] == 'Skill' and line['decision'] == 'allow'


def test_fork_skill_without_agent_warns_about_general_purpose(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, personal={'old': 'name: old\ncontext: fork'})
    out = run(skill('/old', tmp_path), env, '--mode=enforce')
    assert is_warning(out) and 'general-purpose' in context(out)
    assert gate_log(tmp_path)[-1]['decision'] == 'warn'


def test_fork_skill_with_half_definition_warns_instead_of_denying(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, personal={'half': 'name: half\ncontext: fork\nagent: half-def'})
    out = run(skill('half', tmp_path), env, '--mode=enforce')
    assert is_warning(out) and 'set agent: to an agent' in context(out)
    out = run(skill('half', tmp_path), config(tmp_path, env, {'skill_fork_missing_agent': 'deny'}), '--mode=enforce')
    assert is_deny(out) and 'set agent: to an agent' in reason(out)


def test_fork_skill_with_model_override_is_reported(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, personal={'m': 'name: m\ncontext: fork\nagent: full-def\nmodel: haiku'})
    out = run(skill('m', tmp_path), env, '--mode=enforce')
    assert is_warning(out) and 'overrides' in context(out)


def test_fork_skill_at_the_5h_threshold_denied(tmp_path, env):
    usage(tmp_path, 80)
    env = setup_skills(tmp_path, env, personal={'review': 'name: review\ncontext: fork\nagent: full-def'})
    out = run(skill('review', tmp_path), env, '--mode=enforce')
    assert is_deny(out) and '5h' in reason(out)


def test_inline_unknown_and_foreign_skills_not_responsible(tmp_path, env):
    usage(tmp_path, 99)  # no intervention even at 99 %: an inline skill starts no agent
    env = setup_skills(tmp_path, env, personal={'knowledge': 'name: knowledge\ndescription: Reference'})
    for name in ('knowledge', 'does-not-exist', 'code-review:code-review', '../personal/knowledge', '', 'a b'):
        assert run(skill(name, tmp_path), env, '--mode=enforce') is None, name
    assert run({'tool_name': 'Skill', 'tool_input': {'skill': 7}}, env, '--mode=enforce') is None
    assert gate_log(tmp_path) == []


def test_path_tricks_reach_no_fork_outside_the_skill_folders(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, personal={'harmless': 'name: harmless\ndescription: inline'},
                       project={'foreign': 'name: foreign\ncontext: fork'})
    env['CCUC_SKILL_DIRS'] = str(tmp_path / 'skills' / 'personal')  # project/ is outside
    for name in ('../project/foreign', 'project:foreign', '..'):
        assert run(skill(name, tmp_path), env, '--mode=enforce') is None, name
    assert gate_log(tmp_path) == []


def test_a_fork_whose_name_has_a_dot_or_a_letter_beyond_ascii_is_checked(tmp_path, env):
    # Claude Code starts these by their folder or file name; a name the gate did not parse went through unchecked
    usage(tmp_path, 80)
    fork = 'context: fork\nagent: full-def'
    env = setup_skills(tmp_path, env, personal={'my.skill': f'name: x\n{fork}', 'über': f'name: y\n{fork}'},
                       commands={'cmd.v2': f'name: z\n{fork}'})
    for name in ('my.skill', 'über', 'cmd.v2', '/my.skill some args'):
        out = run(skill(name, tmp_path), env, '--mode=enforce')
        assert is_deny(out) and '5h' in reason(out), name


def test_dot_parts_of_a_skill_name_reach_no_fork_outside_the_skill_folders(tmp_path, env):
    usage(tmp_path, 80)
    env = setup_skills(tmp_path, env, project={'foreign': 'name: foreign\ncontext: fork'})
    env['CCUC_SKILL_DIRS'] = str(tmp_path / 'skills' / 'personal')      # project/ is outside
    (tmp_path / 'skills' / 'personal').mkdir(parents=True, exist_ok=True)
    # Forks a dot part would reach: <personal>/../SKILL.md and <cwd>/x/../.claude/skills/up/SKILL.md
    skill_file(tmp_path / 'skills' / 'SKILL.md', 'name: up\ncontext: fork')
    skill_file(tmp_path / '.claude' / 'skills' / 'up' / 'SKILL.md', 'name: up\ncontext: fork')
    (tmp_path / 'x').mkdir()
    for name in ('..', 'x/..:up', '.:up', './x', '../project:foreign', '../project/foreign'):
        assert run(skill(name, tmp_path), env, '--mode=enforce') is None, name


def test_personal_skill_before_project_skill(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, personal={'deploy': 'name: deploy\ndescription: inline'},
                       project={'deploy': 'name: deploy\ncontext: fork'})
    assert run(skill('deploy', tmp_path), env, '--mode=enforce') is None
    assert gate_log(tmp_path) == []


def test_skill_found_by_frontmatter_name_in_any_case(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, project={'other-folder': 'name: Quick\ncontext: fork'})
    assert is_warning(run(skill('quick', tmp_path), env, '--mode=enforce'))


def test_command_file_with_fork_is_checked_skill_takes_precedence(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, personal={'both': 'name: both\ndescription: inline'},
                       commands={'cmd-only': 'context: fork', 'both': 'context: fork'})
    assert is_warning(run(skill('cmd-only', tmp_path), env, '--mode=enforce'))
    assert run(skill('both', tmp_path), env, '--mode=enforce') is None


def test_fork_skill_to_an_exception_agent_is_allowed(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, personal={'guide': 'name: guide\ncontext: fork\nagent: claude-code-guide'})
    out = run(skill('guide', tmp_path), env, '--mode=enforce')
    assert is_warning(out) and 'exception' in context(out)


def test_missing_and_unreadable_skill_folders_are_skipped(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, project={'there': 'name: there\ncontext: fork\nagent: full-def'})
    locked = tmp_path / 'locked'
    locked.mkdir()
    locked.chmod(0)
    try:
        env['CCUC_SKILL_DIRS'] = os.pathsep.join([str(tmp_path / 'missing'), str(locked), env['CCUC_SKILL_DIRS']])
        assert run(skill('there', tmp_path), env, '--mode=enforce') is None
        assert gate_log(tmp_path)[-1]['decision'] == 'allow'
    finally:
        locked.chmod(0o700)


def test_fork_in_a_parent_folder_up_to_the_repo_root_is_found(tmp_path, env):
    usage(tmp_path, 80)
    (tmp_path / 'repo' / '.git').mkdir(parents=True)
    skill_file(tmp_path / 'repo' / '.claude' / 'skills' / 'root-fork' / 'SKILL.md', 'name: root-fork\ncontext: fork')
    (tmp_path / 'repo' / 'sub' / 'deep').mkdir(parents=True)
    out = run(skill('root-fork', tmp_path / 'repo' / 'sub' / 'deep'), without_skill_env(env), '--mode=enforce')
    assert is_deny(out)  # a fork counts as an agent start, 80 % → no start


def test_worktree_without_skills_folder_uses_the_main_checkout(tmp_path, env):
    usage(tmp_path, 80)
    (tmp_path / 'main' / '.git' / 'worktrees' / 'wt').mkdir(parents=True)
    skill_file(tmp_path / 'main' / '.claude' / 'skills' / 'main-fork' / 'SKILL.md', 'name: main-fork\ncontext: fork')
    (tmp_path / 'wt' / 'sub').mkdir(parents=True)
    (tmp_path / 'wt' / '.git').write_text(f'gitdir: {tmp_path}/main/.git/worktrees/wt\n', encoding='utf-8')
    env = without_skill_env(env)
    assert is_deny(run(skill('main-fork', tmp_path / 'wt' / 'sub'), env, '--mode=enforce'))
    (tmp_path / 'wt' / '.claude' / 'skills').mkdir(parents=True)  # own skills folder → main checkout does not count
    assert run(skill('main-fork', tmp_path / 'wt' / 'sub'), env, '--mode=enforce') is None


def test_large_and_non_utf8_skill_files_hide_no_fork(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env)
    base = tmp_path / 'skills' / 'personal'
    skill_file(base / 'large' / 'SKILL.md', None, ('---\nname: large\ncontext: fork\n---\n' + 'x' * 300_000).encode())
    skill_file(base / 'latin' / 'SKILL.md', None, '---\nname: latin\ncontext: fork\ndescription: caf\xe9\n---\n'.encode('latin-1'))
    for name in ('large', 'latin'):
        assert is_warning(run(skill(name, tmp_path), env, '--mode=enforce')), name


def test_local_colon_names_are_checked_plugins_are_not(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, commands={})
    skill_file(tmp_path / 'commands' / 'grp' / 'cmd.md', 'context: fork')
    project = tmp_path / 'project'
    skill_file(project / 'apps' / 'web' / '.claude' / 'skills' / 'deploy' / 'SKILL.md', 'name: deploy\ncontext: fork')
    assert is_warning(run(skill('grp:cmd', project), env, '--mode=enforce'))
    assert is_warning(run(skill('apps/web:deploy', project), env, '--mode=enforce'))
    assert run(skill('a-plugin:review', project), env, '--mode=enforce') is None
    assert run(skill('apps/web', project), env, '--mode=enforce') is None  # path without ':'


@pytest.mark.parametrize('head', ['"context": fork', 'context: >-\n  fork', 'context: |\n  fork', 'context: !!str fork',
                                  'context: &a fork', 'context:\n  fork', 'context: Fork', "context: 'fork' # comment",
                                  # each of these is context == 'fork' for a YAML parser
                                  'context: "f\\x6frk"', 'context: "\\u0066ork"', '"cont\\x65xt": fork',
                                  'x: &a fork\ncontext: *a', 'x: &k context\n*k : fork',
                                  'context: !<tag:yaml.org,2002:str> fork', 'context: &a !!str fork',
                                  'context: "fo\\\n  rk"', 'context: !!str |\n  fork',
                                  # a comment or an empty value, fork on one of the following lines
                                  'context: # c\n  fork', 'context: !!str # c\n  fork', 'context:   # c "x"\n  "fork"',
                                  'context: #\n fork', 'context: # a\n  # b\n\n  fork', 'context: |- # c\n  fork',
                                  'context: &a # c\n  fork', 'context:\n\n  fork'])
def test_yaml_spellings_of_fork_are_recognised(tmp_path, env, head):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, personal={'variant': f'name: variant\n{head}'})
    out = run(skill('variant', tmp_path), env, '--mode=enforce')
    assert is_warning(out) and 'Skill "variant" (context: fork)' in context(out), head
    usage(tmp_path, 97)
    assert is_deny(run(skill('variant', tmp_path), env, '--mode=enforce')), head


def test_fork_behind_a_frontmatter_beyond_the_read_limit_is_counted(tmp_path, env):
    usage(tmp_path, 97)
    env = setup_skills(tmp_path, env, personal={'big': 'name: big\ndescription: ' + 'x' * 70_000 + '\ncontext: fork'})
    out = run(skill('big', tmp_path), env, '--mode=enforce')
    assert is_deny(out) and '5h window at 97 %' in reason(out)


def test_skill_with_arguments_is_recognised_by_the_first_word(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, personal={'old': 'name: old\ncontext: fork'})
    assert is_warning(run(skill('/old --fast please', tmp_path), env, '--mode=enforce'))


def test_fork_skill_with_model_check_off_counts_without_notes(tmp_path, env):
    usage(tmp_path, 10)
    env = setup_skills(tmp_path, env, personal={'old': 'name: old\ncontext: fork'})
    env = config(tmp_path, env, {'model_effort_action': 'off'})
    assert run(skill('old', tmp_path), env, '--mode=enforce') is None
    usage(tmp_path, 80)
    assert is_deny(run(skill('old', tmp_path), env, '--mode=enforce'))


# --- In process: internal errors and decide() -----------------------------------------------------------------

@pytest.mark.parametrize('tool,expected', [('Skill', 'warn'), ('Agent', 'deny')])
def test_internal_error_warns_for_skills_denies_otherwise(tmp_path, monkeypatch, capsys, tool, expected):
    def broken(*_a, **_k):
        raise RuntimeError('probe')
    monkeypatch.setattr(spawn_gate, 'decide', broken)
    monkeypatch.setattr(sys, 'argv', ['spawn_gate.py', '--mode=enforce'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'tool_name': tool, 'tool_input': {'skill': 'x'}})))
    assert spawn_gate.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert (is_warning(out) if expected == 'warn' else is_deny(out)) and 'RuntimeError' in json.dumps(out)
    assert gate_log(tmp_path)[-1]['decision'] == expected


def test_internal_error_in_shadow_mode_is_only_logged(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(spawn_gate, 'decide', lambda *_a, **_k: 1 / 0)
    monkeypatch.setattr(sys, 'argv', ['spawn_gate.py'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'tool_name': 'Workflow', 'tool_input': {}})))
    assert spawn_gate.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert is_shadow_note(out, 'would have been denied') and 'ZeroDivisionError' in context(out)
    line = gate_log(tmp_path)[-1]
    assert line['decision'] == 'deny' and line['mode'] == 'shadow' and 'ZeroDivisionError' in line['reason']


def test_not_determinable_also_appears_in_the_deny_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(_spawn_check, 'MAX_FILES', 1)
    for n in ('a', 'b'):
        (tmp_path / 'skills' / n).mkdir(parents=True)
    monkeypatch.setenv('CCUC_SKILL_DIRS', str(tmp_path / 'skills'))
    monkeypatch.setenv('CCUC_AGENT_DIRS', str(tmp_path / 'agents'))
    usage(tmp_path, 80)
    decision, text = spawn_gate.decide(skill('inline-or-not', tmp_path), _config.load())
    assert decision == 'deny' and '5h' in text and 'cannot be determined' in text


def test_decide_uses_the_given_clock_and_date(tmp_path, monkeypatch):
    monkeypatch.setenv('CCUC_AGENT_DIRS', str(tmp_path / 'agents'))
    measured = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
    (tmp_path / 'usage.json').write_text(json.dumps({
        'five_hour': {'used_percentage': 10, 'resets_at': measured.timestamp() + 3600},
        'seven_day': {'used_percentage': 90, 'resets_at': measured.timestamp() + 86400},
        'measured_at': '2026-01-14T10:00:00Z'}), encoding='utf-8')
    cfg = dict(_config.load(), spawn_rules=dict(_config.DEFAULTS['spawn_rules'], model_effort_action='off'),
               thresholds=THRESHOLDS)
    now = measured + timedelta(minutes=5)
    assert spawn_gate.decide(agent(), cfg, DAY, DAY, now)[0] == 'warn'
    assert spawn_gate.decide(agent(), cfg, DAY, DAY + timedelta(days=1), now)[0] == 'deny'
    assert spawn_gate.decide(agent(), cfg, DAY, DAY, now + timedelta(minutes=20))[1].startswith('Usage stale')


# --- decide_start: the usage thresholds -------------------------------------------------------------------------

def measured(five, week=20.0, age=1.0):
    return _usage.Usage(five, week, age)


def test_start_allowed_below_the_5h_threshold():
    assert spawn_gate.decide_start(measured(74.9), 1, THRESHOLDS) == ('allow', '')


def test_start_denied_at_the_5h_threshold():
    action, text = spawn_gate.decide_start(measured(75), 1, THRESHOLDS)
    assert action == 'deny' and text == '5h window at 75 %: no new block until the window resets.'


def test_start_denied_at_the_week_threshold():
    assert spawn_gate.decide_start(measured(10, week=75), 1, THRESHOLDS)[0] == 'deny'
    assert spawn_gate.decide_start(measured(10, week=74), 1, THRESHOLDS)[0] == 'allow'


def test_start_with_unknown_usage_fails_open_with_a_warning():
    action, text = spawn_gate.decide_start(_usage.Usage(None, None, None), 1, THRESHOLDS)
    assert action == 'warn' and text == 'Usage unknown: start allowed (fail open), keep blocks small.'


def test_start_with_unknown_or_stale_usage_no_block_with_several_agents():
    for usage_now in (_usage.Usage(None, None, None), measured(10, age=60), measured(10, age=-5)):
        action, text = spawn_gate.decide_start(usage_now, 6, THRESHOLDS)
        assert action == 'deny' and 'fresh measurement first' in text and f'Usage {usage_now.state}' in text
        assert spawn_gate.decide_start(usage_now, 2, THRESHOLDS)[0] == 'deny'
        assert spawn_gate.decide_start(usage_now, None, THRESHOLDS)[0] == 'deny'
        assert spawn_gate.decide_start(usage_now, 1, THRESHOLDS)[0] == 'warn'
        assert spawn_gate.decide_start(usage_now, 0, THRESHOLDS)[0] == 'warn'


def test_week_ok_until_up_to_and_including_the_day_only_warns():
    ok = date(2026, 1, 15)
    action, text = spawn_gate.decide_start(measured(10, week=85), 1, THRESHOLDS, ok, date(2026, 1, 15))
    assert action == 'warn' and '85 %' in text and '2026-01-15' in text
    assert spawn_gate.decide_start(measured(10, week=85), 1, THRESHOLDS, ok, date(2026, 1, 16))[0] == 'deny'
    assert spawn_gate.decide_start(measured(80, week=85), 1, THRESHOLDS, ok, DAY)[0] == 'deny'  # 5h stays
    assert 'week-ok-until' in spawn_gate.decide_start(measured(10, week=85), 1, THRESHOLDS)[1]


MODES = ['default', 'acceptEdits', 'plan', 'bypassPermissions', 'dontAsk', 'auto', 'somethingNew', None]


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('verified', [False, True])
@pytest.mark.parametrize('week_action', ['warn', 'ask', 'deny'])
def test_week_action_for_every_permission_mode(week_action, verified, mode):
    limits = dict(THRESHOLDS, week_action=week_action, ask_verified_in_bypass=verified)
    action, text = spawn_gate.decide_start(measured(10, week=85), 1, limits, None, DAY, mode)
    if week_action == 'ask':
        shown = mode in ('default', 'acceptEdits', 'plan') or verified
        expected = 'ask' if shown else 'deny'
    else:
        expected = week_action
    assert action == expected and (text == 'WEEK 85 % · start agent?' if expected == 'ask' else '7-day window at 85 %' in text)
    if expected == 'deny' and week_action == 'ask':
        assert f'permission mode "{mode or "unknown"}"' in text and 'ask_verified_in_bypass is false' in text


@pytest.mark.parametrize('week_action', ['warn', 'ask', 'deny'])
def test_week_action_below_the_threshold_and_with_approval_does_not_apply(week_action):
    limits = dict(THRESHOLDS, week_action=week_action)
    assert spawn_gate.decide_start(measured(10, week=74), 1, limits, None, DAY, 'default') == ('allow', '')
    action, text = spawn_gate.decide_start(measured(10, week=85), 1, limits, DAY, DAY, 'bypassPermissions')
    assert action == 'warn' and 'start approved until 2026-01-14' in text


def test_the_5h_block_beats_a_weekly_ask_and_unknown_usage_never_asks():
    limits = dict(THRESHOLDS, week_action='ask')
    assert spawn_gate.decide_start(measured(80, week=85), 1, limits, None, DAY, 'default')[0] == 'deny'
    assert spawn_gate.decide_start(_usage.Usage(None, None, None), 1, limits, None, DAY, 'default')[0] == 'warn'


def test_ask_reaches_claude_code_as_a_permission_prompt(tmp_path, env):
    usage(tmp_path, 10, week=85)
    env = config(tmp_path, env, thresholds={'week_action': 'ask'})
    out = run(dict(agent('full-def'), permission_mode='default'), env, '--mode=enforce')
    assert out is not None and list(out) == ['hookSpecificOutput']
    # No additionalContext: the model reads it next to the tool result and took an approved start for a block
    assert set(out['hookSpecificOutput']) == {'hookEventName', 'permissionDecision', 'permissionDecisionReason',
                                              'updatedInput'}
    assert out['hookSpecificOutput']['permissionDecision'] == 'ask'
    assert reason(out) == 'WEEK 85 % · start full-def?'
    # The phone app shows the description, not the question: the number goes there too, every other field unchanged
    assert out['hookSpecificOutput']['updatedInput'] == {'prompt': 'secret task', 'description': 'WEEK 85 % · x',
                                                         'subagent_type': 'full-def'}
    assert is_deny(run(dict(agent('full-def'), permission_mode='bypassPermissions'), env, '--mode=enforce'))
    assert is_deny(run(agent('full-def'), env, '--mode=enforce'))                     # no mode in the input
    assert is_shadow_note(run(dict(agent('full-def'), permission_mode='default'), env, '--mode=shadow'),
                          "would have needed the user's approval")
    lines = gate_log(tmp_path)
    assert [line['decision'] for line in lines] == ['ask', 'deny', 'deny', 'ask']
    assert [line['permission_mode'] for line in lines] == ['default', 'bypassPermissions', None, 'default']


def test_the_ask_dialog_speaks_the_language_of_claude_codes_settings(tmp_path, env):
    usage(tmp_path, 10, week=85.4)
    env = config(tmp_path, env, thresholds={'week_action': 'ask'})
    settings = os.path.join(os.environ['HOME'], '.claude', 'settings.json')
    os.makedirs(os.path.dirname(settings))
    with open(settings, 'w', encoding='utf-8') as f:
        json.dump({'language': 'German'}, f)
    out = run(dict(agent('full-def'), permission_mode='default'), env, '--mode=enforce')
    assert out['hookSpecificOutput']['permissionDecision'] == 'ask' and reason(out) == 'WOCHE 85 % · full-def starten?'
    assert out['hookSpecificOutput']['updatedInput']['description'] == 'WOCHE 85 % · x'
    # Only the question is translated: a denial goes to the model and stays English
    denied = run(dict(agent('full-def'), permission_mode='bypassPermissions'), env, '--mode=enforce')
    assert is_deny(denied) and reason(denied).startswith('7-day window at 85 %: the user has to approve')


@pytest.mark.parametrize('description,expected', [
    ('Check the logs', 'WOCHE 92 % · Check the logs'),
    (None, 'WOCHE 92 %'),
    ('', 'WOCHE 92 %'),
    ('  ', 'WOCHE 92 %'),
    (['list'], 'WOCHE 92 %'),
])
def test_dialog_input_puts_the_number_of_the_question_in_front_of_the_description(description, expected):
    tool_input = {'prompt': 'p', 'subagent_type': 'arbeiter-bauen', 'run_in_background': True, 'model': 'sonnet'}
    if description is not None:
        tool_input['description'] = description
    before = dict(tool_input)
    question = 'WOCHE 92 % · arbeiter-bauen starten? "half-def" allowed as an exception.'
    assert spawn_gate.dialog_input('Agent', tool_input, question) == dict(before, description=expected)
    assert tool_input == before                                               # the input it got stays as it was


@pytest.mark.parametrize('language', ['en', 'de'])
def test_dialog_input_takes_the_number_from_either_question(language):
    label = spawn_gate.WEEK_LABELS[language].format(week=85.4)
    question = spawn_gate.QUESTIONS[language].format(labels=label, what='x')
    assert spawn_gate.dialog_input('Agent', {'description': 'd'}, question) == {'description': f'{label} · d'}


@pytest.mark.parametrize('tool,tool_input', [
    ('Skill', {'skill': 'review'}), ('Workflow', {'script': 'x'}), ('Agent', None), ('Agent', ['list']),
])
def test_dialog_input_leaves_skills_workflows_and_broken_inputs_alone(tool, tool_input):
    # Skill and Workflow have no description to show; a broken input would be replaced by a lone description
    assert spawn_gate.dialog_input(tool, tool_input, 'WEEK 85 % · start x?') is None


@pytest.mark.parametrize('content,expected', [
    ({'language': 'German'}, 'de'), ({'language': ' deutsch '}, 'de'), ({'language': 'DE'}, 'de'),
    ({'language': 'English'}, 'en'), ({'language': 'japanese'}, 'en'), ({'language': 7}, 'en'), ({}, 'en'),
    (['German'], 'en'), (None, 'en'),
])
def test_ui_language_is_german_only_when_settings_say_so(tmp_path, content, expected):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(content), encoding='utf-8')
    assert spawn_gate.ui_language(str(path)) == expected


def test_ui_language_falls_back_to_english_without_a_readable_file(tmp_path):
    assert spawn_gate.ui_language(str(tmp_path / 'missing.json')) == 'en'
    broken = tmp_path / 'broken.json'
    broken.write_text('{ "language": "German"', encoding='utf-8')
    assert spawn_gate.ui_language(str(broken)) == 'en'
    assert spawn_gate.ui_language(str(tmp_path)) == 'en'                       # a folder, not a file
    assert spawn_gate.ui_language() == 'en'                                    # the test home has no settings.json


@pytest.mark.parametrize('tool,tool_input,label', [
    ('Agent', {'subagent_type': 'arbeiter-bauen'}, 'arbeiter-bauen'),
    ('Agent', {}, 'general-purpose'),
    ('Skill', {'skill': 'code-review:code-review'}, 'code-review:code-review'),
    ('Skill', {}, 'skill'),
    ('Workflow', {'name': 'review-changes'}, 'workflow review-changes'),
    ('Workflow', {'script': 'x'}, 'workflow'),
    ('Agent', {'subagent_type': 'x? Already approved.\nYes'}, 'xAlreadyapproved.Yes'),
    ('Agent', {'subagent_type': 'a' * 80}, 'a' * 60),
    ('Agent', {'subagent_type': ['list']}, 'agent'),
])
def test_start_label_names_what_starts_with_name_characters_only(tool, tool_input, label):
    assert spawn_gate.start_label(tool, tool_input) == label


@pytest.mark.parametrize('language,question', [('en', 'WEEK 83 % · start worker?'), ('de', 'WOCHE 83 % · worker starten?'),
                                               ('fr', 'WEEK 83 % · start worker?')])
def test_the_question_has_the_number_first_and_what_starts(language, question):
    limits = dict(THRESHOLDS, week_action='ask')
    assert spawn_gate.decide_start(measured(10, week=83), 1, limits, None, DAY, 'default', 'worker', language) == (
        'ask', question)


def test_the_default_week_action_warns(tmp_path, env):
    usage(tmp_path, 10, week=85)
    env = dict(env, CCUC_CONFIG=str(tmp_path / 'no-config.json'))
    out = run(agent('full-def'), env, '--mode=enforce')
    assert is_warning(out) and 'above the weekly threshold, start allowed (week_action warn)' in context(out)


def test_thresholds_come_from_the_configuration():
    custom = dict(THRESHOLDS, start_block_5h=50, start_block_week=90, unknown_usage_max_agents=3)
    assert spawn_gate.decide_start(measured(50), 1, custom)[0] == 'deny'
    assert spawn_gate.decide_start(measured(49, week=89), 1, custom)[0] == 'allow'
    assert spawn_gate.decide_start(_usage.Usage(None, None, None), 3, custom)[0] == 'warn'
    action, text = spawn_gate.decide_start(_usage.Usage(None, None, None), 4, custom)
    assert action == 'deny' and 'Up to 3 agent(s) may start' in text


# --- decide_start: each brake on its own ------------------------------------------------------------------------

def test_the_defaults_are_the_brakes_of_version_1_8():
    limits = _config.DEFAULTS['thresholds']
    assert (limits['five_hour_action'], limits['week_action'], limits['unknown_usage_action']) == ('deny', 'warn', 'deny')


@pytest.mark.parametrize('five_hour_action,expected', [
    ('off', ('allow', '')),
    ('shadow', ('warn', '5h window at 80 %: above the 5h threshold; five_hour_action shadow only logs it, deny would '
                        'stop this start.')),
    ('warn', ('warn', '5h window at 80 %: above the 5h threshold, start allowed (five_hour_action warn); keep blocks '
                      'small.')),
    ('ask', ('ask', '5H 80 % · start agent?')),
    ('deny', ('deny', '5h window at 80 %: no new block until the window resets.')),
])
def test_five_hour_action_decides_above_the_5h_threshold(five_hour_action, expected):
    limits = dict(THRESHOLDS, five_hour_action=five_hour_action)
    assert spawn_gate.decide_start(measured(80), 1, limits, None, DAY, 'default') == expected
    assert spawn_gate.decide_start(measured(74), 1, limits, None, DAY, 'default') == ('allow', '')


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('verified', [False, True])
def test_a_5h_ask_needs_a_mode_that_shows_the_prompt(verified, mode):
    limits = dict(THRESHOLDS, five_hour_action='ask', ask_verified_in_bypass=verified)
    action, text = spawn_gate.decide_start(measured(80), 1, limits, None, DAY, mode)
    if mode in spawn_gate.ASK_SHOWN or verified:
        assert (action, text) == ('ask', '5H 80 % · start agent?')
    else:
        assert action == 'deny' and text.startswith('5h window at 80 %: the user has to approve this start')
        assert f'permission mode "{mode or "unknown"}"' in text and 'no new block until the window resets' in text


@pytest.mark.parametrize('language,question', [('en', 'WEEK 85 % / 5H 80 % · start worker?'),
                                               ('de', 'WOCHE 85 % / 5H 80 % · worker starten?')])
def test_both_windows_asking_make_one_question_with_both_numbers(language, question):
    limits = dict(THRESHOLDS, week_action='ask', five_hour_action='ask')
    assert spawn_gate.decide_start(measured(80, week=85), 1, limits, None, DAY, 'default', 'worker', language) == (
        'ask', question)
    label = question.split(' · ', 1)[0]
    assert spawn_gate.dialog_input('Agent', {'description': 'd'}, question) == {'description': f'{label} · d'}


def test_the_strongest_brake_wins():
    def decide(week_action, five_hour_action):
        limits = dict(THRESHOLDS, week_action=week_action, five_hour_action=five_hour_action)
        return spawn_gate.decide_start(measured(80, week=85), 1, limits, None, DAY, 'default')[0]
    assert decide('ask', 'deny') == decide('deny', 'ask') == decide('deny', 'off') == 'deny'
    assert decide('warn', 'ask') == decide('ask', 'shadow') == 'ask'
    assert decide('warn', 'shadow') == decide('off', 'warn') == 'warn'
    assert decide('off', 'off') == 'allow'


@pytest.mark.parametrize('week_action,week_ok_until,note', [
    ('shadow', None, 'week_action shadow only logs it'), ('warn', None, '(week_action warn)'),
    ('deny', DAY, 'start approved until 2026-01-14')])
def test_a_5h_ask_keeps_the_note_of_the_week(week_action, week_ok_until, note):
    limits = dict(THRESHOLDS, week_action=week_action, five_hour_action='ask')
    action, text = spawn_gate.decide_start(measured(80, week=90), 1, limits, week_ok_until, DAY, 'default')
    assert action == 'ask' and text.startswith('5H 80 % · start agent? ') and note in text


def test_both_notes_reach_the_model_when_both_brakes_only_warn():
    limits = dict(THRESHOLDS, week_action='shadow', five_hour_action='warn')
    action, text = spawn_gate.decide_start(measured(80, week=85), 1, limits, None, DAY, 'default')
    assert action == 'warn' and 'week_action shadow only logs it' in text and '(five_hour_action warn)' in text


@pytest.mark.parametrize('week_action,expected', [
    ('off', ('allow', '')),
    ('shadow', ('warn', '7-day window at 85 %: above the weekly threshold; week_action shadow only logs it, deny '
                        'would stop this start.')),
])
def test_week_action_off_and_shadow(week_action, expected):
    limits = dict(THRESHOLDS, week_action=week_action)
    assert spawn_gate.decide_start(measured(10, week=85), 1, limits, None, DAY, 'default') == expected


def test_week_action_off_ignores_the_approval_too():
    limits = dict(THRESHOLDS, week_action='off')
    assert spawn_gate.decide_start(measured(10, week=85), 1, limits, DAY, DAY, 'default') == ('allow', '')


@pytest.mark.parametrize('unknown_usage_action,many,one', [
    ('off', ('warn', 'Usage unknown: start allowed (fail open), keep blocks small.'), None),
    ('shadow', ('warn', 'Usage unknown and 3 agents in the block: unknown_usage_action shadow only logs it, deny would '
                        'stop this start (up to 1 agent(s) while the usage is unknown).'), None),
    ('warn', ('warn', 'Usage unknown and 3 agents in the block: start allowed (unknown_usage_action warn); take a fresh '
                      'measurement before the next block.'), None),
    ('deny', ('deny', 'Usage unknown and 3 agents in the block: fresh measurement first, then the block. Up to 1 '
                      'agent(s) may start while the usage is unknown.'), None),
])
def test_unknown_usage_action_decides_above_the_block_size(unknown_usage_action, many, one):
    limits = dict(THRESHOLDS, unknown_usage_action=unknown_usage_action)
    unknown = _usage.Usage(None, None, None)
    assert spawn_gate.decide_start(unknown, 3, limits) == many
    assert spawn_gate.decide_start(unknown, 1, limits) == (
        one or ('warn', 'Usage unknown: start allowed (fail open), keep blocks small.'))
    assert spawn_gate.decide_start(unknown, None, limits)[0] == many[0]


def test_model_effort_action_shadow_starts_and_names_the_finding(tmp_path, env):
    usage(tmp_path, 10)
    env = config(tmp_path, env, spawn_rules={'model_effort_action': 'shadow'})
    out = run(agent('half-def'), env, '--mode=enforce')
    assert is_warning(out) and '(model_effort_action shadow: logged only, deny would stop this start.)' in context(out)
    assert gate_log(tmp_path)[-1]['decision'] == 'warn'


def test_a_5h_ask_reaches_claude_code_as_a_permission_prompt(tmp_path, env):
    usage(tmp_path, 80)
    env = config(tmp_path, env, thresholds={'five_hour_action': 'ask'})
    out = run(dict(agent('full-def'), permission_mode='default'), env, '--mode=enforce')
    assert out['hookSpecificOutput']['permissionDecision'] == 'ask' and reason(out) == '5H 80 % · start full-def?'
    assert out['hookSpecificOutput']['updatedInput']['description'] == '5H 80 % · x'


def test_mode_parsing_without_hookio():
    assert spawn_gate._mode([]) == 'shadow'
    assert spawn_gate._mode(['--mode=enforce']) == 'enforce'
    assert spawn_gate._mode(['--mode=enforce', '--mode=shadow']) == 'shadow'
    assert spawn_gate._mode(['--mode=ENFORCE', 'enforce']) == 'shadow'


# --- Shadow mode: the hint ------------------------------------------------------------------------------------

def test_shadow_mode_stays_silent_when_enforce_would_allow(tmp_path, env):
    usage(tmp_path, 10)
    assert run(agent('full-def'), env, '--mode=enforce') is None
    assert run(agent('full-def'), env) is None
    assert gate_log(tmp_path)[-1]['decision'] == 'allow'


def test_shadow_mode_turns_a_warning_into_the_hint(tmp_path, env):
    usage(tmp_path, 10)
    env = config(tmp_path, env, spawn_rules={'model_effort_action': 'warn'})
    assert is_warning(run(agent(), env, '--mode=enforce'))
    out = run(agent(), env)
    assert is_shadow_note(out, 'would have come with a warning') and not context(out).startswith('Spawn gate: ')


def test_shadow_mode_hints_at_the_usage_block_with_its_numbers(tmp_path, env):
    usage(tmp_path, 80)
    out = run(agent('full-def'), env)
    assert is_shadow_note(out, 'would have been denied') and '80' in context(out)



# --- A planted special file never hangs the gate (L1) ---------------------------------------------------------------
# Claude Code ends a hook after the timeout of its entry (5 s) and, without onFailure "block", lets the call
# through: a gate that hangs on a named pipe allows. Each case must still deny, well within those 5 s.

def denies_in_time(tmp_path, env):
    usage(tmp_path, 80)
    started = time.monotonic()
    out = run(agent('full-def'), env, '--mode=enforce')
    assert is_deny(out) and '5h' in reason(out)
    assert time.monotonic() - started < 5


@pytest.mark.posix_only
def test_a_named_pipe_as_the_log_does_not_hang_the_deny(tmp_path, env):
    (tmp_path / 'log').mkdir()
    os.mkfifo(tmp_path / 'log' / 'log.jsonl')
    denies_in_time(tmp_path, env)


@pytest.mark.posix_only
def test_a_named_pipe_as_the_config_does_not_hang_the_deny(tmp_path, env):
    os.mkfifo(tmp_path / 'fifo-config.json')
    denies_in_time(tmp_path, dict(env, CCUC_CONFIG=str(tmp_path / 'fifo-config.json')))


@pytest.mark.posix_only
@pytest.mark.parametrize('kind', ['fifo', 'zero'])
def test_claude_codes_settings_as_a_named_pipe_or_an_endless_source_do_not_hang_the_deny(tmp_path, env, kind):
    settings = tmp_path / 'home' / '.claude' / 'settings.json'
    settings.parent.mkdir(parents=True)
    os.mkfifo(settings) if kind == 'fifo' else settings.symlink_to('/dev/zero')
    denies_in_time(tmp_path, env)
    assert spawn_gate.ui_language(str(settings)) == 'en'


def test_ui_language_of_a_settings_file_above_the_size_limit_is_english(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{"language": "German"}' + ' ' * _config.MAX_READ_BYTES, encoding='utf-8')
    assert spawn_gate.ui_language(str(path)) == 'en'


@pytest.mark.posix_only
def test_ui_language_follows_a_link_to_a_regular_settings_file(tmp_path):
    real = tmp_path / 'dotfiles.json'
    real.write_text('{"language": "German"}', encoding='utf-8')
    (tmp_path / 'settings.json').symlink_to(real)
    assert spawn_gate.ui_language(str(tmp_path / 'settings.json')) == 'de'
