"""Tests for hooks/_usage.py: block validity, merge rules, locked writing and reading."""
import json
import os
import random
import threading
from datetime import datetime, timedelta, timezone

import pytest

import _usage

NOW = 1768384800.0            # 2026-01-14 10:00:00 UTC
WINDOW_5H = int(NOW) + 3600   # reset 11:00 UTC
WINDOW_7D = int(NOW) + 5 * 86400
NOW_DT = datetime.fromtimestamp(NOW, timezone.utc)


def b(percent, reset):
    return {'used_percentage': percent, 'resets_at': reset}


def write_json(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding='utf-8')
    return str(path)


def iso(dt):
    return dt.isoformat().replace('+00:00', 'Z')


# --- Validity and merge rules ------------------------------------------------------------

def test_same_window_higher_value_wins():
    assert _usage.pick(b(48, WINDOW_7D), b(19, WINDOW_7D), NOW) == (b(48, WINDOW_7D), False)
    assert _usage.pick(b(48, WINDOW_7D), b(49, WINDOW_7D), NOW) == (b(49, WINDOW_7D), True)
    assert _usage.pick(b(48, WINDOW_7D), b(48, WINDOW_7D + 30), NOW)[1] is True   # equal counts as fresh
    assert _usage.pick(b(48, WINDOW_7D), b(48, WINDOW_7D + 59), NOW)[1] is True   # still the same window
    assert _usage.pick(b(48, WINDOW_7D), b(10, WINDOW_7D + 60), NOW) == (b(10, WINDOW_7D + 60), True)  # 60 s = later


def test_later_window_wins_even_with_a_lower_value():
    assert _usage.pick(b(82, WINDOW_5H), b(0, WINDOW_5H + 18000), NOW) == (b(0, WINDOW_5H + 18000), True)
    assert _usage.pick(b(0, WINDOW_5H + 18000), b(82, WINDOW_5H), NOW) == (b(0, WINDOW_5H + 18000), False)


@pytest.mark.parametrize('new', [None, {}, b(None, WINDOW_5H), b(50, NOW - 1), b(50, NOW), b(True, WINDOW_5H),
                                 b(50, True), b('50', WINDOW_5H), b(50, str(WINDOW_5H)), b(float('nan'), WINDOW_5H),
                                 b(float('inf'), WINDOW_5H), b(50, float('inf')), 'broken', 7, [1]])
def test_invalid_new_block_is_ignored(new):
    assert _usage.pick(b(79, WINDOW_5H), new, NOW) == (b(79, WINDOW_5H), False)


def test_expired_old_block_is_replaced_or_dropped():
    assert _usage.pick(b(82, NOW - 5), b(3, WINDOW_5H), NOW) == (b(3, WINDOW_5H), True)
    assert _usage.pick(b(82, NOW - 5), None, NOW) == (None, False)
    assert _usage.pick(None, None, NOW) == (None, False)
    assert _usage.pick('junk', b(3, WINDOW_5H), NOW) == (b(3, WINDOW_5H), True)


def test_measured_at_only_renewed_by_an_own_fresh_five_hour_value():
    old = {'five_hour': b(79, WINDOW_5H), 'seven_day': b(48, WINDOW_7D), 'measured_at': 'OLD'}
    idle = {'rate_limits': {'five_hour': None, 'seven_day': b(19, WINDOW_7D)}}
    assert _usage.merge(old, idle, NOW) == old
    active = {'rate_limits': {'five_hour': b(80, WINDOW_5H), 'seven_day': b(48, WINDOW_7D)}}
    merged = _usage.merge(old, active, NOW)
    assert merged['five_hour'] == b(80, WINDOW_5H) and merged['measured_at'] == '2026-01-14T10:00:00Z'
    assert _usage.merge(old, {}, NOW) == old
    # An active session that does not beat the stored value (lower, same window) does not renew it either
    lower = {'rate_limits': {'five_hour': b(70, WINDOW_5H)}}
    assert _usage.merge(old, lower, NOW) == old


def test_merge_survives_garbage_input_and_garbage_old_content():
    assert _usage.merge({}, {'rate_limits': 'x'}, NOW) == {'five_hour': None, 'seven_day': None, 'measured_at': None}
    assert _usage.merge({'measured_at': 5, 'five_hour': 'x'}, {}, NOW)['measured_at'] is None


# --- Writing: lock, atomic, only on change ---------------------------------------------------

def test_update_file_creates_the_file_and_its_folder(tmp_path):
    path = str(tmp_path / 'deep' / 'er' / 'usage.json')
    merged = _usage.update_file(path, {'rate_limits': {'five_hour': b(40, WINDOW_5H)}}, NOW)
    assert json.load(open(path, encoding='utf-8')) == merged
    assert merged['five_hour'] == b(40, WINDOW_5H) and merged['measured_at'] == '2026-01-14T10:00:00Z'


def test_update_file_writes_only_on_change(tmp_path):
    path = tmp_path / 'usage.json'
    status = {'rate_limits': {'five_hour': b(40, WINDOW_5H), 'seven_day': b(60, WINDOW_7D)}}
    _usage.update_file(str(path), status, NOW)
    first = path.read_text(encoding='utf-8')
    os.utime(path, (1, 1))
    _usage.update_file(str(path), {'rate_limits': {'five_hour': None, 'seven_day': b(10, WINDOW_7D)}}, NOW + 5)  # idle
    _usage.update_file(str(path), {}, NOW + 6)
    assert path.read_text(encoding='utf-8') == first and os.stat(path).st_mtime == 1
    _usage.update_file(str(path), {'rate_limits': {'five_hour': b(41, WINDOW_5H)}}, NOW + 7)
    assert path.read_text(encoding='utf-8') != first


def test_update_file_leaves_no_temporary_files(tmp_path):
    path = str(tmp_path / 'usage.json')
    for i in range(5):
        _usage.update_file(path, {'rate_limits': {'five_hour': b(10 + i, WINDOW_5H)}}, NOW + i)
    assert sorted(os.listdir(tmp_path)) == ['usage.json', 'usage.json.lock']


def test_update_file_cleans_up_when_replace_fails(tmp_path, monkeypatch):
    path = str(tmp_path / 'usage.json')

    def broken(*_a):
        raise OSError('disk')
    monkeypatch.setattr(os, 'replace', broken)
    with pytest.raises(OSError):
        _usage.update_file(path, {'rate_limits': {'five_hour': b(10, WINDOW_5H)}}, NOW)
    assert not [p for p in os.listdir(tmp_path) if '.tmp.' in p] and not os.path.exists(path)


def test_update_file_reports_an_error_if_the_temporary_file_cannot_be_created(tmp_path):
    path = str(tmp_path / 'usage.json')
    os.mkdir(f'{path}.tmp.{os.getpid()}')      # a folder occupies the temporary name: creating and removing fail
    with pytest.raises(OSError):
        _usage.update_file(path, {'rate_limits': {'five_hour': b(10, WINDOW_5H)}}, NOW)
    assert not os.path.exists(path)


def test_update_file_raises_oserror_if_the_folder_is_not_writable(tmp_path):
    locked = tmp_path / 'locked'
    locked.mkdir()
    locked.chmod(0o500)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip('running as a user that ignores directory permissions')
        with pytest.raises(OSError):
            _usage.update_file(str(locked / 'usage.json'), {'rate_limits': {'five_hour': b(1, WINDOW_5H)}}, NOW)
    finally:
        locked.chmod(0o700)


@pytest.mark.parametrize('content', ['{broken', '[1, 2]', '"text"', '', json.dumps({'five_hour': 'x'}),
                                     json.dumps({'fuenf': b(90, WINDOW_5H), 'measured': 'old format'})])
def test_file_in_another_format_counts_as_empty(tmp_path, content):
    path = write_json(tmp_path / 'usage.json', content)
    merged = _usage.update_file(path, {'rate_limits': {'five_hour': b(5, WINDOW_5H)}}, NOW)
    assert merged['five_hour'] == b(5, WINDOW_5H)
    assert _usage.read_usage(path, NOW_DT).state == 'ok'


def test_concurrent_writers_end_with_the_maximum(tmp_path):
    path = str(tmp_path / 'usage.json')
    errors = []

    def worker(offset):
        try:
            for i in range(40):
                _usage.update_file(path, {'rate_limits': {'five_hour': b(offset + i, WINDOW_5H)}}, NOW)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k * 3,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert json.load(open(path, encoding='utf-8'))['five_hour'] == b(15 + 39, WINDOW_5H)
    assert sorted(os.listdir(tmp_path)) == ['usage.json', 'usage.json.lock']


# --- Several sessions, one file (synthetic replacement for a recorded observation) ---------------

def test_many_sessions_writing_alternately_never_read_unknown_or_stale(tmp_path):
    """650 writes by three sessions: an active one with a rising value, an idle one with an old weekly value
    and no five-hour block, and an active one whose numbers are off by +-2 points. After every write:
    never 'unknown', never an old value, inside the window always the maximum, and after the window reset
    the new, smaller value wins."""
    rng = random.Random(20260114)
    path = str(tmp_path / 'usage.json')
    reset_1, reset_2 = int(NOW) + 2500, int(NOW) + 2500 + 18000
    step_s, reset_step, total = 5, 500, 650
    a_writes, expected_five, expected_week = 0, None, None
    idle_writes = offset_rollbacks = 0
    max_before_reset = value_after_reset = None

    for step in range(total):
        now = NOW + step * step_s
        if step == reset_step:
            max_before_reset, expected_five, a_writes = expected_five, None, 0   # the new window starts from scratch
        after_reset = step >= reset_step
        window = reset_2 if after_reset else reset_1
        true_week = 48 if step < 250 else 49
        who = 'A' if step in (0, reset_step) else rng.choice('ABC')   # an active session always opens a window
        if who == 'A':
            base = (3 if after_reset else 10) + 0.15 * a_writes
            a_writes += 1
            status = {'rate_limits': {'five_hour': b(round(base, 2), window), 'seven_day': b(true_week, WINDOW_7D)}}
            reported_five, reported_week = round(base, 2), true_week
        elif who == 'B':
            idle_writes += 1
            status = {'rate_limits': {'five_hour': None, 'seven_day': b(19, WINDOW_7D)}}
            reported_five, reported_week = None, 19
        else:
            lagging = after_reset and rng.random() < 0.3   # still shows the window that has just ended
            base = (3 if after_reset else 10) + 0.15 * a_writes
            skewed = max(0.0, round(base + rng.choice((-2, 2)), 2))
            offset_rollbacks += skewed < base
            if lagging:
                status = {'rate_limits': {'five_hour': b(max_before_reset, reset_1), 'seven_day': b(true_week, WINDOW_7D)}}
                reported_five = None   # an ended window is invalid and must not count
            else:
                status = {'rate_limits': {'five_hour': b(skewed, window), 'seven_day': b(true_week, WINDOW_7D)}}
                reported_five = skewed
            reported_week = true_week

        if reported_five is not None:
            expected_five = reported_five if expected_five is None else max(expected_five, reported_five)
        expected_week = reported_week if expected_week is None else max(expected_week, reported_week)

        _usage.update_file(path, status, now)
        usage = _usage.read_usage(path, datetime.fromtimestamp(now, timezone.utc), max_age_min=15)

        assert usage.state == 'ok', (step, who, usage)
        assert usage.five_hour == expected_five, (step, who)   # maximum inside the window, never an older value
        assert usage.week == expected_week and usage.week >= 48, (step, who)
        assert usage.age_min <= 15
        if step == reset_step:
            value_after_reset = usage.five_hour

    assert idle_writes > 100 and offset_rollbacks > 50           # the stream really contains the problem cases
    assert value_after_reset < max_before_reset - 20            # after the reset the smaller value won
    assert json.load(open(path, encoding='utf-8'))['seven_day'] == b(49, WINDOW_7D)


def test_idle_sessions_alone_cannot_keep_the_measurement_fresh(tmp_path):
    path = str(tmp_path / 'usage.json')
    _usage.update_file(path, {'rate_limits': {'five_hour': b(60, WINDOW_5H), 'seven_day': b(48, WINDOW_7D)}}, NOW)
    idle = {'rate_limits': {'five_hour': None, 'seven_day': b(19, WINDOW_7D)}}
    for minute in range(1, 31):     # only an idle session keeps redrawing its status line
        now = NOW + minute * 60
        _usage.update_file(path, idle, now)
        usage = _usage.read_usage(path, datetime.fromtimestamp(now, timezone.utc), max_age_min=15)
        assert usage.state == ('ok' if minute <= 15 else 'stale'), minute
        assert usage.five_hour == 60.0 and usage.week == 48.0


# --- Reading ------------------------------------------------------------------------------

def measured(minutes_ago):
    return iso(NOW_DT - timedelta(minutes=minutes_ago))


def usage_file(tmp_path, five=42, week=23, minutes_ago=2, **extra):
    data = {'five_hour': None if five is None else b(five, WINDOW_5H),
            'seven_day': None if week is None else b(week, WINDOW_7D), 'measured_at': measured(minutes_ago)}
    data.update(extra)
    return write_json(tmp_path / 'usage.json', data)


def test_fresh_measurement(tmp_path):
    u = _usage.read_usage(usage_file(tmp_path), NOW_DT)
    assert (u.five_hour, u.week, u.state) == (42.0, 23.0, 'ok')
    assert u.age_min == pytest.approx(2)


def test_reset_times_only_for_valid_blocks(tmp_path):
    path = write_json(tmp_path / 'usage.json', {'five_hour': b(42, WINDOW_5H), 'seven_day': b(23, NOW - 60),
                                                'measured_at': measured(2)})
    u = _usage.read_usage(path, NOW_DT)
    assert (u.five_hour_reset, u.week_reset, u.week) == (float(WINDOW_5H), None, None)
    for broken in (True, str(WINDOW_5H), None):
        path = write_json(tmp_path / 'usage.json', {'five_hour': b(42, broken), 'measured_at': measured(2)})
        assert _usage.read_usage(path, NOW_DT).five_hour_reset is None


def test_a_block_without_reset_time_is_not_valid(tmp_path):
    path = write_json(tmp_path / 'usage.json', {'five_hour': {'used_percentage': 42}, 'measured_at': measured(2)})
    assert _usage.read_usage(path, NOW_DT).state == 'unknown'


def test_null_right_after_a_reset_is_unknown(tmp_path):
    u = _usage.read_usage(usage_file(tmp_path, five=None), NOW_DT)
    assert u.five_hour is None and u.week == 23.0 and u.state == 'unknown'


def test_old_measurement_is_stale_and_the_limit_is_configurable(tmp_path):
    path = usage_file(tmp_path, minutes_ago=40)
    assert _usage.read_usage(path, NOW_DT).state == 'stale'
    assert _usage.read_usage(path, NOW_DT, max_age_min=60).state == 'ok'
    path = usage_file(tmp_path, minutes_ago=15)
    assert _usage.read_usage(path, NOW_DT, max_age_min=15).state == 'ok'          # at the limit is still fine
    assert _usage.read_usage(usage_file(tmp_path, minutes_ago=15.5), NOW_DT, max_age_min=15).state == 'stale'


def test_default_max_age_comes_from_the_config(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'thresholds': {'max_age_min': 5}}), encoding='utf-8')
    monkeypatch.setenv('CCUC_CONFIG', str(config))
    assert _usage.read_usage(usage_file(tmp_path, minutes_ago=6), NOW_DT).state == 'stale'
    assert _usage.read_usage(usage_file(tmp_path, minutes_ago=4), NOW_DT).state == 'ok'


def test_missing_and_broken_files_are_unknown(tmp_path):
    assert _usage.read_usage(str(tmp_path / 'missing.json'), NOW_DT).state == 'unknown'
    for content in ('{broken', '[1]', '', 'null'):
        assert _usage.read_usage(write_json(tmp_path / 'usage.json', content), NOW_DT).state == 'unknown'


def test_unparsable_or_naive_timestamps_are_unknown(tmp_path):
    for stamp in ('yesterday', '', None, 5, '2026-01-14T09:58:00'):   # the last one has no time zone
        path = write_json(tmp_path / 'usage.json', {'five_hour': b(42, WINDOW_5H), 'measured_at': stamp})
        u = _usage.read_usage(path, NOW_DT)
        assert (u.five_hour, u.state) == (None, 'unknown'), stamp


def test_timestamp_from_the_future_is_stale(tmp_path):
    path = write_json(tmp_path / 'usage.json', {'five_hour': b(10, WINDOW_5H), 'seven_day': b(5, WINDOW_7D),
                                                'measured_at': iso(NOW_DT + timedelta(days=1))})
    assert _usage.read_usage(path, NOW_DT).state == 'stale'
    slightly = write_json(tmp_path / 'usage.json', {'five_hour': b(10, WINDOW_5H),
                                                   'measured_at': iso(NOW_DT + timedelta(seconds=30))})
    assert _usage.read_usage(slightly, NOW_DT).state == 'ok'        # clock skew below one minute is tolerated


def test_ended_window_is_unknown(tmp_path):
    ended, running = NOW - 300, NOW + 7200
    path = write_json(tmp_path / 'usage.json', {'five_hour': b(90, ended), 'seven_day': b(5, running),
                                                'measured_at': measured(1)})
    assert _usage.read_usage(path, NOW_DT).state == 'unknown'
    path = write_json(tmp_path / 'usage.json', {'five_hour': b(90, running), 'measured_at': measured(1)})
    assert _usage.read_usage(path, NOW_DT).five_hour == 90.0


def test_file_in_the_old_format_reads_as_unknown(tmp_path):
    path = write_json(tmp_path / 'usage.json', {'fuenf': b(50, WINDOW_5H), 'measured': measured(1)})
    assert _usage.read_usage(path, NOW_DT).state == 'unknown'


def test_default_path_and_env_override(tmp_path, monkeypatch):
    assert _usage.usage_path() == str(tmp_path / 'usage.json')
    monkeypatch.delenv('CCUC_USAGE_FILE')
    assert _usage.usage_path() == str(tmp_path / 'home' / '.claude' / 'rate-limit.json')
    monkeypatch.setenv('CCUC_USAGE_FILE', '~/x/u.json')
    assert _usage.usage_path() == str(tmp_path / 'home' / 'x' / 'u.json')


def test_read_usage_uses_the_env_path_by_default(tmp_path):
    usage_file(tmp_path)
    u = _usage.read_usage(now=NOW_DT)
    assert u.five_hour == 42.0 and u.state == 'ok'
    assert _usage.read_usage().state in ('stale', 'unknown')   # real clock: the synthetic timestamp is long past
