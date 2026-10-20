"""Tests for the component `sync` of tools/install.py (0034-SYNC-INSTALL): --sync-repo, --sync-mode, the clone,
--check and --uninstall.

The installer runs in this process (main(argv)), so that the remote can be a local bare repository and the visibility
probe a function handed in, as in tests/test_sync_git.py: no test needs the network. Git runs with an empty global
config of its own (git_alone). settings.json, the target and the log folder lie below tmp_path."""
import importlib.util
import json
import os
import subprocess
import time

import pytest

import _config
import _plat
import _sync_core
import _sync_git
from conftest import HOOKS, ROOT, lock_is_held
from test_sync_git import ASKS, REPO, UNKNOWN_HOST, answer, base_size, git, git_alone, remote  # noqa: F401

INSTALL = os.path.join(ROOT, 'tools', 'install.py')
PRIVATE = answer(128, ASKS)      # the probe of a push that only needs the answer, not its calls


def load():
    spec = importlib.util.spec_from_file_location('install_sync_under_test', INSTALL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Setup:
    """settings.json, target and log folder of one machine, and the installer as a module."""

    def __init__(self, tmp_path, capsys, monkeypatch):
        self.tmp, self.capsys = tmp_path, capsys
        self.settings, self.target, self.log = tmp_path / 'settings.json', tmp_path / 'target', tmp_path / 'log'
        self.settings.write_text('{}', encoding='utf-8')
        self.module = load()
        self.probe = answer(128, ASKS)
        real = _sync_git.check_visibility
        monkeypatch.setattr(_sync_git, 'check_visibility',
                            lambda repo, directory, deadline=None, execute=None: real(repo, directory, deadline,
                                                                                      self.probe))
        monkeypatch.delenv('CCUC_CONFIG')      # the config of the installation is <target>/config.json

    def run(self, *args):
        code = self.module.main(['--settings', str(self.settings), '--target', str(self.target), *map(str, args)])
        return code, self.capsys.readouterr().out

    def sync(self, *args):
        """An installation of the sync alone, with --sync-repo."""
        return self.run('--components', 'sync', '--sync-repo', REPO, *args)

    @property
    def config(self):
        return self.target / 'config.json'

    def config_data(self):
        return json.loads(self.config.read_text(encoding='utf-8'))

    def set_config(self, **sync_values):
        self.target.mkdir(exist_ok=True)
        self.config.write_text(json.dumps({'sync': sync_values}), encoding='utf-8')

    def data(self):
        return json.loads(self.settings.read_text(encoding='utf-8'))

    @property
    def directory(self):
        return self.log / 'sync'

    @property
    def clone(self):
        return self.directory / 'repo'

    def state(self):
        return _sync_core.read_state(str(self.directory))

    def write_state(self, **values):
        _sync_core.write_state(_sync_core.ensure_dir(str(self.log)), _sync_core.State(**values))

    def leftovers(self):
        """What a clone attempt may have left in the sync folder besides its own files."""
        if not self.directory.exists():
            return []
        return sorted(p.name for p in self.directory.iterdir() if p.name.startswith('repo'))

    def backups(self, name):
        return sorted(p.name for p in self.tmp.rglob(f'{name}.bak-usage-clock-*'))


@pytest.fixture
def setup(tmp_path, capsys, monkeypatch, remote):
    return Setup(tmp_path, capsys, monkeypatch)


def sync_commands(data):
    return {event: [h['command'] for g in data['hooks'][event] for h in g['hooks']]
            for event in ('SessionStart', 'SessionEnd') if event in data.get('hooks', {})}


def expected(target, argument):
    return _plat.script_command(os.path.join(str(target), 'sync.py'), argument) + ' || true'


def lines(out, prefix):
    return [line for line in out.splitlines() if line.startswith(prefix)]


# --- The criteria of the node -----------------------------------------------------------------------------------------

@pytest.mark.parametrize('returncode,errors', [(0, ''), (128, UNKNOWN_HOST), (1, ''),
                                               (128, 'fatal: Authentication failed\n')],
                         ids=['public', 'unknown-host', 'exit-1', 'other-128'])
def test_a_public_or_unclear_repo_stops_the_installation_with_exit_1(setup, returncode, errors):
    setup.probe = probe = answer(returncode, errors)
    code, out = setup.sync()
    assert code == 1, out
    assert ('is public' if returncode == 0 else 'could not be told') in out
    assert 'nothing was installed' in out
    assert setup.data() == {} and not setup.target.exists() and not setup.clone.exists() and setup.leftovers() == []
    assert len(probe.calls) == 1


def test_a_private_repo_is_cloned_and_the_two_hooks_are_installed(setup, remote):
    code, out = setup.sync()
    assert code == 0, out
    assert sync_commands(setup.data()) == {'SessionStart': [expected(setup.target, 'session-start')],
                                           'SessionEnd': [expected(setup.target, 'session-end')]}
    assert all('matcher' not in g and g['hooks'][0]['timeout'] == 5
               for event in ('SessionStart', 'SessionEnd') for g in setup.data()['hooks'][event])
    assert setup.config_data()['sync']['repo'] == REPO and setup.config_data()['sync']['mode'] == 'shadow'
    assert (setup.clone / 'README.md').read_text(encoding='utf-8') == 'sync\n'
    assert git('remote', 'get-url', 'origin', cwd=setup.clone).stdout.strip() == _sync_git.remote_url(REPO, 'https')
    assert _sync_git.cached_private(str(setup.directory), REPO, time.time())
    assert f'Sync: {REPO} is not public' in out and f'Sync: cloned {REPO} to {setup.clone}' in out
    assert 'Sync mode: shadow (default, nothing installed yet)' in out
    assert len(setup.probe.calls) == 1 and setup.leftovers() == ['repo']


def test_the_clone_of_the_installer_is_one_the_push_takes(setup, remote):
    """The push opens it as it is (branch, origin/HEAD, remote URL) and pushes into the remote."""
    assert setup.sync('--sync-mode', 'on')[0] == 0
    cfg = _config.load()
    with open(setup.config, encoding='utf-8') as f:
        cfg['sync'].update(json.load(f)['sync'])
    runner = lambda run: _sync_git.push(run, projects_dir=str(setup.tmp / 'projects'), execute=PRIVATE)  # noqa: E731
    assert _sync_core.push(str(setup.log), cfg, runner, forced=True) == 0
    assert setup.state().last_error is None
    assert any(path.startswith('machines/') for path in remote.files())


def test_two_runs_with_the_same_options_change_nothing_the_second_time(setup):
    assert setup.sync()[0] == 0
    before = (setup.settings.read_bytes(), setup.config.read_bytes(), git('rev-parse', 'HEAD', cwd=setup.clone).stdout)
    backups = setup.backups('settings.json') + setup.backups('config.json')
    code, out = setup.sync()
    assert code == 0, out
    assert (setup.settings.read_bytes(), setup.config.read_bytes(),
            git('rev-parse', 'HEAD', cwd=setup.clone).stdout) == before
    assert setup.backups('settings.json') + setup.backups('config.json') == backups
    assert git('status', '--porcelain', cwd=setup.clone).stdout == ''
    assert 'already up to date - not written' in out and 'config.json: sync keys already set - unchanged' in out
    assert f'Sync: the clone {setup.clone} is there and points to {REPO} - unchanged' in out
    assert 'Sync mode: shadow (kept from config.json)' in out


def test_the_dry_run_shows_every_change_of_the_sync_and_clones_nothing(setup):
    code, out = setup.sync('--dry-run')
    assert code == 0, out
    assert setup.data() == {} and not setup.target.exists() and not setup.log.exists()
    assert setup.probe.calls == []
    for needle in ('SessionStart: sync.py added', 'SessionEnd: sync.py added',
                   "config.json: sync.repo '' -> 'owner/name', sync.mode 'off' -> 'shadow' would be written",
                   f'Sync: would check that {REPO} is not public, then clone it to {setup.clone}', 'DRY RUN'):
        assert needle in out, needle


def test_the_dry_run_of_an_installed_sync_says_the_clone_stays(setup):
    assert setup.sync()[0] == 0
    code, out = setup.sync('--dry-run')
    assert code == 0 and f'Sync: would check that {REPO} is not public; the clone {setup.clone} stays' in out, out
    assert 'config.json: sync keys already set - unchanged' in out


def test_a_rerun_with_sync_repo_lifts_a_lasting_visibility_block(setup):
    assert setup.sync()[0] == 0
    setup.write_state(last_error='visibility', backoff_level=3)
    code, out = setup.sync()
    assert code == 0, out
    assert setup.state().last_error is None and setup.state().backoff_level == 0 and setup.state().due(time.time())
    assert 'Sync: the block after a public repo is lifted, the next push is due.' in out


def test_the_block_stays_while_the_repo_is_still_public(setup):
    assert setup.sync()[0] == 0
    setup.write_state(last_error='visibility', backoff_level=1)
    setup.probe = answer(0)
    code, out = setup.sync()
    assert code == 1 and 'is public' in out and setup.state().last_error == 'visibility'


def test_a_dry_run_names_the_block_it_would_lift(setup):
    assert setup.sync()[0] == 0
    setup.write_state(last_error='visibility', backoff_level=1)
    code, out = setup.sync('--dry-run')
    assert code == 0 and 'Sync: the block after a public repo would be lifted' in out, out
    assert setup.state().last_error == 'visibility'


def test_an_unreadable_state_blocks_nothing_and_is_left_to_the_next_run(setup):
    """The core counts a broken state.json as empty (with a log line of its own): there is no block to lift."""
    _sync_core.ensure_dir(str(setup.log))
    (setup.directory / _sync_core.STATE_FILE).write_text('{broken', encoding='utf-8')
    code, out = setup.sync()
    assert code == 0 and 'lifted' not in out, out
    assert (setup.directory / _sync_core.STATE_FILE).read_text(encoding='utf-8') == '{broken'


def test_a_held_lock_lifts_no_block_and_says_so(setup, monkeypatch):
    assert setup.sync()[0] == 0
    setup.write_state(last_error='visibility', backoff_level=1)
    monkeypatch.setattr(_sync_core, 'clear_visibility', lambda log_dir, now: False)
    code, out = setup.sync()
    assert code == 1 and 'a sync run holds the lock' in out, out


# --- Mode --------------------------------------------------------------------------------------------------------------

def test_sync_mode_is_written_and_kept_and_says_where_it_comes_from(setup):
    code, out = setup.sync('--sync-mode', 'on')
    assert code == 0 and 'Sync mode: on (--sync-mode)' in out and setup.config_data()['sync']['mode'] == 'on'
    code, out = setup.sync()
    assert code == 0 and 'Sync mode: on (kept from config.json)' in out and setup.config_data()['sync']['mode'] == 'on'
    code, out = setup.run('--components', 'sync', '--sync-mode', 'off')
    assert code == 0 and 'Sync mode: off (--sync-mode)' in out and setup.config_data()['sync']['mode'] == 'off'
    assert 'Sync mode: off (kept from config.json)' in setup.run('--components', 'sync')[1]


def test_a_first_installation_starts_in_shadow_whatever_the_config_says(setup):
    setup.set_config(mode='on', repo=REPO)
    code, out = setup.sync()
    assert code == 0 and 'Sync mode: shadow (default, nothing installed yet)' in out
    assert setup.config_data() == {'sync': {'mode': 'shadow', 'repo': REPO}}      # the other keys of the file stay
    assert len(setup.backups('config.json')) == 1


def test_a_kept_mode_that_is_no_mode_says_the_hooks_run_with_off(setup):
    assert setup.sync()[0] == 0
    data = setup.config_data()
    data['sync']['mode'] = 'loud'
    setup.config.write_text(json.dumps(data), encoding='utf-8')
    code, out = setup.run('--components', 'sync')
    assert code == 0 and "Sync mode: off (kept from config.json, whose sync.mode is not valid: the hooks run as off)" in out


def test_the_shadow_hint_names_sync_mode_on(setup):
    out = setup.sync()[1]
    assert 'Sync mode shadow: commits stay on this machine, nothing is pushed. Use --sync-mode on to push.' in out
    assert 'Sync mode shadow' not in setup.sync('--sync-mode', 'on')[1]


# --- Selection and arguments ---------------------------------------------------------------------------------------------

def test_sync_is_not_in_the_default_selection(setup):
    code, out = setup.run()
    assert code == 0, out
    assert sync_commands(setup.data()) == {} and 'Sync' not in out
    with open(os.path.join(ROOT, 'config.example.json'), 'rb') as f:
        assert setup.config.read_bytes() == f.read()
    assert not setup.clone.exists()


def test_the_default_run_leaves_an_installed_sync_alone(setup):
    assert setup.sync('--sync-mode', 'on')[0] == 0
    before = sync_commands(setup.data())
    code, out = setup.run()
    assert code == 0 and sync_commands(setup.data()) == before and setup.config_data()['sync']['mode'] == 'on'


@pytest.mark.parametrize('args', [['--sync-repo', REPO], ['--sync-mode', 'on'],
                                  ['--components', 'statusline', '--sync-repo', REPO]])
def test_the_sync_options_need_the_component(setup, args, capsys):
    with pytest.raises(SystemExit) as stop:
        setup.module.main(['--settings', str(setup.settings), '--target', str(setup.target), *args])
    assert stop.value.code == 2 and 'need the component sync' in capsys.readouterr().err
    assert setup.data() == {} and not setup.target.exists()


@pytest.mark.parametrize('bad', ['owner', 'owner/name/more', '../name', 'owner/..', 'owner/.', '-owner/name',
                                 'owner/name\n', 'https://github.com/owner/name'])
def test_a_sync_repo_that_is_no_owner_name_is_an_argument_error(setup, bad, capsys):
    with pytest.raises(SystemExit) as stop:
        setup.module.main(['--settings', str(setup.settings), '--target', str(setup.target), '--components', 'sync',
                           f'--sync-repo={bad}'])
    assert stop.value.code == 2 and '--sync-repo must be owner/name' in capsys.readouterr().err


def test_an_unknown_sync_mode_is_an_argument_error(setup, capsys):
    with pytest.raises(SystemExit) as stop:
        setup.run('--components', 'sync', '--sync-mode', 'loud')
    assert stop.value.code == 2


def test_the_component_without_any_repo_is_an_argument_error(setup):
    code, out = setup.run('--components', 'sync')
    assert code == 2 and 'needs --sync-repo owner/name' in out, out
    assert setup.data() == {} and not setup.target.exists()


def test_the_component_without_sync_repo_takes_the_configured_repo_and_names_a_missing_clone(setup):
    setup.set_config(repo=REPO)
    code, out = setup.run('--components', 'sync')
    assert code == 0, out
    assert f'Sync: the clone {setup.clone} is missing; run again with --sync-repo {REPO} to make it.' in out
    assert setup.probe.calls == [] and sync_commands(setup.data())


@pytest.mark.parametrize('content', ['{broken', '[]', '{"sync": []}'])
def test_a_config_json_that_cannot_take_the_sync_keys_stops_before_anything_is_written(setup, content):
    setup.target.mkdir()
    setup.config.write_text(content, encoding='utf-8')
    code, out = setup.sync()
    assert code == 2 and 'config.json' in out and 'nothing changed' in out, out
    assert setup.data() == {} and setup.config.read_text(encoding='utf-8') == content and not setup.clone.exists()


@pytest.mark.posix_only
def test_a_config_json_that_is_a_link_is_refused(setup, tmp_path):
    real = tmp_path / 'real-config.json'
    real.write_text('{}', encoding='utf-8')
    setup.target.mkdir()
    os.symlink(real, setup.config)
    code, out = setup.sync()
    assert code == 2 and 'must be a regular file, not a symlink' in out
    assert os.path.islink(setup.config) and real.read_text(encoding='utf-8') == '{}'


def test_the_config_file_of_ccuc_config_is_the_one_written(setup, tmp_path, monkeypatch):
    other = tmp_path / 'elsewhere.json'
    monkeypatch.setenv('CCUC_CONFIG', str(other))
    assert setup.sync()[0] == 0
    assert json.loads(other.read_text(encoding='utf-8'))['sync']['repo'] == REPO
    assert setup.config_data()['sync']['repo'] == ''      # created from the example, as always


# --- The clone ---------------------------------------------------------------------------------------------------------

def test_no_git_config_of_the_user_changes_the_line_ends_of_the_clone(setup, remote, git_alone):
    """Git for Windows sets core.autocrlf=true in its system config; a plain `git clone` checks LF files out with CRLF
    (measured). The clone of the installer runs with the settings of every other git call of the sync."""
    with open(git_alone, 'a', encoding='utf-8') as f:
        f.write('[core]\n\tautocrlf = true\n')
    remote.foreign_commit('machines/other/days/2026-10.jsonl', b'{"d":1}\n{"d":2}\n')
    assert setup.sync()[0] == 0
    assert (setup.clone / 'machines' / 'other' / 'days' / '2026-10.jsonl').read_bytes() == b'{"d":1}\n{"d":2}\n'
    readme = subprocess.run(['git', 'cat-file', 'blob', 'HEAD:README.md'], cwd=setup.clone, capture_output=True,
                            check=True).stdout
    assert (setup.clone / 'README.md').read_bytes() == readme       # as stored, whatever this system wrote


def big(remote, size):
    remote.foreign_commit('big.bin', b'x' * size)


def test_a_remote_over_max_clone_mb_is_not_checked_out_and_stops_with_exit_1(setup, remote):
    big(remote, 1024 * 1024 + 1)
    setup.set_config(max_clone_mb=1)
    code, out = setup.sync()
    assert code == 1, out
    assert ('Sync: the sync repository is larger than sync.max_clone_mb (1 MB): nothing was checked out; raise the '
            'limit if this size is expected') in out
    assert not setup.clone.exists() and setup.leftovers() == [] and sync_commands(setup.data()) == {}


def test_a_remote_at_max_clone_mb_is_taken(setup, remote):
    readme = base_size(remote)      # the README of the first commit, as this system wrote it
    big(remote, 1024 * 1024 - readme)
    setup.set_config(max_clone_mb=1)
    code, out = setup.sync()
    assert code == 0, out
    assert (setup.clone / 'big.bin').stat().st_size == 1024 * 1024 - readme


def test_the_size_is_checked_before_anything_is_checked_out(setup, remote, monkeypatch):
    real, seen = _sync_git._check_size, []

    def check_size(clone):
        seen.append(sorted(os.listdir(clone.path)))
        real(clone)
    monkeypatch.setattr(_sync_git, '_check_size', check_size)
    big(remote, 10)
    assert setup.sync()[0] == 0
    assert seen == [['.git']] and (setup.clone / 'big.bin').is_file()


def test_a_remote_with_too_many_files_is_not_checked_out(setup, remote, monkeypatch):
    monkeypatch.setattr(_sync_git, 'MAX_CLONE_FILES', 1)
    remote.foreign_commit('second.txt', 'two\n')
    code, out = setup.sync()
    assert code == 1 and 'Sync: the sync repository has more than 1 files: nothing was checked out' in out, out
    assert not setup.clone.exists() and setup.leftovers() == []


def test_the_clone_holds_the_sync_lock_and_asks_for_no_credentials(setup, monkeypatch):
    real, seen = _sync_core.run_command, []

    def run_command(argv, deadline, env=None, cwd=None):
        if 'clone' in argv:
            seen.append((lock_is_held(str(setup.directory / _sync_core.LOCK_FILE)), env))
        return real(argv, deadline, env, cwd)
    monkeypatch.setattr(_sync_core, 'run_command', run_command)
    assert setup.sync()[0] == 0
    (held, env), = seen
    assert held
    assert env['GIT_TERMINAL_PROMPT'] == '0' and env['GIT_ASKPASS'] == '' and env['GCM_INTERACTIVE'] == 'never'
    assert 'BatchMode=yes' in env['GIT_SSH_COMMAND'] and 'GIT_DIR' not in env and 'GIT_WORK_TREE' not in env
    assert env['LC_ALL'] == 'C'


def test_a_lock_held_by_a_sync_run_stops_the_installation(setup):
    directory = _sync_core.ensure_dir(str(setup.log))
    with _sync_core.try_lock(directory) as held:
        assert held
        code, out = setup.sync()
    assert code == 1 and 'a sync run holds the lock' in out and not setup.clone.exists(), out
    assert setup.data() == {}


def test_a_clone_that_fails_leaves_nothing_behind(setup, remote, tmp_path):
    os.rename(remote.bare, tmp_path / 'gone.git')
    code, out = setup.sync()
    assert code == 1 and 'Sync: the clone did not reach the sync repo' in out, out
    assert not setup.clone.exists() and setup.leftovers() == [] and setup.data() == {}


def test_a_failing_checkout_leaves_nothing_behind(setup, monkeypatch):
    real = _sync_core.run_command

    def run_command(argv, deadline, env=None, cwd=None):
        if 'checkout' in argv:
            return subprocess.CompletedProcess(argv, 1, '', 'error')
        return real(argv, deadline, env, cwd)
    monkeypatch.setattr(_sync_core, 'run_command', run_command)
    code, out = setup.sync()
    assert code == 1 and 'Sync: the sync repository could not be checked out' in out, out
    assert not setup.clone.exists() and setup.leftovers() == []


def test_refused_credentials_are_named(setup, monkeypatch):
    real = _sync_core.run_command

    def run_command(argv, deadline, env=None, cwd=None):
        if 'clone' in argv:
            return subprocess.CompletedProcess(argv, 128, '', "fatal: could not read Username for 'https://github.com'")
        return real(argv, deadline, env, cwd)
    monkeypatch.setattr(_sync_core, 'run_command', run_command)
    code, out = setup.sync()
    assert code == 1 and 'credentials missing or not accepted' in out and 'never asks for credentials' in out, out


def test_a_clone_without_a_branch_is_refused(setup, monkeypatch):
    real = _sync_core.run_command

    def run_command(argv, deadline, env=None, cwd=None):
        if 'symbolic-ref' in argv:
            return subprocess.CompletedProcess(argv, 1, '', '')
        return real(argv, deadline, env, cwd)
    monkeypatch.setattr(_sync_core, 'run_command', run_command)
    code, out = setup.sync()
    assert code == 1 and 'Sync: the clone has no branch checked out' in out, out
    assert not setup.clone.exists() and setup.leftovers() == []


def test_an_empty_remote_is_cloned_and_the_first_push_fills_it(tmp_path, capsys, monkeypatch):
    bare = tmp_path / 'remotes' / 'owner' / 'name.git'
    bare.parent.mkdir(parents=True)
    git('init', '--bare', '--quiet', '--initial-branch=main', str(bare))
    setup = Setup(tmp_path, capsys, monkeypatch)
    code, out = setup.sync('--sync-mode', 'on')
    assert code == 0, out
    assert git('symbolic-ref', '--short', 'HEAD', cwd=setup.clone).stdout.strip() == 'main'
    cfg = _config.load()
    cfg['sync'].update(setup.config_data()['sync'])
    runner = lambda run: _sync_git.push(run, projects_dir=str(tmp_path / 'projects'), execute=PRIVATE)  # noqa: E731
    assert _sync_core.push(str(setup.log), cfg, runner, forced=True) == 0
    assert git('ls-tree', '-r', '--name-only', 'refs/heads/main', cwd=bare).stdout.startswith('machines/')


def test_an_existing_folder_of_another_repository_stays_untouched(setup, tmp_path):
    other = tmp_path / 'remotes' / 'someone' / 'else.git'
    other.parent.mkdir(parents=True)
    git('init', '--bare', '--quiet', '--initial-branch=main', str(other))
    _sync_core.ensure_dir(str(setup.log))
    git('clone', '--quiet', str(other), str(setup.clone))
    (setup.clone / 'mine.txt').write_text('keep\n', encoding='utf-8')
    code, out = setup.sync()
    assert code == 1, out
    assert f'Sync: {setup.clone} exists but is no clone of {_sync_git.remote_url(REPO, "https")}' in out
    assert 'Move it away' in out and (setup.clone / 'mine.txt').read_text(encoding='utf-8') == 'keep\n'
    assert setup.data() == {}


def test_an_existing_file_in_place_of_the_clone_stays_untouched(setup):
    _sync_core.ensure_dir(str(setup.log))
    setup.clone.write_text('a file\n', encoding='utf-8')
    code, out = setup.sync()
    assert code == 1 and 'exists but is no clone' in out and setup.clone.read_text(encoding='utf-8') == 'a file\n'


@pytest.mark.posix_only
def test_the_clone_folder_is_private(setup):
    assert setup.sync()[0] == 0
    assert (os.stat(setup.clone).st_mode & 0o777) == 0o700 and (os.stat(setup.directory).st_mode & 0o777) == 0o700


def test_a_sync_hook_that_breaks_fails_the_smoke_test(setup, tmp_path):
    copy = tmp_path / 'source-hooks'
    import shutil
    shutil.copytree(HOOKS, copy, ignore=shutil.ignore_patterns('__pycache__'))
    (copy / '_sync_git.py').write_text('raise ImportError("broken")\n', encoding='utf-8')
    setup.module.SOURCE_HOOKS = str(copy)
    code, out = setup.sync()
    assert code == 1 and 'SMOKE TEST FAILED' in out and 'sync.py: exit 1' in out, out
    assert setup.data() == {} and not setup.clone.exists()


def test_a_sync_hook_that_prints_fails_the_smoke_test(setup, tmp_path):
    copy = tmp_path / 'source-hooks'
    import shutil
    shutil.copytree(HOOKS, copy, ignore=shutil.ignore_patterns('__pycache__'))
    script = (copy / 'sync.py').read_text(encoding='utf-8')
    (copy / 'sync.py').write_text(script.replace("if __name__ == '__main__':", "print('{}')\nif __name__ == '__main__':"),
                                  encoding='utf-8')
    setup.module.SOURCE_HOOKS = str(copy)
    code, out = setup.sync()
    assert code == 1 and 'a session hook must print nothing' in out, out


# --- Uninstall -----------------------------------------------------------------------------------------------------------

def test_uninstall_removes_the_sync_entries_and_leaves_clone_and_remote_alone(setup, remote):
    assert setup.sync()[0] == 0
    head, files = remote.head(), sorted(p.name for p in setup.clone.iterdir())
    config = setup.config.read_bytes()
    code, out = setup.run('--uninstall')
    assert code == 0, out
    assert 'SessionStart: sync.py removed' in out and 'SessionEnd: sync.py removed' in out
    assert sync_commands(setup.data()) == {} and 'hooks' not in setup.data()
    assert sorted(p.name for p in setup.clone.iterdir()) == files and remote.head() == head
    assert setup.config.read_bytes() == config


def test_uninstall_of_other_components_keeps_the_sync(setup):
    assert setup.sync()[0] == 0
    before = sync_commands(setup.data())
    assert setup.run('--uninstall', '--components', 'statusline,usage-clock')[0] == 0
    assert sync_commands(setup.data()) == before


# --- --check -------------------------------------------------------------------------------------------------------------

def check(setup, *args):
    code = setup.module.main(['--check', '--settings', str(setup.settings), '--target', str(setup.target),
                              *map(str, args)])
    return code, setup.capsys.readouterr().out


def test_check_shows_every_sync_line_of_a_fresh_installation_and_changes_nothing(setup):
    assert setup.sync()[0] == 0
    snapshot = sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in setup.log.rglob('*') if p.is_file())
    code, out = check(setup, '--components', 'sync')
    assert code == 0, out
    for needle in ('OK   SessionStart: sync.py', 'OK   SessionEnd: sync.py',
                   f'OK   Sync repo: {REPO} (https, mode shadow)',
                   f'OK   Sync clone: {setup.clone} is there and clean',
                   f'OK   Sync remote: {_sync_git.remote_url(REPO, "https")}',
                   f'OK   Sync visibility: {REPO} is not public (checked 0 h ago)',
                   'NOTE Sync last success: none yet; the first run starts when a session ends',
                   'OK   Sync errors: none, no backoff',
                   'OK   Sync clone size: 0 MB'):
        assert needle in out, needle
    assert sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in setup.log.rglob('*') if p.is_file()) \
        == snapshot


def test_check_without_components_takes_the_sync_if_it_is_installed(setup):
    assert 'Sync' not in check(setup)[1]
    assert setup.sync()[0] == 0
    assert 'Sync repo' in check(setup)[1]


def test_check_ends_with_exit_1_if_a_sync_line_is_fix(setup):
    assert setup.sync()[0] == 0
    os.rename(setup.clone, setup.directory / 'moved')
    code, out = check(setup, '--components', 'sync')
    assert code == 1
    assert f'FIX  Sync clone: {setup.clone} is missing. Run install.py --components sync --sync-repo {REPO}.' in out
    assert 'Sync remote' not in out and 'Sync clone size' not in out


def test_check_names_a_repo_that_is_not_set(setup):
    assert setup.sync()[0] == 0
    data = setup.config_data()
    data['sync']['repo'] = ''
    setup.config.write_text(json.dumps(data), encoding='utf-8')
    code, out = check(setup, '--components', 'sync')
    assert code == 1 and 'FIX  Sync repo: sync.repo is not set. Run install.py --components sync --sync-repo owner/name.' \
        in out, out
    assert 'Sync clone' not in out


def test_check_names_a_dirty_clone_a_wrong_remote_and_a_clone_that_is_no_git_folder(setup):
    assert setup.sync()[0] == 0
    (setup.clone / 'loose.txt').write_text('x\n', encoding='utf-8')
    code, out = check(setup, '--components', 'sync')
    assert code == 0 and f'NOTE Sync clone: {setup.clone} has changes that are not committed' in out, out
    git('remote', 'set-url', 'origin', 'https://example.invalid/x.git', cwd=setup.clone)
    code, out = check(setup, '--components', 'sync')
    assert code == 1 and 'FIX  Sync remote: the remote of the clone is not ' in out, out
    git('remote', 'set-url', 'origin', _sync_git.remote_url(REPO, 'https'), cwd=setup.clone)
    git('remote', 'set-url', '--push', 'origin', 'https://example.invalid/x.git', cwd=setup.clone)
    assert 'FIX  Sync remote' in check(setup, '--components', 'sync')[1]
    os.rename(setup.clone / '.git', setup.clone / 'git-moved')
    code, out = check(setup, '--components', 'sync')
    assert code == 1 and f'FIX  Sync clone: {setup.clone} is no git folder' in out, out


def test_check_names_a_git_that_cannot_run_in_the_clone(setup, monkeypatch):
    assert setup.sync()[0] == 0

    def run_command(argv, deadline, env=None, cwd=None):
        raise _sync_core.SyncError('internal', 'git could not be started (FileNotFoundError)')
    monkeypatch.setattr(_sync_core, 'run_command', run_command)
    code, out = check(setup, '--components', 'sync')
    assert code == 1 and 'FIX  Sync clone: git status failed in the clone' in out, out


def test_check_names_a_git_status_that_fails(setup, monkeypatch):
    assert setup.sync()[0] == 0
    real = _sync_core.run_command

    def run_command(argv, deadline, env=None, cwd=None):
        if 'status' in argv:
            return subprocess.CompletedProcess(argv, 128, '', 'fatal: index file corrupt')
        return real(argv, deadline, env, cwd)
    monkeypatch.setattr(_sync_core, 'run_command', run_command)
    code, out = check(setup, '--components', 'sync')
    assert code == 1 and f'FIX  Sync clone: git status failed in the clone {setup.clone}.' in out, out


def test_check_reads_the_visibility_from_the_cache_with_its_age(setup):
    assert setup.sync()[0] == 0
    cache = setup.directory / _sync_git.VISIBILITY_FILE
    old = json.loads(cache.read_text(encoding='utf-8'))
    old['checked'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 30 * 3600))
    cache.write_text(json.dumps(old), encoding='utf-8')
    code, out = check(setup, '--components', 'sync')
    assert code == 0 and f'NOTE Sync visibility: {REPO} was found not public 30 h ago; the next push checks again' in out
    cache.write_text('{broken', encoding='utf-8')
    assert 'NOTE Sync visibility: not checked yet; the next push checks it' in check(setup, '--components', 'sync')[1]


@pytest.mark.parametrize('category,status', [('network', 'NOTE'), ('timeout', 'NOTE'), ('auth', 'FIX'),
                                             ('internal', 'FIX'), ('conflict', 'FIX'), ('rejected', 'FIX')])
def test_check_shows_backoff_and_error_category(setup, category, status):
    assert setup.sync()[0] == 0
    now = time.time()
    setup.write_state(last_success=now - 7200, last_error=category, backoff_level=2, next_due=now + 3600)
    code, out = check(setup, '--components', 'sync')
    assert code == (1 if status == 'FIX' else 0), out
    assert f'{status:<4} Sync errors: the last run failed with {category}, backoff level 2, next try at ' in out
    assert 'OK   Sync last success: ' in out and '(2 h ago)' in out


def test_check_names_a_blocked_sync_as_fix(setup):
    assert setup.sync()[0] == 0
    setup.write_state(last_error='visibility', backoff_level=1)
    code, out = check(setup, '--components', 'sync')
    assert code == 1 and 'FIX  Sync visibility: the repo was found public, nothing is pushed' in out, out
    assert '--sync-repo' in out


def test_check_notes_an_unreadable_state(setup):
    assert setup.sync()[0] == 0
    (setup.directory / _sync_core.STATE_FILE).write_text('{broken', encoding='utf-8')
    code, out = check(setup, '--components', 'sync')
    assert code == 0 and 'NOTE Sync state: state.json cannot be read, the next run counts it as empty' in out, out


def test_check_notes_a_large_clone(setup, monkeypatch):
    assert setup.sync()[0] == 0
    (setup.clone / 'large.bin').write_bytes(b'x' * (2 * 1024 * 1024))
    monkeypatch.setattr(setup.module, 'CLONE_NOTE_MB', 1)
    code, out = check(setup, '--components', 'sync')
    assert code == 0 and 'NOTE Sync clone size: 2 MB, at least 1 MB' in out, out


def test_the_note_threshold_of_the_clone_size_is_about_500_mb():
    assert load().CLONE_NOTE_MB == 500


def test_check_takes_no_sync_option(setup, capsys):
    for extra in (['--sync-repo', REPO], ['--sync-mode', 'on']):
        with pytest.raises(SystemExit) as stop:
            setup.module.main(['--check', '--components', 'sync', *extra])
        assert stop.value.code == 2 and '--check' in capsys.readouterr().err
