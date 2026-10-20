"""The sync core: whether and when this machine pushes its usage data to the sync repo, and the frame of every run.

The core decides, it does not sync. The git work is a function handed in (`Runner`: the push of 0034-SYNC-PUSH,
the pull of 0034-SYNC-REPORT). It runs under the lock with a deadline and raises SyncError with a fixed category when
it fails; whatever else it raises counts as `internal`.

- **State** `<log_dir>/sync/state.json`: `last_attempt`, `last_success`, `backoff_level`, `next_due`, `last_error`
  of the push and `last_pull`, the last successful pull (times ISO 8601 UTC, `last_error` null or one of CATEGORIES).
  Folder 0700, files 0600 whatever the umask, written atomically, never through a link. A missing file is an empty
  state; a broken one, too, with one log line.
- **Triggers:** T1 the status line (`statusline_tick`: no network, no lock, no waiting), T2 the hook SessionEnd
  (`session_end`), T3 the hook SessionStart (`session_start`), T4 a manual `sync.py push --now`. A detached start
  happens only if no push may still be running and no start of the same kind is younger than START_GAP_S (a mark in
  the log folder; a mark that cannot be written starts nothing).
- **Lock** `<log_dir>/sync/sync.lock`, taken without waiting through `_usage.locked`; push and pull share it. Held
  elsewhere: the run ends with exit 0 and changes nothing.
- **Backoff:** after a failure `next_due = now + min(interval * 2^level, backoff_max_h) +- 20 %`; a success resets it
  and waits one interval +- 20 %. After `visibility` no push starts any more until the installer clears it
  (`clear_visibility`), not even a manual one. A `next_due` further ahead than any wait the core sets counts as due.
- **Git** runs through `run_command` (or `Run.git`): never interactive (`git_env`), and the whole tree of the child is
  ended when the deadline of the run is reached (category `timeout`). An output that may be endless (the listing of
  a foreign tree) goes through `scan_command`: read piece by piece, the tree ended as soon as the reader has enough. A watchdog ends the whole process if a run
  outlives its deadline anyway, so the lock is never held for good.

`sync.mode` `off` makes every entry point do nothing; `shadow` and `on` run the core alike (the push tells them apart).
"""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import os
import stat
import sys
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

import _context_state
import _hookio
import _plat
import _usage

SYNC_DIR = 'sync'
STATE_FILE = 'state.json'
LOCK_FILE = 'sync.lock'
# In the log folder, not in sync/: they must work when sync/ is broken (a file, a link, no rights)
START_MARKS = {'push': 'sync-push.started', 'pull': 'sync-pull.started'}
READ_ERROR_NOTE = 'sync-read-error.logged'     # when the status line last logged an unreadable state
ERROR_NOTE = 'sync-error.logged'               # when sync.py last logged that it could not use sync/
CATEGORIES = ('visibility', 'auth', 'network', 'rejected', 'conflict', 'timeout', 'internal')
MIN_INTERVAL_MIN = 10
JITTER = 0.2                    # +- 20 % of every wait
MAX_EXPONENT = 30               # 2^30 intervals lie beyond every cap; a larger power would not fit a float
PUSH_TIMEOUT_S = 60
PULL_BACKGROUND_TIMEOUT_S = 30
KILL_WAIT_S = 5                 # how long the output of a killed child is still read
SCAN_CHUNK_BYTES = 65536        # the most scan_command hands on at once
RUN_SLACK_S = 10                # beyond deadline and kill wait: then the watchdog ends the process
START_GAP_S = 120               # at most one detached start of a kind in this time
CONFIG_TIMEOUT_S = 5            # git config --get core.sshCommand
STALE_SUCCESS_S = 24 * 3600     # SessionStart catches up a push after this long without success
NOTE_EVERY_S = 3600             # a repeated problem is logged at most this often
MAX_STATE_BYTES = 65536
GIT = 'git'
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sync.py')

_children: set = set()          # the live children of run_command, for the watchdog
_exit = os._exit                # replaced by tests


class SyncError(Exception):
    """A failed run. `category` is one of CATEGORIES (anything else raises ValueError, which the run then counts as
    `internal`). `detail` goes to the log: a fixed text of the caller, never raw output of git."""

    def __init__(self, category: str, detail: str = ''):
        if category not in CATEGORIES:
            raise ValueError(f'unknown sync error category {category!r}')
        super().__init__(f'{category}: {detail}' if detail else category)
        self.category = category
        self.detail = detail


class BrokenState(ValueError):
    """state.json is there but cannot be used."""


# --- State ----------------------------------------------------------------------------------------------------------

def _moment(value, key: str) -> float | None:
    """An ISO 8601 time with a time zone as Unix seconds; None for null."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{key}: not a time')
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError(f'{key}: no time zone')
    return moment.timestamp()


def _text(moment: float | None) -> str | None:
    return None if moment is None else _usage._measured_at(moment)


def running_window_s() -> float:
    """How long after `last_attempt` a push may still be running: its deadline, the kill wait and the watchdog."""
    return PUSH_TIMEOUT_S + KILL_WAIT_S + RUN_SLACK_S


@dataclasses.dataclass(frozen=True)
class State:
    last_attempt: float | None = None
    last_success: float | None = None
    backoff_level: int = 0
    next_due: float | None = None
    last_error: str | None = None
    last_pull: float | None = None          # the last successful pull; the report pulls again after PULL_GAP_S

    @property
    def blocked(self) -> bool:
        """The repo was found public: no push until the installer clears it."""
        return self.last_error == 'visibility'

    def in_backoff(self, now: float) -> bool:
        return self.backoff_level > 0 and self.next_due is not None and now < self.next_due

    def due(self, now: float) -> bool:
        return not self.blocked and (self.next_due is None or now >= self.next_due)

    def running(self, now: float) -> bool:
        """A push began less than running_window_s() ago and has not succeeded yet: it may still hold the lock."""
        return (self.last_attempt is not None
                and (self.last_success is None or self.last_attempt > self.last_success)
                and 0 <= now - self.last_attempt < running_window_s())

    def to_json(self) -> str:
        return json.dumps({'last_attempt': _text(self.last_attempt), 'last_success': _text(self.last_success),
                           'backoff_level': self.backoff_level, 'next_due': _text(self.next_due),
                           'last_error': self.last_error, 'last_pull': _text(self.last_pull)}, separators=(',', ':'))

    @classmethod
    def from_json(cls, text: str) -> State:
        """Raises ValueError for anything but an object with valid fields; missing fields take their default."""
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError('not an object')
        level = data.get('backoff_level', 0)
        if not isinstance(level, int) or isinstance(level, bool) or level < 0:
            raise ValueError('backoff_level: not a whole number of at least 0')
        error = data.get('last_error')
        if error is not None and error not in CATEGORIES:
            raise ValueError('last_error: not a category')
        return cls(_moment(data.get('last_attempt'), 'last_attempt'), _moment(data.get('last_success'), 'last_success'),
                   level, _moment(data.get('next_due'), 'next_due'), error, _moment(data.get('last_pull'), 'last_pull'))


def max_wait_s(cfg: dict) -> float:
    """The longest wait the core ever sets: the larger of the backoff cap and the interval, plus the jitter."""
    interval = max(cfg['sync']['interval_min'], MIN_INTERVAL_MIN) * 60
    return (1 + JITTER) * max(cfg['sync']['backoff_max_h'] * 3600, interval)


def bounded(state: State, now: float, cfg: dict) -> State:
    """`next_due` further ahead than max_wait_s counts as due (a clock that went back, a file edited by hand): the push
    must not wait for years."""
    if state.next_due is not None and state.next_due - now > max_wait_s(cfg):
        return dataclasses.replace(state, next_due=None)
    return state


def _read_small(path: str, limit: int = MAX_STATE_BYTES) -> bytes:
    """The bytes of a small regular file (at most `limit`), opened without following a link and without blocking on a
    named pipe. FileNotFoundError if it is missing; any other OSError or ValueError if it cannot be used."""
    descriptor = os.open(path, os.O_RDONLY | _plat.NO_FOLLOW | _plat.O_NONBLOCK | _plat.O_BINARY)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('not a regular file')
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(descriptor, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        data = b''.join(chunks)
    finally:
        os.close(descriptor)
    if len(data) > limit:
        raise ValueError('too large')
    return data


def read_state(directory: str) -> State:
    """The state in `directory` (`<log_dir>/sync`); an empty one if there is no file. Raises BrokenState otherwise."""
    try:
        return State.from_json(_read_small(os.path.join(directory, STATE_FILE)).decode('utf-8'))
    except FileNotFoundError:
        # Windows says the same for a path through a file (sync/ as a file), Linux says NotADirectoryError
        try:
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                raise BrokenState(f'{directory} is not a folder') from None
        except FileNotFoundError:
            pass
        return State()
    except (OSError, ValueError, RecursionError) as error:    # UnicodeDecodeError and json errors are ValueErrors
        raise BrokenState(f'{type(error).__name__}: {error}') from error


def write_state(directory: str, state: State) -> None:
    """Atomically, 0600, never through a link. Raises OSError if it cannot be written."""
    if not _context_state.write_private(directory, STATE_FILE, state.to_json()):
        raise OSError(f'{directory} is a link')


def ensure_dir(log_dir: str) -> str:
    """`<log_dir>/sync`, created if needed, mode 0700 whatever the umask. Raises OSError if it is a link or no
    folder."""
    directory = os.path.join(log_dir, SYNC_DIR)
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    if not _plat.is_folder(os.lstat(directory)):
        raise OSError(f'{directory} is not a folder')      # a link or a file: never followed, never written into
    os.chmod(directory, 0o700)
    return directory


def log(log_dir: str, decision: str, reason: str = '', **extra) -> None:
    """One line in the shared log, hook `sync`."""
    _hookio.write_log_line({'time': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'hook': 'sync',
                            'decision': decision, 'reason': _hookio.mask_quoted(reason)[:300], **extra}, log_dir)


def log_hourly(log_dir: str, now: float, note: str, decision: str, reason: str) -> None:
    """One log line per NOTE_EVERY_S at most for a problem that repeats; `<log_dir>/<note>` holds when it was last
    logged. A note that cannot be read is logged again; one that cannot be written is ignored."""
    try:
        logged = _moment(_read_small(os.path.join(log_dir, note)).decode('utf-8'), note)
        if 0 <= now - logged < NOTE_EVERY_S:
            return
    except (OSError, ValueError):    # no note yet, or a broken one: log
        pass
    log(log_dir, decision, reason)
    try:
        _context_state.write_private(log_dir, note, _usage._measured_at(now))
    except OSError:
        pass


def _read_logged(directory: str, log_dir: str) -> State:
    """The state; a broken one counts as empty and writes one log line."""
    try:
        return read_state(directory)
    except BrokenState as problem:
        log(log_dir, 'state_unreadable', f'{STATE_FILE} counts as empty: {problem}')
        return State()


# --- Waits ------------------------------------------------------------------------------------------------------------

def interval_s(cfg: dict, log_dir: str) -> float:
    """sync.interval_min in seconds, at least MIN_INTERVAL_MIN minutes (a smaller value writes one log line)."""
    minutes = cfg['sync']['interval_min']
    if minutes < MIN_INTERVAL_MIN:
        log(log_dir, 'interval_raised', f'sync.interval_min {minutes} is below {MIN_INTERVAL_MIN}, '
                                        f'using {MIN_INTERVAL_MIN}')
        minutes = MIN_INTERVAL_MIN
    return minutes * 60


def jittered(gap: float, rand: Callable[[], float]) -> float:
    """`gap` +- JITTER, uniformly distributed (`rand` gives a number in [0, 1))."""
    return gap * (1 + JITTER * (2 * rand() - 1))


def backoff_gap(interval: float, level: int, max_h: float) -> float:
    """interval * 2^level, capped at max_h hours, before the jitter."""
    return min(interval * 2 ** min(level, MAX_EXPONENT), max_h * 3600)


def succeeded(state: State, now: float, interval: float, rand: Callable[[], float]) -> State:
    return dataclasses.replace(state, last_success=now, backoff_level=0, last_error=None,
                               next_due=now + jittered(interval, rand))


def failed(state: State, category: str, now: float, interval: float, max_h: float,
           rand: Callable[[], float]) -> State:
    """One more level of backoff. After `visibility` next_due is left as it is: the push stays blocked anyway."""
    level = state.backoff_level + 1
    if category == 'visibility':
        return dataclasses.replace(state, backoff_level=level, last_error=category)
    return dataclasses.replace(state, backoff_level=level, last_error=category,
                               next_due=now + jittered(backoff_gap(interval, level, max_h), rand))


# --- Lock -------------------------------------------------------------------------------------------------------------

@contextlib.contextmanager
def try_lock(directory: str) -> Iterator[bool]:
    """True for the block if this process holds `<directory>/sync.lock`, False at once if another one does."""
    descriptor = os.open(os.path.join(directory, LOCK_FILE), os.O_RDWR | os.O_CREAT | _plat.NO_FOLLOW | _plat.O_BINARY,
                         0o600)
    with os.fdopen(descriptor, 'r+b') as file, contextlib.ExitStack() as stack:
        _plat.fchmod(descriptor, 0o600)
        try:
            stack.enter_context(_usage.locked(file, wait=False))
        except BlockingIOError:
            yield False
            return
        yield True


# --- Running git ------------------------------------------------------------------------------------------------------

class Deadline:
    """The end of the total time of a run, on a monotonic clock."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def left(self) -> float:
        return self._end - self._clock()

    def check(self) -> None:
        """Raises SyncError('timeout') once the time is used up; a runner calls it between its steps."""
        if self.left() <= 0:
            raise SyncError('timeout', 'the run took too long')


def _ssh_command(env: dict, deadline: Deadline | None) -> str | None:
    """The ssh command git would use: GIT_SSH_COMMAND, else core.sshCommand of the git config, else `ssh`. None if
    GIT_SSH names a program (plink, a wrapper): a GIT_SSH_COMMAND would replace it, and it takes no ssh options."""
    if env.get('GIT_SSH_COMMAND'):
        return env['GIT_SSH_COMMAND']
    if env.get('GIT_SSH'):
        return None
    seconds = CONFIG_TIMEOUT_S if deadline is None else min(CONFIG_TIMEOUT_S, deadline.left())
    try:
        done = run_command([GIT, 'config', '--get', 'core.sshCommand'], Deadline(seconds), env)
    except SyncError:      # no git, no time: git itself will fail the same way
        return 'ssh'
    configured = done.stdout.strip()
    return configured if done.returncode == 0 and configured else 'ssh'


def git_env(base: dict | None = None, deadline: Deadline | None = None) -> dict[str, str]:
    """The environment of every git call: nothing may ask for credentials. No prompt on the terminal, no window of the
    Git Credential Manager, no askpass program (git asks GIT_ASKPASS, core.askPass and SSH_ASKPASS before the terminal,
    and an empty GIT_ASKPASS skips all three), and ssh in batch mode: `-o BatchMode=yes` appended to the ssh command
    git would use (see _ssh_command), which GIT_SSH_COMMAND then carries. Reading core.sshCommand runs git config."""
    env = dict(os.environ if base is None else base)
    env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', GIT_ASKPASS='')
    ssh = _ssh_command(env, deadline)
    if ssh is not None:
        env['GIT_SSH_COMMAND'] = f'{ssh} -o BatchMode=yes'
    return env


def run_command(argv: list[str], deadline: Deadline, env: dict | None = None, cwd: str | None = None):
    """Runs `argv` without stdin until it ends or the deadline is reached (CompletedProcess, output as text).

    At the deadline the child and everything it started are ended (`_plat.kill_tree`): SyncError('timeout'). A program
    that cannot be started (no git): SyncError('internal').
    """
    import subprocess
    name = os.path.basename(argv[0])
    if deadline.left() <= 0:
        raise SyncError('timeout', f'no time left for {name}')
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, cwd=cwd, **_plat.GROUP_OPTIONS)
    except OSError as error:
        raise SyncError('internal', f'{name} could not be started ({type(error).__name__})') from error
    _children.add(process)
    try:
        out, err = process.communicate(timeout=max(deadline.left(), 0))
    except subprocess.TimeoutExpired:
        _plat.kill_tree(process)
        try:
            process.communicate(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            pass
        raise SyncError('timeout', f'{name} did not finish in time') from None
    except BaseException:       # Ctrl+C: the child leads a group of its own and would outlive the caller
        _plat.kill_tree(process)
        try:
            process.communicate(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        _children.discard(process)
    return subprocess.CompletedProcess(argv, process.returncode, out.decode('utf-8', 'replace'),
                                       err.decode('utf-8', 'replace'))


def scan_command(argv: list[str], deadline: Deadline, take: Callable[[bytes], bool], env: dict | None = None,
                 cwd: str | None = None) -> int | None:
    """Runs `argv` like run_command, but hands its output to `take` piece by piece (at most SCAN_CHUNK_BYTES) instead
    of keeping it: the memory stays small however long the output. The error output is dropped (a full pipe could
    not block the child). `take` returns False to stop: the child and its tree are ended at once, None. Else the exit
    code.

    A timer ends the tree at the deadline (a read on a pipe has no timeout on Windows): SyncError('timeout'). A
    program that cannot be started: SyncError('internal'). No child outlives the call, unless it survives the kill
    for KILL_WAIT_S.
    """
    import subprocess
    import threading
    name = os.path.basename(argv[0])
    if deadline.left() <= 0:
        raise SyncError('timeout', f'no time left for {name}')
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env=env, cwd=cwd, **_plat.GROUP_OPTIONS)
    except OSError as error:
        raise SyncError('internal', f'{name} could not be started ({type(error).__name__})') from error
    _children.add(process)
    expired: list[bool] = []

    def expire() -> None:
        expired.append(True)
        _plat.kill_tree(process)
    timer = threading.Timer(max(deadline.left(), 0), expire)
    timer.daemon = True
    timer.start()
    try:
        with process.stdout:
            for chunk in iter(functools.partial(process.stdout.read1, SCAN_CHUNK_BYTES), b''):
                if not take(chunk):
                    return None
        process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:      # stopped, or `take` failed: the tree goes now
            _plat.kill_tree(process)
            try:
                process.wait(KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
        _children.discard(process)
    if expired:
        raise SyncError('timeout', f'{name} did not finish in time')
    return process.returncode


@dataclasses.dataclass(frozen=True)
class Run:
    """What a push or pull function gets. It holds the lock while it runs."""
    kind: str               # 'push' or 'pull'
    mode: str               # 'shadow' or 'on'
    cfg: dict               # the whole configuration
    log_dir: str
    directory: str          # <log_dir>/sync
    deadline: Deadline
    started: float          # Unix seconds

    @functools.cached_property
    def env(self) -> dict[str, str]:
        """git_env, worked out once per run."""
        return git_env(deadline=self.deadline)

    def git(self, *args: str, cwd: str | None = None):
        """git with the non-interactive environment, within the deadline of the run."""
        return run_command([GIT, *args], self.deadline, self.env, cwd)


Runner = Callable[[Run], None]


def _outcome(runner: Runner, run: Run) -> tuple[str | None, str]:
    """(None, '') on success, else (category, detail)."""
    try:
        runner(run)
    except SyncError as error:
        return error.category, error.detail
    except Exception as error:
        return 'internal', f'unexpected {type(error).__name__}'
    return None, ''


@contextlib.contextmanager
def _watchdog(seconds: float, give_up: Callable[[], None]) -> Iterator[None]:
    """Calls `give_up` in a thread of its own if the block is still running after `seconds`. A runner that hangs
    outside a child process (the deadline ends only those) must not hold the lock for good.

    `give_up` and the end of the block exclude each other: once it fired, the caller gets past the block only after
    it (never, as it ends the process), so both never write the state at once (same process, same temporary file);
    once the block ended, a late timer does nothing."""
    import threading
    guard, finished = threading.Lock(), []

    def fire() -> None:
        with guard:
            if not finished:
                give_up()
    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        with guard:
            finished.append(True)
        timer.cancel()


def _end_children() -> None:
    for process in list(_children):
        _plat.kill_tree(process)


# --- Runs -------------------------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Outcome:
    """How a push or a pull ended: `ok`, `locked` (another run holds the lock, nothing ran), `off` (sync.mode off),
    for a push also `blocked` (category `visibility`) and `waiting` (not due, or inside a backoff without `forced`),
    or `failed` with the category and the fixed text of its SyncError."""
    status: str
    category: str | None = None
    detail: str = ''


def push(log_dir: str, cfg: dict, runner: Runner, forced: bool = False, if_due: bool = False,
         clock: Callable[[], float] = time.time, rand: Callable[[], float] | None = None,
         timeout_s: float | None = None) -> int:
    """`push_outcome` as an exit code: 0 success or nothing to do, 1 failed or blocked by `visibility`."""
    outcome = push_outcome(log_dir, cfg, runner, forced, if_due, clock, rand, timeout_s)
    return 1 if outcome.status in ('failed', 'blocked') else 0


def push_outcome(log_dir: str, cfg: dict, runner: Runner, forced: bool = False, if_due: bool = False,
                 clock: Callable[[], float] = time.time, rand: Callable[[], float] | None = None,
                 timeout_s: float | None = None) -> Outcome:
    """One push under the lock, and how it ended (`Outcome`).

    `if_due` (T1) runs only if the push is due, so a process that got the lock just after another one pushed ends
    without a change. Without `forced` (T1, T2) a push inside a backoff ends without a change; `forced` (T4, `--now`)
    runs past the wait and the backoff. None runs after `visibility`. Only then `last_attempt` is written, before the
    runner. A runner that outlives the deadline by KILL_WAIT_S + RUN_SLACK_S ends the process: its children are ended,
    the run is recorded as `timeout` and the process exits with 1.
    """
    mode = cfg['sync']['mode']
    if mode == 'off':
        return Outcome('off')
    if rand is None:
        import random
        rand = random.random
    seconds = PUSH_TIMEOUT_S if timeout_s is None else timeout_s
    max_h = cfg['sync']['backoff_max_h']
    directory = ensure_dir(log_dir)
    with try_lock(directory) as held:
        if not held:
            return Outcome('locked')
        now = clock()
        state = bounded(_read_logged(directory, log_dir), now, cfg)
        if state.blocked:
            return Outcome('blocked', 'visibility')
        if (if_due and not state.due(now)) or (not forced and state.in_backoff(now)):
            return Outcome('waiting')
        state = dataclasses.replace(state, last_attempt=now)
        write_state(directory, state)

        def give_up() -> None:
            _end_children()
            try:
                write_state(directory, failed(state, 'timeout', clock(), interval_s(cfg, log_dir), max_h, rand))
            except OSError:
                pass
            log(log_dir, 'failure', 'the run outlived its deadline, the process was ended', action='push', mode=mode,
                category='timeout')
            _exit(1)
        with _watchdog(seconds + KILL_WAIT_S + RUN_SLACK_S, give_up):
            category, detail = _outcome(runner, Run('push', mode, cfg, log_dir, directory, Deadline(seconds), now))
        end, interval = clock(), interval_s(cfg, log_dir)
        if category is None:
            write_state(directory, succeeded(state, end, interval, rand))
            log(log_dir, 'success', action='push', mode=mode)
            return Outcome('ok')
        write_state(directory, failed(state, category, end, interval, max_h, rand))
        log(log_dir, 'failure', detail, action='push', mode=mode, category=category)
        return Outcome('failed', category, detail)


def pull_outcome(log_dir: str, cfg: dict, runner: Runner, background: bool,
                 clock: Callable[[], float] = time.time) -> Outcome:
    """One pull under the lock: 30 s in the background (T3), else sync.pull_timeout_s. A success writes `last_pull`;
    the push state is not touched otherwise (no backoff for pulls). The same watchdog as for a push."""
    mode = cfg['sync']['mode']
    if mode == 'off':
        return Outcome('off')
    directory = ensure_dir(log_dir)
    with try_lock(directory) as held:
        if not held:
            return Outcome('locked')
        seconds = PULL_BACKGROUND_TIMEOUT_S if background else cfg['sync']['pull_timeout_s']

        def give_up() -> None:
            _end_children()
            log(log_dir, 'failure', 'the run outlived its deadline, the process was ended', action='pull', mode=mode,
                category='timeout')
            _exit(1)
        with _watchdog(seconds + KILL_WAIT_S + RUN_SLACK_S, give_up):
            category, detail = _outcome(runner, Run('pull', mode, cfg, log_dir, directory, Deadline(seconds), clock()))
        if category is None:
            write_state(directory, dataclasses.replace(_read_logged(directory, log_dir), last_pull=clock()))
    if category is None:
        log(log_dir, 'success', action='pull', mode=mode)
        return Outcome('ok')
    log(log_dir, 'failure', detail, action='pull', mode=mode, category=category)
    return Outcome('failed', category, detail)


def pull(log_dir: str, cfg: dict, runner: Runner, background: bool, clock: Callable[[], float] = time.time) -> int:
    """`pull_outcome` as an exit code: 0 success, lock held elsewhere or sync off, 1 failed."""
    return 1 if pull_outcome(log_dir, cfg, runner, background, clock).status == 'failed' else 0


def command(*arguments: str) -> list[str]:
    """The command line of sync.py next to this file, run by the Python that runs now."""
    return [sys.executable, SCRIPT, *arguments]


def _start(log_dir: str, kind: str, now: float, argv: list[str], start: Callable | None) -> bool:
    """Starts `argv` detached unless a start of this kind is younger than START_GAP_S. The mark (its modification
    time) is written first; a mark that cannot be written starts nothing, so a broken log folder can never start a
    process on every tick. True if it started."""
    mark = os.path.join(log_dir, START_MARKS[kind])
    try:
        if 0 <= now - os.lstat(mark).st_mtime < START_GAP_S:
            return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        os.makedirs(log_dir, mode=0o700, exist_ok=True)
        descriptor = os.open(mark, os.O_WRONLY | os.O_CREAT | _plat.NO_FOLLOW | _plat.O_BINARY, 0o600)
        try:
            _plat.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        os.utime(mark, (now, now))
    except OSError:
        return False
    (start or _plat.start_detached)(argv)
    return True


def statusline_tick(log_dir: str, cfg: dict, now: float, start: Callable | None = None) -> bool:
    """T1: starts `sync.py push --if-due` detached if the push is due and none may be running; True if it did. Reads
    one small file: no network, no lock, no waiting. `start` replaces _plat.start_detached (tests)."""
    if cfg['sync']['mode'] == 'off':
        return False
    try:
        state = read_state(os.path.join(log_dir, SYNC_DIR))
    except BrokenState as problem:
        log_hourly(log_dir, now, READ_ERROR_NOTE, 'state_unreadable', f'{STATE_FILE} counts as empty: {problem}')
        state = State()
    state = bounded(state, now, cfg)
    if not state.due(now) or state.running(now):
        return False
    return _start(log_dir, 'push', now, command('push', '--if-due'), start)


def session_end(log_dir: str, cfg: dict, now: float, start: Callable | None = None) -> bool:
    """T2: starts `sync.py push` detached unless a backoff is running, the push is blocked or one may still be
    running; True if it did. The last session before a shutdown has no status line tick left, so setting next_due
    would not be enough."""
    if cfg['sync']['mode'] == 'off':
        return False
    try:
        state = read_state(os.path.join(log_dir, SYNC_DIR))
    except BrokenState:
        state = State()      # the push started here logs it
    state = bounded(state, now, cfg)
    if state.blocked or state.in_backoff(now) or state.running(now):
        return False
    return _start(log_dir, 'push', now, command('push'), start)


def needs_catch_up(state: State, now: float) -> bool:
    """SessionStart brings the next push forward: no success for 24 h or none at all, or a push that was cut off
    (last_attempt after last_success). Never in a backoff, never after `visibility`."""
    if state.blocked or state.in_backoff(now):
        return False
    return (state.last_success is None or now - state.last_success > STALE_SUCCESS_S
            or (state.last_attempt is not None and state.last_attempt > state.last_success))


def session_start(log_dir: str, cfg: dict, now: float, start: Callable | None = None) -> None:
    """T3: next_due = now if a push is to be caught up (only if the lock is free: a run that holds it sets next_due
    itself), then `sync.py pull --background` detached, unless a push may still be running."""
    if cfg['sync']['mode'] == 'off':
        return
    directory = ensure_dir(log_dir)
    with try_lock(directory) as held:
        if held:
            state = bounded(_read_logged(directory, log_dir), now, cfg)
            if needs_catch_up(state, now):
                write_state(directory, dataclasses.replace(state, next_due=now))
    try:
        running = read_state(directory).running(now)
    except BrokenState:
        running = False
    if not running:
        _start(log_dir, 'pull', now, command('pull', '--background'), start)


def clear_visibility(log_dir: str, now: float) -> bool:
    """For the installer (`--sync-repo` run again): lifts the block after `visibility`, the next push is due at once.
    False if the lock is held by a run; True otherwise, also if there was nothing to lift."""
    directory = ensure_dir(log_dir)
    with try_lock(directory) as held:
        if not held:
            return False
        state = _read_logged(directory, log_dir)
        if state.blocked:
            write_state(directory, dataclasses.replace(state, last_error=None, backoff_level=0, next_due=now))
        return True
