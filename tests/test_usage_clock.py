"""Tests for hooks/usage_clock.py: line format, the two display switches, robustness, the real hook process.

Every test through the real script checks the exact output key: a wrong key would silently do nothing in
Claude Code.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import _config
import _usage
import usage_clock
from conftest import HOOKS

SCRIPT = os.path.join(HOOKS, 'usage_clock.py')
UTC = timezone.utc
# Wed 14 Jan 2026, 10:05 UTC. Jan 19 is a Monday, Jan 5 a Monday.
NOW = datetime(2026, 1, 14, 10, 5, tzinfo=UTC)
CFG = _config.DEFAULTS


def ts(*args, tz=UTC):
    return datetime(*args, tzinfo=tz).timestamp()


def usage(five=40.0, week=61.0, age=0.0, five_reset=None, week_reset=None, max_age=15.0):
    return _usage.Usage(five, week, age, five_reset, week_reset, max_age)


def write_usage(path, five=42, week=23, age_min=1, five_in_h=2, week_in_h=100, measured_at=None, five_block=True):
    now = datetime.now(UTC)
    data = {'five_hour': {'used_percentage': five, 'resets_at': (now + timedelta(hours=five_in_h)).timestamp()}
            if five_block else None,
            'seven_day': {'used_percentage': week, 'resets_at': (now + timedelta(hours=week_in_h)).timestamp()},
            'measured_at': measured_at or (now - timedelta(minutes=age_min)).strftime('%Y-%m-%dT%H:%M:%SZ')}
    path.write_text(json.dumps(data), encoding='utf-8')


def run_raw(script, stdin, env_extra=None):
    """Runs a hook script as a process. Returns (exit code, stdout text, stderr text)."""
    env = dict(os.environ, **(env_extra or {}))
    r = subprocess.run([sys.executable, str(script)], input=stdin.encode('utf-8'), capture_output=True, env=env,
                       timeout=20)
    return r.returncode, r.stdout.decode('utf-8'), r.stderr.decode('utf-8')


def run_hook(env_extra=None, stdin='{"hook_event_name": "UserPromptSubmit", "prompt": "hello"}'):
    """The context text of the hook, or None when it printed nothing. Asserts exit 0 and the exact key layout."""
    code, out, err = run_raw(SCRIPT, stdin, env_extra)
    assert code == 0, err
    if not out.strip():
        return None
    data = json.loads(out)
    assert list(data) == ['hookSpecificOutput']
    assert data['hookSpecificOutput']['hookEventName'] == 'UserPromptSubmit'
    assert list(data['hookSpecificOutput']) == ['hookEventName', 'additionalContext']
    return data['hookSpecificOutput']['additionalContext']


def write_config(tmp_path, content):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(content), encoding='utf-8')
    return {'CCUC_CONFIG': str(path)}


# --- The line with fixed times -------------------------------------------------------------------

def test_line_matches_the_documented_example():
    u = usage(40, 61, 0, ts(2026, 1, 14, 14, 0), ts(2026, 1, 19, 9, 0))
    assert usage_clock.usage_text(u, 75, NOW) + ' · ' + usage_clock.time_text(NOW) == (
        'Usage: 5h 40 % (resets 14:00) · week 61 % (resets Mon 19 Jan 09:00) · measured 0 min ago · '
        'Now: Wed 14 Jan 2026 10:05')


def test_reset_today_shows_only_the_time_other_days_weekday_and_date():
    assert usage_clock.format_reset(ts(2026, 1, 14, 23, 59), NOW) == '23:59'
    assert usage_clock.format_reset(ts(2026, 1, 15, 0, 0), NOW) == 'Thu 15 Jan 00:00'
    assert usage_clock.format_reset(ts(2026, 1, 5, 9, 0), NOW) == 'Mon 5 Jan 09:00'   # day without leading zero


def test_reset_from_another_year_and_month_is_not_taken_for_today():
    assert usage_clock.format_reset(ts(2027, 1, 14, 14, 0), NOW) == 'Thu 14 Jan 14:00'
    assert usage_clock.format_reset(ts(2026, 2, 14, 14, 0), NOW) == 'Sat 14 Feb 14:00'


@pytest.mark.parametrize('day,name', [(5, 'Mon'), (6, 'Tue'), (7, 'Wed'), (8, 'Thu'), (9, 'Fri'), (10, 'Sat'),
                                      (11, 'Sun')])
def test_weekday_names_are_english_abbreviations(day, name):
    late = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert usage_clock.format_reset(ts(2026, 1, day, 9, 0), late).startswith(f'{name} {day} Jan')


@pytest.mark.parametrize('month,name', [(1, 'Jan'), (2, 'Feb'), (3, 'Mar'), (4, 'Apr'), (5, 'May'), (6, 'Jun'),
                                        (7, 'Jul'), (8, 'Aug'), (9, 'Sep'), (10, 'Oct'), (11, 'Nov'), (12, 'Dec')])
def test_month_names_are_english_abbreviations_in_reset_and_clock(month, name):
    moment = datetime(2026, month, 15, 12, 0, tzinfo=UTC)
    assert f' 15 {name} 12:00' in usage_clock.format_reset(moment.timestamp(), datetime(2025, 6, 1, tzinfo=UTC))
    assert f' 15 {name} 2026 12:00' in usage_clock.time_text(moment)


def test_clock_names_the_weekday_for_every_day_of_the_week():
    names = [usage_clock.time_text(datetime(2026, 1, day, 8, 30, tzinfo=UTC)) for day in range(5, 12)]
    assert names == ['Now: Mon 5 Jan 2026 08:30', 'Now: Tue 6 Jan 2026 08:30', 'Now: Wed 7 Jan 2026 08:30',
                     'Now: Thu 8 Jan 2026 08:30', 'Now: Fri 9 Jan 2026 08:30', 'Now: Sat 10 Jan 2026 08:30',
                     'Now: Sun 11 Jan 2026 08:30']


def test_reset_times_use_the_time_zone_of_the_clock_not_utc():
    plus2 = timezone(timedelta(hours=2))
    now = datetime(2026, 1, 14, 22, 30, tzinfo=plus2)                      # 20:30 UTC
    assert usage_clock.format_reset(ts(2026, 1, 14, 21, 30), now) == '23:30'          # still today locally
    assert usage_clock.format_reset(ts(2026, 1, 14, 22, 30), now) == 'Thu 15 Jan 00:30'   # tomorrow locally
    assert usage_clock.time_text(now) == 'Now: Wed 14 Jan 2026 22:30'


def test_missing_reset_times_leave_the_values_bare_and_a_missing_week_is_a_question_mark():
    assert usage_clock.usage_text(usage(40, 61, 3), 75, NOW) == 'Usage: 5h 40 % · week 61 % · measured 3 min ago'
    assert usage_clock.usage_text(usage(40, None, 1), 75, NOW) == 'Usage: 5h 40 % · week ? · measured 1 min ago'


def test_percentages_and_age_are_rounded_to_whole_numbers():
    assert usage_clock.usage_text(usage(40.6, 61.4, 2.4), 75, NOW) == 'Usage: 5h 41 % · week 61 % · measured 2 min ago'


# --- Threshold note ------------------------------------------------------------------------------

def test_threshold_note_appears_at_the_configured_value_and_not_below():
    assert usage_clock.usage_text(usage(74, None, 0), 75, NOW).endswith('measured 0 min ago')
    at = usage_clock.usage_text(usage(75, None, 0), 75, NOW)
    assert at.endswith('measured 0 min ago — from 75 % on, no new blocks')


def test_threshold_note_uses_the_configured_number_not_a_fixed_one():
    assert usage_clock.usage_text(usage(65, None, 0), 60, NOW).endswith('— from 60 % on, no new blocks')
    assert usage_clock.usage_text(usage(65, None, 0), 75, NOW).endswith('measured 0 min ago')
    assert usage_clock.usage_text(usage(90, None, 0), 92.5, NOW).endswith('measured 0 min ago')
    assert usage_clock.usage_text(usage(93, None, 0), 92.5, NOW).endswith('— from 92.5 % on, no new blocks')


def test_threshold_note_comes_before_nothing_else_and_only_once():
    line = usage_clock.usage_text(usage(80, 20, 1, ts(2026, 1, 14, 14, 0)), 75, NOW)
    assert line == 'Usage: 5h 80 % (resets 14:00) · week 20 % · measured 1 min ago — from 75 % on, no new blocks'


# --- Unknown and stale ---------------------------------------------------------------------------

def test_unknown_usage_says_larger_blocks_wait_for_a_fresh_measurement():
    line = usage_clock.usage_text(usage(None, None, None), 75, NOW)
    assert line == ('Usage: unknown (no valid measurement in the usage file) — '
                    'start larger blocks only after a fresh measurement.')


def test_stale_usage_shows_the_old_values_and_the_same_advice():
    line = usage_clock.usage_text(usage(80, 20, 40, max_age=15), 75, NOW)
    assert line == ('Usage: 5h 80 % · week 20 % · measured 40 min ago — STALE, counts as unknown; '
                    'start larger blocks only after a fresh measurement')
    assert 'no new blocks' not in line       # a stale value does not also trigger the threshold note


def test_measurement_from_the_future_is_stale_and_not_shown_as_negative_minutes():
    line = usage_clock.usage_text(usage(10, 20, -5), 75, NOW)
    assert 'measured in the future' in line and '-5' not in line and 'STALE' in line


# --- The display switches (through the real hook) --------------------------------------------------

def test_both_parts_by_default(tmp_path):
    write_usage(tmp_path / 'usage.json', five=42, week=23, age_min=2)
    context = run_hook()
    assert re.fullmatch(r'Usage: 5h 42 % \((resets \d\d:\d\d|resets \w{3} \d{1,2} \w{3} \d\d:\d\d)\) · '
                        r'week 23 % \(resets \w{3} \d{1,2} \w{3} \d\d:\d\d\) · measured 2 min ago · '
                        r'Now: \w{3} \d{1,2} \w{3} \d{4} \d\d:\d\d', context), context


def test_show_usage_off_leaves_only_the_time(tmp_path):
    write_usage(tmp_path / 'usage.json')
    env = write_config(tmp_path, {'display': {'show_usage': False}})
    context = run_hook(env)
    assert re.fullmatch(r'Now: \w{3} \d{1,2} \w{3} \d{4} \d\d:\d\d', context), context


def test_show_time_off_leaves_only_the_usage(tmp_path):
    write_usage(tmp_path / 'usage.json', five=42, week=23)
    env = write_config(tmp_path, {'display': {'show_time': False}})
    context = run_hook(env)
    assert context.startswith('Usage: 5h 42 %') and 'Now:' not in context and context.count(' · ') == 2


def test_both_off_prints_nothing_and_exits_0(tmp_path):
    write_usage(tmp_path / 'usage.json')
    env = write_config(tmp_path, {'display': {'show_usage': False, 'show_time': False}})
    assert run_raw(SCRIPT, '{}', env) == (0, '', '')


def test_time_part_works_without_a_usage_file(tmp_path):
    env = write_config(tmp_path, {'display': {'show_usage': False}})
    assert not (tmp_path / 'usage.json').exists()
    assert run_hook(env).startswith('Now: ')


def test_time_part_survives_a_broken_usage_file(tmp_path):
    (tmp_path / 'usage.json').write_bytes(b'\xff\xfe {not json')
    env = write_config(tmp_path, {'display': {'show_usage': False}})
    assert run_hook(env).startswith('Now: ')
    # With the usage part on, a broken file is "unknown", and the time part is still there.
    context = run_hook()
    assert context.startswith('Usage: unknown') and ' · Now: ' in context


def test_usage_part_alone_with_a_missing_file_says_unknown(tmp_path):
    env = write_config(tmp_path, {'display': {'show_time': False}})
    assert run_hook(env) == ('Usage: unknown (no valid measurement in the usage file) — '
                             'start larger blocks only after a fresh measurement.')


def test_null_five_hour_block_after_a_reset_is_unknown(tmp_path):
    write_usage(tmp_path / 'usage.json', five_block=False)
    context = run_hook()
    assert context.startswith('Usage: unknown') and 'Now:' in context


def test_stale_file_through_the_hook(tmp_path):
    write_usage(tmp_path / 'usage.json', five=42, age_min=40)
    context = run_hook()
    assert 'measured 40 min ago — STALE' in context and 'fresh measurement' in context


def test_max_age_from_the_config_decides_stale(tmp_path):
    write_usage(tmp_path / 'usage.json', five=42, age_min=40)
    env = write_config(tmp_path, {'thresholds': {'max_age_min': 60}})
    assert 'STALE' not in run_hook(env)


def test_threshold_note_through_the_hook_follows_the_config(tmp_path):
    write_usage(tmp_path / 'usage.json', five=65, week=10)
    assert 'no new blocks' not in run_hook()
    env = write_config(tmp_path, {'thresholds': {'start_block_5h': 60}})
    assert 'from 60 % on, no new blocks' in run_hook(env)


def test_usage_file_from_the_config_is_used_when_no_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv('CCUC_USAGE_FILE')
    other = tmp_path / 'elsewhere.json'
    write_usage(other, five=33)
    env = write_config(tmp_path, {'usage_file': str(other)})
    assert '5h 33 %' in run_hook(env)


def test_config_problems_are_logged_by_the_hook_and_defaults_apply(tmp_path):
    write_usage(tmp_path / 'usage.json')
    env = write_config(tmp_path, {'display': {'show_usage': 'no'}, 'bogus': 1})
    context = run_hook(env)
    assert context.startswith('Usage: 5h 42 %') and ' · Now: ' in context
    lines = [json.loads(line) for line in (tmp_path / 'log' / 'log.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(lines) == 1 and lines[0]['hook'] == 'config' and 'display.show_usage' in lines[0]['reason']


def test_a_normal_run_writes_no_log_line(tmp_path):
    write_usage(tmp_path / 'usage.json')
    run_hook()
    assert not (tmp_path / 'log').exists()


# --- Clock and time zone through the real process --------------------------------------------------

def clock_of(context):
    return datetime.strptime(re.search(r'Now: (.*)$', context).group(1), '%a %d %b %Y %H:%M')


def test_the_clock_follows_the_time_zone_of_the_machine(tmp_path):
    env = write_config(tmp_path, {'display': {'show_usage': False}})
    utc = clock_of(run_hook(dict(env, TZ='UTC0')))
    plus5 = clock_of(run_hook(dict(env, TZ='XXX-5')))     # POSIX notation: five hours ahead of UTC
    assert timedelta(hours=4, minutes=58) <= plus5 - utc <= timedelta(hours=5, minutes=2)


def test_reset_time_is_shown_in_local_time(tmp_path):
    now = datetime.now(UTC)
    reset = now + timedelta(minutes=30)
    (tmp_path / 'usage.json').write_text(json.dumps({
        'five_hour': {'used_percentage': 10, 'resets_at': reset.timestamp()},
        'measured_at': now.strftime('%Y-%m-%dT%H:%M:%SZ')}), encoding='utf-8')
    context = run_hook({'TZ': 'XXX-5'})
    shown = re.search(r'\(resets (?:\w{3} \d{1,2} \w{3} )?(\d\d:\d\d)\)', context).group(1)
    assert shown == (reset + timedelta(hours=5)).strftime('%H:%M')


def test_the_line_survives_a_machine_with_an_ascii_terminal_encoding(tmp_path):
    write_usage(tmp_path / 'usage.json')
    context = run_hook({'PYTHONIOENCODING': 'ascii', 'PYTHONUTF8': '0', 'LC_ALL': 'C'})
    assert ' · week ' in context      # the middle dot came through as UTF-8


# --- Robustness: the hook never rejects a prompt -------------------------------------------------------

@pytest.mark.parametrize('stdin', ['', 'not json', '[1, 2]', 'null', '{"prompt": "x"}', '{'])
def test_broken_or_odd_input_still_gives_the_line_and_exit_0(tmp_path, stdin):
    write_usage(tmp_path / 'usage.json')
    context = run_hook(stdin=stdin)
    assert context.startswith('Usage: 5h 42 %')


def test_missing_sibling_files_give_exit_0_and_no_output(tmp_path):
    write_usage(tmp_path / 'usage.json', five=99)
    alone = tmp_path / 'alone'
    alone.mkdir()
    shutil.copy(SCRIPT, alone / 'usage_clock.py')
    assert run_raw(alone / 'usage_clock.py', '{}') == (0, '', '')


def test_broken_sibling_files_give_exit_0_and_no_output(tmp_path):
    write_usage(tmp_path / 'usage.json', five=99)
    for broken in ('_config.py', '_hookio.py'):
        folder = tmp_path / ('broken' + broken)
        folder.mkdir()
        for name in ('usage_clock.py', '_config.py', '_usage.py', '_hookio.py'):
            shutil.copy(os.path.join(HOOKS, name), folder / name)
        (folder / broken).write_text('def (:\n', encoding='utf-8')
        assert run_raw(folder / 'usage_clock.py', '{}') == (0, '', ''), broken


def test_a_broken_usage_module_still_leaves_the_time_part(tmp_path):
    folder = tmp_path / 'partial'
    folder.mkdir()
    for name in ('usage_clock.py', '_config.py', '_hookio.py'):
        shutil.copy(os.path.join(HOOKS, name), folder / name)
    (folder / '_usage.py').write_text('def (:\n', encoding='utf-8')
    code, out, _ = run_raw(folder / 'usage_clock.py', '{}')
    context = json.loads(out)['hookSpecificOutput']['additionalContext']
    assert code == 0 and re.fullmatch(r'Now: .*', context)


# --- In-process: main and build_line -------------------------------------------------------------------

def call_main(monkeypatch, capsys, stdin='{}'):
    monkeypatch.setattr(sys, 'stdin', io.StringIO(stdin))
    code = usage_clock.main()
    return code, capsys.readouterr().out


def test_main_prints_one_json_line_with_the_context(monkeypatch, capsys, tmp_path):
    write_usage(tmp_path / 'usage.json', five=42)
    code, out = call_main(monkeypatch, capsys)
    assert code == 0 and out.count('\n') == 1
    assert json.loads(out)['hookSpecificOutput']['additionalContext'].startswith('Usage: 5h 42 %')


def test_main_with_both_parts_off_prints_nothing(monkeypatch, capsys, tmp_path):
    env = write_config(tmp_path, {'display': {'show_usage': False, 'show_time': False}})
    monkeypatch.setenv('CCUC_CONFIG', env['CCUC_CONFIG'])
    assert call_main(monkeypatch, capsys) == (0, '')


def test_main_returns_0_when_reading_the_config_fails(monkeypatch, capsys):
    def boom(**_):
        raise RuntimeError('boom')
    monkeypatch.setattr(_config, 'load', boom)
    assert call_main(monkeypatch, capsys) == (0, '')


def test_an_exception_in_the_usage_part_leaves_the_time_part(monkeypatch):
    def boom(**_):
        raise RuntimeError('boom')
    monkeypatch.setattr(_usage, 'read_usage', boom)
    assert usage_clock.build_line(CFG, NOW) == 'Now: Wed 14 Jan 2026 10:05'


def test_build_line_reads_the_file_with_the_configured_max_age(tmp_path):
    now = datetime.now(UTC)
    write_usage(tmp_path / 'usage.json', five=42, week=23, age_min=20)
    cfg = json.loads(json.dumps(CFG))
    assert 'STALE' in usage_clock.build_line(cfg, now)
    cfg['thresholds']['max_age_min'] = 30
    assert 'STALE' not in usage_clock.build_line(cfg, now)


def test_build_line_without_a_time_uses_the_local_clock(tmp_path):
    cfg = json.loads(json.dumps(CFG))
    cfg['display']['show_usage'] = False
    assert re.fullmatch(r'Now: \w{3} \d{1,2} \w{3} \d{4} \d\d:\d\d', usage_clock.build_line(cfg))


def test_build_line_is_empty_with_both_parts_off():
    cfg = json.loads(json.dumps(CFG))
    cfg['display'].update(show_usage=False, show_time=False)
    assert usage_clock.build_line(cfg, NOW) == ''
