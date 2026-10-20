"""The measurement history: one line per change of the usage numbers, for reports over days, weeks and months.

`<log_dir>/usage-history.jsonl` gets one line per measurement:
{"t": "<UTC ISO>", "five": <number|null>, "week": <number|null>, "five_resets": <Unix s|null>,
"week_resets": <Unix s|null>}. Numbers only: no session id, no path. The numbers are taken over as they come
(whole or decimal); a block that is missing or over counts as null.

The source is the merged content of the shared usage file (see _usage.py), not what one session reports, and the
line is appended while the usage file's lock is held (`_usage.update_file(..., after_merge=...)`), so sessions
running side by side never write the same measurement twice. A line is written only if

- `five`, `week` or one of the resets differs from the last line, or
- the last line is at least `heartbeat_min` old (an idle stretch stays visible as a stretch, not as a gap), or
  further than FUTURE_SLACK_S ahead of now (the clock was set back),

and only if at least one block is valid. Above `max_kb` the file becomes `usage-history.jsonl.1` (an older
`.1` goes); the new file starts with the next line. A last line cut off without its line break (a crash
mid-write) gets one before the new line, so the new line stands alone. The file is 0600 in a 0700 folder, opened with `O_NOFOLLOW`
(a planted link is never written through, a link as the folder is not followed). `append` never raises: the
status line must never fail because of the history.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import _config
import _plat
import _usage

FILE_NAME = 'usage-history.jsonl'
TAIL_BYTES = 4096    # a line is about 120 bytes: the tail holds the last one for sure
KEYS = ('five', 'week', 'five_resets', 'week_resets')
# A last line less than this ahead of now counts as now: status lines started side by side take the lock in any
# order, and `t` is cut to the second, so the one that waited can see the line of a later one up to a second ahead.
FUTURE_SLACK_S = 60


def _sample(merged, now: float) -> tuple | None:
    """(five, week, five_resets, week_resets) from the merged blocks that are still valid at `now`; None if no
    block is valid."""
    if not isinstance(merged, dict):
        return None
    blocks = [block if _usage.block_valid(block, now) else None
              for block in (merged.get('five_hour'), merged.get('seven_day'))]
    if blocks == [None, None]:
        return None
    return (tuple(block['used_percentage'] if block else None for block in blocks)
            + tuple(block['resets_at'] if block else None for block in blocks))


def _last_line(descriptor: int, size: int) -> tuple[tuple, float] | None:
    """(values, time as Unix seconds) of the last line, or None if there is none or it cannot be read."""
    tail = _plat.pread(descriptor, TAIL_BYTES, max(0, size - TAIL_BYTES)).decode('utf-8', errors='replace')
    rows = [row for row in tail.splitlines() if row.strip()]
    if not rows:
        return None
    try:
        line = json.loads(rows[-1])
        stamp = datetime.fromisoformat(line['t'].replace('Z', '+00:00')).timestamp()
        return tuple(line[key] for key in KEYS), stamp
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None


def _due(descriptor: int, size: int, sample: tuple, now: float, heartbeat_min: float) -> bool:
    last = _last_line(descriptor, size)
    if last is None:
        return True
    values, stamp = last
    # a last line further ahead than FUTURE_SLACK_S is from the future (a clock set back): it must not hold the
    # history back
    return values != sample or not -FUTURE_SLACK_S < now - stamp < heartbeat_min * 60


def _open(path: str) -> int:
    descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | _usage.NO_FOLLOW | _plat.O_BINARY, 0o600)
    _plat.fchmod(descriptor, 0o600)     # a file of any earlier umask becomes private, too
    return descriptor


def append(log_dir: str, merged, now: float, heartbeat_min: float = _config.DEFAULTS['history']['heartbeat_min'],
           max_kb: float = _config.DEFAULTS['history']['max_kb']) -> None:
    """Appends one line for `merged` if it is new (or the heartbeat is due). The caller holds the usage file's lock.
    Any error is ignored."""
    try:
        sample = _sample(merged, now)
        if sample is None or os.path.islink(log_dir):
            return
        # makedirs gives its mode only to the last folder; the one above it is created first, also private
        os.makedirs(os.path.dirname(os.path.abspath(log_dir)), mode=0o700, exist_ok=True)
        os.makedirs(log_dir, mode=0o700, exist_ok=True)
        path = os.path.join(log_dir, FILE_NAME)
        line = json.dumps(dict(zip(('t',) + KEYS, (_usage._measured_at(now),) + sample)), separators=(',', ':')) + '\n'
        descriptor = _open(path)
        try:
            size = os.fstat(descriptor).st_size
            if not _due(descriptor, size, sample, now, heartbeat_min):
                return
            if size > max_kb * 1024:
                os.close(descriptor)      # Windows cannot rename a file that is open, not even in this process
                descriptor = None
                _usage.replace(path, path + '.1')      # a link as `.1` is replaced, never written through
                descriptor = _open(path)
            size = os.fstat(descriptor).st_size      # after a rotation the file is new
            if size and _plat.pread(descriptor, 1, size - 1) != b'\n':
                line = '\n' + line      # a cut-off last line stays its own line, the new one stands alone
            os.write(descriptor, line.encode('utf-8'))
        finally:
            if descriptor is not None:
                os.close(descriptor)
    except Exception:
        pass
