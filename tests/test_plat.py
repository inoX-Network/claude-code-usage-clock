"""Tests for hooks/_plat.py, the platform switch. The Windows side is tested in windows/cli/tests/."""
import os
import stat

import pytest

import _plat
import _usage
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
    assert _plat.hook_shell() == '/bin/sh' and _plat.hook_shell({'a': 'b'}) == '/bin/sh'


@pytest.mark.posix_only
def test_the_script_command_is_python3_and_a_shell_quoted_path():
    assert _plat.script_command('/home/u/hooks/x.py') == 'python3 /home/u/hooks/x.py'
    assert _plat.script_command('/home/u/my hooks/x.py', 'sess-1', '--mode=enforce') == (
        "python3 '/home/u/my hooks/x.py' sess-1 --mode=enforce")
    assert _plat.quote_path("/tmp/it's here") == "'/tmp/it'\"'\"'s here'"

