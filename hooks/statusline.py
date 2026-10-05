#!/usr/bin/env python3
"""Status line: model, five-hour window, weekly limit, context size and prompt cache clock. Also merges the
usage values into the shared usage file and saves the context of each session (see _context_state.py).

Claude Code passes a JSON document to the status line on stdin. It contains rate_limits.five_hour and
rate_limits.seven_day (used_percentage, resets_at) without any network call: the values are already there.

The second purpose is the reason this script exists: the values are also written to the shared usage file
(see _usage.py), so the model and the hooks can read their own usage instead of guessing it. Several sessions
share that file; the merge makes sure an idle session cannot overwrite fresh values with old ones, and the
line shows the merged values, so every session shows the same.

If rate_limits is missing (API key, Bedrock, Vertex), that part stays silent instead of claiming 0 %: a
missing measurement is not "zero percent". The same goes for context_window and prompt_cache. If the merge
fails, the session's own values are shown and nothing is written. The exit code is always 0.

Output: <model> | 5h NN% ↻HH:MM | week NN% | ctx 231k/1000k 23% | cache 54m (the weekly reset time from yellow on)
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _config  # noqa: E402
import _context_state  # noqa: E402
import _hookio  # noqa: E402
import _usage  # noqa: E402

GRAY = '\033[38;5;245m'
YELLOW = '\033[38;5;179m'
RED = '\033[38;5;167m'
RESET = '\033[0m'
WEEK_STOP_TEXT = '<- weekly limit reached'
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')   # fixed English names, independent of the locale


def _own_value(status_input: dict, name: str) -> float | None:
    limits = status_input.get('rate_limits')
    block = limits.get(name) if isinstance(limits, dict) else None
    value = block.get('used_percentage') if isinstance(block, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _merged_value(merged: dict, name: str) -> float | None:
    block = merged.get(name)
    return float(block['used_percentage']) if isinstance(block, dict) else None


def _reset_of(source: dict | None, name: str):
    """resets_at of one block, unchecked: _reset_text decides whether it can be shown."""
    block = source.get(name) if isinstance(source, dict) else None
    return block.get('resets_at') if isinstance(block, dict) else None


def _reset_text(reset, now: float, with_day: bool) -> str:
    """` ↻19:10` in local time, for the week with the weekday (` ↻Mon 15:00`); '' for a reset that is missing,
    broken or already past: a guessed time would be worse than none."""
    if not _usage._number(reset) or reset <= now:
        return ''
    try:
        moment = time.localtime(reset)
    except (OverflowError, OSError, ValueError):
        return ''
    day = f'{WEEKDAYS[moment.tm_wday]} ' if with_day else ''
    return f' ↻{day}{moment.tm_hour:02d}:{moment.tm_min:02d}'


def _rounded(value: float) -> int | None:
    # '%.0f' rounds like C printf (half to even) and, unlike int(), never depends on a locale
    try:
        return int('%.0f' % value)
    except (ValueError, OverflowError):
        return None


def _part(label: str, value: float | None, yellow_from: float, red_from: float, suffix: str = '') -> str:
    number = _rounded(value) if value is not None else None
    if number is None:
        return ''
    color = RED if number >= red_from else YELLOW if number >= yellow_from else GRAY
    return f' {GRAY}| {color}{label} {number}%{suffix}{RESET}'


def _count(value, minimum: float = 0) -> bool:
    """A finite number (never bool) of at least `minimum`."""
    return _usage._number(value) and value >= minimum


def _context_numbers(status_input: dict) -> tuple[float, float | None, float | None] | None:
    """(window size, used tokens, used percentage) from context_window; None without a valid window size.

    Used tokens are None until the first API answer: total_input_tokens is 0 then, which means "not measured
    yet", not a real zero. A broken percentage is None, too.
    """
    block = status_input.get('context_window')
    size = block.get('context_window_size') if isinstance(block, dict) else None
    if not _count(size, 1000):
        return None
    used, percent = block.get('total_input_tokens'), block.get('used_percentage')
    return size, used if _count(used) and used > 0 else None, percent if _count(percent) else None


def _context_part(numbers: tuple[float, float | None, float | None] | None, line_cfg: dict) -> str:
    """`ctx 231k/1000k 23%`; before the first answer `ctx –/1000k`; nothing without a valid window size.

    The colour follows the absolute tokens only, never the percentage: a bigger window must not hide a
    context that is already expensive.
    """
    if numbers is None:
        return ''
    size, used, percent = numbers
    window = f'{int(size) // 1000}k'
    if used is None:
        return f' {GRAY}| {GRAY}ctx –/{window}{RESET}'
    rounded = _rounded(percent) if percent is not None else None
    color = (RED if used >= line_cfg['context_red_from_k'] * 1000
             else YELLOW if used >= line_cfg['context_yellow_from_k'] * 1000 else GRAY)
    return f' {GRAY}| {color}ctx {int(used) // 1000}k/{window}{f" {rounded}%" if rounded is not None else ""}{RESET}'


def _cache_part(status_input: dict, line_cfg: dict, now: float) -> str:
    """`cache 54m` (minutes left, rounded up), yellow below the limit, `cache cold` in red.

    prompt_cache is missing until the first request. Warm without a usable expires_at says nothing: a clock
    that is guessed would be worse than none.
    """
    cache = status_input.get('prompt_cache')
    if not isinstance(cache, dict):
        return ''
    warm = cache.get('warm')
    if warm is False:
        return f' {GRAY}| {RED}cache cold{RESET}'
    expires = cache.get('expires_at')
    if warm is not True or not _usage._number(expires):
        return ''
    minutes = math.ceil((expires - now) / 60)
    if minutes <= 0:
        return f' {GRAY}| {RED}cache cold{RESET}'
    color = YELLOW if minutes < line_cfg['cache_yellow_below_min'] else GRAY
    return f' {GRAY}| {color}cache {minutes}m{RESET}'


def render(model: str, five: float | None, week: float | None, cfg: dict, status_input: dict | None = None,
           now: float | None = None, resets: tuple = (None, None)) -> str:
    """`resets` = (resets_at of the five-hour window, of the week). The weekly one is shown only from
    `yellow_from` on: below that it is days away and only takes room."""
    line_cfg, limits = cfg['statusline'], cfg['thresholds']
    now = time.time() if now is None else now
    five_reset, week_reset = resets if line_cfg['show_reset'] else (None, None)
    week_rounded = _rounded(week) if week is not None else None
    if week_rounded is None or week_rounded < line_cfg['yellow_from']:
        week_reset = None
    line = f'{GRAY}{model}{RESET}'
    line += _part('5h', five, line_cfg['yellow_from'], line_cfg['red_from'], _reset_text(five_reset, now, False))
    line += _part('week', week, line_cfg['yellow_from'], line_cfg['red_from'], _reset_text(week_reset, now, True))
    # The limit from which no new agents start: visible, not only agreed on.
    if line_cfg['week_stop_marker'] and week_rounded is not None and week_rounded >= limits['start_block_week']:
        line += f' {RED}{WEEK_STOP_TEXT}{RESET}'
    if status_input is not None:
        line += _context_part(_context_numbers(status_input), line_cfg)
        line += _cache_part(status_input, line_cfg, now)
    return line


def _model_name(status_input: dict) -> str:
    model = status_input.get('model')
    name = model.get('display_name') if isinstance(model, dict) else None
    return name if isinstance(name, str) and name else 'Claude'


def _save_context(status_input: dict, now: float) -> None:
    """The context state for the later reminder. Needs a measured context (not before the first answer);
    any error is ignored, the line is more important."""
    try:
        numbers = _context_numbers(status_input)
        if numbers is not None and numbers[1] is not None:
            _context_state.save(os.path.join(_hookio.log_dir(), 'context'), status_input.get('session_id'),
                                numbers[0], numbers[1], now)
    except Exception:
        pass


def main() -> int:
    try:
        now = time.time()
        try:
            status_input = json.load(sys.stdin)
        except (ValueError, RecursionError):
            status_input = {}
        if not isinstance(status_input, dict):
            status_input = {}
        five, week = _own_value(status_input, 'five_hour'), _own_value(status_input, 'seven_day')
        source = status_input.get('rate_limits')
        try:
            merged = _usage.update_file(_usage.usage_path(), status_input, now)
            five, week = _merged_value(merged, 'five_hour'), _merged_value(merged, 'seven_day')
            source = merged
        except Exception:  # merge failed: show the own values, write nothing
            pass
        _save_context(status_input, now)
        resets = (_reset_of(source, 'five_hour'), _reset_of(source, 'seven_day'))
        line = render(_model_name(status_input), five, week, _config.load(log_problems=False), status_input, now,
                      resets)
        # The line contains a non-ASCII dash; a machine with another locale must not swallow it.
        sys.stdout.reconfigure(encoding='utf-8')
        print(line)
    except Exception:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
