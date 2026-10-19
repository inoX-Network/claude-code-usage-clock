#!/usr/bin/env python3
"""Sync across machines, the command: push, pull and the two session hooks (see _sync_core.py).

    sync.py push [--now|--if-due] the push; SessionEnd (T2) starts it detached, the status line (T1) with --if-due
                                  (only if it is still due once the lock is taken). --now is the manual run (T4): past
                                  the wait and the backoff, never past `visibility` or the lock
    sync.py pull [--background]   the pull; --background with 30 s (SessionStart starts it), else sync.pull_timeout_s
    sync.py session-start         hook SessionStart (T3)
    sync.py session-end           hook SessionEnd (T2)

The push is `_sync_git.push` (0034-SYNC-PUSH). The pull is `git_pull` below (0034-SYNC-REPORT): the clone brought onto
the remote with the helper the push uses, never merged; the usage report runs it before a report as well.

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
import _sync_git  # noqa: E402


def git_pull(run: _sync_core.Run) -> None:
    """The pull: the clone checked as for the push, then brought onto the remote (a fast forward, or its own commits
    rebased onto it); never a merge. A history that went apart while the work tree of the clone has changes leaves the
    clone as it is: `conflict`, as for a rebase that does not fit."""
    clone = _sync_git.open_clone(run)
    run.deadline.check()
    if _sync_git.update_clone(run, clone) == _sync_git.DIVERGED_DIRTY:
        raise _sync_core.SyncError('conflict', 'the sync history went apart and the clone has changes: not updated')


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
    """`push_runner` and `pull_runner` replace _sync_git.push and git_pull (tests)."""
    args = _parser().parse_args(argv)
    if args.command in ('session-start', 'session-end'):
        return _hook(args.command)
    cfg, log_dir = _config.load(), _hookio.log_dir()
    try:
        if args.command == 'push':
            return _sync_core.push(log_dir, cfg, push_runner or _sync_git.push, forced=args.now, if_due=args.if_due)
        return _sync_core.pull(log_dir, cfg, pull_runner or git_pull, background=args.background)
    except OSError as error:      # no sync folder, no lock file: nothing could be recorded in the state
        _sync_core.log_hourly(log_dir, time.time(), _sync_core.ERROR_NOTE, 'error',
                              f'{args.command}: {type(error).__name__}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
