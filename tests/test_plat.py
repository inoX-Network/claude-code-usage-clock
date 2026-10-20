"""Tests for hooks/_plat.py, the platform switch. The Windows side is tested in windows/cli/tests/."""
import json
import os
import stat
import subprocess
import sys

import pytest

import _plat
import _usage
import _win
from conftest import lock_is_held


def test_usage_replace_replaces_the_target(tmp_path):
    (tmp_path / 'new').write_text('new')
    (tmp_path / 'old').write_text('old')
    _usage.replace(str(tmp_path / 'new'), str(tmp_path / 'old'))
    assert (tmp_path / 'old').read_text() == 'new' and not (tmp_path / 'new').exists()


def test_replace_replaces_the_target(tmp_path):
    (tmp_path / 'new').write_text('new')
    (tmp_path / 'old').write_text('old')
    _plat.replace(str(tmp_path / 'new'), str(tmp_path / 'old'))
    assert (tmp_path / 'old').read_text() == 'new' and not (tmp_path / 'new').exists()


def test_replace_raises_what_os_replace_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        _plat.replace(str(tmp_path / 'missing'), str(tmp_path / 'target'))


def test_the_usage_module_holds_the_lock_for_the_block_and_lets_go_after_it(tmp_path):
    path = tmp_path / 'file.lock'
    path.write_bytes(b'')
    with open(path, 'r+b') as f:
        assert not lock_is_held(str(path))
        with _usage.locked(f):
            assert lock_is_held(str(path))
        assert not lock_is_held(str(path))


def test_the_usage_module_lets_go_of_the_lock_when_the_block_raises(tmp_path):
    path = tmp_path / 'file.lock'
    path.write_bytes(b'')
    with open(path, 'r+b') as f:
        with pytest.raises(RuntimeError):
            with _usage.locked(f):
                raise RuntimeError
        assert not lock_is_held(str(path))


def test_pread_reads_at_an_offset_and_leaves_appending_alone(tmp_path):
    path = tmp_path / 'f'
    path.write_bytes(b'abcdef')
    descriptor = os.open(path, os.O_RDWR | os.O_APPEND | _plat.O_BINARY)
    try:
        assert _plat.pread(descriptor, 3, 2) == b'cde'
        assert _plat.pread(descriptor, 99, 4) == b'ef'
        assert _plat.pread(descriptor, 3, 99) == b''
        os.write(descriptor, b'XY')
    finally:
        os.close(descriptor)
    assert path.read_bytes() == b'abcdefXY'


@pytest.mark.posix_only
def test_fchmod_sets_the_mode(tmp_path):
    path = tmp_path / 'f'
    path.write_bytes(b'x')
    descriptor = os.open(path, os.O_RDWR)
    try:
        _plat.fchmod(descriptor, 0o640)
    finally:
        os.close(descriptor)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


@pytest.mark.posix_only
def test_the_open_flags_are_the_ones_of_the_system():
    assert _plat.NO_FOLLOW == os.O_NOFOLLOW and _plat.O_NONBLOCK == os.O_NONBLOCK and _plat.O_BINARY == 0


@pytest.mark.posix_only
def test_nothing_is_translated_for_a_posix_system():
    assert _plat.normalize_path('C:\\Users\\x') == 'C:\\Users\\x' and _plat.normalize_path('/c/x') == '/c/x'
    assert _plat.native_path('C:\\Users\\x') == 'C:\\Users\\x' and _plat.native_path('/c/x') == '/c/x'
    assert _plat.hook_shell() == '/bin/sh' and _plat.hook_shell({'a': 'b'}) == '/bin/sh'


@pytest.mark.posix_only
def test_the_script_command_is_python3_and_a_shell_quoted_path():
    assert _plat.script_command('/home/u/hooks/x.py') == 'python3 /home/u/hooks/x.py'
    assert _plat.script_command('/home/u/my hooks/x.py', 'sess-1', '--mode=enforce') == (
        "python3 '/home/u/my hooks/x.py' sess-1 --mode=enforce")
    assert _plat.quote_path("/tmp/it's here") == "'/tmp/it'\"'\"'s here'"



# --- The sync core: a lock without waiting, a detached start, ending a process tree ---------------------------------

def test_the_usage_lock_without_waiting_raises_at_once_while_another_handle_holds_it(tmp_path):
    path = tmp_path / 'sync.lock'
    path.write_bytes(b'')
    with open(path, 'r+b') as first, open(path, 'r+b') as second:
        with _usage.locked(first, wait=False):
            with pytest.raises(BlockingIOError):
                with _usage.locked(second, wait=False):
                    pass
        with _usage.locked(second, wait=False):
            assert lock_is_held(str(path))
    assert not lock_is_held(str(path))


# The child reports its session, what it reads on stdin and whether its stdout is the null device.
DETACHED_CHILD = '''
import json, os, sys
out = os.fstat(1)
null = os.stat(os.devnull)
with open(sys.argv[1], 'w') as f:
    json.dump({'sid': os.getsid(0), 'stdin': sys.stdin.read(),
               'null_out': (out.st_dev, out.st_ino) == (null.st_dev, null.st_ino)}, f)
'''


@pytest.mark.posix_only
def test_a_detached_start_has_a_session_of_its_own_and_the_null_device_for_its_streams(tmp_path):
    report = tmp_path / 'report.json'
    process = _plat.start_detached([sys.executable, '-c', DETACHED_CHILD, str(report)])
    assert process.wait(timeout=30) == 0
    seen = json.loads(report.read_text())
    assert seen == {'sid': process.pid, 'stdin': '', 'null_out': True}


@pytest.mark.posix_only
def test_ending_the_tree_of_a_process_that_is_gone_raises_nothing():
    process = subprocess.Popen([sys.executable, '-c', 'pass'], **_plat.GROUP_OPTIONS)
    process.wait(timeout=30)
    _plat.kill_tree(process)


def test_the_windows_start_uses_the_three_flags_and_the_null_device():
    calls = []
    _win.start_detached(['python', 'sync.py', 'push'], popen=lambda *args, **kwargs: calls.append((args, kwargs)))
    (args, kwargs), = calls
    assert args == (['python', 'sync.py', 'push'],)
    assert kwargs['creationflags'] == 0x00000008 | 0x00000200 | 0x08000000   # DETACHED, NEW_PROCESS_GROUP, NO_WINDOW
    assert kwargs['stdin'] == kwargs['stdout'] == kwargs['stderr'] == subprocess.DEVNULL
    assert _win.GROUP_FLAGS == 0x00000200 | 0x08000000


def test_a_detached_start_from_a_venv_runs_the_python_the_venv_was_made_from(tmp_path, monkeypatch):
    """The python.exe of a venv is a launcher that starts the real one as its child; without a console of its own
    (DETACHED_PROCESS) that child gets a new, visible console window (measured on Windows 11, 20.10.2026)."""
    base = tmp_path / 'Python314' / 'python.exe'
    base.parent.mkdir()
    base.write_bytes(b'')
    launcher = str(tmp_path / '.venv' / 'Scripts' / 'python.exe')
    monkeypatch.setattr(sys, 'executable', launcher)
    monkeypatch.setattr(sys, 'prefix', str(tmp_path / '.venv'))
    monkeypatch.setattr(sys, 'base_prefix', str(base.parent))
    calls = []
    _win.start_detached([launcher, 'sync.py', 'push'], popen=lambda *args, **kwargs: calls.append(args[0]))
    _win.start_detached(['C:/other/python.exe', 'sync.py'], popen=lambda *args, **kwargs: calls.append(args[0]))
    assert calls == [[str(base), 'sync.py', 'push'], ['C:/other/python.exe', 'sync.py']]
