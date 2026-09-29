"""Tests for hooks/statusline.py: the rendered line, the merge into the shared usage file, robustness."""
import io
import json
import os
import re
import subprocess
import sys
import time

import pytest

import _config
import statusline
from conftest import HOOKS

SCRIPT = os.path.join(HOOKS, 'statusline.py')
GRAY, YELLOW, RED, RESET = statusline.GRAY, statusline.YELLOW, statusline.RED, statusline.RESET
CFG = _config.DEFAULTS


def b(percent, reset):
    return {'used_percentage': percent, 'resets_at': reset}


def plain(text):
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def run(status_input, tmp_path, extra_env=None, raw=None):
    env = dict(os.environ, **(extra_env or {}))
    return subprocess.run([sys.executable, SCRIPT], input=raw if raw is not None else json.dumps(status_input),
                          capture_output=True, text=True, env=env, timeout=20)


# --- The rendered line ---------------------------------------------------------------------------

def test_line_with_both_values():
    assert plain(statusline.render('Opus', 40.2, 61.0, CFG)) == 'Opus | 5h 40% | week 61%'


def test_exact_colors_and_layout():
    line = statusline.render('Opus', 40.0, 61.0, CFG)
    assert line == f'{GRAY}Opus{RESET} {GRAY}| {GRAY}5h 40%{RESET} {GRAY}| {YELLOW}week 61%{RESET}'


@pytest.mark.parametrize('value,color', [(0, GRAY), (49.4, GRAY), (49.5, YELLOW), (50, YELLOW), (74.4, YELLOW),
                                         (74.6, RED), (75, RED), (100, RED)])
def test_color_thresholds_use_the_rounded_number(value, color):
    line = statusline.render('M', value, None, CFG)
    assert f'{color}5h ' in line


def test_thresholds_come_from_the_config():
    cfg = json.loads(json.dumps(CFG))
    cfg['statusline'].update(yellow_from=10, red_from=20)
    assert f'{YELLOW}5h 15%' in statusline.render('M', 15, None, cfg)
    assert f'{RED}5h 20%' in statusline.render('M', 20, None, cfg)
    assert f'{GRAY}5h 9%' in statusline.render('M', 9, None, cfg)


def test_missing_values_are_left_out_never_shown_as_zero():
    assert plain(statusline.render('Opus', None, None, CFG)) == 'Opus'
    assert plain(statusline.render('Opus', 12, None, CFG)) == 'Opus | 5h 12%'
    assert plain(statusline.render('Opus', None, 12, CFG)) == 'Opus | week 12%'


def test_rounding_is_printf_style_half_to_even():
    assert plain(statusline.render('M', 23.4, 22.5, CFG)) == 'M | 5h 23% | week 22%'
    assert plain(statusline.render('M', 23.5, 24.5, CFG)) == 'M | 5h 24% | week 24%'
    assert plain(statusline.render('M', 0.4, None, CFG)) == 'M | 5h 0%'


def test_non_finite_values_are_left_out():
    assert plain(statusline.render('M', float('nan'), float('inf'), CFG)) == 'M'


def test_weekly_limit_marker_from_the_threshold():
    assert 'weekly limit reached' not in statusline.render('M', 10, 74.4, CFG)
    line = statusline.render('M', 10, 75, CFG)
    assert line.endswith(f' {RED}<- weekly limit reached{RESET}')
    assert 'weekly limit reached' in statusline.render('M', None, 90, CFG)      # also without a five-hour value
    assert 'weekly limit reached' not in statusline.render('M', 99, None, CFG)  # only the week counts


def test_weekly_limit_marker_can_be_switched_off_and_follows_the_threshold():
    cfg = json.loads(json.dumps(CFG))
    cfg['statusline']['week_stop_marker'] = False
    assert 'weekly limit' not in statusline.render('M', 10, 99, cfg)
    cfg['statusline']['week_stop_marker'] = True
    cfg['thresholds']['start_block_week'] = 90
    assert 'weekly limit' not in statusline.render('M', 10, 89, cfg)
    assert 'weekly limit' in statusline.render('M', 10, 90, cfg)


@pytest.mark.parametrize('status,expected', [({}, 'Claude'), ({'model': None}, 'Claude'), ({'model': 'x'}, 'Claude'),
                                             ({'model': {'display_name': ''}}, 'Claude'),
                                             ({'model': {'display_name': 5}}, 'Claude'),
                                             ({'model': {'display_name': 'Opus'}}, 'Opus')])
def test_model_name_fallback(status, expected):
    assert statusline._model_name(status) == expected


# --- Whole script: merge into the shared file ----------------------------------------------------------

def test_script_writes_the_file_and_shows_the_merged_values(tmp_path):
    usage = tmp_path / 'usage.json'
    now = time.time()
    r = run({'model': {'display_name': 'Opus'}, 'rate_limits': {'five_hour': b(79, now + 3600),
                                                                 'seven_day': b(48, now + 86400)}}, tmp_path)
    assert r.returncode == 0 and plain(r.stdout).strip() == 'Opus | 5h 79% | week 48%'
    saved = json.loads(usage.read_text(encoding='utf-8'))
    assert saved['five_hour']['used_percentage'] == 79 and saved['seven_day']['used_percentage'] == 48
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', saved['measured_at'])


def test_idle_session_shows_the_shared_values_and_does_not_write(tmp_path):
    usage = tmp_path / 'usage.json'
    now = time.time()
    active = {'model': {'display_name': 'Opus'}, 'rate_limits': {'five_hour': b(79, now + 3600),
                                                                  'seven_day': b(48, now + 86400)}}
    assert run(active, tmp_path).returncode == 0
    first = usage.read_text(encoding='utf-8')
    idle = {'model': {'display_name': 'Sonnet'}, 'rate_limits': {'five_hour': None, 'seven_day': b(19, now + 86400)}}
    r = run(idle, tmp_path)
    out = plain(r.stdout)
    assert r.returncode == 0 and '5h 79%' in out and 'week 48%' in out and '19%' not in out and out.startswith('Sonnet')
    assert usage.read_text(encoding='utf-8') == first
    assert not [p for p in os.listdir(tmp_path) if '.tmp.' in p]


def test_week_marker_appears_through_the_script(tmp_path):
    now = time.time()
    r = run({'model': {'display_name': 'M'}, 'rate_limits': {'five_hour': b(10, now + 3600),
                                                              'seven_day': b(80, now + 86400)}}, tmp_path)
    assert plain(r.stdout).strip() == 'M | 5h 10% | week 80% <- weekly limit reached'


def test_without_rate_limits_only_the_model_is_shown_and_nothing_is_written(tmp_path):
    for status in ({'model': {'display_name': 'Opus'}}, {'model': {'display_name': 'Opus'}, 'rate_limits': {}},
                   {'model': {'display_name': 'Opus'}, 'rate_limits': 'x'}):
        r = run(status, tmp_path)
        assert r.returncode == 0 and plain(r.stdout).strip() == 'Opus'
    assert not (tmp_path / 'usage.json').exists()


@pytest.mark.parametrize('raw', ['not json', '', '[1, 2]', '"text"', 'null', '[' * 100000])
def test_garbage_on_stdin_still_exits_zero_with_a_line(tmp_path, raw):
    r = run(None, tmp_path, raw=raw)
    assert r.returncode == 0 and plain(r.stdout).strip() == 'Claude'


def test_garbage_input_keeps_the_stored_values_on_display(tmp_path):
    now = time.time()
    run({'rate_limits': {'five_hour': b(79, now + 3600), 'seven_day': b(48, now + 86400)}}, tmp_path)
    r = run(None, tmp_path, raw='no object')
    assert r.returncode == 0 and plain(r.stdout).strip() == 'Claude | 5h 79% | week 48%'


def test_broken_usage_file_is_rebuilt_by_an_active_session(tmp_path):
    (tmp_path / 'usage.json').write_text('broken', encoding='utf-8')
    r = run({'rate_limits': {'five_hour': None}}, tmp_path)
    assert r.returncode == 0 and plain(r.stdout).strip() == 'Claude'
    now = time.time()
    r = run({'rate_limits': {'five_hour': b(30, now + 3600)}}, tmp_path)
    assert plain(r.stdout).strip() == 'Claude | 5h 30%'
    assert json.loads((tmp_path / 'usage.json').read_text(encoding='utf-8'))['five_hour']['used_percentage'] == 30


def test_failed_merge_shows_the_own_values_and_writes_nothing(tmp_path):
    blocker = tmp_path / 'blocker'
    blocker.write_text('a file where a folder should be', encoding='utf-8')
    now = time.time()
    r = run({'model': {'display_name': 'Opus'}, 'rate_limits': {'five_hour': b(23.4, now + 3600),
                                                                 'seven_day': b(19, now + 86400)}},
            tmp_path, {'CCUC_USAGE_FILE': str(blocker / 'usage.json')})
    assert r.returncode == 0 and plain(r.stdout).strip() == 'Opus | 5h 23% | week 19%'
    assert sorted(os.listdir(tmp_path)) == ['blocker']


def test_own_values_are_shown_without_a_reset_time_when_the_merge_fails(tmp_path):
    blocker = tmp_path / 'blocker'
    blocker.write_text('x', encoding='utf-8')
    r = run({'rate_limits': {'five_hour': {'used_percentage': 12}, 'seven_day': {'used_percentage': True}}},
            tmp_path, {'CCUC_USAGE_FILE': str(blocker / 'usage.json')})
    assert plain(r.stdout).strip() == 'Claude | 5h 12%'


def test_decimal_values_work_in_a_comma_locale(tmp_path):
    now = time.time()
    r = run({'rate_limits': {'five_hour': b(23.4, now + 3600)}}, tmp_path, {'LC_ALL': 'de_DE.UTF-8', 'LANG': 'de_DE.UTF-8'})
    assert r.returncode == 0 and plain(r.stdout).strip() == 'Claude | 5h 23%'


def test_config_file_controls_colors_and_marker(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'statusline': {'yellow_from': 5, 'red_from': 10, 'week_stop_marker': False},
                                  'thresholds': {'start_block_week': 20}}), encoding='utf-8')
    now = time.time()
    r = run({'rate_limits': {'five_hour': b(7, now + 3600), 'seven_day': b(30, now + 86400)}}, tmp_path,
            {'CCUC_CONFIG': str(config)})
    assert f'{YELLOW}5h 7%' in r.stdout and f'{RED}week 30%' in r.stdout and 'weekly limit' not in r.stdout


def test_broken_config_falls_back_to_defaults_without_logging(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text('{broken', encoding='utf-8')
    now = time.time()
    r = run({'rate_limits': {'five_hour': b(60, now + 3600)}}, tmp_path, {'CCUC_CONFIG': str(config)})
    assert r.returncode == 0 and f'{YELLOW}5h 60%' in r.stdout
    assert not (tmp_path / 'log').exists()          # the status line runs every few seconds: it does not log


# --- main() in process: exit code is always 0 --------------------------------------------------------

def test_main_returns_zero_even_if_rendering_fails(monkeypatch, capsys):
    def boom(*_a):
        raise RuntimeError('boom')
    monkeypatch.setattr(statusline, 'render', boom)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{}'))
    assert statusline.main() == 0
    assert capsys.readouterr().out == ''


def test_main_survives_a_failing_merge_in_process(monkeypatch, capsys):
    def boom(*_a):
        raise OSError('locked')
    monkeypatch.setattr(statusline._usage, 'update_file', boom)
    now = time.time()
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'rate_limits': {'five_hour': b(40, now + 60)}})))
    assert statusline.main() == 0
    assert plain(capsys.readouterr().out).strip() == 'Claude | 5h 40%'
