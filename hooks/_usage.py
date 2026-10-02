"""The shared usage file: merging what all sessions report, and reading it back.

Several Claude Code sessions write the same file. Idle sessions keep redrawing their status line with old
numbers (an old weekly value, no five-hour block at all). Whoever writes last would win, so the readers kept
seeing "unknown". The merge rules below prevent that:

- A block (five-hour or weekly) counts only with a numeric value and a reset time in the future.
- Same window (reset times less than 60 s apart): the higher value wins. Usage never goes down in a window.
- Later window: the new block wins, even with a lower value (after a reset).
- `measured_at` is renewed only if this session contributed a valid five-hour block that did not lose.
  An idle session can therefore never make old data look fresh.
- Writing happens under an exclusive flock, atomically via a temporary file, and only when something changed.
  Lock and temporary file are opened without following links, so a planted link never redirects the write.

File format: {"five_hour": {"used_percentage", "resets_at"}, "seven_day": {...}, "measured_at": "<UTC ISO>"}.
A file in any other format counts as empty.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import _config

NO_FOLLOW = getattr(os, 'O_NOFOLLOW', 0)
SAME_WINDOW_S = 60
MIN_AGE_MIN = -1.0  # a timestamp from the future counts as stale, not as permanently fresh


def usage_path() -> str:
    """Env CCUC_USAGE_FILE, else config `usage_file`."""
    return os.path.expanduser(os.environ.get('CCUC_USAGE_FILE') or _config.load(log_problems=False)['usage_file'])


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def block_valid(block, now: float) -> bool:
    return (isinstance(block, dict) and _number(block.get('used_percentage')) and _number(block.get('resets_at'))
            and block['resets_at'] > now)


def pick(old, new, now: float) -> tuple[dict | None, bool]:
    """(chosen block, comes_from_new). comes_from_new only if `new` is valid and does not lose."""
    if not block_valid(new, now):
        return (old if block_valid(old, now) else None), False
    if not block_valid(old, now):
        return new, True
    if abs(new['resets_at'] - old['resets_at']) < SAME_WINDOW_S:
        return (new, True) if new['used_percentage'] >= old['used_percentage'] else (old, False)
    return (new, True) if new['resets_at'] > old['resets_at'] else (old, False)


def _measured_at(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def merge(old: dict, status_input: dict, now: float) -> dict:
    """Merges the stored file content with the status line input of one session."""
    limits = status_input.get('rate_limits') if isinstance(status_input.get('rate_limits'), dict) else {}
    five, five_is_new = pick(old.get('five_hour'), limits.get('five_hour'), now)
    week, _ = pick(old.get('seven_day'), limits.get('seven_day'), now)
    stored = old.get('measured_at')
    measured = _measured_at(now) if five_is_new else (stored if isinstance(stored, str) else None)
    return {'five_hour': five, 'seven_day': week, 'measured_at': measured}


def _read_dict(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def update_file(path: str, status_input: dict, now: float) -> dict:
    """Reads, merges and writes the usage file under an exclusive lock; returns the merged content.

    Raises OSError if the file cannot be locked or written: the caller then shows its own values.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    lock_fd = os.open(path + '.lock', os.O_RDWR | os.O_CREAT | NO_FOLLOW, 0o600)
    with os.fdopen(lock_fd, 'r+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = _read_dict(path)
        merged = merge(old, status_input, now)
        if merged != {key: old.get(key) for key in merged}:
            tmp = f'{path}.tmp.{os.getpid()}'
            try:
                try:
                    os.unlink(tmp)  # a leftover, possibly a planted link: remove it, never write through it
                except FileNotFoundError:
                    pass
                descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NO_FOLLOW, 0o600)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as f:
                    json.dump(merged, f, separators=(',', ':'))
                os.replace(tmp, path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    return merged


@dataclass(frozen=True)
class Usage:
    five_hour: float | None
    week: float | None
    age_min: float | None
    # Reset times (Unix seconds), for display only; None if missing or already past
    five_hour_reset: float | None = None
    week_reset: float | None = None
    max_age_min: float = 15.0

    @property
    def state(self) -> str:
        """'ok', 'stale' (too old or from the future) or 'unknown' (no valid five-hour block or no timestamp)."""
        if self.five_hour is None or self.age_min is None:
            return 'unknown'
        if self.age_min > self.max_age_min or self.age_min < MIN_AGE_MIN:
            return 'stale'
        return 'ok'


def read_usage(path: str | None = None, now: datetime | None = None, max_age_min: float | None = None) -> Usage:
    """Reads the usage file. Every error results in 'unknown', never in an exception."""
    if max_age_min is None:
        max_age_min = _config.load(log_problems=False)['thresholds']['max_age_min']
    now = now or datetime.now(timezone.utc)
    limit = float(max_age_min)
    try:
        data = _read_dict(path or usage_path())
        measured = datetime.fromisoformat(str(data.get('measured_at', '')).replace('Z', '+00:00'))
        age = (now - measured).total_seconds() / 60
    except (ValueError, TypeError, AttributeError):
        return Usage(None, None, None, max_age_min=limit)
    ts = now.timestamp()
    five, week = data.get('five_hour'), data.get('seven_day')
    return Usage(
        float(five['used_percentage']) if block_valid(five, ts) else None,
        float(week['used_percentage']) if block_valid(week, ts) else None,
        age,
        float(five['resets_at']) if block_valid(five, ts) else None,
        float(week['resets_at']) if block_valid(week, ts) else None,
        limit,
    )
