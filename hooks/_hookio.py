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
import stat
import sys
from datetime import date, datetime, timezone

import _config

if os.name == 'nt':
    import _plat      # the Windows side of the platform switch (windows/cli/_win.py)

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
    """Search order = precedence: project agents, the deepest folder first, before user agents.

    As Claude Code reads them: from the project root (CLAUDE_PROJECT_DIR) and each parent up to the repo root (a
    folder with .git), without one up to the home folder, which is never a project folder itself. Never from the cwd
    of the input, which moves with every cd while Claude Code keeps reading the project root; it stands in for the
    root only if the variable is missing.
    """
    override = _paths_from_env('CCUC_AGENT_DIRS')
    if override:
        return override
    cwd = hook_input.get('cwd')
    start = os.environ.get('CLAUDE_PROJECT_DIR') or (cwd if isinstance(cwd, str) and cwd else os.getcwd())
    home = os.path.expanduser('~')
    chain, folder = [], os.path.abspath(start)
    while os.path.normcase(folder) != os.path.normcase(os.path.abspath(home)):
        chain.append(os.path.join(folder, '.claude', 'agents'))
        parent = os.path.dirname(folder)
        if os.path.exists(os.path.join(folder, '.git')) or parent == folder:
            break
        folder = parent
    return chain + [os.path.join(home, '.claude', 'agents')]


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
                marker = '/.git/worktrees/'
                if os.name == 'nt':      # git writes `gitdir: C:/...` with forward slashes, the folders here have backslashes
                    target = _plat.normalize_path(target)
                if marker in target:
                    root = target.split(marker)[0]
                    chain.append(_plat.native_path(root) if os.name == 'nt' else root)
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


def emit_ask(event: str, reason: str, updated_input: dict | None = None) -> None:
    """Claude Code shows its permission prompt with the reason to the user. No additionalContext: the model reads
    it next to the tool result and took an approved start for a block. `updated_input` replaces the whole tool
    input, and the prompt shows it."""
    output = {'hookEventName': event, 'permissionDecision': 'ask', 'permissionDecisionReason': reason}
    if updated_input is not None:
        output['updatedInput'] = updated_input
    print(json.dumps({'hookSpecificOutput': output}, ensure_ascii=False))


def emit_context(event: str, text: str) -> None:
    """A warning without denial: reaches the model as a system reminder (additionalContext)."""
    print(json.dumps({'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}, ensure_ascii=False))


SHADOW_PHRASES = {'deny': 'would have been denied', 'ask': "would have needed the user's approval",
                  'warn': 'would have come with a warning'}


def shadow_note(gate: str, subject: str, action: str, reason: str, tell: str) -> str:
    """What a gate in shadow mode tells the model instead of deciding: what enforce would have done and why. The
    model reads a PreToolUse context next to the tool result, so it is said in the past."""
    reason = reason.strip() or '(none)'
    if reason[-1] not in '.?!':
        reason += '.'
    return (f'{gate} (shadow mode, information only): in enforce mode {subject} {SHADOW_PHRASES[action]}. '
            f'Reason: {reason} Nothing was blocked. {tell} The gates stay in shadow mode until the user runs '
            'tools/install.py --mode enforce.')


def log_dir() -> str:
    """Env CCUC_LOG_DIR, else config `log_dir`."""
    return os.path.expanduser(os.environ.get('CCUC_LOG_DIR') or _config.load(log_problems=False)['log_dir'])


def write_log_line(record: dict, directory: str | None = None) -> None:
    """Appends one JSON line to <log_dir>/log.jsonl, rotating to log.jsonl.1 above 5 MB.

    Folder 0700, file 0600 whatever the umask, and never written through a link. A log left readable by an
    earlier version is made private on the next write, a named pipe or any other non-regular file gets no line.
    Errors while logging must never disturb a hook.
    """
    try:
        line = json.dumps(record, ensure_ascii=False) + '\n'
        directory = directory or log_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, 'log.jsonl')
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            (_plat.replace if os.name == 'nt' else os.replace)(path, path + '.1')      # Windows: tried again for a moment
        # O_NOFOLLOW here rather than from _usage: logging must not depend on the module it may have to report.
        # O_NONBLOCK: a named pipe planted as the log must not hang the gate (it logs before it denies) into Claude
        # Code's timeout, which lets the call through; without a reader the open fails, with one S_ISREG skips it.
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0), 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            return
        with os.fdopen(descriptor, 'a', encoding='utf-8', newline='\n') as f:      # no CRLF on Windows
            if os.name != 'nt':      # Windows has no file modes
                os.fchmod(descriptor, 0o600)
            f.write(line)
    except (OSError, TypeError, ValueError):
        pass


DISABLED_SESSIONS = 50      # sessions per hook whose line `disabled` is remembered (<log_dir>/disabled/<hook>.json)
DISABLED_MAX_BYTES = 64 * 1024


def log_disabled(hook: str, key: str, hook_input: dict) -> None:
    """One log line `disabled` for a hook switched off with `modules.<key>`, at most once per session: the sessions it
    was logged for are kept in <log_dir>/disabled/<hook>.json, the last DISABLED_SESSIONS of them. Never raises."""
    session = str(hook_input.get('session_id') or '')
    folder = os.path.join(log_dir(), 'disabled')
    path = os.path.join(folder, f'{hook}.json')
    try:
        seen = json.loads(_config.read_regular(path, DISABLED_MAX_BYTES).decode('utf-8'))
        seen = [str(item) for item in seen] if isinstance(seen, list) else []
    except (OSError, ValueError, RecursionError):
        seen = []
    if session in seen:
        return
    write_log_line({'time': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'hook': hook, 'mode': 'off',
                    'decision': 'disabled', 'reason': f'modules.{key} is off in config.json'})
    try:
        import _context_state      # here: only a hook that is switched off needs it
        _context_state.write_private(folder, f'{hook}.json', json.dumps((seen + [session])[-DISABLED_SESSIONS:]))
    except Exception:      # the line is written; a list that cannot be kept only means a second line later
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
        'permission_mode': _short_text(hook_input.get('permission_mode')),
        'input_keys': sorted(hook_input.keys()), 'tool_input_keys': sorted(tool_input.keys()),
        'subagent_type': _short_text(tool_input.get('subagent_type')),
        'model_in_call': _short_text(tool_input.get('model')), **extra,
    })
