#!/usr/bin/env python3
"""The usage of a session of Claude Code on the web into the sync: what the skill /usage-export runs.

Each chat of Claude Code on the web runs in a VM of its own, without the hooks of this tool. This script puts the
usage of that one session into the sync repository, once, at the end of the chat:

1. It runs only in Claude Code on the web (`CLAUDE_CODE_REMOTE=true`, a valid `CLAUDE_CODE_SESSION_ID`); anywhere
   else it changes nothing (exit 2).
2. It installs the component `sync` into the VM with the installer of this checkout: `--sync-name claude-web`, the
   repository and the mode given (`shadow` unless `--sync-mode on`), and a settings file of its own in the log folder,
   so that the session it runs in gets no hook. The installer checks the visibility, clones and makes the machine id.
   Every chat is a machine of its own; the report counts a request it finds on two machines once, by `k`.
3. It reads the debug log of Claude Code (numbers, times and source names only; never the text of a line) and appends
   the side calls of this session to `<log_dir>/side-calls.jsonl`: one line per run of calls that never reach the
   transcript (the memory pass `extract_memories`), and one window per run of this script with the span of the log.
   Without a debug log the window says so: not measured, never zero.
4. It starts the push of the sync once (`sync.py push --now`): the push and its checks are those of the sync.
5. It prints what this chat holds in the repository, the side calls and the outcome of the push.

Exit codes: 0 done, 1 the installer or the push failed, 2 not in Claude Code on the web or bad arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from typing import NamedTuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'hooks'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import _context_state  # noqa: E402
import _plat  # noqa: E402
import _sync_export  # noqa: E402
import _sync_git  # noqa: E402
import sync_format  # noqa: E402
from log_summary import resolve_log_dir  # noqa: E402

MACHINE_NAME = 'claude-web'
DEBUG_LOG = '/tmp/claude-code.log'        # where Claude Code on the web writes its debug log (measured 2026-10-21)
DEFAULT_TARGET = '~/.claude/hooks/usage-clock'
SETTINGS_FILE = 'web-settings.json'      # in the log folder: the installer needs one, the session gets no hook
MAX_SIDE_BYTES = 8 * 1024 * 1024         # side-calls.jsonl read for the keys already written

# The shapes of the debug log lines that count. Each line starts with its time and level; only digits, the model and
# lower-case source names are taken from it.
LINE = re.compile(rb'(?P<t>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z) \[[A-Z]+\] '
                  rb'(?P<rest>.*)')
REQUEST = re.compile(rb'\[API REQUEST\] /v1/messages \S+ source=(?P<src>[a-z_]{1,20})\s*')
DISPATCH = re.compile(rb'\[API:timing\] dispatching to [A-Za-z]+ model=(?P<model>[A-Za-z0-9_.:/-]{1,64})\s*')
FINISHED = re.compile(rb'Forked agent \[(?P<src>[a-z_]{1,20})\] finished: [0-9]+ messages?, .*totalUsage: '
                      rb'input=(?P<in>[0-9]{1,12}) output=(?P<out>[0-9]{1,12}) cacheRead=(?P<cr>[0-9]{1,12}) '
                      rb'cacheCreate=(?P<cw>[0-9]{1,12})\s*')
MAIN_SOURCE = b'sdk'                     # the requests of the session itself: they are in the transcript


class DebugLog(NamedTuple):
    """What the debug log says: the finished runs, the first and last time in it, and the calls outside the
    transcript that no run with token counts took up."""
    runs: list[dict]
    first: str | None
    last: str | None
    unmeasured: int


def _time(raw: bytes) -> str:
    """A time of the log as the sync writes it (milliseconds at most)."""
    return sync_format.iso_time(sync_format.parse_time(raw.decode('ascii')))


def read_debug_log(path: str) -> DebugLog | None:
    """The side calls in the debug log at `path`; None if there is none (or it is a link, or no regular file)."""
    if os.path.islink(path):      # Windows has no O_NOFOLLOW
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | _plat.NO_FOLLOW | _plat.O_NONBLOCK | _plat.O_BINARY)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):      # a folder opens too
        os.close(descriptor)
        return None
    runs, pending, model = [], {}, None
    first = last = None
    with os.fdopen(descriptor, 'rb') as f:
        for raw in f:
            line = LINE.fullmatch(raw.rstrip(b'\r\n'))
            if line is None:
                continue
            t = _time(line['t'])
            first, last = first or t, t
            rest = line['rest']
            if (found := DISPATCH.fullmatch(rest)) is not None:
                model = found['model'].decode('ascii')
            elif (found := REQUEST.fullmatch(rest)) is not None and found['src'] != MAIN_SOURCE:
                calls, _model = pending.get(found['src'], (0, None))
                pending[found['src']] = (calls + 1, model)
            elif (found := FINISHED.fullmatch(rest)) is not None:
                calls, of = pending.pop(found['src'], (0, None))
                runs.append({'t': t, 'src': found['src'].decode('ascii'), 'model': of, 'calls': calls,
                             'in': int(found['in']), 'cw': int(found['cw']), 'cr': int(found['cr']),
                             'out': int(found['out'])})
    return DebugLog(runs, first, last, sum(calls for calls, _model in pending.values()))


def side_lines(session: str, found: DebugLog | None, now: float) -> list[dict]:
    """The lines for side-calls.jsonl: one per run (with its key), then the window of this export."""
    lines = [{'kind': 'side', 'sid': session, 'k': sync_format.side_key(session, run['src'], run['t']), **run}
             for run in (found.runs if found else [])]
    measured = found is not None and found.first is not None
    lines.append({'kind': 'side_window', 't': sync_format.iso_time(int(now)), 'sid': session, 'log': measured,
                  'from': found.first if measured else None, 'to': found.last if measured else None,
                  'unmeasured': found.unmeasured if measured else 0})
    return lines


def _known_keys(path: str) -> set[str]:
    """The keys of the runs already in side-calls.jsonl (a broken line is left to the export, which counts it)."""
    try:
        descriptor = os.open(path, os.O_RDONLY | _plat.NO_FOLLOW | _plat.O_BINARY)
    except FileNotFoundError:
        return set()
    with os.fdopen(descriptor, 'rb') as f:
        content = f.read(MAX_SIDE_BYTES)
    keys = set()
    for raw in content.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get('k'), str):
            keys.add(row['k'])
    return keys


def append_side(log_dir: str, lines: list[dict]) -> int:
    """Appends the runs not yet in `<log_dir>/side-calls.jsonl` and every window: the export reads the file on from
    where it stopped, so it is only ever appended to (0600, never through a link). The number of lines written."""
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    if not _plat.is_folder(os.lstat(log_dir)):
        raise OSError(f'{log_dir} is not a folder')
    path = os.path.join(log_dir, _sync_export.SIDE_FILE)
    if os.path.islink(path):      # Windows has no O_NOFOLLOW
        raise OSError(f'{path} is a link')
    known = _known_keys(path)
    new = [line for line in lines if line['kind'] != 'side' or line['k'] not in known]
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _plat.NO_FOLLOW | _plat.O_BINARY, 0o600)
    with os.fdopen(descriptor, 'ab') as f:
        if os.name != 'nt':
            os.fchmod(descriptor, 0o600)
        f.write(''.join(json.dumps(line, separators=(',', ':')) + '\n' for line in new).encode('utf-8'))
    return len(new)


def run(argv: list[str]) -> int:
    """Runs a step (the installer, the push) with its output on the terminal; its exit code."""
    return subprocess.run(argv, check=False).returncode


def _sync_lines(log_dir: str, offset: int) -> list[dict]:
    """The lines of hook `sync` that log.jsonl got from byte `offset` on."""
    try:
        with open(os.path.join(log_dir, 'log.jsonl'), 'rb') as f:
            f.seek(offset)
            raws = f.read().splitlines()
    except OSError:
        return []
    lines = []
    for raw in raws:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('hook') == 'sync':
            lines.append(row)
    return lines


def _log_size(log_dir: str) -> int:
    try:
        return os.path.getsize(os.path.join(log_dir, 'log.jsonl'))
    except OSError:
        return 0


def push_text(lines: list[dict], repo: str, mode: str) -> str:
    """What the push did, from the log lines it wrote."""
    outcome = next((line for line in reversed(lines) if line.get('decision') in ('success', 'failure')), None)
    if outcome is None:
        return 'push: no outcome in the log'
    shadow = next((line for line in reversed(lines) if line.get('decision') == 'shadow'), None)
    if outcome['decision'] == 'success' and shadow is not None:       # shadow, then the core's success
        outcome = shadow
    if outcome['decision'] == 'shadow':
        return (f'shadow: committed here, pushed nothing ({outcome.get("lines", 0)} lines, {outcome.get("bytes", 0)} '
                'bytes). Run /usage-export on to push for real.')
    if outcome['decision'] == 'success':
        return f'pushed to {repo}' if mode == 'on' else 'push: done'
    return f'push failed ({outcome.get("category", "?")}): {outcome.get("reason", "")}'


def side_text(found: DebugLog | None) -> list[str]:
    if found is None or found.first is None:
        return ['side calls: not measured (no debug log)']
    texts = []
    for source in sorted({run['src'] for run in found.runs}):
        runs = [run for run in found.runs if run['src'] == source]
        texts.append(f'side calls {source}: {len(runs)} runs, {sum(r["calls"] for r in runs)} calls '
                     f'(read {sum(r["cr"] for r in runs)}, written {sum(r["cw"] for r in runs)}, '
                     f'input {sum(r["in"] for r in runs)}, output {sum(r["out"] for r in runs)} tokens)')
    texts.append(f'debug log from {found.first[11:19]} to {found.last[11:19]} UTC; requests of this session '
                 'outside this span have no measurement of their side calls')
    if found.unmeasured:
        texts.append(f'{found.unmeasured} calls without token counts in the log')
    return texts


def held_text(log_dir: str, session: str) -> str:
    """What the clone holds of this session after the push."""
    machine = sync_format.load_machine_id(log_dir, create=False)
    if machine is None:
        return 'no machine id yet'
    skipped = dict.fromkeys(sync_format.COUNTERS, 0)
    held = sync_format.read_machine(_sync_git.clone_dir(log_dir), machine, skipped)
    requests = sum(1 for row in held.requests if row['sid'] == session)
    runs = sum(1 for row in held.side if row['sid'] == session)
    return (f'this chat is machine {machine[:8]} ({MACHINE_NAME}); the repository holds {requests} request'
            f'{"" if requests == 1 else "s"} and {runs} side run{"" if runs == 1 else "s"} of session {session[:8]}')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='web_export.py', allow_abbrev=False,
                                     description='Claude Code on the web: the usage of this session into the sync.')
    parser.add_argument('--sync-repo', required=True, metavar='OWNER/NAME', help='the private sync repository')
    parser.add_argument('--sync-mode', choices=('shadow', 'on'), default='shadow',
                        help='shadow (default): commit here and push nothing; on: push')
    parser.add_argument('--debug-log', default=DEBUG_LOG, help=f'the debug log of Claude Code (default {DEBUG_LOG})')
    parser.add_argument('--target', default=DEFAULT_TARGET, help=f'where the installer puts the hooks '
                                                                 f'(default {DEFAULT_TARGET})')
    return parser


def main(argv: list[str] | None = None, now: float | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.environ.get('CLAUDE_CODE_REMOTE') != 'true':
        print('web_export.py runs only in Claude Code on the web: nothing was done. On your computer the hooks of the '
              'usage clock export by themselves.')
        return 2
    session = os.environ.get('CLAUDE_CODE_SESSION_ID', '')
    if not sync_format.SESSION_ID.fullmatch(session):
        print('No usable session id in CLAUDE_CODE_SESSION_ID: nothing was done.')
        return 2
    target = os.path.expanduser(args.target)
    log_dir = (os.path.expanduser(os.environ['CCUC_LOG_DIR']) if os.environ.get('CCUC_LOG_DIR')    # as the hooks do
               else resolve_log_dir(os.path.join(target, 'config.json')))
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    settings = os.path.join(log_dir, SETTINGS_FILE)
    if not os.path.lexists(settings):
        _context_state.write_private(log_dir, SETTINGS_FILE, '{}')
    if run([sys.executable, os.path.join(ROOT, 'tools', 'install.py'), '--settings', settings, '--target', target,
            '--components', 'sync', '--sync-repo', args.sync_repo, '--sync-mode', args.sync_mode,
            '--sync-name', MACHINE_NAME]) != 0:
        print('usage-export: the installer stopped (see above); nothing was exported.')
        return 1
    found = read_debug_log(args.debug_log)
    append_side(log_dir, side_lines(session, found, time.time() if now is None else now))
    offset = _log_size(log_dir)
    code = run([sys.executable, os.path.join(target, 'sync.py'), 'push', '--now'])
    print('\n'.join(['usage-export:', *(f'  {text}' for text in side_text(found)),
                     f'  {push_text(_sync_lines(log_dir, offset), args.sync_repo, args.sync_mode)}',
                     f'  {held_text(log_dir, session)}']))
    return 0 if code == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
