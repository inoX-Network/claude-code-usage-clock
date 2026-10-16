"""The Windows branches of the core modules (hooks/): lock, replace retry, binary files, paths, owner check."""
import json
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import _config
import _context_state
import _hookio
import _history
import _plat
import _spawn_check
import _usage
import _win
import context_reporter
from conftest import HOOKS, ROOT, WINDOWS_CLI


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(_win.time, 'sleep', calls.append)
    return calls


@pytest.fixture
def replace_fails_once(monkeypatch, sleeps):
    """The first os.replace of every call site is refused, as Windows does while a reader has the target open."""
    calls = []
    real = os.replace

    def replace(src, dst):
        calls.append(dst)
        if calls.count(dst) == 1:
            raise PermissionError(13, 'in use')
        return real(src, dst)

    monkeypatch.setattr(os, 'replace', replace)
    return calls


# --- the platform switch ------------------------------------------------------------------------------------------

def test_the_switch_hands_the_windows_functions_through():
    assert _plat.locked is _win.locked and _plat.pread is _win.pread


def test_replace_retries(tmp_path, replace_fails_once):
    (tmp_path / 'a').write_text('new')
    (tmp_path / 'b').write_text('old')
    _plat.replace(str(tmp_path / 'a'), str(tmp_path / 'b'))
    assert (tmp_path / 'b').read_text() == 'new' and len(replace_fails_once) == 2


def test_pread_reads_at_an_offset_and_does_not_disturb_appending(tmp_path):
    path = tmp_path / 'f'
    path.write_bytes(b'abcdef')
    descriptor = os.open(path, os.O_RDWR | os.O_APPEND | _plat.O_BINARY)
    try:
        assert _plat.pread(descriptor, 3, 2) == b'cde'
        assert _plat.pread(descriptor, 99, 4) == b'ef'
        assert _plat.pread(descriptor, 3, 99) == b''
        os.write(descriptor, b'XY')
    finally:
        os.close(descriptor)
    assert path.read_bytes() == b'abcdefXY'


def test_fchmod_does_nothing_and_pretends_nothing(tmp_path):
    """Windows knows no 0600: the call neither fails nor makes the file read-only."""
    path = tmp_path / 'f'
    path.write_bytes(b'x')
    descriptor = os.open(path, os.O_RDWR | _plat.O_BINARY)
    try:
        _plat.fchmod(descriptor, 0o400)
        os.write(descriptor, b'y')
    finally:
        os.close(descriptor)
    assert os.access(path, os.W_OK) and path.read_bytes() == b'y'


def test_the_open_flags_are_binary_and_no_link_flag_exists():
    assert _plat.O_BINARY == os.O_BINARY != 0
    assert _plat.NO_FOLLOW == 0 and _plat.O_NONBLOCK == 0 and _usage.NO_FOLLOW == 0


def test_the_modules_find_the_windows_module_from_a_checkout_and_from_an_installation(tmp_path):
    """From the checkout windows/cli/ is added to the path; in an installation _win.py sits next to the hooks."""
    show = 'import _plat, sys; print(_plat._win.__file__)'
    done = subprocess.run([sys.executable, '-I', '-c', f'import sys; sys.path.insert(0, {HOOKS!r}); {show}'],
                          capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert os.path.samefile(done.stdout.strip(), os.path.join(WINDOWS_CLI, '_win.py'))
    installed = tmp_path / 'installed'
    installed.mkdir()
    for name in os.listdir(HOOKS):
        if name.endswith('.py'):
            (installed / name).write_bytes(open(os.path.join(HOOKS, name), 'rb').read())
    (installed / '_win.py').write_bytes(open(os.path.join(WINDOWS_CLI, '_win.py'), 'rb').read())
    done = subprocess.run([sys.executable, '-I', '-c', f'import sys; sys.path.insert(0, {str(installed)!r}); {show}'],
                          capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert os.path.samefile(done.stdout.strip(), installed / '_win.py')


# --- every os.replace of the core goes through the retry ----------------------------------------------------------

def test_the_usage_file_is_written_although_a_reader_blocks_the_first_replace(tmp_path, replace_fails_once):
    now = time.time()
    status = {'rate_limits': {'five_hour': {'used_percentage': 12, 'resets_at': now + 3600}}}
    merged = _usage.update_file(str(tmp_path / 'usage.json'), status, now, str(tmp_path / 'log'))
    assert json.loads((tmp_path / 'usage.json').read_text())['five_hour']['used_percentage'] == 12
    assert merged['five_hour']['used_percentage'] == 12 and len(replace_fails_once) == 2


def test_the_history_rotates_although_a_reader_blocks_the_first_replace(tmp_path, replace_fails_once):
    now = time.time()
    log = str(tmp_path / 'log')
    for percent in (10, 11):
        merged = {'five_hour': {'used_percentage': percent, 'resets_at': now + 3600}, 'seven_day': None}
        _history.append(log, merged, now, max_kb=0.05)
    assert os.path.exists(os.path.join(log, 'usage-history.jsonl.1'))
    assert len(open(os.path.join(log, 'usage-history.jsonl')).read().splitlines()) == 1


def test_the_log_rotates_although_a_reader_blocks_the_first_replace(tmp_path, monkeypatch, replace_fails_once):
    monkeypatch.setattr(_hookio, 'LOG_MAX_BYTES', 10)
    log = str(tmp_path / 'log')
    _hookio.write_log_line({'a': 'x' * 50}, log)
    _hookio.write_log_line({'a': 'y'}, log)
    assert os.path.exists(os.path.join(log, 'log.jsonl.1'))
    assert json.loads(open(os.path.join(log, 'log.jsonl')).read()) == {'a': 'y'}


def test_a_state_file_is_written_while_another_process_reads_it(tmp_path):
    """The real case: the status line writes the context file of a session while a hook still has the old one open."""
    folder = str(tmp_path / 'context')
    assert _context_state.write_private(folder, 's.json', '{"v":1}')
    reader = open(os.path.join(folder, 's.json'), 'rb')
    try:
        threading.Timer(0.2, reader.close).start()
        assert _context_state.write_private(folder, 's.json', '{"v":2}')
    finally:
        reader.close()
    assert open(os.path.join(folder, 's.json')).read() == '{"v":2}'


# --- files are binary: no line break translation, no Ctrl-Z as end of file -----------------------------------------

def test_history_and_log_lines_end_with_a_bare_line_feed(tmp_path):
    now = time.time()
    log = str(tmp_path / 'log')
    for percent in (10, 11):
        _history.append(log, {'five_hour': {'used_percentage': percent, 'resets_at': now + 3600}, 'seven_day': None}, now)
    _hookio.write_log_line({'a': 1}, log)
    for name, lines in (('usage-history.jsonl', 2), ('log.jsonl', 1)):
        data = open(os.path.join(log, name), 'rb').read()
        assert b'\r' not in data and data.count(b'\n') == lines


def test_a_cut_off_history_line_gets_its_line_break(tmp_path):
    now = time.time()
    log = tmp_path / 'log'
    log.mkdir()
    (log / 'usage-history.jsonl').write_bytes(b'{"t":"2026-01-01T00:00:00Z","five":')
    _history.append(str(log), {'five_hour': {'used_percentage': 10, 'resets_at': now + 3600}, 'seven_day': None}, now)
    lines = (log / 'usage-history.jsonl').read_bytes().split(b'\n')
    assert lines[0].startswith(b'{"t":"2026') and json.loads(lines[1])['five'] == 10 and lines[2] == b''


def test_a_ctrl_z_in_an_agent_file_does_not_cut_the_frontmatter(tmp_path):
    """In text mode the C runtime takes 0x1A for the end of the file and the frontmatter would look unfinished."""
    path = tmp_path / 'agent.md'
    path.write_bytes(b'---\nname: a\nmodel: haiku\ndescription: x\x1ay\neffort: low\n---\nbody')
    head = _spawn_check._read_head(str(path))
    assert head is not None and head['name'] == 'a' and head['effort'] == 'low'


def test_a_ctrl_z_in_a_skill_file_does_not_hide_its_fork(tmp_path):
    """Cut at the Ctrl-Z, `context: fork` would be missed and the skill would start an agent unchecked."""
    path = tmp_path / 'SKILL.md'
    path.write_bytes(b'---\nname: s\ndescription: a\x1ab\ncontext: fork\n---\nbody')
    head = _spawn_check._read_skill_head(str(path))
    assert head is not None and head['_fork'] == 'yes'


# --- paths ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('spelling', ['/', '\\'])
def test_a_worktree_is_followed_to_its_main_checkout(tmp_path, spelling):
    """git writes `gitdir: C:/.../.git/worktrees/x` with forward slashes; os.sep is a backslash here."""
    main = tmp_path / 'main'
    gitdir = main / '.git' / 'worktrees' / 'wt'
    gitdir.mkdir(parents=True)
    worktree = tmp_path / 'wt'
    worktree.mkdir()
    (worktree / '.git').write_text('gitdir: ' + str(gitdir).replace('\\', spelling) + '\n')
    roots = _hookio._project_roots(str(worktree))
    assert len(roots) == 2 and os.path.samefile(roots[0], worktree) and os.path.samefile(roots[1], main)


def test_skills_of_the_main_checkout_are_searched_for_a_worktree(tmp_path):
    main = tmp_path / 'main'
    gitdir = main / '.git' / 'worktrees' / 'wt'
    gitdir.mkdir(parents=True)
    worktree = tmp_path / 'wt'
    worktree.mkdir()
    (worktree / '.git').write_text(f'gitdir: {gitdir.as_posix()}\n')
    skills, _ = _hookio.skill_dirs({'cwd': str(worktree)})
    wanted = os.path.normcase(os.path.normpath(main / '.claude' / 'skills'))
    assert wanted in [os.path.normcase(os.path.normpath(s)) for s in skills]


# --- the context reporter: no owner check, no group/other bits, a command Git Bash can run -------------------------

def test_a_routine_file_is_accepted_although_windows_reports_it_writable_for_everybody(tmp_path):
    """st_mode of a normal Windows file has the group and other write bits; the POSIX test would refuse every file."""
    routine = tmp_path / 'routine.md'
    routine.write_text('Save progress.\n', encoding='utf-8')
    assert context_reporter.read_routine(str(routine)) == ('Save progress.', None)


def test_the_owner_check_is_left_out_and_a_regular_file_opens(tmp_path):
    path = tmp_path / 'f'
    path.write_text('x')
    descriptor, reason = context_reporter._open_regular(str(path))
    assert reason is None
    os.close(descriptor)


def test_a_missing_and_a_directory_are_still_refused(tmp_path):
    assert context_reporter._open_regular(str(tmp_path / 'gone')) == (None, 'missing')
    assert context_reporter._open_regular(str(tmp_path))[0] is None


def test_the_wake_offer_names_the_running_interpreter_and_git_bash_can_run_it(tmp_path):
    offer = context_reporter.wake_offer(str(tmp_path / 'context'), 'sess-1', _now())
    script = os.path.join(HOOKS, 'wake_watcher.py')
    command = offer.split('timeout 7200000): ', 1)[1]
    assert command == _win.interpreter_command(script, 'sess-1') and 'python3' not in offer
    done = subprocess.run([_win.find_git_bash(), '-c', command], capture_output=True, text=True, encoding='utf-8',
                          timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith('Wake-up watcher for session sess-1: disabled')


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


# --- the lock between processes ------------------------------------------------------------------------------------

WORKER = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    import _history, _usage
    worker, rounds, go = int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    root = os.environ['CCUC_TEST_ROOT']
    while not os.path.exists(go):
        time.sleep(0.001)
    for i in range(rounds):
        now = time.time()
        status = {'rate_limits': {'five_hour': {'used_percentage': worker * rounds + i + 1, 'resets_at': now + 3600}}}
        # the history is appended while the lock is held, and held a little longer to make a missing lock show
        _usage.update_file(root + '/usage.json', status, now, root + '/log',
                           lambda merged: (time.sleep(0.003), _history.append(root + '/log', merged, now)))
''')


def test_several_processes_never_lose_a_measurement(tmp_path, monkeypatch):
    workers, rounds = 4, 30
    monkeypatch.setenv('CCUC_TEST_ROOT', str(tmp_path))
    go = tmp_path / 'go'
    processes = [subprocess.Popen([sys.executable, '-c', WORKER, HOOKS, str(n), str(rounds), str(go)],
                                  env={**os.environ, 'PYTHONPATH': WINDOWS_CLI}) for n in range(workers)]
    time.sleep(1.0)
    go.write_text('')
    for process in processes:
        assert process.wait(timeout=60) == 0
    lines = [json.loads(line) for line in (tmp_path / 'log' / 'usage-history.jsonl').read_text().splitlines()]
    values = [line['five'] for line in lines]
    # Usage only goes up inside a window: a lower value after a higher one is an update that was lost to a race
    assert values == sorted(values), 'the history went back: two writers were inside at the same time'
    assert values[-1] == workers * rounds
    assert json.loads((tmp_path / 'usage.json').read_text())['five_hour']['used_percentage'] == workers * rounds
