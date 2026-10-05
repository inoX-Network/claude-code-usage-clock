"""Tests for hooks/_config.py: defaults, per-key fallback and logging of problems."""
import json
import os

import pytest

import _config
from conftest import ROOT


def write_config(tmp_path, content):
    path = tmp_path / 'config.json'
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')
    os.environ['CCUC_CONFIG'] = str(path)


def log_lines(tmp_path):
    path = tmp_path / 'log' / 'log.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def test_defaults_in_code_match_config_example():
    with open(os.path.join(ROOT, 'config.example.json'), encoding='utf-8') as f:
        assert _config.DEFAULTS == json.load(f)


def test_missing_file_gives_defaults_without_log(tmp_path):
    cfg = _config.load()
    assert cfg['thresholds'] == _config.DEFAULTS['thresholds']
    assert cfg['spawn_rules'] == _config.DEFAULTS['spawn_rules']
    assert log_lines(tmp_path) == []


def test_tilde_in_paths_is_expanded_but_not_in_defaults(tmp_path):
    cfg = _config.load()
    home = str(tmp_path / 'home')
    assert cfg['usage_file'] == os.path.join(home, '.claude', 'rate-limit.json')
    assert cfg['log_dir'] == os.path.join(home, '.cache', 'claude-usage-clock')
    assert _config.DEFAULTS['usage_file'].startswith('~')
    write_config(tmp_path, {'usage_file': '~/elsewhere/usage.json'})
    assert _config.load()['usage_file'] == os.path.join(home, 'elsewhere', 'usage.json')


def test_valid_keys_override_defaults_others_stay(tmp_path):
    write_config(tmp_path, {'thresholds': {'deny': 99, 'max_age_min': 7.5}, 'display': {'show_time': False},
                            'spawn_rules': {'exceptions': ['a'], 'suggested_agents': ['x', 'y']}})
    cfg = _config.load()
    assert cfg['thresholds']['deny'] == 99 and cfg['thresholds']['max_age_min'] == 7.5
    assert cfg['thresholds']['warn'] == 85
    assert cfg['display'] == {'show_usage': True, 'show_time': False}
    assert cfg['spawn_rules']['exceptions'] == ['a'] and cfg['spawn_rules']['suggested_agents'] == ['x', 'y']
    assert log_lines(tmp_path) == []


def test_loaded_config_does_not_alias_the_defaults():
    cfg = _config.load()
    cfg['spawn_rules']['effort_values'].append('bogus')
    cfg['thresholds']['deny'] = 1
    assert 'bogus' not in _config.DEFAULTS['spawn_rules']['effort_values']
    assert _config.DEFAULTS['thresholds']['deny'] == 95


@pytest.mark.parametrize('content', ['{broken', '', '\x00\x01', '[' * 100000])
def test_unreadable_file_gives_defaults_and_one_log_line(tmp_path, content):
    write_config(tmp_path, content)
    cfg = _config.load()
    assert cfg['thresholds'] == _config.DEFAULTS['thresholds']
    lines = log_lines(tmp_path)
    assert len(lines) == 1 and lines[0]['hook'] == 'config' and 'unreadable' in lines[0]['reason']


def test_unreadable_because_it_is_a_directory(tmp_path):
    os.environ['CCUC_CONFIG'] = str(tmp_path)
    assert _config.load()['thresholds'] == _config.DEFAULTS['thresholds']
    assert len(log_lines(tmp_path)) == 1


@pytest.mark.parametrize('content', [[], 'text', 5, None])
def test_top_level_not_an_object_gives_defaults(tmp_path, content):
    write_config(tmp_path, json.dumps(content))
    cfg = _config.load()
    assert cfg['thresholds'] == _config.DEFAULTS['thresholds']
    assert 'expected an object' in log_lines(tmp_path)[0]['reason']


def test_empty_object_is_fine_and_silent(tmp_path):
    write_config(tmp_path, {})
    assert _config.load()['thresholds'] == _config.DEFAULTS['thresholds']
    assert log_lines(tmp_path) == []


def test_wrong_types_fall_back_per_key_and_keep_the_valid_neighbours(tmp_path):
    write_config(tmp_path, {'thresholds': {'deny': '95', 'warn': True, 'checkpoint_only': 90, 'max_age_min': None},
                            'display': {'show_usage': 1, 'show_time': False},
                            'statusline': {'yellow_from': [50]},
                            'spawn_rules': {'effort_values': ['low', 5], 'exceptions': 'x', 'model_pattern': 3},
                            'usage_file': 5, 'checkpoint_dir_suffix': ['a']})
    cfg = _config.load()
    d = _config.DEFAULTS
    assert cfg['thresholds']['deny'] == d['thresholds']['deny']
    assert cfg['thresholds']['warn'] == d['thresholds']['warn']            # bool is not a number
    assert cfg['thresholds']['max_age_min'] == d['thresholds']['max_age_min']
    assert cfg['thresholds']['checkpoint_only'] == 90                      # valid neighbour is kept
    assert cfg['display'] == {'show_usage': True, 'show_time': False}     # 1 is not a bool
    assert cfg['statusline']['yellow_from'] == 50
    assert cfg['spawn_rules']['effort_values'] == d['spawn_rules']['effort_values']
    assert cfg['spawn_rules']['exceptions'] == d['spawn_rules']['exceptions']
    assert cfg['spawn_rules']['model_pattern'] == d['spawn_rules']['model_pattern']
    assert cfg['usage_file'].endswith('rate-limit.json') and cfg['checkpoint_dir_suffix'] == '/agent-checkpoints'
    reason = log_lines(tmp_path)[0]['reason']
    assert 'thresholds.deny' in reason and 'display.show_usage' in reason


def test_unknown_keys_are_ignored_and_logged_once(tmp_path):
    write_config(tmp_path, {'surprise': 1, 'thresholds': {'nope': 2, 'deny': 96}})
    cfg = _config.load()
    assert 'surprise' not in cfg and 'nope' not in cfg['thresholds'] and cfg['thresholds']['deny'] == 96
    lines = log_lines(tmp_path)
    assert len(lines) == 1 and 'surprise' in lines[0]['reason'] and 'thresholds.nope' in lines[0]['reason']


def test_section_with_wrong_type_gives_the_section_defaults(tmp_path):
    write_config(tmp_path, {'thresholds': 5, 'spawn_rules': [1]})
    cfg = _config.load()
    assert cfg['thresholds'] == _config.DEFAULTS['thresholds'] and cfg['spawn_rules'] == _config.DEFAULTS['spawn_rules']
    assert 'thresholds: expected an object' in log_lines(tmp_path)[0]['reason']


def test_invalid_regular_expression_falls_back(tmp_path):
    write_config(tmp_path, {'spawn_rules': {'model_pattern': '(unclosed'}})
    cfg = _config.load()
    assert cfg['spawn_rules']['model_pattern'] == _config.DEFAULTS['spawn_rules']['model_pattern']
    assert 'model_pattern' in log_lines(tmp_path)[0]['reason']


@pytest.mark.parametrize('key,valid', [('model_effort_action', ['off', 'warn', 'deny']),
                                       ('skill_fork_missing_agent', ['warn', 'deny']),
                                       ('model_override_in_call', ['allow', 'warn', 'deny'])])
def test_choice_keys_accept_their_values(tmp_path, key, valid):
    for value in valid:
        write_config(tmp_path, {'spawn_rules': {key: value}})
        assert _config.load()['spawn_rules'][key] == value
    assert log_lines(tmp_path) == []


@pytest.mark.parametrize('key,value', [('skill_fork_missing_agent', 'allow'), ('skill_fork_missing_agent', 'Deny'),
                                       ('skill_fork_missing_agent', ''), ('model_override_in_call', 'block'),
                                       ('model_override_in_call', 'warn '), ('model_effort_action', 'true'),
                                       ('model_effort_action', 'DENY'), ('model_effort_action', '')])
def test_choice_keys_with_other_values_fall_back_and_log(tmp_path, key, value):
    write_config(tmp_path, {'spawn_rules': {key: value, 'exceptions': ['kept']}})
    cfg = _config.load()
    assert cfg['spawn_rules'][key] == _config.DEFAULTS['spawn_rules'][key]
    assert cfg['spawn_rules']['exceptions'] == ['kept']
    lines = log_lines(tmp_path)
    assert len(lines) == 1 and f'spawn_rules.{key}' in lines[0]['reason'] and 'must be one of' in lines[0]['reason']


def test_week_action_accepts_its_values_and_falls_back_on_others(tmp_path):
    for value in ('warn', 'ask', 'deny'):
        write_config(tmp_path, {'thresholds': {'week_action': value}})
        assert _config.load()['thresholds']['week_action'] == value
    assert log_lines(tmp_path) == []
    write_config(tmp_path, {'thresholds': {'week_action': 'block', 'start_block_week': 80}})
    cfg = _config.load()
    assert cfg['thresholds']['week_action'] == 'warn' and cfg['thresholds']['start_block_week'] == 80
    assert 'thresholds.week_action: must be one of warn|ask|deny' in log_lines(tmp_path)[0]['reason']


def test_ask_verified_in_bypass_must_be_a_boolean(tmp_path):
    write_config(tmp_path, {'thresholds': {'ask_verified_in_bypass': 'yes'}})
    assert _config.load()['thresholds']['ask_verified_in_bypass'] is False
    assert 'thresholds.ask_verified_in_bypass: wrong type' in log_lines(tmp_path)[0]['reason']


def test_the_removed_require_key_is_ignored_and_logged(tmp_path):
    write_config(tmp_path, {'spawn_rules': {'require_model_and_effort': True}})
    assert _config.load()['spawn_rules']['model_effort_action'] == 'warn'
    assert 'spawn_rules.require_model_and_effort: unknown key ignored' in log_lines(tmp_path)[0]['reason']


@pytest.mark.parametrize('value', [[], [''], ['low', ''], ['  ']])
def test_empty_effort_values_fall_back_and_log(tmp_path, value):
    write_config(tmp_path, {'spawn_rules': {'effort_values': value, 'exceptions': ['kept']}})
    cfg = _config.load()
    assert cfg['spawn_rules']['effort_values'] == _config.DEFAULTS['spawn_rules']['effort_values']
    assert cfg['spawn_rules']['exceptions'] == ['kept']
    lines = log_lines(tmp_path)
    assert len(lines) == 1 and 'spawn_rules.effort_values' in lines[0]['reason'] and 'non-empty' in lines[0]['reason']


@pytest.mark.parametrize('value', ['', '   '])
def test_empty_model_pattern_falls_back_and_logs(tmp_path, value):
    write_config(tmp_path, {'spawn_rules': {'model_pattern': value}})
    assert _config.load()['spawn_rules']['model_pattern'] == _config.DEFAULTS['spawn_rules']['model_pattern']
    lines = log_lines(tmp_path)
    assert len(lines) == 1 and 'spawn_rules.model_pattern: must not be empty' in lines[0]['reason']


@pytest.mark.parametrize('value', ['', '/', '//', '  ', ' / '])
def test_empty_checkpoint_suffix_falls_back_and_logs(tmp_path, value):
    write_config(tmp_path, {'checkpoint_dir_suffix': value})
    assert _config.load()['checkpoint_dir_suffix'] == '/agent-checkpoints'
    lines = log_lines(tmp_path)
    assert len(lines) == 1 and 'checkpoint_dir_suffix: must not be empty' in lines[0]['reason']


def test_valid_values_of_the_three_checked_keys_are_kept_silently(tmp_path):
    write_config(tmp_path, {'checkpoint_dir_suffix': '/work/notes', 'spawn_rules': {'effort_values': ['fast'],
                                                                                    'model_pattern': 'x+'}})
    cfg = _config.load()
    assert cfg['checkpoint_dir_suffix'] == '/work/notes'
    assert cfg['spawn_rules']['effort_values'] == ['fast'] and cfg['spawn_rules']['model_pattern'] == 'x+'
    assert log_lines(tmp_path) == []


def test_status_line_mode_does_not_log(tmp_path):
    write_config(tmp_path, '{broken')
    assert _config.load(log_problems=False)['thresholds'] == _config.DEFAULTS['thresholds']
    assert log_lines(tmp_path) == []


def test_log_reason_is_capped(tmp_path):
    write_config(tmp_path, {f'unknown_{i}': i for i in range(100)})
    _config.load()
    assert len(log_lines(tmp_path)[0]['reason']) <= 300


def test_default_path_is_next_to_the_scripts(monkeypatch):
    monkeypatch.delenv('CCUC_CONFIG')
    assert _config.config_path() == os.path.join(ROOT, 'hooks', 'config.json')


def test_statusline_context_and_cache_keys_have_their_defaults_and_are_overridable(tmp_path):
    cfg = _config.load()
    assert (cfg['statusline']['context_yellow_from_k'], cfg['statusline']['context_red_from_k'],
            cfg['statusline']['cache_yellow_below_min']) == (300, 500, 5)
    write_config(tmp_path, {'statusline': {'context_yellow_from_k': 100, 'context_red_from_k': 150.5,
                                           'cache_yellow_below_min': 10}})
    cfg = _config.load()
    assert (cfg['statusline']['context_yellow_from_k'], cfg['statusline']['context_red_from_k'],
            cfg['statusline']['cache_yellow_below_min']) == (100, 150.5, 10)
    assert log_lines(tmp_path) == []


def test_statusline_context_and_cache_keys_with_wrong_types_fall_back_and_log(tmp_path):
    write_config(tmp_path, {'statusline': {'context_yellow_from_k': '300', 'context_red_from_k': True,
                                           'cache_yellow_below_min': [5], 'yellow_from': 40}})
    cfg = _config.load()
    assert cfg['statusline'] == dict(_config.DEFAULTS['statusline'], yellow_from=40)
    reason = log_lines(tmp_path)[0]['reason']
    for key in ('context_yellow_from_k', 'context_red_from_k', 'cache_yellow_below_min'):
        assert f'statusline.{key}: wrong type' in reason


# --- context_reporter ---------------------------------------------------------------------------------------------

def test_context_reporter_keys_have_their_defaults_and_are_overridable(tmp_path):
    assert _config.load()['context_reporter'] == {'after_tool_from': 50, 'max_age_min': 15}
    write_config(tmp_path, {'context_reporter': {'after_tool_from': 0, 'max_age_min': 0.5}})
    assert _config.load()['context_reporter'] == {'after_tool_from': 0, 'max_age_min': 0.5}
    write_config(tmp_path, {'context_reporter': {'after_tool_from': 100.0}})
    assert _config.load()['context_reporter'] == {'after_tool_from': 100.0, 'max_age_min': 15}
    assert log_lines(tmp_path) == []


@pytest.mark.parametrize('value', ['50', True, None, [50]])
def test_context_reporter_keys_with_wrong_types_fall_back_and_log(tmp_path, value):
    write_config(tmp_path, {'context_reporter': {'after_tool_from': value, 'max_age_min': value}})
    assert _config.load()['context_reporter'] == _config.DEFAULTS['context_reporter']
    reason = log_lines(tmp_path)[0]['reason']
    assert 'context_reporter.after_tool_from: wrong type' in reason and 'context_reporter.max_age_min: wrong type' in reason


@pytest.mark.parametrize('value', [-1, -0.01, 100.01, 101, float('nan'), float('inf'), float('-inf')])
def test_after_tool_from_outside_0_to_100_falls_back_and_logs(tmp_path, value):
    write_config(tmp_path, '{"context_reporter": {"after_tool_from": %s, "max_age_min": 20}}' % json.dumps(value))
    cfg = _config.load()
    assert cfg['context_reporter'] == {'after_tool_from': 50, 'max_age_min': 20}
    assert 'context_reporter.after_tool_from: must be a number from 0 to 100, using default' in log_lines(tmp_path)[0]['reason']


@pytest.mark.parametrize('value', [0, -1, -0.5, float('nan'), float('inf'), float('-inf')])
def test_max_age_min_that_is_not_a_positive_finite_number_falls_back_and_logs(tmp_path, value):
    write_config(tmp_path, '{"context_reporter": {"after_tool_from": 60, "max_age_min": %s}}' % json.dumps(value))
    cfg = _config.load()
    assert cfg['context_reporter'] == {'after_tool_from': 60, 'max_age_min': 15}
    assert 'context_reporter.max_age_min: must be a positive number, using default' in log_lines(tmp_path)[0]['reason']


def test_context_reporter_section_with_another_type_or_unknown_key(tmp_path):
    write_config(tmp_path, {'context_reporter': 'on'})
    assert _config.load()['context_reporter'] == _config.DEFAULTS['context_reporter']
    write_config(tmp_path, {'context_reporter': {'bogus': 1}})
    assert _config.load()['context_reporter'] == _config.DEFAULTS['context_reporter']
    assert 'context_reporter.bogus: unknown key ignored' in log_lines(tmp_path)[-1]['reason']
