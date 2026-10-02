"""Shared input and output of the hooks: arguments, hook input, directory discovery, output, JSONL log.

The mode comes from the hook command in settings.json (--mode=shadow|enforce, --week-ok-until=YYYY-MM-DD),
not from config.json. Where settings.json is protected against the model, the model cannot switch itself free.
Default is shadow: block nothing, only log.

Known limit: the usage file and the counters are NOT protected by this module. A model with write access to
them could fake them (0 % with a fresh timestamp, a reset counter) and get around the thresholds.

The log contains field names and decisions, NEVER prompts, commands or script text. Quoted values in a reason
(script parts, model literals, paths) are masked as '…'.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone

import _config

LOG_MAX_BYTES = 5 * 1024 * 1024


def parse_args(argv: list[str]) -> dict[str, str]:
    """`--key=value` arguments. `mode` defaults to 'shadow'; anything but 'enforce' becomes 'shadow'."""
    values = {'mode': 'shadow', 'week-ok-until': ''}
    for arg in argv:
        if arg.startswith('--') and '=' in arg:
            key, value = arg[2:].split('=', 1)
            values[key] = value
    if values['mode'] not in ('shadow', 'enforce'):
        values['mode'] = 'shadow'
    return values


def parse_date(text: str | None) -> date | None:
    """--week-ok-until=YYYY-MM-DD from settings.json. Invalid or empty: None (no approval)."""
    if not isinstance(text, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def read_input() -> dict:
    try:
        data = json.load(sys.stdin)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError, RecursionError):
        return {}


def _paths_from_env(name: str) -> list[str] | None:
    value = os.environ.get(name)
    return None if value is None else [d for d in value.split(os.pathsep) if d]


def agent_dirs(hook_input: dict) -> list[str]:
    """Search order = precedence: project agents before user agents.

    Project agents live in the project root (CLAUDE_PROJECT_DIR). The cwd from the input moves with every cd
    and stays only as a fallback if the variable is missing.
    """
    override = _paths_from_env('CCUC_AGENT_DIRS')
    if override:
        return override
    cwd = hook_input.get('cwd')
    roots = [os.environ.get('CLAUDE_PROJECT_DIR'), cwd if isinstance(cwd, str) and cwd else os.getcwd()]
    project = [os.path.join(r, '.claude', 'agents') for r in dict.fromkeys(r for r in roots if r)]
    return project + [os.path.join(os.path.expanduser('~'), '.claude', 'agents')]


def _project_roots(start: str) -> list[str]:
    """`start` and every parent up to the repo or worktree root (a folder with .git), nearest first.

    A worktree without its own skills folder at its root is followed by the main checkout. Without a .git
    above, only `start` itself is returned.
    """
    chain, folder = [], os.path.abspath(start)
    while True:
        chain.append(folder)
        git = os.path.join(folder, '.git')
        if os.path.exists(git):
            if os.path.isfile(git) and not os.path.isdir(os.path.join(folder, '.claude', 'skills')):
                try:
                    with open(git, encoding='utf-8', errors='replace') as f:
                        line = f.read(4096).strip()
                except OSError:
                    line = ''
                target = line[len('gitdir:'):].strip() if line.startswith('gitdir:') else ''
                marker = os.sep + '.git' + os.sep + 'worktrees' + os.sep
                if marker in target:
                    chain.append(target.split(marker)[0])
            return chain
        parent = os.path.dirname(folder)
        if parent == folder:
            return [os.path.abspath(start)]
        folder = parent


def skill_dirs(hook_input: dict) -> tuple[list[str], list[str]]:
    """(skill folders, command folders) in precedence order: personal before project.

    Project = the start folder and its parents up to the repo root. Plugins, managed skills, --add-dir and
    skills in folders below the start folder are not covered.
    """
    skills, commands = _paths_from_env('CCUC_SKILL_DIRS'), _paths_from_env('CCUC_COMMAND_DIRS')
    if skills is not None or commands is not None:
        return skills or [], commands or []
    cwd = hook_input.get('cwd')
    starts = [s for s in (os.environ.get('CLAUDE_PROJECT_DIR'), cwd if isinstance(cwd, str) and cwd else os.getcwd()) if s]
    roots = [os.path.expanduser('~')] + list(dict.fromkeys(r for s in starts for r in _project_roots(s)))
    return ([os.path.join(r, '.claude', 'skills') for r in roots],
            [os.path.join(r, '.claude', 'commands') for r in roots])


def emit_deny(event: str, reason: str) -> None:
    print(json.dumps({'hookSpecificOutput': {'hookEventName': event, 'permissionDecision': 'deny',
                                             'permissionDecisionReason': reason}}, ensure_ascii=False))


def emit_ask(event: str, reason: str, context: str) -> None:
    """Claude Code shows its permission prompt; the reason goes to the user, the context to the model."""
    print(json.dumps({'hookSpecificOutput': {'hookEventName': event, 'permissionDecision': 'ask',
                                             'permissionDecisionReason': reason, 'additionalContext': context}},
                     ensure_ascii=False))


def emit_context(event: str, text: str) -> None:
    """A warning without denial: reaches the model as a system reminder (additionalContext)."""
    print(json.dumps({'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}, ensure_ascii=False))


def log_dir() -> str:
    """Env CCUC_LOG_DIR, else config `log_dir`."""
    return os.path.expanduser(os.environ.get('CCUC_LOG_DIR') or _config.load(log_problems=False)['log_dir'])


def write_log_line(record: dict, directory: str | None = None) -> None:
    """Appends one JSON line to <log_dir>/log.jsonl, rotating to log.jsonl.1 above 5 MB.

    Errors while logging must never disturb a hook.
    """
    try:
        line = json.dumps(record, ensure_ascii=False) + '\n'
        directory = directory or log_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, 'log.jsonl')
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + '.1')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line)
    except (OSError, TypeError, ValueError):
        pass


# Quoted parts of a reason carry values from the input: script parts, model literals, paths
_QUOTED = re.compile('"[^"]*"|\'[^\']*\'|„[^“”]*[“”]|`[^`]*`')
_QUOTE_CHAR = re.compile('["\'„“”`]')


def mask_quoted(text: str) -> str:
    """Every quoted part becomes '…'. A quote without its partner cuts the text there (fail closed)."""
    masked = _QUOTED.sub('…', text)
    stray = _QUOTE_CHAR.search(masked)
    return masked if stray is None else masked[:stray.start()] + '…'


def _short_text(value) -> str | None:
    """Only short strings are logged verbatim; anything else could carry input content."""
    return value[:100] if isinstance(value, str) else None


def log_decision(hook: str, mode: str, hook_input: dict, decision: str, reason: str, **extra) -> None:
    """One line per decision. Contains the KEYS of the input, never its values. The reason is logged with its
    quoted parts masked; the hook output to the model keeps them."""
    raw = hook_input.get('tool_input')
    tool_input = raw if isinstance(raw, dict) else {}
    write_log_line({
        'time': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'hook': hook, 'mode': mode,
        'decision': decision, 'reason': mask_quoted(reason)[:300],
        'tool_name': _short_text(hook_input.get('tool_name')), 'agent_id': _short_text(hook_input.get('agent_id')),
        'agent_type': _short_text(hook_input.get('agent_type')),
        'input_keys': sorted(hook_input.keys()), 'tool_input_keys': sorted(tool_input.keys()),
        'subagent_type': _short_text(tool_input.get('subagent_type')),
        'model_in_call': _short_text(tool_input.get('model')), **extra,
    })
