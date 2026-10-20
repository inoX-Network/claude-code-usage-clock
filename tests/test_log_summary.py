"""Tests for tools/log_summary.py. Synthetic log lines (January 2026) plus lines written by the real hooks."""
import importlib.util
import json
import os
import re
import subprocess
import sys

import pytest

from conftest import HOOKS, ROOT

SCRIPT = os.path.join(ROOT, 'tools', 'log_summary.py')


def run(*args, env=None):
    done = subprocess.run([sys.executable, SCRIPT, *map(str, args)], capture_output=True, text=True, encoding='utf-8', timeout=30, env=env)
    return done.returncode, done.stdout + done.stderr


def line(time, hook, decision, **more):
    return json.dumps({'time': time, 'hook': hook, 'mode': 'shadow', 'decision': decision,
                       'reason': more.pop('reason', ''), **more})


SAMPLE = [
    line('2026-01-14T09:00:00+00:00', 'spawn_gate', 'deny', reason='test run', subagent_type='test', tool_name='Agent',
         tool_input_keys=['prompt', 'subagent_type']),
    line('2026-01-14T10:30:00+00:00', 'spawn_gate', 'deny', reason='no definition with model and effort',
         subagent_type='general-purpose', tool_name='Agent', tool_input_keys=['prompt', 'subagent_type']),
    line('2026-01-14T10:30:05+00:00', 'spawn_gate', 'allow', tool_name='Agent', subagent_type='reviewer',
         tool_input_keys=['prompt', 'subagent_type']),
    line('2026-01-14T10:31:00+00:00', 'soft_stop', 'allow', agent_id='a1', agent_type='reviewer', tool_name='Bash'),
    line('2026-01-14T10:31:01+00:00', 'soft_stop', 'warn', agent_id='a2', agent_type='reviewer', tool_name='Read'),
]


def write_log(tmp_path, lines, name='log.jsonl'):
    path = tmp_path / name
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def test_summary_of_a_shadow_log(tmp_path):
    code, out = run(write_log(tmp_path, SAMPLE))
    assert code == 0, out
    assert '5 entries, 2026-01-14T09:00:00+00:00 to 2026-01-14T10:31:01+00:00' in out
    assert 'spawn_gate     shadow   deny      2' in out and 'soft_stop      shadow   warn      1' in out
    assert 'Would have denied (shadow mode), spawn_gate:' in out
    assert '   1x general-purpose: no definition with model and effort' in out
    assert 'Denied (enforce mode)' not in out
    assert "Agent: ['prompt', 'subagent_type'] (3x)" in out
    assert "Soft stop calls with agent_id: 2 of 2 - agent_type: {'reviewer': 2}" in out


def test_enforce_denials_are_listed_apart_from_shadow_ones(tmp_path):
    lines = SAMPLE[:2] + [json.dumps({'time': '2026-01-15T08:00:00+00:00', 'hook': 'soft_stop', 'mode': 'enforce',
                                      'decision': 'deny', 'reason': 'Usage pause: 96 %', 'tool_name': 'Bash'})]
    code, out = run(write_log(tmp_path, lines))
    assert code == 0
    assert 'Would have denied (shadow mode), spawn_gate:' in out
    assert 'Denied (enforce mode), soft_stop:' in out and 'Bash: Usage pause: 96 %' in out
    assert 'Would have denied (shadow mode), soft_stop' not in out


def test_since_hides_older_entries(tmp_path):
    path = write_log(tmp_path, SAMPLE)
    code, out = run(path, '--since=2026-01-14T09:30:00')
    assert code == 0 and '4 entries' in out and 'test run' not in out, out
    code, out = run('--since', '2026-01-14T10:31:01Z', path)
    assert code == 0 and '1 entry, ' in out
    code, out = run(path, '--since=2026-01-14T12:30:00+02:00')  # 10:30 UTC
    assert code == 0 and '4 entries' in out
    code, out = run(path, '--since=2026-01-15')
    assert code == 1 and 'No entries since 2026-01-15' in out


@pytest.mark.parametrize('bad', ['yesterday', '14.01.2026', '', '2026-13-01'])
def test_an_invalid_since_is_an_argument_error(tmp_path, bad):
    code, out = run(write_log(tmp_path, SAMPLE), f'--since={bad}')
    assert code == 2 and '--since must be an ISO 8601 time' in out and 'Traceback' not in out


def test_entries_without_a_readable_time_only_drop_out_with_since(tmp_path):
    lines = [json.dumps({'hook': 'spawn_gate', 'decision': 'allow'}), json.dumps({'time': 5, 'hook': 'soft_stop'}), SAMPLE[1]]
    path = write_log(tmp_path, lines)
    assert '3 entries' in run(path)[1]
    assert '1 entry, ' in run(path, '--since=2026-01-01')[1]


def test_lines_without_mode_and_tool_name_go_through(tmp_path):
    """The config problem line has neither mode nor tool_name."""
    config = json.dumps({'time': '2026-01-14T10:00:00+00:00', 'hook': 'config', 'decision': 'defaults',
                         'reason': 'thresholds.deny: wrong type, using default'})
    code, out = run(write_log(tmp_path, [config, SAMPLE[1]]))
    assert code == 0, out
    assert 'config         -        defaults  1' in out
    assert 'Problems found in config.json' in out and 'thresholds.deny: wrong type, using default' in out
    assert 'Traceback' not in out


def test_internal_errors_are_listed_with_their_reason(tmp_path):
    error = line('2026-01-14T10:00:00+00:00', 'soft_stop', 'error', reason='KeyError', agent_id='a1')
    code, out = run(write_log(tmp_path, [error, error, SAMPLE[1]]))
    assert code == 0 and 'Internal errors' in out and '   2x soft_stop: KeyError' in out


def test_odd_field_types_do_not_crash_the_summary(tmp_path):
    odd = [json.dumps({'time': '2026-01-14T10:00:00+00:00', 'hook': 5, 'mode': ['x'], 'decision': None,
                       'reason': 7, 'tool_input_keys': 'prompt', 'tool_name': None, 'agent_id': 3}),
           json.dumps({'time': '2026-01-14T10:00:01+00:00', 'hook': 'spawn_gate', 'mode': 'shadow', 'decision': 'deny',
                       'reason': None, 'tool_input_keys': [1, 'a'], 'subagent_type': 5}),
           '[1, 2]', '"text"', '5']
    code, out = run(write_log(tmp_path, odd))
    assert code == 0 and '3 unreadable line' in out and 'Traceback' not in out, out
    assert "None: [] (" not in out and "['1', 'a']" in out


def test_a_half_written_line_is_skipped(tmp_path):
    path = write_log(tmp_path, [SAMPLE[1], '{"time": "2026-01-14T10:3', SAMPLE[3]])
    code, out = run(path)
    assert code == 0 and '1 unreadable line(s) skipped' in out and '2 entries' in out


def test_empty_and_missing_logs(tmp_path):
    empty = tmp_path / 'empty.jsonl'
    empty.write_text('\n  \n', encoding='utf-8')
    code, out = run(empty)
    assert code == 1 and 'No entries' in out and 'Traceback' not in out
    code, out = run(tmp_path / 'missing.jsonl')
    assert code == 1 and 'No log at' in out and 'CCUC_LOG_DIR' in out
    code, out = run(tmp_path)  # a folder is no log
    assert code == 1 and 'No log at' in out


def test_no_spawn_gate_lines_say_so(tmp_path):
    code, out = run(write_log(tmp_path, [SAMPLE[3]]))
    assert code == 0 and 'Input fields of the spawn gate' in out and '  none' in out


def test_an_unreadable_file_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('log_summary_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def refuse(*args):
        raise PermissionError('denied')

    monkeypatch.setattr(module, 'read_entries', refuse)
    assert module.main([str(write_log(tmp_path, SAMPLE))]) == 1
    assert 'Cannot read' in capsys.readouterr().out


# --- Wiring: the summary reads what the real hooks write ---------------------------------------------------------

def hook(script, payload, *args):
    done = subprocess.run([sys.executable, os.path.join(HOOKS, script), *args], input=json.dumps(payload),
                          capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert done.returncode == 0, done.stderr


def test_default_path_and_field_names_match_what_the_hooks_write(tmp_path, monkeypatch):
    (tmp_path / 'config.json').write_text('{"thresholds": {"deny": "high"}, "spawn_rules": {"model_effort_action": "deny"}}',
                                             encoding='utf-8')
    monkeypatch.setenv('CCUC_CONFIG', str(tmp_path / 'config.json'))
    hook('spawn_gate.py', {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'nobody-defined-this', 'prompt': 'x'}},
         '--mode=shadow')
    hook('soft_stop.py', {'agent_id': 'agent-7', 'agent_type': 'reviewer', 'tool_name': 'Read',
                          'tool_input': {'file_path': '/tmp/x'}}, '--mode=shadow')
    code, out = run()  # no path: <CCUC_LOG_DIR>/log.jsonl
    assert code == 0, out
    assert 'spawn_gate     shadow   deny      1' in out and 'soft_stop      shadow   ' in out
    assert 'Would have denied (shadow mode), spawn_gate:' in out and 'nobody-defined-this' in out
    assert "Agent: ['prompt', 'subagent_type'] (1x)" in out
    assert "Soft stop calls with agent_id: 1 of 1 - agent_type: {'reviewer': 1}" in out
    assert 'thresholds.deny: wrong type' in out


def test_log_dir_comes_from_the_config_without_the_environment_variable(tmp_path, monkeypatch):
    log_dir = tmp_path / 'custom-log'
    (tmp_path / 'config.json').write_text(json.dumps({'log_dir': str(log_dir)}), encoding='utf-8')
    monkeypatch.setenv('CCUC_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.delenv('CCUC_LOG_DIR')
    log_dir.mkdir()
    (log_dir / 'log.jsonl').write_text(SAMPLE[1] + '\n', encoding='utf-8')
    code, out = run()
    assert code == 0 and '1 entry, ' in out


# --- Where the log is looked for: the config.json of the standard installation ----------------------------------------

@pytest.fixture
def home_env(tmp_path, monkeypatch):
    """No CCUC_LOG_DIR / CCUC_CONFIG (conftest sets both): only HOME (a temporary folder) decides."""
    monkeypatch.delenv('CCUC_LOG_DIR')
    monkeypatch.delenv('CCUC_CONFIG')
    return tmp_path / 'home'


def install_config(home, log_dir):
    folder = home / '.claude' / 'hooks' / 'usage-clock'
    folder.mkdir(parents=True)
    (folder / 'config.json').write_text(json.dumps({'log_dir': str(log_dir)}), encoding='utf-8')


def put_log(folder, entry=SAMPLE[1]):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'log.jsonl').write_text(entry + '\n', encoding='utf-8')


def test_the_config_of_the_standard_installation_is_used_without_any_variable(home_env, tmp_path):
    put_log(tmp_path / 'from-installed-config')
    install_config(home_env, tmp_path / 'from-installed-config')
    put_log(home_env / '.cache' / 'claude-usage-clock', SAMPLE[3])  # the default folder holds another log
    code, out = run()
    assert code == 0 and '1 entry, ' in out and 'general-purpose' in out, out


def test_without_any_config_the_default_log_folder_is_used(home_env):
    put_log(home_env / '.cache' / 'claude-usage-clock')
    code, out = run()
    assert code == 0 and 'general-purpose' in out, out


def test_ccuc_config_beats_the_standard_installation(home_env, tmp_path, monkeypatch):
    install_config(home_env, tmp_path / 'installed')
    put_log(tmp_path / 'installed', SAMPLE[3])
    (tmp_path / 'other.json').write_text(json.dumps({'log_dir': str(tmp_path / 'other-log')}), encoding='utf-8')
    put_log(tmp_path / 'other-log')
    monkeypatch.setenv('CCUC_CONFIG', str(tmp_path / 'other.json'))
    code, out = run()
    assert code == 0 and 'general-purpose' in out, out


def test_ccuc_log_dir_still_beats_the_installed_config(home_env, tmp_path, monkeypatch):
    install_config(home_env, tmp_path / 'installed')
    put_log(tmp_path / 'installed', SAMPLE[3])
    put_log(tmp_path / 'env-log')
    monkeypatch.setenv('CCUC_LOG_DIR', str(tmp_path / 'env-log'))
    code, out = run()
    assert code == 0 and 'general-purpose' in out, out


def test_config_option_names_the_config_file(home_env, tmp_path):
    install_config(home_env, tmp_path / 'installed')
    put_log(tmp_path / 'installed', SAMPLE[3])
    (tmp_path / 'mine.json').write_text(json.dumps({'log_dir': str(tmp_path / 'mine-log')}), encoding='utf-8')
    put_log(tmp_path / 'mine-log')
    code, out = run('--config', tmp_path / 'mine.json')
    assert code == 0 and 'general-purpose' in out, out


def test_config_option_beats_the_environment_variables(home_env, tmp_path, monkeypatch):
    (tmp_path / 'mine.json').write_text(json.dumps({'log_dir': str(tmp_path / 'mine-log')}), encoding='utf-8')
    put_log(tmp_path / 'mine-log')
    put_log(tmp_path / 'env-log', SAMPLE[3])
    monkeypatch.setenv('CCUC_LOG_DIR', str(tmp_path / 'env-log'))
    monkeypatch.setenv('CCUC_CONFIG', str(tmp_path / 'env-config.json'))
    code, out = run('--config', tmp_path / 'mine.json')
    assert code == 0 and 'general-purpose' in out, out


def test_an_explicit_log_path_beats_every_config(home_env, tmp_path):
    install_config(home_env, tmp_path / 'installed')
    put_log(tmp_path / 'installed', SAMPLE[3])
    (tmp_path / 'mine.json').write_text(json.dumps({'log_dir': str(tmp_path / 'installed')}), encoding='utf-8')
    code, out = run('--config', tmp_path / 'mine.json', write_log(tmp_path, [SAMPLE[1]]))
    assert code == 0 and '1 entry, ' in out and 'general-purpose' in out and 'soft_stop' not in out, out


def test_a_missing_config_file_is_an_argument_error(home_env, tmp_path):
    code, out = run('--config', tmp_path / 'missing.json')
    assert code == 2 and '--config' in out and 'not a file' in out and 'Traceback' not in out


def test_a_broken_config_falls_back_to_the_default_folder(home_env, tmp_path):
    (tmp_path / 'broken.json').write_text('{nope', encoding='utf-8')
    put_log(home_env / '.cache' / 'claude-usage-clock')
    code, out = run('--config', tmp_path / 'broken.json')
    assert code == 0 and 'general-purpose' in out and 'Traceback' not in out, out


def test_the_no_log_message_names_the_config_option(home_env):
    code, out = run()
    assert code == 1 and 'No log at' in out and '--config' in out
    shown = re.search(r'No log at (.*?) - are the hooks', out).group(1)
    assert os.path.normpath(shown) == str(home_env / '.cache' / 'claude-usage-clock' / 'log.jsonl')


def test_config_option_expands_the_home_folder(home_env, tmp_path):
    home_env.mkdir(parents=True)
    (home_env / 'mine.json').write_text(json.dumps({'log_dir': '~/mine-log'}), encoding='utf-8')
    put_log(home_env / 'mine-log')
    code, out = run('--config', '~/mine.json')
    assert code == 0 and 'general-purpose' in out, out


# --- Sync: runs, successes, failures and what shadow mode would have pushed ---------------------------------------------

def sync_line(time, decision, **more):
    return json.dumps({'time': time, 'hook': 'sync', 'decision': decision, 'reason': more.pop('reason', ''), **more})


SYNC_SHADOW = [
    sync_line('2026-10-14T10:00:00+00:00', 'shadow', reason='committed, not pushed', lines=12, bytes=3400),
    sync_line('2026-10-14T10:00:01+00:00', 'success', action='push', mode='shadow'),
    sync_line('2026-10-14T10:30:00+00:00', 'failure', reason='the fetch did not reach the sync repo', action='push',
              mode='shadow', category='network'),
    sync_line('2026-10-14T11:00:00+00:00', 'shadow', reason='nothing new, not pushed', lines=0, bytes=0),
    sync_line('2026-10-14T11:00:01+00:00', 'success', action='push', mode='shadow'),
    sync_line('2026-10-14T11:30:00+00:00', 'failure', reason='the fetch was refused', action='push', mode='shadow',
              category='auth'),
    sync_line('2026-10-14T11:40:00+00:00', 'failure', reason='the fetch did not reach the sync repo', action='push',
              mode='shadow', category='network'),
    sync_line('2026-10-14T12:00:00+00:00', 'success', action='pull', mode='shadow'),
    sync_line('2026-10-14T12:10:00+00:00', 'failure', reason='the run took too long', action='pull', mode='shadow',
              category='timeout'),
    sync_line('2026-10-14T12:20:00+00:00', 'shadow', reason='committed, not pushed', lines=3, bytes=600),
    sync_line('2026-10-14T12:20:01+00:00', 'success', action='push', mode='shadow'),
]


def test_the_sync_section_counts_runs_failures_per_category_and_what_shadow_would_have_pushed(tmp_path):
    code, out = run(write_log(tmp_path, SYNC_SHADOW + [SAMPLE[1]]))
    assert code == 0, out
    section = out[out.index('Sync:'):].split('\n\n')[0].splitlines()
    assert section == ['Sync:',
                       '  push: 6 runs, 3 succeeded, 3 failed (network 2, auth 1)',
                       '  pull: 2 runs, 1 succeeded, 1 failed (timeout 1)',
                       '  shadow: 3 runs committed here and pushed nothing; they would have pushed 15 lines, 4000 bytes']


def test_without_sync_lines_there_is_no_sync_section(tmp_path):
    code, out = run(write_log(tmp_path, SAMPLE))
    assert code == 0 and 'Sync:' not in out


def test_a_sync_section_without_shadow_lines_has_no_shadow_line(tmp_path):
    code, out = run(write_log(tmp_path, [sync_line('2026-10-14T10:00:00+00:00', 'success', action='push', mode='on')]))
    assert code == 0, out
    assert '  push: 1 run, 1 succeeded, 0 failed' in out and 'shadow:' not in out and 'pull:' not in out


def test_odd_sync_fields_do_not_crash_the_section(tmp_path):
    odd = [sync_line('2026-10-14T10:00:00+00:00', 'shadow', lines='many', bytes=True),
           sync_line('2026-10-14T10:00:01+00:00', 'shadow', lines=2.5, bytes=-1),
           sync_line('2026-10-14T10:00:02+00:00', 'failure', action='push', category=['x']),
           sync_line('2026-10-14T10:00:03+00:00', 'success', action=7)]
    code, out = run(write_log(tmp_path, odd))
    assert code == 0 and 'Traceback' not in out, out
    assert '  push: 1 run, 0 succeeded, 1 failed (? 1)' in out
    assert 'they would have pushed 0 lines, 0 bytes' in out


def test_the_sync_section_reads_what_the_sync_core_writes(tmp_path, monkeypatch):
    import _sync_core
    log_dir = str(tmp_path / 'log')
    _sync_core.log(log_dir, 'shadow', 'committed, not pushed', lines=4, bytes=100)
    _sync_core.log(log_dir, 'success', action='push', mode='shadow')
    _sync_core.log(log_dir, 'failure', 'the push was refused', action='push', mode='shadow', category='auth')
    code, out = run()      # <CCUC_LOG_DIR>/log.jsonl
    assert code == 0, out
    assert '  push: 2 runs, 1 succeeded, 1 failed (auth 1)' in out
    assert 'they would have pushed 4 lines, 100 bytes' in out
