"""Tests for hooks/sync.py: the commands, the two session hooks and the honest placeholder of the pull (the push is
tested in test_sync_git.py)."""
import io
import json
import os
import subprocess
import sys
import time

import pytest

import _plat
import _sync_core
import sync
from conftest import HOOKS

SCRIPT = os.path.join(HOOKS, 'sync.py')


@pytest.fixture
def mode(tmp_path, monkeypatch):
    """Writes a config with this sync mode."""
    def write(value):
        path = tmp_path / 'config.json'
        path.write_text(json.dumps({'sync': {'mode': value}}), encoding='utf-8')
        monkeypatch.setenv('CCUC_CONFIG', str(path))
    return write


@pytest.fixture
def starts(monkeypatch):
    calls = []
    monkeypatch.setattr(_plat, 'start_detached', calls.append)
    return calls


def state_of(tmp_path):
    return _sync_core.read_state(str(tmp_path / 'log' / 'sync'))


def sync_lines(tmp_path):
    path = tmp_path / 'log' / 'log.jsonl'
    lines = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    return [r for r in lines if r.get('hook') == 'sync']


def test_a_push_without_a_sync_repo_fails_as_internal_and_backs_off_never_a_success(tmp_path, mode):
    mode('shadow')
    assert sync.main(['push']) == 1
    state = state_of(tmp_path)
    assert (state.last_error, state.backoff_level, state.last_success) == ('internal', 1, None)
    line, = sync_lines(tmp_path)
    assert (line['decision'], line['category'], line['reason']) == (
        'failure', 'internal', 'sync.repo is not set, run the installer with --sync-repo')


def test_without_the_git_work_a_pull_fails_as_internal(tmp_path, mode):
    mode('on')
    assert sync.main(['pull', '--background']) == 1
    line, = sync_lines(tmp_path)
    assert (line['action'], line['category'], line['reason']) == ('pull', 'internal', 'pull is not built yet')


def test_push_now_is_the_forced_run_and_pull_background_the_long_one(tmp_path, mode):
    mode('shadow')
    assert sync.main(['push']) == 1                                           # into a backoff
    runs = []
    assert sync.main(['push'], push_runner=runs.append) == 0 and runs == []   # waits for it
    assert sync.main(['push', '--now'], push_runner=runs.append) == 0 and len(runs) == 1
    assert state_of(tmp_path).backoff_level == 0
    assert sync.main(['pull', '--background'], pull_runner=runs.append) == 0
    assert sync.main(['pull'], pull_runner=runs.append) == 0
    assert runs[1].deadline.left() > 29 and runs[2].deadline.left() <= 5


def test_push_if_due_runs_only_when_due(tmp_path, mode):
    mode('shadow')
    runs = []
    assert sync.main(['push', '--if-due'], push_runner=runs.append) == 0 and len(runs) == 1
    assert sync.main(['push', '--if-due'], push_runner=runs.append) == 0 and len(runs) == 1   # next_due is ahead


def test_with_sync_off_every_command_does_nothing(tmp_path, starts):
    for argv in (['push', '--now'], ['pull'], ['session-start'], ['session-end']):
        assert sync.main(argv) == 0
    assert starts == [] and not (tmp_path / 'log').exists()


def test_the_session_hooks_read_their_input_and_start_detached(tmp_path, mode, starts, monkeypatch, capsys):
    mode('shadow')
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"hook_event_name": "SessionStart"}'))
    assert sync.main(['session-start']) == 0
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"hook_event_name": "SessionEnd"}'))
    assert sync.main(['session-end']) == 0
    assert starts == [_sync_core.command('pull', '--background'), _sync_core.command('push')]
    assert state_of(tmp_path).next_due is not None      # never pushed: SessionStart made it due now
    assert capsys.readouterr() == ('', '')


def test_a_failing_session_hook_still_exits_0_silently(mode, monkeypatch, capsys):
    mode('shadow')
    def boom(*_args):
        raise RuntimeError('a bug')
    monkeypatch.setattr(_sync_core, 'session_end', boom)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{}'))
    assert sync.main(['session-end']) == 0
    assert capsys.readouterr() == ('', '')


def test_a_sync_folder_that_cannot_be_used_ends_with_1_and_one_log_line_an_hour(tmp_path, mode):
    mode('shadow')
    (tmp_path / 'log').mkdir()
    (tmp_path / 'log' / 'sync').write_text('', encoding='utf-8')
    assert sync.main(['push', '--now']) == 1 and sync.main(['pull']) == 1
    line, = sync_lines(tmp_path)
    assert (line['decision'], line['reason']) == ('error', 'push: OSError')


@pytest.mark.parametrize('argv', [[], ['merge'], ['push', '--later'], ['pull', '--now'], ['push', '--now', '--if-due']])
def test_bad_arguments_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as stop:
        sync.main(argv)
    assert stop.value.code == 2


def test_the_script_runs_as_a_hook_and_as_a_command(tmp_path, mode):
    mode('shadow')
    # Inside a backoff the hook starts no real push in the background, which would race the manual run below. The
    # backoff ends within the longest wait of the core: a next_due further ahead counts as due (bounded).
    (tmp_path / 'log' / 'sync').mkdir(parents=True)
    _sync_core.write_state(str(tmp_path / 'log' / 'sync'),
                           _sync_core.State(backoff_level=1, next_due=time.time() + 3600, last_error='network'))
    done = subprocess.run([sys.executable, SCRIPT, 'session-end'], input='{"hook_event_name": "SessionEnd"}',
                          capture_output=True, text=True, timeout=60)
    assert (done.returncode, done.stdout, done.stderr) == (0, '', '')
    done = subprocess.run([sys.executable, SCRIPT, 'push', '--now'], capture_output=True, text=True, timeout=60)
    assert (done.returncode, done.stdout) == (1, '') and state_of(tmp_path).last_error == 'internal'
