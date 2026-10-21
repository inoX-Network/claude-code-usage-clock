"""Tests for hooks/settings_cli.py (0034-REPORT-SETTINGS), the one way the Settings tab of /report writes: show --json,
set, check-repo and set-repo. It changes config.json only, never settings.json.

The installation is made by install.py below tmp_path; the script then runs in this process (main(argv)) with
--settings and --target, so that the visibility probe and the check with the login can be functions handed in and
coverage sees it. One test runs the installed copy as the plugin does. git runs with a config of its own (git_alone)."""
import importlib.util
import io
import json
import os
import subprocess
import sys

import pytest

import _sync_core
import _sync_git
import settings_cli
from conftest import ROOT
from test_sync_git import ASKS, REPO, answer, git_alone, remote  # noqa: F401

INSTALL = os.path.join(ROOT, 'tools', 'install.py')
OTHER = 'anna/usage-sync'


def load_install():
    spec = importlib.util.spec_from_file_location('install_settings_under_test', INSTALL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Machine:
    def __init__(self, tmp_path, monkeypatch, capsys):
        self.tmp, self.monkeypatch, self.capsys = tmp_path, monkeypatch, capsys
        self.settings, self.target = tmp_path / 'settings.json', tmp_path / 'target'
        self.settings.write_text('{}', encoding='utf-8')
        self.install_module = load_install()
        monkeypatch.delenv('CCUC_CONFIG')
        real = _sync_git.probe_visibility
        self.probe = answer(128, ASKS)
        monkeypatch.setattr(_sync_git, 'probe_visibility', lambda repo, directory, deadline=None, execute=None,
                            api=None: real(repo, directory, deadline, self.probe, api))
        self.access = 'reachable'
        monkeypatch.setattr(_sync_git, 'check_access', lambda repo, transport, directory, deadline=None: self.access)

    def install(self, *args):
        code = self.install_module.main(['--settings', str(self.settings), '--target', str(self.target), *args])
        out = self.capsys.readouterr().out
        assert code == 0, out
        return out

    def cli(self, *args):
        code = settings_cli.main(['--settings', str(self.settings), '--target', str(self.target), *args])
        out = self.capsys.readouterr().out
        return code, json.loads(out)

    @property
    def config(self):
        return self.target / 'config.json'

    def config_data(self):
        return json.loads(self.config.read_text(encoding='utf-8'))

    def tree(self):
        return {str(p.relative_to(self.tmp)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in sorted(self.tmp.rglob('*')) if p.is_file() and 'remotes' not in p.parts
                and 'gitconfig' not in p.name}

    def settings_state(self):
        return self.settings.read_bytes(), self.settings.stat().st_mtime_ns


@pytest.fixture
def machine(tmp_path, monkeypatch, capsys, remote):
    return Machine(tmp_path, monkeypatch, capsys)


def modules(shown):
    return {m['id']: m for m in shown['modules']}


# --- show -------------------------------------------------------------------------------------------------------

def test_show_lists_each_part_installed_or_not_with_its_switch(machine):
    machine.install('--components', 'statusline,usage-clock,spawn-gate', '--mode', 'enforce')
    code, shown = machine.cli('show', '--json')
    assert code == 0
    found = modules(shown)
    assert list(found) == ['statusline', 'usage_clock', 'context_reporter', 'spawn_gate', 'soft_stop']
    assert found['statusline']['installed'] and found['statusline']['choices'] == []
    assert found['usage_clock'] == dict(found['usage_clock'], installed=True, key='modules.usage_clock', value=True,
                                        choices=['true', 'false'])
    assert not found['context_reporter']['installed'] and not found['soft_stop']['installed']
    assert found['spawn_gate'] == dict(found['spawn_gate'], installed=True, value='enforce', installed_mode='enforce',
                                       choices=['off', 'shadow', 'enforce'])
    assert shown['sync'] == dict(shown['sync'], installed=False, repo='', mode='off', last_success=None,
                                 last_error=None)


def test_show_names_the_mode_of_the_entry_when_the_config_has_none(machine):
    machine.install('--components', 'soft-stop')
    found = modules(machine.cli('show', '--json')[1])
    assert found['soft_stop']['value'] == '' and found['soft_stop']['installed_mode'] == 'shadow'


def test_show_carries_the_texts_of_the_assistant_byte_for_byte_and_a_sentence_for_the_tab(machine):
    machine.install('--components', 'statusline,usage-clock,context-reporter,spawn-gate,soft-stop')
    shown = machine.cli('show', '--json')[1]
    info = machine.install_module.INFO
    for module_id, key in (('statusline', 'statusline'), ('usage_clock', 'usage-clock'),
                           ('context_reporter', 'context-reporter'), ('spawn_gate', 'spawn-gate'),
                           ('soft_stop', 'soft-stop')):
        assert modules(shown)[module_id]['info'] == '\n'.join(info[key])
        assert modules(shown)[module_id]['tab']
        assert modules(shown)[module_id]['summary'] == machine.install_module.SUMMARY[key]
    assert shown['sync']['info'] == '\n'.join(info['sync']) and shown['sync']['repo_info'] == '\n'.join(info['repo'])
    assert shown['sync']['tab'] and shown['sync']['repo_tab']


def test_show_gives_the_sentence_of_the_sync_without_what_a_first_install_does(machine):
    # The tab installs nothing: the half of the assistant's sentence about the first install is left out
    sync = machine.cli('show', '--json')[1]['sync']
    assert sync['summary'] == ('Puts the usage of each computer into a private GitHub repository of yours, so that the '
                               'report shows all of them.')
    assert machine.install_module.SUMMARY['sync'].startswith(sync['summary'][:-1] + '; ')


def test_the_assistant_prints_exactly_the_text_show_carries(machine, monkeypatch):
    machine.install('--components', 'spawn-gate')
    text = modules(machine.cli('show', '--json')[1])['spawn_gate']['info']
    printed = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', printed)
    answers = iter(['?', 'n'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    machine.install_module.ask('explained', 'Spawn gate?', True, info='spawn-gate')
    monkeypatch.setattr(sys, 'stdout', sys.__stdout__)
    assert text + '\n' in printed.getvalue()


def test_show_reads_the_state_of_the_sync(machine):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    _sync_core.write_state(_sync_core.ensure_dir(os.environ['CCUC_LOG_DIR']),
                           _sync_core.State(last_success=1_760_000_000.0, last_error='network', backoff_level=1))
    sync = machine.cli('show', '--json')[1]['sync']
    assert sync == dict(sync, installed=True, repo=REPO, mode='shadow', last_error='network')
    assert sync['last_success'].startswith('2025-10-09')


def test_show_with_a_broken_state_of_the_sync_names_none(machine):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    (machine.tmp / 'log' / 'sync' / 'state.json').write_text('{broken', encoding='utf-8')
    sync = machine.cli('show', '--json')[1]['sync']
    assert sync['last_success'] is None and sync['last_error'] is None


def test_show_names_the_problems_of_config_json(machine):
    machine.install('--components', 'usage-clock')
    machine.config.write_text('{"modules": {"usage_clock": "no"}}', encoding='utf-8')
    shown = machine.cli('show', '--json')[1]
    assert modules(shown)['usage_clock']['value'] is True
    assert any('modules.usage_clock' in problem for problem in shown['problems'])


# --- set --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('key,value,stored', [('modules.usage_clock', 'false', False),
                                              ('modules.context_reporter', 'false', False),
                                              ('modules.spawn_gate', 'off', 'off'),
                                              ('modules.soft_stop', 'enforce', 'enforce'),
                                              ('modules.spawn_gate', '', '')])
def test_set_writes_only_that_key_with_a_backup(machine, key, value, stored):
    machine.install('--components', 'usage-clock,context-reporter,spawn-gate,soft-stop', '--mode', 'shadow')
    before = machine.config_data()
    code, done = machine.cli('set', key, value)
    assert code == 0 and done == dict(done, key=key, value=stored)
    expected = json.loads(json.dumps(before))
    expected['modules'][key.split('.')[1]] = stored
    assert machine.config_data() == expected
    assert len(list(machine.target.glob('config.json.bak-usage-clock-*'))) == (1 if done['changed'] else 0)


def test_set_to_the_value_it_has_writes_nothing(machine):
    machine.install('--components', 'usage-clock')
    before = machine.tree()
    code, done = machine.cli('set', 'modules.usage_clock', 'true')
    assert code == 0 and done['changed'] is False and machine.tree() == before


def test_set_on_a_config_without_modules_adds_the_one_key(machine):
    machine.install('--components', 'usage-clock')
    machine.config.write_text('{"log_dir": "~/x"}', encoding='utf-8')
    assert machine.cli('set', 'modules.usage_clock', 'false')[0] == 0
    assert machine.config_data() == {'log_dir': '~/x', 'modules': {'usage_clock': False}}


def test_set_sync_mode_on_writes_what_the_installer_writes(machine):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    start = machine.config.read_bytes()
    assert machine.cli('set', 'sync.mode', 'on')[0] == 0
    by_tab = machine.config.read_bytes()
    machine.config.write_bytes(start)
    machine.install('--components', 'sync', '--sync-mode', 'on')
    assert machine.config.read_bytes() == by_tab and json.loads(by_tab)['sync']['mode'] == 'on'


@pytest.mark.parametrize('key,value', [('modules.usage_clock', 'yes'), ('modules.spawn_gate', 'on'),
                                       ('modules.statusline', 'false'), ('sync.repo', 'a/b'), ('thresholds.warn', '1'),
                                       ('sync.mode', 'loud'), ('modules', 'x')])
def test_set_refuses_a_key_or_value_outside_the_table(machine, key, value):
    machine.install('--components', 'usage-clock,spawn-gate,sync', '--sync-repo', REPO)
    before = machine.tree()
    code, done = machine.cli('set', key, value)
    assert code == 2 and 'error' in done and machine.tree() == before


@pytest.mark.parametrize('key,value', [('modules.usage_clock', 'false'), ('modules.context_reporter', 'false'),
                                       ('modules.spawn_gate', 'off'), ('modules.soft_stop', 'off'),
                                       ('sync.mode', 'on')])
def test_set_needs_the_part_installed(machine, key, value):
    machine.install('--components', 'statusline')
    before = machine.tree()
    code, done = machine.cli('set', key, value)
    assert code == 2 and 'install.py' in done['error'] and machine.tree() == before


def test_set_with_a_config_json_that_cannot_be_read_changes_nothing(machine):
    machine.install('--components', 'usage-clock')
    machine.config.write_text('[1]', encoding='utf-8')
    code, done = machine.cli('set', 'modules.usage_clock', 'false')
    assert code == 2 and 'JSON object' in done['error'] and machine.config.read_text(encoding='utf-8') == '[1]'


def test_set_without_config_json_changes_nothing(machine):
    machine.install('--components', 'usage-clock')
    os.replace(machine.config, machine.tmp / 'moved.json')
    code, done = machine.cli('set', 'modules.usage_clock', 'false')
    assert code == 2 and 'install.py' in done['error'] and not machine.config.exists()


def test_set_that_cannot_replace_config_json_answers_json_and_leaves_no_temporary_file(machine, monkeypatch):
    machine.install('--components', 'usage-clock')
    before = machine.config.read_bytes()

    def refused(source, destination):
        raise PermissionError(13, 'Access is denied', destination)
    monkeypatch.setattr(settings_cli._setup._plat, 'replace', refused)
    code, done = machine.cli('set', 'modules.usage_clock', 'false')
    assert code == 1 and 'Access is denied' in done['error']
    assert machine.config.read_bytes() == before
    assert not list(machine.target.glob('*' + settings_cli._setup.TEMP_SUFFIX))


def test_an_unusable_settings_json_is_an_error_for_every_command(machine):
    machine.settings.write_text('[1]', encoding='utf-8')
    for args in (['show', '--json'], ['set', 'modules.usage_clock', 'false'], ['check-repo', REPO],
                 ['set-repo', REPO]):
        code, done = machine.cli(*args)
        assert code == 2 and 'JSON object' in done['error']


# --- check-repo -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('probe,access,result', [
    ((128, ASKS), 'reachable', 'private_ok'),
    ((0, ''), 'reachable', 'public'),
    ((1, ''), 'reachable', 'unclear'),
    ((128, ASKS), 'not_found_or_no_access', 'not_found_or_no_access'),
    ((128, ASKS), 'unclear', 'unclear')])
def test_check_repo_gives_each_result_and_writes_nothing(machine, probe, access, result):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    machine.probe, machine.access = answer(*probe), access
    before = machine.tree()
    code, done = machine.cli('check-repo', OTHER)
    assert done == dict(done, result=result, repo=OTHER) and done['text']
    assert code == (0 if result == 'private_ok' else 1)
    assert machine.tree() == before


@pytest.mark.parametrize('private, result', [(True, 'private_ok'), (False, 'public'), (None, 'unclear')])
def test_check_repo_behind_a_proxy_that_adds_credentials_takes_the_answer_of_the_api(machine, monkeypatch, private,
                                                                                      result):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    machine.probe = answer(0)
    monkeypatch.setattr(_sync_git, 'ask_api', lambda repo, seconds: (None, 'timeout') if private is None else (
        json.dumps({'private': private, 'full_name': repo}).encode(), ''))
    before = machine.tree()
    code, done = machine.cli('check-repo', OTHER)
    assert (code, done['result']) == ((0 if private else 1), result)
    assert machine.tree() == before                        # writes nothing, also no log line


def test_check_repo_refuses_a_name_that_is_no_owner_name_without_asking_github(machine):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    machine.probe = answer(128, ASKS)
    code, done = machine.cli('check-repo', 'https://github.com/a/b')
    assert code == 1 and done['result'] == 'invalid' and machine.probe.calls == []


def test_check_repo_needs_the_sync_installed(machine):
    machine.install('--components', 'usage-clock')
    code, done = machine.cli('check-repo', OTHER)
    assert code == 2 and 'install.py' in done['error']


# --- set-repo ---------------------------------------------------------------------------------------------------

def test_set_repo_after_a_private_check_sets_the_repo_and_clones_it(machine):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    cfg = machine.config_data()
    cfg['sync']['repo'] = OTHER
    machine.config.write_text(json.dumps(cfg), encoding='utf-8')
    os.replace(machine.tmp / 'log' / 'sync' / 'repo', machine.tmp / 'old-clone')
    code, done = machine.cli('set-repo', REPO)
    assert code == 0 and done['result'] == 'saved', done
    assert machine.config_data()['sync']['repo'] == REPO and f"sync.repo '{OTHER}' -> '{REPO}'" in done['text']
    assert (machine.tmp / 'log' / 'sync' / 'repo' / 'README.md').read_text(encoding='utf-8') == 'sync\n'
    assert len(list(machine.target.glob('config.json.bak-usage-clock-*'))) == 1


def relative_log_dir(machine, where):
    """A log folder the hooks would resolve against the folder of each session; the tab runs in the folder of its own."""
    machine.install('--components', 'sync', '--sync-repo', REPO)
    machine.monkeypatch.chdir(machine.tmp)
    if where == 'env':
        machine.monkeypatch.setenv('CCUC_LOG_DIR', 'rel-log')
    else:
        machine.monkeypatch.delenv('CCUC_LOG_DIR')
        machine.config.write_text(json.dumps(dict(machine.config_data(), log_dir='rel-log')), encoding='utf-8')
    return machine.tree()


@pytest.mark.parametrize('where', ['env', 'config'])
def test_set_repo_with_a_relative_log_folder_clones_nothing(machine, where):
    before = relative_log_dir(machine, where)
    code, done = machine.cli('set-repo', OTHER)
    assert code == 2 and "needs an absolute log folder, not 'rel-log'" in done['error']
    assert machine.tree() == before and not (machine.tmp / 'rel-log').exists()


@pytest.mark.parametrize('where', ['env', 'config'])
def test_set_sync_mode_with_a_relative_log_folder_switches_only_off(machine, where):
    before = relative_log_dir(machine, where)
    for mode in ('shadow', 'on'):
        code, done = machine.cli('set', 'sync.mode', mode)
        assert code == 2 and 'absolute log folder' in done['error'] and machine.tree() == before
    code, done = machine.cli('set', 'sync.mode', 'off')
    assert code == 0 and machine.config_data()['sync']['mode'] == 'off'


@pytest.mark.parametrize('probe,access,result', [((0, ''), 'reachable', 'public'),
                                                 ((128, ASKS), 'not_found_or_no_access', 'not_found_or_no_access'),
                                                 ((1, ''), 'reachable', 'unclear')])
def test_set_repo_without_a_private_check_changes_nothing(machine, probe, access, result):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    machine.probe, machine.access = answer(*probe), access
    before = machine.tree()
    code, done = machine.cli('set-repo', OTHER)
    assert code == 1 and done['result'] == result and machine.tree() == before


def test_set_repo_with_an_invalid_name_changes_nothing(machine):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    before = machine.tree()
    code, done = machine.cli('set-repo', 'owner')
    assert code == 1 and done['result'] == 'invalid' and machine.tree() == before


def test_set_repo_whose_clone_fails_reports_it_and_writes_no_repo(machine):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    os.replace(machine.tmp / 'log' / 'sync' / 'repo', machine.tmp / 'old-clone')
    code, done = machine.cli('set-repo', 'nobody/missing')       # no bare repository of that name: the clone fails
    assert code == 1 and done['result'] == 'failed' and 'Nothing was installed.' in done['text']
    assert machine.config_data()['sync']['repo'] == REPO


def test_set_repo_lifts_the_block_after_a_public_repo(machine):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    log_dir = os.environ['CCUC_LOG_DIR']
    _sync_core.write_state(_sync_core.ensure_dir(log_dir), _sync_core.State(last_error='visibility'))
    code, done = machine.cli('set-repo', REPO)
    assert code == 0 and not _sync_core.read_state(os.path.join(log_dir, 'sync')).blocked
    assert 'the block after a public repo is lifted' in done['text']


def test_set_repo_whose_block_cannot_be_lifted_reports_it(machine, monkeypatch):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    log_dir = os.environ['CCUC_LOG_DIR']
    _sync_core.write_state(_sync_core.ensure_dir(log_dir), _sync_core.State(last_error='visibility'))
    monkeypatch.setattr(_sync_core, 'clear_visibility', lambda log_dir, now: False)
    cfg = machine.config_data()
    cfg['sync']['repo'] = OTHER
    machine.config.write_text(json.dumps(cfg), encoding='utf-8')
    code, done = machine.cli('set-repo', REPO)
    assert code == 1 and done['result'] == 'failed' and 'holds the lock' in done['text']
    assert machine.config_data()['sync']['repo'] == OTHER


def test_set_repo_needs_the_sync_installed(machine):
    machine.install('--components', 'usage-clock')
    code, done = machine.cli('set-repo', OTHER)
    assert code == 2 and 'install.py' in done['error']


# --- settings.json stays as it is -------------------------------------------------------------------------------

def test_no_command_writes_settings_json(machine):
    machine.install('--components', 'statusline,usage-clock,context-reporter,spawn-gate,soft-stop,sync',
                    '--sync-repo', REPO)
    for args in (['show', '--json'], ['set', 'modules.usage_clock', 'false'], ['set', 'modules.spawn_gate', 'off'],
                 ['set', 'sync.mode', 'on'], ['check-repo', OTHER], ['set-repo', REPO], ['set', 'nonsense', 'x']):
        before = machine.settings_state()
        machine.cli(*args)
        assert machine.settings_state() == before, args
    assert not list(machine.tmp.glob('settings.json.bak-usage-clock-*'))[1:]      # only the one of the install


def test_the_installed_copy_runs_as_the_plugin_starts_it(machine):
    machine.install('--components', 'statusline,usage-clock')
    done = subprocess.run([sys.executable, str(machine.target / 'settings_cli.py'), '--settings', str(machine.settings),
                           'show', '--json'], capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert done.returncode == 0, done.stderr
    assert modules(json.loads(done.stdout))['usage_clock']['installed']


def test_bad_arguments_are_exit_2(machine, capsys):
    with pytest.raises(SystemExit) as stop:
        settings_cli.main(['show'])
    assert stop.value.code == 2


# --- check_access ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('returncode,errors,expected', [
    (0, '', 'reachable'),
    (128, 'remote: Repository not found.\nfatal: repository not found\n', 'not_found_or_no_access'),
    (128, "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n",
     'not_found_or_no_access'),
    (128, 'fatal: unable to access: Could not resolve host\n', 'unclear')])
def test_check_access_reads_the_answer_of_git_with_the_login(tmp_path, returncode, errors, expected):
    probe = answer(returncode, errors)
    assert _sync_git.check_access('owner/name', 'https', str(tmp_path), execute=probe) == expected
    argv, env, cwd = probe.calls[0]
    assert argv[-1] == _sync_git.remote_url('owner/name', 'https') and 'ls-remote' in argv
    assert env['GIT_TERMINAL_PROMPT'] == '0' and env['GIT_DIR'] == os.devnull


def test_check_access_without_git_is_unclear(tmp_path):
    def broken(argv, deadline, env, cwd):
        raise _sync_core.SyncError('internal', 'git could not be started')
    assert _sync_git.check_access('owner/name', 'https', str(tmp_path), execute=broken) == 'unclear'


def test_the_script_of_the_checkout_runs_as_a_program(machine):
    machine.install('--components', 'usage-clock')
    done = subprocess.run([sys.executable, os.path.join(ROOT, 'hooks', 'settings_cli.py'), '--settings',
                           str(machine.settings), '--target', str(machine.target), 'set', 'modules.usage_clock',
                           'false'], capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)['changed'] and machine.config_data()['modules']['usage_clock'] is False


# --- A name that looks like an option, and a clone of another repository ---------------------------------------------

@pytest.mark.parametrize('command', ['check-repo', 'set-repo'])
@pytest.mark.parametrize('name', ['--help', '-x/y'])
def test_a_name_after_double_dash_is_a_name_and_answers_json(machine, command, name):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    before = machine.tree()
    code, done = machine.cli(command, '--', name)
    assert code == 1 and done == dict(done, result='invalid', repo=name) and machine.tree() == before


def test_set_repo_with_a_clone_of_another_repository_changes_nothing_and_says_how_to_go_on(machine):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    before = machine.tree()
    code, done = machine.cli('set-repo', OTHER)
    clone = str(machine.tmp / 'log' / 'sync' / 'repo')
    assert code == 1 and done['result'] == 'other_clone', done
    assert clone in done['text'] and 'Move that folder away' in done['text'] and 'never pushed' in done['text']
    assert machine.tree() == before and machine.config_data()['sync']['repo'] == REPO


def test_set_repo_with_a_folder_that_is_no_clone_changes_nothing(machine):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    os.replace(machine.tmp / 'log' / 'sync' / 'repo', machine.tmp / 'old-clone')
    (machine.tmp / 'log' / 'sync' / 'repo').mkdir()
    before = machine.tree()
    code, done = machine.cli('set-repo', REPO)
    assert code == 1 and done['result'] == 'other_clone' and machine.tree() == before


def test_set_repo_whose_clone_git_cannot_check_leaves_the_reason_to_the_installer_code(machine, monkeypatch):
    machine.install('--components', 'sync', '--sync-repo', REPO)
    import _setup

    def slow(plan):
        raise _sync_core.SyncError('timeout', 'git did not finish in time')
    monkeypatch.setattr(_setup, 'clone_points_to', slow)
    code, done = machine.cli('set-repo', OTHER)
    assert code == 1 and done['result'] == 'failed' and 'git did not finish in time' in done['text']
    assert machine.config_data()['sync']['repo'] == REPO
