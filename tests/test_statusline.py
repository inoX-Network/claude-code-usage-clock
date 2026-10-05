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


# --- Context: always visible, colour only by absolute tokens ----------------------------------------

def window(total, size=1_000_000, percent=23):
    return {'context_window': {'total_input_tokens': total, 'context_window_size': size, 'used_percentage': percent}}


def ctx_of(status_input, cfg=CFG):
    """The context part of the line, plain, or '' if the line has none."""
    text = statusline.render('M', None, None, cfg, status_input, 0)
    match = re.search(r'\| (?:\x1b\[[0-9;]*m)*(ctx [^\x1b]*)', text)
    return match.group(1) if match else ''


def test_context_part_shows_tokens_window_and_percent_in_the_documented_order():
    line = statusline.render('Opus', 40, 61, CFG, window(231_000, percent=23.4), 0)
    assert plain(line) == 'Opus | 5h 40% | week 61% | ctx 231k/1000k 23%'
    assert plain(statusline.render('M', 10, 80, CFG, window(231_000), 0)) == (
        'M | 5h 10% | week 80% <- weekly limit reached | ctx 231k/1000k 23%')


def test_context_part_is_the_only_part_if_there_are_no_limits():
    assert plain(statusline.render('M', None, None, CFG, window(5_000, percent=1), 0)) == 'M | ctx 5k/1000k 1%'


def test_context_exact_colors_and_layout():
    line = statusline.render('M', None, None, CFG, window(231_000), 0)
    assert line == f'{GRAY}M{RESET} {GRAY}| {GRAY}ctx 231k/1000k 23%{RESET}'


def test_context_tokens_are_cut_not_rounded_and_the_percent_is_rounded_like_the_limits():
    assert ctx_of(window(231_999, percent=22.5)) == 'ctx 231k/1000k 22%'
    assert ctx_of(window(231_000, size=200_000, percent=23.5)) == 'ctx 231k/200k 24%'


@pytest.mark.parametrize('tokens,color', [(1, GRAY), (299_000, GRAY), (299_999, GRAY), (300_000, YELLOW),
                                          (300_001, YELLOW), (499_000, YELLOW), (499_999, YELLOW), (500_000, RED),
                                          (900_000, RED)])
def test_context_color_depends_on_the_absolute_tokens_at_the_exact_limit(tokens, color):
    line = statusline.render('M', None, None, CFG, window(tokens), 0)
    assert f'{color}ctx ' in line


def test_context_color_ignores_the_percent_and_the_window_size():
    assert f'{GRAY}ctx ' in statusline.render('M', None, None, CFG, window(100_000, size=100_000, percent=100), 0)
    assert f'{RED}ctx ' in statusline.render('M', None, None, CFG, window(500_000, size=10_000_000, percent=5), 0)


def test_context_thresholds_come_from_the_config():
    cfg = json.loads(json.dumps(CFG))
    cfg['statusline'].update(context_yellow_from_k=100, context_red_from_k=150.5)
    assert f'{GRAY}ctx ' in statusline.render('M', None, None, cfg, window(99_999), 0)
    assert f'{YELLOW}ctx ' in statusline.render('M', None, None, cfg, window(100_000), 0)
    assert f'{YELLOW}ctx ' in statusline.render('M', None, None, cfg, window(150_499), 0)
    assert f'{RED}ctx ' in statusline.render('M', None, None, cfg, window(150_500), 0)


@pytest.mark.parametrize('total', [0, None, 0.0])
def test_before_the_first_answer_the_part_shows_a_dash_in_gray_never_a_zero(total):
    status = window(total, percent=0)
    line = statusline.render('M', None, None, CFG, status, 0)
    assert plain(line) == 'M | ctx –/1000k'
    assert f'{GRAY}ctx –/1000k{RESET}' in line


def test_missing_total_field_and_null_current_usage_count_as_before_the_first_answer():
    assert ctx_of({'context_window': {'context_window_size': 1_000_000}}) == 'ctx –/1000k'
    status = window(0)
    status['context_window']['current_usage'] = None
    assert ctx_of(status) == 'ctx –/1000k'


@pytest.mark.parametrize('status', [{}, {'context_window': None}, {'context_window': 'x'}, {'context_window': []},
                                    {'context_window': {}}, window(5_000, size=None), window(5_000, size=0),
                                    window(5_000, size=-1), window(5_000, size=999), window(5_000, size=True),
                                    window(5_000, size='1000000'), window(5_000, size=float('nan')),
                                    window(5_000, size=float('inf')), window(None, size=None)])
def test_without_a_valid_window_size_the_part_is_left_out(status):
    assert ctx_of(status) == ''
    assert plain(statusline.render('M', 10, None, CFG, status, 0)) == 'M | 5h 10%'


@pytest.mark.parametrize('total', [True, '231000', -5, float('nan'), float('inf'), [1], {}])
def test_broken_token_values_are_treated_as_unknown_not_shown(total):
    assert ctx_of(window(total)) == 'ctx –/1000k'


@pytest.mark.parametrize('percent,expected', [(None, 'ctx 231k/1000k'), (True, 'ctx 231k/1000k'),
                                              ('23', 'ctx 231k/1000k'), (-1, 'ctx 231k/1000k'),
                                              (float('nan'), 'ctx 231k/1000k'), (0, 'ctx 231k/1000k 0%')])
def test_a_missing_or_broken_percent_leaves_out_only_the_percent(percent, expected):
    assert ctx_of(window(231_000, percent=percent)) == expected


def test_the_part_is_left_out_if_no_status_input_is_given():
    assert plain(statusline.render('M', 10, 20, CFG)) == 'M | 5h 10% | week 20%'


def test_script_shows_the_context_part_in_utf8_even_on_an_ascii_machine(tmp_path):
    now = time.time()
    status = dict(window(231_000), model={'display_name': 'Opus'},
                  rate_limits={'five_hour': b(40, now + 3600), 'seven_day': b(61, now + 86400)})
    r = subprocess.run([sys.executable, SCRIPT], input=json.dumps(status).encode('utf-8'), capture_output=True,
                       env=dict(os.environ, PYTHONIOENCODING='ascii', PYTHONUTF8='0', LC_ALL='C'), timeout=20)
    assert r.returncode == 0 and plain(r.stdout.decode('utf-8')).strip() == 'Opus | 5h 40% | week 61% | ctx 231k/1000k 23%'
    early = subprocess.run([sys.executable, SCRIPT], input=json.dumps(window(0)).encode('utf-8'), capture_output=True,
                           env=dict(os.environ, PYTHONIOENCODING='ascii', LC_ALL='C',
                                    CCUC_USAGE_FILE=str(tmp_path / 'other.json')), timeout=20)
    assert plain(early.stdout.decode('utf-8')).strip() == 'Claude | ctx –/1000k'


def test_script_shows_the_context_without_any_rate_limits(tmp_path):
    r = run(dict(window(120_000, percent=12), model={'display_name': 'Opus'}), tmp_path)
    assert plain(r.stdout).strip() == 'Opus | ctx 120k/1000k 12%'
    assert not (tmp_path / 'usage.json').exists()


def test_config_file_controls_the_context_colors(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'statusline': {'context_yellow_from_k': 10, 'context_red_from_k': 20}}), encoding='utf-8')
    env = {'CCUC_CONFIG': str(config)}
    assert f'{GRAY}ctx 9k' in run(window(9_999), tmp_path, env).stdout
    assert f'{YELLOW}ctx 10k' in run(window(10_000), tmp_path, env).stdout
    assert f'{RED}ctx 20k' in run(window(20_000), tmp_path, env).stdout


# --- Cache clock from prompt_cache -------------------------------------------------------------------

NOW = 1_800_000_000.0


def cache_of(prompt_cache, cfg=CFG, now=NOW, **status):
    """The cache part of the line, as the coloured text, or '' if the line has none."""
    text = statusline.render('M', None, None, cfg, dict(status, prompt_cache=prompt_cache), now)
    match = re.search(r'\| ((?:\x1b\[[0-9;]*m)*cache .*?)\x1b\[0m', text)
    return match.group(1) if match else ''


def warm(seconds_left, **extra):
    return dict({'warm': True, 'ttl': '1h', 'expires_at': NOW + seconds_left}, **extra)


def test_cache_part_follows_the_context_part_in_the_documented_order():
    status = dict(window(231_000), prompt_cache=warm(54 * 60))
    assert plain(statusline.render('Opus', 40, 61, CFG, status, NOW)) == (
        'Opus | 5h 40% | week 61% | ctx 231k/1000k 23% | cache 54m')


def test_cache_part_without_a_context_part():
    assert plain(statusline.render('M', None, None, CFG, {'prompt_cache': warm(3600)}, NOW)) == 'M | cache 60m'


def test_cache_exact_colors_and_layout():
    assert statusline.render('M', None, None, CFG, {'prompt_cache': warm(54 * 60)}, NOW) == (
        f'{GRAY}M{RESET} {GRAY}| {GRAY}cache 54m{RESET}')
    assert statusline.render('M', None, None, CFG, {'prompt_cache': warm(3 * 60)}, NOW) == (
        f'{GRAY}M{RESET} {GRAY}| {YELLOW}cache 3m{RESET}')
    assert statusline.render('M', None, None, CFG, {'prompt_cache': {'warm': False}}, NOW) == (
        f'{GRAY}M{RESET} {GRAY}| {RED}cache cold{RESET}')


@pytest.mark.parametrize('seconds,text,color', [(54 * 60, 'cache 54m', GRAY), (5 * 60 + 1, 'cache 6m', GRAY),
                                                (5 * 60, 'cache 5m', GRAY), (4 * 60 + 1, 'cache 5m', GRAY),
                                                (4 * 60, 'cache 4m', YELLOW), (3 * 60 + 1, 'cache 4m', YELLOW),
                                                (60, 'cache 1m', YELLOW), (1, 'cache 1m', YELLOW),
                                                (0, 'cache cold', RED), (-1, 'cache cold', RED),
                                                (-3600, 'cache cold', RED)])
def test_cache_minutes_are_rounded_up_and_yellow_below_five_cold_when_expired(seconds, text, color):
    assert cache_of(warm(seconds)) == f'{color}{text}'


def test_cache_yellow_limit_comes_from_the_config():
    cfg = json.loads(json.dumps(CFG))
    cfg['statusline']['cache_yellow_below_min'] = 10
    assert cache_of(warm(9 * 60), cfg) == f'{YELLOW}cache 9m'
    assert cache_of(warm(10 * 60), cfg) == f'{GRAY}cache 10m'
    cfg['statusline']['cache_yellow_below_min'] = 0
    assert cache_of(warm(60), cfg) == f'{GRAY}cache 1m'


def test_cache_cold_when_not_warm_whatever_expires_at_says():
    assert cache_of({'warm': False}) == f'{RED}cache cold'
    assert cache_of({'warm': False, 'expires_at': NOW + 3600}) == f'{RED}cache cold'
    assert cache_of({'warm': False, 'expires_at': None, 'ttl': '5m'}) == f'{RED}cache cold'


@pytest.mark.parametrize('prompt_cache', [None, 'warm', 5, [], [{'warm': True}], {},
                                          {'warm': True}, {'warm': True, 'expires_at': None},
                                          {'warm': True, 'expires_at': True}, {'warm': True, 'expires_at': '1800003600'},
                                          {'warm': True, 'expires_at': float('nan')},
                                          {'warm': True, 'expires_at': float('inf')},
                                          {'warm': 'false', 'expires_at': NOW + 3600},
                                          {'warm': None, 'expires_at': NOW + 3600},
                                          {'warm': 0, 'expires_at': NOW + 3600},
                                          {'expires_at': NOW + 3600}])
def test_cache_part_is_left_out_if_it_cannot_be_judged(prompt_cache):
    assert cache_of(prompt_cache) == ''
    assert plain(statusline.render('M', 10, None, CFG, {'prompt_cache': prompt_cache}, NOW)) == 'M | 5h 10%'


def test_a_missing_prompt_cache_key_leaves_the_part_out():
    assert plain(statusline.render('M', None, None, CFG, window(5_000), NOW)) == 'M | ctx 5k/1000k 23%'


def test_cache_part_through_the_script_uses_the_real_clock(tmp_path):
    expires = int(time.time()) + 20 * 60
    r = run({'model': {'display_name': 'Opus'}, 'prompt_cache': {'warm': True, 'ttl': '1h', 'expires_at': expires}},
            tmp_path)
    assert re.fullmatch(r'Opus \| cache (19|20)m', plain(r.stdout).strip())
    r = run({'prompt_cache': {'warm': True, 'expires_at': int(time.time()) - 5}}, tmp_path)
    assert plain(r.stdout).strip() == 'Claude | cache cold' and f'{RED}cache cold' in r.stdout
