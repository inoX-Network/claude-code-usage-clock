"""Tests for hooks/_history.py: the measurement history that the status line appends to under the usage file's lock."""
import json
import os
import stat
import subprocess
import sys
import time

import pytest

import _history
import _usage
from conftest import HOOKS, lock_is_held

NOW = 1_800_000_000.0           # a fixed "now"; the blocks below reset after it
FIVE_RESET = NOW + 3600
WEEK_RESET = NOW + 86400
STATUSLINE = os.path.join(HOOKS, 'statusline.py')
NOT_ROOT = pytest.mark.skipif(hasattr(os, 'geteuid') and os.geteuid() == 0, reason='root can write everywhere')


def merged(five: object = 40, week: object = 61, five_reset: object = FIVE_RESET, week_reset: object = WEEK_RESET):
    """What _usage.update_file returns; None for a block that is missing."""
    return {'five_hour': {'used_percentage': five, 'resets_at': five_reset} if five is not None else None,
            'seven_day': {'used_percentage': week, 'resets_at': week_reset} if week is not None else None,
            'measured_at': '2027-01-15T08:00:00Z'}


def history(tmp_path):
    return tmp_path / 'log' / 'usage-history.jsonl'


def lines(tmp_path):
    path = history(tmp_path)
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def add(tmp_path, data, now=NOW, **kwargs):
    _history.append(str(tmp_path / 'log'), data, now, **kwargs)


# --- The line ---------------------------------------------------------------------------------------------------

def test_the_first_measurement_is_written_with_exactly_the_documented_fields(tmp_path):
    add(tmp_path, merged())
    assert lines(tmp_path) == [{'t': '2027-01-15T08:00:00Z', 'five': 40, 'week': 61, 'five_resets': FIVE_RESET,
                                'week_resets': WEEK_RESET}]


def test_the_line_holds_numbers_only_no_session_no_path(tmp_path):
    add(tmp_path, merged())
    line = lines(tmp_path)[0]
    assert list(line) == ['t', 'five', 'week', 'five_resets', 'week_resets']
    assert all(isinstance(v, (int, float)) for k, v in line.items() if k != 't')


def test_the_time_is_the_given_now_in_utc(tmp_path):
    add(tmp_path, merged(), now=1_768_381_200.0)
    assert lines(tmp_path)[0]['t'] == '2026-01-14T09:00:00Z'


def test_decimal_and_whole_numbers_are_taken_over_unchanged(tmp_path):
    add(tmp_path, merged(five=23.4, week=61))
    raw = history(tmp_path).read_text(encoding='utf-8')
    assert '"five":23.4' in raw and '"week":61,' in raw


def test_a_missing_block_is_null_and_the_other_is_still_written(tmp_path):
    add(tmp_path, merged(five=None))
    assert lines(tmp_path)[0]['five'] is None and lines(tmp_path)[0]['five_resets'] is None
    assert lines(tmp_path)[0]['week'] == 61


# --- Nothing valid, nothing written -----------------------------------------------------------------------------

@pytest.mark.parametrize('data', [
    {}, None, 'x', [],
    merged(five=None, week=None),
    merged(five=True, week=True),
    merged(five=float('nan'), week=float('inf')),
    merged(five='40', week='61'),
    merged(five_reset=NOW - 1, week_reset=NOW),             # windows that are over
    merged(five_reset=None, week_reset='soon'),
    {'five_hour': 'x', 'seven_day': 5},
])
def test_without_a_valid_block_nothing_is_written_and_no_file_is_created(tmp_path, data):
    add(tmp_path, data)
    assert not history(tmp_path).exists()


def test_a_block_that_is_over_is_written_as_null_next_to_a_valid_one(tmp_path):
    add(tmp_path, merged(five=90, five_reset=NOW - 5))
    assert lines(tmp_path)[0]['five'] is None and lines(tmp_path)[0]['week'] == 61


# --- When a line is written -------------------------------------------------------------------------------------

def test_an_unchanged_measurement_writes_nothing(tmp_path):
    add(tmp_path, merged())
    add(tmp_path, merged(), now=NOW + 60)
    add(tmp_path, merged(), now=NOW + 120)
    assert len(lines(tmp_path)) == 1


@pytest.mark.parametrize('change', [{'five': 41}, {'week': 62}, {'five': 40.5}, {'five_reset': FIVE_RESET + 18000},
                                    {'week_reset': WEEK_RESET + 604800}, {'five': 3, 'five_reset': FIVE_RESET + 18000}])
def test_a_change_of_a_value_or_a_reset_writes(tmp_path, change):
    add(tmp_path, merged())
    add(tmp_path, merged(**change), now=NOW + 30)
    assert len(lines(tmp_path)) == 2


def test_the_same_percentage_in_a_new_window_is_a_change(tmp_path):
    add(tmp_path, merged(five=0))
    add(tmp_path, merged(five=0, five_reset=FIVE_RESET + 18000), now=NOW + 30)
    assert [line['five_resets'] for line in lines(tmp_path)] == [FIVE_RESET, FIVE_RESET + 18000]


def test_a_whole_number_and_the_same_decimal_are_no_change(tmp_path):
    add(tmp_path, merged(five=40))
    add(tmp_path, merged(five=40.0), now=NOW + 30)
    assert len(lines(tmp_path)) == 1


def test_a_block_that_drops_out_is_a_change(tmp_path):
    add(tmp_path, merged())
    add(tmp_path, merged(five=None), now=NOW + 30)
    assert [line['five'] for line in lines(tmp_path)] == [40, None]


def test_the_heartbeat_writes_an_unchanged_measurement_once_the_last_line_is_old_enough(tmp_path):
    add(tmp_path, merged(), heartbeat_min=15)
    add(tmp_path, merged(), now=NOW + 15 * 60 - 1, heartbeat_min=15)
    assert len(lines(tmp_path)) == 1
    add(tmp_path, merged(), now=NOW + 15 * 60, heartbeat_min=15)
    assert [line['t'] for line in lines(tmp_path)] == ['2027-01-15T08:00:00Z', '2027-01-15T08:15:00Z']


def test_the_heartbeat_counts_from_the_last_line_not_from_the_first(tmp_path):
    add(tmp_path, merged(), heartbeat_min=10)
    add(tmp_path, merged(), now=NOW + 600, heartbeat_min=10)
    add(tmp_path, merged(), now=NOW + 1199, heartbeat_min=10)
    assert len(lines(tmp_path)) == 2
    add(tmp_path, merged(), now=NOW + 1200, heartbeat_min=10)
    assert len(lines(tmp_path)) == 3


def test_the_heartbeat_interval_comes_from_the_argument_default_15(tmp_path):
    add(tmp_path, merged())
    add(tmp_path, merged(), now=NOW + 14 * 60)
    assert len(lines(tmp_path)) == 1
    add(tmp_path, merged(), now=NOW + 15 * 60)
    assert len(lines(tmp_path)) == 2


def test_a_heartbeat_without_a_valid_block_writes_nothing(tmp_path):
    add(tmp_path, merged())
    add(tmp_path, merged(five=None, week=None), now=NOW + 3 * 3600)
    assert len(lines(tmp_path)) == 1


def test_a_last_line_from_the_future_does_not_block_the_heartbeat(tmp_path):
    add(tmp_path, merged(), now=NOW + 7200)
    add(tmp_path, merged(), now=NOW)
    assert len(lines(tmp_path)) == 2


def test_a_session_that_took_its_time_before_the_last_writer_does_not_write_the_measurement_again(tmp_path):
    """Two status lines started side by side: the later one gets the lock first and writes `t` cut to its second,
    the earlier one's now lies before that second (seen once in the CI, 19.10.2026). Less than FUTURE_SLACK_S
    ahead counts as now; from there on the line is from the future and the heartbeat writes."""
    add(tmp_path, merged(), now=NOW + 0.02)
    add(tmp_path, merged(), now=NOW - 0.02)
    add(tmp_path, merged(), now=NOW - _history.FUTURE_SLACK_S + 1)
    assert len(lines(tmp_path)) == 1
    add(tmp_path, merged(), now=NOW - _history.FUTURE_SLACK_S)
    assert len(lines(tmp_path)) == 2


@pytest.mark.parametrize('garbage', ['not json\n', '[1,2]\n', '{"t": "x", "five": 40}\n', '{"t": 5}\n', '\n'])
def test_an_unreadable_last_line_does_not_stop_the_history(tmp_path, garbage):
    (tmp_path / 'log').mkdir()
    history(tmp_path).write_text(garbage, encoding='utf-8')
    add(tmp_path, merged())
    assert lines_raw(tmp_path)[-1]['five'] == 40


def lines_raw(tmp_path):
    out = []
    for line in history(tmp_path).read_text(encoding='utf-8').splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def test_only_the_tail_is_read_and_a_long_file_still_gets_its_change_detection(tmp_path):
    for i in range(300):
        add(tmp_path, merged(five=i % 100, five_reset=FIVE_RESET + i), now=NOW + i, max_kb=1024)
    count = len(lines(tmp_path))
    add(tmp_path, merged(five=299 % 100, five_reset=FIVE_RESET + 299), now=NOW + 300)
    assert len(lines(tmp_path)) == count


def test_a_truncated_last_line_gets_a_line_break_so_the_new_line_stands_alone(tmp_path):
    (tmp_path / 'log').mkdir()
    history(tmp_path).write_text('{"t":"2027-01-15T07:00:00Z","five":3,"we', encoding='utf-8')
    add(tmp_path, merged())
    rows = history(tmp_path).read_text(encoding='utf-8').splitlines()
    assert rows[0] == '{"t":"2027-01-15T07:00:00Z","five":3,"we' and json.loads(rows[1])['five'] == 40 and len(rows) == 2


def test_complete_lines_get_no_extra_line_break(tmp_path):
    add(tmp_path, merged())
    add(tmp_path, merged(five=41), now=NOW + 30)
    raw = history(tmp_path).read_text(encoding='utf-8')
    assert '\n\n' not in raw and raw.count('\n') == 2 and raw.endswith('\n') and not raw.startswith('\n')


def test_an_empty_file_gets_no_line_break_either(tmp_path):
    (tmp_path / 'log').mkdir()
    history(tmp_path).write_text('', encoding='utf-8')
    add(tmp_path, merged())
    assert not history(tmp_path).read_text(encoding='utf-8').startswith('\n') and len(lines(tmp_path)) == 1


# --- Rotation ---------------------------------------------------------------------------------------------------

def fill(tmp_path, size, text='x'):
    (tmp_path / 'log').mkdir(exist_ok=True)
    history(tmp_path).write_text(text * (size - 1) + '\n', encoding='utf-8', newline='')      # exact size, no CRLF


def test_a_file_exactly_at_the_limit_is_not_rotated(tmp_path):
    fill(tmp_path, 1024)
    add(tmp_path, merged(), max_kb=1)
    assert not (tmp_path / 'log' / 'usage-history.jsonl.1').exists()
    assert lines_raw(tmp_path)[-1]['five'] == 40


def test_a_file_above_the_limit_becomes_dot_one_and_a_new_file_starts_with_the_new_line(tmp_path):
    fill(tmp_path, 1025)
    add(tmp_path, merged(), max_kb=1)
    old = tmp_path / 'log' / 'usage-history.jsonl.1'
    assert old.read_text(encoding='utf-8') == 'x' * 1024 + '\n'
    assert len(lines(tmp_path)) == 1


def test_the_previous_dot_one_is_replaced(tmp_path):
    old = tmp_path / 'log' / 'usage-history.jsonl.1'
    fill(tmp_path, 2000, 'a')
    old.write_text('older', encoding='utf-8')
    add(tmp_path, merged(), max_kb=1)
    assert old.read_text(encoding='utf-8') == 'a' * 1999 + '\n'


def test_the_size_limit_comes_from_the_argument_default_1024_kb(tmp_path):
    fill(tmp_path, 1024 * 1024)
    add(tmp_path, merged())
    assert not (tmp_path / 'log' / 'usage-history.jsonl.1').exists()
    add(tmp_path, merged(five=41))
    assert (tmp_path / 'log' / 'usage-history.jsonl.1').exists()


def test_an_unchanged_measurement_does_not_rotate(tmp_path):
    add(tmp_path, merged())
    with open(history(tmp_path), 'a', encoding='utf-8') as f:
        f.write('')
    add(tmp_path, merged(), now=NOW + 1, max_kb=0.05)     # below the file size, but nothing to write
    assert not (tmp_path / 'log' / 'usage-history.jsonl.1').exists()


# --- Permissions ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('mask', [0o022, 0o002, 0o000])
@pytest.mark.posix_only
def test_the_file_is_private_and_so_are_the_folders(tmp_path, mask):
    old = os.umask(mask)
    try:
        add(tmp_path, merged())
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(history(tmp_path)).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / 'log').st_mode) == 0o700


@pytest.mark.posix_only
def test_a_file_that_was_readable_is_made_private_on_the_next_write(tmp_path):
    add(tmp_path, merged())
    os.chmod(history(tmp_path), 0o644)
    add(tmp_path, merged(five=50))
    assert stat.S_IMODE(os.stat(history(tmp_path)).st_mode) == 0o600


@pytest.mark.posix_only
def test_the_log_folder_above_a_new_folder_is_private_too(tmp_path):
    old = os.umask(0o022)
    try:
        _history.append(str(tmp_path / 'cache' / 'log'), merged(), NOW)
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(tmp_path / 'cache').st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / 'cache' / 'log').st_mode) == 0o700


# --- No links are followed --------------------------------------------------------------------------------------

@pytest.mark.posix_only
def test_a_planted_link_as_the_file_is_never_written_through(tmp_path):
    victim = tmp_path / 'victim.txt'
    victim.write_text('keep me', encoding='utf-8')
    (tmp_path / 'log').mkdir()
    os.symlink(victim, history(tmp_path))
    add(tmp_path, merged())
    assert victim.read_text(encoding='utf-8') == 'keep me'


@pytest.mark.posix_only
def test_a_planted_link_as_the_folder_is_not_followed(tmp_path):
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    os.symlink(elsewhere, tmp_path / 'log')
    add(tmp_path, merged())
    assert os.listdir(elsewhere) == []


@pytest.mark.posix_only
def test_a_planted_link_as_dot_one_is_replaced_not_written_through(tmp_path):
    victim = tmp_path / 'victim.txt'
    victim.write_text('keep me', encoding='utf-8')
    fill(tmp_path, 2000)
    os.symlink(victim, tmp_path / 'log' / 'usage-history.jsonl.1')
    add(tmp_path, merged(), max_kb=1)
    assert victim.read_text(encoding='utf-8') == 'keep me'
    assert not (tmp_path / 'log' / 'usage-history.jsonl.1').is_symlink()


# --- Errors are swallowed ---------------------------------------------------------------------------------------

def test_a_file_where_the_folder_should_be_is_swallowed(tmp_path):
    blocker = tmp_path / 'blocker'
    blocker.write_text('x', encoding='utf-8')
    _history.append(str(blocker / 'log'), merged(), NOW)
    assert blocker.read_text(encoding='utf-8') == 'x'


def test_a_directory_as_the_file_is_swallowed(tmp_path):
    (tmp_path / 'log' / 'usage-history.jsonl').mkdir(parents=True)
    add(tmp_path, merged())


@NOT_ROOT
@pytest.mark.posix_only
def test_an_unwritable_folder_is_swallowed(tmp_path):
    (tmp_path / 'log').mkdir(mode=0o700)
    os.chmod(tmp_path / 'log', 0o500)
    try:
        add(tmp_path, merged())
    finally:
        os.chmod(tmp_path / 'log', 0o700)
    assert not history(tmp_path).exists()


@pytest.mark.parametrize('now', [float('nan'), float('inf'), None, 'x', 1e30])
def test_a_broken_time_is_swallowed(tmp_path, now):
    add(tmp_path, merged(), now=now)


@pytest.mark.parametrize('kwargs', [{'heartbeat_min': float('nan')}, {'max_kb': 'x'}, {'heartbeat_min': None}])
def test_broken_limits_are_swallowed(tmp_path, kwargs):
    add(tmp_path, merged(), **kwargs)


# --- Under the lock of the usage file ---------------------------------------------------------------------------

def test_the_callback_runs_after_the_merge_with_the_merged_content_and_while_the_lock_is_held(tmp_path):
    seen = []

    def after(content):
        assert lock_is_held(str(tmp_path / 'log' / 'usage.json.lock'))
        seen.append((content, (tmp_path / 'usage.json').exists()))

    status = {'rate_limits': {'five_hour': {'used_percentage': 40, 'resets_at': FIVE_RESET},
                              'seven_day': {'used_percentage': 61, 'resets_at': WEEK_RESET}}}
    result = _usage.update_file(str(tmp_path / 'usage.json'), status, NOW, str(tmp_path / 'log'), after)
    assert len(seen) == 1 and seen[0][0] == result and seen[0][1] is True
    assert result['five_hour']['used_percentage'] == 40


def test_the_callback_also_runs_when_the_file_does_not_change(tmp_path):
    calls = []
    status = {'rate_limits': {'five_hour': {'used_percentage': 40, 'resets_at': FIVE_RESET}}}
    for _ in range(2):
        _usage.update_file(str(tmp_path / 'usage.json'), status, NOW, str(tmp_path / 'log'), calls.append)
    assert len(calls) == 2 and calls[0] == calls[1]


def test_the_callback_does_not_run_if_the_usage_file_cannot_be_written(tmp_path):
    blocker = tmp_path / 'blocker'
    blocker.write_text('x', encoding='utf-8')
    calls = []
    status = {'rate_limits': {'five_hour': {'used_percentage': 40, 'resets_at': FIVE_RESET}}}
    with pytest.raises(OSError):
        _usage.update_file(str(blocker / 'usage.json'), status, NOW, str(tmp_path / 'log'), calls.append)
    assert calls == []


def test_a_failing_callback_does_not_break_the_merge(tmp_path):
    def boom(_content):
        raise RuntimeError('boom')
    status = {'rate_limits': {'five_hour': {'used_percentage': 40, 'resets_at': FIVE_RESET}}}
    result = _usage.update_file(str(tmp_path / 'usage.json'), status, NOW, str(tmp_path / 'log'), boom)
    assert result['five_hour']['used_percentage'] == 40
    assert json.loads((tmp_path / 'usage.json').read_text(encoding='utf-8'))['five_hour']['used_percentage'] == 40


def test_parallel_sessions_write_one_line_for_one_measurement(tmp_path):
    """Eight status lines at once with the same numbers: the lock makes the first one write, the others see its line."""
    now = time.time()
    status = json.dumps({'rate_limits': {'five_hour': {'used_percentage': 40, 'resets_at': now + 3600},
                                         'seven_day': {'used_percentage': 61, 'resets_at': now + 86400}}})
    procs = [subprocess.Popen([sys.executable, STATUSLINE], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True, encoding='utf-8')
             for _ in range(8)]
    for proc in procs:
        proc.stdin.write(status)
        proc.stdin.close()
    assert [proc.wait(timeout=30) for proc in procs] == [0] * 8
    assert len(lines(tmp_path)) == 1


def test_parallel_sessions_with_growing_numbers_leave_each_value_once(tmp_path):
    now = time.time()
    procs = []
    for value in range(1, 9):
        status = json.dumps({'rate_limits': {'five_hour': {'used_percentage': value, 'resets_at': now + 3600}}})
        proc = subprocess.Popen([sys.executable, STATUSLINE], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True, encoding='utf-8')
        proc.stdin.write(status)
        proc.stdin.close()
        procs.append(proc)
    assert [proc.wait(timeout=30) for proc in procs] == [0] * 8
    values = [line['five'] for line in lines(tmp_path)]
    assert len(values) == len(set(values)) and values == sorted(values)
    assert values[-1] == 8
