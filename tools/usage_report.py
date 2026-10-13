#!/usr/bin/env python3
"""What was used in a day, a week or a month: in total and per CLI, measured from the Claude Code transcripts.

Reads only numbers: per request the token counts, the model, the time, the working folder and the ids that tell
requests apart, the agent type of a subagent, the usage line of this tool, and the measurement history and the log
of the hooks. It never reads or prints message text.

Usage: python3 usage_report.py [--day [YYYY-MM-DD] | --week | --month [YYYY-MM] | --since YYYY-MM-DD [--until YYYY-MM-DD]]
                               [--json] [--projects-dir DIR] [--config PATH]
       python3 usage_report.py --archive [--projects-dir DIR] [--config PATH]

The default is today. Days are local days; --week is the subscription week that ends at the weekly reset time
(not a calendar week); --since and --until are whole days, both included. --json prints the same figures as a
stable JSON object. --archive writes every finished day that is not in <log_dir>/usage-days.jsonl yet and prints
nothing: Claude Code deletes transcripts after about 30 days, the archive keeps the day figures (run it daily).

The cost weight is what the tokens would cost at API prices (tools/prices.json); a model without a price is shown
with its tokens as "no price", never estimated. The subscription rise per CLI is an ESTIMATE (marked ~): the rise of
the measured subscription percentages between two measurements is shared by the cost of the CLIs that made requests
in between; a rise without any request stays "unexplained". Exit codes: 0 ok, 1 no transcripts or a file that
cannot be read, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import bisect
import copy
import fcntl
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'hooks'))
sys.path.insert(0, HERE)
import _usage  # noqa: E402
import log_summary  # noqa: E402

PRICES_FILE = os.path.join(HERE, 'prices.json')
PROJECTS_DIR = '~/.claude/projects'
HISTORY_FILE = 'usage-history.jsonl'
ARCHIVE_FILE = 'usage-days.jsonl'
ARCHIVE_VERSION = 1

KINDS = ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'output')   # also the order of the prices
METRICS = ('five', 'week')
MAX_GAP_S = 3600            # a longer stretch between two measurements is too coarse to share
RESET_TOLERANCE_S = 60      # reset times closer than this belong to the same window (as in _usage.py)
COLD_START_TOKENS = 50_000  # a request that writes at least this much cache, after the first, is a cold start
MIN_SECTIONS = 50           # sections with a single CLI needed before a cost per point is shown
FRESH_MAX_MIN = 1           # a usage line counts as a measurement only if it says "measured 1 min ago" at most
WEEK_S = 7 * 86400
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

# Counts of the optional details, kept apart from the sums so that "not reported" is not a zero: how many requests report
# the detail (`_n`), its sum, and for thinking the output tokens of those requests (the denominator of its share).
DETAIL_KEYS = ('thinking_n', 'thinking', 'thinking_out', 'search_n', 'search', 'fetch_n', 'fetch')

NUMBER = r'(\d+(?:\.\d+)?)'
USAGE_LINES = (re.compile(rf'Usage: 5h {NUMBER} %[^·]*· week {NUMBER} %[^·]*· measured (\d+) min ago'),
               re.compile(rf'Nutzung: 5h {NUMBER} %[^·]*· 7T {NUMBER} %[^·]*· Messung vor (\d+) min'))
AGENT_TYPE_KEYS = ('spawnedAgentType', 'customAgentType', 'agentType')     # the first one present is the type
AGENT_TYPE = re.compile(r'[A-Za-z0-9_.:-]{1,64}')
MODEL_NAME = re.compile(r'[A-Za-z0-9_.:/-]{1,64}')
WORD = re.compile(r'[a-z_]{1,20}')
DAY_TEXT = re.compile(r'\d{4}-\d{2}-\d{2}')
MONTH_TEXT = re.compile(r'\d{4}-\d{2}')


# --- Times ----------------------------------------------------------------------------------------------------------

def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_ts(text) -> float | None:
    moment = log_summary.parse_time(text)
    return moment.timestamp() if moment else None


def local_day(ts: float) -> date:
    return datetime.fromtimestamp(ts).date()


def day_start(day: date) -> float:
    return datetime(day.year, day.month, day.day).timestamp()


def day_bounds(day: date) -> tuple[float, float]:
    return day_start(day), day_start(day + timedelta(days=1))


def clock(ts: float, with_date: bool = False) -> str:
    moment = datetime.fromtimestamp(ts)
    stamp = f'{WEEKDAYS[moment.weekday()]} {moment.day} {MONTHS[moment.month - 1]} ' if with_date else ''
    return f'{stamp}{moment:%H:%M}'


# --- Prices ---------------------------------------------------------------------------------------------------------

class PriceFileError(Exception):
    pass


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_rates(rates, where: str) -> None:
    if not isinstance(rates, dict) or not all(_is_number(rates.get(k)) and rates[k] >= 0 for k in KINDS):
        raise PriceFileError(f'{where}: needs a number for each of {", ".join(KINDS)}')


def load_prices(path: str) -> list[dict]:
    """The entries of prices.json, in file order; the first whose `match` is part of the model name wins."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError) as error:
        raise PriceFileError(f'{path}: {error}') from error
    models = data.get('models') if isinstance(data, dict) else None
    if not isinstance(models, list) or not models:
        raise PriceFileError(f'{path}: needs a non-empty "models" list')
    for entry in models:
        if not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not isinstance(entry.get('match'), str):
            raise PriceFileError(f'{path}: every model needs a "name" and a "match" text')
        _check_rates(entry, f'{path}: {entry["name"]}')
        tier = entry.get('long_context')
        if tier is not None:
            _check_rates(tier, f'{path}: {entry["name"]} long_context')
            if not _is_number(tier.get('above_tokens')):
                raise PriceFileError(f'{path}: {entry["name"]} long_context needs "above_tokens"')
    return models


def find_price(prices: list[dict], model: str) -> dict | None:
    name = model.lower()
    return next((entry for entry in prices if entry['match'] in name), None)


def request_cost(entry: dict, tokens: tuple) -> float:
    """USD for one request. Above `above_tokens` of context (input, cache write and cache read) the tier's prices apply."""
    rates = entry
    tier = entry.get('long_context')
    if tier is not None and sum(tokens[:4]) > tier['above_tokens']:
        rates = tier
    return sum(count * rates[kind] for count, kind in zip(tokens, KINDS)) / 1e6


# --- Reading the sources ------------------------------------------------------------------------------------------------

class Request(NamedTuple):
    ts: float
    session: str
    sub: bool
    agent_type: str | None
    model: str
    tokens: tuple          # in KINDS order
    cwd: str | None
    thinking: int | None = None    # part of the output tokens; None = the transcript does not say
    searches: int | None = None
    fetches: int | None = None


class Measurement(NamedTuple):
    t: float
    five: float | None
    week: float | None
    five_resets: float | None
    week_resets: float | None


def _count(value) -> int:
    return int(value) if _is_number(value) and value > 0 else 0


def usage_tokens(usage: dict) -> tuple:
    total = _count(usage.get('cache_creation_input_tokens'))
    detail = usage.get('cache_creation') if isinstance(usage.get('cache_creation'), dict) else {}
    one_hour = _count(detail.get('ephemeral_1h_input_tokens'))
    if not total:
        total = one_hour + _count(detail.get('ephemeral_5m_input_tokens'))
    one_hour = min(one_hour, total)
    return (_count(usage.get('input_tokens')), total - one_hour, one_hour, _count(usage.get('cache_read_input_tokens')),
            _count(usage.get('output_tokens')))


def _known(value) -> int | None:
    return int(value) if _is_number(value) and value >= 0 else None


def usage_details(usage: dict, output: int) -> tuple:
    """(thinking tokens, web searches, web fetches); None for what the line does not say. Thinking is part of the output."""
    details = usage.get('output_tokens_details') if isinstance(usage.get('output_tokens_details'), dict) else {}
    tools = usage.get('server_tool_use') if isinstance(usage.get('server_tool_use'), dict) else {}
    thinking = _known(details.get('thinking_tokens'))
    return (None if thinking is None else min(thinking, output), _known(tools.get('web_search_requests')),
            _known(tools.get('web_fetch_requests')))


def parse_request(raw: str) -> tuple | None:
    """(message id, request id, time, tokens, model, cwd, details) of an assistant line with usage numbers, else None."""
    try:
        line = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    message = line.get('message') if isinstance(line, dict) and line.get('type') == 'assistant' else None
    usage = message.get('usage') if isinstance(message, dict) else None
    ts = parse_ts(line.get('timestamp')) if isinstance(usage, dict) else None
    if ts is None:
        return None
    tokens = usage_tokens(usage)
    if not any(tokens):
        return None
    model = message.get('model') if isinstance(message.get('model'), str) else '?'
    cwd = line.get('cwd') if isinstance(line.get('cwd'), str) else None
    return message.get('id'), line.get('requestId'), ts, tokens, model, cwd, usage_details(usage, tokens[4])


def parse_usage_line(raw: str) -> tuple | None:
    """(time, five, week) of a fresh usage line of this tool in an attachment, else None."""
    try:
        line = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    ts = parse_ts(line.get('timestamp')) if isinstance(line, dict) and line.get('type') == 'attachment' else None
    if ts is None:
        return None
    text = json.dumps(line.get('attachment'), ensure_ascii=False)
    found = next((match for match in (pattern.search(text) for pattern in USAGE_LINES) if match), None)
    if found is None or float(found.group(3)) > FRESH_MAX_MIN:
        return None
    return ts, float(found.group(1)), float(found.group(2))


def agent_type_of(transcript: str) -> str:
    """The type of a subagent from agent-<id>.meta.json next to its transcript: `spawnedAgentType`, else
    `customAgentType`, else `agentType` (for a named subagent that one holds the name, which is never read)."""
    try:
        with open(transcript[:-len('.jsonl')] + '.meta.json', encoding='utf-8') as f:
            data = json.loads(f.read(65536))
    except (OSError, ValueError, RecursionError):
        return 'unknown'
    data = data if isinstance(data, dict) else {}
    value = next((data[key] for key in AGENT_TYPE_KEYS if data.get(key) is not None), None)
    if value is None:
        return 'unknown'
    return value if isinstance(value, str) and AGENT_TYPE.fullmatch(value) else 'other'


class Data:
    """Everything read: the requests (sorted by time), the measurements, the log of the gates."""

    def __init__(self):
        self.requests: list[Request] = []
        self.times: list[float] = []
        self.measurements: list[Measurement] = []
        self.measurement_times: list[float] = []
        self.guards: list[tuple] = []            # (time, hook, mode, decision)
        self.files = 0
        self.session_of: dict[str, str] = {}     # first 8 characters of a session id -> the session
        self.label: dict[str, str] = {}
        self.start: dict[str, float] = {}
        self.first_main: dict[str, int] = {}
        self.price_cache: dict[str, dict | None] = {}

    def set_requests(self, requests: list[Request]) -> None:
        self.requests = sorted(requests, key=lambda r: r.ts)
        self.times = [r.ts for r in self.requests]
        for index, request in enumerate(self.requests):
            self.start.setdefault(request.session, request.ts)
            self.session_of.setdefault(request.session[:8], request.session)
            if request.session not in self.label and request.cwd:
                folder = re.sub(r'[^\w .+@=-]', '?', os.path.basename(request.cwd.rstrip('/')))[:64]
                if folder:
                    self.label[request.session] = folder
            if not request.sub:
                self.first_main.setdefault(request.session, index)

    def set_measurements(self, measurements: list[Measurement]) -> None:
        self.measurements = sorted(measurements, key=lambda m: m.t)
        self.measurement_times = [m.t for m in self.measurements]


def find_transcripts(projects_dir: str):
    """(path, session, is_subagent) of every transcript below the folder, links not followed."""
    for folder, _dirs, files in os.walk(projects_dir):
        for name in sorted(files):
            path = os.path.join(folder, name)
            parts = os.path.relpath(path, projects_dir).split(os.sep)
            if name.endswith('.jsonl') and len(parts) >= 2 and not os.path.islink(path):
                yield path, parts[1][:-len('.jsonl')] if len(parts) == 2 else parts[1], len(parts) > 2


def read_transcripts(projects_dir: str, modified_since: float, want_usage_lines: bool) -> tuple[list[Request], dict, int]:
    """(requests, usage-line measurements by time, files read). Of the lines of one request (same message id and
    request id) only the last one counts: it carries the complete output tokens. Files not modified since
    `modified_since` cannot hold anything newer and are not opened."""
    best: dict = {}
    fresh: dict[float, tuple] = {}
    files = 0
    for path, session, sub in find_transcripts(projects_dir):
        try:
            if os.stat(path).st_mtime < modified_since:
                continue
            handle = open(path, encoding='utf-8', errors='replace')
        except OSError:
            continue
        files += 1
        agent_type = agent_type_of(path) if sub else None
        with handle:
            for number, raw in enumerate(handle):
                if '"usage"' in raw:
                    parsed = parse_request(raw)
                    if parsed is not None:
                        message_id, request_id, ts, tokens, model, cwd, details = parsed
                        key = (message_id, request_id) if isinstance(message_id, str) and message_id else (path, number)
                        if key not in best or tokens[4] >= best[key][0]:
                            best[key] = (tokens[4], Request(ts, session, sub, agent_type, model, tokens, cwd, *details))
                if want_usage_lines and ('Usage: 5h' in raw or 'Nutzung: 5h' in raw) and '"attachment"' in raw:
                    parsed = parse_usage_line(raw)
                    if parsed is not None:
                        fresh[parsed[0]] = parsed[1:]
    return [value[1] for value in best.values()], fresh, files


def _optional_number(row: dict, key: str):
    value = row.get(key)
    if value is not None and not _is_number(value):
        raise ValueError(key)
    return value


def read_lines(path: str):
    """The non-empty lines of a file; nothing if it cannot be opened."""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for raw in f:
                if raw.strip():
                    yield raw
    except OSError:
        return


def read_history(log_dir: str) -> tuple[list[Measurement], int]:
    """(measurements, unreadable lines) of usage-history.jsonl.1 and usage-history.jsonl."""
    rows, broken = [], 0
    for name in (HISTORY_FILE + '.1', HISTORY_FILE):
        for raw in read_lines(os.path.join(log_dir, name)):
            try:
                row = json.loads(raw)
                ts = parse_ts(row['t'])
                if ts is None:
                    raise ValueError('t')
                rows.append(Measurement(ts, *(_optional_number(row, key) for key in
                                              ('five', 'week', 'five_resets', 'week_resets'))))
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                broken += 1
    return rows, broken


def read_guard_log(log_dir: str) -> list[tuple]:
    entries = []
    for name in ('log.jsonl.1', 'log.jsonl'):
        for raw in read_lines(os.path.join(log_dir, name)):
            try:
                row = json.loads(raw)
                ts = parse_ts(row['time'])
                if ts is not None and row['hook'] in ('spawn_gate', 'soft_stop'):
                    entries.append((ts, row['hook'], _word(row.get('mode'), 'unknown'), _word(row.get('decision'), 'other')))
            except (ValueError, TypeError, KeyError, RecursionError):
                continue
    return entries


def _word(value, fallback: str) -> str:
    return value if isinstance(value, str) and WORD.fullmatch(value) else fallback


def read_archive(log_dir: str) -> tuple[dict[str, dict], int]:
    """({day: stats}, unreadable lines). The first line of a day counts."""
    days, broken = {}, 0
    for raw in read_lines(os.path.join(log_dir, ARCHIVE_FILE)):
        try:
            row = json.loads(raw)
            day, stats = row['day'], row['stats']
            if row['v'] != ARCHIVE_VERSION or not isinstance(stats, dict) or not DAY_TEXT.fullmatch(day):
                raise ValueError('shape')
            date.fromisoformat(day)
        except (ValueError, TypeError, KeyError, RecursionError):
            broken += 1
            continue
        days.setdefault(day, stats)
    return days, broken


# --- Figures of a period, mergeable ---------------------------------------------------------------------------------------
# A figure set is a dict of numbers (nested dicts of counts), so that the sets of several days, read from the archive
# or computed, add up to the set of a longer period.

def new_sums() -> dict:
    return {'n': 0, 'c2': 0.0, 'cd': 0.0}      # sections, sum of cost^2, sum of cost x rise: slope through zero


def new_cli(label: str, start: float) -> dict:
    return {'label': label, 'start': start, 'requests': 0, 'cost': 0.0, 'tokens': dict.fromkeys(KINDS, 0), 'models': {},
            'subs': {}, 'cold_starts': 0, 'five_pts': 0.0, 'week_pts': 0.0}


def new_stats() -> dict:
    return {'requests': 0, 'cost': 0.0, 'cold_starts': 0, 'extra': {}, 'tokens': dict.fromkeys(KINDS, 0),
            'main': {'requests': 0, 'cost': 0.0},
            'sub': {'requests': 0, 'cost': 0.0}, 'agent_types': {}, 'models': {}, 'unpriced': {}, 'clis': {},
            'abo': {'week_start': None, 'week_end': None, 'week_resets': None, 'five_windows': []},
            'alloc': {m: {'counted': 0.0, 'attributed': 0.0, 'rest': 0.0, 'skipped_sections': 0, 'skipped_points': 0.0}
                      for m in METRICS},
            'cal': {m: {'all': new_sums(), 'models': {}, 'days': {}} for m in METRICS},
            'guards': {}}


def _add_counts(target: dict, extra) -> None:
    """Adds the numbers of a nested dict. Anything that is not a number or a dict raises."""
    if not isinstance(extra, dict):
        raise TypeError('not a dict')
    for key, value in extra.items():
        if isinstance(value, dict):
            _add_counts(target.setdefault(key, {}), value)
        elif _is_number(value):
            target[key] = target.get(key, 0) + value
        else:
            raise TypeError(key)


def _merge_window(windows: list[dict], window: dict) -> None:
    resets = window['resets']
    for known in windows:
        if resets is not None and known['resets'] is not None and abs(known['resets'] - resets) < RESET_TOLERANCE_S:
            known['first'] = min(known['first'], window['first'])
            if (window['peak'], -window['peak_at']) > (known['peak'], -known['peak_at']):
                known['peak'], known['peak_at'] = window['peak'], window['peak_at']
            return
    windows.append(dict(window))


def _merge_abo(target: dict, extra) -> None:
    if not isinstance(extra, dict):
        raise TypeError('abo')
    for key, later in (('week_start', False), ('week_end', True)):
        point = extra.get(key)
        if point is not None:
            if not (_is_number(point['t']) and _is_number(point['value'])):
                raise TypeError(key)
            known = target[key]
            if known is None or (point['t'] > known['t'] if later else point['t'] < known['t']):
                target[key] = {'t': point['t'], 'value': point['value']}
    if extra.get('week_resets') is not None:
        if not _is_number(extra['week_resets']):
            raise TypeError('week_resets')
        target['week_resets'] = max(target['week_resets'] or 0, extra['week_resets'])
    for window in extra.get('five_windows', []):
        resets = window['resets']
        numbers = all(_is_number(window[k]) for k in ('first', 'peak', 'peak_at'))
        if not numbers or not (resets is None or _is_number(resets)):
            raise TypeError('window')
        _merge_window(target['five_windows'], {k: window[k] for k in ('resets', 'first', 'peak', 'peak_at')})
    target['five_windows'].sort(key=lambda w: w['first'])


def merge_stats(target: dict, extra) -> None:
    """Adds the figures `extra` (computed or from the archive) to `target`. Raises if `extra` is malformed; the
    caller merges into a copy then."""
    target['requests'] += extra.get('requests', 0)
    target['cost'] += extra.get('cost', 0)
    target['cold_starts'] += extra.get('cold_starts', 0)
    for key in ('extra', 'tokens', 'main', 'sub', 'agent_types', 'models', 'unpriced', 'alloc', 'cal', 'guards'):
        _add_counts(target[key], extra.get(key, {}))
    for ident, cli in extra.get('clis', {}).items():
        if not (isinstance(cli['label'], str) and _is_number(cli['start'])):
            raise TypeError(ident)
        mine = target['clis'].setdefault(ident, new_cli(cli['label'], cli['start']))
        mine['start'] = min(mine['start'], cli['start'])
        for key in ('requests', 'cost', 'cold_starts', 'five_pts', 'week_pts'):
            mine[key] += cli.get(key, 0)
        for key in ('tokens', 'models', 'subs'):
            _add_counts(mine[key], cli.get(key, {}))
        _add_counts(mine, {key: cli[key] for key in DETAIL_KEYS if key in cli})
    _merge_abo(target['abo'], extra.get('abo', {}))


# --- Computing the figures of intervals ---------------------------------------------------------------------------------

def _price_of(data: Data, prices: list[dict], model: str) -> dict | None:
    if model not in data.price_cache:
        data.price_cache[model] = find_price(prices, model)
    return data.price_cache[model]


def request_details(request: Request) -> dict:
    """The details a request reports, as counts for DETAIL_KEYS; a detail it does not report adds nothing."""
    counts = {}
    if request.thinking is not None:
        counts.update(thinking_n=1, thinking=request.thinking, thinking_out=request.tokens[4])
    if request.searches is not None:
        counts.update(search_n=1, search=request.searches)
    if request.fetches is not None:
        counts.update(fetch_n=1, fetch=request.fetches)
    return counts


def _count_request(stats: dict, data: Data, prices: list[dict], index: int) -> None:
    request = data.requests[index]
    entry = _price_of(data, prices, request.model)
    cost = request_cost(entry, request.tokens) if entry else 0.0
    name = entry['name'] if entry else (request.model if MODEL_NAME.fullmatch(request.model) else '?')
    details = request_details(request)
    tokens = dict(zip(KINDS, request.tokens))
    stats['requests'] += 1
    stats['cost'] += cost
    _add_counts(stats['extra'], details)
    side = stats['sub' if request.sub else 'main']
    side['requests'] += 1
    side['cost'] += cost
    for kind, count in zip(KINDS, request.tokens):
        stats['tokens'][kind] += count
    if entry:
        _add_counts(stats['models'], {name: {'requests': 1, 'cost': cost, 'tokens': tokens, **details}})
    else:
        _add_counts(stats['unpriced'], {name: {'requests': 1, 'tokens': tokens, **details}})
    if request.sub:
        _add_counts(stats['agent_types'], {request.agent_type: {'requests': 1, 'cost': cost}})
    cli = _cli(stats, data, request.session)
    _add_counts(cli, details)
    cli['requests'] += 1
    cli['cost'] += cost
    for kind, count in zip(KINDS, request.tokens):
        cli['tokens'][kind] += count
    cli['models'][name] = cli['models'].get(name, 0.0) + cost
    if request.sub:
        _add_counts(cli['subs'], {request.agent_type: {'requests': 1, 'cost': cost}})
    elif index != data.first_main[request.session] and request.tokens[1] + request.tokens[2] >= COLD_START_TOKENS:
        cli['cold_starts'] += 1
        stats['cold_starts'] += 1


def _cli(stats: dict, data: Data, session: str) -> dict:
    return stats['clis'].setdefault(session[:8], new_cli(data.label.get(session, '?'), data.start[session]))


def _delta(first: Measurement, second: Measurement, metric: str) -> tuple[str | None, float]:
    """(how the stretch between two measurements counts, rise in points): 'ok', 'reset' (the window renewed or the value
    fell), 'gap' (longer than MAX_GAP_S) or None (a value is missing)."""
    before, after = getattr(first, metric), getattr(second, metric)
    if before is None or after is None:
        return None, 0
    resets = getattr(first, metric + '_resets'), getattr(second, metric + '_resets')
    if after < before or (None not in resets and abs(resets[1] - resets[0]) >= RESET_TOLERANCE_S):
        return 'reset', 0
    if second.t - first.t > MAX_GAP_S:
        return 'gap', after - before
    return 'ok', after - before


def _section_costs(data: Data, prices: list[dict], first: float, second: float) -> tuple[dict, set, bool]:
    """(cost by CLI, model names, whether a request has no price) of the requests after `first` up to `second`."""
    costs: dict[str, float] = {}
    models: set = set()
    unpriced = False
    lo, hi = bisect.bisect_right(data.times, first), bisect.bisect_right(data.times, second)
    for request in data.requests[lo:hi]:
        entry = _price_of(data, prices, request.model)
        ident = request.session[:8]
        costs[ident] = costs.get(ident, 0.0) + (request_cost(entry, request.tokens) if entry else 0.0)
        if entry:
            models.add(entry['name'])
        else:
            unpriced = True
    return costs, models, unpriced


def _count_sections(stats: dict, data: Data, prices: list[dict], start: float, end: float) -> None:
    times, measurements = data.measurement_times, data.measurements
    for index in range(max(bisect.bisect_left(times, start), 1), bisect.bisect_left(times, end)):
        first, second = measurements[index - 1], measurements[index]
        section = None
        for metric in METRICS:
            status, rise = _delta(first, second, metric)
            if status is None:
                continue
            alloc = stats['alloc'][metric]
            if status != 'ok':
                alloc['skipped_sections'] += 1
                alloc['skipped_points'] += rise
                continue
            alloc['counted'] += rise
            section = section or _section_costs(data, prices, first.t, second.t)
            costs, models, unpriced = section
            total = sum(costs.values())
            if total <= 0:
                alloc['rest'] += rise       # no request in between, or none with a price: nobody to share it with
                continue
            alloc['attributed'] += rise
            for ident, cost in costs.items():
                _cli(stats, data, data.session_of[ident])[metric + '_pts'] += rise * cost / total
            if len(costs) == 1 and not unpriced:
                cal = stats['cal'][metric]
                sums = [cal['all'], cal['days'].setdefault(local_day(second.t).isoformat(), new_sums())]
                if len(models) == 1:
                    sums.append(cal['models'].setdefault(next(iter(models)), new_sums()))
                for entry in sums:
                    entry['n'] += 1
                    entry['c2'] += total * total
                    entry['cd'] += total * rise


def _same_window(before: Measurement, first: Measurement) -> bool:
    if before.week_resets is not None and first.week_resets is not None:
        return abs(first.week_resets - before.week_resets) < RESET_TOLERANCE_S
    return first.week >= before.week


def _subscription(data: Data, start: float, end: float) -> dict:
    times, measurements = data.measurement_times, data.measurements
    lo, hi = bisect.bisect_left(times, start), bisect.bisect_left(times, end)
    inside = measurements[lo:hi]
    piece = {'week_start': None, 'week_end': None, 'week_resets': None, 'five_windows': []}
    weekly = [m for m in inside if m.week is not None]
    if weekly:
        before = next((m for m in reversed(measurements[:lo]) if m.week is not None), None)
        first = before if before is not None and _same_window(before, weekly[0]) else weekly[0]
        piece['week_start'] = {'t': first.t, 'value': first.week}
        piece['week_end'] = {'t': weekly[-1].t, 'value': weekly[-1].week}
        piece['week_resets'] = next((m.week_resets for m in reversed(weekly) if m.week_resets is not None), None)
    previous = None
    for m in inside:
        if m.five is None:
            continue
        resets = (previous.five_resets, m.five_resets) if previous else (None, None)
        renewed = previous is None or m.five < previous.five or (
            None not in resets and abs(resets[1] - resets[0]) >= RESET_TOLERANCE_S)
        if renewed:
            piece['five_windows'].append({'resets': m.five_resets, 'first': m.t, 'peak': m.five, 'peak_at': m.t})
        else:
            window = piece['five_windows'][-1]
            if m.five > window['peak']:
                window['peak'], window['peak_at'] = m.five, m.t
            if window['resets'] is None:
                window['resets'] = m.five_resets
        previous = m
    return piece


def compute_stats(data: Data, prices: list[dict], intervals: list[tuple[float, float]]) -> dict:
    """The figures of the requests, measurements and gate decisions inside the (half open) intervals."""
    stats = new_stats()
    for start, end in intervals:
        for index in range(bisect.bisect_left(data.times, start), bisect.bisect_left(data.times, end)):
            _count_request(stats, data, prices, index)
        _count_sections(stats, data, prices, start, end)
        _merge_abo(stats['abo'], _subscription(data, start, end))
        for ts, hook, mode, decision in data.guards:
            if start <= ts < end:
                _add_counts(stats['guards'], {hook: {mode: {decision: 1}}})
    return stats


# --- The report ---------------------------------------------------------------------------------------------------

def _usd_per_point(sums: dict) -> float | None:
    """Slope through zero of the rise on the cost, inverted: sum(cost^2) / sum(cost x rise)."""
    return sums['c2'] / sums['cd'] if sums['cd'] > 0 else None


def _per_point(table: dict, minimum: int = 0) -> dict:
    return {name: {'sections': s['n'], 'usd_per_point': _usd_per_point(s)} for name, s in sorted(table.items())
            if s['n'] >= minimum and (minimum == 0 or _usd_per_point(s) is not None)}


def _calibration(cal: dict) -> dict:
    overall = _usd_per_point(cal['all'])
    calibrated = cal['all']['n'] >= MIN_SECTIONS and overall is not None
    return {'status': 'calibrated' if calibrated else 'not calibrated yet', 'sections': cal['all']['n'],
            'usd_per_point': overall if calibrated else None,
            'per_day': _per_point(cal['days']), 'per_model': _per_point(cal['models'], MIN_SECTIONS)}


def _by_cost(table: dict) -> list:
    return sorted(table.items(), key=lambda item: (-item[1]['cost'], item[0]))


def _hit_rate(tokens: dict) -> float | None:
    everything = sum(tokens[kind] for kind in KINDS[:4])
    return tokens['cache_read'] / everything if everything else None


def _details_report(counts: dict) -> dict:
    """Thinking and web figures; None where no request reported them. The share is of the output of the requests that
    report thinking."""
    reported = counts.get('thinking_n', 0) > 0
    return {'thinking_tokens': counts['thinking'] if reported else None,
            'thinking_share': counts['thinking'] / counts['thinking_out'] if reported and counts['thinking_out'] else None,
            'web_searches': counts['search'] if counts.get('search_n') else None,
            'web_fetches': counts['fetch'] if counts.get('fetch_n') else None}


def _models_report(table: dict, priced: bool) -> dict:
    items = _by_cost(table) if priced else sorted(table.items())
    return {name: {'requests': v['requests'], **({'cost_usd': v['cost']} if priced else {}), 'tokens': v['tokens'],
                   'cache_hit_rate': _hit_rate(v['tokens']), **_details_report(v)} for name, v in items}


def _requests_and_cost(table: dict) -> dict:
    return {name: {'requests': v['requests'], 'cost_usd': v['cost']} for name, v in _by_cost(table)}


def _point(entry: dict | None) -> dict | None:
    return None if entry is None else {'at': iso(entry['t']), 'value': entry['value']}


def _cli_report(ident: str, cli: dict) -> dict:
    return {'id': ident, 'label': cli['label'], 'start': iso(cli['start']), 'requests': cli['requests'],
            'cost_usd': cli['cost'], 'tokens': cli['tokens'],
            'models': dict(sorted(cli['models'].items(), key=lambda item: -item[1])),
            'subagents': _requests_and_cost(cli['subs']),
            'cache_hit_rate': _hit_rate(cli['tokens']), 'cold_starts': cli['cold_starts'],
            'estimated_share': {'five': cli['five_pts'], 'week': cli['week_pts']}, **_details_report(cli)}


def _allocation(alloc: dict) -> dict:
    return {'counted_points': alloc['counted'], 'attributed_points': alloc['attributed'],
            'unexplained_points': alloc['rest'], 'skipped_sections': alloc['skipped_sections'],
            'skipped_points': alloc['skipped_points']}


def build_report(stats: dict, period: dict, coverage: dict) -> dict:
    abo = stats['abo']
    windows = [{'resets': w['resets'], 'first_seen': iso(w['first']), 'peak': w['peak'], 'peak_at': iso(w['peak_at'])}
               for w in abo['five_windows']]
    subagents = {'requests': stats['sub']['requests'], 'cost_usd': stats['sub']['cost'],
                 'by_type': _requests_and_cost(stats['agent_types'])}
    return {
        'period': period,
        'coverage': coverage,
        'subscription': {'week_start': _point(abo['week_start']), 'week_end': _point(abo['week_end']),
                         'week_resets': abo['week_resets'], 'five_windows': windows},
        'total': {'requests': stats['requests'], 'tokens': stats['tokens'], 'cost_usd': stats['cost'],
                  'main': {'requests': stats['main']['requests'], 'cost_usd': stats['main']['cost']},
                  'subagents': subagents, 'models': _models_report(stats['models'], True),
                  'unpriced': _models_report(stats['unpriced'], False), 'cache_hit_rate': _hit_rate(stats['tokens']),
                  'cold_starts': stats['cold_starts'], **_details_report(stats['extra'])},
        'clis': [_cli_report(ident, cli) for ident, cli in _by_cost(stats['clis'])],
        'allocation': {metric: _allocation(alloc) for metric, alloc in stats['alloc'].items()},
        'calibration': {metric: _calibration(cal) for metric, cal in stats['cal'].items()},
        'guards': stats['guards'],
    }


def tokens_text(count: float) -> str:
    return f'{count / 1e6:.1f}M' if count >= 1e6 else f'{count / 1e3:.0f}k' if count >= 1e4 else str(int(count))


def _kinds_text(tokens: dict) -> str:
    return ', '.join(f'{kind.replace("_", " ")} {tokens_text(tokens[kind])}' for kind in KINDS)


def _costs_text(table: dict) -> str:
    return ', '.join(f'{name} {v["cost_usd"]:.2f} USD ({v["requests"]})' for name, v in table.items())


def _subscription_lines(abo: dict) -> list[str]:
    lines = ['Subscription']
    if abo['week_start'] is not None:
        start, end = abo['week_start']['value'], abo['week_end']['value']
        reset = f', resets {clock(abo["week_resets"], True)}' if abo['week_resets'] is not None else ''
        lines.append(f'  Week: {start:g} % -> {end:g} % ({end - start:+g} points){reset}')
    else:
        lines.append('  Week: no measurement in this period')
    lines.append(f'  5h windows: {len(abo["five_windows"])}')
    for window in abo['five_windows']:
        reset = f'reset {clock(window["resets"])}, ' if window['resets'] is not None else ''
        lines.append(f'    {reset}peak {window["peak"]:g} % at {clock(parse_ts(window["peak_at"]))}'
                     f' (first seen {clock(parse_ts(window["first_seen"]))})')
    return lines


def _percent(share: float | None) -> str:
    return 'n/a' if share is None else f'{100 * share:.0f} %'


def _count_text(count: int | None) -> str:
    return 'n/a' if count is None else str(count)


def _details_text(block: dict) -> str:
    thinking = ('thinking n/a' if block['thinking_tokens'] is None
                else f'thinking {tokens_text(block["thinking_tokens"])} ({_percent(block["thinking_share"])})')
    return (f'{thinking}, web searches {_count_text(block["web_searches"])}, '
            f'web fetches {_count_text(block["web_fetches"])}')


def _total_lines(total: dict) -> list[str]:
    lines = ['Total', f'  Requests: {total["requests"]} (main {total["main"]["requests"]}, '
                      f'subagents {total["subagents"]["requests"]})',
             f'  Tokens: {_kinds_text(total["tokens"])}',
             f'  Cost weight: {total["cost_usd"]:.2f} USD at API prices (main {total["main"]["cost_usd"]:.2f}, '
             f'subagents {total["subagents"]["cost_usd"]:.2f})']
    lines += [f'  Output: {_details_text(total)}',
              f'  Cache: hit rate {_percent(total["cache_hit_rate"])}, cold starts {total["cold_starts"]}']
    if total['subagents']['by_type']:
        lines.append(f'  Subagents by type: {_costs_text(total["subagents"]["by_type"])}')
    if total['models'] or total['unpriced']:
        lines.append('  Models:')
    for name, model in total['models'].items():
        lines.append(f'    model {name}: {model["cost_usd"]:.2f} USD, {model["requests"]} requests; '
                     f'{_details_text(model)}; cache hits {_percent(model["cache_hit_rate"])}')
    for name, model in total['unpriced'].items():
        lines.append(f'    model {name}: no price (not in the cost weight), {model["requests"]} request(s), '
                     f'{tokens_text(sum(model["tokens"].values()))} tokens; {_details_text(model)}')
    return lines


def _cli_lines(clis: list[dict]) -> list[str]:
    lines = ['By CLI (main session and its subagents, most expensive first; ~ = estimate)']
    for cli in clis:
        share = cli['estimated_share']
        lines.append(f'  {cli["label"]}  {cli["id"]}  started {clock(parse_ts(cli["start"]), True)}  '
                     f'{cli["cost_usd"]:.2f} USD  {cli["requests"]} requests  ~{share["five"]:.1f} 5h points  '
                     f'~{share["week"]:.2f} week points')
        lines.append(f'    tokens {_kinds_text(cli["tokens"])}')
        lines.append('    models ' + ', '.join(f'{name} {cost:.2f}' for name, cost in cli['models'].items()))
        lines.append(f'    output {_details_text(cli)}')
        if cli['subagents']:
            lines.append(f'    subagents {_costs_text(cli["subagents"])}')
        rate = 'n/a' if cli['cache_hit_rate'] is None else f'{100 * cli["cache_hit_rate"]:.1f} %'
        lines.append(f'    cache hits {rate}, cold starts {cli["cold_starts"]}')
    return lines


def _allocation_lines(allocation: dict) -> list[str]:
    lines = ['Allocation of the subscription rise (estimate)']
    for metric, name in (('five', '5h'), ('week', 'week')):
        a = allocation[metric]
        lines.append(f'  {name}: {a["counted_points"]:g} points measured, ~{a["attributed_points"]:.1f} shared by cost '
                     f'between the CLIs, {a["unexplained_points"]:g} unexplained (no request in between)')
        lines.append(f'      {a["skipped_sections"]} stretch(es) not counted (a reset, or over 60 minutes between '
                     f'measurements), {a["skipped_points"]:g} points rise in them')
    return lines


def _calibration_lines(calibration: dict) -> list[str]:
    lines = ['Calibration (USD per point, from stretches in which only one CLI was active)']
    for metric, name in (('five', '5h'), ('week', 'week')):
        cal = calibration[metric]
        if cal['status'] != 'calibrated':
            lines.append(f'  {name}: not calibrated yet ({cal["sections"]} of {MIN_SECTIONS} stretches)'
                         if cal['sections'] < MIN_SECTIONS else f'  {name}: no rise measured in {cal["sections"]} stretches')
            continue
        lines.append(f'  {name}: {cal["usd_per_point"]:.2f} USD per point from {cal["sections"]} stretches')
        days = [f'{day[5:]} {v["usd_per_point"]:.2f} ({v["sections"]})'
                for day, v in cal['per_day'].items() if v['usd_per_point'] is not None]
        if len(days) > 1:
            lines.append('    per day: ' + ', '.join(days))
        if cal['per_model']:
            lines.append('    per model: ' + ', '.join(f'{m} {v["usd_per_point"]:.2f} ({v["sections"]})'
                                                      for m, v in cal['per_model'].items()))
    return lines


def _guard_lines(guards: dict) -> list[str]:
    lines = ['Gates (in shadow mode a decision is logged, nothing is blocked)']
    for hook, name in (('spawn_gate', 'spawn gate'), ('soft_stop', 'soft stop')):
        for mode, counts in sorted(guards.get(hook, {}).items()):
            if hook == 'spawn_gate':
                text = ', '.join(f'{label} {counts.get(key, 0)}' for key, label in
                                 (('allow', 'allowed'), ('warn', 'warned'), ('ask', 'asked'), ('deny', 'denied')))
            else:
                text = f'warnings {counts.get("warn", 0)}, denied {counts.get("deny", 0)}'
            lines.append(f'  {name} ({mode}): {text}')
    return lines


def render(report: dict) -> str:
    period, coverage = report['period'], report['coverage']
    offset = datetime.fromtimestamp(parse_ts(period['start'])).astimezone().strftime('%z')
    sources = [f'{coverage["transcript_files"]} transcript file(s) read']
    if coverage['archive_days']:
        sources.append(f'{len(coverage["archive_days"])} day(s) from the archive: {", ".join(coverage["archive_days"])}')
    for key, name in (('unreadable_history_lines', 'history'), ('unreadable_archive_lines', 'archive')):
        if coverage[key]:
            sources.append(f'{coverage[key]} unreadable {name} line(s) skipped')
    head = [f'Usage report {period["label"]} (local time, UTC{offset[:3]}:{offset[3:]})', 'Sources: ' + '; '.join(sources)]
    sections = [head, _subscription_lines(report['subscription']), _total_lines(report['total'])]
    if report['clis']:
        sections.append(_cli_lines(report['clis']))
    sections += [_allocation_lines(report['allocation']), _calibration_lines(report['calibration'])]
    if report['guards']:
        sections.append(_guard_lines(report['guards']))
    return '\n\n'.join('\n'.join(section) for section in sections)


# --- The archive --------------------------------------------------------------------------------------------------

def write_archive(log_dir: str, new_days: dict[str, dict]) -> None:
    """Appends a line for each day of `new_days` that the file does not have yet (a day is never overwritten). Same
    care as the measurement history: folder 0700, file 0600, opened without following links, a cut-off last line
    gets a line break, one writer at a time (flock). Raises OSError."""
    if os.path.islink(log_dir):
        raise OSError(f'{log_dir} is a link')
    os.makedirs(os.path.dirname(os.path.abspath(log_dir)), mode=0o700, exist_ok=True)
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    descriptor = os.open(os.path.join(log_dir, ARCHIVE_FILE), os.O_RDWR | os.O_APPEND | os.O_CREAT | _usage.NO_FOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+b') as archive:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(archive, fcntl.LOCK_EX)
        content = archive.read()
        known = {day for day, _ in _archive_days(content.decode('utf-8', errors='replace'))}
        text = ''.join(json.dumps({'v': ARCHIVE_VERSION, 'day': day, 'stats': stats}, separators=(',', ':')) + '\n'
                       for day, stats in sorted(new_days.items()) if day not in known)
        if text:
            archive.write((b'\n' if content and not content.endswith(b'\n') else b'') + text.encode('utf-8'))


def _archive_days(content: str):
    for raw in content.splitlines():
        try:
            row = json.loads(raw)
            yield row['day'], row['stats']
        except (ValueError, TypeError, KeyError, RecursionError):
            continue


# --- Command line -------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='usage_report.py', allow_abbrev=False,
                                     description='What was used in a day, a week or a month, in total and per CLI.')
    parser.add_argument('--day', nargs='?', const='', metavar='YYYY-MM-DD', help='one local day (default: today)')
    parser.add_argument('--week', action='store_true', help='the subscription week that ends at the weekly reset time')
    parser.add_argument('--month', nargs='?', const='', metavar='YYYY-MM', help='one calendar month (default: this one)')
    parser.add_argument('--since', metavar='YYYY-MM-DD', help='first day of a range of days')
    parser.add_argument('--until', metavar='YYYY-MM-DD', help='last day of the range (default: today), needs --since')
    parser.add_argument('--json', action='store_true', help='print the figures as JSON')
    parser.add_argument('--archive', action='store_true',
                        help='write the finished days that are not in the archive yet, print nothing')
    parser.add_argument('--projects-dir', metavar='DIR', help=f'transcripts (default: {PROJECTS_DIR})')
    parser.add_argument('--config', metavar='PATH', help='config.json to take log_dir from (as for log_summary.py)')
    return parser


def _day_arg(parser, text: str, name: str) -> date:
    try:
        if not DAY_TEXT.fullmatch(text):
            raise ValueError(text)
        return date.fromisoformat(text)
    except ValueError:
        parser.error(f'{name} must be a day like 2026-01-14')


def _period(parser, args, today: date, history: list[Measurement], now: float) -> dict:
    """kind, label, start, end (Unix seconds) of the requested period; {'error': text} if --week has no reset time."""
    if args.week:
        resets = next((m.week_resets for m in reversed(history) if m.week_resets is not None), None)
        if resets is None:
            return {'error': 'No weekly reset time in the measurement history: use --since and --until instead.'}
        while resets <= now:
            resets += WEEK_S      # the weekly windows follow each other every seven days
        return {'kind': 'week', 'label': f'week to {clock(resets, True)}', 'start': resets - WEEK_S, 'end': resets}
    if args.month is not None:
        if args.month and not (MONTH_TEXT.fullmatch(args.month) and 1 <= int(args.month[5:]) <= 12):
            parser.error('--month must be a month like 2026-01')
        year, month = (int(args.month[:4]), int(args.month[5:])) if args.month else (today.year, today.month)
        first = date(year, month, 1)
        following = date(year + month // 12, month % 12 + 1, 1)
        return {'kind': 'month', 'label': f'{year:04d}-{month:02d}', 'start': day_start(first), 'end': day_start(following)}
    if args.since is not None:
        first = _day_arg(parser, args.since, '--since')
        last = _day_arg(parser, args.until, '--until') if args.until is not None else today
        if last < first:
            parser.error('--until must not be before --since')
        return {'kind': 'range', 'label': f'{first} to {last}', 'start': day_start(first), 'end': day_bounds(last)[1]}
    day = _day_arg(parser, args.day, '--day') if args.day else today
    return {'kind': 'day', 'label': day.isoformat(), 'start': day_bounds(day)[0], 'end': day_bounds(day)[1]}


def _cut(start: float, end: float, days: list[date]) -> list[tuple[float, float]]:
    """[start, end) without the given whole days."""
    pieces, cursor = [], start
    for day in sorted(days):
        first, last = day_bounds(day)
        if first > cursor:
            pieces.append((cursor, first))
        cursor = last
    if end > cursor:
        pieces.append((cursor, end))
    return pieces


def _whole_days(start: float, end: float) -> list[date]:
    days, day = [], local_day(start)
    while day_start(day) < end:
        if day_start(day) >= start and day_bounds(day)[1] <= end:
            days.append(day)
        day += timedelta(days=1)
    return days


def load_data(projects_dir: str, log_dir: str, history: list[Measurement], modified_since: float, start: float) -> Data:
    """Requests from the transcripts; for the time before the history began, the usage lines in the transcripts stand in
    for it."""
    data = Data()
    first_history = min((m.t for m in history), default=math.inf)
    requests, fresh = [], {}
    if os.path.isdir(projects_dir):
        requests, fresh, data.files = read_transcripts(projects_dir, modified_since, want_usage_lines=start < first_history)
    data.set_requests(requests)
    older = [Measurement(ts, five, week, None, None) for ts, (five, week) in fresh.items() if ts < first_history]
    data.set_measurements(history + older)
    data.guards = read_guard_log(log_dir)
    return data


def main(argv: list[str] | None = None, now: float | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    now = time.time() if now is None else now
    given = (('--day', args.day is not None), ('--week', args.week), ('--month', args.month is not None),
             ('--since', args.since is not None))
    chosen = [name for name, present in given if present]
    if len(chosen) > 1:
        parser.error(f'choose one of {", ".join(chosen)}')
    if args.until is not None and args.since is None:
        parser.error('--until needs --since')
    if args.archive and (chosen or args.until is not None or args.json):
        parser.error('--archive takes no period and no --json')
    config = os.path.expanduser(args.config) if args.config else None
    if config is not None and not os.path.isfile(config):
        parser.error(f'--config: not a file: {args.config}')
    log_dir = log_summary.resolve_log_dir(config)
    projects_dir = os.path.expanduser(args.projects_dir or PROJECTS_DIR)
    try:
        prices = load_prices(PRICES_FILE)
    except PriceFileError as error:
        print(f'Cannot read the prices: {error}', file=sys.stderr)
        return 1
    today = local_day(now)
    history, broken_history = read_history(log_dir)
    archive, broken_archive = read_archive(log_dir)
    if args.archive:
        period = {'kind': 'archive', 'start': 0.0, 'end': math.inf}
    else:
        period = _period(parser, args, today, history, now)
    if 'error' in period:
        print(period['error'])
        return 1
    start, end = period['start'], period['end']
    data = load_data(projects_dir, log_dir, history, start - MAX_GAP_S, start)
    if args.archive:
        return archive_finished_days(data, prices, archive, log_dir, projects_dir, today)

    live_days = {local_day(r.ts) for r in data.requests}
    archived = [d for d in _whole_days(start, end) if d not in live_days and d.isoformat() in archive]
    if not data.files and not archived and not os.path.isdir(projects_dir):
        print(f'No transcripts found at {projects_dir} - pass --projects-dir.')
        return 1
    stats = compute_stats(data, prices, _cut(start, end, archived))
    used = []
    for day in archived:
        try:
            candidate = copy.deepcopy(stats)
            merge_stats(candidate, archive[day.isoformat()])
        except (TypeError, ValueError, KeyError, AttributeError):
            broken_archive += 1
            continue
        stats = candidate
        used.append(day.isoformat())
    coverage = {'transcript_files': data.files, 'archive_days': used, 'unreadable_history_lines': broken_history,
                'unreadable_archive_lines': broken_archive}
    shown = {'kind': period['kind'], 'label': period['label'], 'start': iso(start), 'end': iso(end)}
    report = build_report(stats, shown, coverage)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


def archive_finished_days(data: Data, prices: list[dict], archive: dict, log_dir: str, projects_dir: str,
                          today: date) -> int:
    if not os.path.isdir(projects_dir):
        print(f'No transcripts found at {projects_dir} - pass --projects-dir.')
        return 1
    missing = sorted({d for d in (local_day(r.ts) for r in data.requests) if d < today and d.isoformat() not in archive})
    days = {day.isoformat(): compute_stats(data, prices, [day_bounds(day)]) for day in missing}
    try:
        write_archive(log_dir, days)
    except OSError as error:
        print(f'Cannot write the archive in {log_dir}: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
