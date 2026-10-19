#!/usr/bin/env python3
"""What was used in a day, a week or a month: in total and per CLI, measured from the Claude Code transcripts.

Reads only numbers: per request the token counts, the model, the time, the working folder and the ids that tell
requests apart, the agent type of a subagent, the usage line of this tool, and the measurement history and the log
of the hooks. It never reads or prints message text.

Usage: python3 usage_report.py [--day [YYYY-MM-DD] | --week | --month [YYYY-MM] | --year [YYYY]
                               | --since YYYY-MM-DD [--until YYYY-MM-DD]]
                               [--full | --project NAME | --session ID | --json] [--projects-dir DIR] [--config PATH]
       python3 usage_report.py --archive [--projects-dir DIR] [--config PATH]

The default is today. Days are local days; --week is the subscription week that ends at the weekly reset time
(from the measurement history, else from the usage file; not a calendar week); --since and --until are whole days,
both included; --year is the days of a calendar year (this year up to today). Without a switch the report is an overview of about 20 lines: the projects by cost, what is notable.
--full prints every figure, --project and --session (a prefix of the session id) print the blocks of --full for those
sessions and their sum. --json prints the same figures as a stable JSON object, including the overview (see
docs/report-format.md). --archive writes every finished day that is not in <log_dir>/usage-days.jsonl yet and prints
nothing: Claude Code deletes transcripts after about 30 days, the archive keeps the day figures (run it daily).
A project is the name of the folder a session ran in (the report knows no paths: folders of the same name
in different places are one project). --session also shows how the context of that session was made up after its first
answer (the file the desktop plugin writes to <log_dir>/context-breakdown/): names and token counts, never content.

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
import json
import math
import os
import re
import statistics
import sys
import time
from unittest import mock
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'hooks'))
sys.path.insert(0, HERE)
import _config  # noqa: E402
import _plat  # noqa: E402
import _spawn_check  # noqa: E402
import _sync_core  # noqa: E402
import _sync_git  # noqa: E402
import _transcripts  # noqa: E402
import _usage  # noqa: E402
import log_summary  # noqa: E402
import sync as sync_command  # noqa: E402
import sync_format  # noqa: E402

PRICES_FILE = os.path.join(HERE, 'prices.json')
PROJECTS_DIR = '~/.claude/projects'
CLAUDE_DIR = '~/.claude'
HISTORY_FILE = 'usage-history.jsonl'
ARCHIVE_FILE = 'usage-days.jsonl'
ARCHIVE_VERSION = 1
CONTEXT_DIR = 'context-breakdown'   # in the log folder: one <session id>.json per session, written by the desktop plugin
CONTEXT_VERSION = 1
MAX_CONTEXT_BYTES = 4_000_000       # a larger context file is not read
CONTEXT_TEXT_CHARS = 200            # of a name or path in a context file only this much is kept
MAX_LISTED = 10                     # tools, skills and agents with a row in the context block of --session

METRICS = ('five', 'week')
MAX_GAP_S = 3600            # a longer stretch between two measurements is too coarse to share
RESET_TOLERANCE_S = 60      # reset times closer than this belong to the same window (as in _usage.py)
COLD_START_TOKENS = 50_000  # a request that writes at least this much cache, after the first, is a cold start
MIN_SECTIONS = 50           # sections with a single CLI needed before a cost per point is shown
MIN_DAY_SECTIONS = 20       # sections a day needs before its cost per point is listed in the text
MAX_PROJECTS = 5            # projects with a row in the overview; the rest is one line
NAME_WIDTH = 30             # characters of a project name in the overview table; a longer one is shortened in the middle
CACHE_LOW = 0.9             # a project whose cache hit rate is below this is notable
MAX_REASONS = 3             # reasons named in a notable line; the rest is "other"
REASON_LENGTH = 60          # characters of a reason kept
MAX_FILE_CHARS = 1_000_000  # of a CLAUDE.md or rule file only this much is read (and counted)
HEAD_CHARS = 65_536         # of an agent or skill file the front part is read for its description
MEMORY_LINES = 200          # of MEMORY.md Claude Code loads the first 200 lines or 25 KB, whichever comes first
MEMORY_CHARS = 25 * 1024
NOT_ESTIMABLE = ('system prompt', 'built-in tools', 'MCP instructions', 'skills of plugins and other apps',
                 'output of hooks')      # parts of the start that only Claude Code knows
SCHEMA_VERSION = 1          # of the JSON object; changes when a field is renamed or removed
WEEK_S = 7 * 86400
RECOVERY_S = 900           # a stale number is followed by the real one within this time
FIVE_HOURS_S = 5 * 3600     # a five hour window cannot span more than this
MAX_TIME = 4_102_444_800    # Unix seconds of 2100-01-01: a later reset time is not a measurement
MIN_YEAR, MAX_YEAR = 2000, 2999     # the days a period may name
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

# Counts of the optional details, kept apart from the sums so that "not reported" is not a zero: how many requests report
# the detail (`_n`), its sum, and for thinking the output tokens of those requests (the denominator of its share).
DETAIL_KEYS = ('thinking_n', 'thinking', 'thinking_out', 'search_n', 'search', 'fetch_n', 'fetch')

MODEL_NAME = re.compile(r'[A-Za-z0-9_.:/-]{1,64}')
WORD = re.compile(r'[a-z_]{1,20}')
PERCENT = re.compile(r'\d+(?:\.\d+)? ?%')
REASON_CHARS = re.compile(r'[^\w .,…()/+%-]')
NUMBER = re.compile(r'(?<![\w.-])\d+(?:\.\d+)?(?![\w.-])')      # a number of its own, not the 5 of `5h` or `7-day`
QUOTED = re.compile('"[^"]*"|\'[^\']*\'|„[^“”]*[“”]|`[^`]*`')   # names and values a gate quotes in its reason
REASONED = ('warn', 'ask', 'deny')      # the decisions that have a reason worth counting
# The question of the spawn gate's ask dialog (spawn_gate.QUESTIONS): `WEEK 92 % · start x?`, `WOCHE 92 % · x starten?`
WEEK_QUESTION = re.compile(r'(?:WEEK|WOCHE) \d+(?:\.\d+)? % · ')
# The kind version 1.4.0 archived for that question before reason_kind knew it: `wOCHE N % ? x starten?`
ARCHIVED_QUESTION = re.compile(r'w(?:EEK|OCHE) N % \? ')
WEEK_KIND = '7-day window at N %'
DAY_TEXT = re.compile(r'\d{4}-\d{2}-\d{2}')
CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f]')
MONTH_TEXT = re.compile(r'\d{4}-\d{2}')
YEAR_TEXT = re.compile(r'[0-9]{4}')


# --- Times ----------------------------------------------------------------------------------------------------------

def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def local_day(ts: float) -> date:
    return datetime.fromtimestamp(ts).date()


def day_start(day: date) -> float:
    return datetime(day.year, day.month, day.day).timestamp()


def day_bounds(day: date) -> tuple[float, float]:
    return day_start(day), day_start(day + timedelta(days=1))


def day_text(ts: float) -> str:
    moment = datetime.fromtimestamp(ts)
    return f'{WEEKDAYS[moment.weekday()]} {moment.day} {MONTHS[moment.month - 1]}'


def clock(ts: float, with_date: bool = False) -> str:
    return (day_text(ts) + ' ' if with_date else '') + f'{datetime.fromtimestamp(ts):%H:%M}'


# --- Prices ---------------------------------------------------------------------------------------------------------

class PriceFileError(Exception):
    pass


def _check_rates(rates, where: str) -> None:
    if not isinstance(rates, dict) or not all(_transcripts.is_number(rates.get(k)) and rates[k] >= 0
                                              for k in _transcripts.KINDS):
        raise PriceFileError(f'{where}: needs a number for each of {", ".join(_transcripts.KINDS)}')


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
            if not _transcripts.is_number(tier.get('above_tokens')):
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
    return sum(count * rates[kind] for count, kind in zip(tokens, _transcripts.KINDS)) / 1e6


# --- Reading the sources ------------------------------------------------------------------------------------------------

class Measurement(NamedTuple):
    t: float
    five: float | None
    week: float | None
    five_resets: float | None
    week_resets: float | None


def _comes_back(measurements: list[Measurement], index: int, metric: str, reference: float) -> bool:
    """Whether a measurement within RECOVERY_S after `measurements[index]` has the value again, at least `reference`."""
    for later in measurements[index + 1:]:
        if later.t - measurements[index].t > RECOVERY_S:
            return False
        value = getattr(later, metric)
        if value is not None and value >= reference:
            return True
    return False


def drop_noise(measurements: list[Measurement], anchor: float | None = None) -> list[Measurement]:
    """Measurements without stale numbers. Sessions that sat idle keep showing old numbers (before the usage file merged
    them), which look like short falls. Without reset times a value that fell, but not to half or less, is such a stale
    number if a value at least as high as before comes back within RECOVERY_S and the last good value is not more than
    MAX_GAP_S old; everything else is a new window. The weekly value is stale at any fall if the measurements are in
    one weekly window that `anchor` (a known weekly reset time) tells: usage does not fall inside a window, so that
    measurement comes from an idle session as a whole and is left out. Otherwise the stale value is replaced by the last
    good one (the other value of the measurement stays, 5h and week are judged apart); a measurement with only stale
    values is left out. Where both
    measurements carry reset times, those decide and nothing is changed."""
    kept: list[Measurement] = []
    last: dict[str, Measurement] = {}
    for index, m in enumerate(measurements):
        stale, certain = set(), False
        for metric in METRICS:
            value, earlier = getattr(m, metric), last.get(metric)
            if value is None or earlier is None:
                continue
            reference = getattr(earlier, metric)
            both_known = getattr(m, metric + '_resets') is not None and getattr(earlier, metric + '_resets') is not None
            if both_known or value >= reference:
                continue
            if metric == 'week' and anchor is not None and _week_reset(m, anchor) == _week_reset(earlier, anchor):
                certain = True
            elif (reference / 2 < value and m.t - earlier.t <= MAX_GAP_S
                  and _comes_back(measurements, index, metric, reference)):
                stale.add(metric)
        present = {metric for metric in METRICS if getattr(m, metric) is not None}
        if certain or (stale and stale == present):
            continue
        for metric in stale:
            earlier = last[metric]
            m = m._replace(**{metric: getattr(earlier, metric), metric + '_resets': getattr(earlier, metric + '_resets')})
        kept.append(m)
        last.update({metric: m for metric in present - stale})
    return kept


class Data:
    """Everything read: the requests (sorted by time), the measurements, the log of the gates."""

    def __init__(self):
        self.requests: list[_transcripts.Request] = []
        self.times: list[float] = []
        self.measurements: list[Measurement] = []
        self.measurement_times: list[float] = []
        self.guards: list[tuple] = []            # (time, hook, mode, decision, kind of reason or None)
        self.files = 0
        self.unreadable_requests = 0             # request lines that were left out because they cannot be requests
        self.week_anchor: float | None = None     # a known weekly reset time: the weekly windows follow it every 7 days
        self.session_of: dict[str, str] = {}     # first 8 characters of a session id -> the session
        self.label: dict[str, str] = {}
        self.start: dict[str, float] = {}
        self.first_main: dict[str, int] = {}
        self.start_indices: list[int] = []        # the first request of every transcript, in time order
        self.start_times: list[float] = []
        self.price_cache: dict[str, dict | None] = {}
        self.machine: dict[str, str] = {}         # with the sync: CLI id -> the machine it ran on
        self.only: str | None = None              # --machine: only the requests and starts of this machine count

    def counted(self, ident: str) -> bool:
        """Whether the CLI `ident` (the first 8 characters of a session id) counts: always without --machine."""
        return self.only is None or self.machine.get(ident) == self.only

    def set_requests(self, requests: list[_transcripts.Request]) -> None:
        self.requests = sorted(requests, key=lambda r: r.ts)
        self.times = [r.ts for r in self.requests]
        for index, request in enumerate(self.requests):
            self.start.setdefault(request.session, request.ts)
            self.session_of.setdefault(request.session[:8], request.session)
            if request.session not in self.label and request.cwd:
                folder = re.sub(r'[^\w .+@=-]', '?', _transcripts.folder_of(request.cwd))[:64]
                if folder:
                    self.label[request.session] = folder
            if not request.sub:
                self.first_main.setdefault(request.session, index)
        self.start_indices = [index for index in _transcripts.start_indices(self.requests)
                              if self.requests[index].source != NO_START]
        self.start_times = [self.times[index] for index in self.start_indices]

    def set_measurements(self, measurements: list[Measurement]) -> None:
        self.measurements = drop_noise(sorted(measurements, key=lambda m: m.t), self.week_anchor)
        self.measurement_times = [m.t for m in self.measurements]


def _optional_number(row: dict, key: str, maximum: float = _transcripts.MAX_COUNT):
    value = row.get(key)
    if value is not None and not (_transcripts.is_number(value) and abs(value) <= maximum):
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
                ts = _transcripts.parse_ts(row['t'])
                if ts is None:
                    raise ValueError('t')
                rows.append(Measurement(ts, _optional_number(row, 'five'), _optional_number(row, 'week'),
                                        _optional_number(row, 'five_resets', MAX_TIME),
                                        _optional_number(row, 'week_resets', MAX_TIME)))
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                broken += 1
    return rows, broken


def read_guard_log(log_dir: str) -> list[tuple]:
    entries = []
    for name in ('log.jsonl.1', 'log.jsonl'):
        for raw in read_lines(os.path.join(log_dir, name)):
            try:
                row = json.loads(raw)
                ts = _transcripts.parse_ts(row['time'])
                if ts is not None and row['hook'] in ('spawn_gate', 'soft_stop'):
                    decision = _word(row.get('decision'), 'other')
                    entries.append((ts, row['hook'], _word(row.get('mode'), 'unknown'), decision,
                                    reason_kind(row.get('reason')) if decision in REASONED else None))
            except (ValueError, TypeError, KeyError, RecursionError):
                continue
    return entries


def _word(value, fallback: str) -> str:
    return value if isinstance(value, str) and WORD.fullmatch(value) else fallback


def reason_kind(reason) -> str:
    """The kind of a reason from the log: the text before the first colon, without percentages and numbers (`5h window
    at 78 %` and `... at 80 %` are one kind, so are `3 agents` and `4 agents`), quoted names replaced by `…`, made
    harmless, at most REASON_LENGTH characters, starting in lower case. The question of the ask dialog has no colon and
    names the agent: it is the kind of the other reasons of the weekly threshold."""
    if isinstance(reason, str) and WEEK_QUESTION.match(reason):
        return WEEK_KIND
    prefix = reason.split(':', 1)[0] if isinstance(reason, str) else ''
    prefix = NUMBER.sub('N', PERCENT.sub('N %', QUOTED.sub('…', prefix)))
    prefix = REASON_CHARS.sub('?', prefix.strip())[:REASON_LENGTH]
    return prefix[:1].lower() + prefix[1:] or 'no reason'


def parse_archive_line(raw: str) -> tuple[str, dict]:
    """(day, figures) of a line of the day archive; raises ValueError (or TypeError, KeyError) for a line the archive does
    not accept: another version, a wrong shape, figures that do not fit the format."""
    row = json.loads(raw)
    day, stats = row['day'], row['stats']
    # a day that is not text makes fullmatch raise, which the callers treat like any other bad line
    if row['v'] != ARCHIVE_VERSION or not isinstance(stats, dict) or not DAY_TEXT.fullmatch(day):
        raise ValueError('shape')
    date.fromisoformat(day)
    try:
        merge_stats(new_stats(), stats)       # figures in another format (or damaged) do not merge: not an archive line
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        raise ValueError('figures') from error
    _merge_archived_questions(stats)
    return day, stats


def _merge_archived_questions(stats: dict) -> None:
    """Counts the ask questions a day of version 1.4.0 archived as kinds of their own under WEEK_KIND."""
    for modes in stats.get('guard_reasons', {}).values():
        for decisions in modes.values():
            for decision, kinds in decisions.items():
                merged: dict = {}
                for kind, count in kinds.items():
                    key = WEEK_KIND if ARCHIVED_QUESTION.match(kind) else kind
                    merged[key] = merged.get(key, 0) + count
                decisions[decision] = merged


def read_archive(log_dir: str) -> tuple[dict[str, dict], int]:
    """({day: stats}, unreadable lines). The first line of a day counts."""
    days, broken = {}, 0
    for raw in read_lines(os.path.join(log_dir, ARCHIVE_FILE)):
        try:
            day, stats = parse_archive_line(raw)
        except (ValueError, TypeError, KeyError, RecursionError):
            broken += 1
            continue
        days.setdefault(day, stats)
    return days, broken


# --- The context split of a session --------------------------------------------------------------------------------
# The desktop plugin writes <log_dir>/context-breakdown/<session id>.json after the first answer of a session: how the
# context was made up (the split /context shows). Only names and numbers are taken over; a field the format does not
# name is dropped, a file that does not fit is counted and left out.

CATEGORY_FIELDS = (('name', 'text'), ('tokens', 'count'), ('kind', 'label'))
MEMORY_FIELDS = (('path', 'text'), ('type', 'label'), ('tokens', 'count'))
TOOL_FIELDS = (('server', 'text'), ('name', 'text'), ('tokens', 'count'), ('loaded', 'flag'))
SKILL_FIELDS = (('name', 'text'), ('tokens', 'count'))
AGENT_FIELDS = (('type', 'text'), ('source', 'label'), ('tokens', 'count'))


def _context_value(value, kind: str):
    """A text (made harmless and cut), a count, or for a label / flag also None; ValueError for anything else."""
    if value is None and kind in ('label', 'flag'):
        return None
    if kind == 'count':
        if not _transcripts.is_number(value) or not 0 <= value <= _transcripts.MAX_COUNT:
            raise ValueError(kind)
        return int(value)
    if kind == 'flag':
        if not isinstance(value, bool):
            raise ValueError(kind)
        return value
    if not isinstance(value, str):
        raise ValueError(kind)
    return CONTROL_CHARS.sub('?', value)[:CONTEXT_TEXT_CHARS]


def _context_row(row, fields: tuple) -> dict:
    if not isinstance(row, dict):
        raise ValueError('row')
    return {name: _context_value(row.get(name), kind) for name, kind in fields}


def _context_rows(rows, fields: tuple) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError('rows')
    return [_context_row(row, fields) for row in rows]


def parse_context(data, session: str) -> dict:
    """The context of `session` from its file: only the fields of the format, as in the file but without `session_id`.
    Raises ValueError for a file of another version or session or one that lacks a required field."""
    if (not isinstance(data, dict) or type(data.get('schema_version')) is not int
            or data['schema_version'] != CONTEXT_VERSION or data.get('session_id') != session):
        raise ValueError('shape')
    skills = data.get('skills')      # null: the engine gave no split of the skills
    if 'skills' not in data or not (skills is None or isinstance(skills, dict)):
        raise ValueError('skills')
    return {'schema_version': CONTEXT_VERSION, 'at': _context_value(data.get('at'), 'text'),
            'cwd': _context_value(data.get('cwd'), 'label'), 'model': _context_value(data.get('model'), 'label'),
            'detail': _context_value(data.get('detail'), 'label'),
            'total_tokens': _context_value(data.get('total_tokens'), 'count'),
            'max_tokens': _context_value(data.get('max_tokens'), 'count'),
            'categories': _context_rows(data.get('categories'), CATEGORY_FIELDS),
            'memory_files': _context_rows(data.get('memory_files'), MEMORY_FIELDS),
            'mcp_tools': _context_rows(data.get('mcp_tools'), TOOL_FIELDS),
            'skills': None if skills is None else {
                'tokens': _context_value(skills.get('tokens'), 'count'),
                'included': _context_value(skills.get('included'), 'count'),
                'total': _context_value(skills.get('total'), 'count'),
                'items': _context_rows(skills.get('items'), SKILL_FIELDS)},
            'agents': _context_rows(data.get('agents'), AGENT_FIELDS)}


def read_contexts(log_dir: str, wanted: set[str]) -> tuple[dict[str, dict], int]:
    """({CLI id: context}, unreadable files). Only the files of the CLIs in `wanted` (the first 8 characters of a session
    id, as `clis[].id`) are read; of several files for one CLI the first by name counts. A file that cannot be read or
    does not fit the format is counted, never an error."""
    folder = os.path.join(log_dir, CONTEXT_DIR)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return {}, 0
    found, broken = {}, 0
    for name in names:
        session = name[:-len('.json')]
        if not name.endswith('.json') or session[:8] not in wanted:
            continue
        try:
            with open(os.path.join(folder, name), 'rb') as f:
                raw = f.read(MAX_CONTEXT_BYTES + 1)
            if len(raw) > MAX_CONTEXT_BYTES:
                raise ValueError('size')
            context = parse_context(json.loads(raw.decode('utf-8')), session)
        except (OSError, ValueError, RecursionError):
            broken += 1
            continue
        found.setdefault(session[:8], context)
    return found, broken


# --- Figures of a period, mergeable ---------------------------------------------------------------------------------------
# A figure set is a dict of numbers (nested dicts of counts), so that the sets of several days, read from the archive
# or computed, add up to the set of a longer period.

def new_sums() -> dict:
    return {'n': 0, 'c2': 0.0, 'cd': 0.0}      # sections, sum of cost^2, sum of cost x rise: slope through zero


def new_cli(label: str, start: float) -> dict:
    return {'label': label, 'start': start, 'requests': 0, 'cost': 0.0, 'tokens': dict.fromkeys(_transcripts.KINDS, 0),
            'models': {}, 'subs': {}, 'cold_starts': 0, 'five_pts': 0.0, 'week_pts': 0.0, 'five_cov': 0.0, 'week_cov': 0.0}


def new_stats() -> dict:
    return {'requests': 0, 'cost': 0.0, 'cold_starts': 0, 'extra': {}, 'tokens': dict.fromkeys(_transcripts.KINDS, 0),
            'main': {'requests': 0, 'cost': 0.0},
            'sub': {'requests': 0, 'cost': 0.0}, 'agent_types': {}, 'models': {}, 'unpriced': {}, 'clis': {},
            'points': {'main': dict.fromkeys(METRICS, 0.0), 'types': {}},
            'abo': {'weeks': [], 'five_windows': []},
            'alloc': {m: {'counted': 0.0, 'attributed': 0.0, 'rest': 0.0, 'skipped_sections': 0, 'skipped_points': 0.0}
                      for m in METRICS},
            'cal': {m: {'all': new_sums(), 'models': {}, 'days': {}} for m in METRICS},
            'guards': {}, 'guard_reasons': {}, 'starts': []}


def _add_counts(target: dict, extra) -> None:
    """Adds the numbers of a nested dict. Anything that is not a number or a dict raises."""
    if not isinstance(extra, dict):
        raise TypeError('not a dict')
    for key, value in extra.items():
        if isinstance(value, dict):
            _add_counts(target.setdefault(key, {}), value)
        elif _transcripts.is_number(value):
            target[key] = target.get(key, 0) + value
        else:
            raise TypeError(key)


WINDOW_KEYS = ('resets', 'first', 'peak', 'peak_at', 'last', 'first_value', 'last_value')


def _same_window(earlier: dict, later: dict) -> bool:
    """Whether two five hour windows (the later one not starting earlier) are one: the same reset time, or, if one has
    none, together no longer than five hours and no fall of the value from the one to the other."""
    if earlier['resets'] is not None and later['resets'] is not None:
        return abs(later['resets'] - earlier['resets']) < RESET_TOLERANCE_S
    return later['last'] - earlier['first'] < FIVE_HOURS_S and later['first_value'] >= earlier['last_value']


def _merge_windows(target: list, extra: list) -> None:
    windows = []
    for window in extra:
        numbers = all(_transcripts.is_number(window[k]) for k in WINDOW_KEYS[1:])
        if not numbers or not (window['resets'] is None or _transcripts.is_number(window['resets'])):
            raise TypeError('window')
        windows.append({k: window[k] for k in WINDOW_KEYS})
    merged: list[dict] = []
    for window in sorted(target + windows, key=lambda w: (w['first'], w['last'])):
        if merged and _same_window(merged[-1], window):
            known = merged[-1]
            if (window['peak'], -window['peak_at']) > (known['peak'], -known['peak_at']):
                known['peak'], known['peak_at'] = window['peak'], window['peak_at']
            if window['last'] >= known['last']:
                known['last'], known['last_value'] = window['last'], window['last_value']
            known['resets'] = known['resets'] if known['resets'] is not None else window['resets']
        else:
            merged.append(window)
    target[:] = merged


def _week_point(point) -> dict:
    if not (_transcripts.is_number(point['t']) and _transcripts.is_number(point['value'])):
        raise TypeError('week point')
    return {'t': point['t'], 'value': point['value']}


def _same_segment(first: dict, second: dict) -> bool:
    """Whether two week segments (the second not earlier) are one weekly window: the reset times, if both are known,
    agree, and the second overlaps the first in time or the value did not fall between them. Overlapping segments come
    from a live part that spans an archived day; two windows never overlap."""
    if None not in (first['resets'], second['resets']) and abs(second['resets'] - first['resets']) >= RESET_TOLERANCE_S:
        return False
    return second['start']['t'] <= first['end']['t'] or second['start']['value'] >= first['end']['value']


def _merge_weeks(target: list, extra: list) -> None:
    segments = []
    for segment in extra:
        resets = segment['resets']
        if not (resets is None or _transcripts.is_number(resets)):
            raise TypeError('resets')
        segments.append({'start': _week_point(segment['start']), 'end': _week_point(segment['end']), 'resets': resets})
    merged: list[dict] = []
    for segment in sorted(target + segments, key=lambda seg: (seg['start']['t'], seg['end']['t'])):
        if merged and _same_segment(merged[-1], segment):
            last = merged[-1]
            if segment['end']['t'] >= last['end']['t']:
                last['end'] = segment['end']
            last['resets'] = last['resets'] if last['resets'] is not None else segment['resets']
        else:
            merged.append(segment)
    target[:] = merged


def _merge_abo(target: dict, extra) -> None:
    if not isinstance(extra, dict):
        raise TypeError('abo')
    _merge_weeks(target['weeks'], extra.get('weeks', []))
    _merge_windows(target['five_windows'], extra.get('five_windows', []))


START_COUNTS = ('input', 'cache_write', 'cache_read')
START_TEXT = re.compile(r'[\w .:/?-]{1,64}')       # a model name of the price file, a model id or `?`


def _merge_starts(target: list, extra) -> None:
    """Adds the starts of `extra`, which must be a list of well-formed starts: a start of another shape is not read as
    one (the archive line it comes from is not accepted)."""
    if not isinstance(extra, list):
        raise TypeError('starts')
    for start in extra:
        kind, model, session = start['type'], start['model'], start['session']
        counts = [start[key] for key in START_COUNTS]
        if not (_transcripts.is_number(start['t']) and 0 < start['t'] < MAX_TIME and isinstance(session, str)
                and len(session) <= 8
                and (kind is None or (isinstance(kind, str) and _transcripts.AGENT_TYPE.fullmatch(kind)))
                and isinstance(model, str) and START_TEXT.fullmatch(model)
                and all(_transcripts.is_number(count) and 0 <= count <= _transcripts.MAX_COUNT for count in counts)):
            raise ValueError('start')
        target.append({'t': start['t'], 'session': session, 'type': kind, 'model': model,
                       **dict(zip(START_COUNTS, counts))})


def merge_stats(target: dict, extra) -> None:
    """Adds the figures `extra` (computed or from the archive) to `target`. Raises if `extra` is malformed; the
    caller merges into a copy then."""
    target['requests'] += extra.get('requests', 0)
    target['cost'] += extra.get('cost', 0)
    target['cold_starts'] += extra.get('cold_starts', 0)
    for key in ('extra', 'tokens', 'main', 'sub', 'agent_types', 'models', 'unpriced', 'alloc', 'cal', 'guards',
                'guard_reasons', 'points'):
        _add_counts(target[key], extra.get(key, {}))      # an archived day of an older version lacks reasons and points
    _merge_starts(target['starts'], extra.get('starts', []))     # an archived day of an earlier version has none
    for ident, cli in extra.get('clis', {}).items():
        _merge_cli(target['clis'], ident, cli)
    _merge_abo(target['abo'], extra.get('abo', {}))


def _merge_cli(clis: dict, ident: str, cli: dict) -> None:
    """Adds the figures of one CLI to the CLI `ident` of `clis` (made if it is not there)."""
    if not (isinstance(cli['label'], str) and _transcripts.is_number(cli['start'])):
        raise TypeError(ident)
    mine = clis.setdefault(ident, new_cli(cli['label'], cli['start']))
    mine['start'] = min(mine['start'], cli['start'])
    for key in ('requests', 'cost', 'cold_starts', 'five_pts', 'week_pts', 'five_cov', 'week_cov'):
        mine[key] += cli.get(key, 0)
    for key in ('tokens', 'models', 'subs'):
        _add_counts(mine[key], cli.get(key, {}))
    _add_counts(mine, {key: cli[key] for key in DETAIL_KEYS if key in cli})


# --- Computing the figures of intervals ---------------------------------------------------------------------------------

def _price_of(data: Data, prices: list[dict], model: str) -> dict | None:
    if model not in data.price_cache:
        data.price_cache[model] = find_price(prices, model)
    return data.price_cache[model]


def request_details(request: _transcripts.Request) -> dict:
    """The details a request reports, as counts for DETAIL_KEYS; a detail it does not report adds nothing."""
    counts = {}
    if request.thinking is not None:
        counts.update(thinking_n=1, thinking=request.thinking, thinking_out=request.tokens[4])
    if request.searches is not None:
        counts.update(search_n=1, search=request.searches)
    if request.fetches is not None:
        counts.update(fetch_n=1, fetch=request.fetches)
    return counts


def _model_name(entry: dict | None, model: str) -> str:
    """The name the report shows for a model: the one of its price entry, else its id, else `?`."""
    return entry['name'] if entry else (model if MODEL_NAME.fullmatch(model) else '?')


def _count_request(stats: dict, data: Data, prices: list[dict], index: int) -> None:
    request = data.requests[index]
    entry = _price_of(data, prices, request.model)
    cost = request_cost(entry, request.tokens) if entry else 0.0
    name = _model_name(entry, request.model)
    details = request_details(request)
    tokens = dict(zip(_transcripts.KINDS, request.tokens))
    stats['requests'] += 1
    stats['cost'] += cost
    _add_counts(stats['extra'], details)
    side = stats['sub' if request.sub else 'main']
    side['requests'] += 1
    side['cost'] += cost
    for kind, count in zip(_transcripts.KINDS, request.tokens):
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
    for kind, count in zip(_transcripts.KINDS, request.tokens):
        cli['tokens'][kind] += count
    cli['models'][name] = cli['models'].get(name, 0.0) + cost
    if request.sub:
        _add_counts(cli['subs'], {request.agent_type: {'requests': 1, 'cost': cost}})
    elif index != data.first_main[request.session] and request.tokens[1] + request.tokens[2] >= COLD_START_TOKENS:
        cli['cold_starts'] += 1
        stats['cold_starts'] += 1


def _starts_between(data: Data, prices: list[dict], start: float, end: float) -> list[dict]:
    """The starts inside [start, end): the first request of every transcript, with the tokens it carries."""
    lo, hi = bisect.bisect_left(data.start_times, start), bisect.bisect_left(data.start_times, end)
    starts = []
    for index in data.start_indices[lo:hi]:
        request = data.requests[index]
        if not data.counted(request.session[:8]):
            continue
        starts.append({'t': request.ts, 'session': request.session[:8], 'type': request.agent_type,
                       'model': _model_name(_price_of(data, prices, request.model), request.model),
                       'input': request.tokens[0], 'cache_write': request.tokens[1] + request.tokens[2],
                       'cache_read': request.tokens[3]})
    return starts


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


def _section_costs(data: Data, prices: list[dict], first: float, second: float) -> tuple[dict, dict, set, bool]:
    """(cost by CLI, cost by agent (None = the main sessions, else the type of the subagents; with --machine only of
    the CLIs that count), model names, whether a request has no price) of the requests after `first` up to `second`."""
    costs: dict[str, float] = {}
    agents: dict[str | None, float] = {}
    models: set = set()
    unpriced = False
    lo, hi = bisect.bisect_right(data.times, first), bisect.bisect_right(data.times, second)
    for request in data.requests[lo:hi]:
        entry = _price_of(data, prices, request.model)
        ident = request.session[:8]
        cost = request_cost(entry, request.tokens) if entry else 0.0
        costs[ident] = costs.get(ident, 0.0) + cost
        if data.counted(ident):
            agents[request.agent_type] = agents.get(request.agent_type, 0.0) + cost
        if entry:
            models.add(entry['name'])
        else:
            unpriced = True
    return costs, agents, models, unpriced


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
            costs, agents, models, unpriced = section
            total = sum(costs.values())
            if total <= 0:
                alloc['rest'] += rise       # no request in between, or none with a price: nobody to share it with
                continue
            alloc['attributed'] += rise
            for ident, cost in costs.items():
                if not data.counted(ident):
                    continue        # --machine: the share of a CLI of another machine
                cli = _cli(stats, data, data.session_of[ident])
                cli[metric + '_pts'] += rise * cost / total
                cli[metric + '_cov'] += cost         # the cost that lay in a counted section: its points are measured
            for kind, cost in agents.items():      # the same shares, by agent: the main sessions or a subagent type
                points = stats['points']['main'] if kind is None else stats['points']['types'].setdefault(
                    kind, dict.fromkeys(METRICS, 0.0))
                points[metric] += rise * cost / total
            if len(costs) == 1 and not unpriced:
                cal = stats['cal'][metric]
                sums = [cal['all'], cal['days'].setdefault(local_day(second.t).isoformat(), new_sums())]
                if len(models) == 1:
                    sums.append(cal['models'].setdefault(next(iter(models)), new_sums()))
                for entry in sums:
                    entry['n'] += 1
                    entry['c2'] += total * total
                    entry['cd'] += total * rise


def _week_reset(m: Measurement, anchor: float | None) -> float | None:
    """The end of the weekly window a measurement belongs to: the recorded reset time, else the next one in the
    weekly rhythm of `anchor` (a known weekly reset time), else unknown."""
    if m.week_resets is not None or anchor is None:
        return m.week_resets
    return anchor + (math.floor((m.t - anchor) / WEEK_S) + 1) * WEEK_S


def _same_week(earlier: Measurement, later: Measurement, anchor: float | None) -> bool:
    """Both measurements are in one weekly window: the value did not fall and the reset times (if known) agree."""
    if later.week < earlier.week:
        return False
    resets = _week_reset(earlier, anchor), _week_reset(later, anchor)
    return None in resets or abs(resets[1] - resets[0]) < RESET_TOLERANCE_S


def _week_segments(measurements: list[Measurement], lo: int, weekly: list[Measurement], anchor: float | None) -> list[dict]:
    """The weekly windows in the period, each with its first and last value; `weekly` are the measurements inside the
    period that have a weekly value, `lo` the index of the first one inside in `measurements`."""
    before = next((m for m in reversed(measurements[:lo]) if m.week is not None), None)
    segments: list[dict] = []
    previous = None
    for m in weekly:
        point = {'t': m.t, 'value': m.week}
        if previous is not None and _same_week(previous, m, anchor):
            segments[-1]['end'] = point
        else:
            joined = previous is None and before is not None and _same_week(before, m, anchor)
            start = {'t': before.t, 'value': before.week} if joined else point
            segments.append({'start': start, 'end': point, 'resets': _week_reset(m, anchor)})
        previous = m
    return segments


def _subscription(data: Data, start: float, end: float) -> dict:
    times, measurements = data.measurement_times, data.measurements
    lo, hi = bisect.bisect_left(times, start), bisect.bisect_left(times, end)
    inside = measurements[lo:hi]
    weekly = [m for m in inside if m.week is not None]
    piece = {'weeks': _week_segments(measurements, lo, weekly, data.week_anchor), 'five_windows': []}
    previous = None
    for m in inside:
        if m.five is None:
            continue
        resets = (previous.five_resets, m.five_resets) if previous else (None, None)
        window = piece['five_windows'][-1] if piece['five_windows'] else None
        renewed = previous is None or m.five < previous.five or (
            None not in resets and abs(resets[1] - resets[0]) >= RESET_TOLERANCE_S) or (
            None in resets and m.t - window['first'] >= FIVE_HOURS_S)      # without reset times: a window lasts five hours
        if renewed:
            piece['five_windows'].append({'resets': m.five_resets, 'first': m.t, 'peak': m.five, 'peak_at': m.t,
                                          'last': m.t, 'first_value': m.five, 'last_value': m.five})
        else:
            window = piece['five_windows'][-1]
            if m.five > window['peak']:
                window['peak'], window['peak_at'] = m.five, m.t
            window['last'], window['last_value'] = m.t, m.five
            if window['resets'] is None:
                window['resets'] = m.five_resets
        previous = m
    return piece


def compute_stats(data: Data, prices: list[dict], intervals: list[tuple[float, float]]) -> dict:
    """The figures of the requests, measurements and gate decisions inside the (half open) intervals."""
    stats = new_stats()
    for start, end in intervals:
        for index in range(bisect.bisect_left(data.times, start), bisect.bisect_left(data.times, end)):
            if data.counted(data.requests[index].session[:8]):
                _count_request(stats, data, prices, index)
        _count_sections(stats, data, prices, start, end)
        stats['starts'] += _starts_between(data, prices, start, end)
        _merge_abo(stats['abo'], _subscription(data, start, end))
        for ts, hook, mode, decision, kind in data.guards:
            if start <= ts < end:
                _add_counts(stats['guards'], {hook: {mode: {decision: 1}}})
                if kind is not None:
                    _add_counts(stats['guard_reasons'], {hook: {mode: {decision: {kind: 1}}}})
    return stats


# --- The report ---------------------------------------------------------------------------------------------------

def _usd_per_point(sums: dict) -> float | None:
    """Slope through zero of the rise on the cost, inverted: sum(cost^2) / sum(cost x rise). The rise is the dependent
    value because the rounding to whole points is in the rise, not in the cost."""
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
    everything = sum(tokens[kind] for kind in _transcripts.KINDS[:4])
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


def _types_report(types: dict, points: dict) -> dict:
    """The subagent types with requests, cost weight and the estimated points (week and 5h) that fall to them. A type may
    have points without a request in the period: its request lay in a stretch that began before the period."""
    table = {name: {'requests': 0, 'cost': 0.0, **types.get(name, {})} for name in types.keys() | points.keys()}
    return {name: {'requests': v['requests'], 'cost_usd': v['cost'], 'week_points': points.get(name, {}).get('week', 0.0),
                   'five_points': points.get(name, {}).get('five', 0.0)} for name, v in _by_cost(table)}


def _point(entry: dict) -> dict:
    return {'at': iso(entry['t']), 'value': entry['value']}


def _cli_report(ident: str, cli: dict) -> dict:
    return {'id': ident, 'label': cli['label'], 'start': iso(cli['start']), 'requests': cli['requests'],
            'cost_usd': cli['cost'], 'tokens': cli['tokens'],
            'models': dict(sorted(cli['models'].items(), key=lambda item: -item[1])),
            'subagents': _requests_and_cost(cli['subs']),
            'cache_hit_rate': _hit_rate(cli['tokens']), 'cold_starts': cli['cold_starts'],
            'estimated_share': {'five': cli['five_pts'], 'week': cli['week_pts']},
            'measured_share': {metric: _measured(cli.get(metric + '_cov', 0.0), cli['cost']) for metric in METRICS},
            **_details_report(cli)}


def _measured(covered: float, cost: float) -> float | None:
    """The part of a cost that lay in a counted section (0 to 1): 0 means its points are not measured, not zero."""
    return min(1.0, covered / cost) if cost > 0 else None


def _allocation(alloc: dict) -> dict:
    return {'counted_points': alloc['counted'], 'attributed_points': alloc['attributed'],
            'unexplained_points': alloc['rest'], 'skipped_sections': alloc['skipped_sections'],
            'skipped_points': alloc['skipped_points']}


def reason_counts(report: dict, hook: str, mode: str, decision: str) -> dict:
    """The reasons of one decision of one gate in one mode, with the decisions of days that were archived without
    reasons as "reason not recorded"."""
    reasons = dict(report['guard_reasons'].get(hook, {}).get(mode, {}).get(decision, {}))
    missing = report['guards'].get(hook, {}).get(mode, {}).get(decision, 0) - sum(reasons.values())
    if missing > 0:
        reasons['reason not recorded'] = missing
    return dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0])))


def _reasons_text(reasons: dict, limit: int | None = None) -> str:
    """`name n, name n`; with a limit the rest of the reasons is summed up as "other"."""
    items = list(reasons.items())
    if limit is not None and len(items) > limit:
        items = items[:limit] + [('other', sum(count for _, count in items[limit:]))]
    return ', '.join(f'{name}: {count}' for name, count in items)


GATE_NAMES = (('spawn_gate', 'spawn gate', 'agent start'), ('soft_stop', 'soft stop', 'subagent tool call'))


def _gate_notes(report: dict) -> list[dict]:
    """A line for every gate and mode that denied or asked about something."""
    notes = []
    for hook, name, noun in GATE_NAMES:
        for mode, counts in sorted(report['guards'].get(hook, {}).items()):
            for decision, verb, rule in (('deny', 'denied', 'gate_denied'), ('ask', 'asked before', 'gate_asked')):
                count = counts.get(decision, 0)
                if not count:
                    continue
                reasons = reason_counts(report, hook, mode, decision)
                shadow = mode == 'shadow'
                note = '; ' + ('shadow mode, nothing was blocked' if shadow else f'{mode} mode') if mode != 'enforce' else ''
                text = (f'{name} {"would have " if shadow else ""}{verb} {count} {noun}{"" if count == 1 else "s"} '
                        f'({_reasons_text(reasons, MAX_REASONS)}{note})')
                notes.append({'rule': rule, 'gate': hook, 'mode': mode, 'count': count, 'reasons': reasons, 'text': text})
    return notes


def _window_note(windows: list[dict], threshold: float) -> list[dict]:
    hot = [w for w in windows if w['peak'] >= threshold]
    if not hot:
        return []
    top = max(hot, key=lambda w: w['peak'])
    day = day_text(_transcripts.parse_ts(top['peak_at']))
    text = (f'one 5h window reached {top["peak"]:g} % ({day})' if len(hot) == 1 else
            f'{len(hot)} 5h windows reached {threshold:g} % or more (highest {top["peak"]:g} %, {day})')
    return [{'rule': 'five_hour_window', 'windows': len(hot), 'threshold_percent': threshold,
             'highest_percent': top['peak'], 'highest_at': top['peak_at'], 'text': text}]


def _cache_note(projects: list[dict]) -> list[dict]:
    low = [{'name': p['name'], 'cache_hit_rate': p['cache_hit_rate']} for p in projects
           if p['cache_hit_rate'] is not None and p['cache_hit_rate'] < CACHE_LOW]
    if not low:
        return []
    listing = ', '.join(f'{p["name"]} {math.floor(100 * p["cache_hit_rate"])} %' for p in low)
    return [{'rule': 'cache_hits_low', 'threshold': CACHE_LOW, 'projects': low,
             'text': f'cache hits under {100 * CACHE_LOW:.0f} %: {listing}'}]


def _dominant_note(projects: list[dict]) -> list[dict]:
    top = projects[0] if projects else None
    if top is None or top['share'] is None or top['share'] <= 0.5:
        return []
    return [{'rule': 'project_dominates', 'project': top['name'], 'share': top['share'], 'cost_usd': top['cost_usd'],
             'text': f'{top["name"]} made {100 * top["share"]:.0f} % of the cost ({usd_text(top["cost_usd"])} USD)'}]


def _unexplained_note(allocation: dict) -> list[dict]:
    week, five = allocation['week']['unexplained_points'], allocation['five']['unexplained_points']
    parts = [f'{name} {points_text(points)} points' for name, points in (('week', week), ('5h', five)) if points > 0]
    if not parts:
        return []
    return [{'rule': 'unexplained_rise', 'week_points': week, 'five_points': five,
             'text': f'unexplained rise (no request with a price in between): {", ".join(parts)}'}]


def _title(period: dict) -> str:
    label = period['label']
    return {'day': f'Day {label}', 'month': f'Month {label}', 'year': f'Year {label}',
            'range': f'Days {label}'}.get(period['kind'], label[:1].upper() + label[1:])


def _measured_of(rows: list[dict]) -> dict:
    """`measured_share` of several sessions or projects: their parts, weighted by cost."""
    cost = sum(row['cost_usd'] for row in rows)
    return {metric: _measured(sum((row['measured_share'][metric] or 0.0) * row['cost_usd'] for row in rows), cost)
            for metric in METRICS}


def _project_row(name: str, clis: list[dict], total: float) -> dict:
    tokens = {kind: sum(cli['tokens'][kind] for cli in clis) for kind in _transcripts.KINDS}
    cost = sum(cli['cost_usd'] for cli in clis)
    return {'name': name, 'cost_usd': cost, 'share': cost / total if total > 0 else None,
            'week_points': sum(cli['estimated_share']['week'] for cli in clis),
            'five_points': sum(cli['estimated_share']['five'] for cli in clis),
            'measured_share': _measured_of(clis), 'sessions': len(clis),
            'cache_hit_rate': _hit_rate(tokens), 'session_ids': [cli['id'] for cli in clis]}


def build_overview(period: dict, report: dict, start_block_5h: float, notes: list[dict] | None = None) -> dict:
    """The overview from the other sections of the report; the text overview is made of this object alone. `notes` are
    the notable lines of the sync, last."""
    total, abo = report['total'], report['subscription']
    by_project: dict[str, list[dict]] = {}
    for cli in report['clis']:
        by_project.setdefault(cli['label'], []).append(cli)
    projects = sorted((_project_row(name, clis, total['cost_usd']) for name, clis in by_project.items()),
                      key=lambda row: (-row['cost_usd'], row['name']))
    rest = projects[MAX_PROJECTS:]
    more = None
    if rest:
        cost = sum(p['cost_usd'] for p in rest)
        more = {'projects': len(rest), 'cost_usd': cost,
                'share': cost / total['cost_usd'] if total['cost_usd'] > 0 else None,
                'week_points': sum(p['week_points'] for p in rest), 'five_points': sum(p['five_points'] for p in rest),
                'measured_share': _measured_of(rest), 'sessions': sum(p['sessions'] for p in rest)}
    segments, windows = abo['week_segments'], abo['five_windows']
    week = None
    if segments:
        resets = segments[-1]['reset_at']
        week = {'used_percent': segments[-1]['end']['value'],
                'rise_points': sum(seg['end']['value'] - seg['start']['value'] for seg in segments),
                'windows': len(segments), 'reset_at': resets}
    top = max(windows, key=lambda w: w['peak'], default=None)
    families: dict[str, float] = {}
    for name, model in total['models'].items():
        families[name.split()[0]] = families.get(name.split()[0], 0.0) + model['cost_usd']
    top_type = next(iter(total['subagents']['by_type'].items()), None)
    notable = (_gate_notes(report) + _window_note(windows, start_block_5h) + _cache_note(projects)
               + _dominant_note(projects) + _unexplained_note(report['allocation']) + (notes or []))
    machine = report['coverage'].get('sync', {}).get('machine')
    return {'title': _title(period) + ('' if machine is None else f' · machine {machine}'), 'cost_usd': total['cost_usd'], 'week': week,
            'five_hour': {'windows': len(windows), 'highest_percent': top['peak'] if top else None,
                          'highest_at': top['peak_at'] if top else None},
            'projects': projects, 'max_projects': MAX_PROJECTS, 'more': more,
            'split': {'main_usd': total['main']['cost_usd'], 'subagents_usd': total['subagents']['cost_usd'],
                      'top_subagent': {'type': top_type[0], 'cost_usd': top_type[1]['cost_usd'],
                                       'week_points': top_type[1]['week_points']} if top_type else None},
            'models': {'families': [{'name': name, 'cost_usd': cost} for name, cost in
                                    sorted(families.items(), key=lambda item: (-item[1], item[0]))],
                       'unpriced_requests': sum(model['requests'] for model in total['unpriced'].values())},
            'thinking_share': total['thinking_share'], 'cache_hit_rate': total['cache_hit_rate'],
            'startup': _overview_startup(report['startup']), 'notable': notable,
            'details': {'project': projects[0]['name'] if projects else None,
                        'session': projects[0]['session_ids'][0] if projects else None}}


def _group(starts: list[dict]) -> dict | None:
    """Median, smallest and largest start in tokens and how many of the starts were warm (read the cache)."""
    if not starts:
        return None
    totals = [start['tokens']['total'] for start in starts]
    warm = sum(1 for start in starts if start['tokens']['cache_read'] > 0)
    return {'starts': len(starts), 'median_tokens': round(statistics.median(totals)), 'min_tokens': min(totals),
            'max_tokens': max(totals), 'warm_starts': warm, 'warm_share': warm / len(starts)}


def startup_groups(starts: list[dict]) -> dict:
    """The measured summary of starts in report form (see `_start_report`): main sessions, subagents (together and by
    type, most starts first) and one row per local day."""
    main = [start for start in starts if start['type'] is None]
    subagents = [start for start in starts if start['type'] is not None]
    types: dict[str, list[dict]] = {}
    days: dict[str, list[dict]] = {}
    for start in starts:
        if start['type'] is not None:
            types.setdefault(start['type'], []).append(start)
        days.setdefault(local_day(_transcripts.parse_ts(start['at'])).isoformat(), []).append(start)
    return {'main': _group(main),
            'subagents': {'all': _group(subagents), 'by_type': {kind: _group(found) for kind, found in
                                                                sorted(types.items(), key=lambda i: (-len(i[1]), i[0]))}},
            'per_day': [{'day': day, 'main': _group([s for s in found if s['type'] is None]),
                         'subagents': _group([s for s in found if s['type'] is not None])}
                        for day, found in sorted(days.items())]}


def _start_report(starts: list[dict], estimate: dict | None) -> dict:
    shown = [{'at': iso(start['t']), 'session': start['session'], 'type': start['type'], 'model': start['model'],
              'tokens': {'input': start['input'], 'cache_write': start['cache_write'], 'cache_read': start['cache_read'],
                         'total': start['input'] + start['cache_write'] + start['cache_read']}}
             for start in sorted(starts, key=lambda start: start['t'])]
    return {'starts': shown, **startup_groups(shown), 'estimate': estimate}


def _overview_startup(startup: dict) -> dict | None:
    main, medians = startup['main'], [g['median_tokens'] for g in startup['subagents']['by_type'].values()]
    if main is None and not medians:
        return None
    return {'main_median_tokens': main['median_tokens'] if main else None,
            'subagent_type_median_tokens': {'min': min(medians), 'max': max(medians)} if medians else None}


def build_report(stats: dict, period: dict, coverage: dict, start_block_5h: float, estimate: dict | None = None,
                 contexts: dict | None = None, machines: dict | None = None, notes: list[dict] | None = None) -> dict:
    """`start_block_5h` (percent) is the threshold of the configuration from which a 5h window is notable; `estimate`
    the estimate of the fixed start (see `estimate_start`), if one was made; `contexts` the context split by CLI id
    (see `read_contexts`); with the sync `machines` the name of the machine by CLI id and `notes` its notable lines."""
    abo = stats['abo']
    contexts = contexts or {}
    windows = [{'resets': w['resets'], 'first_seen': iso(w['first']), 'peak': w['peak'], 'peak_at': iso(w['peak_at'])}
               for w in abo['five_windows']]
    subagents = {'requests': stats['sub']['requests'], 'cost_usd': stats['sub']['cost'],
                 'by_type': _types_report(stats['agent_types'], stats['points']['types'])}
    report = {
        'schema_version': SCHEMA_VERSION,
        'period': period,
        'coverage': coverage,
        'overview': None,
        'subscription': {'week_segments': [{'start': _point(w['start']), 'end': _point(w['end']), 'reset_at': w['resets']}
                                           for w in abo['weeks']],
                         'five_windows': windows},
        'total': {'requests': stats['requests'], 'tokens': stats['tokens'], 'cost_usd': stats['cost'],
                  'main': {'requests': stats['main']['requests'], 'cost_usd': stats['main']['cost'],
                           'week_points': stats['points']['main']['week'], 'five_points': stats['points']['main']['five']},
                  'subagents': subagents, 'models': _models_report(stats['models'], True),
                  'unpriced': _models_report(stats['unpriced'], False), 'cache_hit_rate': _hit_rate(stats['tokens']),
                  'cold_starts': stats['cold_starts'], **_details_report(stats['extra'])},
        'startup': _start_report(stats['starts'], estimate),
        'clis': [{**_cli_report(ident, cli), 'context': contexts.get(ident),
                  **({} if machines is None else {'machine': machines.get(ident)})}
                 for ident, cli in _by_cost(stats['clis'])],
        'allocation': {metric: _allocation(alloc) for metric, alloc in stats['alloc'].items()},
        'calibration': {metric: _calibration(cal) for metric, cal in stats['cal'].items()},
        'guards': stats['guards'],
        'guard_reasons': stats['guard_reasons'],
    }
    report['overview'] = build_overview(period, report, start_block_5h, notes)
    return report


def points_text(points: float, decimals: int = 1) -> str:
    """Points of the subscription: nothing is `0`, anything below one is `<1` (a rounded `0.0` or `1.0` for a small
    session would say more than the estimate knows)."""
    if points <= 0:
        return '0'
    return '<1' if points < 1 else f'{points:.{decimals}f}'


def usd_text(cost: float) -> str:
    """Two decimals below 1 USD, one below 10, none above."""
    return f'{cost:.2f}' if cost < 1 else f'{cost:.1f}' if cost < 9.95 else f'{cost:.0f}'


def _estimate_text(points: float, decimals: int) -> str:
    text = points_text(points, decimals)
    return text if text.startswith(('<', '0')) else '~' + text


def _share_text(points: float, measured: float | None, decimals: int) -> str:
    """An estimate of points, or `-` where none of the cost lay in a counted section: not measured, not zero."""
    return '-' if measured == 0 and points == 0 else _estimate_text(points, decimals)


def tokens_text(count: float) -> str:
    return f'{count / 1e6:.1f}M' if count >= 1e6 else f'{count / 1e3:.0f}k' if count >= 1e4 else str(int(count))


def _kinds_text(tokens: dict) -> str:
    return ', '.join(f'{kind.replace("_", " ")} {tokens_text(tokens[kind])}' for kind in _transcripts.KINDS)


def _costs_text(table: dict, with_points: bool = False) -> str:
    return ', '.join(f'{name} {v["cost_usd"]:.2f} USD ({v["requests"]})'
                     + (f' {_points_pair(v)}' if with_points else '') for name, v in table.items())


def _points_pair(entry: dict) -> str:
    """`~x pts ~y pts 5h`: the estimated points of the weekly and of the 5h limit."""
    return f'{_estimate_text(entry["week_points"], 2)} pts {_estimate_text(entry["five_points"], 1)} pts 5h'


def _week_text(segments: list[dict]) -> str:
    """`A % -> B % (+n points) until <reset>, then C % -> D % ...`: a weekly reset splits the line, nothing is
    subtracted across it. The last window says when it resets."""
    parts = []
    for index, segment in enumerate(segments):
        start, end = segment['start']['value'], segment['end']['value']
        text = f'{start:g} % -> {end:g} % ({end - start:+g} points)'
        if segment['reset_at'] is not None:
            text += f', resets {clock(segment["reset_at"], True)}' if index == len(segments) - 1 else \
                f' until {clock(segment["reset_at"], True)}'
        parts.append(text)
    return ', then '.join(parts)


def _subscription_lines(abo: dict) -> list[str]:
    lines = ['Subscription']
    if abo['week_segments']:
        lines.append('  Week: ' + _week_text(abo['week_segments']))
    else:
        lines.append('  Week: no measurement in this period')
    lines.append(f'  5h windows: {len(abo["five_windows"])}')
    for window in abo['five_windows']:
        reset = f'reset {clock(window["resets"])}, ' if window['resets'] is not None else ''
        lines.append(f'    {reset}peak {window["peak"]:g} % at {clock(_transcripts.parse_ts(window["peak_at"]), True)}'
                     f' (first seen {clock(_transcripts.parse_ts(window["first_seen"]))})')
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
    lines.append(f'  Main session: {_points_pair(total["main"])}')
    if total['subagents']['by_type']:
        lines.append(f'  Subagents by type: {_costs_text(total["subagents"]["by_type"], True)}')
    if total['models'] or total['unpriced']:
        lines.append('  Models:')
    for name, model in total['models'].items():
        lines.append(f'    model {name}: {model["cost_usd"]:.2f} USD, {model["requests"]} requests; '
                     f'{_details_text(model)}; cache hits {_percent(model["cache_hit_rate"])}')
    for name, model in total['unpriced'].items():
        lines.append(f'    model {name}: no price (not in the cost weight), {model["requests"]} request(s), '
                     f'{tokens_text(sum(model["tokens"].values()))} tokens; {_details_text(model)}')
    return lines


def _cli_block(head: str, cli: dict) -> list[str]:
    """The lines of one CLI (or of a sum of CLIs): the head line, then its tokens, models, output, subagents and cache."""
    share, measured = cli['estimated_share'], cli['measured_share']
    lines = [f'  {head + "  " if head else ""}{cli["cost_usd"]:.2f} USD  {cli["requests"]} requests  '
             f'{_share_text(share["five"], measured["five"], 1)} 5h points  '
             f'{_share_text(share["week"], measured["week"], 2)} week points',
             f'    tokens {_kinds_text(cli["tokens"])}',
             '    models ' + ', '.join(f'{name} {cost:.2f}' for name, cost in cli['models'].items()),
             f'    output {_details_text(cli)}']
    if cli['subagents']:
        lines.append(f'    subagents {_costs_text(cli["subagents"])}')
    rate = 'n/a' if cli['cache_hit_rate'] is None else f'{100 * cli["cache_hit_rate"]:.1f} %'
    lines.append(f'    cache hits {rate}, cold starts {cli["cold_starts"]}')
    return lines


def _cli_lines(clis: list[dict]) -> list[str]:
    lines = ['By CLI (main session and its subagents, most expensive first; ~ = estimate)']
    for cli in clis:
        started = clock(_transcripts.parse_ts(cli['start']), True)
        machine = f'  on {cli["machine"]}' if cli.get('machine') else ''
        lines += _cli_block(f'{cli["label"]}  {cli["id"]}  started {started}{machine}', cli)
    return lines


def _allocation_lines(allocation: dict) -> list[str]:
    lines = ['Allocation of the subscription rise (estimate)']
    for metric, name in (('five', '5h'), ('week', 'week')):
        a = allocation[metric]
        lines.append(f'  {name}: {a["counted_points"]:g} points counted, ~{a["attributed_points"]:.1f} shared by cost '
                     f'between the CLIs, {a["unexplained_points"]:g} unexplained (no request with a price in between)')
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
        days = [f'{day[5:]} {v["usd_per_point"]:.2f} ({v["sections"]})' for day, v in cal['per_day'].items()
                if v['usd_per_point'] is not None and v['sections'] >= MIN_DAY_SECTIONS]
        if len(days) > 1:
            lines.append('    per day: ' + ', '.join(days))
        if cal['per_model']:
            lines.append('    per model: ' + ', '.join(f'{m} {v["usd_per_point"]:.2f} ({v["sections"]})'
                                                      for m, v in cal['per_model'].items()))
    return lines


def _guard_lines(report: dict) -> list[str]:
    lines = ['Gates']
    for hook, name, _noun in GATE_NAMES:
        for mode, counts in sorted(report['guards'].get(hook, {}).items()):
            kind = {'shadow': 'shadow mode, only logged', 'enforce': 'enforce mode'}.get(mode, f'{mode} mode')
            if hook == 'spawn_gate':
                text = ', '.join(f'{label} {counts.get(key, 0)}' for key, label in
                                 (('allow', 'allowed'), ('warn', 'warned'), ('ask', 'asked'), ('deny', 'denied')))
            else:
                text = f'warnings {counts.get("warn", 0)}, denied {counts.get("deny", 0)}'
            lines.append(f'  {name} ({kind}): {text}')
            for decision, label in (('warn', 'warned'), ('ask', 'asked'), ('deny', 'denied')):
                if counts.get(decision):
                    lines.append(f'      {label} by reason: {_reasons_text(reason_counts(report, hook, mode, decision))}')
    return lines


def _plural(count: int, noun: str) -> str:
    return f'{count} {noun}' + ('' if count == 1 else 's')


def _group_text(group: dict) -> str:
    return (f'{_plural(group["starts"], "start")}, median {tokens_text(group["median_tokens"])} tokens, '
            f'min {tokens_text(group["min_tokens"])}, max {tokens_text(group["max_tokens"])}, '
            f'{group["warm_starts"]} warm ({_percent(group["warm_share"])})')


def _day_part(name: str, group: dict | None) -> str:
    return (f'{name} n/a' if group is None else
            f'{name} {tokens_text(group["median_tokens"])} tokens ({_plural(group["starts"], "start")})')


def _measured_start_lines(startup: dict) -> list[str]:
    lines = ['  Measured, the first request of a session or subagent (input + cache write + cache read):']
    if startup['main']:
        lines.append(f'    main: {_group_text(startup["main"])}')
    if startup['subagents']['by_type']:
        lines.append('    subagents by type:')
        lines += [f'      {kind}: {_group_text(group)}' for kind, group in startup['subagents']['by_type'].items()]
    lines.append('    per day:')
    for row in startup['per_day']:
        lines.append(f'      {day_text(day_start(date.fromisoformat(row["day"])))}: {_day_part("main", row["main"])}, '
                     f'{_day_part("subagents", row["subagents"])}')
    return lines


START_PARTS = {'claude_md': ('CLAUDE.md files', 'file'), 'memory': ('MEMORY.md', 'file'),
               'agents': ('agent descriptions', 'agent'), 'skills': ('skill descriptions', 'skill')}


def _estimate_lines(estimate: dict | None) -> list[str]:
    if estimate is None:
        return ['  Estimate (~): not available, no folder of a start in this period exists any more']
    lines = [f'  Estimate (~) from the files as they are now, for the folder of {estimate["project"]}, at '
             f'{estimate["chars_per_token"]:g} characters per token:']
    for part in estimate['parts']:
        label, noun = START_PARTS[part['name']]
        lines.append(f'    {label}: ~{tokens_text(part["tokens"])} tokens ({_plural(part["items"], noun)})')
        if part['name'] == 'claude_md':
            lines += [f'      {file["file"]}: ~{tokens_text(file["tokens"])}' for file in part['files']]
    lines.append(f'    together ~{tokens_text(estimate["total_tokens"])} tokens; not estimable: '
                 + ', '.join(estimate['not_estimable']))
    if estimate['subagents']:
        lines.append('    subagents with their own definition: ' + '; '.join(
            f'{agent["type"]}: omitClaudeMd, no CLAUDE.md files' if agent['omit_claude_md'] else
            f'{agent["type"]}: CLAUDE.md files ~{tokens_text(agent["claude_md_tokens"])} tokens'
            for agent in estimate['subagents']))
    return lines


def _startup_lines(startup: dict, title: str = 'Startup context', with_estimate: bool = True) -> list[str]:
    if not startup['starts']:
        return [title, '  no starts in this period']
    return [title] + _measured_start_lines(startup) + (_estimate_lines(startup['estimate']) if with_estimate else [])


def _shorten(name: str) -> str:
    head = NAME_WIDTH // 2
    return name if len(name) <= NAME_WIDTH else name[:head] + '…' + name[len(name) - (NAME_WIDTH - 1 - head):]


def short_names(names: list[str]) -> list[str]:
    """The names as the table shows them: one longer than NAME_WIDTH loses its middle for a `…` (the start keeps the odd
    character). Names whose short forms are the same are all kept whole."""
    short = [_shorten(name) for name in names]
    return [text if short.count(text) == 1 else name for name, text in zip(names, short)]


def _table_lines(view: dict) -> list[str]:
    shown = view['projects'][:view['max_projects']]
    rows = [(name, p['cost_usd'], p['share'], p['week_points'], p['measured_share']['week'], p['sessions'])
            for name, p in zip(short_names([p['name'] for p in shown]), shown)]
    if view['more']:
        more = view['more']
        rows.append((f'{more["projects"]} more', more['cost_usd'], more['share'], more['week_points'],
                     more['measured_share']['week'], more['sessions']))
    width = max(23, 4 + max(len(row[0]) for row in rows))
    lines = [f'{"Where it went":<{width}}{"USD":>5}{"share":>8}{"~week pts":>11}{"sessions":>10}']
    for name, cost, share, points, measured, sessions in rows:
        percent = '<1 %' if share is not None and 0 < share < 0.01 else _percent(share)
        shown_points = '-' if measured == 0 and points == 0 else points_text(points)
        lines.append(f'  {name:<{width - 2}}{usd_text(cost):>5}{percent:>8}{shown_points:>11}{sessions:>10}')
    return lines


def _split_lines(view: dict) -> list[str]:
    split = view['split']
    main = f'Main {usd_text(split["main_usd"])} USD · subagents {usd_text(split["subagents_usd"])} USD'
    if split['top_subagent']:
        top = split['top_subagent']
        main += f' (top: {top["type"]} {usd_text(top["cost_usd"])} USD, {_estimate_text(top["week_points"], 1)} week pts)'
    models = [f'{m["name"]} {usd_text(m["cost_usd"])} USD' for m in view['models']['families']]
    if view['models']['unpriced_requests']:
        models.append(f'{view["models"]["unpriced_requests"]} request(s) without a price')
    thinking = view['thinking_share']
    models += ['thinking n/a' if thinking is None else f'thinking {_percent(thinking)} of output',
               f'cache hits {_percent(view["cache_hit_rate"])}']
    lines = [main, ' · '.join(models)]
    start = view['startup']
    if start:
        main = start['main_median_tokens']
        parts = [] if main is None else [f'main {tokens_text(main)} tokens (median)']
        types = start['subagent_type_median_tokens']
        if types:
            low, high = tokens_text(types['min']), tokens_text(types['max'])
            parts.append(f'subagents {low if low == high else low + "-" + high} (median per type)')
        lines.append('Start: ' + ', '.join(parts))
    return lines


def render_overview(report: dict) -> str:
    """The overview, made of `report['overview']` alone: the object in the JSON is what the text says."""
    view = report['overview']
    week, five = view['week'], view['five_hour']
    if week is None:
        used = 'no weekly measurement'
    elif week['windows'] == 1:
        used = f'{week["used_percent"]:g} % of the weekly limit used (+{week["rise_points"]:g} points)'
    else:      # the rises of several weekly windows add up, the percentage is that of the last one
        used = (f'weekly limit at {week["used_percent"]:g} % now · +{week["rise_points"]:g} points over '
                f'{week["windows"]} weekly windows')
    windows = ('none measured' if not five['windows'] else
               f'{five["windows"]}, highest {five["highest_percent"]:g} % '
               f'({clock(_transcripts.parse_ts(five["highest_at"]), True)})')
    blocks = [[f'{view["title"]} · {used} · {usd_text(view["cost_usd"])} USD at API prices', f'5h windows: {windows}']]
    blocks += [_table_lines(view), _split_lines(view)] if view['projects'] else [['No requests in this period.']]
    if view['notable']:
        blocks.append(['Notable'] + [f'  {note["text"]}' for note in view['notable']])
    details = view['details']
    options = [f'--project {details["project"]}', f'--session {details["session"]}'] if details['project'] else []
    blocks.append(['Details: ' + ' · '.join(options + ['--full'])])
    return '\n\n'.join('\n'.join(block) for block in blocks)


def _home_short(path: str) -> str:
    """`path` with the home folder written `~`."""
    home = os.path.expanduser('~').rstrip('/\\')
    return '~' + path[len(home):] if home and (path == home or path.startswith((home + '/', home + '\\'))) else path


def _top_lines(rows: list[tuple[str, int]], indent: str) -> list[str]:
    """The MAX_LISTED dearest rows (name, tokens), dearest first (equal tokens by name), then how many are left out."""
    rows = sorted(rows, key=lambda row: (-row[1], row[0]))
    lines = [f'{indent}{name}: {tokens:,}' for name, tokens in rows[:MAX_LISTED]]
    if len(rows) > MAX_LISTED:
        lines.append(f'{indent}… {len(rows) - MAX_LISTED} more')
    return lines


DEFERRED = ' (deferred, not in the context)'


def _context_lines(cli: dict) -> list[str]:
    """The block of --session: how the context of the session was made up after its first answer."""
    context = cli['context']
    if context is None:
        return ['No context recorded for this session.']
    window = f'{context["total_tokens"]:,} of {context["max_tokens"]:,} tokens'
    if context['max_tokens']:
        window += f' ({100 * context["total_tokens"] / context["max_tokens"]:.1f} %)'
    labels = [f'{name} {context[name]}' for name in ('model', 'detail') if context[name]]
    lines = [f'Context after the first answer (session {cli["id"]}, {cli["label"]}, measured {context["at"]})',
             '  ' + ', '.join([window] + labels)]
    if context['categories']:
        lines.append('  Categories')
        # Deferred tools are listed with their size but not loaded: their tokens are not part of the total
        lines += [f'    {row["name"]}: {row["tokens"]:,}' + (DEFERRED if row['kind'] == 'deferred' else '')
                  for row in context['categories']]
    servers: dict[str, list[dict]] = {}
    for tool in context['mcp_tools']:
        servers.setdefault(tool['server'], []).append(tool)
    if servers:
        total = sum(tool['tokens'] for tool in context['mcp_tools'])
        waiting = [tool for tool in context['mcp_tools'] if tool['loaded'] is False]
        lines.append(f'  MCP servers: {total:,} tokens in {_plural(len(servers), "server")}'
                     + ('' if not waiting else DEFERRED if len(waiting) == len(context['mcp_tools'])
                        else f', {sum(tool["tokens"] for tool in waiting):,} of them deferred'))
    else:
        lines.append('  MCP servers: none')
    for server, tools in sorted(servers.items(), key=lambda item: (-sum(t['tokens'] for t in item[1]), item[0])):
        waiting = sum(1 for t in tools if t['loaded'] is False)
        lines.append(f'    {server}: {sum(t["tokens"] for t in tools):,} tokens, {_plural(len(tools), "tool")}'
                     + ('' if not waiting else ', deferred' if waiting == len(tools) else f', {waiting} deferred'))
        lines += _top_lines([(t['name'], t['tokens']) for t in tools], '      ')
    memory = context['memory_files']
    lines.append(f'  Memory files: {sum(f["tokens"] for f in memory):,} tokens in {_plural(len(memory), "file")}'
                 if memory else '  Memory files: none')
    lines += _top_lines([(_home_short(f['path']) + (f' ({f["type"]})' if f['type'] else ''), f['tokens'])
                         for f in memory], '    ')
    skills = context['skills']
    if skills is None:
        lines.append('  Skills: no skill breakdown')
    else:
        lines.append(f'  Skills: {skills["tokens"]:,} tokens, {skills["included"]} of {skills["total"]} included')
        lines += _top_lines([(item['name'], item['tokens']) for item in skills['items']], '    ')
    agents = context['agents']
    lines.append(f'  Agents: {sum(a["tokens"] for a in agents):,} tokens in {_plural(len(agents), "agent")}'
                 if agents else '  Agents: none')
    lines += _top_lines([(a['type'] + (f' ({a["source"]})' if a['source'] else ''), a['tokens']) for a in agents], '    ')
    return lines


class SelectionError(Exception):
    pass


def select_clis(report: dict, stats: dict, project: str | None, session: str | None) -> dict:
    """The CLIs of one project (its name or the start of it) and/or one session (the start of its id, or longer),
    with their sum. Raises SelectionError with what to say when nothing, or more than one, fits."""
    clis = report['clis']
    parts, context_of = [], None
    if project is not None:
        if not project:
            raise SelectionError('--project must not be empty')
        names = [p['name'] for p in report['overview']['projects']]
        found = [project] if project in names else [name for name in names if name.startswith(project)]
        if not found:
            raise SelectionError(f"--project '{project}' matches no project in this period "
                                 f"(projects: {', '.join(names) or 'none'})")
        if len(found) > 1:
            raise SelectionError(f"--project '{project}' matches {len(found)} projects: {', '.join(found)} "
                                 '- give more characters')
        project = found[0]
        clis = [cli for cli in clis if cli['label'] == project]
        parts.append(f'project {project}')
    if session is not None:
        if not session:
            raise SelectionError('--session must not be empty')
        found = [cli for cli in clis if cli['id'].startswith(session) or session.startswith(cli['id'])]
        if not found:
            raise SelectionError(f"--session '{session}' matches no session "
                                 + (f'in project {project}' if project is not None else 'in this period'))
        if len(found) > 1:
            listing = ', '.join(f'{cli["id"]} ({cli["label"]})' for cli in found)
            raise SelectionError(f"--session '{session}' matches {len(found)} sessions: {listing} - give more characters")
        clis = found
        context_of = found[0]
        parts.append(f'session {found[0]["id"]}')
    merged: dict = {}
    for cli in clis:
        _merge_cli(merged, 'sum', stats['clis'][cli['id']])
    ids = {cli['id'] for cli in clis}
    starts = [start for start in report['startup']['starts'] if start['session'] in ids]
    return {'description': ', '.join(parts), 'clis': clis, 'sum': _cli_report('sum', merged['sum']),
            'startup': {'starts': starts, **startup_groups(starts)}, 'context_of': context_of}


def render(report: dict, selection: dict | None = None) -> str:
    """The full report; with a selection (see select_clis) only the blocks of those CLIs and their sum."""
    period, coverage = report['period'], report['coverage']
    offset = datetime.fromtimestamp(_transcripts.parse_ts(period['start'])).astimezone().strftime('%z')
    sources = [f'{coverage["transcript_files"]} transcript file(s) read']
    if coverage['archive_days']:
        sources.append(f'{len(coverage["archive_days"])} day(s) from the archive: {", ".join(coverage["archive_days"])}')
    if coverage['archive_days_without_agent_split']:
        days = coverage['archive_days_without_agent_split']
        sources.append(f'{len(days)} archived day(s) without the split of points by agent type: {", ".join(days)}')
    for key, name in (('unreadable_history_lines', 'history'), ('unreadable_archive_lines', 'archive'),
                      ('unreadable_transcript_lines', 'transcript')):
        if coverage[key]:
            sources.append(f'{coverage[key]} unreadable {name} line(s) skipped')
    if coverage['unreadable_context_files']:
        sources.append(f'{coverage["unreadable_context_files"]} unreadable context file(s) skipped')
    sync = coverage.get('sync')
    if sync is not None:
        sources.append(sync['text'])
        skipped = [f'{count} {name.replace("_", " ")}' for name, count in sync['skipped'].items() if count]
        if skipped:
            sources.append(f'{sum(sync["skipped"].values())} item(s) of the sync repository skipped '
                           f'({", ".join(skipped)})')
        if sync['unreadable_day_lines']:
            sources.append(f'{sync["unreadable_day_lines"]} unreadable day line(s) of other machines skipped')
    title = f'Usage report {period["label"]} (local time, UTC{offset[:3]}:{offset[3:]})'
    head = [title + (f' - {selection["description"]}' if selection else ''), 'Sources: ' + '; '.join(sources)]
    if selection:
        count = len(selection['clis'])
        total = [f'Sum of {count} CLI{"" if count == 1 else "s"}'] + _cli_block('', selection['sum'])
        sections = [head, _cli_lines(selection['clis']), total,
                    _startup_lines(selection['startup'], 'Startup context of the selected sessions', False)]
        if selection['context_of'] is not None:
            sections.append(_context_lines(selection['context_of']))
    else:
        sections = [head, _subscription_lines(report['subscription']), _total_lines(report['total']),
                    _startup_lines(report['startup'])]
        if report['clis']:
            sections.append(_cli_lines(report['clis']))
        sections += [_allocation_lines(report['allocation']), _calibration_lines(report['calibration'])]
        if report['guards']:
            sections.append(_guard_lines(report))
    return '\n\n'.join('\n'.join(section) for section in sections)


# --- The estimate of the fixed start ------------------------------------------------------------------------------
# Only the size of local files is read, never their text: how many characters a start would read at the beginning.

FRONT_MATTER = re.compile(r'\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)', re.S)
BLOCK_MARK = re.compile(r'[>|][+-]?\d*')
FILE_NAME_CHARS = re.compile(r'[^\w .+@=/~-]')


def _read_chars(path: str, limit: int) -> str | None:
    """The first `limit` characters of a regular file (undecodable bytes become one character each), else None."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read(limit)
    except OSError:
        return None


def description_length(text: str) -> int | None:
    """The characters of the `description` in the frontmatter of an agent or skill file, folded to one line like a
    YAML block or plain value is read; None if there is none or it is empty."""
    match = FRONT_MATTER.match(text)
    lines = match.group(1).splitlines() if match else []
    start = next((number for number, line in enumerate(lines) if line.startswith('description:')), None)
    if start is None:
        return None
    parts = [lines[start][len('description:'):]]
    for line in lines[start + 1:]:
        if line.strip() and not line[0].isspace():
            break          # the next key
        parts.append(line)
    words = [part.strip() for part in parts if part.strip()]
    if words and BLOCK_MARK.fullmatch(words[0]):
        words = words[1:]
    value = ' '.join(words)
    if len(value) > 1 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1]
    return len(value) or None


def _markdown_files(folder: str) -> list[str]:
    """The .md files below a folder, in name order, without the rules that only apply to some paths (`paths:` in their
    frontmatter): those are not read at the start."""
    found = []
    for root, folders, files in os.walk(folder):
        folders.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            head = _spawn_check._read_head(path) if name.endswith('.md') else {'paths': ''}
            if 'paths' not in (head or {}):
                found.append(path)
    return found


def _ancestors(folder: str) -> list[str]:
    """The folder and every folder above it, the folder first."""
    found = [folder]
    while os.path.dirname(found[-1]) != found[-1]:
        found.append(os.path.dirname(found[-1]))
    return found


def claude_md_files(user_dir: str, cwd: str) -> list[dict]:
    """The memory files a start in `cwd` loads: CLAUDE.md and rules of the user folder, then, from the outermost folder
    to `cwd`, CLAUDE.md, CLAUDE.local.md, .claude/CLAUDE.md and .claude/rules of each. A file counts once. Names are
    relative to the user folder (`~/.claude/...`) or to `cwd`."""
    candidates = [('~/.claude/CLAUDE.md', os.path.join(user_dir, 'CLAUDE.md'))]
    candidates += [('~/.claude/' + os.path.relpath(path, user_dir).replace(os.sep, '/'), path)
                   for path in _markdown_files(os.path.join(user_dir, 'rules'))]
    for folder in reversed(_ancestors(cwd)):
        paths = [os.path.join(folder, 'CLAUDE.md'), os.path.join(folder, 'CLAUDE.local.md'),
                 os.path.join(folder, '.claude', 'CLAUDE.md')] + _markdown_files(os.path.join(folder, '.claude', 'rules'))
        candidates += [(os.path.relpath(path, cwd).replace(os.sep, '/'), path) for path in paths]
    files, seen = [], set()
    for label, path in candidates:
        text = _read_chars(path, MAX_FILE_CHARS)
        if text is not None and os.path.realpath(path) not in seen:
            seen.add(os.path.realpath(path))
            files.append({'file': FILE_NAME_CHARS.sub('?', label), 'chars': len(text)})
    return files


def repository_root(cwd: str) -> str:
    """The main working tree of the git repository `cwd` is in: the memory of a repository is shared by its linked
    worktrees (their `.git` is a file that names `<main>/.git/worktrees/<name>`) and its subfolders. `cwd` itself
    if it is in no repository."""
    for folder in _ancestors(cwd):
        marker = os.path.join(folder, '.git')
        if os.path.isdir(marker):
            return folder
        text = _read_chars(marker, 4096)
        if text is not None:
            found = re.match(r'gitdir: (.*)/\.git/worktrees/[^/\n]+\s*$', text)
            return found.group(1) if found else folder
    return cwd


def project_folders(cwd: str) -> list[str]:
    """The folders whose `.claude/agents` and `.claude/skills` a start in `cwd` loads, `cwd` first: up to the root of
    its git working tree (the folder with `.git`, in a linked worktree a file), only `cwd` outside a repository."""
    found = []
    for folder in _ancestors(cwd):
        found.append(folder)
        if os.path.exists(os.path.join(folder, '.git')):
            return found
    return [cwd]


def memory_file(projects_dir: str, cwd: str) -> list[dict]:
    """MEMORY.md of the repository of `cwd`, else of `cwd` itself (as the folder name is made from the path), as far as
    Claude Code loads it (the first lines and bytes): a list of zero or one file."""
    for folder in dict.fromkeys([repository_root(cwd), cwd]):
        text = _read_chars(os.path.join(projects_dir, re.sub('[^A-Za-z0-9]', '-', folder), 'memory', 'MEMORY.md'),
                           MAX_FILE_CHARS)
        if text is not None:
            lines = ''.join(text.splitlines(keepends=True)[:MEMORY_LINES])
            return [{'file': 'MEMORY.md', 'chars': len(lines[:MEMORY_CHARS])}]
    return []


def agent_definitions(folders: list[str]) -> dict[str, dict]:
    """name -> {'chars': characters of name and description, 'omit': omitClaudeMd} of the agents (every .md file with a
    name and a description) in the folders; the first definition of a name wins."""
    found: dict[str, dict] = {}
    for folder in folders:
        for root, subfolders, files in os.walk(folder):
            subfolders.sort()
            for file in sorted(name for name in files if name.endswith('.md')):
                path = os.path.join(root, file)
                head = _spawn_check._read_head(path)
                name = head.get('name') if head else None
                length = description_length(_read_chars(path, HEAD_CHARS)) if name and name not in found else None
                if length:
                    found[name] = {'chars': len(name) + length, 'omit': head.get('omitClaudeMd', '').lower() == 'true'}
    return found


def skill_definitions(folders: list[str]) -> dict[str, int]:
    """name -> characters of name and description of the skills (`<folder>/<skill>/SKILL.md` with a description; the name
    is the `name` of the frontmatter, else the name of the skill's folder); the first definition of a name wins. A skill
    with `disable-model-invocation: true` (any case) has no description in the context and is left out."""
    found: dict[str, int] = {}
    for folder in folders:
        try:
            skills = sorted(os.listdir(folder))
        except OSError:
            continue
        for skill in skills:
            text = _read_chars(os.path.join(folder, skill, 'SKILL.md'), HEAD_CHARS)
            length = description_length(text) if text is not None else None
            head = _spawn_check._head(text) if length else {}
            if head.get('disable-model-invocation', '').lower() == 'true':
                continue
            name = (head.get('name') or skill) if length else None
            if name and name not in found:
                found[name] = len(name) + length
    return found


def estimate_start(project: str, cwd: str, user_dir: str, projects_dir: str, factor: float, types: list[str]) -> dict:
    """The estimate (marked ~) of what a start in `cwd` reads from local files, at `factor` characters per token: the
    CLAUDE.md files, MEMORY.md, the descriptions of agents and skills. `types` are the agent types of the subagent starts
    of the period: those with a definition get their share of the CLAUDE.md files (none with omitClaudeMd)."""
    def tokens(chars: int) -> int:
        return round(chars / factor)

    def with_tokens(files: list[dict]) -> list[dict]:
        return [{**file, 'tokens': tokens(file['chars'])} for file in files]

    def part(name: str, items: int, chars: int, files: list[dict] | None = None) -> dict:
        return {'name': name, 'items': items, **({} if files is None else {'files': with_tokens(files)}),
                'chars': chars, 'tokens': tokens(chars)}

    chain, memory = claude_md_files(user_dir, cwd), memory_file(projects_dir, cwd)
    folders = [os.path.join(folder, '.claude') for folder in project_folders(cwd)] + [user_dir]
    agents = agent_definitions([os.path.join(folder, 'agents') for folder in folders])
    skills = skill_definitions([os.path.join(folder, 'skills') for folder in folders])
    parts = [part('claude_md', len(chain), sum(f['chars'] for f in chain), chain),
             part('memory', len(memory), sum(f['chars'] for f in memory), memory),
             part('agents', len(agents), sum(a['chars'] for a in agents.values())),
             part('skills', len(skills), sum(skills.values()))]
    return {'project': project, 'chars_per_token': factor, 'parts': parts, 'total_tokens': sum(p['tokens'] for p in parts),
            'not_estimable': list(NOT_ESTIMABLE),
            'subagents': [{'type': kind, 'omit_claude_md': agents[kind]['omit'],
                           'claude_md_tokens': 0 if agents[kind]['omit'] else parts[0]['tokens']}
                          for kind in sorted(types) if kind in agents]}


def estimate_folder(data: Data, start: float, end: float) -> tuple[str, str] | None:
    """(project, folder) of the latest start in the period whose working folder still exists, else None."""
    lo, hi = bisect.bisect_left(data.start_times, start), bisect.bisect_left(data.start_times, end)
    for index in reversed(data.start_indices[lo:hi]):
        request = data.requests[index]
        if request.cwd and os.path.isabs(request.cwd) and os.path.isdir(request.cwd):
            return data.label.get(request.session, '?'), request.cwd
    return None


# --- The archive --------------------------------------------------------------------------------------------------

def write_archive(log_dir: str, new_days: dict[str, dict]) -> None:
    """Appends a line for each day of `new_days` that the file does not have yet (a day is never overwritten). Same
    care as the measurement history: folder 0700, file 0600, opened without following links, a cut-off last line
    gets a line break, one writer at a time (a file lock, see _plat.py). Raises OSError."""
    if os.path.islink(log_dir):
        raise OSError(f'{log_dir} is a link')
    os.makedirs(os.path.dirname(os.path.abspath(log_dir)), mode=0o700, exist_ok=True)
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    descriptor = os.open(os.path.join(log_dir, ARCHIVE_FILE),
                         os.O_RDWR | os.O_APPEND | os.O_CREAT | _usage.NO_FOLLOW | _plat.O_BINARY, 0o600)
    with os.fdopen(descriptor, 'r+b') as archive, _usage.locked(archive):
        _plat.fchmod(descriptor, 0o600)
        content = archive.read()
        known = {day for day, _ in _archive_days(content.decode('utf-8', errors='replace'))}
        text = ''.join(json.dumps({'v': ARCHIVE_VERSION, 'day': day, 'stats': stats}, separators=(',', ':')) + '\n'
                       for day, stats in sorted(new_days.items()) if day not in known)
        if text:
            archive.write((b'\n' if content and not content.endswith(b'\n') else b'') + text.encode('utf-8'))


# --- Other machines: the sync -------------------------------------------------------------------------------------
# With sync.mode shadow or on the report adds the folders of the other machines in the local clone of the sync
# repository (hooks/sync_format.py reads them as foreign data). This machine comes from its local files; of its own
# folder in the clone only the requests are read, which the local ones replace by `k` (the transcripts Claude Code
# deleted after 30 days are still there). Requests count once by `k`, measurements of several machines once, a day
# line only for a day without requests on any machine.

PULL_GAP_S = 300            # a report pulls again only after this long since the last successful pull
SAME_MEASUREMENT_S = 120    # equal measurements (both values, both reset times) within this time are one
STALE_PUSH_S = 24 * 3600    # a machine without a successful push for longer is notable
NO_START = 'sync:'          # the source of a request of the clone that is not the first of its transcript
SUBSCRIPTION_KEYS = ('abo', 'alloc', 'cal', 'points')      # the parts of a day line that only one line may give


def machine_name(text: str) -> str:
    """A name of a machine as the report prints it: control characters as `?`, at most 64 characters. Names come from
    foreign data; sync_format already allows only a small set of characters."""
    return CONTROL_CHARS.sub('?', text)[:64] or '?'


class Machines:
    """The machines of the sync: this one (key `own`, from its local files) and the others (their folders in the
    clone), and what was read of the clone: request lines, measurements, gate decisions and day lines by machine."""

    def __init__(self, own: str, name: str, last_success: float | None):
        self.own = own                    # the machine id, or '' if this machine never synced
        self.names = {own: name}
        self.platforms = {own: sync_format.platform_name()}
        self.last_success: dict[str, float | None] = {own: last_success}
        self.requests: list[dict] = []
        self.history: list[Measurement] = []
        self.guards: dict[str, list[tuple]] = {}
        self.days: dict[str, dict[str, dict]] = {}
        self.skipped = dict.fromkeys(sync_format.COUNTERS, 0)
        self.broken_days = 0

    def add(self, key: str, machine: sync_format.Machine) -> None:
        """The folder of another machine. Of lines without `k` a copy (a push cut off and repeated) counts once: a
        measurement per time, a gate decision per line, a day line per day (the first, as in the local archive)."""
        info, status = machine.machine or {}, machine.status or {}
        self.names[key] = machine_name(info.get('name') or sync_format.display_name(key, ''))
        self.platforms[key] = info.get('platform')
        self.last_success[key] = sync_format.parse_time(status['last_success']) if status else None
        self.requests += [{**row, '_machine': key} for row in machine.requests]
        times: set[float] = set()
        for row in machine.history:
            ts = sync_format.parse_time(row['t'])
            if ts not in times:
                times.add(ts)
                self.history.append(Measurement(ts, row['five'], row['week'], row['five_resets'], row['week_resets']))
        guards = []
        for t, hook, decision, mode, reason in dict.fromkeys(
                (row['t'], row['hook'], row['decision'], row['mode'], row['reason']) for row in machine.gates):
            guards.append((sync_format.parse_time(t), hook, mode, decision,
                           reason_kind(reason) if decision in REASONED else None))
        self.guards[key] = guards
        days: dict[str, dict] = {}
        for row in machine.days:
            try:
                day, stats = parse_archive_line(json.dumps(row))
            except (ValueError, TypeError, KeyError, RecursionError):
                self.broken_days += 1
                continue
            days.setdefault(day, stats)
        self.days[key] = days

    def merge_requests(self, local: list[_transcripts.Request]) -> tuple[list[_transcripts.Request], dict, dict]:
        """(requests, machine by session, project label by session of the clone). The local requests and those of
        the clone count once by `k`, by the reading rule of the format; of equal lines the local one (read last)."""
        rows, keyless = list(self.requests), []
        for request in local:
            if request.ids is None:
                keyless.append(request)      # no message id: no `k` the export could have made the same
                continue
            rows.append({'t': sync_format.iso_time(request.ts), 'sid': request.session, 'out': request.tokens[4],
                         'k': sync_format.request_key(request.ids[0], request.ids[1] or ''), '_request': request,
                         '_machine': self.own})
        winners = [(row.get('_request') or _clone_request(row), row) for row in sync_format.latest_by_key(rows).values()]
        winners += [(request, {'_machine': self.own}) for request in keyless]
        machine_of: dict[str, str] = {}
        labels: dict[str, str] = {}
        for request, row in sorted(winners, key=lambda pair: pair[0].ts):     # a session runs on the machine it began on
            machine_of.setdefault(request.session, row['_machine'])
            if 'proj' in row:
                labels.setdefault(request.session, row['proj'])
        return [request for request, _row in winners], machine_of, labels

    def combine_day(self, lines: list[tuple[str, dict]], machine_of: dict[str, str]) -> dict:
        """The figures of a day without requests from its day lines: the usage of every machine added, the
        subscription (abo, allocation, calibration, points) of the first line only. A line that does not add up with
        the others is counted and left out. The CLIs are noted with their machine."""
        combined = new_stats()
        first = lines[0][1]
        for index, (key, stats) in enumerate(lines):
            trial = copy.deepcopy(combined)
            try:
                merge_stats(trial, stats if index == 0 else _usage_only(stats))
            except (TypeError, ValueError, KeyError, AttributeError):
                self.broken_days += 1
                continue
            combined = trial
            for ident in stats.get('clis', {}):
                machine_of.setdefault(ident, key)
        for key in ('starts', 'points'):
            if key not in first:
                del combined[key]       # archived by an earlier version: the main loop knows what to do
        return combined


def _clone_request(row: dict) -> _transcripts.Request:
    """A request line of the clone as a request of the report. Only the first request of a transcript is a start."""
    source = f'{NO_START}{row["_machine"]}:{row["k"]}' if row['first'] else NO_START
    return _transcripts.Request(sync_format.parse_time(row['t']), row['sid'], row['sub'] is not None, row['sub'],
                                row['model'], (row['in'], row['cw5'], row['cw1h'], row['cr'], row['out']), None,
                                row['think'], row['ws'], row['wf'], source=source)


def merge_measurements(measurements: list[Measurement]) -> list[Measurement]:
    """The measurements of all machines in time order; equal ones (both values and both reset times) within
    SAME_MEASUREMENT_S of one that is kept are one measurement: every machine measures the same subscription."""
    kept, last = [], {}
    for m in sorted(measurements, key=lambda m: m.t):
        values = m[1:]
        if values in last and m.t - last[values] <= SAME_MEASUREMENT_S:
            continue
        last[values] = m.t
        kept.append(m)
    return kept


def read_clone(root: str, machines: Machines) -> None:
    """Adds the folders of the other machines in the clone at `root`, and the requests of the own folder."""
    repo = sync_format.read_repo(root, skip=machines.own or None)
    machines.skipped = repo.skipped
    for key, machine in sorted(repo.machines.items()):
        machines.add(key, machine)
    if machines.own and os.path.isdir(os.path.join(root, sync_format.MACHINES, machines.own)):
        own = sync_format.read_machine(root, machines.own, machines.skipped)
        machines.requests += [{**row, '_machine': machines.own} for row in own.requests]


def _state(log_dir: str) -> _sync_core.State:
    try:
        return _sync_core.read_state(os.path.join(log_dir, _sync_core.SYNC_DIR))
    except _sync_core.BrokenState:
        return _sync_core.State()


def sync_push(log_dir: str, cfg: dict, now: float) -> dict:
    """`--sync`: the push of this machine past the wait and the backoff (`sync.py push --now`), never past the block
    after `visibility` and never past the lock. Its status: `ok`, `failed` (with the category), `blocked`, `locked`."""
    before = _state(log_dir)
    try:
        code = _sync_core.push(log_dir, cfg, _sync_git.push, forced=True, clock=lambda: now)
    except OSError:
        return {'status': 'failed', 'category': 'internal'}
    after = _state(log_dir)
    if after == before:      # nothing ran: blocked after `visibility`, or another run holds the lock
        return {'status': 'blocked', 'category': 'visibility'} if before.blocked else {'status': 'locked',
                                                                                       'category': None}
    return {'status': 'ok', 'category': None} if code == 0 else {'status': 'failed', 'category': after.last_error}


def sync_pull(log_dir: str, cfg: dict, now: float, wanted: bool, forced: bool) -> dict:
    """The pull before a report: none with --no-pull (`skipped`), none within PULL_GAP_S of the last successful one
    unless `forced` (`recent`), else `sync.py pull` with sync.pull_timeout_s: `ok`, `locked` (another run holds the
    lock: the clone as it is), `diverged` (the history could not be brought together: nothing merged) or `failed`
    with its category. `detail` is the fixed text of the error (never output of git)."""
    if not wanted:
        return {'status': 'skipped', 'category': None, 'detail': None}
    last = _state(log_dir).last_pull
    if not forced and last is not None and 0 <= now - last < PULL_GAP_S:
        return {'status': 'recent', 'category': None, 'detail': None}
    try:
        outcome = _sync_core.pull_outcome(log_dir, cfg, sync_command.git_pull, background=False, clock=lambda: now)
    except OSError:
        return {'status': 'failed', 'category': 'internal', 'detail': 'the sync folder cannot be used'}
    if outcome.status == 'failed':
        return {'status': 'diverged' if outcome.category == 'conflict' else 'failed', 'category': outcome.category,
                'detail': outcome.detail}
    return {'status': outcome.status, 'category': None, 'detail': None}


def age_text(seconds: float) -> str:
    if seconds < 60:
        return 'just now'
    if seconds < 3600:
        return f'{seconds // 60:.0f} min ago'
    return f'{seconds // 3600:.0f} h ago' if seconds < 48 * 3600 else f'{seconds // 86400:.0f} d ago'


def _when(moment: float | None, now: float, never: str = 'never') -> str:
    return never if moment is None else age_text(now - moment)


PULL_TEXTS = {'ok': 'pull ok', 'skipped': 'no pull (--no-pull)', 'locked': 'no pull, another sync run holds the lock',
              'failed': 'pull failed ({category}), last pull {last}',
              'diverged': 'sync history not brought together, nothing merged, last pull {last}',
              'recent': 'last pull {last}'}
PUSH_TEXTS = {'ok': 'push ok', 'failed': 'push failed ({category})', 'locked': 'no push, another sync run holds the lock',
              'blocked': 'no push: the sync repository is public (visibility)'}


def sync_coverage(machines: Machines, pull: dict, push: dict | None, state: _sync_core.State, cfg: dict,
                  chosen: str | None, now: float) -> dict:
    """`coverage.sync`: the machines with the time of their last successful push, the pull, the push of --sync, the
    backoff of this machine and what was skipped in the clone; `text` is the line `Sources:` prints."""
    keys = [machines.own] + sorted((key for key in machines.names if key != machines.own),
                                   key=lambda key: (machines.names[key], key))
    last_pull = state.last_pull
    pull = {**pull, 'last_pull': None if last_pull is None else iso(last_pull)}
    parts = [f'Sync: {_plural(len(keys), "device")}']
    parts += [f'{machines.names[key]} {_when(machines.last_success[key], now, "never pushed")}' for key in keys]
    if push is not None:
        parts.append(PUSH_TEXTS[push['status']].format(category=push['category']))
    parts.append(PULL_TEXTS[pull['status']].format(category=pull['category'], last=_when(last_pull, now)))
    if chosen is not None:
        parts.append(f'only {machines.names[chosen]}')
    return {'mode': cfg['sync']['mode'], 'text': ' · '.join(parts),
            'machine': None if chosen is None else machines.names[chosen],
            'machines': [{'id': key or None, 'name': machines.names[key], 'platform': machines.platforms[key],
                          'own': key == machines.own,
                          'last_success': None if machines.last_success[key] is None else iso(machines.last_success[key])}
                         for key in keys],
            'pull': pull, 'push': push,
            'backoff': {'level': state.backoff_level, 'last_error': state.last_error,
                        'next_due': None if state.next_due is None else iso(state.next_due)},
            'skipped': dict(machines.skipped), 'unreadable_day_lines': machines.broken_days}


def sync_notes(sync: dict, state: _sync_core.State, now: float) -> list[dict]:
    """The notable lines of the sync: machines without a push for over 24 hours, this machine blocked or backing off,
    a pull that failed or found no fast forward."""
    notes = []
    stale = [machine for machine in sync['machines'] if machine['last_success'] is None
             or now - _transcripts.parse_ts(machine['last_success']) > STALE_PUSH_S]
    if stale:
        listing = ', '.join(f'{m["name"]} ' + ('never' if m['last_success'] is None else
                                               age_text(now - _transcripts.parse_ts(m['last_success'])))
                            for m in stale)
        notes.append({'rule': 'sync_stale', 'machines': [{'name': m['name'], 'last_success': m['last_success']}
                                                         for m in stale],
                      'text': f'no push for over 24 h: {listing}'})
    if state.blocked:
        notes.append({'rule': 'sync_backoff', 'category': 'visibility', 'next_due': None,
                      'text': 'sync of this machine blocked: the sync repository is public (visibility); '
                              'run tools/install.py with --sync-repo again'})
    elif state.in_backoff(now):
        notes.append({'rule': 'sync_backoff', 'category': state.last_error, 'next_due': iso(state.next_due),
                      'text': f'sync of this machine backs off after an error ({state.last_error}), next push '
                              f'{clock(state.next_due, True)}'})
    pull = sync['pull']
    if pull['status'] in ('failed', 'diverged'):
        last = 'not pulled yet' if pull['last_pull'] is None else \
            f'as of {age_text(now - _transcripts.parse_ts(pull["last_pull"]))}'
        cause = (f'pull failed ({pull["category"]}: {pull["detail"]})' if pull['status'] == 'failed'
                 else f'the sync history could not be brought together ({pull["detail"]}), nothing was merged')
        notes.append({'rule': 'sync_pull', 'status': pull['status'], 'category': pull['category'],
                      'text': f'{cause}: the other machines {last}'})
    return notes


def choose_machine(parser, machines: Machines, name: str) -> str:
    """The key of the machine `--machine` names (its name, or the start of it); exit 2 if none or several fit."""
    if not name:
        parser.error('--machine must not be empty')
    names = machines.names
    found = [key for key in names if names[key] == name] or [key for key in names if names[key].startswith(name)]
    if len(found) != 1:
        listing = ', '.join(sorted(names.values()))
        parser.error(f"--machine '{name}' matches {'no machine' if not found else f'{len(found)} machines'} "
                     f'(machines: {listing})')
    return found[0]


def gather_sync(parser, args, log_dir: str, cfg: dict, now: float) -> tuple[Machines, str | None, dict, list[dict]]:
    """(machines, the key of --machine, coverage.sync, the notable lines of the sync): the push of --sync, the pull,
    then the clone as it is now. A machine id that cannot be used (a link, not a UUID4) leaves the clone unread: its
    own folder could not be told from the others. Nothing here fails the report."""
    push = sync_push(log_dir, cfg, now) if args.sync else None
    pull = sync_pull(log_dir, cfg, now, wanted=not args.no_pull, forced=args.sync)
    state = _state(log_dir)
    try:
        own, usable = sync_format.load_machine_id(log_dir, create=False) or '', True
    except sync_format.FormatError:
        own, usable = '', False
    configured = cfg['sync']['machine_name']
    name = sync_format.display_name(own, configured) if own else configured or 'this machine'
    machines = Machines(own, machine_name(name), state.last_success)
    root = _sync_git.clone_dir(log_dir)
    if usable and os.path.isdir(root):
        read_clone(root, machines)
    chosen = None if args.machine is None else choose_machine(parser, machines, args.machine)
    sync = sync_coverage(machines, pull, push, state, cfg, chosen, now)
    return machines, chosen, sync, sync_notes(sync, state, now)


def day_lines(machines: Machines, archive: dict[str, dict], chosen: str | None) -> dict[str, list[tuple[str, dict]]]:
    """The day lines by day, as (machine, figures): this machine's archive first, then the others by id; with
    --machine only that one's."""
    lines: dict[str, list[tuple[str, dict]]] = {}
    sources = [(machines.own, archive)] + sorted(machines.days.items())
    for key, days in sources:
        if chosen is None or key == chosen:
            for day, stats in days.items():
                lines.setdefault(day, []).append((key, stats))
    return lines


def _usage_only(stats: dict) -> dict:
    """A day line without its subscription figures, which another line of the day gives: the usage counts, the CLIs
    without points (a day line shares the whole rise among the CLIs of its own machine)."""
    figures = {key: value for key, value in stats.items() if key not in SUBSCRIPTION_KEYS}
    figures['clis'] = {ident: {**cli, 'five_pts': 0, 'week_pts': 0, 'five_cov': 0, 'week_cov': 0}
                       for ident, cli in stats.get('clis', {}).items()}
    return figures


def _archive_days(content: str):
    """The days of the lines the archive accepts, the same lines `read_archive` takes."""
    for raw in content.splitlines():
        try:
            yield parse_archive_line(raw)
        except (ValueError, TypeError, KeyError, RecursionError):
            continue


# --- Command line -------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='usage_report.py', allow_abbrev=False,
                                     description='What was used in a day, a week or a month, in total and per CLI.')
    parser.add_argument('--day', nargs='?', const='', metavar='YYYY-MM-DD', help='one local day (default: today)')
    parser.add_argument('--week', action='store_true', help='the subscription week that ends at the weekly reset time')
    parser.add_argument('--month', nargs='?', const='', metavar='YYYY-MM', help='one calendar month (default: this one)')
    parser.add_argument('--year', nargs='?', const='', metavar='YYYY',
                        help='one calendar year, this one up to today (default: this one)')
    parser.add_argument('--since', metavar='YYYY-MM-DD', help='first day of a range of days')
    parser.add_argument('--until', metavar='YYYY-MM-DD', help='last day of the range (default: today), needs --since')
    parser.add_argument('--full', action='store_true', help='print every figure instead of the overview')
    parser.add_argument('--project', metavar='NAME', help='the blocks of --full for this project, and their sum')
    parser.add_argument('--session', metavar='ID', help='the blocks of --full for the session whose id starts with ID, '
                        'and how its context was made up after the first answer')
    parser.add_argument('--json', action='store_true', help='print the figures, overview included, as JSON')
    parser.add_argument('--archive', action='store_true',
                        help='write the finished days that are not in the archive yet, print nothing')
    parser.add_argument('--projects-dir', metavar='DIR', help=f'transcripts (default: {PROJECTS_DIR})')
    parser.add_argument('--claude-dir', metavar='DIR', help=f'the user folder of Claude Code for the estimate of the '
                        f'start (default: {CLAUDE_DIR})')
    parser.add_argument('--config', metavar='PATH', help='config.json to take log_dir from (as for log_summary.py)')
    parser.add_argument('--local', action='store_true', help='with the sync: this machine only, no pull')
    parser.add_argument('--machine', metavar='NAME', help='with the sync: the requests and sessions of one machine')
    parser.add_argument('--no-pull', action='store_true', help='with the sync: the clone as it is, no pull')
    parser.add_argument('--sync', action='store_true', help='push this machine and pull before the report')
    return parser


def load_config(config: str | None) -> dict:
    """The effective configuration, found like `log_summary.resolve_log_dir` finds the log folder: --config, else
    CCUC_CONFIG, else the configuration of the standard installation. A problem in it is not logged here (the hooks do)."""
    chosen = config or os.environ.get('CCUC_CONFIG') or os.path.expanduser(log_summary.STANDARD_CONFIG)
    with mock.patch.dict(os.environ, {'CCUC_CONFIG': chosen}):
        return _config.load(log_problems=False)


def resolve_usage_file(config: str | None) -> str:
    """The shared usage file: --config, else CCUC_USAGE_FILE, else `usage_file` of the configuration."""
    if config is None and os.environ.get('CCUC_USAGE_FILE'):
        return os.path.expanduser(os.environ['CCUC_USAGE_FILE'])
    return load_config(config)['usage_file']


def week_anchor(history: list[Measurement], usage_file: str) -> float | None:
    """A weekly reset time: the latest one in the history, else the one in the usage file."""
    recorded = next((m.week_resets for m in reversed(history) if m.week_resets is not None), None)
    return recorded if recorded is not None else usage_file_week_reset(usage_file)


def usage_file_week_reset(path: str) -> float | None:
    """`seven_day.resets_at` of the usage file, if it has one."""
    block = _usage._read_dict(path).get('seven_day')
    reset = block.get('resets_at') if isinstance(block, dict) else None
    return reset if _transcripts.is_number(reset) and 0 < reset < MAX_TIME else None


def _day_arg(parser, text: str, name: str) -> date:
    try:
        if not DAY_TEXT.fullmatch(text):
            raise ValueError(text)
        day = date.fromisoformat(text)
    except ValueError:
        parser.error(f'{name} must be a day like 2026-01-14')
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        parser.error(f'{name} must be a day from {MIN_YEAR} to {MAX_YEAR}')
    return day


def _period(parser, args, today: date, anchor: float | None, now: float) -> dict:
    """kind, label, start, end (Unix seconds) of the requested period; {'error': text} if --week has no reset time."""
    if args.week:
        resets = anchor
        if resets is None:
            return {'error': 'No weekly reset time in the measurement history or the usage file: '
                             'use --since and --until instead.'}
        while resets <= now:
            resets += WEEK_S      # the weekly windows follow each other every seven days
        return {'kind': 'week', 'label': f'week to {clock(resets, True)}', 'start': resets - WEEK_S, 'end': resets}
    if args.month is not None:
        if args.month and not (MONTH_TEXT.fullmatch(args.month) and 1 <= int(args.month[5:]) <= 12
                               and MIN_YEAR <= int(args.month[:4]) <= MAX_YEAR):
            parser.error(f'--month must be a month like 2026-01, from {MIN_YEAR} to {MAX_YEAR}')
        year, month = (int(args.month[:4]), int(args.month[5:])) if args.month else (today.year, today.month)
        first = date(year, month, 1)
        following = date(year + month // 12, month % 12 + 1, 1)
        return {'kind': 'month', 'label': f'{year:04d}-{month:02d}', 'start': day_start(first), 'end': day_start(following)}
    if args.year is not None:
        if args.year and not (YEAR_TEXT.fullmatch(args.year) and MIN_YEAR <= int(args.year) <= MAX_YEAR):
            parser.error(f'--year must be a year like 2026, from {MIN_YEAR} to {MAX_YEAR}')
        year = int(args.year) if args.year else today.year
        last = today if year == today.year else date(year, 12, 31)      # this year only up to today
        return {'kind': 'year', 'label': f'{year:04d}', 'start': day_start(date(year, 1, 1)), 'end': day_bounds(last)[1]}
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


def load_data(projects_dir: str, log_dir: str, history: list[Measurement], modified_since: float, start: float,
              anchor: float | None = None, machines: Machines | None = None, chosen: str | None = None) -> Data:
    """Requests from the transcripts; for the time before the history began, the usage lines in the transcripts stand in
    for it. With the sync (`machines`) the requests and gate decisions of the other machines join them; `chosen` is the
    machine of --machine."""
    data = Data()
    data.week_anchor = anchor
    first_history = min((m.t for m in history), default=math.inf)
    requests, fresh = [], {}
    if os.path.isdir(projects_dir):
        requests, fresh, data.files, data.unreadable_requests = _transcripts.read_transcripts(
            projects_dir, modified_since, want_usage_lines=start < first_history)
    if machines is not None:
        requests, machine_of, labels = machines.merge_requests(requests)
    data.set_requests(requests)
    older = [Measurement(ts, five, week, None, None) for ts, (five, week) in fresh.items() if ts < first_history]
    data.set_measurements(history + older)
    data.guards = read_guard_log(log_dir)
    if machines is not None:
        for session, label in labels.items():
            data.label.setdefault(session, label)
        for session, key in machine_of.items():
            data.machine.setdefault(session[:8], key)
        data.only = chosen
        data.guards = (data.guards if chosen in (None, machines.own) else []) + [
            guard for key, guards in sorted(machines.guards.items()) if chosen in (None, key) for guard in guards]
    return data


def main(argv: list[str] | None = None, now: float | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    now = time.time() if now is None else now
    given = (('--day', args.day is not None), ('--week', args.week), ('--month', args.month is not None),
             ('--year', args.year is not None), ('--since', args.since is not None))
    chosen = [name for name, present in given if present]
    if len(chosen) > 1:
        parser.error(f'choose one of {", ".join(chosen)}')
    if args.until is not None and args.since is None:
        parser.error('--until needs --since')
    selecting = args.project is not None or args.session is not None
    if args.archive and (chosen or args.until is not None or args.json or args.full or selecting):
        parser.error('--archive takes no period, no --json, no --full, no --project and no --session')
    if args.json and (args.full or selecting):
        parser.error('--json prints all projects and sessions already (each CLI has its project): '
                     '--full, --project and --session are for the text report')
    syncing = args.machine is not None or args.no_pull or args.sync
    if args.archive and (syncing or args.local):
        parser.error('--archive writes the archive of this machine: no --local, --machine, --no-pull or --sync')
    if args.local and syncing:
        parser.error('--local reads this machine only, without a pull: no --machine, --no-pull or --sync')
    if args.sync and args.no_pull:
        parser.error('--sync pulls: not with --no-pull')
    config = os.path.expanduser(args.config) if args.config else None
    if config is not None and not os.path.isfile(config):
        parser.error(f'--config: not a file: {args.config}')
    settings = load_config(config)
    synced = settings['sync']['mode'] != 'off' and not args.local and not args.archive
    if not synced and (args.machine is not None or args.sync):
        parser.error('--machine and --sync need the sync: sync.mode shadow or on in config.json')
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
    machines = chosen = sync = None
    notes: list[dict] = []
    if synced:
        machines, chosen, sync, notes = gather_sync(parser, args, log_dir, settings, now)
        history = merge_measurements(history + machines.history)
    anchor = week_anchor(history, resolve_usage_file(config))
    if args.archive:
        period = {'kind': 'archive', 'start': 0.0, 'end': math.inf}
    else:
        period = _period(parser, args, today, anchor, now)
    if 'error' in period:
        print(period['error'])
        return 1
    start, end = period['start'], period['end']
    data = load_data(projects_dir, log_dir, history, start - MAX_GAP_S, start, anchor, machines, chosen)
    if args.archive:
        return archive_finished_days(data, prices, archive, log_dir, projects_dir, today)

    if machines is None:
        lines = archive
        archived = [d for d in _whole_days(start, end) if d < today and d.isoformat() in archive]
    else:       # a day line counts only for a day without requests on any machine: never added to them
        busy = {local_day(ts) for ts in data.times}
        by_day = day_lines(machines, archive, chosen)
        archived = [d for d in _whole_days(start, end) if d < today and d not in busy and d.isoformat() in by_day]
        lines = {d.isoformat(): machines.combine_day(by_day[d.isoformat()], data.machine) for d in archived}
        sync['unreadable_day_lines'] = machines.broken_days      # with the lines that did not add up
    if not data.files and not archived and not os.path.isdir(projects_dir):
        print(f'No transcripts found at {projects_dir} - pass --projects-dir.')
        return 1
    while True:
        stats = compute_stats(data, prices, _cut(start, end, archived))
        used, unsplit, unfit = [], [], None
        for day in archived:
            try:
                merge_stats(stats, lines[day.isoformat()])
            except (TypeError, ValueError, KeyError, AttributeError):
                unfit = day       # does not fit the live figures: all is computed again with this day from the transcripts
                break
            if 'starts' not in lines[day.isoformat()]:      # archived by an earlier version: from the transcripts
                stats['starts'] += _starts_between(data, prices, *day_bounds(day))
            if 'points' not in lines[day.isoformat()] and not _split_from_transcripts(stats, data, prices, day, lines):
                unsplit.append(day.isoformat())
            used.append(day.isoformat())
        if unfit is None:
            break
        archived.remove(unfit)
        broken_archive += 1
    contexts, broken_contexts = read_contexts(log_dir, set(stats['clis']))
    coverage = {'transcript_files': data.files, 'archive_days': used, 'archive_days_without_agent_split': unsplit,
                'unreadable_history_lines': broken_history,
                'unreadable_archive_lines': broken_archive, 'unreadable_transcript_lines': data.unreadable_requests,
                'unreadable_context_files': broken_contexts}
    if sync is not None:
        coverage['sync'] = sync
    shown = {'kind': period['kind'], 'label': period['label'], 'start': iso(start), 'end': iso(end)}
    folder = estimate_folder(data, start, end) if args.json or args.full else None
    estimate = None if folder is None else estimate_start(
        folder[0], folder[1], os.path.expanduser(args.claude_dir or CLAUDE_DIR), projects_dir,
        settings['start_estimate']['chars_per_token'], sorted({s['type'] for s in stats['starts'] if s['type']}))
    names = None if machines is None else {ident: machines.names[key] for ident, key in data.machine.items()}
    report = build_report(stats, shown, coverage, settings['thresholds']['start_block_5h'], estimate, contexts, names,
                          notes)
    if args.json:
        print(json.dumps(report, indent=2))
    elif selecting:
        if args.full:
            parser.error('--full shows everything; --project and --session already show the full blocks of their sessions')
        try:
            print(render(report, select_clis(report, stats, args.project, args.session)))
        except SelectionError as error:
            parser.error(str(error))
    else:
        print(render(report) if args.full else render_overview(report))
    return 0


def _split_from_transcripts(stats: dict, data: Data, prices: list[dict], day: date, archive: dict) -> bool:
    """Adds the points by agent of an archived day that has none (archived by an earlier version), computed from the
    transcripts as long as they exist. False if they do not hold all that the archive says was attributed."""
    points = compute_stats(data, prices, [day_bounds(day)])['points']
    _add_counts(stats['points'], points)
    attributed = archive[day.isoformat()].get('alloc', {})
    return all(points['main'][metric] + sum(kind[metric] for kind in points['types'].values())
               >= attributed.get(metric, {}).get('attributed', 0) - 0.005 for metric in METRICS)


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
