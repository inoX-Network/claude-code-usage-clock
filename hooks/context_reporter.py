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

Silent (no output, exit 0) for a subagent (its context is another one), for another event, and for every file
that is missing, a link, not a regular file, broken, of another session, implausible, older than
`context_reporter.max_age_min` or from the future. Never blocks and always exits with 0, even if an import
fails: exit code 2 on UserPromptSubmit would reject every prompt the user types.
"""
import json
import os
import shlex
import stat
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROMPT = 'UserPromptSubmit'
AFTER_TOOL = 'PostToolUse'
# The writer produces about 150 bytes; anything much bigger is not its file.
MAX_BYTES = 4096


def read_state(directory: str, session_id, suffix: str = '.json') -> dict | None:
    """The state of one session, None for every reason to stay silent. Never follows a link. `suffix` picks the
    file: the state file of the status line, or the heartbeat of the wake-up watcher (`.watch`, see _wake.py)."""
    import _context_state
    import _usage
    if not isinstance(session_id, str) or not _context_state.SESSION_ID.fullmatch(session_id):
        return None      # the session id becomes a file name: no path tricks
    if os.path.islink(directory):
        return None
    try:
        # O_NONBLOCK: a named pipe planted as the file must not block the hook
        descriptor = os.open(os.path.join(directory, f'{session_id}{suffix}'),
                             os.O_RDONLY | os.O_NONBLOCK | _usage.NO_FOLLOW)
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
        import _wake
        if _wake.blocks_hint(_wake.read_heartbeat(directory, session_id), now.timestamp()):
            return ''
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wake_watcher.py')
        return (' Wake-up watcher: start it once now in the background (Bash, run_in_background true, '
                f'timeout 7200000): python3 {shlex.quote(script)} {session_id}')
    except Exception:
        return ''


def build_context(hook_input: dict, cfg: dict, directory: str, now: datetime) -> tuple[str, str] | None:
    """(event, line) for the model, None to stay silent."""
    event = hook_input.get('hook_event_name')
    if event not in (PROMPT, AFTER_TOOL) or 'agent_id' in hook_input:
        return None
    state = read_state(directory, hook_input.get('session_id'))
    if state is None or not is_fresh(state.get('measured_at'), now, cfg['context_reporter']['max_age_min']):
        return None
    measured = numbers(state)
    if measured is None:
        return None
    size, used = measured
    # The exact share against the limit, not the rounded one shown in the line
    if event == AFTER_TOOL and used * 100 < cfg['context_reporter']['after_tool_from'] * size:
        return None
    line = text(size, used)
    from_k = cfg['wake_watcher']['from_k']
    if event == PROMPT and from_k > 0 and used >= from_k * 1000:
        line += wake_offer(directory, hook_input['session_id'], now)
    return event, line


def main() -> int:
    try:
        import _config
        import _hookio
        hook_input = _hookio.read_input()
        # No logging of config problems: this runs after every tool call, a broken config would fill the log.
        cfg = _config.load(log_problems=False)
        result = build_context(hook_input, cfg, os.path.join(_hookio.log_dir(), 'context'),
                               datetime.now(timezone.utc))
        if result:
            _hookio.emit_context(*result)
    except Exception:  # the report must never disturb a prompt or a tool call, not even with a missing sibling file
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
