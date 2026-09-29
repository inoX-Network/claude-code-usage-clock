#!/usr/bin/env python3
"""Status line: model, five-hour window and weekly limit. Also merges the values into the shared usage file.

Claude Code passes a JSON document to the status line on stdin. It contains rate_limits.five_hour and
rate_limits.seven_day (used_percentage, resets_at) without any network call: the values are already there.

The second purpose is the reason this script exists: the values are also written to the shared usage file
(see _usage.py), so the model and the hooks can read their own usage instead of guessing it. Several sessions
share that file; the merge makes sure an idle session cannot overwrite fresh values with old ones, and the
line shows the merged values, so every session shows the same.

If rate_limits is missing (API key, Bedrock, Vertex), that part stays silent instead of claiming 0 %: a
missing measurement is not "zero percent". If the merge fails, the session's own values are shown and
nothing is written. The exit code is always 0.

Output: <model> | 5h NN% | week NN%
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _config  # noqa: E402
import _usage  # noqa: E402

GRAY = '\033[38;5;245m'
YELLOW = '\033[38;5;179m'
RED = '\033[38;5;167m'
RESET = '\033[0m'
WEEK_STOP_TEXT = '<- weekly limit reached'


def _own_value(status_input: dict, name: str) -> float | None:
    limits = status_input.get('rate_limits')
    block = limits.get(name) if isinstance(limits, dict) else None
    value = block.get('used_percentage') if isinstance(block, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _merged_value(merged: dict, name: str) -> float | None:
    block = merged.get(name)
    return float(block['used_percentage']) if isinstance(block, dict) else None


def _rounded(value: float) -> int | None:
    # '%.0f' rounds like C printf (half to even) and, unlike int(), never depends on a locale
    try:
        return int('%.0f' % value)
    except (ValueError, OverflowError):
        return None


def _part(label: str, value: float | None, yellow_from: float, red_from: float) -> str:
    number = _rounded(value) if value is not None else None
    if number is None:
        return ''
    color = RED if number >= red_from else YELLOW if number >= yellow_from else GRAY
    return f' {GRAY}| {color}{label} {number}%{RESET}'


def render(model: str, five: float | None, week: float | None, cfg: dict) -> str:
    line_cfg, limits = cfg['statusline'], cfg['thresholds']
    line = f'{GRAY}{model}{RESET}'
    line += _part('5h', five, line_cfg['yellow_from'], line_cfg['red_from'])
    line += _part('week', week, line_cfg['yellow_from'], line_cfg['red_from'])
    # The limit from which no new agents start: visible, not only agreed on.
    week_rounded = _rounded(week) if week is not None else None
    if line_cfg['week_stop_marker'] and week_rounded is not None and week_rounded >= limits['start_block_week']:
        line += f' {RED}{WEEK_STOP_TEXT}{RESET}'
    return line


def _model_name(status_input: dict) -> str:
    model = status_input.get('model')
    name = model.get('display_name') if isinstance(model, dict) else None
    return name if isinstance(name, str) and name else 'Claude'


def main() -> int:
    try:
        try:
            status_input = json.load(sys.stdin)
        except (ValueError, RecursionError):
            status_input = {}
        if not isinstance(status_input, dict):
            status_input = {}
        five, week = _own_value(status_input, 'five_hour'), _own_value(status_input, 'seven_day')
        try:
            merged = _usage.update_file(_usage.usage_path(), status_input, time.time())
            five, week = _merged_value(merged, 'five_hour'), _merged_value(merged, 'seven_day')
        except Exception:  # merge failed: show the own values, write nothing
            pass
        print(render(_model_name(status_input), five, week, _config.load(log_problems=False)))
    except Exception:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
