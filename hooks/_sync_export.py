"""The export of the sync: what is new in the local data of this machine, appended to its folder in the sync clone.

`export` reads the local sources from where the last run stopped and appends their new lines below
`machines/<own id>/` of the checked-out sync repository, in the formats of sync_format.py (which checks every field
and lets no other one pass):

- the transcripts below the projects folder, read with _transcripts by the rules of the usage report: one line per
  request in `requests/<YYYY-MM>/<session>.jsonl` (month of its time, UTC); a request whose line changes later is
  appended again, the reader keeps the last line of a `k`
- the measurement history `usage-history.jsonl` with its rotated `.1`: `history/<YYYY-MM>.jsonl`
- the gate decisions of the log `log.jsonl` with its rotated `.1`, only with `sync.include_gates`:
  `gates/<YYYY-MM>.jsonl`
- the day archive `usage-days.jsonl`: `days/<YYYY-MM>.jsonl`, the project names after `sync.project_labels`

and machine.json (if it is missing or its name or platform changed). status.json, commit and push are the job of the
push (0034-SYNC-PUSH), which calls `export` under the sync lock; the clone is its argument.

**Cursor** `<log_dir>/sync/cursor.json` (0600, written atomically): per source file the byte offset after the last
complete line exported, its size, its id (`st_dev:st_ino`; none where the system gives no inode number), its
modification time and a hash of its first bytes. Only complete lines from the offset on are read: a half last line
waits for the next run. A file that got smaller, has another id or other first bytes (without an id: also an older
modification time) is read from byte 0 again; the reader removes the doubled requests by `k`. A rotated file is
known by its id and first bytes under its new name and read on from where its old name stopped. The cursor of a source
changes only once its lines are in the clone; a run that ends early (an error, the deadline) keeps what it finished.
A cursor that cannot be read counts as empty, with one log line.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
from collections.abc import Callable

import _context_state
import _history
import _sync_core
import _transcripts
import sync_format

CURSOR_FILE = 'cursor.json'
CURSOR_VERSION = 1
MAX_CURSOR_BYTES = 32 * 1024 * 1024
HEAD_BYTES = 1024           # the first bytes of a file whose hash tells it from another one
RESERVE_S = 30              # of the deadline of the push, kept for commit and push: the export stops at the next file
HISTORY_FILE = _history.FILE_NAME
LOG_FILE = 'log.jsonl'
ARCHIVE_FILE = 'usage-days.jsonl'
TRANSCRIPTS = 'projects/'   # the cursor key of a transcript: this and its path below the projects folder, with `/`
KINDS = ('request', 'history', 'gate', 'day')
# What a source line that does not fit raises on its way to a line of the format (OSError: a time Windows
# cannot convert)
BAD_LINE = (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError, OSError)


@dataclasses.dataclass
class Result:
    """What one run did: the lines appended to the clone by kind (KINDS) and their bytes, the source lines that could
    not be exported (counted, never fatal), the source files that could not be read (tried again next run), whether
    machine.json was written, and whether every source was read (False: the deadline came first)."""
    lines: dict = dataclasses.field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    bytes: int = 0
    skipped: int = 0
    unreadable_files: int = 0
    machine_written: bool = False
    complete: bool = True


# --- The cursor -------------------------------------------------------------------------------------------------------

def _valid_entry(entry: object) -> bool:
    def count(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return (isinstance(entry, dict) and count(entry.get('o')) and count(entry.get('s')) and count(entry.get('m'))
            and isinstance(entry.get('h'), str)
            and all(isinstance(entry.get(name), (str, type(None))) for name in ('id', 'first', 'folder')))


def read_cursor(directory: str, log_dir: str) -> dict:
    """The entries of `<directory>/cursor.json` by source; none if there is no file. A file that cannot be used counts
    as empty (one log line), an entry that does not fit is left out: its file is read from byte 0 again."""
    try:
        data = json.loads(_sync_core._read_small(os.path.join(directory, CURSOR_FILE), MAX_CURSOR_BYTES))
        if not isinstance(data, dict) or data.get('v') != CURSOR_VERSION or not isinstance(data.get('files'), dict):
            raise ValueError('not a cursor of version 1')
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as error:
        _sync_core.log(log_dir, 'cursor_unreadable', f'{CURSOR_FILE} counts as empty: {type(error).__name__}')
        return {}
    return {key: entry for key, entry in data['files'].items() if _valid_entry(entry)}


def write_cursor(directory: str, files: dict) -> None:
    """Writes the cursor atomically, 0600, never through a link. SyncError `internal` if it cannot be written."""
    text = json.dumps({'v': CURSOR_VERSION, 'files': files}, separators=(',', ':'), sort_keys=True)
    try:
        if not _context_state.write_private(directory, CURSOR_FILE, text):
            raise OSError('the sync folder is a link')
    except OSError as error:
        raise _sync_core.SyncError('internal', f'{CURSOR_FILE} cannot be written ({type(error).__name__})') from error


def file_id(info: os.stat_result) -> str | None:
    """`st_dev:st_ino`; None where the system gives no inode number (0): then size, first bytes and modification time
    decide whether it is still the same file."""
    return f'{info.st_dev}:{info.st_ino}' if info.st_ino else None


def _head(path: str, length: int) -> str:
    with open(path, 'rb') as handle:
        return hashlib.sha256(handle.read(length)).hexdigest()[:16]


def unchanged(entry: dict | None, info: os.stat_result) -> bool:
    """The file is as the entry left it: nothing new to read, it need not even be opened."""
    return (entry is not None and file_id(info) == entry['id'] and info.st_size == entry['s']
            and info.st_mtime_ns == entry['m'])


def resume_at(entry: dict | None, info: os.stat_result, path: str) -> int:
    """The offset of `entry` if it describes this file, else 0. Another file: another id (where both have one), a
    smaller size, other first bytes, or without an id a modification time older than the entry's."""
    if entry is None:
        return 0
    known, current = entry['id'], file_id(info)
    if known and current:
        if known != current:
            return 0
    elif info.st_mtime_ns < entry['m']:
        return 0
    if info.st_size < entry['s'] or _head(path, min(HEAD_BYTES, entry['o'])) != entry['h']:
        return 0
    return entry['o']


def _entry(info: os.stat_result, path: str, end: int, **extra: object) -> dict:
    return {'o': end, 's': max(info.st_size, end), 'id': file_id(info), 'm': info.st_mtime_ns,
            'h': _head(path, min(HEAD_BYTES, end)), **extra}


def _complete_lines(path: str, offset: int) -> tuple[list[bytes], int]:
    """The complete lines of a file from byte `offset` on and the byte after the last of them."""
    lines, end = [], offset
    with open(path, 'rb') as handle:
        handle.seek(offset)
        for raw in handle:
            if not raw.endswith(b'\n'):
                break
            lines.append(raw)
            end += len(raw)
    return lines, end


# --- Lines of the format ----------------------------------------------------------------------------------------------

def history_line(raw: bytes) -> dict:
    """The fields of a line of the measurement history; raises for a line the usage report would not read."""
    row = json.loads(raw)
    t = sync_format.iso_time(_transcripts.parse_ts(row['t']))
    return {'t': t, **{key: row.get(key) for key in _history.KEYS}}


# --- The run ----------------------------------------------------------------------------------------------------------

class _Export:
    """One run: the clone, the own id, the cursor as read (`old`) and as it becomes (`new`), what was done."""

    def __init__(self, root: str, machine_id: str, old: dict, sync: dict):
        self.root, self.machine_id, self.old, self.sync = root, machine_id, old, sync
        self.new = dict(old)
        self.result = Result()

    def label(self, folder: str) -> str:
        return sync_format.project_label(folder, self.sync['project_labels'], self.sync['project_aliases'],
                                         self.sync['label_salt'])

    def append(self, kind: str, lines: dict[str, list[str]]) -> None:
        """Appends the lines, by file of the clone. A file that cannot be written ends the run (`internal`)."""
        for relative, texts in lines.items():
            try:
                self.result.bytes += sync_format.append(self.root, self.machine_id, relative, texts)
            except sync_format.FormatError as error:
                raise _sync_core.SyncError('internal', f'the clone cannot be written: {error}') from error
            except OSError as error:
                raise _sync_core.SyncError('internal',
                                           f'the clone cannot be written ({type(error).__name__})') from error
            self.result.lines[kind] += len(texts)

    def source_info(self, path: str) -> os.stat_result | None:
        """lstat of a source file that is a regular file; None (counted) if it is anything else or cannot be read."""
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError:
            self.result.unreadable_files += 1
            return None
        if not stat.S_ISREG(info.st_mode):
            self.result.unreadable_files += 1
            return None
        return info

    # --- Transcripts

    def transcript(self, key: str, path: str, session: str, sub: bool, main_key: str) -> None:
        info = self.source_info(path)
        entry = self.old.get(key)
        if info is None or unchanged(entry, info):
            return
        try:
            offset = resume_at(entry, info, path)
            requests, end, unreadable = _transcripts.read_requests_from(path, offset, session, sub)
            done = _entry(info, path, end)
        except OSError:
            self.result.unreadable_files += 1
            return
        self.result.skipped += unreadable
        first = entry.get('first') if offset and entry else None
        folder = entry.get('folder') if offset and entry else None
        if folder is None:
            folder = next((_transcripts.folder_of(r.cwd) for r in requests.values() if r.cwd), None)
        main = self.new.get(main_key) if sub else None      # a subagent has the project of its session
        label = self.label((main or {}).get('folder') or folder or '')
        lines: dict[str, list[str]] = {}
        for (message_id, request_id), request in requests.items():
            if message_id is None:      # no message id: the line is a request of its own, known by its place
                k = sync_format.request_key(key, str(request_id))
            else:
                k = sync_format.request_key(message_id, request_id or '')
            first = first or k
            try:
                t = sync_format.iso_time(request.ts)
                line = sync_format.encode('request', {
                    't': t, 'k': k, 'sid': session, 'sub': request.agent_type if sub else None, 'proj': label,
                    'model': request.model, 'in': request.tokens[0], 'cw5': request.tokens[1],
                    'cw1h': request.tokens[2], 'cr': request.tokens[3], 'out': request.tokens[4],
                    'think': request.thinking, 'ws': request.searches, 'wf': request.fetches, 'first': k == first})
                relative = sync_format.request_path(self.machine_id, t, session)
            except BAD_LINE:
                self.result.skipped += 1
                continue
            lines.setdefault(relative, []).append(line)
        self.append('request', lines)
        self.new[key] = dict(done, first=first, folder=folder)

    # --- Line files of the log folder

    def line_file(self, key: str, path: str, kind: str, convert: Callable[[bytes], tuple[str, str] | None],
                  earlier: tuple[str, ...] = ()) -> None:
        """The new lines of one file of the log folder. `earlier` are the keys whose entries may describe this file
        under an earlier name (a rotated file), tried before its own."""
        info = self.source_info(path)
        if info is None:
            self.new.pop(key, None)
            return
        entry = self.old.get(key)
        if unchanged(entry, info):
            return
        try:
            offset = next((found for found in (resume_at(self.old.get(name), info, path) for name in (*earlier, key))
                           if found), 0)
            raws, end = _complete_lines(path, offset)
            done = _entry(info, path, end)
        except OSError:
            self.result.unreadable_files += 1
            return
        lines: dict[str, list[str]] = {}
        for raw in raws:
            try:
                converted = convert(raw)
            except BAD_LINE:
                self.result.skipped += 1
                continue
            if converted is not None:
                lines.setdefault(converted[0], []).append(converted[1])
        self.append(kind, lines)
        self.new[key] = done

    def history(self, raw: bytes) -> tuple[str, str]:
        values = history_line(raw)
        return sync_format.history_path(self.machine_id, values['t']), sync_format.encode('history', values)

    def gate(self, raw: bytes) -> tuple[str, str] | None:
        values = sync_format.gate_from_log(json.loads(raw))
        if values is None:
            return None      # a line of another hook
        return sync_format.gate_path(self.machine_id, values['t']), sync_format.encode('gate', values)

    def day(self, raw: bytes) -> tuple[str, str]:
        line = sync_format.encode_day(raw.decode('utf-8', errors='replace'), self.sync['project_labels'],
                                      self.sync['project_aliases'], self.sync['label_salt'])
        return sync_format.day_path(self.machine_id, json.loads(line)['day']), line


def _out_of_time(deadline: _sync_core.Deadline | None) -> bool:
    return deadline is not None and deadline.left() <= RESERVE_S


def export(clone_root: str, log_dir: str, cfg: dict, projects_dir: str, now: float,
           deadline: _sync_core.Deadline | None = None) -> Result:
    """Appends the new data of this machine to `machines/<own id>/` below `clone_root` and returns what it did. The
    caller holds the sync lock. With a `deadline` the export stops at the next file once RESERVE_S are left, and
    says so (`complete`); the next run goes on from there.

    SyncError `internal` (nothing written) for `project_labels: hash` without `label_salt` or a machine id that cannot
    be used, and (what was finished is kept) for a clone or cursor that cannot be written. A source file that cannot
    be read and a line that does not fit are counted and left out."""
    sync = cfg['sync']
    try:
        sync_format.project_label('', sync['project_labels'], sync['project_aliases'], sync['label_salt'])
        machine_id = sync_format.load_machine_id(log_dir)
    except sync_format.FormatError as error:
        raise _sync_core.SyncError('internal', str(error)) from error
    directory = _sync_core.ensure_dir(log_dir)
    run = _Export(clone_root, machine_id, read_cursor(directory, log_dir), sync)
    try:
        run.result.machine_written = sync_format.write_machine(clone_root, machine_id, {
            'name': sync_format.display_name(machine_id, sync['machine_name']), 'platform': sync_format.platform_name(),
            'schema': sync_format.SCHEMA, 'created': sync_format.iso_time(now)})
    except (sync_format.FormatError, OSError) as error:
        raise _sync_core.SyncError('internal', f'machine.json cannot be written ({type(error).__name__})') from error
    try:
        _export_sources(run, log_dir, projects_dir, sync['include_gates'], deadline)
    finally:
        if run.new != run.old:
            write_cursor(directory, run.new)
    return run.result


def _export_sources(run: _Export, log_dir: str, projects_dir: str, include_gates: bool,
                    deadline: _sync_core.Deadline | None) -> None:
    seen = set()
    for path, session, sub in _transcripts.find_transcripts(projects_dir):
        if _out_of_time(deadline):
            run.result.complete = False
            return
        relative = os.path.relpath(path, projects_dir).replace(os.sep, '/')
        key = TRANSCRIPTS + relative
        seen.add(key)
        run.transcript(key, path, session, sub, f'{TRANSCRIPTS}{relative.split("/")[0]}/{session}.jsonl')
    for key in [key for key in run.new if key.startswith(TRANSCRIPTS) and key not in seen]:
        del run.new[key]      # Claude Code removed the transcript
    sources = [(HISTORY_FILE, 'history', run.history)] + ([(LOG_FILE, 'gate', run.gate)] if include_gates else [])
    for name, kind, convert in sources:
        for key, earlier in ((name + '.1', (name,)), (name, ())):       # the older lines first
            if _out_of_time(deadline):
                run.result.complete = False
                return
            run.line_file(key, os.path.join(log_dir, key), kind, convert, earlier)
    if _out_of_time(deadline):
        run.result.complete = False
        return
    run.line_file(ARCHIVE_FILE, os.path.join(log_dir, ARCHIVE_FILE), 'day', run.day)
