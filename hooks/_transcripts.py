"""Reading the Claude Code transcripts: the requests and their token counts, by one set of rules for every tool.

Transcripts are `<projects>/<project>/<session>.jsonl` and `<project>/<session>/subagents/agent-*.jsonl`; links are not
followed. Per assistant line only `timestamp`, `cwd`, `requestId`, `message.id`, `message.model` and `message.usage`
are read, never text. Of the lines with one `(message.id, requestId)` the last counts (it has the complete output
tokens). A subagent belongs to the session whose folder it is in; its type comes from `agent-*.meta.json`. The first
request of a transcript is a start. A line that has the shape of a request but cannot be one is counted as
unreadable. Besides the requests, the usage line of the usage clock (in an attachment a hook added) can stand in for a
measurement. Standard library only; this module imports no other module of the tool.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from typing import NamedTuple

KINDS = ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'output')   # also the order of the prices
MAX_COUNT = 10 ** 12        # a larger token count or percentage is not a measurement: the line is not read
FRESH_MAX_MIN = 1           # a usage line counts as a measurement only if it says "measured 1 min ago" at most
NUMBER = r'(\d+(?:\.\d+)?)'
USAGE_LINES = (re.compile(rf'Usage: 5h {NUMBER} %[^·]*· week {NUMBER} %[^·]*· measured (\d+) min ago'),
               re.compile(rf'Nutzung: 5h {NUMBER} %[^·]*· 7T {NUMBER} %[^·]*· Messung vor (\d+) min'))
AGENT_TYPE_KEYS = ('spawnedAgentType', 'customAgentType', 'agentType')     # the first one present is the type
AGENT_TYPE = re.compile(r'[A-Za-z0-9_.:-]{1,64}')


def is_number(value) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


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


def parse_ts(text) -> float | None:
    moment = parse_time(text)
    return moment.timestamp() if moment else None


class BadLine(Exception):
    """A transcript line that has the shape of a request but cannot be one (a wrong type, an impossible number)."""


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
    source: str = ''               # the transcript the request stands in (never shown): its first request is a start


def _count(value) -> int:
    if is_number(value) and value > MAX_COUNT:
        raise BadLine('count')
    return int(value) if is_number(value) and value > 0 else 0


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
    if is_number(value) and value > MAX_COUNT:
        raise BadLine('count')
    return int(value) if is_number(value) and value >= 0 else None


def usage_details(usage: dict, output: int) -> tuple:
    """(thinking tokens, web searches, web fetches); None for what the line does not say. Thinking is part of the output."""
    details = usage.get('output_tokens_details') if isinstance(usage.get('output_tokens_details'), dict) else {}
    tools = usage.get('server_tool_use') if isinstance(usage.get('server_tool_use'), dict) else {}
    thinking = _known(details.get('thinking_tokens'))
    return (None if thinking is None else min(thinking, output), _known(tools.get('web_search_requests')),
            _known(tools.get('web_fetch_requests')))


def parse_request(raw: str) -> tuple | None:
    """(message id, request id, time, tokens, model, cwd, details) of an assistant line with usage numbers, else None.
    Raises BadLine for a line that is one but has a request id of the wrong type or an impossible number."""
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
    if line.get('requestId') is not None and not isinstance(line['requestId'], str):
        raise BadLine('requestId')
    return message.get('id'), line.get('requestId'), ts, tokens, model, cwd, usage_details(usage, tokens[4])


def parse_usage_line(raw: str) -> tuple | None:
    """(time, five, week) of a fresh usage line of this tool in an attachment, else None."""
    try:
        line = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    attachment = line.get('attachment') if isinstance(line, dict) and line.get('type') == 'attachment' else None
    if not isinstance(attachment, dict) or attachment.get('type') != 'hook_additional_context':
        return None       # only what a hook added to the prompt: a file shown in the transcript may quote the line
    ts = parse_ts(line.get('timestamp'))
    if ts is None:
        return None
    text = json.dumps(attachment, ensure_ascii=False)
    found = next((match for match in (pattern.search(text) for pattern in USAGE_LINES) if match), None)
    if found is None or float(found.group(3)) > FRESH_MAX_MIN:
        return None
    return ts, float(found.group(1)), float(found.group(2))


def agent_type_of(transcript: str) -> str:
    """The type of a subagent from agent-<id>.meta.json next to its transcript: `spawnedAgentType`, else
    `customAgentType`, else `agentType`, which for a named subagent is only its name: then the type is unknown."""
    try:
        with open(transcript[:-len('.jsonl')] + '.meta.json', encoding='utf-8') as f:
            data = json.loads(f.read(65536))
    except (OSError, ValueError, RecursionError):
        return 'unknown'
    data = data if isinstance(data, dict) else {}
    key = next((key for key in AGENT_TYPE_KEYS if data.get(key) is not None), None)
    value = data[key] if key else None
    if value is None or (key == 'agentType' and value == data.get('name')):
        return 'unknown'      # without a spawned or custom type, agentType of a named subagent is only its name
    return value if isinstance(value, str) and AGENT_TYPE.fullmatch(value) else 'other'


def folder_of(cwd: str) -> str:
    """The name of the folder a request ran in: its project. Never a path."""
    return os.path.basename(cwd.rstrip('/'))


def replaces(known: tuple | None, output: int, path: str) -> bool:
    """Whether a line of a request with `output` tokens in transcript `path` replaces the line known so far, given as
    (output, path, ...): one with more output, or with the same output later in the same transcript."""
    return known is None or output > known[0] or (output == known[0] and known[1] == path)


def find_transcripts(projects_dir: str):
    """(path, session, is_subagent) of every transcript below the folder, links not followed."""
    for folder, dirs, files in os.walk(projects_dir):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(folder, name)
            parts = os.path.relpath(path, projects_dir).split(os.sep)
            if name.endswith('.jsonl') and len(parts) >= 2 and not os.path.islink(path):
                yield path, parts[1][:-len('.jsonl')] if len(parts) == 2 else parts[1], len(parts) > 2


def read_transcripts(projects_dir: str, modified_since: float,
                     want_usage_lines: bool) -> tuple[list[Request], dict, int, int]:
    """(requests, usage-line measurements by time, files read, unreadable request lines). Of the lines of one request (same
    message id and request id) only the last one counts: it carries the complete output tokens. A request that stands in
    two transcripts (a continued session) is counted once and belongs to the file modified first (then the one that
    sorts first). Files not modified since `modified_since` cannot hold anything newer and are not opened."""
    candidates = []
    for path, session, sub in find_transcripts(projects_dir):
        try:
            modified = os.stat(path).st_mtime
        except OSError:
            continue
        if modified >= modified_since:
            candidates.append((modified, path, session, sub))
    best: dict = {}
    fresh: dict[float, tuple] = {}
    files = unreadable = 0
    for _modified, path, session, sub in sorted(candidates):
        try:
            handle = open(path, encoding='utf-8', errors='replace')
        except OSError:
            continue
        files += 1
        agent_type = agent_type_of(path) if sub else None
        with handle:
            for number, raw in enumerate(handle):
                if '"usage"' in raw:
                    try:
                        parsed = parse_request(raw)
                    except BadLine:
                        unreadable += 1
                        continue
                    if parsed is not None:
                        message_id, request_id, ts, tokens, model, cwd, details = parsed
                        key = (message_id, request_id) if isinstance(message_id, str) and message_id else (path, number)
                        if replaces(best.get(key), tokens[4], path):
                            request = Request(ts, session, sub, agent_type, model, tokens, cwd, *details, source=path)
                            best[key] = (tokens[4], path, request)
                if want_usage_lines and ('Usage: 5h' in raw or 'Nutzung: 5h' in raw) and '"attachment"' in raw:
                    parsed = parse_usage_line(raw)
                    if parsed is not None:
                        fresh[parsed[0]] = parsed[1:]
    return [value[2] for value in best.values()], fresh, files, unreadable


def read_requests_from(path: str, offset: int, session: str, sub: bool) -> tuple[dict, int, int]:
    """The requests in the complete lines of one transcript from byte `offset` on: ({key: request}, end, unreadable
    request lines), `end` being the byte after the last line break read. A last line without its line break is still
    being written: it is left for a later call from `end`. Of the lines of one request the last counts (`replaces`);
    the key is (message id, request id), for a line without a message id (None, its byte offset): each such line is
    a request of its own. The requests keep the order in which they first appear. Raises OSError if the file cannot
    be read."""
    best: dict = {}
    unreadable = 0
    agent_type = agent_type_of(path) if sub else None
    with open(path, 'rb') as handle:
        handle.seek(offset)
        position = offset
        for raw in handle:
            if not raw.endswith(b'\n'):
                break
            start, position = position, position + len(raw)
            if b'"usage"' not in raw:
                continue
            try:
                parsed = parse_request(raw.decode('utf-8', errors='replace'))
            except BadLine:
                unreadable += 1
                continue
            if parsed is not None:
                message_id, request_id, ts, tokens, model, cwd, details = parsed
                key = (message_id, request_id) if isinstance(message_id, str) and message_id else (None, start)
                if replaces(best.get(key), tokens[4], path):
                    request = Request(ts, session, sub, agent_type, model, tokens, cwd, *details, source=path)
                    best[key] = (tokens[4], path, request)
    return {key: value[2] for key, value in best.items()}, position, unreadable


def start_indices(requests: list[Request]) -> list[int]:
    """The index of the first request of every transcript in `requests` (sorted by time), in that order: the start of
    a session or subagent. After the deduplication, so a request that stands in two transcripts starts the one that
    owns it."""
    first_of: dict[str, int] = {}
    for index, request in enumerate(requests):
        first_of.setdefault(request.source, index)
    return list(first_of.values())
