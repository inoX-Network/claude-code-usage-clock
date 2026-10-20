"""Tests for hooks/_sync_core.py: state, due check, lock, backoff with jitter, timeouts, git environment, T1-T4.

Time and chance are handed in (`clock`, `rand`, `now`), so no test waits for a backoff. The only real waits are the
child processes of the lock and timeout tests.
"""
import copy
import dataclasses
import json
import os
import random
import socket
import stat
import statistics
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import _config
import _plat
import _sync_core
from _sync_core import State
from conftest import HOOKS

T0 = 1_768_400_000.0            # 2026-01-14 14:13:20 UTC, a whole second
INTERVAL_S = 30 * 60


def cfg(**sync):
    config = copy.deepcopy(_config.DEFAULTS)
    config['sync'].update({'mode': 'shadow', **sync})
    return config


def log_dir(tmp_path):
    return str(tmp_path / 'log')


def sync_dir(tmp_path):
    return tmp_path / 'log' / 'sync'


def put_state(tmp_path, state):
    os.makedirs(sync_dir(tmp_path), exist_ok=True)
    _sync_core.write_state(str(sync_dir(tmp_path)), state)


def put_raw(tmp_path, text):
    os.makedirs(sync_dir(tmp_path), exist_ok=True)
    (sync_dir(tmp_path) / 'state.json').write_text(text, encoding='utf-8')


def state_of(tmp_path):
    return _sync_core.read_state(str(sync_dir(tmp_path)))


def sync_lines(tmp_path, decision=None):
    path = tmp_path / 'log' / 'log.jsonl'
    lines = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    return [r for r in lines if r.get('hook') == 'sync' and decision in (None, r.get('decision'))]


def ok(run):
    pass


def failing(category):
    def runner(run):
        raise _sync_core.SyncError(category, f'{category} for the test')
    return runner


class Starts:
    """Stands in for _plat.start_detached: records the command lines."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)


def push(tmp_path, runner=ok, now=T0, forced=False, config=None, rand=lambda: 0.5, end=None, if_due=False):
    """One push at `now` (ending at `end`, default the same moment)."""
    times = iter([now, now if end is None else end])
    return _sync_core.push(log_dir(tmp_path), config or cfg(), runner, forced=forced, if_due=if_due,
                           clock=lambda: next(times), rand=rand)


# --- Criterion: the status line check uses no network and takes under 5 ms -----------------------------------------

def test_the_status_line_check_uses_no_network_and_its_median_is_under_5_ms(tmp_path, monkeypatch):
    touched = []      # collected, not raised: an exception could be swallowed by the code under test

    def no_network(*args, **_kwargs):
        touched.append(args)
        raise OSError('no network in this test')
    for name in ('socket', 'create_connection', 'getaddrinfo', 'gethostbyname', 'socketpair'):
        monkeypatch.setattr(socket, name, no_network)
    config, starts = cfg(), Starts()
    put_state(tmp_path, State(last_attempt=T0 - 60, last_success=T0 - 60, next_due=T0 + 600))
    waiting, due = [], []
    for i in range(200):
        for samples, now in ((waiting, T0), (due, T0 + 601 + i * _sync_core.START_GAP_S)):
            begin = time.perf_counter()
            _sync_core.statusline_tick(log_dir(tmp_path), config, now, starts)
            samples.append(time.perf_counter() - begin)
    assert touched == []
    assert len(starts.calls) == 200      # only the due half started a push, with its start mark
    assert statistics.median(waiting) < 0.005 and statistics.median(due) < 0.005


# --- Criterion: three pushes started at once, exactly one runs ------------------------------------------------------

# A push process whose runner announces itself with the state it sees and then waits for the test (stdin).
PUSH_PROCESS = textwrap.dedent('''
    import os, sys
    sys.path.insert(0, sys.argv[1])
    import sync
    def runner(run):
        with open(os.path.join(run.directory, 'state.json'), encoding='utf-8') as f:
            print('running ' + f.read(), flush=True)
        sys.stdin.read()
    sys.exit(sync.main(['push'], push_runner=runner))
''')


def first_lines_within(processes, seconds):
    """The first line of each process ('' at its end); fails instead of hanging if one neither writes nor ends (a
    lock that waits would do that)."""
    lines = [None] * len(processes)

    def read(i):
        lines[i] = processes[i].stdout.readline()
    readers = [threading.Thread(target=read, args=(i,), daemon=True) for i in range(len(processes))]
    for reader in readers:
        reader.start()
    end = time.monotonic() + seconds
    for reader in readers:
        reader.join(max(end - time.monotonic(), 0))
    assert None not in lines, 'a push process neither wrote nor ended: does the lock wait?'
    return lines


def test_three_pushes_started_at_once_run_exactly_one_and_the_others_change_nothing(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'sync': {'mode': 'shadow'}}), encoding='utf-8')
    monkeypatch.setenv('CCUC_CONFIG', str(config))
    processes = [subprocess.Popen([sys.executable, '-c', PUSH_PROCESS, HOOKS], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, encoding='utf-8') for _ in range(3)]
    try:
        # The runner holds the lock until the test writes to it, so the other two can only find it held and end.
        first_lines = first_lines_within(processes, 60)
        running = [i for i, line in enumerate(first_lines) if line.startswith('running ')]
        assert len(running) == 1
        for i, process in enumerate(processes):
            if i not in running:
                assert first_lines[i] == '' and process.wait(timeout=30) == 0
        seen_by_runner = first_lines[running[0]][len('running '):].strip()
        assert (sync_dir(tmp_path) / 'state.json').read_text(encoding='utf-8') == seen_by_runner
        runner = processes[running[0]]
        runner.stdin.close()
        assert runner.wait(timeout=30) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    state = state_of(tmp_path)
    assert state.last_success is not None and state.backoff_level == 0
    assert len(sync_lines(tmp_path, 'success')) == 1


# --- Criterion: backoff doubles from 1 to 6 failures, capped, within +-20 % ----------------------------------------

@pytest.mark.parametrize('chance, factor', [(0.0, 0.8), (0.5, 1.0), (0.999999, 1.2)])
def test_one_to_six_failures_wait_interval_times_two_to_the_n_capped_within_20_percent(tmp_path, chance, factor):
    now = T0
    for failures, expected_min in zip(range(1, 7), (60, 120, 240, 360, 360, 360)):     # 30 min * 2^n, cap 6 h
        assert push(tmp_path, failing('network'), now, rand=lambda: chance) == 1
        state = state_of(tmp_path)
        assert state.backoff_level == failures and state.last_error == 'network'
        assert state.next_due - now == pytest.approx(expected_min * 60 * factor, abs=1)   # the file keeps whole seconds
        assert 0.8 * expected_min * 60 - 1 <= state.next_due - now <= 1.2 * expected_min * 60
        now = state.next_due


def test_the_jitter_of_the_real_chance_stays_within_20_percent():
    gaps = [_sync_core.jittered(1000, random.random) for _ in range(2000)]
    assert 800 <= min(gaps) and max(gaps) < 1200 and min(gaps) < 850 and max(gaps) > 1150


def test_the_cap_comes_from_the_config_and_a_huge_level_does_not_overflow():
    assert _sync_core.backoff_gap(INTERVAL_S, 3, 1) == 3600
    assert _sync_core.backoff_gap(INTERVAL_S, 5000, 6) == 6 * 3600


# --- Criterion: a backoff holds against the status line and SessionEnd ---------------------------------------------

def test_during_a_backoff_the_status_line_starts_no_push_before_next_due_and_session_end_keeps_it(tmp_path):
    push(tmp_path, failing('network'))
    before = state_of(tmp_path)
    starts = Starts()
    assert not _sync_core.statusline_tick(log_dir(tmp_path), cfg(), before.next_due - 1, starts)
    assert not _sync_core.session_end(log_dir(tmp_path), cfg(), before.next_due - 1, starts)
    assert starts.calls == [] and state_of(tmp_path) == before
    assert _sync_core.statusline_tick(log_dir(tmp_path), cfg(), before.next_due, starts)
    assert starts.calls == [_sync_core.command('push', '--if-due')]


def test_a_push_started_inside_a_backoff_ends_without_a_change(tmp_path):
    push(tmp_path, failing('auth'))
    before = (sync_dir(tmp_path) / 'state.json').read_bytes()
    called = []
    assert push(tmp_path, lambda run: called.append(run), state_of(tmp_path).next_due - 1) == 0
    assert called == [] and (sync_dir(tmp_path) / 'state.json').read_bytes() == before


# --- Criterion: a success resets the backoff ---------------------------------------------------------------------

def test_a_success_resets_the_backoff_level_and_waits_one_interval(tmp_path):
    for _ in range(3):
        push(tmp_path, failing('rejected'), state_of(tmp_path).next_due or T0)
    assert state_of(tmp_path).backoff_level == 3
    now = state_of(tmp_path).next_due
    assert push(tmp_path, ok, now, end=now + 7) == 0
    state = state_of(tmp_path)
    assert (state.backoff_level, state.last_error, state.last_attempt, state.last_success) == (0, None, now, now + 7)
    assert state.next_due == now + 7 + INTERVAL_S
    assert [r['action'] for r in sync_lines(tmp_path, 'success')] == ['push']


# --- Criterion: SessionEnd starts one detached push without backoff, none inside one ------------------------------

@pytest.mark.parametrize('state', [State(), State(last_success=T0 - 60, next_due=T0 + 600),
                                   State(backoff_level=2, next_due=T0 - 1, last_error='network')])
def test_session_end_without_a_backoff_starts_exactly_one_detached_push(tmp_path, state):
    put_state(tmp_path, state)
    starts = Starts()
    assert _sync_core.session_end(log_dir(tmp_path), cfg(), T0, starts)
    assert starts.calls == [[sys.executable, os.path.join(HOOKS, 'sync.py'), 'push']]
    assert state_of(tmp_path) == state


def test_session_end_inside_a_backoff_starts_nothing(tmp_path):
    put_state(tmp_path, State(backoff_level=1, next_due=T0 + 60, last_error='timeout'))
    starts = Starts()
    assert not _sync_core.session_end(log_dir(tmp_path), cfg(), T0, starts)
    assert starts.calls == []


def test_session_end_with_a_broken_state_starts_the_push_and_leaves_the_log_line_to_it(tmp_path):
    put_raw(tmp_path, '{broken')
    starts = Starts()
    assert _sync_core.session_end(log_dir(tmp_path), cfg(), T0, starts) and len(starts.calls) == 1
    assert sync_lines(tmp_path) == []


# --- Criterion: SessionStart catches up a push that was cut off ----------------------------------------------------

def test_session_start_after_a_cut_off_push_sets_next_due_to_now_and_pulls_in_the_background(tmp_path):
    put_state(tmp_path, State(last_attempt=T0 - 3000, last_success=T0 - 3600, next_due=T0 + 600))
    starts = Starts()
    _sync_core.session_start(log_dir(tmp_path), cfg(), T0, starts)
    assert state_of(tmp_path).next_due == T0
    assert starts.calls == [_sync_core.command('pull', '--background')]


@pytest.mark.parametrize('state, catch_up', [
    (State(), True),                                                                      # never pushed
    (State(last_attempt=T0 - 90000, last_success=T0 - 90000, next_due=T0 + 60), True),   # older than 24 h
    (State(last_attempt=T0 - 60, last_success=T0 - 60, next_due=T0 + 600), False),       # fresh and complete
    (State(last_attempt=T0 - 600, last_success=None, backoff_level=1, next_due=T0 + 60, last_error='network'), False),
    (State(last_attempt=T0 - 600, last_success=T0 - 90000, last_error='visibility', backoff_level=1), False),
])
def test_session_start_catches_up_only_outside_a_backoff_and_the_block(tmp_path, state, catch_up):
    put_state(tmp_path, state)
    starts = Starts()
    _sync_core.session_start(log_dir(tmp_path), cfg(), T0, starts)
    assert state_of(tmp_path) == (dataclasses.replace(state, next_due=T0) if catch_up else state)
    assert len(starts.calls) == 1


def test_session_start_leaves_the_state_to_a_run_that_holds_the_lock_and_still_pulls(tmp_path):
    put_state(tmp_path, State(last_attempt=T0 - 3000, last_success=T0 - 3600, next_due=T0 + 600))
    starts = Starts()
    with _sync_core.try_lock(str(sync_dir(tmp_path))) as held:
        assert held
        _sync_core.session_start(log_dir(tmp_path), cfg(), T0, starts)
    assert state_of(tmp_path).next_due == T0 + 600 and len(starts.calls) == 1


# --- Criterion: a manual run pushes in a backoff, not after visibility; the block holds until the installer -------

def test_a_manual_run_pushes_during_a_backoff(tmp_path):
    push(tmp_path, failing('network'))
    assert state_of(tmp_path).in_backoff(T0 + 1)
    assert push(tmp_path, ok, T0 + 1, forced=True) == 0
    assert state_of(tmp_path).backoff_level == 0 and state_of(tmp_path).last_success == T0 + 1


def test_after_visibility_not_even_a_manual_run_pushes(tmp_path):
    push(tmp_path, failing('visibility'))
    before = (sync_dir(tmp_path) / 'state.json').read_bytes()
    called = []
    assert push(tmp_path, lambda run: called.append(run), T0 + 86400 * 30, forced=True) == 1
    assert push(tmp_path, lambda run: called.append(run), T0 + 86400 * 30) == 1
    assert called == [] and (sync_dir(tmp_path) / 'state.json').read_bytes() == before


def test_after_visibility_no_push_starts_until_the_installer_clears_it(tmp_path):
    put_state(tmp_path, State(last_attempt=T0 - 3600, last_success=T0 - 7200, next_due=T0 - 3600))
    push(tmp_path, failing('visibility'), T0)
    state = state_of(tmp_path)
    assert state.last_error == 'visibility' and state.next_due == T0 - 3600      # next_due is not set
    starts, later = Starts(), T0 + 86400 * 7
    assert not _sync_core.statusline_tick(log_dir(tmp_path), cfg(), later, starts)
    assert not _sync_core.session_end(log_dir(tmp_path), cfg(), later, starts)
    _sync_core.session_start(log_dir(tmp_path), cfg(), later, starts)
    assert starts.calls == [_sync_core.command('pull', '--background')]      # the pull only, no catch-up
    assert state_of(tmp_path) == state
    assert _sync_core.clear_visibility(log_dir(tmp_path), later)
    cleared = state_of(tmp_path)
    assert (cleared.last_error, cleared.backoff_level, cleared.next_due) == (None, 0, later)
    assert _sync_core.statusline_tick(log_dir(tmp_path), cfg(), later, starts)


def test_clear_visibility_changes_nothing_without_a_block_and_waits_for_no_run(tmp_path):
    put_state(tmp_path, State(backoff_level=2, next_due=T0 + 60, last_error='network'))
    assert _sync_core.clear_visibility(log_dir(tmp_path), T0)
    assert state_of(tmp_path).last_error == 'network'
    with _sync_core.try_lock(str(sync_dir(tmp_path))):
        assert not _sync_core.clear_visibility(log_dir(tmp_path), T0)


# --- Criterion: a hanging git child is ended at the timeout, category timeout -------------------------------------

# A child that starts a grandchild on the same output pipe and hangs: only ending the whole tree frees the pipe.
HANGING = textwrap.dedent('''
    import subprocess, sys, time
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    print('started', flush=True)
    time.sleep(60)
''')


def test_a_hanging_child_is_ended_with_its_tree_at_the_timeout_and_the_push_records_timeout(tmp_path):
    def hanging(run):
        _sync_core.run_command([sys.executable, '-c', HANGING], run.deadline)
    begin = time.monotonic()
    code = _sync_core.push(log_dir(tmp_path), cfg(), hanging, clock=lambda: T0, rand=lambda: 0.5, timeout_s=0.5)
    elapsed = time.monotonic() - begin
    assert code == 1 and state_of(tmp_path).last_error == 'timeout' and state_of(tmp_path).backoff_level == 1
    # The grandchild held the pipe for 60 s: returning this soon means the tree was ended, not waited for.
    assert elapsed < _sync_core.KILL_WAIT_S
    assert sync_lines(tmp_path, 'failure')[0]['category'] == 'timeout'


def test_a_child_whose_output_stays_open_after_the_kill_still_ends_as_timeout(tmp_path, monkeypatch):
    left = []
    monkeypatch.setattr(_plat, 'kill_tree', left.append)          # the tree survives: the second read times out
    monkeypatch.setattr(_sync_core, 'KILL_WAIT_S', 0.1)
    with pytest.raises(_sync_core.SyncError) as error:
        _sync_core.run_command([sys.executable, '-c', 'import time; time.sleep(30)'], _sync_core.Deadline(0.2))
    assert error.value.category == 'timeout'
    left[0].kill()
    left[0].wait()


def test_no_child_is_started_once_the_deadline_has_passed():
    with pytest.raises(_sync_core.SyncError) as error:
        _sync_core.run_command([sys.executable, '-c', 'pass'], _sync_core.Deadline(-1))
    assert error.value.category == 'timeout'


def test_a_program_that_cannot_be_started_is_internal(tmp_path):
    with pytest.raises(_sync_core.SyncError) as error:
        _sync_core.run_command([str(tmp_path / 'no-git')], _sync_core.Deadline(10))
    assert error.value.category == 'internal' and 'no-git could not be started' in error.value.detail


# --- scan_command: the output piece by piece, never all of it in memory --------------------------------------------

# Writes without end; a pipe closed by the reader ends it with an error, which it ignores and sleeps on: only a kill
# ends it soon.
ENDLESS = textwrap.dedent('''
    import sys, time
    try:
        while True:
            sys.stdout.buffer.write(b'x' * 65536)
            sys.stdout.buffer.flush()
    except OSError:
        pass
    time.sleep(30)
''')


@pytest.fixture
def started(monkeypatch):
    """The Popen objects scan_command starts."""
    found, real = [], subprocess.Popen

    def popen(*args, **kwargs):
        found.append(real(*args, **kwargs))
        return found[-1]
    monkeypatch.setattr(subprocess, 'Popen', popen)
    return found


def test_scan_command_hands_on_the_output_in_pieces_and_returns_the_exit_code(monkeypatch, started):
    monkeypatch.setattr(_sync_core, 'SCAN_CHUNK_BYTES', 4)
    pieces = []
    code = _sync_core.scan_command([sys.executable, '-c', 'import sys; sys.stdout.write("0123456789"); sys.exit(3)'],
                                   _sync_core.Deadline(30), lambda chunk: pieces.append(chunk) or True)
    assert code == 3 and b''.join(pieces) == b'0123456789' and max(map(len, pieces)) <= 4
    assert started[0].stdout.closed and not _sync_core._children


def test_scan_command_ends_the_child_as_soon_as_take_says_stop(started):
    pieces = []
    begin = time.monotonic()
    code = _sync_core.scan_command([sys.executable, '-c', ENDLESS], _sync_core.Deadline(30),
                                   lambda chunk: pieces.append(len(chunk)) or len(pieces) < 3)
    assert code is None and len(pieces) == 3 and time.monotonic() - begin < 5
    assert started[0].poll() is not None and started[0].stdout.closed and not _sync_core._children


def test_scan_command_ends_the_child_and_its_tree_at_the_deadline(started):
    begin = time.monotonic()
    with pytest.raises(_sync_core.SyncError) as error:
        _sync_core.scan_command([sys.executable, '-c', HANGING], _sync_core.Deadline(0.5), lambda chunk: True)
    assert (error.value.category, error.value.detail) == ('timeout', f'{os.path.basename(sys.executable)} did not '
                                                                     'finish in time')
    assert time.monotonic() - begin < _sync_core.KILL_WAIT_S        # the grandchild held the pipe for 60 s
    assert started[0].poll() is not None and not _sync_core._children


def test_scan_command_reads_on_while_a_slow_writer_is_within_the_deadline():
    pieces = []
    child = 'import sys, time\nfor _ in range(3):\n    sys.stdout.write("ab"); sys.stdout.flush(); time.sleep(0.2)'
    assert _sync_core.scan_command([sys.executable, '-c', child], _sync_core.Deadline(30),
                                   lambda chunk: pieces.append(chunk) or True) == 0
    assert b''.join(pieces) == b'ababab'


def test_scan_command_drops_the_error_output_so_a_full_pipe_cannot_block_it():
    child = 'import sys; sys.stderr.write("e" * 4_000_000); sys.stdout.write("done")'
    pieces = []
    assert _sync_core.scan_command([sys.executable, '-c', child], _sync_core.Deadline(30),
                                   lambda chunk: pieces.append(chunk) or True) == 0
    assert b''.join(pieces) == b'done'


def test_scan_command_ends_the_child_when_take_fails(started):
    def take(chunk):
        raise ValueError('broken')
    with pytest.raises(ValueError):
        _sync_core.scan_command([sys.executable, '-c', ENDLESS], _sync_core.Deadline(30), take)
    assert started[0].poll() is not None and not _sync_core._children


def test_scan_command_gives_up_waiting_for_a_child_that_survives_the_kill(monkeypatch, started):
    monkeypatch.setattr(_plat, 'kill_tree', lambda process: None)
    monkeypatch.setattr(_sync_core, 'KILL_WAIT_S', 0.1)
    assert _sync_core.scan_command([sys.executable, '-c', ENDLESS], _sync_core.Deadline(30), lambda chunk: False) \
        is None
    assert started[0].poll() is None and not _sync_core._children
    started[0].kill()
    started[0].wait()


def test_scan_command_starts_nothing_after_the_deadline_or_without_the_program(tmp_path):
    with pytest.raises(_sync_core.SyncError) as error:
        _sync_core.scan_command([sys.executable, '-c', 'pass'], _sync_core.Deadline(-1), lambda chunk: True)
    assert error.value.category == 'timeout'
    with pytest.raises(_sync_core.SyncError) as error:
        _sync_core.scan_command([str(tmp_path / 'no-git')], _sync_core.Deadline(10), lambda chunk: True)
    assert (error.value.category, error.value.detail) == ('internal', 'no-git could not be started '
                                                                      '(FileNotFoundError)')


def test_a_finished_child_returns_its_exit_code_and_its_output_as_text():
    done = _sync_core.run_command([sys.executable, '-c', 'import sys; print("out"); sys.exit(3)'],
                                  _sync_core.Deadline(30))
    assert (done.returncode, done.stdout.strip(), done.stderr) == (3, 'out', '')


def test_the_deadline_runs_on_the_clock_it_is_given():
    now = [100.0]
    deadline = _sync_core.Deadline(5, clock=lambda: now[0])
    deadline.check()
    now[0] = 104.5
    assert deadline.left() == 0.5
    now[0] = 105
    with pytest.raises(_sync_core.SyncError, match='timeout'):
        deadline.check()


# --- Criterion: git never asks for credentials --------------------------------------------------------------------

@pytest.fixture
def git_config(tmp_path, monkeypatch):
    """Points git at a global config file of this test (never the user's) and no system config; returns a function
    that writes it. No GIT_SSH_COMMAND or GIT_SSH from outside."""
    path = tmp_path / 'gitconfig'
    path.write_text('', encoding='utf-8')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(path))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    for name in ('GIT_SSH_COMMAND', 'GIT_SSH'):
        monkeypatch.delenv(name, raising=False)
    return lambda text: path.write_text(text, encoding='utf-8')


def test_the_git_environment_forbids_every_prompt(git_config, monkeypatch):
    monkeypatch.setenv('GIT_ASKPASS', '/usr/bin/ksshaskpass')
    monkeypatch.setenv('CCUC_MARKER', 'kept')
    env = _sync_core.git_env()
    assert env['GIT_TERMINAL_PROMPT'] == '0' and env['GCM_INTERACTIVE'] == 'never' and env['GIT_ASKPASS'] == ''
    assert env['GIT_SSH_COMMAND'] == 'ssh -o BatchMode=yes' and env['CCUC_MARKER'] == 'kept'


def test_batch_mode_is_appended_to_the_users_ssh_command():
    env = _sync_core.git_env({'GIT_SSH_COMMAND': 'ssh -i key', 'GIT_SSH': 'plink'})
    assert env['GIT_SSH_COMMAND'] == 'ssh -i key -o BatchMode=yes'


def test_batch_mode_is_appended_to_core_ssh_command_of_the_git_config(git_config):
    git_config('[core]\n\tsshCommand = ssh -i /keys/sync_key\n')
    assert _sync_core.git_env()['GIT_SSH_COMMAND'] == 'ssh -i /keys/sync_key -o BatchMode=yes'


def test_a_program_in_git_ssh_is_left_alone(git_config, monkeypatch):
    git_config('[core]\n\tsshCommand = ssh -i key\n')
    monkeypatch.setenv('GIT_SSH', 'C:/Program Files/PuTTY/plink.exe')
    env = _sync_core.git_env()
    assert 'GIT_SSH_COMMAND' not in env and env['GIT_SSH'] == 'C:/Program Files/PuTTY/plink.exe'


def test_without_a_usable_git_config_the_ssh_command_is_ssh(git_config, monkeypatch):
    git_config('[core\n')                                         # broken: the lookup ends with an error
    assert _sync_core.git_env()['GIT_SSH_COMMAND'] == 'ssh -o BatchMode=yes'
    monkeypatch.setattr(_sync_core, 'GIT', 'no-such-git-here')
    assert _sync_core.git_env()['GIT_SSH_COMMAND'] == 'ssh -o BatchMode=yes'
    used_up = _sync_core.Deadline(-1)                               # no time left for the lookup
    assert _sync_core.git_env(deadline=used_up)['GIT_SSH_COMMAND'] == 'ssh -o BatchMode=yes'


def test_run_git_hands_the_environment_and_the_deadline_to_the_child(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_SSH_COMMAND', 'ssh')
    calls = []
    monkeypatch.setattr(_sync_core, 'run_command', lambda *args: calls.append(args))
    deadline = _sync_core.Deadline(60)
    run = _sync_core.Run('push', 'shadow', cfg(), log_dir(tmp_path), str(sync_dir(tmp_path)), deadline, T0)
    run.git('ls-remote', 'origin', cwd='repo')
    run.git('status')
    (argv, given_deadline, env, cwd), second = calls
    assert argv == ['git', 'ls-remote', 'origin'] and given_deadline is deadline and cwd == 'repo'
    assert (env['GIT_TERMINAL_PROMPT'], env['GCM_INTERACTIVE'], env['GIT_ASKPASS']) == ('0', 'never', '')
    assert env['GIT_SSH_COMMAND'] == 'ssh -o BatchMode=yes' and second[2] is env      # worked out once per run


def test_the_child_really_sees_the_non_interactive_environment(git_config):
    show = ('import os; print(os.environ["GIT_TERMINAL_PROMPT"], os.environ["GCM_INTERACTIVE"], '
            'os.environ["GIT_SSH_COMMAND"])')
    done = _sync_core.run_command([sys.executable, '-c', show], _sync_core.Deadline(30), _sync_core.git_env())
    assert done.stdout.split()[:2] == ['0', 'never'] and done.stdout.strip().endswith('-o BatchMode=yes')


# --- Criterion: a broken state.json crashes nothing and logs exactly one line -------------------------------------

@pytest.mark.parametrize('content', ['{broken', '[]', '"text"', '{"backoff_level": -1}', '{"backoff_level": true}',
                                     '{"backoff_level": "2"}', '{"last_error": "bogus"}', '{"last_error": ["network"]}',
                                     '{"next_due": 5}', '{"next_due": "2026-01-14T10:00:00"}',
                                     '{"last_success": "yesterday"}',
                                     # an id of its own: as the test id, 100000 characters would not fit into the
                                     # environment variable PYTEST_CURRENT_TEST on Windows (32767 at most)
                                     pytest.param('[' * 100000, id='deeply-nested')])
def test_a_broken_state_file_counts_as_empty_with_exactly_one_log_line(tmp_path, content):
    put_raw(tmp_path, content)
    assert push(tmp_path) == 0
    assert len(sync_lines(tmp_path, 'state_unreadable')) == 1
    assert state_of(tmp_path).last_success == T0


def test_a_state_file_in_another_encoding_is_broken(tmp_path):
    os.makedirs(sync_dir(tmp_path))
    (sync_dir(tmp_path) / 'state.json').write_bytes(b'{"last_error": "\xff"}')
    with pytest.raises(_sync_core.BrokenState, match='UnicodeDecodeError'):
        state_of(tmp_path)


def test_a_huge_or_irregular_state_file_is_broken(tmp_path):
    put_raw(tmp_path, ' ' * (_sync_core.MAX_STATE_BYTES + 1))
    with pytest.raises(_sync_core.BrokenState, match='too large'):
        state_of(tmp_path)
    (sync_dir(tmp_path) / 'state.json').unlink()
    (sync_dir(tmp_path) / 'state.json').mkdir()
    with pytest.raises(_sync_core.BrokenState):
        state_of(tmp_path)


def test_a_missing_state_is_empty_without_a_log_line(tmp_path):
    assert state_of(tmp_path) == State()
    assert sync_lines(tmp_path) == []


def test_the_state_file_holds_the_six_fields_in_iso_utc(tmp_path):
    put_state(tmp_path, State(last_attempt=T0, last_success=T0 - 60, backoff_level=2, next_due=T0 + 3600,
                              last_error='conflict', last_pull=T0 - 120))
    assert json.loads((sync_dir(tmp_path) / 'state.json').read_text(encoding='utf-8')) == {
        'last_attempt': '2026-01-14T14:13:20Z', 'last_success': '2026-01-14T14:12:20Z', 'backoff_level': 2,
        'next_due': '2026-01-14T15:13:20Z', 'last_error': 'conflict', 'last_pull': '2026-01-14T14:11:20Z'}
    put_raw(tmp_path, '{"last_success": "2026-01-14T15:13:20+01:00"}')
    assert state_of(tmp_path) == State(last_success=T0)


def test_the_status_line_logs_an_unreadable_state_at_most_once_an_hour(tmp_path):
    put_raw(tmp_path, '{broken')
    starts = Starts()
    for now in (T0, T0 + 60, T0 + 3599):
        _sync_core.statusline_tick(log_dir(tmp_path), cfg(), now, starts)
    assert len(sync_lines(tmp_path, 'state_unreadable')) == 1
    _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0 + 3600, starts)
    assert len(sync_lines(tmp_path, 'state_unreadable')) == 2
    assert len(starts.calls) == 2      # T0 and T0 + 3599; the others were within START_GAP_S of a start


def test_the_status_line_logs_again_if_its_note_is_broken_or_cannot_be_written(tmp_path):
    put_raw(tmp_path, '{broken')
    (tmp_path / 'log' / _sync_core.READ_ERROR_NOTE).mkdir()      # neither readable nor replaceable
    for now in (T0, T0 + 60):
        _sync_core.statusline_tick(log_dir(tmp_path), cfg(), now, Starts())
    assert len(sync_lines(tmp_path, 'state_unreadable')) == 2


# --- Criterion: folder 0700, files 0600, whatever the umask ------------------------------------------------------

def make_every_sync_file(tmp_path):
    """state.json, sync.lock and the note of the status line."""
    push(tmp_path, failing('network'))
    put_raw(tmp_path, '{broken')
    _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0, Starts())
    push(tmp_path, ok, T0 + 86400)


def modes(tmp_path):
    """The sync folder, its two files, and the start mark and the note in the log folder."""
    folder = sync_dir(tmp_path)
    return ([stat.S_IMODE(os.stat(folder).st_mode)]
            + [stat.S_IMODE(os.stat(path).st_mode) for path in (
                folder / 'state.json', folder / 'sync.lock', tmp_path / 'log' / _sync_core.START_MARKS['push'],
                tmp_path / 'log' / _sync_core.READ_ERROR_NOTE)])


@pytest.mark.posix_only
@pytest.mark.parametrize('umask', [0o000, 0o022, 0o077])
def test_the_sync_folder_is_0700_and_its_files_0600_whatever_the_umask(tmp_path, umask):
    old = os.umask(umask)
    try:
        make_every_sync_file(tmp_path)
    finally:
        os.umask(old)
    assert modes(tmp_path) == [0o700, 0o600, 0o600, 0o600, 0o600]


@pytest.mark.posix_only
def test_an_open_sync_folder_of_an_earlier_umask_becomes_private(tmp_path):
    sync_dir(tmp_path).mkdir(parents=True)
    sync_dir(tmp_path).chmod(0o755)
    push(tmp_path)
    assert stat.S_IMODE(os.stat(sync_dir(tmp_path)).st_mode) == 0o700


@pytest.mark.posix_only
def test_the_files_are_0600_even_under_a_umask_that_takes_the_owner_rights(tmp_path):
    make_every_sync_file(tmp_path)
    for path in (sync_dir(tmp_path) / 'state.json', sync_dir(tmp_path) / 'sync.lock',
                 tmp_path / 'log' / _sync_core.START_MARKS['push'], tmp_path / 'log' / _sync_core.READ_ERROR_NOTE):
        path.unlink()
    old = os.umask(0o277)      # a new file would be 0400: only the explicit chmod gives 0600
    try:
        make_every_sync_file(tmp_path)
    finally:
        os.umask(old)
    assert modes(tmp_path) == [0o700, 0o600, 0o600, 0o600, 0o600]


@pytest.mark.posix_only
def test_a_link_as_the_sync_folder_is_never_followed(tmp_path):
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (tmp_path / 'log').mkdir()
    os.symlink(elsewhere, sync_dir(tmp_path))
    with pytest.raises(OSError, match='not a folder'):
        push(tmp_path)
    assert list(elsewhere.iterdir()) == []
    with pytest.raises(OSError, match='is a link'):
        _sync_core.write_state(str(sync_dir(tmp_path)), State())


def test_a_file_as_the_sync_folder_is_refused(tmp_path):
    (tmp_path / 'log').mkdir()
    sync_dir(tmp_path).write_text('', encoding='utf-8')
    with pytest.raises(OSError):
        push(tmp_path)


# --- Lock, modes, runners, interval --------------------------------------------------------------------------------

def test_a_push_or_pull_finding_the_lock_held_ends_with_0_and_changes_nothing(tmp_path):
    put_state(tmp_path, State(last_success=T0 - 60, next_due=T0 - 1))
    before = (sync_dir(tmp_path) / 'state.json').read_bytes()
    called = []
    with _sync_core.try_lock(str(sync_dir(tmp_path))) as held:
        assert held
        with _sync_core.try_lock(str(sync_dir(tmp_path))) as second:
            assert not second
        assert push(tmp_path, called.append) == 0 and push(tmp_path, called.append, forced=True) == 0
        assert _sync_core.pull(log_dir(tmp_path), cfg(), called.append, background=True) == 0
    assert called == [] and (sync_dir(tmp_path) / 'state.json').read_bytes() == before and sync_lines(tmp_path) == []


def test_last_attempt_is_written_before_the_runner_runs(tmp_path):
    seen = []
    push(tmp_path, lambda run: seen.append(_sync_core.read_state(run.directory)), T0)
    assert seen == [State(last_attempt=T0)]


def test_mode_off_does_nothing_at_all(tmp_path):
    off, starts, called = cfg(mode='off'), Starts(), []
    assert _sync_core.push(log_dir(tmp_path), off, called.append, forced=True) == 0
    assert _sync_core.pull(log_dir(tmp_path), off, called.append, background=True) == 0
    assert not _sync_core.statusline_tick(log_dir(tmp_path), off, T0, starts)
    assert not _sync_core.session_end(log_dir(tmp_path), off, T0, starts)
    _sync_core.session_start(log_dir(tmp_path), off, T0, starts)
    assert called == [] and starts.calls == [] and not (tmp_path / 'log').exists()


@pytest.mark.parametrize('mode', ['shadow', 'on'])
def test_shadow_and_on_run_the_core_alike_and_hand_the_mode_to_the_runner(tmp_path, mode):
    runs = []
    assert push(tmp_path, runs.append, config=cfg(mode=mode)) == 0
    assert runs[0].mode == mode and runs[0].kind == 'push' and runs[0].started == T0
    assert runs[0].directory == str(sync_dir(tmp_path)) and runs[0].log_dir == log_dir(tmp_path)
    assert 59 < runs[0].deadline.left() <= _sync_core.PUSH_TIMEOUT_S


def test_an_unexpected_error_of_the_runner_is_internal(tmp_path):
    def broken(run):
        raise RuntimeError('a bug')
    assert push(tmp_path, broken) == 1
    line, = sync_lines(tmp_path, 'failure')
    assert (line['category'], line['reason'], line['action'], line['mode']) == (
        'internal', 'unexpected RuntimeError', 'push', 'shadow')


def test_an_unknown_category_is_refused_and_counts_as_internal(tmp_path):
    with pytest.raises(ValueError):
        _sync_core.SyncError('bogus')
    assert push(tmp_path, failing('bogus')) == 1 and state_of(tmp_path).last_error == 'internal'
    assert str(_sync_core.SyncError('auth')) == 'auth' and str(_sync_core.SyncError('auth', 'x')) == 'auth: x'


def test_an_interval_below_10_minutes_is_raised_to_10_with_a_log_line(tmp_path):
    push(tmp_path, ok, config=cfg(interval_min=3))
    assert state_of(tmp_path).next_due == T0 + 600
    assert sync_lines(tmp_path, 'interval_raised')[0]['reason'] == 'sync.interval_min 3 is below 10, using 10'
    assert _sync_core.interval_s(cfg(interval_min=45), log_dir(tmp_path)) == 2700


def test_the_backoff_cap_comes_from_the_config(tmp_path):
    push(tmp_path, failing('network'), config=cfg(backoff_max_h=0.5))
    assert state_of(tmp_path).next_due == T0 + 1800


@pytest.mark.parametrize('background, seconds', [(True, 30), (False, 5)])
def test_a_pull_runs_under_the_lock_with_its_timeout_and_writes_only_last_pull(tmp_path, background, seconds):
    push_state = State(last_attempt=T0 - 90, last_success=T0 - 100, backoff_level=1, next_due=T0 + 60,
                       last_error='network')
    put_state(tmp_path, push_state)
    runs = []

    def runner(run):
        with _sync_core.try_lock(run.directory) as second:
            assert not second
        runs.append(run)
    assert _sync_core.pull(log_dir(tmp_path), cfg(), runner, background, clock=lambda: T0) == 0
    assert runs[0].kind == 'pull' and seconds - 1 < runs[0].deadline.left() <= seconds
    assert state_of(tmp_path) == dataclasses.replace(push_state, last_pull=T0)
    assert [(r['decision'], r['action']) for r in sync_lines(tmp_path)] == [('success', 'pull')]


def test_a_failed_pull_is_logged_with_its_category_and_leaves_last_pull(tmp_path):
    put_state(tmp_path, State(last_pull=T0 - 600))
    assert _sync_core.pull(log_dir(tmp_path), cfg(), failing('conflict'), background=False) == 1
    line, = sync_lines(tmp_path, 'failure')
    assert (line['action'], line['category'], line['reason']) == ('pull', 'conflict', 'conflict for the test')
    assert state_of(tmp_path) == State(last_pull=T0 - 600)


def test_the_outcome_of_a_pull_names_how_it_ended(tmp_path):
    outcome = _sync_core.pull_outcome
    assert outcome(log_dir(tmp_path), cfg(mode='off'), ok, background=False) == _sync_core.Outcome('off')
    assert outcome(log_dir(tmp_path), cfg(), ok, background=False, clock=lambda: T0) == _sync_core.Outcome('ok')
    assert outcome(log_dir(tmp_path), cfg(), failing('network'), background=False) == _sync_core.Outcome(
        'failed', 'network', 'network for the test')
    with _sync_core.try_lock(str(sync_dir(tmp_path))):
        assert outcome(log_dir(tmp_path), cfg(), ok, background=False) == _sync_core.Outcome('locked')
    assert state_of(tmp_path).last_pull == T0


def test_a_pull_after_a_broken_state_counts_it_as_empty_and_keeps_its_success(tmp_path):
    put_raw(tmp_path, '{broken')
    assert _sync_core.pull(log_dir(tmp_path), cfg(), ok, background=True, clock=lambda: T0) == 0
    assert state_of(tmp_path) == State(last_pull=T0)
    assert [r['decision'] for r in sync_lines(tmp_path)] == ['state_unreadable', 'success']


def test_the_command_line_runs_sync_py_next_to_the_core_with_this_python():
    assert _sync_core.command('pull', '--background') == [sys.executable, os.path.join(HOOKS, 'sync.py'), 'pull',
                                                         '--background']


def test_the_status_line_starts_a_push_on_an_empty_state(tmp_path, monkeypatch):
    starts = Starts()
    monkeypatch.setattr(_plat, 'start_detached', starts)
    assert _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0)
    assert starts.calls == [_sync_core.command('push', '--if-due')] and sync_lines(tmp_path) == []


# --- Review findings: no start while a push may run, one start per gap, --if-due, the watchdog, a far next_due ------

def test_no_start_while_a_push_may_still_be_running(tmp_path):
    put_state(tmp_path, State(last_attempt=T0 - 1, last_success=T0 - 3600, next_due=T0 - 10))
    starts = Starts()
    assert not _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0, starts)
    assert not _sync_core.session_end(log_dir(tmp_path), cfg(), T0, starts)
    _sync_core.session_start(log_dir(tmp_path), cfg(), T0, starts)
    assert starts.calls == []
    later = T0 - 1 + _sync_core.running_window_s()      # by then the watchdog has ended any push
    assert _sync_core.statusline_tick(log_dir(tmp_path), cfg(), later, starts)
    assert starts.calls == [_sync_core.command('push', '--if-due')]


def test_one_start_of_a_kind_per_start_gap(tmp_path):
    starts = Starts()
    assert _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0, starts)
    assert not _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0 + 119, starts)
    assert not _sync_core.session_end(log_dir(tmp_path), cfg(), T0 + 119, starts)      # also a push
    _sync_core.session_start(log_dir(tmp_path), cfg(), T0 + 119, starts)                # a pull: another kind
    assert _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0 + 120, starts)
    assert starts.calls == [_sync_core.command('push', '--if-due'), _sync_core.command('pull', '--background'),
                            _sync_core.command('push', '--if-due')]


def test_a_start_mark_from_the_future_does_not_block(tmp_path):
    starts = Starts()
    assert _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0, starts)
    assert _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0 - 3600, starts)      # the clock went back
    assert len(starts.calls) == 2


def test_a_start_mark_that_cannot_be_written_starts_nothing(tmp_path):
    (tmp_path / 'log' / _sync_core.START_MARKS['push']).mkdir(parents=True)
    starts = Starts()
    assert not _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0, starts)
    assert not _sync_core.session_end(log_dir(tmp_path), cfg(), T0, starts)
    assert starts.calls == []


def test_a_log_folder_that_is_a_file_starts_nothing(tmp_path):
    (tmp_path / 'log').write_text('', encoding='utf-8')
    starts = Starts()
    for minute in range(5):
        assert not _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0 + 60 * minute, starts)
    assert starts.calls == []


def test_a_file_as_the_sync_folder_starts_one_push_per_gap_and_logs_once_an_hour(tmp_path):
    (tmp_path / 'log').mkdir()
    sync_dir(tmp_path).write_text('', encoding='utf-8')
    starts = Starts()
    for minute in range(10):
        _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0 + 60 * minute, starts)
    assert len(starts.calls) == 5 and len(sync_lines(tmp_path, 'state_unreadable')) == 1


def test_a_push_if_due_after_another_one_pushed_ends_without_a_change(tmp_path):
    calls = []
    assert push(tmp_path, calls.append, T0, if_due=True) == 0
    before = (sync_dir(tmp_path) / 'state.json').read_bytes()
    assert push(tmp_path, calls.append, T0 + 5, if_due=True) == 0
    assert len(calls) == 1 and (sync_dir(tmp_path) / 'state.json').read_bytes() == before
    assert push(tmp_path, calls.append, T0 + 5) == 0 and len(calls) == 2      # T2 does not ask for due


@pytest.mark.parametrize('level', [0, 3])
def test_a_next_due_further_ahead_than_any_wait_counts_as_due(tmp_path, level):
    put_state(tmp_path, State(last_success=T0 - 90000, backoff_level=level, next_due=T0 + 100 * 365 * 86400,
                              last_error='network' if level else None))
    starts, calls = Starts(), []
    assert _sync_core.statusline_tick(log_dir(tmp_path), cfg(), T0, starts)
    assert _sync_core.session_end(log_dir(tmp_path), cfg(), T0 + 300, starts)
    assert push(tmp_path, calls.append, T0 + 400, if_due=True) == 0 and len(calls) == 1


def test_the_horizon_is_the_longer_wait_plus_the_jitter():
    assert _sync_core.max_wait_s(cfg()) == pytest.approx(1.2 * 6 * 3600)
    assert _sync_core.max_wait_s(cfg(backoff_max_h=0.1, interval_min=60)) == pytest.approx(1.2 * 3600)
    assert _sync_core.max_wait_s(cfg(backoff_max_h=0.01, interval_min=3)) == pytest.approx(1.2 * 600)
    at_the_edge = State(next_due=T0 + 1.2 * 6 * 3600)
    assert _sync_core.bounded(at_the_edge, T0, cfg()) == at_the_edge
    assert _sync_core.bounded(State(next_due=T0 + 1.2 * 6 * 3600 + 1), T0, cfg()).next_due is None


@pytest.fixture
def quick_watchdog(monkeypatch):
    """The watchdog fires 0.1 s after the deadline; its exit is recorded instead of ending pytest."""
    monkeypatch.setattr(_sync_core, 'KILL_WAIT_S', 0.05)
    monkeypatch.setattr(_sync_core, 'RUN_SLACK_S', 0.05)
    exits, released = [], threading.Event()

    def record(code):
        exits.append(code)
        released.set()
    monkeypatch.setattr(_sync_core, '_exit', record)
    return exits, released


def test_the_watchdog_ends_a_push_whose_runner_hangs_without_a_child(tmp_path, quick_watchdog):
    exits, released = quick_watchdog
    seen = []

    def hanging(run):
        assert released.wait(30)
        seen.append((state_of(tmp_path), [r['reason'] for r in sync_lines(tmp_path, 'failure')]))
        raise _sync_core.SyncError('internal', 'released by the test')      # a real process never gets here
    begin = time.monotonic()
    _sync_core.push(log_dir(tmp_path), cfg(), hanging, clock=lambda: T0, rand=lambda: 0.5, timeout_s=0.1)
    assert time.monotonic() - begin < 10 and exits == [1]
    (state, reasons), = seen
    assert (state.last_error, state.backoff_level) == ('timeout', 1)
    assert reasons == ['the run outlived its deadline, the process was ended']


def test_the_watchdog_ends_the_children_of_a_run(tmp_path, quick_watchdog):
    exits, _released = quick_watchdog
    codes = []

    def runner(run):
        codes.append(_sync_core.run_command([sys.executable, '-c', 'import time; time.sleep(60)'],
                                            _sync_core.Deadline(60)).returncode)
    begin = time.monotonic()
    _sync_core.push(log_dir(tmp_path), cfg(), runner, clock=lambda: T0, rand=lambda: 0.5, timeout_s=0.1)
    assert time.monotonic() - begin < 10 and exits == [1] and codes[0] != 0 and _sync_core._children == set()


def test_the_watchdog_ends_a_hanging_pull(tmp_path, quick_watchdog, monkeypatch):
    exits, released = quick_watchdog
    monkeypatch.setattr(_sync_core, 'PULL_BACKGROUND_TIMEOUT_S', 0.1)

    def hanging(run):
        assert released.wait(30)
    _sync_core.pull(log_dir(tmp_path), cfg(), hanging, background=True)
    assert exits == [1]
    line = sync_lines(tmp_path, 'failure')[0]
    assert (line['action'], line['category']) == ('pull', 'timeout')


# The watchdog for real: os._exit ends the process, its lock goes with it.
WATCHED_PROCESS = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, sys.argv[1])
    import _sync_core, sync
    _sync_core.PUSH_TIMEOUT_S, _sync_core.KILL_WAIT_S, _sync_core.RUN_SLACK_S = 0.2, 0.1, 0.1
    sys.exit(sync.main(['push', '--now'], push_runner=lambda run: time.sleep(60)))
''')


def test_a_push_process_whose_runner_hangs_is_ended_and_frees_the_lock(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'sync': {'mode': 'shadow'}}), encoding='utf-8')
    monkeypatch.setenv('CCUC_CONFIG', str(config))
    begin = time.monotonic()
    done = subprocess.run([sys.executable, '-c', WATCHED_PROCESS, HOOKS], capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and time.monotonic() - begin < 30, done.stderr
    assert state_of(tmp_path).last_error == 'timeout'
    assert sync_lines(tmp_path, 'failure')[0]['reason'] == 'the run outlived its deadline, the process was ended'
    with _sync_core.try_lock(str(sync_dir(tmp_path))) as held:
        assert held


def test_the_watchdog_still_ends_the_run_if_the_state_cannot_be_written(tmp_path, quick_watchdog, monkeypatch):
    exits, released = quick_watchdog

    def hanging(run):
        def refuse(*_args):
            raise OSError('disk full')
        monkeypatch.setattr(_sync_core, 'write_state', refuse)
        assert released.wait(30)
    with pytest.raises(OSError):      # only here, after the recorded exit, the push itself meets the full disk
        _sync_core.push(log_dir(tmp_path), cfg(), hanging, clock=lambda: T0, rand=lambda: 0.5, timeout_s=0.1)
    assert exits == [1] and sync_lines(tmp_path, 'failure')[0]['category'] == 'timeout'


def test_session_start_with_a_broken_state_and_a_held_lock_still_pulls(tmp_path):
    put_raw(tmp_path, '{broken')
    starts = Starts()
    with _sync_core.try_lock(str(sync_dir(tmp_path))):
        _sync_core.session_start(log_dir(tmp_path), cfg(), T0, starts)
    assert starts.calls == [_sync_core.command('pull', '--background')]


def test_the_end_of_the_run_waits_for_a_watchdog_that_fired():
    order, fired = [], threading.Event()

    def give_up():
        fired.set()
        order.append('watchdog starts')
        threading.Event().wait(0.3)      # time for the run to try to get past the block
        order.append('watchdog ends')
    with _sync_core._watchdog(0.01, give_up):
        assert fired.wait(10)
    order.append('block ends')
    assert order == ['watchdog starts', 'watchdog ends', 'block ends']


def test_a_file_as_the_sync_folder_is_a_broken_state_on_every_system(tmp_path):
    (tmp_path / 'log').mkdir()
    sync_dir(tmp_path).write_text('', encoding='utf-8')
    with pytest.raises(_sync_core.BrokenState, match='not a folder|NotADirectoryError'):
        state_of(tmp_path)


@pytest.mark.posix_only
def test_a_dangling_link_as_the_sync_folder_is_a_broken_state(tmp_path):
    (tmp_path / 'log').mkdir()
    os.symlink(tmp_path / 'gone', sync_dir(tmp_path))
    with pytest.raises(_sync_core.BrokenState, match='not a folder'):
        state_of(tmp_path)


def test_a_push_keeps_the_time_of_the_last_pull(tmp_path):
    put_state(tmp_path, State(last_pull=T0 - 60))
    assert push(tmp_path, ok, forced=True) == 0 and state_of(tmp_path).last_pull == T0 - 60
    assert push(tmp_path, failing('network'), forced=True) == 1 and state_of(tmp_path).last_pull == T0 - 60


def test_the_outcome_of_a_push_names_how_it_ended(tmp_path):
    def outcome(runner=ok, config=None, **kwargs):
        return _sync_core.push_outcome(log_dir(tmp_path), config or cfg(), runner, clock=lambda: T0,
                                       rand=lambda: 0.5, **kwargs)
    assert outcome(config=cfg(mode='off')) == _sync_core.Outcome('off')
    assert outcome() == _sync_core.Outcome('ok')
    assert outcome(if_due=True) == _sync_core.Outcome('waiting')               # the success set next_due ahead
    assert outcome(failing('auth'), forced=True) == _sync_core.Outcome('failed', 'auth', 'auth for the test')
    assert outcome() == _sync_core.Outcome('waiting')                          # inside the backoff
    with _sync_core.try_lock(str(sync_dir(tmp_path))):
        assert outcome(forced=True) == _sync_core.Outcome('locked')
    put_state(tmp_path, State(backoff_level=1, last_error='visibility'))
    assert outcome(forced=True) == _sync_core.Outcome('blocked', 'visibility')
    assert [push(tmp_path, forced=True)] == [1]                                # blocked is a failure as exit code
