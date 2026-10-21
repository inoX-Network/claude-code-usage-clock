"""Tests for hooks/_sync_git.py: the push of the sync against local bare repositories.

No test needs the network: the remote is a bare repository below tmp_path (URLS point there, `remote_url` keeps the
real ones in its own test), the visibility probe is a function handed in (`execute`). Git runs with an empty global
config and without the system config, so nothing of the real configuration takes part, and with the C locale as the
push sets it."""
import copy
import dataclasses
import email.message
import functools
import http.client
import io
import json
import os
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request

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
    # A probe that can read the repository asks the API: by default it answers as for a public repository at home
    monkeypatch.setattr(_sync_git, 'ask_api', lambda repo, seconds: (json.dumps(
        {'private': False, 'visibility': 'public', 'full_name': repo}).encode('utf-8'), ''))
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
        """Someone else commits `path` (a text, or bytes as they are) and pushes it."""
        git('pull', '--quiet', '--ff-only', cwd=self.other)
        target = self.other / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
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


def change_outside(clone, change):
    """A change outside the own folder of the clone, of one kind."""
    if change == 'tracked':
        with open(os.path.join(clone, 'README.md'), 'a', encoding='utf-8') as f:
            f.write('more\n')
    elif change == 'untracked':
        with open(os.path.join(clone, 'notes.txt'), 'w', encoding='utf-8') as f:
            f.write('x\n')
    elif change == 'other-machine':
        folder = os.path.join(clone, 'machines', '11111111-2222-4333-8444-555555555555')
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'machine.json'), 'w', encoding='utf-8') as f:
            f.write('{}\n')
    else:
        with open(os.path.join(clone, 'staged.txt'), 'w', encoding='utf-8') as f:
            f.write('x\n')
        git('add', 'staged.txt', cwd=clone)


@pytest.mark.parametrize('change', ['tracked', 'untracked', 'other-machine', 'staged'])
def test_a_changed_file_outside_the_own_folder_prevents_the_commit(device, remote, monkeypatch, change):
    """Changes outside found at the start are put back (`_heal`); one that turns up during the run reaches the
    guard."""
    device.request()
    assert device.push() == 0
    head, before = device.head(), remote.head()
    device.request()
    real_export = _sync_export.export

    def export_and_change_outside(*args, **kwargs):
        result = real_export(*args, **kwargs)
        change_outside(device.clone, change)
        return result
    monkeypatch.setattr(_sync_export, 'export', export_and_change_outside)
    assert device.push() == 1
    assert device.failure()['category'] == 'internal'
    assert device.head() == head and remote.head() == before
    assert len(device.worktree_requests()) == 2                       # the export is kept


@pytest.mark.parametrize('change', ['tracked', 'untracked', 'other-machine', 'staged', 'all'])
def test_changes_outside_the_own_folder_left_by_an_ended_run_are_put_back(device, remote, change):
    """As an ended fast-forward or checkout leaves them: the next run puts back what lies outside the own folder and
    goes on; the own folder, with an export not yet committed, is never touched."""
    device.request()
    assert device.push() == 0
    other = 'machines/11111111-2222-4333-8444-555555555555'
    remote.foreign_commit(f'{other}/a[1].jsonl', '{"v":1}\n')       # a name that would be a glob
    git('pull', '--quiet', '--ff-only', cwd=device.clone)
    for kind in (['tracked', 'untracked', 'other-machine', 'staged'] if change == 'all' else [change]):
        change_outside(device.clone, kind)
    with open(os.path.join(device.clone, *other.split('/'), 'a[1].jsonl'), 'w', encoding='utf-8') as f:
        f.write('half written\n')
    own_line = os.path.join(device.clone, *device.own.split('/'), 'requests', '2026-10', 'aaaaaaaa-1.jsonl')
    with open(own_line, encoding='utf-8') as f:
        exported = f.read()
    device.request()
    assert device.push(now=NOW + 60) == 0
    assert device.status() == '' and remote.head() == device.head()
    assert remote.show('README.md') == 'sync\n' and remote.show(f'{other}/a[1].jsonl') == '{"v":1}\n'
    assert not os.path.exists(os.path.join(device.clone, 'notes.txt'))
    assert not os.path.exists(os.path.join(device.clone, 'staged.txt'))
    with open(own_line, encoding='utf-8') as f:
        assert f.read().startswith(exported) and len(device.worktree_requests()) == 2


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
    assert device.push(now=NOW + 120) == 1                          # the rebase of step 4 meets the foreign change
    assert device.failure()['category'] == 'conflict'
    assert device.status() == '' and not os.path.exists(os.path.join(device.clone, '.git', 'rebase-merge'))
    base = git('merge-base', 'HEAD', foreign, cwd=device.clone).stdout.strip()
    assert git('log', '--format=%an', f'{base}..HEAD', cwd=device.clone).stdout.split() == ['usage-clock']
    assert len(device.worktree_requests()) == 2 and remote.head() == foreign      # the shadow commit stays


def test_a_rebase_conflict_after_the_commit_keeps_both_own_commits(device, remote):
    device.request()
    assert device.push() == 0
    device.cfg['sync']['mode'] = 'shadow'
    device.request()
    assert device.push(now=NOW + 60) == 0
    remote.foreign_commit(f'{device.own}/status.json', '{"v":1}\n')
    foreign = remote.head()
    with open(os.path.join(device.clone, *device.own.split('/'), 'status.json'), 'w', encoding='utf-8') as f:
        f.write('{"v":1,"dirty":true}\n')                            # dirty: step 4 leaves the rebase to step 7b
    device.cfg['sync']['mode'] = 'on'
    device.request()
    assert device.push(now=NOW + 120) == 1 and device.failure()['category'] == 'conflict'
    assert device.status() == '' and not os.path.exists(os.path.join(device.clone, '.git', 'rebase-merge'))
    base = git('merge-base', 'HEAD', foreign, cwd=device.clone).stdout.strip()
    assert git('log', '--format=%an', f'{base}..HEAD', cwd=device.clone).stdout.split() == ['usage-clock'] * 2
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
        assert env['GIT_SSH_COMMAND'].endswith(' -o BatchMode=yes -o StrictHostKeyChecking=yes')
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
    assert env['HOME'] == cwd == str(device.log / 'sync') and env['GIT_DIR'] == os.devnull


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
        real_export = _sync_export.export

        def export_and_change_outside(*args, **kwargs):
            result = real_export(*args, **kwargs)
            change_outside(device.clone, 'untracked')     # the next run puts it back
            return result
        _sync_export.export = export_and_change_outside
        lift = lambda: setattr(_sync_export, 'export', real_export)  # noqa: E731
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
    assert clone.env['GIT_SSH_COMMAND'] == 'ssh -i key -o BatchMode=yes -o StrictHostKeyChecking=yes'
    assert clone.branch == 'main'


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


# --- Visibility behind a proxy that adds credentials (0034-SYNC-PROXY-SICHT v2) ---------------------------------------

def api(answered):
    """An anonymous request to the REST API: bytes are the body of a 200, a text is the reason there was none. Records
    its calls."""
    def ask(repo, seconds):
        ask.calls.append((repo, seconds))
        return (answered, '') if isinstance(answered, bytes) else (None, answered)
    ask.calls = []
    return ask


def never_asked(repo, seconds):
    pytest.fail('the API may only be asked after an anonymous ls-remote that could read the repository')


def body(**fields):
    return json.dumps(fields).encode('utf-8')


INJECTED = body(id=1, private=True, visibility='private', full_name=REPO)
PUBLIC_ANSWER = ('public', '')


def unclear(reason):
    return ('unclear', reason)


@pytest.mark.parametrize('answered, result', [
    (body(private=True, full_name=REPO), ('private', 'credentials_injected')),
    (body(private=True, visibility='private', full_name=REPO), ('private', 'credentials_injected')),
    (body(private=True, full_name='Owner/NAME'), ('private', 'credentials_injected')),
    (body(private=False, full_name=REPO), PUBLIC_ANSWER),
    (body(private=False, visibility='public', full_name=REPO), PUBLIC_ANSWER),
    (body(private=True, visibility='internal', full_name=REPO), PUBLIC_ANSWER),
    (body(private=True, visibility='public', full_name=REPO), PUBLIC_ANSWER),       # a word for public decides
    (body(private=False, visibility='private', full_name=REPO), PUBLIC_ANSWER),
    (body(visibility='public', full_name=REPO), PUBLIC_ANSWER),
    ('timeout', unclear('timeout')), ('network', unclear('network')), ('http_301', unclear('http_301')),
    ('http_403', unclear('http_403')), ('http_404', unclear('http_404')), ('http_429', unclear('http_429')),
    ('http_500', unclear('http_500')), ('too_large', unclear('too_large')),
    (b'', unclear('no_json')), (b'not json', unclear('no_json')), (b'\xff\xfe{}', unclear('no_json')),
    (b'[' * 60000, unclear('no_json')),                                           # nested deeper than the parser goes
    (b'[{"private": true, "full_name": "owner/name"}]', unclear('not_an_object')),
    (b'"private"', unclear('not_an_object')),
    (body(private='true', full_name=REPO), unclear('no_private')),
    (body(private=1, full_name=REPO), unclear('no_private')),
    (body(private=None, full_name=REPO), unclear('no_private')),
    (body(full_name=REPO), unclear('no_private')),                                # `private` missing
    (body(private=True, visibility=None, full_name=REPO), unclear('visibility_other')),
    (body(private=True, visibility='PRIVATE', full_name=REPO), unclear('visibility_other')),
    (body(private=True, full_name='owner/other'), unclear('name_mismatch')),
    (body(private=True, full_name='other/name'), unclear('name_mismatch')),
    (body(private=True, full_name='owner/name/x'), unclear('name_mismatch')),
    (body(private=True), unclear('name_mismatch')),                               # `full_name` missing
    (body(private=True, full_name=[REPO]), unclear('name_mismatch')),
    (body(private=False, full_name='owner/other'), unclear('name_mismatch')),     # public, but another repository
], ids=['private', 'private-visibility', 'case-of-full-name', 'public', 'public-visibility', 'internal',
        'visibility-public-wins', 'private-false-wins', 'only-visibility', 'timeout', 'network', '301', '403', '404',
        '429', '500', 'too-large', 'empty', 'no-json', 'not-utf-8', 'too-deep', 'a-list', 'a-string', 'private-string',
        'private-number', 'private-null', 'private-missing', 'visibility-null', 'visibility-upper-case', 'other-name',
        'other-owner', 'longer-name', 'full-name-missing', 'full-name-list', 'public-of-another-repo'])
def test_a_readable_probe_asks_the_api_once_and_only_an_explicit_answer_decides(tmp_path, answered, result):
    ask = api(answered)
    assert _sync_git.probe_visibility(REPO, str(tmp_path), execute=answer(0), api=ask) == result
    assert _sync_git.check_visibility(REPO, str(tmp_path), execute=answer(0), api=api(answered)) == result[0]
    assert [repo for repo, _seconds in ask.calls] == [REPO]


@pytest.mark.parametrize('probe, result', [(answer(128, ASKS), 'private'), (answer(128, UNKNOWN_HOST), 'unclear'),
                                           (answer(1), 'unclear'), (answer(128, ASKS_GERMAN), 'unclear')],
                         ids=['private', 'unknown-host', 'exit-1', 'german-text'])
def test_without_such_a_proxy_the_api_is_never_asked(tmp_path, probe, result):
    """At home the anonymous ls-remote of a private repository is refused: everything stays as it was."""
    assert _sync_git.probe_visibility(REPO, str(tmp_path), execute=probe, api=never_asked) == (result, '')
    assert _sync_git.check_visibility(REPO, str(tmp_path), execute=probe, api=never_asked) == result


def test_a_probe_that_times_out_never_asks_the_api(tmp_path):
    def hangs(argv, deadline, env, cwd):
        raise _sync_core.SyncError('timeout', 'git did not finish in time')
    assert _sync_git.probe_visibility(REPO, str(tmp_path), execute=hangs, api=never_asked) == ('unclear', '')


def test_the_api_gets_what_is_left_of_the_15_s_and_never_more_than_the_run(tmp_path):
    ask = api(INJECTED)
    _sync_git.probe_visibility(REPO, str(tmp_path), execute=answer(0), api=ask)
    _sync_git.probe_visibility(REPO, str(tmp_path), _sync_core.Deadline(3), answer(0), ask)
    assert 14 < ask.calls[0][1] <= 15 and 0 < ask.calls[1][1] <= 3

    def slow(argv, deadline, env, cwd):
        time.sleep(0.3)
        return subprocess.CompletedProcess(argv, 0, '', '')
    _sync_git.probe_visibility(REPO, str(tmp_path), execute=slow, api=ask)
    assert ask.calls[2][1] <= 14.7                         # 15 s for both: what the probe took is gone


def test_with_no_time_left_for_the_api_the_visibility_is_unclear(tmp_path):
    assert _sync_git.probe_visibility(REPO, str(tmp_path), _sync_core.Deadline(0), answer(0),
                                      never_asked) == unclear('no_time')


class Response:
    """What urlopen hands back: a status and a body. `read(n)` waits for all n bytes (as http.client does), `read1(n)`
    returns what has arrived, at most `burst` bytes; each byte takes `per_byte` seconds on `clock`."""

    def __init__(self, status, data, clock=None, per_byte=0.0, burst=None):
        self.status, self.data, self.asked, self.offset = status, data, [], 0
        self.clock, self.per_byte, self.burst = clock, per_byte, burst

    def _take(self, n):
        self.asked.append(n)
        piece = self.data[self.offset:] if n < 0 else self.data[self.offset:self.offset + n]
        self.offset += len(piece)
        if self.clock is not None:
            self.clock[0] += self.per_byte * len(piece)
        return piece

    def read(self, n=-1):
        return self._take(n)

    def read1(self, n=-1):
        return self._take(n if self.burst is None else min(n, self.burst))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    """An opener that records the request and answers with `outcome` (raised if it is an exception)."""

    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def http_error(code):
    headers = email.message.Message()
    headers['Location'] = 'https://api.github.com/repositories/1'
    return urllib.error.HTTPError(_sync_git.API_URL.format(REPO), code, 'status', headers, io.BytesIO(b'{}'))


REAL_ASK_API = _sync_git.ask_api       # git_alone puts an API in its place that answers like for a public repository


def test_the_api_request_is_an_anonymous_get_of_this_repo():
    opener = Opener(Response(200, INJECTED))
    assert REAL_ASK_API(REPO, 7.5, opener) == (INJECTED, '')
    (request, timeout), = opener.calls
    assert request.full_url == 'https://api.github.com/repos/owner/name' and request.get_method() == 'GET'
    assert request.data is None and timeout == 7.5
    sent = {name.lower() for name in [*request.headers, *request.unredirected_hdrs]}
    assert 'authorization' not in sent and 'cookie' not in sent
    assert request.get_header('User-agent') and request.get_header('Accept') == 'application/vnd.github+json'


@pytest.mark.parametrize('outcome, reason', [
    (http_error(301), 'http_301'), (http_error(302), 'http_302'), (http_error(307), 'http_307'),
    (http_error(308), 'http_308'), (http_error(403), 'http_403'), (http_error(404), 'http_404'),
    (http_error(429), 'http_429'), (http_error(500), 'http_500'), (http_error(503), 'http_503'),
    (urllib.error.URLError('down'), 'network'), (urllib.error.URLError(socket.timeout('timed out')), 'timeout'),
    (TimeoutError(), 'timeout'), (socket.timeout(), 'timeout'), (ConnectionResetError(), 'network'),
    (http.client.IncompleteRead(b'{'), 'network'), (http.client.RemoteDisconnected('gone'), 'network'),
    (ValueError('unknown url type'), 'network')],
    ids=['301', '302', '307', '308', '403', '404', '429', '500', '503', 'url-error', 'url-timeout', 'timeout',
         'socket-timeout', 'reset', 'incomplete', 'disconnected', 'value-error'])
def test_any_failure_of_the_api_request_is_no_answer_with_its_reason(outcome, reason):
    assert REAL_ASK_API(REPO, 5, Opener(outcome)) == (None, reason)


@pytest.mark.parametrize('status', [201, 203, 204, 206, 304])
def test_only_status_200_is_an_answer(status):
    assert REAL_ASK_API(REPO, 5, Opener(Response(status, INJECTED))) == (None, f'status_{status}')


def test_an_answer_over_64_kb_is_no_answer_and_more_is_never_read():
    limit = 64 * 1024
    assert _sync_git.API_MAX_BYTES == limit
    exact = Response(200, b' ' * (limit - len(INJECTED)) + INJECTED)
    assert REAL_ASK_API(REPO, 5, Opener(exact)) == (exact.data, '')
    over = Response(200, b' ' * (limit + 2 - len(INJECTED)) + INJECTED)
    assert REAL_ASK_API(REPO, 5, Opener(over)) == (None, 'too_large')
    assert sum(n for n in over.asked if n > 0) == limit + 1 and all(0 < n <= limit + 1 for n in over.asked)


def test_an_answer_that_trickles_in_past_its_time_is_a_timeout_in_time():
    """urllib's timeout holds for each read of the socket, and read(n) waits for all n bytes: measured by the
    Gegenprüfung, 40 bytes at 0.25 s each with 1.0 s left ended after 10 s. read1 takes what has arrived, so the clock
    is checked after each piece: the timeout comes after the 1.0 s."""
    clock = [100.0]
    slow = Response(200, b'{' + b' ' * 39, clock, per_byte=0.25, burst=1)
    assert REAL_ASK_API(REPO, 1.0, Opener(slow), clock=lambda: clock[0]) == (None, 'timeout')
    assert clock[0] - 100.0 <= 1.25
    clock[0] = 100.0
    quick = Response(200, INJECTED, clock, per_byte=0.001, burst=8)
    assert REAL_ASK_API(REPO, 1.0, Opener(quick), clock=lambda: clock[0]) == (INJECTED, '')


def test_the_api_without_an_opener_builds_its_own(monkeypatch):
    opener = Opener(Response(200, INJECTED))
    monkeypatch.setattr(_sync_git, 'api_opener', lambda: opener)
    assert REAL_ASK_API(REPO, 5) == (INJECTED, '') and len(opener.calls) == 1


def test_the_opener_takes_the_proxy_follows_no_redirect_and_knows_no_credentials(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:3128')
    opener = _sync_git.api_opener()
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies.get('https') == 'http://proxy.invalid:3128'
               for h in opener.handlers)
    assert not any(isinstance(h, (urllib.request.AbstractBasicAuthHandler, urllib.request.AbstractDigestAuthHandler,
                                  urllib.request.HTTPCookieProcessor)) for h in opener.handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in opener.handlers)
    request = urllib.request.Request(_sync_git.API_URL.format(REPO))
    for code in (301, 302, 303, 307, 308):
        headers = email.message.Message()
        headers['Location'] = 'https://api.github.com/repositories/1'
        with pytest.raises(urllib.error.HTTPError) as caught:
            opener.error('https', request, io.BytesIO(b''), code, 'moved', headers)
        assert caught.value.code == code


def test_no_test_reaches_the_network_through_urllib():
    """The guard of conftest.py: a probe that could read the repository asks the API, which no test may reach."""
    with pytest.raises(urllib.error.URLError, match='a test may not reach the network'):
        _sync_git.api_opener().open(_sync_git.API_URL.format(REPO), timeout=1)
    assert REAL_ASK_API(REPO, 1) == (None, 'network')


def test_behind_such_a_proxy_a_private_repo_is_pushed_and_its_reason_cached_and_logged_once(device, remote,
                                                                                         monkeypatch):
    ask = api(INJECTED)
    monkeypatch.setattr(_sync_git, 'ask_api', ask)
    device.request()
    readable = answer(0)
    assert device.push(execute=readable) == 0
    assert f'{device.own}/machine.json' in remote.files()
    cache = json.loads((device.log / 'sync' / _sync_git.VISIBILITY_FILE).read_text(encoding='utf-8'))
    assert (cache['repo'], cache['result'], cache['reason']) == (REPO, 'private', 'credentials_injected')
    assert _sync_git.private_reason(str(device.log / 'sync'), REPO) == 'credentials_injected'
    noted = [r for r in device.lines() if r['decision'] == 'credentials_injected']
    assert len(noted) == 1 and REPO not in noted[0]['reason']
    device.request()
    assert device.push(now=NOW + 3600, execute=readable) == 0       # from the cache: no probe, no API, no line
    assert len(readable.calls) == len(ask.calls) == 1
    assert len([r for r in device.lines() if r['decision'] == 'credentials_injected']) == 1


def test_behind_such_a_proxy_an_explicit_public_answer_blocks_as_before(device, remote, monkeypatch):
    monkeypatch.setattr(_sync_git, 'ask_api', api(body(private=False, full_name=REPO)))
    device.request()
    before = remote.head()
    assert device.push(execute=answer(0)) == 1
    assert (device.state().last_error, device.failure()['category']) == ('visibility', 'visibility')
    assert device.state().blocked
    assert remote.head() == before and not os.path.exists(device.log / 'sync' / _sync_git.VISIBILITY_FILE)
    assert not [r for r in device.lines() if r['decision'] == 'credentials_injected']


@pytest.mark.parametrize('failed', ['timeout', 'http_429', 'http_301', 'http_503'])
def test_no_answer_of_the_api_ends_as_network_names_why_and_the_next_run_asks_again(device, remote, monkeypatch,
                                                                                    failed):
    monkeypatch.setattr(_sync_git, 'ask_api', api(failed))
    device.request()
    before = remote.head()
    assert device.push(execute=answer(0)) == 1
    failure = device.failure()
    assert (failure['category'], device.state().last_error) == ('network', 'network') and not device.state().blocked
    assert failure['reason'] == f'whether the sync repo is public could not be told (GitHub API: {failed}): nothing ' \
                                'is pushed'
    assert remote.head() == before and not os.path.exists(device.log / 'sync' / _sync_git.VISIBILITY_FILE)
    monkeypatch.setattr(_sync_git, 'ask_api', api(INJECTED))
    assert device.push(now=NOW + 60, execute=answer(0)) == 0
    assert f'{device.own}/machine.json' in remote.files()


def test_an_unclear_ls_remote_keeps_its_old_reason_text(device, remote):
    device.request()
    assert device.push(execute=answer(128, UNKNOWN_HOST)) == 1
    assert device.failure()['reason'] == 'whether the sync repo is public could not be told: nothing is pushed'


def test_a_private_result_found_without_a_proxy_has_no_reason(device, remote):
    device.request()
    assert device.push() == 0
    cache = json.loads((device.log / 'sync' / _sync_git.VISIBILITY_FILE).read_text(encoding='utf-8'))
    assert 'reason' not in cache
    assert _sync_git.private_reason(str(device.log / 'sync'), REPO) == ''


@pytest.mark.parametrize('content', [
    {'repo': REPO, 'result': 'private', 'checked': '2026-10-14T12:00:00Z', 'reason': 'credentials_injected'},
    {'repo': REPO, 'result': 'private', 'checked': '2026-10-14T12:00:00Z', 'reason': 'something else'},
    {'repo': REPO, 'result': 'private', 'checked': '2026-10-14T12:00:00Z', 'reason': ['credentials_injected']},
    {'repo': 'other/name', 'result': 'private', 'checked': '2026-10-14T12:00:00Z', 'reason': 'credentials_injected'},
    {'repo': REPO, 'result': 'public', 'checked': '2026-10-14T12:00:00Z', 'reason': 'credentials_injected'},
    {'repo': REPO, 'result': 'private', 'checked': 'yesterday', 'reason': 'credentials_injected'},
    ['credentials_injected'], 'broken', None],
    ids=['injected', 'unknown-reason', 'reason-list', 'other-repo', 'public', 'bad-time', 'a-list', 'broken',
         'missing'])
def test_the_reason_is_read_only_from_a_valid_private_note_of_this_repo(tmp_path, content):
    if content == 'broken':
        (tmp_path / _sync_git.VISIBILITY_FILE).write_text('{broken', encoding='utf-8')
    elif content is not None:
        (tmp_path / _sync_git.VISIBILITY_FILE).write_text(json.dumps(content), encoding='utf-8')
    expected = 'credentials_injected' if isinstance(content, dict) and content == {
        'repo': REPO, 'result': 'private', 'checked': '2026-10-14T12:00:00Z', 'reason': 'credentials_injected'} else ''
    assert _sync_git.private_reason(str(tmp_path), REPO) == expected


def test_an_unknown_reason_is_never_written(tmp_path):
    _sync_git.remember_private(str(tmp_path), REPO, NOW, 'made up')
    assert 'reason' not in json.loads((tmp_path / _sync_git.VISIBILITY_FILE).read_text(encoding='utf-8'))
    _sync_git.remember_private(str(tmp_path), REPO, NOW, 'timeout')
    assert 'reason' not in json.loads((tmp_path / _sync_git.VISIBILITY_FILE).read_text(encoding='utf-8'))



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


# Measured (git 2.56, a valid login): https with its two lines; ssh as GitHub answers it
@pytest.mark.parametrize('errors', ["remote: Repository not found.\nfatal: repository "
                                    "'https://github.com/owner/name.git/' not found\n",
                                    'ERROR: Repository not found.\nfatal: Could not read from remote repository.\n'])
def test_a_repository_that_does_not_exist_is_auth_and_names_sync_repo(device, remote, monkeypatch, errors):
    device.request()
    scripted(monkeypatch, {'fetch': (128, '', errors)})
    assert device.push() == 1
    assert (device.failure()['category'], device.failure()['reason']) == (
        'auth', 'the fetch was refused: the repository does not exist or this login cannot reach it (check sync.repo)')


def test_a_not_found_without_the_answer_of_github_stays_network(device, remote, monkeypatch):
    device.request()
    scripted(monkeypatch, {'fetch': (128, '', "fatal: repository 'https://example.org/owner/name.git/' not found\n")})
    assert device.push() == 1
    assert (device.failure()['category'], device.failure()['reason']) == (
        'network', 'the fetch did not reach the sync repo')


def test_update_clone_fast_forwards_rebases_or_leaves_a_dirty_clone_alone(device, remote):
    run = run_of(device)
    clone = _sync_git.open_clone(run)
    assert _sync_git.update_clone(run, clone) == 'up_to_date'                 # same
    remote.foreign_commit('README.md', 'news\n')
    assert _sync_git.update_clone(run, clone) == 'fast_forwarded' and device.head() == remote.head()
    git('commit', '--quiet', '--allow-empty', '-m', 'local', cwd=device.clone)
    assert _sync_git.update_clone(run, clone) == 'up_to_date'                 # ahead
    remote.foreign_commit('README.md', 'more news\n')
    with open(os.path.join(device.clone, 'notes.txt'), 'w', encoding='utf-8') as f:
        f.write('x\n')
    local = device.head()
    assert _sync_git.update_clone(run, clone) == 'diverged_dirty' and device.head() == local
    os.replace(os.path.join(device.clone, 'notes.txt'), device.log.parent / 'notes.txt')
    assert _sync_git.update_clone(run, clone) == 'rebased'
    git('merge-base', '--is-ancestor', remote.head(), 'HEAD', cwd=device.clone)
    assert git('log', '--format=%s', '-1', cwd=device.clone).stdout.strip() == 'local'
    assert git('log', '--merges', '--oneline', cwd=device.clone).stdout == ''


def test_update_clone_with_an_empty_remote_does_nothing(tmp_path):
    bare = tmp_path / 'remotes' / 'owner' / 'name.git'
    bare.parent.mkdir(parents=True)
    git('init', '--bare', '--quiet', '--initial-branch=main', str(bare))
    device = Device(tmp_path / 'a')
    run = run_of(device)
    assert _sync_git.update_clone(run, _sync_git.open_clone(run)) == 'up_to_date'


def test_no_git_config_of_the_user_changes_the_line_ends_in_the_work_tree(device, remote, git_alone):
    """Git for Windows sets core.autocrlf=true in its system config: the clone would check foreign files out with
    CRLF, and an export appending LF lines would leave them mixed. The work tree holds the bytes of the repository."""
    with open(git_alone, 'a', encoding='utf-8') as f:
        f.write('[core]\n\tautocrlf = true\n')
    run = run_of(device)
    clone = _sync_git.open_clone(run)
    remote.foreign_commit('machines/other/days/2026-10.jsonl', b'{"d":1}\n{"d":2}\n')
    assert _sync_git.update_clone(run, clone) == 'fast_forwarded'
    with open(os.path.join(device.clone, 'machines', 'other', 'days', '2026-10.jsonl'), 'rb') as f:
        assert f.read() == b'{"d":1}\n{"d":2}\n'


@pytest.mark.parametrize('before', [None, b'', b'* filter=lfs\n'], ids=['missing', 'empty', 'changed'])
def test_open_clone_puts_the_attributes_back_and_a_fast_forward_takes_no_foreign_filter(device, remote, git_alone,
                                                                                        monkeypatch, before):
    """A clone without .git/info/attributes (made before they were written), or with other ones, gets them back on
    opening: they only ever switch conversions off. git before 2.43 ignores attr.tree, so without them a .gitattributes
    of another machine would apply to the fast-forward."""
    monkeypatch.setattr(_sync_git, 'SETTINGS', tuple(s for s in _sync_git.SETTINGS if not s.startswith('attr.tree=')))
    with open(git_alone, 'a', encoding='utf-8') as f:
        f.write('[filter "lfs"]\n\tsmudge = tr a-z A-Z\n\tclean = cat\n')
    info = os.path.join(device.clone, '.git', 'info')
    if before is not None:
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, 'attributes'), 'wb') as f:
            f.write(before)
    run = run_of(device)
    clone = _sync_git.open_clone(run)
    with open(os.path.join(info, 'attributes'), 'rb') as f:
        assert f.read() == _sync_git.INFO_ATTRIBUTES.encode()
    remote.foreign_commit('.gitattributes', b'* filter=lfs text eol=crlf\n')
    remote.foreign_commit('machines/other/days/2026-10.jsonl', b'{"d":"abc"}\n')
    assert _sync_git.update_clone(run, clone) == 'fast_forwarded'
    with open(os.path.join(device.clone, 'machines', 'other', 'days', '2026-10.jsonl'), 'rb') as f:
        assert f.read() == b'{"d":"abc"}\n'


def refusing_lstat(monkeypatch, refused):
    """os.lstat raises PermissionError for the path `refused`, as for a folder this user may not read."""
    real = os.lstat

    def lstat(path, *args, **kwargs):
        if os.fspath(path) == refused:
            raise PermissionError(13, 'denied', path)
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, 'lstat', lstat)


def test_a_clone_without_an_info_folder_gets_one_with_the_attributes(device):
    info = os.path.join(device.clone, '.git', 'info')
    for name in os.listdir(info):
        os.unlink(os.path.join(info, name))
    os.rmdir(info)
    _sync_git.pin_attributes(device.clone)
    with open(os.path.join(info, 'attributes'), 'rb') as f:
        assert f.read() == _sync_git.INFO_ATTRIBUTES.encode()


def test_an_info_folder_that_cannot_be_read_is_internal(device, monkeypatch):
    refusing_lstat(monkeypatch, os.path.join(device.clone, '.git', 'info'))
    assert failure_of(_sync_git.pin_attributes, device.clone) == (
        'internal', 'the info folder of the sync clone cannot be read (PermissionError)')


def test_locks_in_a_folder_below_git_that_cannot_be_read_are_left_alone(device, monkeypatch):
    git_dir = os.path.join(device.clone, '.git')
    with open(os.path.join(git_dir, 'refs', 'old.lock'), 'w', encoding='utf-8'):
        pass
    assert os.path.join(git_dir, 'refs', 'old.lock') in _sync_git._left_over(git_dir)[0]
    refusing_lstat(monkeypatch, os.path.join(git_dir, 'refs'))
    assert not [path for path in _sync_git._left_over(git_dir)[0] if 'old.lock' in path]


def test_attributes_that_cannot_be_written_are_internal(device, monkeypatch):
    monkeypatch.setattr(_context_state, 'write_private', lambda *args: False)      # a link as .git/info
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'the attributes of the sync clone cannot be written')

    def full(*args):
        raise OSError(28, 'no space left')
    monkeypatch.setattr(_context_state, 'write_private', full)
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'the attributes of the sync clone cannot be written')


def test_shadow_gathers_commits_while_another_machine_pushes_and_on_sends_them(tmp_path, remote):
    first, second = Device(tmp_path / 'a', mode='shadow'), Device(tmp_path / 'b')
    for minute in range(3):
        first.request()
        assert first.push(now=NOW + 60 * minute) == 0
        second.request()
        assert second.push(now=NOW + 60 * minute + 30) == 0
    base = git('merge-base', 'HEAD', 'refs/remotes/origin/main', cwd=first.clone).stdout.strip()
    assert git('log', '--format=%an', f'{base}..HEAD', cwd=first.clone).stdout.split() == ['usage-clock'] * 3
    first.cfg['sync']['mode'] = 'on'
    assert first.push(now=NOW + 600) == 0
    assert first.state().last_error is None and remote.head() == first.head()
    pushed = [sync_format.decode('request', line)['k'] for line in
              remote.show(f'{first.own}/requests/2026-10/aaaaaaaa-1.jsonl').splitlines()]
    assert len(pushed) == 3 and f'{second.own}/status.json' in remote.files()
    assert git('log', '--merges', '--oneline', 'refs/heads/main', cwd=remote.bare).stdout == ''


def test_after_a_failed_push_and_a_push_of_another_machine_the_next_run_succeeds(tmp_path, remote):
    first, second = Device(tmp_path / 'a'), Device(tmp_path / 'b')
    first.request()
    lift = refuse_pushes(remote)
    assert first.push() == 1 and first.failure()['category'] == 'rejected'   # its commit stays local
    lift()
    second.request()
    assert second.push(now=NOW + 30) == 0
    first.request()
    assert first.push(now=NOW + 60) == 0
    assert first.state().last_error is None and remote.head() == first.head()
    assert len(remote.show(f'{first.own}/requests/2026-10/aaaaaaaa-1.jsonl').splitlines()) == 2
    assert f'{second.own}/status.json' in remote.files()


def test_the_first_push_into_an_empty_remote(tmp_path):
    bare = tmp_path / 'remotes' / 'owner' / 'name.git'
    bare.parent.mkdir(parents=True)
    git('init', '--bare', '--quiet', '--initial-branch=main', str(bare))
    device = Device(tmp_path / 'a')
    device.request()
    assert device.push() == 0
    files = git('ls-tree', '-r', '--name-only', 'refs/heads/main', cwd=bare).stdout.split()
    assert files and all(path.startswith(device.own + '/') for path in files)


# --- The size limit (sync.max_clone_mb) -----------------------------------------------------------------------------

MB = 1024 * 1024
TOO_LARGE = ('internal', 'the sync repository is larger than sync.max_clone_mb (1 MB): nothing was checked out; '
                         'raise the limit if this size is expected')


def base_size(remote):
    """The bytes of the first commit of the remote (its README, as this system wrote it)."""
    return int(git('cat-file', '-s', 'refs/heads/main:README.md', cwd=remote.bare).stdout)


def limited(device, mb=1):
    """A run of `device` with sync.max_clone_mb `mb` and its clone, opened."""
    device.cfg['sync']['max_clone_mb'] = mb
    run = run_of(device)
    return run, _sync_git.open_clone(run)


def big_file(device):
    return os.path.exists(os.path.join(device.clone, 'big.bin'))


def ls_tree_fails(monkeypatch):
    """scan_command of ls-tree ends with exit 128 and no output; the calls are recorded."""
    calls = []

    def scan_command(argv, deadline, take, env=None, cwd=None):
        calls.append(argv)
        return 128
    monkeypatch.setattr(_sync_core, 'scan_command', scan_command)
    return calls


@pytest.mark.parametrize('relation', ['behind', 'apart'])
def test_a_remote_over_the_limit_is_neither_fast_forwarded_nor_rebased(device, remote, relation):
    run, clone = limited(device)
    if relation == 'apart':
        git('commit', '--quiet', '--allow-empty', '-m', 'local', cwd=device.clone)
    remote.foreign_commit('big.bin', b'x' * (2 * MB))
    head = device.head()
    assert failure_of(_sync_git.update_clone, run, clone) == TOO_LARGE
    assert device.head() == head and device.status() == '' and not big_file(device)
    assert git('log', '--format=%s', '-1', cwd=device.clone).stdout.strip() == (
        'local' if relation == 'apart' else 'start')


def test_a_remote_exactly_at_the_limit_is_taken_one_byte_more_is_not(device, remote):
    run, clone = limited(device)
    remote.foreign_commit('big.bin', b'x' * (MB - base_size(remote)))
    assert _sync_git.update_clone(run, clone) == 'fast_forwarded' and device.head() == remote.head()
    remote.foreign_commit('big.bin', b'x' * (MB - base_size(remote) + 1))
    head = device.head()
    assert failure_of(_sync_git.update_clone, run, clone) == TOO_LARGE
    assert device.head() == head and os.path.getsize(os.path.join(device.clone, 'big.bin')) == MB - base_size(remote)


def test_the_limit_counts_megabytes_of_1024_kib(device, remote):
    run, clone = limited(device, mb=2)
    remote.foreign_commit('big.bin', b'x' * (2 * MB - base_size(remote)))
    assert _sync_git.update_clone(run, clone) == 'fast_forwarded'


def test_a_clone_over_the_limit_that_is_up_to_date_or_ahead_is_not_checked(device, remote, monkeypatch):
    remote.foreign_commit('big.bin', b'x' * (2 * MB))
    run, clone = limited(device, mb=3)
    assert _sync_git.update_clone(run, clone) == 'fast_forwarded'
    run, clone = limited(device)
    calls = ls_tree_fails(monkeypatch)
    assert _sync_git.update_clone(run, clone) == 'up_to_date'                 # same
    git('commit', '--quiet', '--allow-empty', '-m', 'local', cwd=device.clone)
    assert _sync_git.update_clone(run, clone) == 'up_to_date'                 # ahead
    assert calls == []


def test_a_size_that_cannot_be_read_is_internal_and_nothing_is_checked_out(device, remote, monkeypatch):
    run, clone = limited(device, mb=1024)
    remote.foreign_commit('README.md', 'news\n')
    head = device.head()
    calls = ls_tree_fails(monkeypatch)
    assert failure_of(_sync_git.update_clone, run, clone) == (
        'internal', 'the size of the sync repository could not be read: nothing was checked out')
    assert device.head() == head and len(calls) == 1


def plumbing(remote, data, *args):
    """git `args` in the bare repository with `data` on stdin; its output."""
    done = subprocess.run(['git', *args], input=data, cwd=remote.bare, capture_output=True,
                          env={**os.environ, **TESTER, 'LC_ALL': 'C'})
    assert done.returncode == 0, done.stderr
    return done.stdout.decode().strip()


@pytest.mark.parametrize('chunk', [65536, 7])
def test_names_with_spaces_tabs_and_line_breaks_are_counted_and_submodules_skipped(device, remote, monkeypatch,
                                                                                   chunk):
    """The tree is made without a work tree (Windows allows no line break in a file name): blobs whose names would
    break a reading by lines, and a submodule, whose size git gives as `-` (it counts as an entry). With pieces of 7
    bytes every entry is cut in several."""
    monkeypatch.setattr(_sync_core, 'SCAN_CHUNK_BYTES', chunk)
    sizes = {'a b.bin': 300_000, 'c\nd.bin': 400_000, 'e\tf.bin': MB - base_size(remote) - 700_000}
    readme = git('rev-parse', 'refs/heads/main:README.md', cwd=remote.bare).stdout.strip()
    entries = [f'100644 blob {readme}\tREADME.md', f'160000 commit {remote.head()}\tsub']
    for name, size in sizes.items():
        entries.append(f'100644 blob {plumbing(remote, b"x" * size, "hash-object", "-w", "--stdin")}\t{name}')

    def push_tree(extra):
        tree = plumbing(remote, ('\0'.join(entries + extra) + '\0').encode(), 'mktree', '-z')
        commit = plumbing(remote, b'names', 'commit-tree', tree, '-p', 'refs/heads/main')
        git('update-ref', 'refs/heads/main', commit, cwd=remote.bare)
    push_tree([])
    run, clone = limited(device)
    _sync_git._fetch(clone)
    _sync_git._check_size(clone)                                    # exactly the limit
    monkeypatch.setattr(_sync_git, 'MAX_CLONE_FILES', 4)
    assert failure_of(_sync_git._check_size, clone) == too_many(4)  # five entries with the submodule
    monkeypatch.setattr(_sync_git, 'MAX_CLONE_FILES', 5)
    empty, one = (plumbing(remote, data, 'hash-object', '-w', '--stdin') for data in (b'', b'x'))
    push_tree([f'100644 blob {empty}\tg\nh', f'100644 blob {one}\ti j'])
    _sync_git._fetch(clone)
    monkeypatch.setattr(_sync_git, 'MAX_CLONE_FILES', 7)
    assert failure_of(_sync_git._check_size, clone) == TOO_LARGE   # one byte more


def too_many(files):
    return 'internal', f'the sync repository has more than {files} files: nothing was checked out'


def git_bomb(remote, levels, width=10):
    """`width ** levels` empty files of `levels + 1` objects: every tree names the one below `width` times."""
    name, mode, kind = plumbing(remote, b'', 'hash-object', '-w', '--stdin'), '100644', 'blob'
    for _ in range(levels):
        listing = ''.join(f'{mode} {kind} {name}\t{kind[0]}{number}\0' for number in range(width))
        name, mode, kind = plumbing(remote, listing.encode(), 'mktree', '-z'), '040000', 'tree'
    commit = plumbing(remote, b'bomb', 'commit-tree', name, '-p', 'refs/heads/main')
    git('update-ref', 'refs/heads/main', commit, cwd=remote.bare)


def counted_reads(monkeypatch):
    """The sizes of the pieces scan_command hands on."""
    pieces, real = [], _sync_core.scan_command

    def scan_command(argv, deadline, take, env=None, cwd=None):
        return real(argv, deadline, lambda chunk: pieces.append(len(chunk)) or take(chunk), env, cwd)
    monkeypatch.setattr(_sync_core, 'scan_command', scan_command)
    return pieces


def test_a_git_bomb_of_a_million_empty_files_is_stopped_early(device, remote, monkeypatch):
    """8 objects, 10**6 empty files, size 0: the number of entries stops it, after a tenth of the listing (about
    79 bytes an entry: 79 MB in all)."""
    git_bomb(remote, 6)
    run, clone = limited(device)
    pieces = counted_reads(monkeypatch)
    head = device.head()
    assert failure_of(_sync_git.update_clone, run, clone) == too_many(100_000)
    assert device.head() == head and device.status() == ''
    assert sum(pieces) < 16 * MB


def test_exactly_max_clone_files_entries_are_taken_one_more_is_not(device, remote, monkeypatch):
    monkeypatch.setattr(_sync_git, 'MAX_CLONE_FILES', 3)
    run, clone = limited(device)
    remote.foreign_commit('a.txt', 'a\n')
    remote.foreign_commit('b.txt', 'b\n')                            # with README.md: three
    assert _sync_git.update_clone(run, clone) == 'fast_forwarded'
    remote.foreign_commit('c.txt', 'c\n')
    head = device.head()
    assert failure_of(_sync_git.update_clone, run, clone) == too_many(3)
    assert device.head() == head and not os.path.exists(os.path.join(device.clone, 'c.txt'))


def test_a_size_over_the_limit_stops_the_listing_where_it_is(device, remote, monkeypatch):
    monkeypatch.setattr(_sync_core, 'SCAN_CHUNK_BYTES', 64)
    big = plumbing(remote, b'x' * (2 * MB), 'hash-object', '-w', '--stdin')
    empty = plumbing(remote, b'', 'hash-object', '-w', '--stdin')
    listing = f'100644 blob {big}\ta.bin\0' + ''.join(f'100644 blob {empty}\tf{number:04}\0' for number in range(2000))
    commit = plumbing(remote, b'big first', 'commit-tree', plumbing(remote, listing.encode(), 'mktree', '-z'),
                      '-p', 'refs/heads/main')
    git('update-ref', 'refs/heads/main', commit, cwd=remote.bare)
    run, clone = limited(device)
    pieces = counted_reads(monkeypatch)
    assert failure_of(_sync_git.update_clone, run, clone) == TOO_LARGE
    whole = len(git('ls-tree', '-r', '-l', '-z', 'refs/heads/main', cwd=remote.bare).stdout)
    assert sum(pieces) < whole // 10


def fetched_tree(remote, clone, tree):
    """The remote gets a commit of `tree` on top of main, and the clone fetches it."""
    commit = plumbing(remote, b'tree', 'commit-tree', tree, '-p', 'refs/heads/main')
    git('update-ref', 'refs/heads/main', commit, cwd=remote.bare)
    _sync_git._fetch(clone)


LONG = ('internal', 'the sync repository has an entry of more than 200 bytes: nothing was checked out')


def test_an_entry_longer_than_max_entry_bytes_stops_the_listing(device, remote, monkeypatch):
    """An entry of ls-tree -l: `100644 blob <40 hex> <size in 7>\t<name>`, 61 bytes and the name. A name of 100 000
    bytes is not read to its end: memory stays small however long the entry."""
    monkeypatch.setattr(_sync_git, 'MAX_ENTRY_BYTES', 200)
    blob = plumbing(remote, b'x', 'hash-object', '-w', '--stdin')
    run, clone = limited(device)

    def fetched(name):
        fetched_tree(remote, clone, plumbing(remote, f'100644 blob {blob}\t{name}\0'.encode(), 'mktree', '-z'))
    fetched('n' * 139)
    _sync_git._check_size(clone)                                    # 200 bytes
    monkeypatch.setattr(_sync_core, 'SCAN_CHUNK_BYTES', 200)
    _sync_git._check_size(clone)                                    # the 200 bytes without their \0 as the rest
    monkeypatch.setattr(_sync_core, 'SCAN_CHUNK_BYTES', 65536)
    fetched('n' * 140)
    assert failure_of(_sync_git._check_size, clone) == LONG
    monkeypatch.setattr(_sync_core, 'SCAN_CHUNK_BYTES', 64)
    # fsck refuses a name above 4096 bytes, not a path of many: 100 parts of 1000 bytes
    deep = plumbing(remote, f'100644 blob {blob}\t{"n" * 999}\0'.encode(), 'mktree', '-z')
    for _ in range(99):
        deep = plumbing(remote, f'040000 tree {deep}\t{"n" * 999}\0'.encode(), 'mktree', '-z')
    fetched_tree(remote, clone, deep)
    pieces = counted_reads(monkeypatch)
    assert failure_of(_sync_git._check_size, clone) == LONG and sum(pieces) <= 200 + 2 * 64


def test_the_deadline_ends_the_listing_and_its_process(device, remote, monkeypatch):
    """The listing is read slowly (the bomb is packed in the clone: git lists it in a fifth of a second): the timer
    ends git at the deadline."""
    git_bomb(remote, 6)
    monkeypatch.setattr(_sync_git, 'MAX_CLONE_FILES', 10 ** 9)
    run, clone = limited(device)
    _sync_git._fetch(clone)
    real_take = _sync_git._Tally.take

    def slow_take(self, chunk):
        time.sleep(0.05)
        return real_take(self, chunk)
    monkeypatch.setattr(_sync_git._Tally, 'take', slow_take)
    started, real = [], subprocess.Popen

    def popen(*args, **kwargs):
        started.append(real(*args, **kwargs))
        return started[-1]
    monkeypatch.setattr(subprocess, 'Popen', popen)
    clone = dataclasses.replace(clone, deadline=_sync_core.Deadline(0.3))
    assert failure_of(_sync_git._check_size, clone) == ('timeout', 'git did not finish in time')
    assert started[0].poll() is not None and started[0].stdout.closed


def test_a_push_over_the_limit_is_internal_and_exports_nothing(device, remote):
    device.cfg['sync']['max_clone_mb'] = 1
    remote.foreign_commit('big.bin', b'x' * (2 * MB))
    device.request()
    head = device.head()
    assert device.push() == 1
    assert (device.failure()['category'], device.failure()['reason']) == TOO_LARGE
    assert device.head() == head and device.status() == '' and not big_file(device)


def test_the_rebase_after_the_commit_is_limited_too(device, remote):
    device.request()
    assert device.push() == 0
    device.cfg['sync']['mode'] = 'shadow'
    device.request()
    assert device.push(now=NOW + 60) == 0
    remote.foreign_commit('big.bin', b'x' * (2 * MB))
    foreign = remote.head()
    with open(os.path.join(device.clone, *device.own.split('/'), 'status.json'), 'w', encoding='utf-8') as f:
        f.write('{"v":1,"dirty":true}\n')                            # dirty: step 4 leaves the rebase to step 7b
    device.cfg['sync'].update(mode='on', max_clone_mb=1)
    device.request()
    assert device.push(now=NOW + 120) == 1
    assert (device.failure()['category'], device.failure()['reason']) == TOO_LARGE
    base = git('merge-base', 'HEAD', foreign, cwd=device.clone).stdout.strip()
    assert git('log', '--format=%an', f'{base}..HEAD', cwd=device.clone).stdout.split() == ['usage-clock'] * 2
    assert not big_file(device) and len(device.worktree_requests()) == 3 and remote.head() == foreign


def test_the_rebase_of_a_second_push_attempt_is_limited_too(tmp_path, remote, monkeypatch):
    device = Device(tmp_path / 'a')
    device.cfg['sync']['max_clone_mb'] = 1
    device.request()
    pushed, real_export = [], _sync_export.export

    def export_after_another_pushed(*args, **kwargs):
        if not pushed:
            remote.foreign_commit('big.bin', b'x' * (2 * MB))       # after the fetch of step 4
            pushed.append(remote.head())
        return real_export(*args, **kwargs)
    monkeypatch.setattr(_sync_export, 'export', export_after_another_pushed)
    assert device.push() == 1
    assert (device.failure()['category'], device.failure()['reason']) == TOO_LARGE
    assert not big_file(device) and remote.head() == pushed[0] and len(device.worktree_requests()) == 1


def test_a_fast_forward_git_refuses_is_a_conflict_and_keeps_the_change(device, remote):
    device.request()
    assert device.push() == 0
    status = os.path.join(device.clone, *device.own.split('/'), 'status.json')
    with open(status, 'w', encoding='utf-8') as f:
        f.write('local change\n')                                 # in the own folder: never put back
    remote.foreign_commit(f'{device.own}/status.json', 'remote change\n')
    device.request()
    assert device.push(now=NOW + 60) == 1
    assert (device.failure()['category'], device.failure()['reason']) == (
        'conflict', 'the sync clone cannot be fast-forwarded to the remote')
    with open(status, encoding='utf-8') as f:
        assert f.read() == 'local change\n'


def test_a_failing_merge_base_is_internal(device, remote, monkeypatch):
    run = run_of(device)
    clone = _sync_git.open_clone(run)
    git('commit', '--quiet', '--allow-empty', '-m', 'local', cwd=device.clone)
    scripted(monkeypatch, {'merge-base': (128, '', 'fatal: bad object')})
    assert failure_of(_sync_git.update_clone, run, clone) == ('internal', 'git merge-base failed in the sync clone')


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


def test_a_log_of_the_outgoing_commits_that_fails_is_internal(device, remote, monkeypatch):
    device.request()
    scripted(monkeypatch, {'log': (128, '', 'fatal: no')})
    assert device.push() == 1 and device.failure()['reason'] == 'git log failed in the sync clone'


def test_a_path_one_commit_adds_and_a_later_one_removes_never_leaves(device, remote):
    """Each outgoing commit is checked, not their sum: the sum of these two commits is nothing outside."""
    device.request()
    assert device.push() == 0
    with open(os.path.join(device.clone, 'secret.txt'), 'w', encoding='utf-8') as f:
        f.write('secret\n')
    git('add', 'secret.txt', cwd=device.clone)
    git('commit', '--quiet', '-m', 'out', cwd=device.clone)
    git('rm', '--quiet', 'secret.txt', cwd=device.clone)
    git('commit', '--quiet', '-m', 'gone again', cwd=device.clone)
    before = remote.head()
    device.request()
    assert device.push(now=NOW + 60) == 1
    assert device.failure()['reason'] == 'a changed path outside the own machine folder: nothing is committed'
    assert remote.head() == before
    assert git('log', '--all', '--format=%s', '--', 'secret.txt', cwd=remote.bare).stdout == ''


def test_a_merge_commit_is_never_pushed(device, remote):
    device.request()
    assert device.push() == 0
    git('checkout', '--quiet', '-b', 'side', cwd=device.clone)
    git('commit', '--quiet', '--allow-empty', '-m', 'side', cwd=device.clone)
    git('checkout', '--quiet', 'main', cwd=device.clone)
    git('commit', '--quiet', '--allow-empty', '-m', 'main', cwd=device.clone)
    git('merge', '--quiet', '--no-ff', '--no-edit', 'side', cwd=device.clone)
    before = remote.head()
    device.request()
    assert device.push(now=NOW + 60) == 1
    assert device.failure()['reason'] == 'a merge commit would be pushed: nothing is pushed'
    assert remote.head() == before


def test_variables_of_the_caller_for_index_attributes_and_dates_do_not_reach_the_clone(device, remote, tmp_path,
                                                                                         monkeypatch):
    device.request()
    index = tmp_path / 'foreign-index'
    monkeypatch.setenv('GIT_INDEX_FILE', str(index))
    monkeypatch.setenv('GIT_AUTHOR_DATE', '@0 +0000')
    monkeypatch.setenv('GIT_COMMITTER_DATE', '@0 +0000')
    monkeypatch.setenv('GIT_ATTR_SOURCE', 'no-such-tree')     # git would stop with a fatal error
    assert device.push() == 0
    assert not index.exists()
    dates = git('log', '-1', '--format=%at %ct', 'refs/heads/main', cwd=remote.bare).stdout.split()
    assert all(int(date) > 1_000_000_000 for date in dates)


@pytest.mark.posix_only
def test_a_gitattributes_of_another_machine_runs_no_filter_of_the_user(device, remote, git_alone, tmp_path):
    marker = tmp_path / 'filter-ran'
    for kind in ('clean', 'smudge'):
        script = tmp_path / f'{kind}.sh'
        script.write_text(f'#!/bin/sh\necho "$PWD" >> "{marker.as_posix()}"\ncat\n', encoding='utf-8')
        os.chmod(script, 0o755)
    with open(git_alone, 'a', encoding='utf-8') as f:
        f.write(f'[filter "probe"]\n\tclean = {(tmp_path / "clean.sh").as_posix()}\n'
                f'\tsmudge = {(tmp_path / "smudge.sh").as_posix()}\n')
    remote.foreign_commit('.gitattributes', '* filter=probe\n')
    device.request()
    assert device.push() == 0
    # The other machine of the test shares the test config: its own add runs the filter (so the filter works); the
    # sync in the clone runs it nowhere.
    ran = set(marker.read_text(encoding='utf-8').splitlines())
    assert ran == {str(remote.other)}


def test_a_gitattributes_of_another_machine_changes_no_own_file(device, remote):
    remote.foreign_commit('.gitattributes', 'machines/** working-tree-encoding=UTF-16 eol=crlf text\n')
    device.request()
    assert device.push() == 0
    path = f'{device.own}/requests/2026-10/aaaaaaaa-1.jsonl'
    stored = git('cat-file', 'blob', f'refs/heads/main:{path}', cwd=remote.bare).stdout
    with open(os.path.join(device.clone, *path.split('/')), encoding='utf-8', newline='') as f:
        assert f.read() == stored and '\r' not in stored
    sync_format.decode('request', stored.splitlines()[0])


def test_a_repository_put_into_the_sync_folder_does_not_steer_the_probe(tmp_path, monkeypatch):
    """A `.git` in <log_dir>/sync whose config rewrites the probe URL to a readable repository: without GIT_DIR on
    the null device the probe would find it and call the repository public."""
    readable = tmp_path / 'readable.git'
    git('init', '--bare', '--quiet', str(readable))
    sync_dir = tmp_path / 'log' / 'sync'
    sync_dir.mkdir(parents=True)
    git('init', '--quiet', str(sync_dir))
    missing = (tmp_path / 'missing').as_posix() + '/{}.git'
    with open(sync_dir / '.git' / 'config', 'a', encoding='utf-8') as f:
        f.write(f'[url "{readable.as_posix()}"]\n\tinsteadOf = {missing.format(REPO)}\n')
    monkeypatch.setattr(_sync_git, 'PROBE_URL', missing)
    assert _sync_git.check_visibility(REPO, str(sync_dir)) == 'unclear'


def test_an_entry_with_a_second_link_is_refused(tmp_path):
    folder = tmp_path / OWN / 'days'
    folder.mkdir(parents=True)
    (folder / '2026-10.jsonl').write_text('{}\n', encoding='utf-8')
    os.link(folder / '2026-10.jsonl', tmp_path / 'outside.jsonl')
    assert failure_of(_sync_git.check_entry, 'A', '100644', f'{OWN}/days/2026-10.jsonl', OWN, lambda p: True,
                      str(tmp_path)) == (
        'internal', 'a changed path that is no regular file of the layout: nothing is committed')


# --- Healing what an ended run left -----------------------------------------------------------------------------------

def age(path, seconds):
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


@pytest.mark.parametrize('lock', ['index.lock', 'refs/remotes/origin/main.lock', 'packed-refs.lock', 'HEAD.lock'])
def test_a_left_over_git_lock_is_removed_and_the_run_goes_on(device, remote, lock):
    device.request()
    assert device.push() == 0
    path = os.path.join(device.clone, '.git', *lock.split('/'))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('')
    age(path, _sync_git.STALE_LOCK_S + 5)
    device.request()
    assert device.push(now=NOW + 60) == 0
    assert not os.path.exists(path) and remote.head() == device.head()


@pytest.mark.parametrize('seconds', [5, -3600])
def test_a_young_git_lock_is_someone_elses_and_ends_the_run(device, remote, seconds):
    path = os.path.join(device.clone, '.git', 'index.lock')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('')
    age(path, seconds)                                        # just made, or from the future
    device.request()
    assert device.push() == 1
    assert device.failure()['reason'] == 'the sync clone is in use by another git process (a young lock file)'
    assert os.path.exists(path)


def pinned(device):
    """The attributes open_clone writes, there already: a test that breaks writing then reaches its own step."""
    os.makedirs(os.path.join(device.clone, '.git', 'info'), exist_ok=True)
    with open(os.path.join(device.clone, '.git', 'info', 'attributes'), 'wb') as f:
        f.write(_sync_git.INFO_ATTRIBUTES.encode())


def test_a_lock_that_cannot_be_removed_is_internal(device, remote, monkeypatch):
    pinned(device)
    path = os.path.join(device.clone, '.git', 'index.lock')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('')
    age(path, _sync_git.STALE_LOCK_S + 5)

    def unlink(target, *args, **kwargs):
        raise PermissionError(13, 'denied')
    monkeypatch.setattr(os, 'unlink', unlink)
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'a left-over file of the sync clone cannot be removed (PermissionError)')


def test_an_interrupted_rebase_is_aborted_and_the_run_goes_on(device, remote):
    device.request()
    assert device.push() == 0
    device.cfg['sync']['mode'] = 'shadow'
    device.request()
    assert device.push(now=NOW + 60) == 0                         # an own commit that is not pushed
    remote.foreign_commit('README.md', 'news\n')
    git('fetch', '--quiet', cwd=device.clone)
    stopped = git('rebase', '--exec', 'false', 'origin/main', cwd=device.clone, check=False)
    assert stopped.returncode != 0 and os.path.isdir(os.path.join(device.clone, '.git', 'rebase-merge'))
    device.cfg['sync']['mode'] = 'on'
    device.request()
    assert device.push(now=NOW + 120) == 0                        # as if the run had been ended mid-rebase
    assert remote.head() == device.head() and len(device.worktree_requests()) == 3
    assert not os.path.isdir(os.path.join(device.clone, '.git', 'rebase-merge'))


def test_an_interrupted_rebase_with_a_conflict_ends_as_conflict_not_stuck(device, remote):
    device.request()
    assert device.push() == 0
    status = os.path.join(device.clone, *device.own.split('/'), 'status.json')
    with open(status, 'a', encoding='utf-8') as f:
        f.write('x\n')
    git('commit', '--quiet', '-am', 'own', cwd=device.clone)
    own = device.head()
    remote.foreign_commit(f'{device.own}/status.json', 'foreign\n')
    git('fetch', '--quiet', cwd=device.clone)
    assert git('rebase', 'origin/main', cwd=device.clone, check=False).returncode != 0
    device.request()
    for minutes in (1, 2):
        assert device.push(now=NOW + 60 * minutes) == 1
        assert device.failure()['category'] == 'conflict'
    git('merge-base', '--is-ancestor', own, 'HEAD', cwd=device.clone)              # the own commit stays
    assert not os.path.isdir(os.path.join(device.clone, '.git', 'rebase-merge'))


def test_an_interrupted_rebase_that_cannot_be_aborted_is_internal(device, monkeypatch):
    os.mkdir(os.path.join(device.clone, '.git', 'rebase-merge'))
    scripted(monkeypatch, {'rebase --abort': (1, '', 'error')})
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'an interrupted rebase in the sync clone could not be aborted')


# --- The branch that is pushed ----------------------------------------------------------------------------------------

def test_only_the_default_branch_of_the_remote_is_pushed(device, remote):
    git('checkout', '--quiet', '-b', 'other', cwd=device.clone)
    device.request()
    assert device.push() == 1
    assert device.failure()['reason'] == (
        'the sync clone has another branch checked out than the default branch of the remote')
    assert git('branch', '--list', 'other', cwd=remote.bare).stdout == ''


def test_a_remote_whose_default_branch_is_not_main(tmp_path):
    bare = tmp_path / 'remotes' / 'owner' / 'name.git'
    bare.parent.mkdir(parents=True)
    git('init', '--bare', '--quiet', '--initial-branch=master', str(bare))
    seed = tmp_path / 'seed'
    git('clone', '--quiet', str(bare), str(seed))
    git('commit', '--quiet', '--allow-empty', '-m', 'start', cwd=seed)
    git('push', '--quiet', 'origin', 'HEAD:refs/heads/master', cwd=seed)
    device = Device(tmp_path / 'a')
    for minutes in (0, 1):                                    # the fetch with --prune keeps origin/HEAD
        device.request()
        assert device.push(now=NOW + 60 * minutes) == 0
    assert git('rev-parse', 'refs/heads/master', cwd=bare).stdout.strip() == device.head()
    assert git('branch', '--list', 'main', cwd=bare).stdout == ''


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


# --- Security review: healing, fetch, environment ---------------------------------------------------------------------

def test_before_the_first_export_every_path_counts_as_foreign_and_is_put_back(tmp_path, remote):
    device = Device(tmp_path / 'a')
    change_outside(device.clone, 'untracked')
    run = run_of(device)
    assert not os.path.exists(device.log / 'sync' / 'machine_id')
    _sync_git.open_clone(run)
    assert device.status() == ''


def test_an_unknown_folder_outside_the_own_folder_is_internal(device, remote):
    nested = os.path.join(device.clone, 'nested')
    git('init', '--quiet', nested)
    git('commit', '--quiet', '--allow-empty', '-m', 'x', cwd=nested)
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'an unknown folder or path outside the own machine folder of the sync clone')


def test_a_foreign_file_that_cannot_be_removed_or_put_back_is_internal(device, remote, monkeypatch):
    change_outside(device.clone, 'untracked')
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if os.fspath(path).endswith('notes.txt'):
            raise PermissionError(13, 'denied')
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, 'unlink', unlink)
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'a file of another machine cannot be removed (PermissionError)')
    monkeypatch.setattr(os, 'unlink', real_unlink)
    change_outside(device.clone, 'tracked')
    scripted(monkeypatch, {'--literal-pathspecs checkout': (128, '', 'fatal: no')})
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'the files of other machines in the sync clone cannot be put back')


@pytest.mark.posix_only
def test_the_path_list_is_never_written_through_a_link(device, remote, tmp_path):
    target = tmp_path / 'elsewhere'
    target.write_text('kept\n', encoding='utf-8')
    os.symlink(target, os.path.join(device.clone, '.git', 'sync-paths'))
    change_outside(device.clone, 'tracked')
    _sync_git.open_clone(run_of(device))
    assert target.read_text(encoding='utf-8') == 'kept\n' and device.status() == ''


def test_a_path_list_that_cannot_be_written_is_internal(device, monkeypatch):
    pinned(device)

    def write_private(*args):
        raise OSError('full disk')
    monkeypatch.setattr(_context_state, 'write_private', write_private)
    change_outside(device.clone, 'tracked')
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'the files of other machines in the sync clone cannot be put back')


def test_a_staged_foreign_file_already_gone_from_the_work_tree_leaves_the_index(device):
    change_outside(device.clone, 'staged')
    os.replace(os.path.join(device.clone, 'staged.txt'), device.log.parent / 'staged.txt')
    _sync_git.open_clone(run_of(device))
    assert device.status() == ''


def test_left_over_temporary_objects_of_an_ended_fetch_are_removed(device, remote):
    objects = os.path.join(device.clone, '.git', 'objects')
    os.makedirs(os.path.join(objects, 'ab'), exist_ok=True)
    old = [os.path.join(objects, 'pack', 'tmp_pack_x1'), os.path.join(objects, 'pack', 'tmp_idx_x2'),
           os.path.join(objects, 'ab', 'tmp_obj_x3')]
    young = os.path.join(objects, 'pack', 'tmp_pack_young')
    folder = os.path.join(objects, 'pack', 'tmp_pack_folder')
    os.mkdir(folder)
    for path in old + [young]:
        with open(path, 'wb') as f:
            f.write(b'\0' * 1024)
    for path in old + [folder]:
        age(path, _sync_git.STALE_LOCK_S + 5)
    device.request()
    assert device.push() == 0
    assert not any(os.path.exists(path) for path in old)
    assert os.path.exists(young) and os.path.isdir(folder)           # perhaps a fetch of another git; no file


@pytest.mark.posix_only
def test_a_temporary_object_that_is_a_link_is_left_alone(device, remote, tmp_path):
    target = tmp_path / 'target'
    target.write_text('kept\n', encoding='utf-8')
    link = os.path.join(device.clone, '.git', 'objects', 'pack', 'tmp_pack_link')
    os.symlink(target, link)
    os.utime(link, (time.time() - 3600, time.time() - 3600), follow_symlinks=False)
    _sync_git.open_clone(run_of(device))
    assert os.path.islink(link) and target.read_text(encoding='utf-8') == 'kept\n'


def test_only_the_default_branch_is_fetched(device, remote):
    git('push', '--quiet', 'origin', 'HEAD:refs/heads/junk', cwd=remote.other)
    device.request()
    assert device.push() == 0
    refs = git('for-each-ref', '--format=%(refname)', 'refs/remotes/', cwd=device.clone).stdout.split()
    assert 'refs/remotes/origin/junk' not in refs and 'refs/remotes/origin/main' in refs


def test_a_deleted_remote_branch_drops_the_tracking_branch_and_sends_no_foreign_history(device, remote):
    device.request()
    assert device.push() == 0
    git('update-ref', '-d', 'refs/heads/main', cwd=remote.bare)
    device.request()
    assert device.push(now=NOW + 60) == 1        # its history holds the commit of another machine: fail-closed
    assert device.failure()['reason'] == 'a changed path outside the own machine folder: nothing is committed'
    assert git('rev-parse', '--verify', '--quiet', 'refs/remotes/origin/main', cwd=device.clone,
               check=False).returncode != 0
    assert git('for-each-ref', 'refs/heads/', cwd=remote.bare).stdout == ''


def test_a_tracking_branch_that_cannot_be_dropped_is_internal(tmp_path, monkeypatch):
    bare = tmp_path / 'remotes' / 'owner' / 'name.git'
    bare.parent.mkdir(parents=True)
    git('init', '--bare', '--quiet', '--initial-branch=main', str(bare))
    device = Device(tmp_path / 'a')
    device.request()
    scripted(monkeypatch, {'update-ref': (128, '', 'fatal: no')})
    assert device.push() == 1
    assert device.failure()['reason'] == 'the remote-tracking branch of the sync clone cannot be removed'


def test_a_rebase_state_that_is_no_folder_is_internal_and_not_aborted(device):
    with open(os.path.join(device.clone, '.git', 'rebase-apply'), 'w', encoding='utf-8') as f:
        f.write('x\n')
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'the rebase state of the sync clone is no folder: nothing is aborted')


@pytest.mark.posix_only
def test_a_rebase_state_that_is_a_link_never_empties_its_target(device, tmp_path):
    victim = tmp_path / 'victim'
    (victim / 'inner').mkdir(parents=True)
    for name, text in (('head-name', 'refs/heads/main\n'), ('orig-head', device.head() + '\n'),
                       ('onto', device.head() + '\n'), ('inner/keep', 'keep\n')):
        (victim / name).write_text(text, encoding='utf-8')
    os.symlink(victim, os.path.join(device.clone, '.git', 'rebase-merge'))
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'the rebase state of the sync clone is no folder: nothing is aborted')
    assert (victim / 'inner' / 'keep').read_text(encoding='utf-8') == 'keep\n'


def test_tls_proxy_and_config_variables_of_the_caller_do_not_reach_the_clone(device, remote, monkeypatch):
    recorded, real_run = [], _sync_core.run_command

    def run_command(argv, deadline, env=None, cwd=None):
        recorded.append((argv, dict(env or {})))
        return real_run(argv, deadline, env, cwd)
    monkeypatch.setattr(_sync_core, 'run_command', run_command)
    foreign = {'GIT_SSL_NO_VERIFY': '1', 'GIT_SSL_CAINFO': '/x/ca.pem', 'GIT_SSL_CAPATH': '/x',
               'GIT_PROXY_COMMAND': 'nc', 'GIT_CONFIG_PARAMETERS': "'core.hookspath'='/x'", 'GIT_CONFIG_COUNT': '1',
               'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': '/x'}
    for name, value in foreign.items():
        monkeypatch.setenv(name, value)
    device.request()
    assert device.push() == 0
    clone_calls = [(argv, env) for argv, env in recorded if env.get('GIT_DIR') == os.path.join(device.clone, '.git')]
    assert clone_calls and not any(set(foreign) & set(env) for _argv, env in clone_calls)
    assert all('http.sslVerify=true' in argv for argv, _env in clone_calls if 'config' not in argv[1:2])


def test_a_git_ssh_program_gets_no_ssh_options(device, monkeypatch):
    monkeypatch.setenv('GIT_SSH', 'plink')
    clone = _sync_git.open_clone(run_of(device))
    assert 'GIT_SSH_COMMAND' not in clone.env


def test_the_outgoing_log_takes_its_range_never_as_a_path(tmp_path):
    """With an empty remote the range is `HEAD`; a file named HEAD in the work tree must not make it ambiguous."""
    bare = tmp_path / 'remotes' / 'owner' / 'name.git'
    bare.parent.mkdir(parents=True)
    git('init', '--bare', '--quiet', '--initial-branch=main', str(bare))
    device = Device(tmp_path / 'a', mode='shadow')
    device.request()
    assert device.push() == 0                                   # an own commit, nothing on the remote
    clone = _sync_git.open_clone(run_of(device))
    with open(os.path.join(device.clone, 'HEAD'), 'w', encoding='utf-8') as f:
        f.write('x\n')
    _sync_git._check_outgoing(clone, device.own)


def test_git_settings_of_the_review_are_set():
    for setting in ('log.showSignature=false', 'fetch.writeCommitGraph=false', 'transfer.fsckObjects=true',
                    'http.sslVerify=true', f'attr.tree={_sync_git.EMPTY_TREE}', 'core.attributesFile='):
        assert setting in _sync_git.SETTINGS



# --- Removing a clone that failed (make_clone) -----------------------------------------------------------------------

def test_a_failed_clone_with_read_only_files_is_removed(tmp_path):
    """git makes its object files read-only, which Windows refuses to delete: the clean-up makes them writable."""
    folder = tmp_path / 'repo.tmp-x'
    (folder / '.git' / 'objects').mkdir(parents=True)
    pack = folder / '.git' / 'objects' / 'pack.idx'
    pack.write_bytes(b'x')
    os.chmod(pack, stat.S_IREAD)
    _sync_git._remove_tree(str(folder))
    assert not folder.exists()


def test_the_clean_up_passes_on_every_other_error(tmp_path, monkeypatch):
    """Only a read-only regular file is made writable; any other failure (a folder that cannot go, a link) is raised,
    nothing is changed through it."""
    folder = tmp_path / 'repo.tmp-x'
    (folder / 'sub').mkdir(parents=True)
    real = os.rmdir

    def rmdir(path, *args, **kwargs):
        if os.path.basename(path) == 'sub':
            raise PermissionError(13, 'denied', path)
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, 'rmdir', rmdir)
    chmods = []
    monkeypatch.setattr(os, 'chmod', lambda *args, **kwargs: chmods.append(args))
    with pytest.raises(PermissionError):
        _sync_git._remove_tree(str(folder))
    assert chmods == []


def test_a_clean_up_that_fails_keeps_the_error_of_the_clone(tmp_path, monkeypatch):
    """The reason the clone failed is what the installer has to tell (`Nothing was installed.`, the hint on credentials);
    a folder left over is named by --check."""
    directory = _sync_core.ensure_dir(str(tmp_path / 'log'))
    url = _sync_git.remote_url('no/such', 'https')
    expected = failure_of(_sync_git.make_clone, directory, url, 1024, _sync_core.Deadline(60))

    def busy(path):
        raise PermissionError(13, 'in use', path)
    monkeypatch.setattr(_sync_git, '_remove_tree', busy)
    assert failure_of(_sync_git.make_clone, directory, url, 1024, _sync_core.Deadline(60)) == expected


def test_a_rewritten_remote_is_refused_before_anything_is_checked_out(tmp_path, remote, git_alone, monkeypatch):
    """An insteadOf leads the clone elsewhere: refused right after `git clone`, before the size check and the checkout
    of what came from there."""
    other = tmp_path / 'remotes' / 'someone' / 'else.git'
    other.parent.mkdir(parents=True)
    git('clone', '--quiet', '--bare', _sync_git.remote_url(REPO, 'https'), str(other))
    with open(git_alone, 'a', encoding='utf-8') as f:
        f.write(f'[url "{other.as_posix()}"]\n\tinsteadOf = {_sync_git.remote_url(REPO, "https")}\n')
    sized = []
    monkeypatch.setattr(_sync_git, '_check_size', sized.append)
    directory = _sync_core.ensure_dir(str(tmp_path / 'log'))
    category, detail = failure_of(_sync_git.make_clone, directory, _sync_git.remote_url(REPO, 'https'), 1024,
                                  _sync_core.Deadline(60))
    assert category == 'internal' and 'the remote of the new clone is not the URL of sync.repo' in detail
    assert sized == [] and os.listdir(directory) == [_sync_git.NO_HOOKS_DIR]
