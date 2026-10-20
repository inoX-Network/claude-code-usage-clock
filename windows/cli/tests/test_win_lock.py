"""The exclusive file lock of the Windows track (msvcrt.locking behind the flock interface)."""
import errno
import msvcrt
import os
import subprocess
import sys
import textwrap
import time

import pytest

import _win
from conftest import HOOKS, WINDOWS_CLI

# A second process takes the lock, writes into the file and keeps holding the lock for a while. It writes the time
# it is still holding it (`done`) before it lets go, and stays alive after that (the buffered write must already be
# on disk when the lock is released, not only when the file is closed).
HOLDER = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, sys.argv[1])
    import _win
    path, ready, done, hold = sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5])
    with open(path, 'r+b') as f:
        with _win.locked(f):
            open(ready, 'w').close()
            f.seek(0, 2)
            f.write(b'data')
            time.sleep(hold)
            with open(done, 'w') as marker:
                marker.write(repr(time.time()))
        time.sleep(float(sys.argv[6]))
''')


def start_holder(tmp_path, hold, linger=0.0):
    data = tmp_path / 'data.bin'
    data.write_bytes(b'')
    paths = [str(tmp_path / 'ready'), str(tmp_path / 'done')]
    process = subprocess.Popen([sys.executable, '-c', HOLDER, WINDOWS_CLI, str(data), *paths, str(hold), str(linger)])
    deadline = time.monotonic() + 10
    while not os.path.exists(paths[0]):
        assert time.monotonic() < deadline, 'the holder never got the lock'
        time.sleep(0.01)
    return process, data, paths[1]


def test_waits_until_the_other_process_lets_go(tmp_path):
    process, data, done = start_holder(tmp_path, hold=1.0)
    with open(data, 'r+b') as f:
        with _win.locked(f):
            acquired = time.time()
            with open(done) as marker:
                released_after = float(marker.read())
    process.wait(timeout=10)
    assert acquired >= released_after


def test_the_lock_is_taken_soon_after_the_other_process_lets_go(tmp_path):
    """The waiter polls often: a slow poll would add up to a second to every hook that meets a writer."""
    process, data, done = start_holder(tmp_path, hold=0.5)
    with open(data, 'r+b') as f:
        with _win.locked(f):
            acquired = time.time()
            with open(done) as marker:
                released_after = float(marker.read())
    process.wait(timeout=10)
    assert acquired - released_after < 0.25


def test_data_is_on_disk_when_the_lock_is_released(tmp_path):
    process, data, _ = start_holder(tmp_path, hold=0.3, linger=2.0)
    with open(data, 'r+b') as f:
        with _win.locked(f):
            content = f.read()
    process.kill()
    assert content == b'data'


def test_the_data_stays_readable_while_it_is_locked(tmp_path):
    """The lock sits far behind the data: a reader that does not lock (read_archive, the reports) is not shut out."""
    data = tmp_path / 'data.bin'
    data.write_bytes(b'0123456789')
    with open(data, 'r+b') as f, _win.locked(f):
        assert data.read_bytes() == b'0123456789'
        with open(data, 'rb') as reader:
            assert reader.read() == b'0123456789'


def test_a_second_handle_of_the_same_process_is_kept_out(tmp_path):
    path = tmp_path / 'file.lock'
    path.write_bytes(b'')
    with open(path, 'r+b') as first, open(path, 'r+b') as second:
        with _win.locked(first):
            os.lseek(second.fileno(), _win.LOCK_OFFSET, os.SEEK_SET)
            with pytest.raises(OSError):
                msvcrt.locking(second.fileno(), msvcrt.LK_NBLCK, 1)
        os.lseek(second.fileno(), _win.LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(second.fileno(), msvcrt.LK_NBLCK, 1)      # free again after the with block
        msvcrt.locking(second.fileno(), msvcrt.LK_UNLCK, 1)


def test_the_file_position_is_kept(tmp_path):
    path = tmp_path / 'data.bin'
    path.write_bytes(b'abcdefgh')
    with open(path, 'r+b') as f:
        assert f.read(3) == b'abc'
        with _win.locked(f):
            assert f.read(2) == b'de'
        assert f.read() == b'fgh'


def test_the_lock_is_gone_when_the_holder_dies(tmp_path):
    process, data, _ = start_holder(tmp_path, hold=30)
    process.kill()
    process.wait(timeout=10)
    started = time.monotonic()
    with open(data, 'r+b') as f, _win.locked(f):
        pass
    assert time.monotonic() - started < 5


def test_a_hard_error_is_raised_not_waited_for(monkeypatch):
    """Only "somebody else holds it" is waited for; a bad descriptor must not spin forever."""
    def no_waiting(_):
        raise AssertionError('waited on an error that waiting cannot fix')
    monkeypatch.setattr(_win.time, 'sleep', no_waiting)

    class NoFile:
        def fileno(self):
            return 9999

    with pytest.raises(OSError) as raised:
        with _win.locked(NoFile()):
            pass
    assert raised.value.errno == errno.EBADF


def test_the_lock_is_released_when_the_block_raises(tmp_path):
    path = tmp_path / 'file.lock'
    path.write_bytes(b'')
    with open(path, 'r+b') as first, open(path, 'r+b') as second:
        with pytest.raises(RuntimeError):
            with _win.locked(first):
                raise RuntimeError
        with _win.locked(second):
            pass


def test_hooks_directory_is_importable_next_to_the_windows_module():
    """The tests rely on both folders being on the path; a changed layout shows up here first."""
    assert os.path.isfile(os.path.join(HOOKS, '_usage.py')) and os.path.isfile(os.path.join(WINDOWS_CLI, '_win.py'))


def test_without_waiting_a_held_lock_raises_blocking_io_error_at_once(tmp_path):
    path = tmp_path / 'file.lock'
    path.write_bytes(b'')
    with open(path, 'r+b') as first, open(path, 'r+b') as second:
        with _win.locked(first, wait=False):
            begin = time.monotonic()
            with pytest.raises(BlockingIOError):
                with _win.locked(second, wait=False):
                    pass
            assert time.monotonic() - begin < 1
        with _win.locked(second, wait=False):      # free again after the with block
            pass


def test_the_process_flags_are_those_of_the_win32_api():
    assert (_win.DETACHED_PROCESS, _win.CREATE_NEW_PROCESS_GROUP, _win.CREATE_NO_WINDOW) == (
        subprocess.DETACHED_PROCESS, subprocess.CREATE_NEW_PROCESS_GROUP, subprocess.CREATE_NO_WINDOW)


def test_a_real_detached_start_of_the_running_python_gets_no_console_window(tmp_path):
    """The suite runs from a venv: sys.executable is the launcher, whose child would get a visible console window
    (measured 20.10.2026). The child reports the handle of its console window, 0 for none."""
    report = tmp_path / 'console.txt'
    process = _win.start_detached([sys.executable, '-c', textwrap.dedent(f'''
        import ctypes
        open({str(report)!r}, 'w').write(str(ctypes.windll.kernel32.GetConsoleWindow()))
    ''')])
    assert process.wait(timeout=30) == 0
    assert report.read_text() == '0'


def test_kill_tree_ends_a_child_and_its_grandchild(tmp_path):
    marker = tmp_path / 'grandchild.pid'
    child = subprocess.Popen([sys.executable, '-c', textwrap.dedent(f'''
        import subprocess, sys, time
        grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        open({str(marker)!r}, 'w').write(str(grandchild.pid))
        time.sleep(60)
    ''')], creationflags=_win.GROUP_FLAGS)
    deadline = time.monotonic() + 30
    while not marker.exists() or not marker.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    grandchild = int(marker.read_text())
    _win.kill_tree(child)
    assert child.wait(timeout=30) is not None
    listed = subprocess.run(['tasklist', '/FI', f'PID eq {grandchild}', '/NH'], capture_output=True, text=True)
    assert str(grandchild) not in listed.stdout
