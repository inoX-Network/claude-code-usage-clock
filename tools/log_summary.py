#!/usr/bin/env python3
"""Summarises the JSONL log of the hooks: the basis for deciding when to switch from shadow to enforce.

Shows the decisions per hook and mode, which starts and tool calls WOULD HAVE BEEN denied in shadow mode (with
the reason), which were denied in enforce mode, internal errors, problems found in config.json, which input
fields Claude Code really delivers to the spawn gate, and how many soft stop calls carried an agent_id.

Usage: python3 log_summary.py [PATH] [--since=<ISO time, e.g. 2026-01-14T10:00:00>] [--config PATH]

PATH defaults to <log_dir>/log.jsonl. log_dir is found like the hooks find it: the environment variable
CCUC_LOG_DIR, else `log_dir` from a config.json. That config.json is the one given with --config, else the one
named by CCUC_CONFIG, else the one of the standard installation (STANDARD_CONFIG below); if there is none, the
default log folder is used. --config beats CCUC_LOG_DIR, since it is the more explicit choice. Times without a
zone count as UTC, like the times in the log. --since hides older lines (for example test runs before the
real shadow period). Exit codes: 0 ok, 1 no log or no entries, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hooks'))
import _config  # noqa: E402

STANDARD_CONFIG = '~/.claude/hooks/usage-clock/config.json'


def resolve_log_dir(config: str | None) -> str:
    """The log folder, found like the hooks find it, but with the config.json of the standard installation as
    the fallback (the hooks run from there, this tool runs from a checkout)."""
    if config is None and os.environ.get('CCUC_LOG_DIR'):
        return os.path.expanduser(os.environ['CCUC_LOG_DIR'])
    chosen = config or os.environ.get('CCUC_CONFIG') or os.path.expanduser(STANDARD_CONFIG)
    with mock.patch.dict(os.environ, {'CCUC_CONFIG': chosen}):  # a missing or broken file gives the defaults
        return _config.load(log_problems=False)['log_dir']


def parse_time(text) -> datetime | None:
    """ISO 8601, with or without zone (none = UTC). Python 3.10 does not read a trailing Z itself."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def read_entries(path: str, since: datetime | None) -> tuple[list[dict], int]:
    """(entries, number of unreadable lines). A half-written line (parallel appends) is skipped, not fatal."""
    entries, broken = [], 0
    with open(path, encoding='utf-8', errors='replace') as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                broken += 1
                continue
            if not isinstance(entry, dict):
                broken += 1
                continue
            moment = parse_time(entry.get('time'))
            if since is None or (moment is not None and moment >= since):
                entries.append(entry)
    return entries, broken


def _key_list(value) -> tuple[str, ...]:
    return tuple(str(k) for k in value) if isinstance(value, list) else ()


def _denials(entries: list[dict], mode: str) -> dict[str, Counter]:
    found: dict[str, Counter] = defaultdict(Counter)
    for entry in entries:
        if entry.get('mode') == mode and entry.get('decision') == 'deny':
            subject = entry.get('subagent_type') or entry.get('tool_name') or '?'
            found[str(entry.get('hook'))][f'{subject}: {str(entry.get("reason", ""))[:110]}'] += 1
    return found


def summarise(entries: list[dict]) -> list[str]:
    first, last = entries[0].get('time'), entries[-1].get('time')
    count = f'{len(entries)} entr' + ('y' if len(entries) == 1 else 'ies')
    lines = [f'{count}, {first} to {last}', '', 'Decisions per hook and mode:']
    counts = Counter((str(e.get('hook')), str(e.get('mode') or '-'), str(e.get('decision'))) for e in entries)
    for (hook, mode, decision), n in sorted(counts.items()):
        lines.append(f'  {hook:14s} {mode:8s} {decision:9s} {n}')
    for title, mode in (('Would have denied (shadow mode)', 'shadow'), ('Denied (enforce mode)', 'enforce')):
        for hook, reasons in _denials(entries, mode).items():
            lines += ['', f'{title}, {hook}:']
            lines += [f'  {n:4d}x {text}' for text, n in reasons.most_common(12)]
    errors = Counter((str(e.get('hook')), str(e.get('reason', ''))) for e in entries if e.get('decision') == 'error')
    if errors:
        lines += ['', 'Internal errors (the hook failed, see the reason):']
        lines += [f'  {n:4d}x {hook}: {reason[:110]}' for (hook, reason), n in errors.most_common(12)]
    config = Counter(str(e.get('reason', '')) for e in entries if e.get('hook') == 'config')
    if config:
        lines += ['', 'Problems found in config.json (the affected keys ran on their defaults):']
        lines += [f'  {n:4d}x {reason[:200]}' for reason, n in config.most_common(5)]
    fields = Counter((str(e.get('tool_name')), _key_list(e.get('tool_input_keys')))
                     for e in entries if e.get('hook') == 'spawn_gate')
    lines += ['', 'Input fields of the spawn gate, as Claude Code delivers them:']
    lines += [f'  {tool}: {list(keys)} ({n}x)' for (tool, keys), n in fields.most_common(8)] or ['  none']
    stops = [e for e in entries if e.get('hook') == 'soft_stop']
    types = Counter(str(e.get('agent_type') or '-') for e in stops if e.get('agent_id'))
    lines += ['', f'Soft stop calls with agent_id: {sum(types.values())} of {len(stops)} - agent_type: '
                  f'{dict(types.most_common(8))}']
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='log_summary.py', allow_abbrev=False,
                                     description='Summarises the JSONL log of the usage-clock hooks.')
    parser.add_argument('path', nargs='?', help='log file (default: <log_dir>/log.jsonl)')
    parser.add_argument('--since', metavar='ISO', help='ignore older entries, e.g. 2026-01-14T10:00:00')
    parser.add_argument('--config', metavar='PATH',
                        help='config.json to take log_dir from (default: CCUC_CONFIG, else the config.json of the '
                             'standard installation, else the default folder)')
    args = parser.parse_args(argv)
    since = None
    if args.since is not None:
        since = parse_time(args.since)
        if since is None:
            parser.error('--since must be an ISO 8601 time, e.g. 2026-01-14T10:00:00')
    config = os.path.expanduser(args.config) if args.config else None
    if config is not None and not os.path.isfile(config):
        parser.error(f'--config: not a file: {args.config}')
    path = args.path or os.path.join(resolve_log_dir(config), 'log.jsonl')
    if not os.path.isfile(path):
        print(f'No log at {path} - are the hooks running? Pass the path, or set CCUC_LOG_DIR, or point --config '
              '(or CCUC_CONFIG) to a config.json with another log_dir.')
        return 1
    try:
        entries, broken = read_entries(path, since)
    except OSError as error:
        print(f'Cannot read {path}: {error}')
        return 1
    if broken:
        print(f'{broken} unreadable line(s) skipped.')
    if not entries:
        print(f'No entries{" since " + args.since if since else ""} in {path}.')
        return 1
    print('\n'.join(summarise(entries)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
