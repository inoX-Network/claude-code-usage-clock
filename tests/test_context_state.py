"""Tests for hooks/_context_state.py (the per-session context file) and for how the status line feeds it."""
import io
import json
import os
import re
import stat
import sys
import time

import pytest

import _context_state
import statusline
from conftest import HOOKS

NOW = 1_800_000_000.0
DAY = 86400


def state_dir(tmp_path):
    return tmp_path / 'log' / 'context'


def save(tmp_path, session_id='abc-123_X', size=1_000_000, used=231_000, now=None):
    _context_state.save(str(state_dir(tmp_path)), session_id, size, used, time.time() if now is None else now)


def read(tmp_path, session_id='abc-123_X'):
    return json.loads((state_dir(tmp_path) / f'{session_id}.json').read_text(encoding='utf-8'))


# --- Content and permissions ----------------------------------------------------------------------------------

def test_the_file_has_exactly_the_documented_content(tmp_path):
    save(tmp_path, now=NOW)
    assert read(tmp_path) == {'session_id': 'abc-123_X', 'context_window_size': 1_000_000, 'used_tokens': 231_000,
                              'measured_at': '2027-01-15T08:00:00Z'}
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', read(tmp_path)['measured_at'])


def test_the_file_is_private_and_so_is_the_folder(tmp_path):
    save(tmp_path)
    assert stat.S_IMODE(os.stat(state_dir(tmp_path) / 'abc-123_X.json').st_mode) == 0o600
    assert stat.S_IMODE(os.stat(state_dir(tmp_path)).st_mode) == 0o700


def test_a_second_write_replaces_the_values_and_leaves_no_temporary_file(tmp_path):
    save(tmp_path, used=100_000, now=NOW)
    save(tmp_path, used=250_000, now=NOW + 60)
    assert read(tmp_path)['used_tokens'] == 250_000 and read(tmp_path)['measured_at'] == '2027-01-15T08:01:00Z'
    assert os.listdir(state_dir(tmp_path)) == ['abc-123_X.json']


def test_two_sessions_do_not_mix(tmp_path):
    save(tmp_path, session_id='one', size=1_000_000, used=111_000)
    save(tmp_path, session_id='two', size=200_000, used=22_000)
    save(tmp_path, session_id='one', size=1_000_000, used=333_000)
    one = read(tmp_path, 'one')
    assert one.pop('measured_at') and one == {'session_id': 'one', 'context_window_size': 1_000_000,
                                              'used_tokens': 333_000}
    assert read(tmp_path, 'two')['used_tokens'] == 22_000 and read(tmp_path, 'two')['context_window_size'] == 200_000
    assert sorted(os.listdir(state_dir(tmp_path))) == ['one.json', 'two.json']


def test_float_numbers_are_stored_as_whole_numbers(tmp_path):
    save(tmp_path, size=1e6, used=231000.0)
    assert read(tmp_path)['context_window_size'] == 1_000_000 and read(tmp_path)['used_tokens'] == 231_000
    assert '.' not in (state_dir(tmp_path) / 'abc-123_X.json').read_text(encoding='utf-8')


# --- The session id decides the file name ---------------------------------------------------------------------

@pytest.mark.parametrize('session_id', ['../x', '..', '.', '', 'a/b', 'a\\b', 'a b', 'a.json', 'x' * 129, 'ä', 'a\n',
                                        '../../etc/passwd', '/abs', 'a\x00b', None, 5, ['a'], b'abc'])
def test_an_invalid_session_id_writes_nothing(tmp_path, session_id):
    save(tmp_path, session_id=session_id)
    assert not (tmp_path / 'log').exists()
    assert not (tmp_path / 'x').exists() and not (tmp_path / 'x.json').exists()


@pytest.mark.parametrize('session_id', ['a', 'A', '0', '_', '-', 'x' * 128, 'abc-DEF_123',
                                        '0b4c1d2e-aaaa-bbbb-cccc-1234567890ab'])
def test_a_valid_session_id_is_written(tmp_path, session_id):
    save(tmp_path, session_id=session_id)
    assert read(tmp_path, session_id)['session_id'] == session_id


@pytest.mark.parametrize('size,used', [(0, 5), (-1, 5), (1_000_000, 0), (1_000_000, -5), (True, 5), (1_000_000, True),
                                       ('1000000', 5), (1_000_000, '5'), (None, 5), (1_000_000, None),
                                       (float('nan'), 5), (1_000_000, float('inf'))])
def test_numbers_that_are_not_positive_finite_numbers_write_nothing(tmp_path, size, used):
    save(tmp_path, size=size, used=used)
    assert not (tmp_path / 'log').exists()


# --- No links are followed -------------------------------------------------------------------------------------

def test_a_planted_link_as_the_target_is_replaced_and_its_victim_stays_untouched(tmp_path):
    victim = tmp_path / 'victim.txt'
    victim.write_text('keep me', encoding='utf-8')
    state_dir(tmp_path).mkdir(parents=True)
    os.symlink(victim, state_dir(tmp_path) / 'abc-123_X.json')
    save(tmp_path)
    assert victim.read_text(encoding='utf-8') == 'keep me'
    target = state_dir(tmp_path) / 'abc-123_X.json'
    assert not target.is_symlink() and read(tmp_path)['used_tokens'] == 231_000


def test_a_planted_link_as_the_temporary_file_is_not_written_through(tmp_path):
    victim = tmp_path / 'victim.txt'
    victim.write_text('keep me', encoding='utf-8')
    state_dir(tmp_path).mkdir(parents=True)
    os.symlink(victim, state_dir(tmp_path) / f'abc-123_X.json.tmp.{os.getpid()}')
    save(tmp_path)
    assert victim.read_text(encoding='utf-8') == 'keep me' and read(tmp_path)['used_tokens'] == 231_000
    assert os.listdir(state_dir(tmp_path)) == ['abc-123_X.json']


def test_a_planted_link_as_the_folder_is_not_followed(tmp_path):
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (tmp_path / 'log').mkdir()
    os.symlink(elsewhere, state_dir(tmp_path))
    save(tmp_path)
    assert os.listdir(elsewhere) == []


def test_old_links_are_not_followed_when_cleaning_up(tmp_path):
    victim = tmp_path / 'victim.json'
    victim.write_text('keep me', encoding='utf-8')
    os.utime(victim, (NOW - 30 * DAY, NOW - 30 * DAY))
    state_dir(tmp_path).mkdir(parents=True)
    link = state_dir(tmp_path) / 'old-link.json'
    os.symlink(victim, link)
    os.utime(link, (NOW - 30 * DAY, NOW - 30 * DAY), follow_symlinks=False)
    save(tmp_path, now=NOW)
    assert victim.read_text(encoding='utf-8') == 'keep me'
    assert link.is_symlink()      # not a regular file: left alone


# --- Cleaning up -----------------------------------------------------------------------------------------------

def make_old(tmp_path, name, age_days, content='{}'):
    state_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    path = state_dir(tmp_path) / name
    path.write_text(content, encoding='utf-8')
    os.utime(path, (NOW - age_days * DAY, NOW - age_days * DAY))
    return path


def test_json_files_older_than_seven_days_are_removed_younger_ones_stay(tmp_path):
    old = make_old(tmp_path, 'old.json', 7.01)
    edge = make_old(tmp_path, 'edge.json', 7)
    young = make_old(tmp_path, 'young.json', 6.9)
    save(tmp_path, now=NOW)
    assert not old.exists() and edge.exists() and young.exists()
    assert (state_dir(tmp_path) / 'abc-123_X.json').exists()


def test_cleanup_touches_only_regular_json_files(tmp_path):
    other = make_old(tmp_path, 'notes.txt', 30)
    temp = make_old(tmp_path, 'x.json.tmp.1', 30)
    folder = state_dir(tmp_path) / 'sub.json'
    folder.mkdir()
    os.utime(folder, (NOW - 30 * DAY, NOW - 30 * DAY))
    save(tmp_path, now=NOW)
    assert other.exists() and temp.exists() and folder.is_dir()


def test_an_old_file_of_the_same_session_is_replaced_not_removed(tmp_path):
    make_old(tmp_path, 'abc-123_X.json', 30)
    save(tmp_path, now=NOW)
    assert read(tmp_path)['used_tokens'] == 231_000


def test_a_failing_cleanup_does_not_undo_or_stop_the_write(tmp_path, monkeypatch):
    def boom(*_a):
        raise OSError('no access')
    monkeypatch.setattr(_context_state, '_prune', boom)
    save(tmp_path)
    assert read(tmp_path)['used_tokens'] == 231_000


def test_a_file_that_cannot_be_inspected_is_skipped_and_the_others_are_still_cleaned(tmp_path, monkeypatch):
    stuck = make_old(tmp_path, 'a-stuck.json', 30)
    old = make_old(tmp_path, 'z-old.json', 30)
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if str(path).endswith('a-stuck.json'):
            raise PermissionError('no access')
        return real_lstat(path, *args, **kwargs)
    monkeypatch.setattr(os, 'lstat', lstat)
    save(tmp_path, now=NOW)
    assert stuck.exists() and not old.exists()


def test_the_file_just_written_survives_a_clock_far_ahead_of_the_file_system(tmp_path):
    save(tmp_path, now=time.time() + 100 * DAY)
    assert read(tmp_path)['used_tokens'] == 231_000


# --- A failing write ---------------------------------------------------------------------------------------------

def test_a_failing_replace_raises_and_leaves_no_temporary_file_and_no_half_written_state(tmp_path, monkeypatch):
    save(tmp_path, used=100_000)

    def boom(*_a):
        raise OSError('disk full')
    monkeypatch.setattr(os, 'replace', boom)
    with pytest.raises(OSError):
        save(tmp_path, used=200_000)
    monkeypatch.undo()
    assert os.listdir(state_dir(tmp_path)) == ['abc-123_X.json'] and read(tmp_path)['used_tokens'] == 100_000


def test_a_failing_open_raises_without_a_leftover(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise PermissionError('no access')
    monkeypatch.setattr(os, 'open', boom)
    with pytest.raises(OSError):
        save(tmp_path)
    monkeypatch.undo()
    assert os.listdir(state_dir(tmp_path)) == []


# --- The status line feeds it -----------------------------------------------------------------------------------

def status(session_id='sess-1', total=231_000, size=1_000_000):
    return {'session_id': session_id,
            'context_window': {'total_input_tokens': total, 'context_window_size': size, 'used_percentage': 23}}


def run_main(monkeypatch, capsys, status_input):
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(status_input)))
    assert statusline.main() == 0
    return capsys.readouterr().out


def files(tmp_path):
    folder = tmp_path / 'log' / 'context'
    return sorted(os.listdir(folder)) if folder.exists() else []


def test_the_status_line_writes_the_state_below_the_log_dir(monkeypatch, capsys, tmp_path):
    out = run_main(monkeypatch, capsys, status())
    assert 'ctx 231k/1000k' in out
    saved = json.loads((tmp_path / 'log' / 'context' / 'sess-1.json').read_text(encoding='utf-8'))
    assert saved['session_id'] == 'sess-1' and saved['used_tokens'] == 231_000
    assert saved['context_window_size'] == 1_000_000 and set(saved) == {'session_id', 'context_window_size',
                                                                         'used_tokens', 'measured_at'}


def test_the_state_follows_the_log_dir_of_the_config(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv('CCUC_LOG_DIR')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'log_dir': str(tmp_path / 'mylog')}), encoding='utf-8')
    monkeypatch.setenv('CCUC_CONFIG', str(config))
    run_main(monkeypatch, capsys, status())
    assert os.listdir(tmp_path / 'mylog' / 'context') == ['sess-1.json']


@pytest.mark.parametrize('bad', [
    status(total=0), status(total=None), status(size=None), status(size=0), status(total=True), status(total='5'),
    status(session_id='../x'), status(session_id=''), status(session_id='x' * 200), status(session_id=None),
    {'context_window': {'total_input_tokens': 5, 'context_window_size': 1_000_000}},    # no session_id
    {'session_id': 'sess-1'}, {'session_id': 'sess-1', 'context_window': None}])
def test_nothing_is_written_before_the_first_answer_or_without_valid_data_but_the_line_is_shown(
        monkeypatch, capsys, tmp_path, bad):
    out = run_main(monkeypatch, capsys, bad)
    assert out.strip() != '' and files(tmp_path) == []


def test_two_sessions_through_the_status_line_stay_apart(monkeypatch, capsys, tmp_path):
    run_main(monkeypatch, capsys, status('one', 111_000))
    run_main(monkeypatch, capsys, status('two', 222_000, 200_000))
    run_main(monkeypatch, capsys, status('one', 333_000))
    one = json.loads((tmp_path / 'log' / 'context' / 'one.json').read_text(encoding='utf-8'))
    two = json.loads((tmp_path / 'log' / 'context' / 'two.json').read_text(encoding='utf-8'))
    assert (one['used_tokens'], one['context_window_size']) == (333_000, 1_000_000)
    assert (two['used_tokens'], two['context_window_size']) == (222_000, 200_000)


def test_a_failing_state_write_never_disturbs_the_line(monkeypatch, capsys, tmp_path):
    def boom(*_a):
        raise OSError('disk full')
    monkeypatch.setattr(statusline._context_state, 'save', boom)
    out = run_main(monkeypatch, capsys, status())
    assert 'ctx 231k/1000k 23%' in out


def test_the_state_folder_cannot_be_created_and_the_line_still_shows(monkeypatch, capsys, tmp_path):
    (tmp_path / 'log').write_text('a file where the log folder should be', encoding='utf-8')
    out = run_main(monkeypatch, capsys, status())
    assert 'ctx 231k/1000k 23%' in out


def test_the_line_comes_first_and_the_measured_time_is_now(monkeypatch, capsys, tmp_path):
    before = time.time()
    run_main(monkeypatch, capsys, status())
    saved = json.loads((tmp_path / 'log' / 'context' / 'sess-1.json').read_text(encoding='utf-8'))
    assert saved['measured_at'] <= time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 1))
    assert saved['measured_at'] >= time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(before - 1))
