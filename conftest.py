"""Shared test setup: import paths, full isolation from the real home directory, and the POSIX-only marker.

One conftest at the repository root serves `tests/` (the main suite, all platforms) and `windows/cli/tests/` (the
Windows track, collected on Windows only).
"""
import builtins
import errno
import io
import os
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
# What Claude Code reads from the real home folder, taken before any test points HOME elsewhere. The estimate of the
# usage report walks up from a working folder to the root; on Windows the temp folder lies in the user profile, so a
# walk from tmp_path passes the real home. No test may see these, on any system (see isolated_env).
REAL_HOME = os.path.expanduser('~')
REAL_HOME_ENTRIES = [os.path.normcase(os.path.join(REAL_HOME, name))
                     for name in ('.claude', 'CLAUDE.md', 'CLAUDE.local.md', '.git')]
HOOKS = os.path.join(ROOT, 'hooks')
WINDOWS_CLI = os.path.join(ROOT, 'windows', 'cli')
sys.path.insert(0, WINDOWS_CLI)
sys.path.insert(0, HOOKS)

# The only reason for which a test may be skipped, and only on Windows. The CI check (.github/check_pytest_output.py)
# accepts exactly this text and rejects every other skip.
POSIX_ONLY_REASON = 'POSIX only: needs symlinks, FIFOs, file modes, user ids or POSIX time zone rules, which Windows does not have'

if os.name != 'nt':
    collect_ignore_glob = ['windows/cli/tests/*']


def pytest_collection_modifyitems(config, items):
    """A test marked `posix_only` is skipped on Windows, with the one reason above. Nowhere else."""
    if os.name != 'nt':
        return
    skip = pytest.mark.skip(reason=POSIX_ONLY_REASON)
    for item in items:
        if item.get_closest_marker('posix_only'):
            item.add_marker(skip)


def tzset():
    """time.tzset(), which Windows lacks: its C runtime reads TZ again on _tzset(). Offsets work as on POSIX; daylight
    saving follows the rules of the United States there, not those of a rule string."""
    if hasattr(time, 'tzset'):
        time.tzset()
    else:
        import ctypes
        ctypes.CDLL('ucrtbase')._tzset()


def lock_is_held(path):
    """True while another handle of this process cannot take the file lock of `path` (flock; on Windows the byte that
    _win.locked takes)."""
    probe = os.open(path, os.O_RDWR)
    try:
        if os.name == 'nt':
            import msvcrt
            import _win
            os.lseek(probe, _win.LOCK_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(probe, msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(probe, msvcrt.LK_UNLCK, 1)
            return False
        import fcntl
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(probe)


def in_real_home(path):
    """True if `path` is one of REAL_HOME_ENTRIES or below one."""
    try:
        name = os.fsdecode(os.path.normcase(os.path.abspath(os.fspath(path))))
    except TypeError:                                       # a file descriptor
        return False
    return any(name == entry or name.startswith(entry + os.sep) for entry in REAL_HOME_ENTRIES)


def _refusing(real):
    def call(*args, **kwargs):
        path = args[0] if args else next(iter(kwargs.values()), '.')
        if in_real_home(path):
            raise PermissionError(errno.EACCES, 'a test may not read the real home folder', os.fsdecode(os.fspath(path)))
        return real(*args, **kwargs)
    return call


def _absent(real):
    def call(path, *args, **kwargs):
        return False if in_real_home(path) else real(path, *args, **kwargs)
    return call


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """No test may read or write the real ~/.claude, the real log or a config.json next to the scripts. The Claude
    files of the real home folder are walled off: opening or listing them fails as if access were denied, and they
    do not exist for os.path (on Windows os.path.isfile and friends do not go through os.stat)."""
    for module, name in ((builtins, 'open'), (io, 'open'), (os, 'open'), (os, 'stat'), (os, 'lstat'), (os, 'listdir'),
                         (os, 'scandir')):
        monkeypatch.setattr(module, name, _refusing(getattr(module, name)))
    for name in ('exists', 'lexists', 'isfile', 'isdir', 'islink'):
        monkeypatch.setattr(os.path, name, _absent(getattr(os.path, name)))
    for name in list(os.environ):
        if name.startswith('CCUC_') or name == 'CLAUDE_PROJECT_DIR':
            monkeypatch.delenv(name)
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    # Python on Windows takes "~" from USERPROFILE (HOME is ignored there), so both are pointed away
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    if os.name == 'nt':
        # the hook commands the installer writes run in UTF-8 mode (-X utf8); a child of a test does the same
        monkeypatch.setenv('PYTHONUTF8', '1')
    monkeypatch.setenv('CCUC_USAGE_FILE', str(tmp_path / 'usage.json'))
    monkeypatch.setenv('CCUC_LOG_DIR', str(tmp_path / 'log'))
    monkeypatch.setenv('CCUC_CONFIG', str(tmp_path / 'no-config.json'))
    # No test reaches the network through urllib (the second stage of the visibility probe asks the GitHub API)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', _no_network)


def _no_network(self, *args, **kwargs):
    raise urllib.error.URLError('a test may not reach the network')
