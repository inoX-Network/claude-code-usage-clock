#!/usr/bin/env python3
"""Sync across machines, the command: push, pull and the two session hooks (see _sync_core.py).

    sync.py push [--now|--if-due] the push; SessionEnd (T2) starts it detached, the status line (T1) with --if-due
                                  (only if it is still due once the lock is taken). --now is the manual run (T4): past
                                  the wait and the backoff, never past `visibility` or the lock
    sync.py pull [--background]   the pull; --background with 30 s (SessionStart starts it), else sync.pull_timeout_s
    sync.py session-start         hook SessionStart (T3)
    sync.py session-end           hook SessionEnd (T2)

The git work is `git_push` and `git_pull` below. Until 0034-SYNC-PUSH and 0034-SYNC-REPORT bring it, both fail with
the category `internal`: a run that synced nothing must never look like a success. It is logged and backs off like any
failure, so the status line does not start a push on every tick (and `sync.mode` is `off` by default).

Exit codes: 0 success or nothing to do (sync off, not due, lock held by another run), 1 failed, blocked after
`visibility` or ended by the watchdog, 2 bad arguments. The hooks print nothing and always exit 0. That sync/ cannot be
used is logged at most once an hour.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _config  # noqa: E402
import _hookio  # noqa: E402
import _sync_core  # noqa: E402


def git_push(run: _sync_core.Run) -> None:
    """The push itself (0034-SYNC-PUSH). Not built yet."""
    raise _sync_core.SyncError('internal', 'push is not built yet')


def git_pull(run: _sync_core.Run) -> None:
    """The pull itself (0034-SYNC-REPORT). Not built yet."""
    raise _sync_core.SyncError('internal', 'pull is not built yet')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='sync.py', description='Sync the usage data across machines.')
    commands = parser.add_subparsers(dest='command', required=True)
    push = commands.add_parser('push', help='push this machine (when due)')
    when = push.add_mutually_exclusive_group()
    when.add_argument('--now', action='store_true', help='push now, past the wait and the backoff')
    when.add_argument('--if-due', action='store_true', help='push only if it is due (the status line)')
    pull = commands.add_parser('pull', help='pull the other machines')
    pull.add_argument('--background', action='store_true', help='with the longer timeout of the background')
    commands.add_parser('session-start', help='hook SessionStart')
    commands.add_parser('session-end', help='hook SessionEnd')
    return parser


def _hook(command: str) -> int:
    """SessionStart and SessionEnd: nothing may block or fail, nothing is printed."""
    try:
        _hookio.read_input()
        cfg, log_dir, now = _config.load(), _hookio.log_dir(), time.time()
        if command == 'session-start':
            _sync_core.session_start(log_dir, cfg, now)
        else:
            _sync_core.session_end(log_dir, cfg, now)
    except Exception:
        pass
    return 0


def main(argv: list[str] | None = None, push_runner: _sync_core.Runner | None = None,
         pull_runner: _sync_core.Runner | None = None) -> int:
    """`push_runner` and `pull_runner` replace git_push and git_pull (tests)."""
    args = _parser().parse_args(argv)
    if args.command in ('session-start', 'session-end'):
        return _hook(args.command)
    cfg, log_dir = _config.load(), _hookio.log_dir()
    try:
        if args.command == 'push':
            return _sync_core.push(log_dir, cfg, push_runner or git_push, forced=args.now, if_due=args.if_due)
        return _sync_core.pull(log_dir, cfg, pull_runner or git_pull, background=args.background)
    except OSError as error:      # no sync folder, no lock file: nothing could be recorded in the state
        _sync_core.log_hourly(log_dir, time.time(), _sync_core.ERROR_NOTE, 'error',
                              f'{args.command}: {type(error).__name__}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
