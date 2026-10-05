"""The per-session context file: what the status line saw, for a later reminder that reads it.

`<log_dir>/context/<session_id>.json` holds {"session_id", "context_window_size", "used_tokens", "measured_at"}
and, while the prompt cache is warm, {"cache_ttl": "1h"|"5m", "cache_expires_at": <Unix seconds>} (the wake-up
watcher reads them). An invalid or cold cache clock leaves both keys out. Several sessions run at once, each
with its own file, so nothing is shared and no lock is needed.

The session id comes from the status line input and becomes a file name: it is accepted only as
[A-Za-z0-9_-]{1,128}, anything else writes nothing. Writing is atomic (temporary file + os.replace) and
never follows links: a planted link is replaced, never written through. Files and folder are private
(0600 / 0700). Every write also removes `*.json` and `*.watch` (heartbeat of the wake-up watcher, see _wake.py)
files older than seven days (not the one just written); links and folders are left alone. The caller treats every error as "no state": the status line must never
fail because of this file.
"""
from __future__ import annotations

import json
import os
import re
import stat

import _usage

SESSION_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
MAX_AGE_S = 7 * 86400
CACHE_TTLS = ('1h', '5m')


def _prune(directory: str, now: float, keep: str) -> None:
    """Removes regular `*.json` and `*.watch` files whose modification time is more than seven days before `now`.
    `keep` is the file just written: it stays even if the clock and the file system disagree."""
    for name in os.listdir(directory):
        if not name.endswith(('.json', '.watch')) or name == keep:
            continue
        path = os.path.join(directory, name)
        try:
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and now - info.st_mtime > MAX_AGE_S:
                os.unlink(path)
        except OSError:
            continue    # gone already, or not ours to delete: the next write tries again


def write_private(directory: str, name: str, content: str) -> bool:
    """Writes `<directory>/<name>` atomically (temporary file + os.replace, never through a link, mode 0600; the
    folder is created with 0700). False, and nothing written, if the folder is a link. Raises OSError if the
    file cannot be written. The caller has validated `name`."""
    if os.path.islink(directory):
        return False      # a planted link as the folder: never write through it
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, name)
    tmp = f'{path}.tmp.{os.getpid()}'
    try:
        try:
            os.unlink(tmp)  # a leftover, possibly a planted link: remove it, never write through it
        except FileNotFoundError:
            pass
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _usage.NO_FOLLOW, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def save(directory: str, session_id, size, used, now: float, cache_ttl=None, cache_expires_at=None) -> None:
    """Writes the state of one session. Does nothing for an invalid session id or for numbers that are not
    positive and finite. The cache clock is written only if the ttl is "1h" or "5m" and the expiry a positive
    finite number; otherwise both keys are left out. Raises OSError if the file cannot be written."""
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return
    if not all(_usage._number(value) and value > 0 for value in (size, used)):
        return
    state = {'session_id': session_id, 'context_window_size': int(size), 'used_tokens': int(used),
             'measured_at': _usage._measured_at(now)}
    if cache_ttl in CACHE_TTLS and _usage._number(cache_expires_at) and cache_expires_at > 0:
        state.update(cache_ttl=cache_ttl, cache_expires_at=int(cache_expires_at))
    name = f'{session_id}.json'
    if not write_private(directory, name, json.dumps(state, separators=(',', ':'))):
        return
    try:
        _prune(directory, now, name)
    except OSError:
        pass
