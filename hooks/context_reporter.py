#!/usr/bin/env python3
"""Hooks UserPromptSubmit and PostToolUse: tell the model how full its context window is.

The only source is the per-session state file of the status line (`<log_dir>/context/<session_id>.json`, see
_context_state.py). The transcript is never read. Without the status line, or before its first measurement,
the hook has nothing to say and stays silent.

UserPromptSubmit always reports. PostToolUse reports only from `context_reporter.after_tool_from` percent of the
window on, so a long run is reminded as the context gets expensive, not after every tool call.

UserPromptSubmit (never PostToolUse) also adds one sentence that offers the wake-up watcher (wake_watcher.py) with a
ready command, from `wake_watcher.from_k` thousand tokens on (0: never). It stays out while a watcher is alive
or has fired within the last hour (heartbeat file, see _wake.py): the routine the watcher triggers would
otherwise start the next one. If the offer fails for any reason, the context line is still given.

When the watcher has fired, Claude Code submits its end as a task notification (UserPromptSubmit) whose text names
the output file of the task. If that file starts with this session's wake-up call and `wake_watcher.routine_file`
names a usable file, its content follows the line: the user's session-end routine reaches the model as hook
context, not only as tool output. An unusable routine file gives one log line and nothing else.

Silent (no output, exit 0) for a subagent (its context is another one), for another event, and for every file
that is missing, a link, not a regular file, broken, of another session, implausible, older than
`context_reporter.max_age_min` or from the future. Never blocks and always exits with 0, even if an import
fails: exit code 2 on UserPromptSubmit would reject every prompt the user types.
"""
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROMPT = 'UserPromptSubmit'
AFTER_TOOL = 'PostToolUse'
# The writer produces about 150 bytes; anything much bigger is not its file.
MAX_BYTES = 4096
ROUTINE_MAX_BYTES = 4096
ROUTINE_HEADER = 'Session-end routine (from wake_watcher.routine_file, presented because the wake-up watcher fired):\n'
# The notification of a finished background task names its output file (form seen on 14 Oct 2026).
OUTPUT_FILE = re.compile(r'<output-file>([^<>\n]+)</output-file>')


def read_state(directory: str, session_id, suffix: str = '.json') -> dict | None:
    """The state of one session, None for every reason to stay silent. Never follows a link. `suffix` picks the
    file: the state file of the status line, or the heartbeat of the wake-up watcher (`.watch`, see _wake.py)."""
    import _context_state
    import _plat
    import _usage
    if not isinstance(session_id, str) or not _context_state.SESSION_ID.fullmatch(session_id):
        return None      # the session id becomes a file name: no path tricks
    if os.path.islink(directory):
        return None
    try:
        # O_NONBLOCK: a named pipe planted as the file must not block the hook
        descriptor = os.open(os.path.join(directory, f'{session_id}{suffix}'),
                             os.O_RDONLY | _plat.O_NONBLOCK | _usage.NO_FOLLOW | _plat.O_BINARY)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        raw = os.read(descriptor, MAX_BYTES)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, RecursionError):      # UnicodeDecodeError is a ValueError
        return None
    return data if isinstance(data, dict) and data.get('session_id') == session_id else None


def is_fresh(measured_at, now: datetime, max_age_min: float) -> bool:
    """Not older than `max_age_min` minutes and not more than one minute in the future (as _usage classifies it)."""
    import _usage
    if not isinstance(measured_at, str):
        return False
    try:
        age = (now - datetime.fromisoformat(measured_at.replace('Z', '+00:00'))).total_seconds() / 60
    except (ValueError, TypeError):      # not a time, or one without a time zone
        return False
    return _usage.MIN_AGE_MIN <= age <= max_age_min


def numbers(state: dict) -> tuple[int, int] | None:
    """(window size, used tokens) if both are plausible: positive finite numbers, a window of at least 1000 as
    the status line requires, and not more used than the window."""
    import _usage
    size, used = state.get('context_window_size'), state.get('used_tokens')
    if not (_usage._number(size) and _usage._number(used)) or size < 1000 or used <= 0 or used > size:
        return None
    return int(size), int(used)


def text(size: int, used: int) -> str:
    """The same cut and rounding as the `ctx` part of the status line: thousands are cut, the percentage is
    rounded half to even (`%.0f`), so both show the same numbers."""
    percent = int('%.0f' % (used * 100 / size))
    return f'Context: {used // 1000}k/{size // 1000}k tokens ({percent}%). Measured, not estimated.'


def wake_offer(directory: str, session_id: str, now: datetime) -> str:
    """The sentence that offers the wake-up watcher, '' if one lives or has fired lately or if anything fails: the
    context line is worth more than the offer."""
    try:
        import _plat
        import _wake
        if _wake.blocks_hint(_wake.read_heartbeat(directory, session_id), now.timestamp()):
            return ''
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wake_watcher.py')
        return (' Wake-up watcher: start it once now in the background (Bash, run_in_background true, '
                f'timeout 7200000): {_plat.script_command(script, session_id)}')
    except Exception:
        return ''


def _open_regular(path: str) -> tuple[int | None, str | None]:
    """(descriptor, None) of a regular file of this user, opened without following a link and without blocking on
    a named pipe; else (None, reason)."""
    import _plat
    import _usage
    try:
        descriptor = os.open(path, os.O_RDONLY | _plat.O_NONBLOCK | _usage.NO_FOLLOW | _plat.O_BINARY)
    except FileNotFoundError:
        return None, 'missing'
    except OSError:
        return None, 'a link' if os.path.islink(path) else 'not readable'
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        reason = 'not a regular file'
    elif os.name != 'nt' and info.st_uid != os.getuid():      # Windows has no uid; the profile folder is protected by its ACL
        reason = 'owned by another user'
    else:
        return descriptor, None
    os.close(descriptor)
    return None, reason


def is_wake_up_call(prompt, session_id: str) -> bool:
    """True if the prompt is the notification of a finished background task whose output starts with the wake-up
    call for this session. Only the start of the file is read, only an absolute path to a regular file of this
    user counts."""
    if not isinstance(prompt, str) or '<task-notification>' not in prompt:
        return False
    expected = f'Wake-up call for session {session_id}:'.encode('utf-8')
    for path in OUTPUT_FILE.findall(prompt):
        if not os.path.isabs(path):
            continue
        descriptor, _ = _open_regular(path)
        if descriptor is None:
            continue
        try:
            start = os.read(descriptor, len(expected))
        finally:
            os.close(descriptor)
        if start == expected:
            return True
    return False


def read_routine(path: str) -> tuple[str | None, str | None]:
    """(text, None) of the routine file, or (None, reason): it must be a regular file of this user that neither
    group nor others may write (its text reaches the model as an instruction), at most ROUTINE_MAX_BYTES, UTF-8
    and not empty."""
    descriptor, reason = _open_regular(path)
    if descriptor is None:
        return None, reason
    try:
        # Windows reports every writable file as writable for group and others: there the check would refuse them all
        if os.name != 'nt' and os.fstat(descriptor).st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return None, 'writable by group or others'
        raw = os.read(descriptor, ROUTINE_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > ROUTINE_MAX_BYTES:
        return None, f'larger than {ROUTINE_MAX_BYTES} bytes'
    try:
        routine = raw.decode('utf-8').strip()
    except UnicodeDecodeError:
        return None, 'not UTF-8'
    return (routine, None) if routine else (None, 'empty')


def wake_routine(hook_input: dict, cfg: dict) -> str:
    """The routine with its header if this prompt delivers a wake-up call for this session, else ''. An unusable
    routine file is logged (without its path) and gives ''; any other failure gives '' silently."""
    try:
        configured = cfg['wake_watcher']['routine_file']
        if not configured or not is_wake_up_call(hook_input.get('prompt'), hook_input.get('session_id')):
            return ''
        routine, reason = read_routine(os.path.expanduser(configured))
        if routine is None:
            import _hookio
            _hookio.write_log_line({'time': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                    'hook': 'context_reporter', 'decision': 'no_routine',
                                    'reason': f'wake_watcher.routine_file: {reason}'})
            return ''
        return ROUTINE_HEADER + routine
    except Exception:
        return ''


def context_line(event: str, hook_input: dict, cfg: dict, directory: str, now: datetime) -> str:
    """The line about the context window (with the offer of the watcher), '' to say nothing about it."""
    state = read_state(directory, hook_input.get('session_id'))
    if state is None or not is_fresh(state.get('measured_at'), now, cfg['context_reporter']['max_age_min']):
        return ''
    measured = numbers(state)
    if measured is None:
        return ''
    size, used = measured
    # The exact share against the limit, not the rounded one shown in the line
    if event == AFTER_TOOL and used * 100 < cfg['context_reporter']['after_tool_from'] * size:
        return ''
    line = text(size, used)
    from_k = cfg['wake_watcher']['from_k']
    if event == PROMPT and from_k > 0 and used >= from_k * 1000:
        line += wake_offer(directory, hook_input['session_id'], now)
    return line


def build_context(hook_input: dict, cfg: dict, directory: str, now: datetime) -> tuple[str, str] | None:
    """(event, text) for the model, None to stay silent."""
    event = hook_input.get('hook_event_name')
    if event not in (PROMPT, AFTER_TOOL) or 'agent_id' in hook_input:
        return None
    line = context_line(event, hook_input, cfg, directory, now)
    if event == PROMPT:
        routine = wake_routine(hook_input, cfg)
        if routine:
            line = f'{line}\n\n{routine}' if line else routine
    return (event, line) if line else None


def main() -> int:
    try:
        import _config
        import _hookio
        hook_input = _hookio.read_input()
        # No logging of config problems: this runs after every tool call, a broken config would fill the log.
        cfg = _config.load(log_problems=False)
        if not cfg['modules']['context_reporter']:      # switched off in config.json: the hook stays and says nothing
            _hookio.log_disabled('context_reporter', 'context_reporter', hook_input)
            return 0
        result = build_context(hook_input, cfg, os.path.join(_hookio.log_dir(), 'context'),
                               datetime.now(timezone.utc))
        if result:
            _hookio.emit_context(*result)
    except Exception:  # the report must never disturb a prompt or a tool call, not even with a missing sibling file
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
