"""What the wake-up watcher (wake_watcher.py) and the context reporter share: two times and the heartbeat file.

`<log_dir>/context/<session_id>.watch` holds {"session_id", "state": "watching"|"fired", "at": "<UTC ISO>"}.
The watcher writes "watching" on every poll and "fired" once, right before it wakes the model. The reporter
reads it to offer the watcher only when none lives and none has fired lately: a watcher counts as alive while its
last heartbeat is at most ALIVE_MIN minutes old, and a fire rests the offer for REST_AFTER_FIRE_MIN minutes
(the routine it triggers would otherwise start the next watcher at once).

The file is written like the state file (atomic, private, never through a link) and read like it (no link, no
named pipe, size limit). Every unusable file reads as "no heartbeat".
"""
from __future__ import annotations

import json
from datetime import datetime

import _context_state
import _usage

ALIVE_MIN = 3
REST_AFTER_FIRE_MIN = 60
STATES = ('watching', 'fired')


def write_heartbeat(directory: str, session_id, state: str, now: float) -> None:
    """Writes the heartbeat of one session. Does nothing for an invalid session id or a linked folder. Raises
    OSError if the file cannot be written."""
    if not isinstance(session_id, str) or not _context_state.SESSION_ID.fullmatch(session_id):
        return
    content = json.dumps({'session_id': session_id, 'state': state, 'at': _usage._measured_at(now)},
                         separators=(',', ':'))
    _context_state.write_private(directory, f'{session_id}.watch', content)


def read_heartbeat(directory: str, session_id) -> dict | None:
    """{"state", "at" (Unix seconds)} of one session, None for every file that is missing or not usable."""
    import context_reporter      # imported here: the reporter imports this module the same way
    data = context_reporter.read_state(directory, session_id, '.watch')
    if data is None or data.get('state') not in STATES or not isinstance(data.get('at'), str):
        return None
    try:
        at = datetime.fromisoformat(data['at'].replace('Z', '+00:00'))
    except ValueError:
        return None
    return {'state': data['state'], 'at': at.timestamp()} if at.tzinfo is not None else None


def blocks_hint(beat: dict | None, now: float) -> bool:
    """True while the offer to start a watcher would be wrong: a watcher is alive, or one fired lately. A time up to
    one minute ahead counts like the present (as the state file does), more than that is a broken file."""
    if beat is None:
        return False
    age_min = (now - beat['at']) / 60
    if age_min < _usage.MIN_AGE_MIN:
        return False
    return age_min <= (ALIVE_MIN if beat['state'] == 'watching' else REST_AFTER_FIRE_MIN)
