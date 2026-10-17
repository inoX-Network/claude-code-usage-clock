"""os.replace with a short retry: Windows refuses to replace a file that another process has open."""
import os
import threading
import time

import pytest

import _win


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(_win.time, 'sleep', calls.append)
    return calls


def failing(monkeypatch, times, error=PermissionError):
    calls = []
    real = os.replace

    def replace(src, dst):
        calls.append((src, dst))
        if len(calls) <= times:
            raise error(13, 'denied')
        return real(src, dst)

    monkeypatch.setattr(os, 'replace', replace)
    return calls


def test_replaces_at_once_without_waiting(tmp_path, sleeps):
    (tmp_path / 'a').write_text('new')
    (tmp_path / 'b').write_text('old')
    _win.replace(str(tmp_path / 'a'), str(tmp_path / 'b'))
    assert (tmp_path / 'b').read_text() == 'new' and not (tmp_path / 'a').exists() and sleeps == []


def test_tries_again_after_a_permission_error(tmp_path, monkeypatch, sleeps):
    (tmp_path / 'a').write_text('new')
    (tmp_path / 'b').write_text('old')
    calls = failing(monkeypatch, 3)
    _win.replace(str(tmp_path / 'a'), str(tmp_path / 'b'))
    assert len(calls) == 4 and sleeps == [_win.REPLACE_DELAY_S] * 3
    assert (tmp_path / 'b').read_text() == 'new'


def test_gives_up_after_the_attempts_and_raises_the_last_error(tmp_path, monkeypatch, sleeps):
    (tmp_path / 'a').write_text('new')
    calls = failing(monkeypatch, 10 ** 6)
    with pytest.raises(PermissionError):
        _win.replace(str(tmp_path / 'a'), str(tmp_path / 'b'))
    assert len(calls) == _win.REPLACE_ATTEMPTS and len(sleeps) == _win.REPLACE_ATTEMPTS - 1


def test_other_errors_are_not_retried(tmp_path, monkeypatch, sleeps):
    calls = failing(monkeypatch, 10 ** 6, error=FileNotFoundError)
    with pytest.raises(FileNotFoundError):
        _win.replace(str(tmp_path / 'a'), str(tmp_path / 'b'))
    assert len(calls) == 1 and sleeps == []


def test_the_total_wait_stays_well_below_a_hook_timeout():
    assert _win.REPLACE_DELAY_S * (_win.REPLACE_ATTEMPTS - 1) <= 1.5


def test_a_reader_that_lets_go_in_time_does_not_stop_the_replace(tmp_path):
    """The real thing: the target is open in another handle for a moment."""
    target, new = tmp_path / 'target', tmp_path / 'new'
    target.write_text('old')
    new.write_text('new')
    reader = open(target, 'rb')
    try:
        with pytest.raises(PermissionError):      # without the retry this is the failure
            os.replace(new, target)
        threading.Timer(0.2, reader.close).start()
        started = time.monotonic()
        _win.replace(str(new), str(target))
        assert time.monotonic() - started >= 0.1
    finally:
        reader.close()
    assert target.read_text() == 'new'
