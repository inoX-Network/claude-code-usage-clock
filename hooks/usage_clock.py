#!/usr/bin/env python3
"""Hook UserPromptSubmit: one line of context per prompt with the usage and/or the local time.

Config `display.show_usage` and `display.show_time` switch the two parts independently; both off means no
output. The time part needs no usage file. Never blocks and always exits with 0, even if an import fails:
exit code 2 on UserPromptSubmit would reject every prompt the user types.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Fixed English names: strftime("%a %b") would follow the locale of the machine.
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
FRESH_MEASUREMENT_HINT = 'start larger blocks only after a fresh measurement'


def format_reset(ts: float, now: datetime) -> str:
    """Reset time in the time zone of `now`: today only the time, otherwise with weekday and date."""
    moment = datetime.fromtimestamp(ts, tz=now.tzinfo)
    if moment.date() == now.date():
        return f'{moment:%H:%M}'
    return f'{WEEKDAYS[moment.weekday()]} {moment.day} {MONTHS[moment.month - 1]} {moment:%H:%M}'


def with_reset(value: str, reset: float | None, now: datetime) -> str:
    return f'{value} (resets {format_reset(reset, now)})' if reset is not None else value


def usage_text(usage, threshold: float, now: datetime) -> str:
    """The usage part. `usage` is a _usage.Usage, `threshold` is thresholds.start_block_5h."""
    if usage.state == 'unknown':
        return f'Usage: unknown (no valid measurement in the usage file) — {FRESH_MEASUREMENT_HINT}.'
    five = with_reset(f'{usage.five_hour:.0f} %', usage.five_hour_reset, now)
    week = with_reset(f'{usage.week:.0f} %', usage.week_reset, now) if usage.week is not None else '?'
    measured = f'{usage.age_min:.0f} min ago' if usage.age_min >= 0 else 'in the future'
    line = f'Usage: 5h {five} · week {week} · measured {measured}'
    if usage.state == 'stale':
        line += f' — STALE, counts as unknown; {FRESH_MEASUREMENT_HINT}'
    elif usage.five_hour >= threshold:
        line += f' — from {threshold:g} % on, no new blocks'
    return line


def time_text(now: datetime) -> str:
    return f'Now: {WEEKDAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]} {now.year} {now:%H:%M}'


def build_line(cfg: dict, now: datetime | None = None) -> str:
    """The whole line; empty if both parts are switched off."""
    now = now or datetime.now().astimezone()
    parts = []
    if cfg['display']['show_usage']:
        try:
            import _usage
            thresholds = cfg['thresholds']
            usage = _usage.read_usage(now=now, max_age_min=thresholds['max_age_min'])
            parts.append(usage_text(usage, thresholds['start_block_5h'], now))
        except Exception:  # a broken usage part must not take the time part down with it
            pass
    if cfg['display']['show_time']:
        parts.append(time_text(now))
    return ' · '.join(parts)


def main() -> int:
    try:
        import _config
        import _hookio
        _hookio.read_input()
        line = build_line(_config.load(log_problems=True))
        if line:
            # Claude Code reads UTF-8; a machine with another locale must not swallow the line.
            sys.stdout.reconfigure(encoding='utf-8')
            _hookio.emit_context('UserPromptSubmit', line)
    except Exception:  # the display must never disturb a prompt, not even with a missing sibling file
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
