"""Shared test setup: import paths, full isolation from the real home directory, and the POSIX-only marker.

One conftest at the repository root serves `tests/` (the main suite, all platforms) and `windows/cli/tests/` (the
Windows track, collected on Windows only).
"""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
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


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """No test may read or write the real ~/.claude, the real log or a config.json next to the scripts."""
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
