"""Tests for hooks/_sync_git.py: the push of the sync against local bare repositories.

No test needs the network: the remote is a bare repository below tmp_path (URLS point there, `remote_url` keeps the
real ones in its own test), the visibility probe is a function handed in (`execute`). Git runs with an empty global
config and without the system config, so nothing of the real configuration takes part, and with the C locale as the
push sets it."""
import copy
import functools
import json
import os
import subprocess

import pytest

import _config
import _context_state
import _sync_core
import _sync_export
import _sync_git
import sync
import sync_format
from test_usage_report import T, req

REPO = 'owner/name'
NOW = T('2026-10-14T12:00:00Z')
REAL_URLS = dict(_sync_git.URLS)
ASKS = "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"
ASKS_GERMAN = ("fatal: Konnte Benutzername für 'https://github.com' nicht lesen: Terminal-Abfragen "
               "deaktiviert\n")
UNKNOWN_HOST = "fatal: unable to access 'https://github.com/owner/name.git/': Could not resolve host: github.com\n"
TESTER = {'GIT_AUTHOR_NAME': 'Tester', 'GIT_AUTHOR_EMAIL': 'tester@example.invalid',
          'GIT_COMMITTER_NAME': 'Tester', 'GIT_COMMITTER_EMAIL': 'tester@example.invalid'}


def git(*args, cwd=None, check=True):
    done = subprocess.run(['git', *args], cwd=None if cwd is None else str(cwd), capture_output=True, text=True,
                          env={**os.environ, **TESTER, 'LC_ALL': 'C'})
    if check:
        assert done.returncode == 0, done.stderr
    return done


@pytest.fixture(autouse=True)
def git_alone(tmp_path, monkeypatch):
    """An empty global git config of its own, no system config, no GIT_* of the caller; the URLs of the sync point to
    the bare repositories below tmp_path/remotes."""
    for name in list(os.environ):
        if name.startswith('GIT_'):
            monkeypatch.delenv(name)
    config = tmp_path / 'gitconfig'
    config.write_text('[init]\n\tdefaultBranch = main\n', encoding='utf-8')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setitem(_sync_git.URLS, 'https', (tmp_path / 'remotes').as_posix() + '/{}.git')
    monkeypatch.setitem(_sync_git.URLS, 'ssh', (tmp_path / 'remotes-ssh').as_posix() + '/{}.git')
    return config


def answer(returncode, errors=''):
    """A visibility probe that answers like git would, and records its calls."""
    def execute(argv, deadline, env, cwd):
        execute.calls.append((argv, env, cwd))
        return subprocess.CompletedProcess(argv, returncode, '', errors)
    execute.calls = []
    return execute


class Remote:
    """The bare repository of `owner/name`, with one first commit, and a clone of someone else beside it."""

    def __init__(self, root):
        self.bare = root / 'remotes' / 'owner' / 'name.git'
        self.bare.parent.mkdir(parents=True)
        git('init', '--bare', '--quiet', '--initial-branch=main', str(self.bare))
        self.other = root / 'other'
        git('clone', '--quiet', str(self.bare), str(self.other))
        (self.other / 'README.md').write_text('sync\n', encoding='utf-8')
        git('add', 'README.md', cwd=self.other)
        git('commit', '--quiet', '-m', 'start', cwd=self.other)
        git('push', '--quiet', 'origin', 'HEAD:refs/heads/main', cwd=self.other)

    def head(self):
        return git('rev-parse', 'refs/heads/main', cwd=self.bare).stdout.strip()

    def files(self, ref='refs/heads/main'):
        return git('ls-tree', '-r', '--name-only', ref, cwd=self.bare).stdout.split()

    def show(self, path):
        return git('show', f'refs/heads/main:{path}', cwd=self.bare).stdout

    def foreign_commit(self, path, text):
        """Someone else commits `path` and pushes it."""
        git('pull', '--quiet', '--ff-only', cwd=self.other)
        target = self.other / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        git('add', path, cwd=self.other)
        git('commit', '--quiet', '-m', 'foreign', cwd=self.other)
        git('push', '--quiet', 'origin', 'HEAD:refs/heads/main', cwd=self.other)


@pytest.fixture
def remote(tmp_path):
    return Remote(tmp_path)


def config(**sync_values):
    cfg = copy.deepcopy(_config.DEFAULTS)
    cfg['sync'].update({'mode': 'on', 'repo': REPO, 'transport': 'https', **sync_values})
    return cfg


class Device:
    """One machine: its log folder, its transcripts, its config and its clone."""

    def __init__(self, root, mode='on', clone=True):
        self.log, self.projects = root / 'log', root / 'projects'
        self.cfg = config(mode=mode)
        self.probe = answer(128, ASKS)
        self.count = 0
        if clone:
            git('clone', '--quiet', _sync_git.remote_url(REPO, 'https'), self.clone)

    @property
    def clone(self):
        return _sync_git.clone_dir(str(self.log))

    @property
    def id(self):
        return sync_format.load_machine_id(str(self.log))

    @property
    def own(self):
        return sync_format.machine_dir(self.id)

    def request(self, ts='2026-10-13T08:00:00Z', session='aaaaaaaa-1', out=10):
        """One more request in the transcript of `session`."""
        self.count += 1
        path = self.projects / '-home-u-proj-a' / f'{session}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        line = req(ts, f'm{self.count}', request=f'r{self.count}', out=out)
        with open(path, 'a', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(line) + '\n')

    def push(self, now=NOW, forced=True, execute=None):
        runner = functools.partial(_sync_git.push, projects_dir=str(self.projects), execute=execute or self.probe)
        return _sync_core.push(str(self.log), self.cfg, runner, forced=forced, clock=lambda: now)

    def state(self):
        return _sync_core.read_state(str(self.log / 'sync'))

    def lines(self):
        path = self.log / 'log.jsonl'
        if not path.exists():
            return []
        return [r for r in map(json.loads, path.read_text(encoding='utf-8').splitlines()) if r.get('hook') == 'sync']

    def failure(self):
        return next(r for r in reversed(self.lines()) if r['decision'] == 'failure')

    def head(self):
        return git('rev-parse', 'HEAD', cwd=self.clone).stdout.strip()

    def status(self):
        return git('status', '--porcelain', '--untracked-files=all', cwd=self.clone).stdout

    def worktree_requests(self):
        """The request lines in the work tree of the clone."""
        folder = os.path.join(self.clone, *self.own.split('/'), 'requests')
        found = []
        for base, _dirs, names in os.walk(folder):
            for name in names:
                with open(os.path.join(base, name), encoding='utf-8') as f:
                    found += [json.loads(line) for line in f if line.strip()]
        return found


@pytest.fixture
def device(tmp_path, remote):
    return Device(tmp_path / 'a')


def keys(records):
    return sorted(r['k'] for r in records)


# --- The criteria of the node -----------------------------------------------------------------------------------------

def test_a_public_repo_is_never_written_also_not_when_triggered_again(device, remote):
    device.request()
    before = remote.head()
    public = answer(0)
    assert device.push(execute=public) == 1
    assert (device.state().last_error, device.failure()['category']) == ('visibility', 'visibility')
    assert device.push(execute=public) == 1                       # --now: the core blocks it
    assert device.push(execute=answer(128, ASKS)) == 1            # even if the probe would now say private
    assert len(public.calls) == 1 and remote.head() == before and device.status() == ''
    assert not os.path.exists(device.log / 'sync' / _sync_git.VISIBILITY_FILE)


@pytest.mark.parametrize('probe', [answer(128, UNKNOWN_HOST), answer(1), answer(128, 'fatal: Authentication failed\n'),
                                   answer(128, ASKS_GERMAN), answer(2, ASKS)],
                         ids=['unknown-host', 'exit-1', 'other-128', 'german-text', 'other-exit'])
def test_an_unclear_visibility_never_leads_to_a_push(device, remote, probe):
    device.request()
    before = remote.head()
    assert device.push(execute=probe) == 1
    assert device.failure()['category'] == 'network' and device.state().last_error == 'network'
    assert remote.head() == before and device.status() == ''
    assert not os.path.exists(device.log / 'sync' / _sync_git.VISIBILITY_FILE)


def test_a_probe_that_times_out_is_unclear_and_never_leads_to_a_push(device, remote):
    def hangs(argv, deadline, env, cwd):
        raise _sync_core.SyncError('timeout', 'git did not finish in time')
    device.request()
    before = remote.head()
    assert device.push(execute=hangs) == 1
    assert device.failure()['category'] == 'network' and remote.head() == before


@pytest.mark.parametrize('change', ['url', 'pushurl', 'insteadof', 'pushinsteadof', 'second-url', 'transport'])
def test_a_different_remote_url_ends_internal_without_a_push(tmp_path, device, remote, git_alone, change):
    elsewhere = (tmp_path / 'remotes' / 'owner' / 'other.git').as_posix()
    expected = _sync_git.remote_url(REPO, 'https')
    if change == 'url':
        git('remote', 'set-url', 'origin', elsewhere, cwd=device.clone)
    elif change == 'pushurl':
        git('config', 'remote.origin.pushurl', elsewhere, cwd=device.clone)
    elif change == 'second-url':
        git('remote', 'set-url', '--add', 'origin', elsewhere, cwd=device.clone)
    elif change == 'transport':
        device.cfg['sync']['transport'] = 'ssh'
    else:
        key = 'insteadOf' if change == 'insteadof' else 'pushInsteadOf'
        with open(git_alone, 'a', encoding='utf-8') as f:
            f.write(f'[url "{elsewhere}"]\n\t{key} = {expected}\n')
    device.request()
    before = remote.head()
    assert device.push() == 1
    failure = device.failure()
    assert (failure['category'], failure['reason']) == ('internal', 'the remote of the sync clone is not sync.repo')
    assert device.probe.calls == [] and remote.head() == before and device.status() == ''


@pytest.mark.parametrize('change', ['tracked', 'untracked', 'other-machine', 'staged'])
def test_a_changed_file_outside_the_own_folder_prevents_the_commit(device, remote, change):
    device.request()
    assert device.push() == 0
    head, before = device.head(), remote.head()
    device.request()
    clone = device.clone
    if change == 'tracked':
        with open(os.path.join(clone, 'README.md'), 'a', encoding='utf-8') as f:
            f.write('more\n')
    elif change == 'untracked':
        with open(os.path.join(clone, 'notes.txt'), 'w', encoding='utf-8') as f:
            f.write('x\n')
    elif change == 'other-machine':
        folder = os.path.join(clone, 'machines', '11111111-2222-4333-8444-555555555555')
        os.makedirs(folder)
        with open(os.path.join(folder, 'machine.json'), 'w', encoding='utf-8') as f:
            f.write('{}\n')
    else:
        with open(os.path.join(clone, 'staged.txt'), 'w', encoding='utf-8') as f:
            f.write('x\n')
        git('add', 'staged.txt', cwd=clone)
    assert device.push() == 1
    assert device.failure()['category'] == 'internal'
    assert device.head() == head and remote.head() == before
    assert len(device.worktree_requests()) == 2                       # the export is kept


def test_own_files_a_gitignore_of_the_repository_hides_are_committed_all_the_same(device, remote):
    remote.foreign_commit('.gitignore', 'machines/\n')
    device.request()
    assert device.push() == 0
    assert f'{device.own}/status.json' in remote.files() and len(device.worktree_requests()) == 1
    assert device.status() == ''


def test_two_machines_pushing_at_the_same_time_both_succeed_after_at_most_one_retry(tmp_path, remote, monkeypatch):
    first, second = Device(tmp_path / 'a'), Device(tmp_path / 'b')
    first.request()
    second.request()
    calls, other, real_export, real_run = [], [], _sync_export.export, _sync_core.run_command

    def export_after_the_other_pushed(clone_root, *args, **kwargs):
        if clone_root == second.clone and not other:
            other.append(first.push())        # the second fetched before, its push will be refused
        return real_export(clone_root, *args, **kwargs)

    def run_command(argv, deadline, env=None, cwd=None):
        calls.append((argv, cwd))
        return real_run(argv, deadline, env, cwd)
    monkeypatch.setattr(_sync_export, 'export', export_after_the_other_pushed)
    monkeypatch.setattr(_sync_core, 'run_command', run_command)
    assert second.push() == 0 and other == [0]
    pushes = [argv for argv, cwd in calls if cwd == second.clone and 'push' in argv]
    assert len(pushes) == 2                                          # refused once, then through
    assert first.state().last_error is None and second.state().last_error is None
    files = remote.files()
    assert f'{first.own}/status.json' in files and f'{second.own}/status.json' in files
    log = git('log', '--merges', '--oneline', 'refs/heads/main', cwd=remote.bare).stdout
    assert log == ''                                                 # never merged


def test_a_rebase_conflict_ends_in_conflict_with_a_clean_clone_and_the_own_commits_kept(device, remote):
    device.request()
    assert device.push() == 0
    device.cfg['sync']['mode'] = 'shadow'
    device.request()
    assert device.push(now=NOW + 60) == 0                             # a commit that is not pushed
    remote.foreign_commit(f'{device.own}/status.json', '{"v":1}\n')  # someone writes into the own folder
    foreign = remote.head()
    device.cfg['sync']['mode'] = 'on'
    device.request()
    assert device.push(now=NOW + 120) == 1
    assert device.failure()['category'] == 'conflict'
    assert device.status() == '' and not os.path.exists(os.path.join(device.clone, '.git', 'rebase-merge'))
    own_commits = git('log', '--format=%an', f'{foreign}..HEAD', cwd=device.clone).stdout.split()
    assert own_commits == ['usage-clock', 'usage-clock']              # the shadow commit and the new one
    assert len(device.worktree_requests()) == 3 and remote.head() == foreign


def test_shadow_commits_locally_reaches_no_remote_and_logs_lines_and_bytes_once(tmp_path, remote):
    device = Device(tmp_path / 'a', mode='shadow')
    device.request()
    device.request()
    before, head = remote.head(), device.head()
    assert device.push() == 0
    assert remote.head() == before and device.head() != head and device.status() == ''
    counted = [line for line in device.lines() if 'lines' in line or 'bytes' in line]
    assert len(counted) == 1
    shadow, = counted
    assert shadow['decision'] == 'shadow' and shadow['lines'] == 2 and shadow['bytes'] > 0
    # Switched to on, the gathered commits go out with the next push
    device.cfg['sync']['mode'] = 'on'
    assert device.push(now=NOW + 60) == 0
    assert remote.head() == device.head() and f'{device.own}/status.json' in remote.files()


def test_a_shadow_run_without_news_says_so_with_zero_lines(tmp_path, remote):
    device = Device(tmp_path / 'a', mode='shadow')
    device.request()
    assert device.push() == 0
    assert device.push(now=NOW + 60) == 0
    last = [line for line in device.lines() if line['decision'] == 'shadow'][-1]
    assert (last['reason'], last['lines'], last['bytes']) == ('nothing new, not pushed', 0, 0)


def test_no_git_call_can_ask_for_credentials(device, remote, monkeypatch):
    recorded, real_run = [], _sync_core.run_command

    def run_command(argv, deadline, env=None, cwd=None):
        recorded.append((argv, env))
        return real_run(argv, deadline, env, cwd)
    monkeypatch.setattr(_sync_core, 'run_command', run_command)
    monkeypatch.setenv('GIT_ASKPASS', 'ask-me')
    monkeypatch.setenv('GIT_DIR', '/elsewhere')
    monkeypatch.setenv('GIT_AUTHOR_NAME', 'Someone')
    device.request()
    assert device.push() == 0
    assert recorded
    for argv, env in recorded:
        assert (env['GIT_TERMINAL_PROMPT'], env['GIT_ASKPASS'], env['GCM_INTERACTIVE']) == ('0', '', 'never')
        assert env['GIT_SSH_COMMAND'].endswith(' -o BatchMode=yes')
        assert env['GIT_DIR'] == os.path.join(device.clone, '.git')
    hooks = ['-c', f'core.hooksPath={device.log / "sync" / "no-hooks"}']
    clone_calls = [env for argv, env in recorded if argv[1:3] == hooks]
    assert len(clone_calls) == len(recorded) - 1                      # all but git config in git_env
    assert all(env['LC_ALL'] == 'C' and env['GIT_AUTHOR_NAME'] == 'usage-clock' for env in clone_calls)
    (argv, env, cwd), = device.probe.calls
    assert argv[1:3] == ['-c', 'credential.helper=']
    assert (env['GIT_TERMINAL_PROMPT'], env['GIT_ASKPASS'], env['GCM_INTERACTIVE']) == ('0', '', 'never')
    assert (env['GIT_CONFIG_GLOBAL'], env['GIT_CONFIG_NOSYSTEM'], env['LC_ALL'], env['LANGUAGE']) == (
        '/dev/null', '1', 'C', 'C')
    assert env['HOME'] == cwd == str(device.log / 'sync') and 'GIT_DIR' not in env


def write_archive(device, days):
    device.log.mkdir(parents=True, exist_ok=True)
    with open(device.log / 'usage-days.jsonl', 'a', encoding='utf-8', newline='\n') as f:
        for day in days:
            f.write(json.dumps({'v': 1, 'day': day, 'stats': {}}) + '\n')


def test_compacting_is_committed_without_rewriting_the_history(device, remote):
    device.request('2026-05-03T12:00:00Z', session='old-1')
    device.request('2026-05-04T12:00:00Z', session='old-2')
    write_archive(device, ['2026-05-03', '2026-05-04'])
    assert device.push(now=T('2026-06-01T12:00:00Z')) == 0
    first = remote.head()
    month = f'{device.own}/requests/2026-05'
    assert {f'{month}/old-1.jsonl', f'{month}/old-2.jsonl'} <= set(remote.files())
    assert device.push(now=NOW) == 0
    files = remote.files()
    assert not any(path.startswith(month) for path in files) and f'{device.own}/days/2026-05.jsonl' in files
    git('merge-base', '--is-ancestor', first, 'refs/heads/main', cwd=remote.bare)     # nothing rewritten
    assert git('log', '--oneline', 'refs/heads/main', cwd=remote.bare).stdout.count('\n') == 3
    assert _sync_git.read_journal(str(device.log / 'sync'), device.id) == set()


@pytest.mark.parametrize('target', ['recent-request', 'days', 'old-request-uncovered'])
def test_a_deletion_outside_the_compacted_months_prevents_the_commit(device, remote, target):
    device.request('2026-05-03T12:00:00Z', session='old-1')
    device.request('2026-05-04T12:00:00Z', session='old-2')
    device.request('2026-10-13T08:00:00Z', session='new-1')
    write_archive(device, ['2026-05-03'])              # 2026-05-04 has requests and no day line
    assert device.push(now=T('2026-10-13T12:00:00Z')) == 0
    assert f'{device.own}/requests/2026-05/old-2.jsonl' in remote.files()      # the month stays: a day line is missing
    head, before = device.head(), remote.head()
    path = {'recent-request': 'requests/2026-10/new-1.jsonl', 'days': 'days/2026-05.jsonl',
            'old-request-uncovered': 'requests/2026-05/old-1.jsonl'}[target]
    os.remove(os.path.join(device.clone, *device.own.split('/'), *path.split('/')))
    device.request('2026-10-13T09:00:00Z', session='new-2')
    assert device.push(now=NOW) == 1
    failure = device.failure()
    assert (failure['category'], failure['reason']) == (
        'internal', 'a deletion that is no compacted request month: nothing is committed')
    assert device.head() == head and remote.head() == before


def test_a_month_missing_a_day_line_for_a_day_with_requests_is_not_compacted(device, remote):
    device.request('2026-05-03T12:00:00Z', session='old-1')
    device.request('2026-05-04T12:00:00Z', session='old-2')
    write_archive(device, ['2026-05-03'])
    assert device.push(now=NOW) == 0
    assert f'{device.own}/requests/2026-05/old-1.jsonl' in remote.files()


def test_a_deleted_month_without_its_day_lines_prevents_the_commit(device, remote):
    device.request('2026-05-03T12:00:00Z', session='old-1')
    device.request('2026-05-04T12:00:00Z', session='old-2')
    write_archive(device, ['2026-05-03'])
    assert device.push(now=NOW) == 0
    head = device.head()
    month = os.path.join(device.clone, *device.own.split('/'), 'requests', '2026-05')
    for name in os.listdir(month):
        os.remove(os.path.join(month, name))
    os.rmdir(month)
    assert device.push(now=NOW + 60) == 1
    failure = device.failure()
    assert (failure['category'], failure['reason']) == (
        'internal', 'a deletion that is no compacted request month: nothing is committed')
    assert device.head() == head


def test_a_run_that_ended_between_compacting_and_committing_does_not_block_the_next(device, remote, monkeypatch):
    device.request('2026-05-03T12:00:00Z', session='old-1')
    write_archive(device, ['2026-05-03'])
    assert device.push(now=T('2026-06-01T12:00:00Z')) == 0

    real_commit, ended = _sync_git._commit, []

    def ends_once(*args):
        if not ended:
            ended.append(True)
            raise _sync_core.SyncError('timeout', 'the run took too long')
        return real_commit(*args)
    monkeypatch.setattr(_sync_git, '_commit', ends_once)
    assert device.push(now=NOW) == 1
    assert _sync_git.read_journal(str(device.log / 'sync'), device.id) == {'2026-05'}
    assert device.push(now=NOW + 60) == 0
    assert not any('/requests/2026-05/' in path for path in remote.files())
    assert _sync_git.read_journal(str(device.log / 'sync'), device.id) == set()


# --- Exported lines survive every failure ---------------------------------------------------------------------------

def refuse_pushes(remote):
    """A pre-receive hook in the bare repository that refuses every push; returns how to lift it."""
    hook = remote.bare / 'hooks' / 'pre-receive'
    hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
    os.chmod(hook, 0o755)
    return lambda: os.replace(hook, remote.bare / 'hooks' / 'pre-receive.off')


@pytest.mark.parametrize('failure', ['guard', 'refused', 'conflict', 'unreachable'])
def test_exported_lines_survive_every_failure_and_go_out_with_the_next_push(tmp_path, device, remote, failure):
    device.request()
    assert device.push() == 0
    device.request()
    lift = None
    if failure == 'guard':
        with open(os.path.join(device.clone, 'notes.txt'), 'w', encoding='utf-8') as f:
            f.write('x\n')
        lift = lambda: os.replace(os.path.join(device.clone, 'notes.txt'), tmp_path / 'notes.txt')  # noqa: E731
    elif failure == 'refused':
        lift = refuse_pushes(remote)
    elif failure == 'conflict':
        device.cfg['sync']['mode'] = 'shadow'
        assert device.push(now=NOW + 30) == 0                 # the second request in a local commit
        device.cfg['sync']['mode'] = 'on'
        remote.foreign_commit('README.md', 'changed\n')       # the histories go apart: a rebase is due

        def rebase_fails(clone):
            raise _sync_core.SyncError('conflict', 'the own commits do not fit onto the remote')
        real_rebase = _sync_git._rebase
        _sync_git._rebase = rebase_fails
        lift = lambda: setattr(_sync_git, '_rebase', real_rebase)  # noqa: E731
    else:
        real_run = _sync_core.run_command

        def push_unreachable(argv, deadline, env=None, cwd=None):
            if 'push' in argv:
                return subprocess.CompletedProcess(argv, 128, '', 'ssh: connect to host github.com port 22: '
                                                                  'Connection timed out\n')
            return real_run(argv, deadline, env, cwd)
        _sync_core.run_command = push_unreachable
        lift = lambda: setattr(_sync_core, 'run_command', real_run)  # noqa: E731
    try:
        assert device.push(now=NOW + 60) == 1
        category = device.failure()['category']
        assert category == {'guard': 'internal', 'refused': 'rejected', 'conflict': 'conflict',
                            'unreachable': 'network'}[failure]
        assert len(device.worktree_requests()) == 2
    finally:
        lift()
    assert device.push(now=NOW + 120) == 0
    pushed = sync_format.decode('request', remote.show(f'{device.own}/requests/2026-10/aaaaaaaa-1.jsonl')
                                .splitlines()[-1])
    assert pushed['k'] == sync_format.request_key('m2', 'r2')


# --- Remote and clone -----------------------------------------------------------------------------------------------

def run_of(device, seconds=60):
    directory = _sync_core.ensure_dir(str(device.log))
    return _sync_core.Run('push', device.cfg['sync']['mode'], device.cfg, str(device.log), directory,
                          _sync_core.Deadline(seconds), NOW)


def failure_of(call, *args):
    with pytest.raises(_sync_core.SyncError) as caught:
        call(*args)
    return caught.value.category, caught.value.detail


def test_remote_urls_are_exactly_the_github_ones(monkeypatch):
    monkeypatch.setattr(_sync_git, 'URLS', REAL_URLS)
    assert _sync_git.remote_url('owner/name', 'https') == 'https://github.com/owner/name.git'
    assert _sync_git.remote_url('owner/name', 'ssh') == 'git@github.com:owner/name.git'
    assert _sync_git.PROBE_URL.format('owner/name') == 'https://github.com/owner/name.git'


@pytest.mark.parametrize('repo, detail', [
    ('', 'sync.repo is not set, run the installer with --sync-repo'),
    ('owner/../name', 'sync.repo is not owner/name'), ('own er/name', 'sync.repo is not owner/name'),
    ('owner/name/more', 'sync.repo is not owner/name'), ('owner', 'sync.repo is not owner/name')])
def test_sync_repo_must_be_set_and_owner_name(repo, detail):
    assert failure_of(_sync_git.configured_repo, config(repo=repo)) == ('internal', detail)


def test_a_missing_clone_is_internal_and_never_cloned(tmp_path, remote):
    device = Device(tmp_path / 'a', clone=False)
    device.request()
    assert device.push() == 1
    assert device.failure()['reason'] == 'the sync clone is missing, run the installer with --sync-repo'
    assert not os.path.exists(device.clone) and device.probe.calls == []


def test_a_clone_that_is_a_file_or_has_a_git_file_is_no_git_folder(tmp_path, remote):
    device = Device(tmp_path / 'a', clone=False)
    os.makedirs(os.path.dirname(device.clone))
    with open(device.clone, 'w', encoding='utf-8') as f:
        f.write('x\n')
    assert failure_of(_sync_git.open_clone, run_of(device)) == ('internal', 'the sync clone is no git folder')
    second = Device(tmp_path / 'b', clone=False)
    os.makedirs(second.clone)
    with open(os.path.join(second.clone, '.git'), 'w', encoding='utf-8') as f:
        f.write(f'gitdir: {remote.bare}\n')
    assert failure_of(_sync_git.open_clone, run_of(second)) == ('internal', 'the sync clone is no git folder')


@pytest.mark.posix_only
def test_a_clone_that_is_a_link_is_no_git_folder(tmp_path, remote):
    device = Device(tmp_path / 'a', clone=False)
    real = tmp_path / 'real-clone'
    git('clone', '--quiet', _sync_git.remote_url(REPO, 'https'), str(real))
    os.makedirs(os.path.dirname(device.clone))
    os.symlink(real, device.clone)
    assert failure_of(_sync_git.open_clone, run_of(device)) == ('internal', 'the sync clone is no git folder')


def test_a_clone_that_cannot_be_read_is_internal(device, monkeypatch):
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if os.fspath(path) == device.clone:
            raise PermissionError(13, 'denied')
        return real_lstat(path, *args, **kwargs)
    monkeypatch.setattr(os, 'lstat', lstat)
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'the sync clone cannot be read (PermissionError)')


def test_the_hook_folder_must_be_an_empty_folder(device, monkeypatch):
    run = run_of(device)
    hooks = os.path.join(run.directory, _sync_git.NO_HOOKS_DIR)
    os.mkdir(hooks)
    with open(os.path.join(hooks, 'pre-commit'), 'w', encoding='utf-8') as f:
        f.write('#!/bin/sh\n')
    assert failure_of(_sync_git.open_clone, run) == ('internal', 'no-hooks in the sync folder is no empty folder')

    def mkdir(path, mode=0o777):
        raise PermissionError(13, 'denied')
    monkeypatch.setattr(os, 'mkdir', mkdir)
    assert failure_of(_sync_git.open_clone, run) == ('internal', 'no-hooks cannot be made (PermissionError)')


@pytest.mark.posix_only
def test_no_hook_of_the_clone_or_of_the_user_runs(device, remote, git_alone):
    marker = device.log.parent / 'hook-ran'
    script = f'#!/bin/sh\necho "$PWD" >> "{marker.as_posix()}"\n'
    user_hooks = device.log.parent / 'user-hooks'
    user_hooks.mkdir()
    for folder in (os.path.join(device.clone, '.git', 'hooks'), str(user_hooks)):
        for name in ('pre-commit', 'commit-msg', 'post-commit', 'pre-push', 'reference-transaction', 'post-rewrite'):
            path = os.path.join(folder, name)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(script)
            os.chmod(path, 0o755)
    device.request()
    assert device.push() == 0
    with open(git_alone, 'a', encoding='utf-8') as f:
        f.write(f'[core]\n\thooksPath = {user_hooks.as_posix()}\n')
    device.request()
    assert device.push(now=NOW + 60) == 0
    assert f'{device.own}/status.json' in remote.files()
    # The bare repository stands for GitHub and reads the same test config: its receiving side runs the user's
    # reference-transaction hook (so the hooks do work), the clone's side runs none.
    ran = marker.read_text(encoding='utf-8').splitlines()
    assert ran and set(ran) == {str(remote.bare)}


def test_a_detached_head_is_no_branch(device):
    git('checkout', '--quiet', '--detach', cwd=device.clone)
    assert failure_of(_sync_git.open_clone, run_of(device)) == ('internal', 'the sync clone has no branch checked out')


def test_a_clone_without_origin_is_not_sync_repo(device):
    git('remote', 'remove', 'origin', cwd=device.clone)
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'the remote of the sync clone is not sync.repo')


def test_the_ssh_command_of_the_clone_gets_batch_mode(device):
    git('config', 'core.sshCommand', 'ssh -i key', cwd=device.clone)
    clone = _sync_git.open_clone(run_of(device))
    assert clone.env['GIT_SSH_COMMAND'] == 'ssh -i key -o BatchMode=yes' and clone.branch == 'main'


# --- Visibility -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('returncode, errors, result', [
    (0, '', 'public'),
    (128, ASKS, 'private'),
    (128, UNKNOWN_HOST, 'unclear'),
    (128, ASKS_GERMAN, 'unclear'),                       # git in German: only the C text counts
    (128, 'remote: Repository not found.\nfatal: repository not found\n', 'unclear'),
    (1, ASKS, 'unclear'),
    (-9, '', 'unclear')])
def test_visibility_table(returncode, errors, result):
    assert _sync_git.judge(returncode, errors) == result


def test_the_probe_asks_git_in_c_so_a_german_system_is_still_told_apart(tmp_path, monkeypatch):
    monkeypatch.setenv('LC_ALL', 'de_DE.UTF-8')
    monkeypatch.setenv('LANGUAGE', 'de')

    def git_in_the_language_of_env(argv, deadline, env, cwd):
        german = env.get('LC_ALL') != 'C' or env.get('LANGUAGE') != 'C'
        return subprocess.CompletedProcess(argv, 128, '', ASKS_GERMAN if german else ASKS)
    monkeypatch.setattr(_sync_core, 'run_command', git_in_the_language_of_env)
    assert _sync_git.check_visibility(REPO, str(tmp_path)) == 'private'


def test_the_probe_has_at_most_15_s_and_never_more_than_the_run(tmp_path):
    seen = []

    def execute(argv, deadline, env, cwd):
        seen.append(deadline.left())
        return subprocess.CompletedProcess(argv, 128, '', ASKS)
    assert _sync_git.check_visibility(REPO, str(tmp_path), execute=execute) == 'private'
    assert _sync_git.check_visibility(REPO, str(tmp_path), _sync_core.Deadline(3), execute) == 'private'
    assert 14 < seen[0] <= 15 and seen[1] <= 3
    assert _sync_git.VISIBILITY_TIMEOUT_S == 15 and _sync_git.VISIBILITY_MAX_AGE_S == 24 * 3600


def test_a_private_result_is_cached_for_24_hours_for_its_repo_only(device, remote):
    device.request()
    assert device.push() == 0 and len(device.probe.calls) == 1
    assert device.push(now=NOW + 23 * 3600) == 0 and len(device.probe.calls) == 1
    assert device.push(now=NOW + 25 * 3600) == 0 and len(device.probe.calls) == 2
    directory = str(device.log / 'sync')
    later = NOW + 25 * 3600
    assert _sync_git.cached_private(directory, REPO, later)
    assert not _sync_git.cached_private(directory, 'owner/other', later)
    assert not _sync_git.cached_private(directory, REPO, later - 60)             # from the future
    for text in ('{', '[]', '{"repo": "owner/name", "result": "private"}', '{"checked": 5}',
                 '{"repo": "owner/name", "result": "public", "checked": "2026-10-14T12:00:00Z"}'):
        with open(os.path.join(directory, _sync_git.VISIBILITY_FILE), 'w', encoding='utf-8') as f:
            f.write(text)
        assert not _sync_git.cached_private(directory, REPO, NOW)


def test_a_cache_that_cannot_be_written_only_means_checking_again(device, remote, monkeypatch):
    def write_private(*args):
        raise OSError('full disk')
    monkeypatch.setattr(_context_state, 'write_private', write_private)
    _sync_git.remember_private(str(device.log), REPO, NOW)
    assert not os.path.exists(device.log / _sync_git.VISIBILITY_FILE)


# --- Fetch, fast-forward, rebase ------------------------------------------------------------------------------------

def scripted(monkeypatch, answers):
    """run_command that answers git subcommands (`fetch`, or two words like `rebase --abort`) with (exit, out, err);
    everything else runs for real."""
    real = _sync_core.run_command

    def run_command(argv, deadline, env=None, cwd=None):
        args = argv[1:]
        while args[:1] == ['-c']:
            args = args[2:]
        for key in (' '.join(args[:2]), args[0]):
            if key in answers:
                code, out, err = answers[key]
                return subprocess.CompletedProcess(argv, code, out, err)
        return real(argv, deadline, env, cwd)
    monkeypatch.setattr(_sync_core, 'run_command', run_command)


def test_an_unreachable_remote_is_network(device, remote, tmp_path):
    os.replace(remote.bare, tmp_path / 'gone.git')
    device.request()
    assert device.push() == 1
    assert (device.failure()['category'], device.failure()['reason']) == (
        'network', 'the fetch did not reach the sync repo')


@pytest.mark.parametrize('errors', ["fatal: could not read Username for 'https://github.com': terminal prompts "
                                    "disabled\n", 'git@github.com: Permission denied (publickey).\n',
                                    "remote: Permission to owner/name.git denied to someone.\n",
                                    "fatal: Authentication failed for 'https://github.com/owner/name.git/'\n"])
def test_missing_or_refused_credentials_are_auth(device, remote, monkeypatch, errors):
    device.request()
    scripted(monkeypatch, {'fetch': (128, '', errors)})
    assert device.push() == 1
    assert (device.failure()['category'], device.failure()['reason']) == (
        'auth', 'the fetch was refused: credentials missing or not accepted')


def test_fast_forward_moves_only_a_clone_that_is_behind(device, remote):
    run = run_of(device)
    clone = _sync_git.open_clone(run)
    assert _sync_git.fast_forward(run, clone) is False                   # same
    remote.foreign_commit('README.md', 'news\n')
    assert _sync_git.fast_forward(run, clone) is True and device.head() == remote.head()
    git('commit', '--quiet', '--allow-empty', '-m', 'local', cwd=device.clone)
    assert _sync_git.fast_forward(run, clone) is False                   # ahead
    remote.foreign_commit('README.md', 'more news\n')
    assert failure_of(_sync_git.fast_forward, run, clone) == (
        'conflict', 'the history of the sync clone went apart from the remote: it is never merged')


def test_fast_forward_of_an_empty_remote_does_nothing(tmp_path):
    bare = tmp_path / 'remotes' / 'owner' / 'name.git'
    bare.parent.mkdir(parents=True)
    git('init', '--bare', '--quiet', '--initial-branch=main', str(bare))
    device = Device(tmp_path / 'a')
    run = run_of(device)
    assert _sync_git.fast_forward(run, _sync_git.open_clone(run)) is False


def test_the_first_push_into_an_empty_remote(tmp_path):
    bare = tmp_path / 'remotes' / 'owner' / 'name.git'
    bare.parent.mkdir(parents=True)
    git('init', '--bare', '--quiet', '--initial-branch=main', str(bare))
    device = Device(tmp_path / 'a')
    device.request()
    assert device.push() == 0
    files = git('ls-tree', '-r', '--name-only', 'refs/heads/main', cwd=bare).stdout.split()
    assert files and all(path.startswith(device.own + '/') for path in files)


def test_a_fast_forward_git_refuses_is_a_conflict_and_keeps_the_change(device, remote):
    with open(os.path.join(device.clone, 'README.md'), 'w', encoding='utf-8') as f:
        f.write('local change\n')
    remote.foreign_commit('README.md', 'remote change\n')
    device.request()
    assert device.push() == 1
    assert (device.failure()['category'], device.failure()['reason']) == (
        'conflict', 'the sync clone cannot be fast-forwarded to the remote')
    with open(os.path.join(device.clone, 'README.md'), encoding='utf-8') as f:
        assert f.read() == 'local change\n'


def test_a_failing_merge_base_is_internal(device, remote, monkeypatch):
    run = run_of(device)
    clone = _sync_git.open_clone(run)
    git('commit', '--quiet', '--allow-empty', '-m', 'local', cwd=device.clone)
    scripted(monkeypatch, {'merge-base': (128, '', 'fatal: bad object')})
    assert failure_of(_sync_git.fast_forward, run, clone) == ('internal', 'git merge-base failed in the sync clone')


def test_a_rebase_that_cannot_start_or_cannot_be_aborted_is_internal(device, remote, monkeypatch):
    clone = _sync_git.open_clone(run_of(device))
    scripted(monkeypatch, {'rebase --quiet': (1, '', 'error: cannot rebase')})
    assert failure_of(_sync_git._rebase, clone) == ('internal', 'the rebase in the sync clone could not start')
    os.mkdir(os.path.join(device.clone, '.git', 'rebase-merge'))
    scripted(monkeypatch, {'rebase --quiet': (1, '', ''), 'rebase --abort': (1, '', '')})
    assert failure_of(_sync_git._rebase, clone) == (
        'internal', 'a failed rebase in the sync clone could not be aborted')


# --- The guard ------------------------------------------------------------------------------------------------------

OWN = 'machines/11111111-2222-4333-8444-555555555555'


def test_status_and_raw_lines_are_parsed_with_both_paths_of_a_rename():
    assert _sync_git.parse_status(f'R  {OWN}/new\0README.md\0?? x\0') == [
        ('R ', f'{OWN}/new'), ('R ', 'README.md'), ('??', 'x')]
    assert _sync_git.parse_raw(f':000000 100644 {"0" * 40} {"1" * 40} A\0{OWN}/status.json\0') == [
        ('A', '100644', f'{OWN}/status.json')]
    assert failure_of(_sync_git.parse_raw, 'garbage\0path\0') == ('internal', 'git diff gave a line that is not --raw')


@pytest.mark.parametrize('path', [f'{OWN}/../x/status.json', f'{OWN}//status.json', f'{OWN}/./status.json',
                                  f'{OWN}\\status.json', 'machines/other/status.json', OWN, f'/{OWN}/status.json'])
def test_a_path_that_is_not_plainly_below_the_own_folder_is_outside(path):
    assert not _sync_git.below(OWN, path)
    assert failure_of(_sync_git.check_entry, 'M', '100644', path, OWN, lambda p: True) == (
        'internal', 'a changed path outside the own machine folder: nothing is committed')


@pytest.mark.parametrize('kind, mode, path', [
    ('T', '100644', f'{OWN}/status.json'), ('A', '120000', f'{OWN}/status.json'), ('A', '160000', f'{OWN}/status.json'),
    ('A', '100755', f'{OWN}/status.json'), ('A', '100644', f'{OWN}/notes.txt'),
    ('A', '100644', f'{OWN}/requests/2026-10/a b.jsonl'), ('U', '100644', f'{OWN}/status.json')])
def test_only_regular_files_of_the_layout_are_committed(kind, mode, path):
    assert failure_of(_sync_git.check_entry, kind, mode, path, OWN, lambda p: True) == (
        'internal', 'a changed path that is no regular file of the layout: nothing is committed')


def test_an_entry_must_be_a_regular_file_in_the_work_tree(tmp_path):
    folder = tmp_path / OWN / 'days' / '2026-10.jsonl'
    folder.mkdir(parents=True)                                 # a folder where the file should be
    for path in (f'{OWN}/days/2026-10.jsonl', f'{OWN}/history/2026-10.jsonl'):    # a folder, missing
        assert failure_of(_sync_git.check_entry, 'A', '100644', path, OWN, lambda p: True, str(tmp_path)) == (
            'internal', 'a changed path that is no regular file of the layout: nothing is committed')


def test_the_guard_refuses_what_git_did_not_take_in_the_own_folder():
    for code in ('??', ' M', 'AM', 'UU'):
        assert failure_of(_sync_git.guard, [(code, f'{OWN}/status.json')], [], OWN, set(), '.') == (
            'internal', 'a change in the own machine folder git did not take: nothing is committed')
    _sync_git.guard([('D ', f'{OWN}/requests/2026-05/s.jsonl')], [('D', '000000', f'{OWN}/requests/2026-05/s.jsonl')],
                    OWN, {'2026-05'}, '.')
    assert _sync_git.request_month(OWN, f'{OWN}/requests/2026-05/s.jsonl') == '2026-05'
    assert _sync_git.request_month(OWN, f'{OWN}/days/2026-05.jsonl') is None


@pytest.mark.posix_only
def test_a_link_in_the_own_folder_prevents_the_commit(device, remote):
    device.request()
    assert device.push() == 0
    head = device.head()
    device.request()
    target = device.log.parent / 'elsewhere.jsonl'
    target.write_text('{}\n', encoding='utf-8')
    os.symlink(target, os.path.join(device.clone, *device.own.split('/'), 'requests', '2026-10', 'linked.jsonl'))
    assert device.push(now=NOW + 60) == 1
    assert device.failure()['reason'] == 'a changed path that is no regular file of the layout: nothing is committed'
    assert device.head() == head


@pytest.mark.parametrize('command, detail', [
    ('status', 'git status failed in the sync clone'), ('diff', 'git diff failed in the sync clone'),
    ('add', 'git add failed in the sync clone'), ('commit', 'the commit in the sync clone failed')])
def test_a_failing_git_step_of_the_commit_is_internal(device, remote, monkeypatch, command, detail):
    device.request()
    scripted(monkeypatch, {command: (128, '', 'fatal: something')})
    assert device.push() == 1
    assert (device.failure()['category'], device.failure()['reason']) == ('internal', detail)


def test_a_status_that_cannot_be_written_is_internal(device, remote, monkeypatch):
    def write_status(*args):
        raise OSError('full disk')
    monkeypatch.setattr(sync_format, 'write_status', write_status)
    device.request()
    assert device.push() == 1 and device.failure()['reason'] == 'status.json cannot be written (OSError)'


def test_an_unusable_machine_id_is_internal(device, remote):
    (device.log / 'sync').mkdir(parents=True, exist_ok=True)
    (device.log / 'sync' / 'machine_id').write_text('not an id\n', encoding='utf-8')
    assert device.push() == 1 and device.failure()['reason'] == 'machine_id: not a UUID4'


# --- The journal of compacted months --------------------------------------------------------------------------------

def test_the_journal_belongs_to_one_machine_and_holds_only_months(tmp_path, monkeypatch):
    directory, machine = str(tmp_path), OWN.split('/')[1]
    assert _sync_git.read_journal(directory, machine) == set()
    assert _sync_git.write_journal(directory, machine, {'2026-05', '2026-04'})
    assert _sync_git.read_journal(directory, machine) == {'2026-04', '2026-05'}
    assert _sync_git.read_journal(directory, '22222222-2222-4333-8444-555555555555') == set()
    for text in ('{', '[]', '{"machine": "%s", "months": "2026-05"}' % machine,
                 '{"machine": "%s", "months": ["2026-13", "../x", 5]}' % machine):
        (tmp_path / _sync_git.JOURNAL_FILE).write_text(text, encoding='utf-8')
        assert _sync_git.read_journal(directory, machine) == set()

    def write_private(*args):
        raise OSError('full disk')
    monkeypatch.setattr(_context_state, 'write_private', write_private)
    assert _sync_git.write_journal(directory, machine, set()) is False


def test_no_journal_no_compacting(device, remote, monkeypatch):
    device.request('2026-05-03T12:00:00Z', session='old-1')
    write_archive(device, ['2026-05-03'])
    assert device.push(now=T('2026-06-01T12:00:00Z')) == 0
    monkeypatch.setattr(_sync_git, 'write_journal', lambda *args: False)
    assert device.push(now=NOW) == 1
    assert device.failure()['reason'] == 'compacting.json cannot be written: nothing is compacted'
    assert f'{device.own}/requests/2026-05/old-1.jsonl' in remote.files()
    assert os.path.exists(os.path.join(device.clone, *device.own.split('/'), 'requests', '2026-05', 'old-1.jsonl'))


def test_compacting_that_fails_is_internal(device, remote, monkeypatch):
    def compactable_months(*args):
        raise sync_format.FormatError('sync.keep_request_months: must be a whole number of at least 1')
    monkeypatch.setattr(sync_format, 'compactable_months', compactable_months)
    device.request()
    assert device.push() == 1
    assert device.failure()['reason'] == 'the old request months cannot be compacted (FormatError)'


# --- The push -------------------------------------------------------------------------------------------------------

REFUSED_BEHIND = '!\tHEAD:refs/heads/main\t[rejected] (fetch first)\nDone\n'


def test_a_push_refused_twice_is_rejected(device, remote, monkeypatch):
    device.request()
    scripted(monkeypatch, {'push': (1, REFUSED_BEHIND, 'error: failed to push some refs\n')})
    assert device.push() == 1
    assert (device.failure()['category'], device.failure()['reason']) == (
        'rejected', 'the push was refused twice: other machines were faster')


def test_a_push_the_repository_refuses_is_rejected(device, remote):
    refuse_pushes(remote)
    device.request()
    assert device.push() == 1
    assert (device.failure()['category'], device.failure()['reason']) == ('rejected', 'the sync repo refused the push')


@pytest.mark.parametrize('errors, category', [
    ("fatal: Authentication failed for 'https://github.com/owner/name.git/'\n", 'auth'),
    ('ssh: connect to host github.com port 22: Connection timed out\n', 'network')])
def test_a_push_that_fails_on_the_way_is_auth_or_network(device, remote, monkeypatch, errors, category):
    device.request()
    scripted(monkeypatch, {'push': (128, '', errors)})
    assert device.push() == 1 and device.failure()['category'] == category


def test_a_push_never_sends_a_path_outside_the_own_folder(device, remote):
    device.request()
    assert device.push() == 0
    with open(os.path.join(device.clone, 'README.md'), 'a', encoding='utf-8') as f:
        f.write('smuggled\n')
    git('commit', '--quiet', '-am', 'by hand', cwd=device.clone)        # a commit the sync did not make
    before = remote.head()
    device.request()
    assert device.push(now=NOW + 60) == 1
    assert device.failure()['reason'] == 'a changed path outside the own machine folder: nothing is committed'
    assert remote.head() == before


def test_an_empty_tree_that_cannot_be_named_is_internal(tmp_path, monkeypatch):
    bare = tmp_path / 'remotes' / 'owner' / 'name.git'
    bare.parent.mkdir(parents=True)
    git('init', '--bare', '--quiet', '--initial-branch=main', str(bare))
    device = Device(tmp_path / 'a')
    device.request()
    scripted(monkeypatch, {'hash-object': (128, '', 'fatal: no')})
    assert device.push() == 1 and device.failure()['reason'] == 'git hash-object failed in the sync clone'


def test_on_without_news_and_nothing_ahead_pushes_nothing(device, remote, monkeypatch):
    device.request()
    assert device.push() == 0
    commands, real_run = [], _sync_core.run_command

    def run_command(argv, deadline, env=None, cwd=None):
        commands.append(argv)
        return real_run(argv, deadline, env, cwd)
    monkeypatch.setattr(_sync_core, 'run_command', run_command)
    assert device.push(now=NOW + 60) == 0
    assert not any('push' in argv for argv in commands) and device.state().last_error is None


def test_sync_py_pushes_with_the_git_work(tmp_path, remote, monkeypatch):
    device = Device(tmp_path / 'log-root')
    config_file = tmp_path / 'config.json'
    config_file.write_text(json.dumps({'sync': {'mode': 'on', 'repo': REPO}}), encoding='utf-8')
    monkeypatch.setenv('CCUC_CONFIG', str(config_file))
    monkeypatch.setenv('CCUC_LOG_DIR', str(device.log))
    monkeypatch.setattr(_sync_git, 'PROJECTS_DIR', str(device.projects))
    monkeypatch.setattr(_sync_core, 'run_command', unchanged_except_probe(_sync_core.run_command))
    device.request()
    assert sync.main(['push', '--now']) == 0
    assert f'{device.own}/status.json' in remote.files()


def unchanged_except_probe(real):
    """run_command, but the visibility probe answers `private` without the network."""
    def run_command(argv, deadline, env=None, cwd=None):
        if 'ls-remote' in argv:
            return subprocess.CompletedProcess(argv, 128, '', ASKS)
        return real(argv, deadline, env, cwd)
    return run_command

