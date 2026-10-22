"""Tests for the section `modules` of config.json (0034-REPORT-SETTINGS): the four hooks that can be switched off, the
mode of the gates from the config before the --mode of their entry, and what install.py writes and checks.

The hooks run as subprocesses, as Claude Code starts them: JSON in, JSON out. conftest points the log folder, the
usage file and the config into tmp_path."""
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

import _config
import _hookio
from conftest import HOOKS, ROOT

INSTALL = os.path.join(ROOT, 'tools', 'install.py')
AGENT = {'hook_event_name': 'PreToolUse', 'tool_name': 'Agent', 'session_id': 's1',
         'tool_input': {'subagent_type': 'general-purpose', 'prompt': 'p'}}
CALL = {'hook_event_name': 'PreToolUse', 'tool_name': 'Read', 'agent_id': 'a1', 'session_id': 's1',
        'tool_input': {'file_path': '/tmp/x'}}
PROMPT = {'hook_event_name': 'UserPromptSubmit', 'prompt': 'hi', 'session_id': 's1'}


def config(tmp_path, **modules):
    path = tmp_path / 'no-config.json'      # the file conftest points CCUC_CONFIG to
    path.write_text(json.dumps({'modules': modules} if modules else {}), encoding='utf-8')


def usage(tmp_path, five):
    now = time.time()
    (tmp_path / 'usage.json').write_text(json.dumps({
        'five_hour': {'used_percentage': five, 'resets_at': now + 3600},
        'seven_day': {'used_percentage': 10, 'resets_at': now + 86400},
        'measured_at': datetime.fromtimestamp(now - 60, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}), encoding='utf-8')


def run(script, hook_input, *args):
    done = subprocess.run([sys.executable, os.path.join(HOOKS, script), *args], input=json.dumps(hook_input),
                          capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def log(tmp_path, hook):
    path = tmp_path / 'log' / 'log.jsonl'
    if not path.exists():
        return []
    return [r for r in map(json.loads, path.read_text(encoding='utf-8').splitlines()) if r.get('hook') == hook]


def decision(stdout):
    return json.loads(stdout)['hookSpecificOutput'].get('permissionDecision') if stdout.strip() else None


def context_state(tmp_path, session='s1'):
    folder = tmp_path / 'log' / 'context'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{session}.json').write_text(json.dumps({
        'session_id': session, 'context_window_size': 1_000_000, 'used_tokens': 123_456,
        'measured_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}), encoding='utf-8')


# --- The config -------------------------------------------------------------------------------------------------

def test_the_defaults_switch_everything_on_and_leave_the_mode_to_the_entry():
    assert _config.DEFAULTS['modules'] == {'desktop_line': 'full', 'usage_clock': True, 'context_reporter': True,
                                           'spawn_gate': '', 'soft_stop': ''}
    with open(os.path.join(ROOT, 'config.example.json'), encoding='utf-8') as f:
        assert json.load(f)['modules'] == _config.DEFAULTS['modules']


@pytest.mark.parametrize('modules,problem', [
    ({'usage_clock': 'no'}, 'modules.usage_clock: wrong type'),
    ({'context_reporter': 0}, 'modules.context_reporter: wrong type'),
    ({'spawn_gate': 'loud'}, 'modules.spawn_gate: must be one of |off|shadow|enforce'),
    ({'soft_stop': True}, 'modules.soft_stop: wrong type')])
def test_a_broken_value_is_the_default_with_a_problem_line(tmp_path, modules, problem):
    config(tmp_path, **modules)
    cfg = _config.load()
    assert cfg['modules'] == _config.DEFAULTS['modules']
    assert any(problem in r['reason'] for r in log(tmp_path, 'config')), log(tmp_path, 'config')


@pytest.mark.parametrize('value,argument,expected', [('', 'shadow', 'shadow'), ('', 'enforce', 'enforce'),
                                                     ('shadow', 'enforce', 'shadow'), ('enforce', 'shadow', 'enforce'),
                                                     ('off', 'enforce', 'off')])
def test_the_mode_of_a_gate_is_the_config_before_the_argument(value, argument, expected):
    cfg = dict(_config.DEFAULTS, modules=dict(_config.DEFAULTS['modules'], spawn_gate=value))
    assert _config.gate_mode(cfg, 'spawn_gate', argument) == expected


# --- The hooks switched off -------------------------------------------------------------------------------------

def test_the_usage_clock_switched_off_adds_nothing_and_logs_once_per_session(tmp_path):
    config(tmp_path, usage_clock=False)
    assert run('usage_clock.py', PROMPT) == ''
    assert run('usage_clock.py', PROMPT) == ''
    assert run('usage_clock.py', dict(PROMPT, session_id='s2')) == ''
    lines = log(tmp_path, 'usage_clock')
    assert [r['decision'] for r in lines] == ['disabled', 'disabled']
    assert 'modules.usage_clock' in lines[0]['reason'] and 'prompt' not in json.dumps(lines)


def test_the_usage_clock_switched_on_or_without_the_key_adds_its_line(tmp_path):
    for modules in ({'usage_clock': True}, {}):
        config(tmp_path, **modules)
        assert 'Now:' in json.loads(run('usage_clock.py', PROMPT))['hookSpecificOutput']['additionalContext']
    assert log(tmp_path, 'usage_clock') == []


def test_the_context_reporter_switched_off_adds_nothing_and_logs_once_per_session(tmp_path):
    context_state(tmp_path)
    config(tmp_path)
    assert 'Context:' in run('context_reporter.py', PROMPT)
    config(tmp_path, context_reporter=False)
    assert run('context_reporter.py', PROMPT) == '' and run('context_reporter.py', PROMPT) == ''
    assert [r['decision'] for r in log(tmp_path, 'context_reporter')] == ['disabled']


def test_the_spawn_gate_switched_off_lets_the_start_through_without_a_decision(tmp_path):
    usage(tmp_path, 90)
    config(tmp_path)
    assert decision(run('spawn_gate.py', AGENT, '--mode=enforce')) == 'deny'
    config(tmp_path, spawn_gate='off')
    assert run('spawn_gate.py', AGENT, '--mode=enforce') == ''
    assert run('spawn_gate.py', AGENT, '--mode=enforce') == ''
    assert [r['decision'] for r in log(tmp_path, 'spawn_gate')] == ['deny', 'disabled']


def test_the_spawn_gate_runs_in_the_mode_of_the_config_before_the_entry(tmp_path):
    usage(tmp_path, 90)
    config(tmp_path, spawn_gate='shadow')
    out = run('spawn_gate.py', AGENT, '--mode=enforce')
    assert decision(out) is None and 'would have been denied' in out
    config(tmp_path, spawn_gate='enforce')
    assert decision(run('spawn_gate.py', AGENT, '--mode=shadow')) == 'deny'
    assert [r['mode'] for r in log(tmp_path, 'spawn_gate')] == ['shadow', 'enforce']


def test_the_spawn_gate_without_a_mode_in_the_config_runs_in_the_mode_of_its_entry(tmp_path):
    usage(tmp_path, 90)
    config(tmp_path, spawn_gate='')
    assert decision(run('spawn_gate.py', AGENT, '--mode=enforce')) == 'deny'
    assert 'would have been denied' in run('spawn_gate.py', AGENT, '--mode=shadow')


def test_the_soft_stop_switched_off_lets_the_call_through_and_counts_nothing(tmp_path):
    usage(tmp_path, 97)
    config(tmp_path, soft_stop='off')
    assert run('soft_stop.py', CALL, '--mode=enforce') == ''
    assert run('soft_stop.py', CALL, '--mode=enforce') == ''
    assert [r['decision'] for r in log(tmp_path, 'soft_stop')] == ['disabled']
    assert not (tmp_path / 'log' / 'counters').exists()


def test_the_soft_stop_runs_in_the_mode_of_the_config_before_the_entry(tmp_path):
    usage(tmp_path, 97)
    config(tmp_path, soft_stop='enforce')
    assert decision(run('soft_stop.py', CALL, '--mode=shadow')) == 'deny'
    config(tmp_path, soft_stop='shadow')
    assert decision(run('soft_stop.py', CALL, '--mode=enforce')) is None
    config(tmp_path, soft_stop='')
    assert decision(run('soft_stop.py', CALL, '--mode=enforce')) == 'deny'


def test_disabled_is_logged_once_for_each_of_the_last_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(_hookio, 'DISABLED_SESSIONS', 2)
    for session in ('a', 'b', 'a', 'c', 'a'):
        _hookio.log_disabled('usage_clock', 'usage_clock', {'session_id': session})
    assert len(log(tmp_path, 'usage_clock')) == 4      # a, b, c, then a again: it fell out of the last two


def test_a_broken_list_of_sessions_logs_again_and_is_written_new(tmp_path):
    folder = tmp_path / 'log' / 'disabled'
    folder.mkdir(parents=True)
    (folder / 'usage_clock.json').write_text('{broken', encoding='utf-8')
    _hookio.log_disabled('usage_clock', 'usage_clock', {})
    assert len(log(tmp_path, 'usage_clock')) == 1
    assert json.loads((folder / 'usage_clock.json').read_text(encoding='utf-8')) == ['']


def test_a_list_of_sessions_nested_too_deep_logs_again_and_never_raises(tmp_path):
    folder = tmp_path / 'log' / 'disabled'
    folder.mkdir(parents=True)
    (folder / 'spawn_gate.json').write_text('[' * 100_000 + ']' * 100_000, encoding='utf-8')
    _hookio.log_disabled('spawn_gate', 'spawn_gate', {'session_id': 's'})
    assert len(log(tmp_path, 'spawn_gate')) == 1
    assert json.loads((folder / 'spawn_gate.json').read_text(encoding='utf-8')) == ['s']


def test_the_spawn_gate_switched_off_stays_silent_with_a_list_of_sessions_nested_too_deep(tmp_path):
    folder = tmp_path / 'log' / 'disabled'
    folder.mkdir(parents=True)
    (folder / 'spawn_gate.json').write_text('[' * 100_000 + ']' * 100_000, encoding='utf-8')
    config(tmp_path, spawn_gate='off')
    assert run('spawn_gate.py', AGENT, '--mode=enforce') == ''
    assert [r['decision'] for r in log(tmp_path, 'spawn_gate')] == ['disabled']


def test_logging_disabled_never_raises(tmp_path, monkeypatch):
    def broken(*args):
        raise OSError('disk full')
    import _context_state
    monkeypatch.setattr(_context_state, 'write_private', broken)
    _hookio.log_disabled('usage_clock', 'usage_clock', {'session_id': 's'})
    assert len(log(tmp_path, 'usage_clock')) == 1


# --- install.py -------------------------------------------------------------------------------------------------

def load_install():
    spec = importlib.util.spec_from_file_location('install_modules_under_test', INSTALL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('CCUC_CONFIG')       # the config of the installation is <target>/config.json
    settings, target = tmp_path / 'settings.json', tmp_path / 'target'
    settings.write_text('{}', encoding='utf-8')
    module = load_install()

    def install(*args):
        code = module.main(['--settings', str(settings), '--target', str(target), *args])
        return code, capsys.readouterr().out
    install.config = lambda: json.loads((target / 'config.json').read_text(encoding='utf-8'))
    install.target = target
    return install


def test_install_with_mode_writes_the_mode_of_each_gate_it_installs(setup):
    assert setup('--components', 'usage-clock,spawn-gate', '--mode', 'enforce')[0] == 0
    assert setup.config()['modules'] == dict(_config.DEFAULTS['modules'], spawn_gate='enforce')
    code, out = setup('--components', 'soft-stop', '--mode', 'shadow')
    assert code == 0 and "modules.soft_stop '' -> 'shadow'" in out
    assert setup.config()['modules'] == dict(_config.DEFAULTS['modules'], spawn_gate='enforce', soft_stop='shadow')
    assert len(list(setup.target.glob('config.json.bak-usage-clock-*'))) == 1


def test_install_without_mode_leaves_config_json_byte_for_byte(setup):
    assert setup('--components', 'usage-clock,spawn-gate')[0] == 0
    before = (setup.target / 'config.json').read_bytes()
    assert 'modules' in json.loads(before)      # the copy of config.example.json
    assert setup('--components', 'usage-clock,spawn-gate,soft-stop')[0] == 0
    assert (setup.target / 'config.json').read_bytes() == before


def test_install_with_mode_on_a_config_without_modules_adds_only_the_gate(setup):
    assert setup('--components', 'spawn-gate')[0] == 0
    (setup.target / 'config.json').write_text('{"sync": {"mode": "off"}}', encoding='utf-8')
    assert setup('--components', 'spawn-gate', '--mode', 'enforce')[0] == 0
    assert setup.config() == {'sync': {'mode': 'off'}, 'modules': {'spawn_gate': 'enforce'}}


def test_install_with_the_same_mode_again_writes_nothing(setup):
    assert setup('--components', 'spawn-gate', '--mode', 'enforce')[0] == 0
    before = (setup.target / 'config.json').read_bytes()
    code, out = setup('--components', 'spawn-gate', '--mode', 'enforce')
    assert code == 0 and (setup.target / 'config.json').read_bytes() == before
    assert not list(setup.target.glob('config.json.bak-usage-clock-*'))


def test_a_dry_run_with_mode_writes_no_config(setup):
    assert setup('--components', 'spawn-gate')[0] == 0
    before = (setup.target / 'config.json').read_bytes()
    code, out = setup('--components', 'spawn-gate', '--mode', 'enforce', '--dry-run')
    assert code == 0 and "modules.spawn_gate '' -> 'enforce' would be written" in out
    assert (setup.target / 'config.json').read_bytes() == before


GATE_OFF = ('NOTE spawn_gate.py: switched off in config.json (modules.spawn_gate "off"), it lets every start through. '
            'Switch it on with install.py --on spawn-gate or in the Settings tab of /report.')
GATE_SHADOW = ('NOTE spawn_gate.py: runs in shadow mode from config.json (modules.spawn_gate "shadow") although its entry '
               'has --mode=enforce: it blocks nothing.')


@pytest.mark.parametrize('value,expected,note', [('', 'mode enforce from settings.json', None),
                                                 ('enforce', 'mode enforce from config', None),
                                                 ('shadow', 'mode shadow from config', GATE_SHADOW),
                                                 ('off', 'mode off from config', GATE_OFF)])
def test_check_names_where_the_mode_comes_from_and_notes_a_gate_switched_off(setup, value, expected, note):
    assert setup('--components', 'spawn-gate', '--mode', 'enforce')[0] == 0
    cfg = setup.config()
    cfg['modules']['spawn_gate'] = value
    (setup.target / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
    code, out = setup('--check', '--components', 'spawn-gate')
    assert f'OK   PreToolUse: spawn_gate.py (matcher Agent|Workflow|Skill, {expected})' in out
    assert [line for line in out.splitlines() if line.startswith('NOTE spawn_gate')] == ([note] if note else [])
    assert code == 0, out


def test_check_notes_no_gate_in_shadow_mode_from_its_own_entry(setup):
    assert setup('--components', 'soft-stop', '--mode', 'shadow')[0] == 0
    assert 'NOTE soft_stop' not in setup('--check', '--components', 'soft-stop')[1]


@pytest.mark.parametrize('component,script,key', [('usage-clock', 'usage_clock.py', 'usage_clock'),
                                                  ('context-reporter', 'context_reporter.py', 'context_reporter'),
                                                  ('soft-stop', 'soft_stop.py', 'soft_stop')])
def test_check_notes_each_part_switched_off_once(setup, component, script, key):
    assert setup('--components', f'statusline,{component}')[0] == 0
    assert setup('--off', component)[0] == 0
    code, out = setup('--check', '--components', f'statusline,{component}')
    value = '"off"' if component == 'soft-stop' else 'false'
    notes = [line for line in out.splitlines() if line.startswith(f'NOTE {script}')]
    assert len(notes) == 1 and f'switched off in config.json (modules.{key} {value})' in notes[0], out
    assert f'install.py --on {component}' in notes[0] and code == 0
    assert setup('--on', component)[0] == 0
    assert f'NOTE {script}' not in setup('--check', '--components', f'statusline,{component}')[1]


def test_install_with_mode_and_a_config_json_that_cannot_be_read_notes_it_and_installs(setup):
    assert setup('--components', 'spawn-gate')[0] == 0
    (setup.target / 'config.json').write_text('{broken', encoding='utf-8')
    code, out = setup('--components', 'spawn-gate', '--mode', 'enforce')
    assert code == 0 and 'Note: modules.spawn_gate not written' in out and "set it to 'enforce' by hand" in out
    assert (setup.target / 'config.json').read_text(encoding='utf-8') == '{broken'


def env_config(setup, tmp_path, value):
    data = json.loads((tmp_path / 'settings.json').read_text(encoding='utf-8'))
    data['env'] = {'CCUC_CONFIG': value}
    (tmp_path / 'settings.json').write_text(json.dumps(data), encoding='utf-8')


def test_check_notes_a_config_json_that_only_the_env_of_settings_json_names(setup, tmp_path):
    assert setup('--components', 'usage-clock')[0] == 0
    env_config(setup, tmp_path, str(tmp_path / 'elsewhere.json'))
    code, out = setup('--check', '--components', 'usage-clock')
    assert (f"NOTE config.json: the env of settings.json sets CCUC_CONFIG to {str(tmp_path / 'elsewhere.json')!r}, so the "
            f'hooks read that file, but this installer and the Settings tab read and write {setup.target / "config.json"}'
            in out), out


@pytest.mark.parametrize('same', ['target', 'environment'])
def test_check_notes_nothing_when_the_env_of_settings_json_names_the_same_config_json(setup, tmp_path, monkeypatch, same):
    assert setup('--components', 'usage-clock')[0] == 0
    path = str(setup.target / 'config.json') if same == 'target' else str(tmp_path / 'elsewhere.json')
    if same == 'environment':
        monkeypatch.setenv('CCUC_CONFIG', path)
    env_config(setup, tmp_path, path)
    assert 'CCUC_CONFIG' not in setup('--check', '--components', 'usage-clock')[1]
