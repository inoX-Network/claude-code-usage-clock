"""The sync repository: its layout and line formats, the contract between the export (sync.py) and the report.

Below the root of a checked-out sync repository every machine has one folder of its own:

    machines/<machine_id>/
      machine.json                              written once: v, name, platform, schema, created
      status.json                               the only file that is overwritten, by its own machine only
      requests/<YYYY-MM>/<session_id>.jsonl     one line per request (month of `t`, UTC)
      history/<YYYY-MM>.jsonl                   the measurement history (month of `t`, UTC)
      gates/<YYYY-MM>.jsonl                     the decisions of the two gates (month of `t`, UTC)
      days/<YYYY>.jsonl                         the lines of the day archive, as they are (year of `day`)

No file name has a colon; times are in the lines only. Every `.jsonl` file is UTF-8, one record per line, appended
to only; the one exception is `compact`, which removes old request months of the own machine.

Writing is an allowlist: `encode` keeps the fields of the format and drops every other one, and checks each value
(a value that does not fit raises `FormatError`, category `internal`). No prompt, message text, command, path, file
name, host name, raw message or request id and no content of CLAUDE.md, skills or agents can pass.

Reading treats everything below `machines/` as foreign data: only regular files (checked with `lstat`, links are
skipped), at most `MAX_FILE_BYTES` per file, only names of the layout, never a path outside the root. A line of
another version or one that does not fit is skipped. Everything skipped is counted. Nothing read is ever run.

Pure file work: no git, no network. Committing is the job of sync.py.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Iterable

import _plat

VERSION = 1                 # `v` of every line and file of the format
SCHEMA = 1                  # `schema` in machine.json and status.json: the version of the line formats a machine writes
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_COUNT = 10 ** 12        # a larger token count or percentage is no measurement (as in the usage report)
MAX_TIME = 4_102_444_800    # Unix seconds of 2100-01-01: a later reset time is no measurement
MACHINES = 'machines'
ID_FILE = os.path.join('sync', 'machine_id')      # below the log folder

# ASCII digits only: \d would also take digits of other scripts, and these become file names
MACHINE_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}')
SESSION_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
MONTH = re.compile(r'[0-9]{4}-(?:0[1-9]|1[0-2])')
YEAR = re.compile(r'[0-9]{4}')
DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
TIME = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3}|\.[0-9]{6})?Z')
KEY = re.compile(r'[0-9a-f]{16}')
AGENT_TYPE = re.compile(r'[A-Za-z0-9_.:-]{1,64}')         # as the usage report accepts an agent type
MODEL_NAME = re.compile(r'[A-Za-z0-9_.:/-]{1,64}')        # as the usage report shows a model id
LABEL = re.compile(r'[\w .+@=?-]{1,64}')
LABEL_UNSAFE = re.compile(r'[^\w .+@=-]')                  # the characters the usage report keeps in a folder name
NAME = re.compile(r'[\w .+@=-]{1,64}')
WORD = re.compile(r'[a-z_]{1,20}')
TOOL_VERSION = re.compile(r'[0-9A-Za-z.+-]{1,32}')
REASON = re.compile(r'[\w .,…()/+%?-]{0,60}')
PLATFORMS = ('linux', 'darwin', 'windows')
GATES = ('spawn_gate', 'soft_stop')
LABEL_MODES = ('plain', 'alias', 'hash')

# The reason of a gate line, made like the kind of a reason in the usage report (reason_kind), numbers kept
REASON_LENGTH = 60
PERCENT = re.compile(r'\d+(?:\.\d+)? ?%')
REASON_CHARS = re.compile(r'[^\w .,…()/+%-]')
QUOTED = re.compile('"[^"]*"|\'[^\']*\'|„[^“”]*[“”]|`[^`]*`')
WEEK_QUESTION = re.compile(r'(?:WEEK|WOCHE) \d+(?:\.\d+)? % · ')     # the ask question names the agent: never synced
WEEK_KIND = '7-day window at N %'

COUNTERS = ('symlinks', 'oversized', 'irregular', 'bad_names', 'unreadable', 'unknown_version', 'broken_lines')


class FormatError(ValueError):
    """A value the format does not allow, or a setting that makes the export impossible. The message names the field,
    never its value. `category` is the error category of the sync state."""
    category = 'internal'


class UnknownVersion(FormatError):
    """A line or file with a `v` this version does not know."""


# --- Times --------------------------------------------------------------------------------------------------------

def iso_time(ts: float) -> str:
    """Unix seconds as the format writes a time: UTC, with milliseconds if there are any, ending in `Z`."""
    seconds, millis = divmod(int(round(ts * 1000)), 1000)
    text = datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    return f'{text}.{millis:03d}Z' if millis else text + 'Z'


def parse_time(text: object) -> float:
    """Unix seconds of a time of the format; FormatError for anything else."""
    if not isinstance(text, str) or not TIME.fullmatch(text):
        raise FormatError('time: not an ISO time in UTC')
    try:
        return datetime.fromisoformat(text[:-1] + '+00:00').timestamp()
    except ValueError as error:
        raise FormatError('time: no such date') from error


def local_day(text: str) -> str:
    """The local day (`YYYY-MM-DD`) of a time of the format: the day archive is kept in local days."""
    return datetime.fromtimestamp(parse_time(text)).date().isoformat()


# --- Values -------------------------------------------------------------------------------------------------------

Check = Callable[[object, str], object]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(pattern: re.Pattern) -> Check:
    def check(value: object, name: str) -> object:
        if isinstance(value, str) and pattern.fullmatch(value):
            return value
        raise FormatError(f'{name}: not allowed')
    return check


def _time(value: object, name: str) -> object:
    try:
        parse_time(value)
    except FormatError as error:
        raise FormatError(f'{name}: not an ISO time in UTC') from error
    return value


def _count(value: object, name: str) -> object:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_COUNT:
        return value
    raise FormatError(f'{name}: not a count')


def _optional(check: Check) -> Check:
    def optional(value: object, name: str) -> object:
        return None if value is None else check(value, name)
    return optional


def _number(maximum: float) -> Check:
    def check(value: object, name: str) -> object:
        if value is None or (_is_number(value) and math.isfinite(value) and abs(value) <= maximum):
            return value
        raise FormatError(f'{name}: not a number')
    return check


def _flag(value: object, name: str) -> object:
    if isinstance(value, bool):
        return value
    raise FormatError(f'{name}: not true or false')


def _model(value: object, name: str) -> object:
    """A model id as the usage report shows it; anything else (`<synthetic>`, text) becomes `?`."""
    return value if isinstance(value, str) and MODEL_NAME.fullmatch(value) else '?'


def _schema(value: object, name: str) -> object:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    raise FormatError(f'{name}: not a schema number')


def _choice(allowed: tuple) -> Check:
    def check(value: object, name: str) -> object:
        if value in allowed:
            return value
        raise FormatError(f'{name}: not allowed')
    return check


# The fields of each kind besides `v`, in the order they are written. A field of OPTIONAL may be left out (null).
FIELDS: dict[str, dict[str, Check]] = {
    'request': {
        't': _time, 'k': _text(KEY), 'sid': _text(SESSION_ID), 'sub': _optional(_text(AGENT_TYPE)),
        'proj': _text(LABEL), 'model': _model, 'in': _count, 'cw5': _count, 'cw1h': _count, 'cr': _count,
        'out': _count, 'think': _optional(_count), 'ws': _optional(_count), 'wf': _optional(_count), 'first': _flag,
    },
    'history': {
        't': _time, 'five': _number(MAX_COUNT), 'week': _number(MAX_COUNT), 'five_resets': _number(MAX_TIME),
        'week_resets': _number(MAX_TIME),
    },
    'gate': {
        't': _time, 'hook': _choice(GATES), 'decision': _text(WORD), 'mode': _text(WORD), 'reason': _text(REASON),
    },
    'machine': {
        'name': _text(NAME), 'platform': _choice(PLATFORMS), 'schema': _schema, 'created': _time,
    },
    'status': {
        'last_success': _time, 'tool_version': _text(TOOL_VERSION), 'schema': _schema,
    },
}
OPTIONAL = {'request': ('sub', 'think', 'ws', 'wf')}
DAY_FIELDS = ('v', 'day', 'stats')


def _clean(kind: str, values: object) -> dict:
    """The record of `kind` made of the allowed fields of `values`; every other field is dropped."""
    if not isinstance(values, dict):
        raise FormatError(f'{kind}: not an object')
    record: dict = {'v': VERSION}
    for name, check in FIELDS[kind].items():
        if name not in values and name not in OPTIONAL.get(kind, ()):
            raise FormatError(f'{kind}.{name}: missing')
        record[name] = check(values.get(name), f'{kind}.{name}')
    return record


def _clean_day(values: dict) -> dict:
    day, stats = values.get('day'), values.get('stats')
    if not isinstance(day, str) or not DAY.fullmatch(day):
        raise FormatError('day.day: not a day')
    try:
        date.fromisoformat(day)
    except ValueError as error:
        raise FormatError('day.day: no such date') from error
    if not isinstance(stats, dict):
        raise FormatError('day.stats: not an object')
    return {'v': VERSION, 'day': day, 'stats': stats}


def _dump(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def encode(kind: str, values: dict) -> str:
    """One record of `kind` (`request`, `history`, `gate`, `machine`, `status`) as text: a line ending in a line
    break. Only the fields of the format, each checked; `v` is set here. FormatError for a missing or wrong value."""
    return _dump(_clean(kind, values)) + '\n'


def encode_day(raw: str) -> str:
    """A line of the day archive (usage-days.jsonl) for `days/`: the line as it is if it has only `v`, `day` and
    `stats`, else written again with only those three. FormatError if it is no archive line of version 1."""
    try:
        values = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise FormatError('day: not JSON') from error
    if not isinstance(values, dict) or values.get('v') != VERSION or isinstance(values.get('v'), bool):
        raise FormatError('day: not an archive line of version 1')
    record = _clean_day(values)
    text = raw.strip()
    if set(values) == set(DAY_FIELDS) and '\n' not in text and '\r' not in text:
        return text + '\n'
    return _dump(record) + '\n'


def decode(kind: str, raw: str | bytes) -> dict:
    """The record in one line or file of `kind` (also `day`), with only the fields of the format. UnknownVersion for
    another `v`, FormatError for anything that does not fit (not UTF-8, not JSON, a missing or wrong field)."""
    try:
        text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        values = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise FormatError(f'{kind}: not UTF-8 JSON') from error
    if not isinstance(values, dict):
        raise FormatError(f'{kind}: not an object')
    version = values.get('v')
    if not isinstance(version, int) or isinstance(version, bool):
        raise FormatError(f'{kind}.v: missing')
    if version != VERSION:
        raise UnknownVersion(f'{kind}.v: unknown version')
    return _clean_day(values) if kind == 'day' else _clean(kind, values)


# --- Fields made from local data ----------------------------------------------------------------------------------

def request_key(message_id: str, request_id: str) -> str:
    """`k` of a request: the first 16 hex characters of SHA-256 of message id, line break, request id."""
    return hashlib.sha256(f'{message_id}\n{request_id}'.encode('utf-8')).hexdigest()[:16]


def folder_label(name: str) -> str:
    """A folder name as the usage report shows it: characters outside its set as `?`, at most 64, `?` if empty."""
    return LABEL_UNSAFE.sub('?', name)[:64] or '?'


def project_label(folder: str, mode: str, aliases: dict, salt: str) -> str:
    """`proj` of a request after `sync.project_labels`. `plain`: the folder name as the report shows it; `alias`: its
    entry in `sync.project_aliases`, else `unbenannt-` and 8 hex characters of SHA-256 of the folder name; `hash`: `p-`
    and 12 hex characters of SHA-256 of salt, line break, folder name. `hash` without a salt raises FormatError
    (category `internal`): the export stops, it never falls back to the plain name."""
    folder = folder_label(folder)
    if mode == 'plain':
        return folder
    if mode == 'alias':
        alias = aliases.get(folder)
        if isinstance(alias, str) and alias:
            return folder_label(alias)
        return 'unbenannt-' + hashlib.sha256(folder.encode('utf-8')).hexdigest()[:8]
    if mode == 'hash':
        if not salt:
            raise FormatError('sync.label_salt: required for project_labels hash')
        return 'p-' + hashlib.sha256(f'{salt}\n{folder}'.encode('utf-8')).hexdigest()[:12]
    raise FormatError('sync.project_labels: unknown mode')


def gate_reason(reason: object) -> str:
    """The reason of a gate line: the text of the local log before its first colon, quoted parts as `…`,
    percentages as `N %`, other characters outside a small set as `?`, at most 60 characters. The question of the
    ask dialog names the agent and becomes the kind the report gives it."""
    if not isinstance(reason, str):
        return ''
    if WEEK_QUESTION.match(reason):
        return WEEK_KIND
    prefix = PERCENT.sub('N %', QUOTED.sub('…', reason.split(':', 1)[0]))
    return REASON_CHARS.sub('?', prefix.strip())[:REASON_LENGTH]


def gate_from_log(row: object) -> dict | None:
    """The fields of a gate line for one line of the local log (log.jsonl), None if it is no gate decision or has no
    time with a zone. Nothing but time, gate, decision, mode and the short reason is taken over."""
    if not isinstance(row, dict) or row.get('hook') not in GATES or not isinstance(row.get('time'), str):
        return None
    try:
        moment = datetime.fromisoformat(row['time'].replace('Z', '+00:00'))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    decision, mode = row.get('decision'), row.get('mode')
    return {'t': iso_time(moment.timestamp()), 'hook': row['hook'],
            'decision': decision if isinstance(decision, str) and WORD.fullmatch(decision) else 'other',
            'mode': mode if isinstance(mode, str) and WORD.fullmatch(mode) else 'unknown',
            'reason': gate_reason(row.get('reason'))}


def platform_name() -> str:
    """`platform` of machine.json: `windows`, `darwin` or `linux`."""
    if os.name == 'nt':
        return 'windows'
    return 'darwin' if sys.platform == 'darwin' else 'linux'


def display_name(machine_id: str, configured: str) -> str:
    """The name of a machine: `sync.machine_name`, empty gives `machine-` and the first 6 characters of the id. The host
    name is never used."""
    return configured or f'machine-{machine_id[:6]}'


# --- The machine id -----------------------------------------------------------------------------------------------

def _read_small(path: str, limit: int) -> bytes:
    """The content of a regular file that is no link, at most `limit` bytes. OSError for anything else."""
    if os.path.islink(path):      # Windows has no O_NOFOLLOW
        raise OSError(f'{path} is a link')
    descriptor = os.open(path, os.O_RDONLY | _plat.NO_FOLLOW | _plat.O_NONBLOCK | _plat.O_BINARY)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise OSError(f'{path} is no regular file of at most {limit} bytes')
        return os.read(descriptor, limit)
    finally:
        os.close(descriptor)


def load_machine_id(log_dir: str, create: bool = True) -> str | None:
    """The id of this machine from `<log_dir>/sync/machine_id`: a UUID4. Missing: a new one is made and stored (mode
    0600, folder 0700) if `create`, else None. A file that is a link, not regular or holds no UUID4 raises
    FormatError: an identity is never replaced behind the user's back."""
    path = os.path.join(log_dir, ID_FILE)
    folder = os.path.dirname(path)
    if os.path.islink(folder):
        raise FormatError('machine_id: the sync folder is a link')
    try:
        content = _read_small(path, 64)
    except FileNotFoundError:
        if not create:
            return None
        _store_machine_id(folder, path)
        content = _read_small(path, 64)
    except OSError as error:
        raise FormatError('machine_id: not a regular file') from error
    machine_id = content.decode('ascii', errors='replace').strip()
    if not MACHINE_ID.fullmatch(machine_id):
        raise FormatError('machine_id: not a UUID4')
    return machine_id


def _store_machine_id(folder: str, path: str) -> None:
    """Writes a new id to a temporary file and links it to `path`: the link fails if another process was quicker,
    and no reader ever sees a half written file."""
    os.makedirs(os.path.dirname(os.path.abspath(folder)), mode=0o700, exist_ok=True)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    tmp = f'{path}.tmp.{os.getpid()}'
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _plat.NO_FOLLOW | _plat.O_BINARY, 0o600)
    try:
        _plat.fchmod(descriptor, 0o600)
        os.write(descriptor, f'{uuid.uuid4()}\n'.encode('ascii'))
    finally:
        os.close(descriptor)
    try:
        os.link(tmp, path)
    except FileExistsError:
        pass
    finally:
        os.unlink(tmp)


# --- Paths --------------------------------------------------------------------------------------------------------

def _id(machine_id: object) -> str:
    if isinstance(machine_id, str) and MACHINE_ID.fullmatch(machine_id):
        return machine_id
    raise FormatError('machine_id: not a UUID4')


def machine_dir(machine_id: str) -> str:
    """`machines/<machine_id>`, relative to the root of the repository, with `/`."""
    return f'{MACHINES}/{_id(machine_id)}'


def machine_path(machine_id: str) -> str:
    return f'{machine_dir(machine_id)}/machine.json'


def status_path(machine_id: str) -> str:
    return f'{machine_dir(machine_id)}/status.json'


def request_path(machine_id: str, t: str, session_id: str) -> str:
    """The file of a request: `requests/<month of t, UTC>/<session_id>.jsonl`."""
    parse_time(t)
    _text(SESSION_ID)(session_id, 'request.sid')
    return f'{machine_dir(machine_id)}/requests/{t[:7]}/{session_id}.jsonl'


def history_path(machine_id: str, t: str) -> str:
    parse_time(t)
    return f'{machine_dir(machine_id)}/history/{t[:7]}.jsonl'


def gate_path(machine_id: str, t: str) -> str:
    parse_time(t)
    return f'{machine_dir(machine_id)}/gates/{t[:7]}.jsonl'


def day_path(machine_id: str, day: str) -> str:
    """The file of a day line: `days/<year of day>.jsonl`."""
    _clean_day({'day': day, 'stats': {}})
    return f'{machine_dir(machine_id)}/days/{day[:4]}.jsonl'


LAYOUT_LINES = re.compile(r'machines/(?P<id>[^/]+)/(?:requests/[0-9]{4}-[0-9]{2}/[A-Za-z0-9_-]{1,128}'
                          r'|history/[0-9]{4}-[0-9]{2}|gates/[0-9]{4}-[0-9]{2}|days/[0-9]{4})\.jsonl')


# --- Writing ------------------------------------------------------------------------------------------------------

def _folder(root: str, relative: str, create: bool) -> str:
    """The folder `relative` (with `/`) below `root`. Every part must be a real folder, never a link; with `create`
    missing parts are made (0700). FormatError for a link or a file on the way, FileNotFoundError without `create`."""
    path = root
    for part in relative.split('/'):
        path = os.path.join(path, part)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
            info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):      # a link is no folder for lstat
            raise FormatError(f'{relative}: a link or a file on the way')
    return path


def _own(machine_id: str, relative: str) -> None:
    if not relative.startswith(machine_dir(machine_id) + '/') or '..' in relative.split('/'):
        raise FormatError('path: outside the own machine folder')


def append(root: str, machine_id: str, relative: str, lines: Iterable[str]) -> int:
    """Appends `lines` (as made by `encode`/`encode_day`) to the file `relative` of the own machine and returns the
    bytes written. Only paths of the layout below `machines/<machine_id>/`; never through a link; a cut-off last
    line gets its line break first, so the new lines stand alone."""
    _own(machine_id, relative)
    if not LAYOUT_LINES.fullmatch(relative):
        raise FormatError('path: not a line file of the layout')
    data = ''.join(lines).encode('utf-8')
    if not data:
        return 0
    if not data.endswith(b'\n') or b'\r' in data:
        raise FormatError('lines: each line must end with a line break')
    folder, name = relative.rsplit('/', 1)
    path = os.path.join(_folder(root, folder, True), name)
    if os.path.islink(path):      # Windows has no O_NOFOLLOW
        raise FormatError(f'{relative}: a link')
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | _plat.NO_FOLLOW | _plat.O_BINARY
    descriptor = os.open(path, flags | _plat.O_NONBLOCK, 0o600)      # a named pipe must not block the open
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise FormatError(f'{relative}: not a regular file')
        if info.st_size and _plat.pread(descriptor, 1, info.st_size - 1) != b'\n':
            data = b'\n' + data
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
    finally:
        os.close(descriptor)
    return len(data)


def _write_new(path: str, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _plat.NO_FOLLOW | _plat.O_BINARY, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
    finally:
        os.close(descriptor)


def write_machine(root: str, machine_id: str, values: dict) -> bool:
    """Creates `machine.json` of the own machine from `values` (name, platform, schema, created). An existing file is
    kept as it is (False): only status.json is ever overwritten."""
    path = os.path.join(_folder(root, machine_dir(machine_id), True), 'machine.json')
    data = encode('machine', values).encode('utf-8')
    try:
        _write_new(path, data)
    except FileExistsError:
        return False
    return True


def write_status(root: str, machine_id: str, values: dict) -> None:
    """Replaces `status.json` of the own machine with `values` (last_success, tool_version, schema), atomically: a
    temporary file next to it, then a rename. A link as status.json is replaced, never written through."""
    path = os.path.join(_folder(root, machine_dir(machine_id), True), 'status.json')
    data = encode('status', values).encode('utf-8')
    tmp = f'{path}.tmp.{os.getpid()}'
    try:
        os.unlink(tmp)      # a leftover, possibly a planted link: removed, never written through
    except FileNotFoundError:
        pass
    try:
        _write_new(tmp, data)
        _plat.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- Reading ------------------------------------------------------------------------------------------------------

@dataclass
class Machine:
    """What was read of one machine folder. Records hold only the fields of the format."""
    machine_id: str
    machine: dict | None = None
    status: dict | None = None
    requests: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    gates: list[dict] = field(default_factory=list)
    days: list[dict] = field(default_factory=list)


@dataclass
class Repo:
    """Every machine folder that was read, by id, and the counts of what was skipped (keys: COUNTERS)."""
    machines: dict[str, Machine]
    skipped: dict[str, int]


def _entries(folder: str, skipped: dict[str, int]) -> list[tuple[str, str, os.stat_result]]:
    """(name, path, lstat) of the regular files and real folders in `folder`, sorted by name. Links and anything else
    are counted and left out, and so is a folder that cannot be listed."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        skipped['unreadable'] += 1
        return []
    found = []
    for name in names:
        path = os.path.join(folder, name)
        try:
            info = os.lstat(path)
        except OSError:
            skipped['unreadable'] += 1
            continue
        if stat.S_ISLNK(info.st_mode):
            skipped['symlinks'] += 1
        elif stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
            found.append((name, path, info))
        else:
            skipped['irregular'] += 1
    return found


def _inside(root: str, path: str) -> bool:
    real_root = os.path.realpath(root)
    return os.path.commonpath([real_root, os.path.realpath(path)]) == real_root


def _file_content(root: str, path: str, info: os.stat_result, skipped: dict[str, int]) -> bytes | None:
    """The bytes of a regular file of at most MAX_FILE_BYTES inside `root`, else None (counted)."""
    if info.st_size > MAX_FILE_BYTES:
        skipped['oversized'] += 1
        return None
    if not _inside(root, path):
        skipped['bad_names'] += 1
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | _plat.NO_FOLLOW | _plat.O_NONBLOCK | _plat.O_BINARY)
    except OSError:
        skipped['unreadable'] += 1
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            skipped['irregular'] += 1      # replaced since the lstat
            return None
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_BYTES:     # grew since the lstat
                skipped['oversized'] += 1
                return None
            chunks.append(chunk)
        return b''.join(chunks)
    except OSError:
        skipped['unreadable'] += 1
        return None
    finally:
        os.close(descriptor)


def _lines(root: str, path: str, info: os.stat_result, kind: str, skipped: dict[str, int]) -> list[dict]:
    content = _file_content(root, path, info, skipped)
    records = []
    for raw in (content or b'').split(b'\n'):
        if not raw.strip():
            continue
        try:
            records.append(decode(kind, raw))
        except UnknownVersion:
            skipped['unknown_version'] += 1
        except FormatError:
            skipped['broken_lines'] += 1
    return records


def _single(root: str, path: str, info: os.stat_result, kind: str, skipped: dict[str, int]) -> dict | None:
    content = _file_content(root, path, info, skipped)
    if content is None:
        return None
    try:
        return decode(kind, content)
    except UnknownVersion:
        skipped['unknown_version'] += 1
    except FormatError:
        skipped['broken_lines'] += 1
    return None


# The folders of a machine with line files directly in them: the kind of their lines
KIND_OF = {'history': 'history', 'gates': 'gate', 'days': 'day'}


def _line_files(root: str, folder: str, pattern: re.Pattern, kind: str, skipped: dict[str, int]) -> list[dict]:
    records = []
    for name, path, info in _entries(folder, skipped):
        if not (stat.S_ISREG(info.st_mode) and name.endswith('.jsonl') and pattern.fullmatch(name[:-6])):
            skipped['bad_names'] += 1
            continue
        records += _lines(root, path, info, kind, skipped)
    return records


def _requests(root: str, folder: str, skipped: dict[str, int]) -> list[dict]:
    records = []
    for month, path, info in _entries(folder, skipped):
        if not (stat.S_ISDIR(info.st_mode) and MONTH.fullmatch(month)):
            skipped['bad_names'] += 1
            continue
        records += _line_files(root, path, SESSION_ID, 'request', skipped)
    return records


def read_machine(root: str, machine_id: str, skipped: dict[str, int]) -> Machine:
    """Everything readable in `machines/<machine_id>/`; skipped items are added to `skipped`."""
    machine = Machine(_id(machine_id))
    folder = os.path.join(root, MACHINES, machine_id)
    for name, path, info in _entries(folder, skipped):
        is_file = stat.S_ISREG(info.st_mode)
        if name in ('machine.json', 'status.json') and is_file:
            setattr(machine, name[:-5], _single(root, path, info, name[:-5], skipped))
        elif name == 'requests' and not is_file:
            machine.requests = _requests(root, path, skipped)
        elif name in KIND_OF and not is_file:
            pattern = MONTH if name != 'days' else YEAR
            setattr(machine, name, _line_files(root, path, pattern, KIND_OF[name], skipped))
        else:
            skipped['bad_names'] += 1
    return machine


def read_repo(root: str, skip: str | None = None) -> Repo:
    """Every machine folder below `<root>/machines/` except `skip` (the own id: the own machine comes from its local
    files). A folder whose name is no UUID4 is skipped and counted."""
    skipped = dict.fromkeys(COUNTERS, 0)
    machines: dict[str, Machine] = {}
    base = os.path.join(root, MACHINES)
    try:
        if not stat.S_ISDIR(os.lstat(base).st_mode):
            skipped['symlinks' if os.path.islink(base) else 'irregular'] += 1
            return Repo(machines, skipped)
    except FileNotFoundError:
        return Repo(machines, skipped)
    for name, _path, info in _entries(base, skipped):
        if not (stat.S_ISDIR(info.st_mode) and MACHINE_ID.fullmatch(name)):
            skipped['bad_names'] += 1
        elif name != skip:
            machines[name] = read_machine(root, name, skipped)
    return Repo(machines, skipped)


def latest_by_key(requests: Iterable[dict]) -> dict[str, dict]:
    """The requests by `k`; of the lines with one `k` the one read last counts."""
    latest: dict[str, dict] = {}
    for request in requests:
        latest[request['k']] = request
    return latest


# --- Compacting ---------------------------------------------------------------------------------------------------

def _month_index(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:7]) - 1


def old_months(root: str, machine_id: str, keep_months: int, now: float) -> list[str]:
    """The months of `requests/` of the own machine that are older than `keep_months` full months (by UTC)."""
    current = datetime.fromtimestamp(now, timezone.utc)
    limit = current.year * 12 + current.month - 1 - keep_months
    try:
        folder = _folder(root, machine_dir(machine_id) + '/requests', False)
    except FileNotFoundError:
        return []
    return [name for name, _path, info in _entries(folder, dict.fromkeys(COUNTERS, 0))
            if stat.S_ISDIR(info.st_mode) and MONTH.fullmatch(name) and _month_index(name) < limit]


def missing_days(root: str, machine_id: str, month: str) -> set[str] | None:
    """The local days with requests in `requests/<month>/` of the own machine that have no line in `days/`. None if
    the month cannot be judged: anything in it was skipped (a link, a file too large, a line that is broken or of
    another version), so a day could be missing unseen."""
    skipped = dict.fromkeys(COUNTERS, 0)
    base = machine_dir(machine_id)
    folder = _folder(root, f'{base}/requests/{_text(MONTH)(month, "month")}', False)
    requests = _line_files(root, folder, SESSION_ID, 'request', skipped)
    if any(skipped.values()):
        return None
    days_folder = os.path.join(root, base, 'days')
    if os.path.isdir(days_folder) and not os.path.islink(days_folder):
        days = _line_files(root, days_folder, YEAR, 'day', dict.fromkeys(COUNTERS, 0))
    else:
        days = []
    covered = {record['day'] for record in days}
    return {local_day(record['t']) for record in requests} - covered


def compactable_months(root: str, machine_id: str, keep_months: int, now: float) -> list[str]:
    """The months of `requests/` that `compact` would remove: own, older than `keep_months` full months, and with a
    day line for every day that has requests in that month."""
    return [month for month in old_months(root, machine_id, keep_months, now)
            if missing_days(root, machine_id, month) == set()]


def compact(root: str, machine_id: str, keep_months: int, now: float) -> list[str]:
    """Removes the `compactable_months` of the own machine from the checked-out repository and returns them: their
    files, then the folder. A month that holds anything but request files is never compactable (`missing_days`).
    `days/` is never touched. Committing is the job of the caller."""
    removed = compactable_months(root, machine_id, keep_months, now)
    for month in removed:
        folder = _folder(root, f'{machine_dir(machine_id)}/requests/{month}', False)
        for name in os.listdir(folder):
            os.unlink(os.path.join(folder, name))
        os.rmdir(folder)
    return removed
