"""The sync core: whether and when this machine pushes its usage data to the sync repo, and the frame of every run.

The core decides, it does not sync. The git work is a function handed in (`Runner`: the push of 0034-SYNC-PUSH,
the pull of 0034-SYNC-REPORT). It runs under the lock with a deadline and raises SyncError with a fixed category when
it fails; whatever else it raises counts as `internal`.

- **State** `<log_dir>/sync/state.json`: `last_attempt`, `last_success`, `backoff_level`, `next_due`, `last_error`
  (times ISO 8601 UTC, `last_error` null or one of CATEGORIES). Folder 0700, files 0600 whatever the umask, written
  atomically, never through a link. A missing file is an empty state; a broken one, too, with one log line.
- **Triggers:** T1 the status line (`statusline_tick`: no network, no lock, no waiting), T2 the hook SessionEnd
  (`session_end`), T3 the hook SessionStart (`session_start`), T4 a manual `sync.py push --now`.
- **Lock** `<log_dir>/sync/sync.lock`, taken without waiting through `_usage.locked`; push and pull share it. Held
  elsewhere: the run ends with exit 0 and changes nothing.
- **Backoff:** after a failure `next_due = now + min(interval * 2^level, backoff_max_h) +- 20 %`; a success resets it
  and waits one interval +- 20 %. After `visibility` no push starts any more until the installer clears it
  (`clear_visibility`), not even a manual one.
- **Git** runs through `run_command` (or `Run.git`): never interactive (`git_env`), and the whole tree of the child is
  ended when the deadline of the run is reached (category `timeout`).

`sync.mode` `off` makes every entry point do nothing; `shadow` and `on` run the core alike (the push tells them apart).
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import random
import stat
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Iterator

import _context_state
import _hookio
import _plat
import _usage

SYNC_DIR = 'sync'
STATE_FILE = 'state.json'
LOCK_FILE = 'sync.lock'
READ_ERROR_FILE = 'read-error.logged'   # when the status line last logged an unreadable state
CATEGORIES = ('visibility', 'auth', 'network', 'rejected', 'conflict', 'timeout', 'internal')
MIN_INTERVAL_MIN = 10
JITTER = 0.2                    # +- 20 % of every wait
MAX_EXPONENT = 30               # 2^30 intervals lie beyond every cap; a larger power would not fit a float
PUSH_TIMEOUT_S = 60
PULL_BACKGROUND_TIMEOUT_S = 30
KILL_WAIT_S = 5                 # how long the output of a killed child is still read
STALE_SUCCESS_S = 24 * 3600     # SessionStart catches up a push after this long without success
READ_ERROR_EVERY_S = 3600       # the status line logs an unreadable state at most this often
MAX_STATE_BYTES = 65536
GIT = 'git'
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sync.py')


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


@dataclasses.dataclass(frozen=True)
class State:
    last_attempt: float | None = None
    last_success: float | None = None
    backoff_level: int = 0
    next_due: float | None = None
    last_error: str | None = None

    @property
    def blocked(self) -> bool:
        """The repo was found public: no push until the installer clears it."""
        return self.last_error == 'visibility'

    def in_backoff(self, now: float) -> bool:
        return self.backoff_level > 0 and self.next_due is not None and now < self.next_due

    def due(self, now: float) -> bool:
        return not self.blocked and (self.next_due is None or now >= self.next_due)

    def to_json(self) -> str:
        return json.dumps({'last_attempt': _text(self.last_attempt), 'last_success': _text(self.last_success),
                           'backoff_level': self.backoff_level, 'next_due': _text(self.next_due),
                           'last_error': self.last_error}, separators=(',', ':'))

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
                   level, _moment(data.get('next_due'), 'next_due'), error)


def _read_small(path: str) -> bytes:
    """The bytes of a small regular file, opened without following a link and without blocking on a named pipe.
    FileNotFoundError if it is missing; any other OSError or ValueError if it cannot be used."""
    descriptor = os.open(path, os.O_RDONLY | _plat.NO_FOLLOW | _plat.O_NONBLOCK | _plat.O_BINARY)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('not a regular file')
        data = os.read(descriptor, MAX_STATE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_STATE_BYTES:
        raise ValueError('too large')
    return data


def read_state(directory: str) -> State:
    """The state in `directory` (`<log_dir>/sync`); an empty one if there is no file. Raises BrokenState otherwise."""
    try:
        return State.from_json(_read_small(os.path.join(directory, STATE_FILE)).decode('utf-8'))
    except FileNotFoundError:
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
    if not stat.S_ISDIR(os.lstat(directory).st_mode):
        raise OSError(f'{directory} is not a folder')      # a link or a file: never followed, never written into
    os.chmod(directory, 0o700)
    return directory


def log(log_dir: str, decision: str, reason: str = '', **extra) -> None:
    """One line in the shared log, hook `sync`."""
    _hookio.write_log_line({'time': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'hook': 'sync',
                            'decision': decision, 'reason': _hookio.mask_quoted(reason)[:300], **extra}, log_dir)


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


def git_env(base: dict | None = None) -> dict[str, str]:
    """The environment of every git call: nothing may ask for credentials. No prompt on the terminal, no window of the
    Git Credential Manager, ssh in batch mode (appended to a GIT_SSH_COMMAND of the user), and no askpass program: git
    asks GIT_ASKPASS, core.askPass and SSH_ASKPASS before the terminal, and an empty GIT_ASKPASS skips all three."""
    env = dict(os.environ if base is None else base)
    env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', GIT_ASKPASS='',
               GIT_SSH_COMMAND=f'{env.get("GIT_SSH_COMMAND") or "ssh"} -o BatchMode=yes')
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
    try:
        out, err = process.communicate(timeout=max(deadline.left(), 0))
    except subprocess.TimeoutExpired:
        _plat.kill_tree(process)
        try:
            process.communicate(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            pass
        raise SyncError('timeout', f'{name} did not finish in time') from None
    return subprocess.CompletedProcess(argv, process.returncode, out.decode('utf-8', 'replace'),
                                       err.decode('utf-8', 'replace'))


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

    def git(self, *args: str, cwd: str | None = None):
        """git with the non-interactive environment, within the deadline of the run."""
        return run_command([GIT, *args], self.deadline, git_env(), cwd)


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


# --- Runs -------------------------------------------------------------------------------------------------------------

def push(log_dir: str, cfg: dict, runner: Runner, forced: bool = False, clock: Callable[[], float] = time.time,
         rand: Callable[[], float] = random.random, timeout_s: float = PUSH_TIMEOUT_S) -> int:
    """One push under the lock: 0 success or nothing to do, 1 failed or blocked by `visibility`.

    Without `forced` (T1, T2) a push inside a backoff ends without a change; `forced` (T4, `--now`) runs past the
    wait and the backoff. Neither runs after `visibility`. Only then `last_attempt` is written, before the runner.
    """
    mode = cfg['sync']['mode']
    if mode == 'off':
        return 0
    directory = ensure_dir(log_dir)
    with try_lock(directory) as held:
        if not held:
            return 0
        state = _read_logged(directory, log_dir)
        now = clock()
        if state.blocked:
            return 1
        if not forced and state.in_backoff(now):
            return 0
        state = dataclasses.replace(state, last_attempt=now)
        write_state(directory, state)
        category, detail = _outcome(runner, Run('push', mode, cfg, log_dir, directory, Deadline(timeout_s), now))
        end, interval = clock(), interval_s(cfg, log_dir)
        if category is None:
            write_state(directory, succeeded(state, end, interval, rand))
            log(log_dir, 'success', action='push', mode=mode)
            return 0
        write_state(directory, failed(state, category, end, interval, cfg['sync']['backoff_max_h'], rand))
        log(log_dir, 'failure', detail, action='push', mode=mode, category=category)
        return 1


def pull(log_dir: str, cfg: dict, runner: Runner, background: bool, clock: Callable[[], float] = time.time) -> int:
    """One pull under the lock: 30 s in the background (T3), else sync.pull_timeout_s. 0 success or lock held
    elsewhere, 1 failed. The push state is not touched (no backoff for pulls)."""
    mode = cfg['sync']['mode']
    if mode == 'off':
        return 0
    directory = ensure_dir(log_dir)
    with try_lock(directory) as held:
        if not held:
            return 0
        seconds = PULL_BACKGROUND_TIMEOUT_S if background else cfg['sync']['pull_timeout_s']
        category, detail = _outcome(runner, Run('pull', mode, cfg, log_dir, directory, Deadline(seconds), clock()))
    if category is None:
        log(log_dir, 'success', action='pull', mode=mode)
        return 0
    log(log_dir, 'failure', detail, action='pull', mode=mode, category=category)
    return 1


def command(*arguments: str) -> list[str]:
    """The command line of sync.py next to this file, run by the Python that runs now."""
    return [sys.executable, SCRIPT, *arguments]


def _log_read_error(log_dir: str, directory: str, now: float, problem: BrokenState) -> None:
    """For the status line: one log line per READ_ERROR_EVERY_S at most, remembered in a file of its own."""
    try:
        logged = _moment(_read_small(os.path.join(directory, READ_ERROR_FILE)).decode('utf-8'), READ_ERROR_FILE)
        if 0 <= now - logged < READ_ERROR_EVERY_S:
            return
    except (OSError, ValueError):    # no file yet, or a broken one: log
        pass
    log(log_dir, 'state_unreadable', f'{STATE_FILE} counts as empty: {problem}')
    try:
        _context_state.write_private(directory, READ_ERROR_FILE, _usage._measured_at(now))
    except OSError:
        pass


def statusline_tick(log_dir: str, cfg: dict, now: float, start: Callable | None = None) -> bool:
    """T1: starts `sync.py push` detached if it is due; True if it did. Reads one small file: no network, no lock,
    no waiting. `start` replaces _plat.start_detached (tests)."""
    if cfg['sync']['mode'] == 'off':
        return False
    directory = os.path.join(log_dir, SYNC_DIR)
    try:
        state = read_state(directory)
    except BrokenState as problem:
        _log_read_error(log_dir, directory, now, problem)
        state = State()
    if not state.due(now):
        return False
    (start or _plat.start_detached)(command('push'))
    return True


def session_end(log_dir: str, cfg: dict, now: float, start: Callable | None = None) -> bool:
    """T2: starts `sync.py push` detached unless a backoff is running or the push is blocked; True if it did. The
    last session before a shutdown has no status line tick left, so setting next_due would not be enough."""
    if cfg['sync']['mode'] == 'off':
        return False
    try:
        state = read_state(os.path.join(log_dir, SYNC_DIR))
    except BrokenState:
        state = State()      # the push started here logs it
    if state.blocked or state.in_backoff(now):
        return False
    (start or _plat.start_detached)(command('push'))
    return True


def needs_catch_up(state: State, now: float) -> bool:
    """SessionStart brings the next push forward: no success for 24 h or none at all, or a push that was cut off
    (last_attempt after last_success). Never in a backoff, never after `visibility`."""
    if state.blocked or state.in_backoff(now):
        return False
    return (state.last_success is None or now - state.last_success > STALE_SUCCESS_S
            or (state.last_attempt is not None and state.last_attempt > state.last_success))


def session_start(log_dir: str, cfg: dict, now: float, start: Callable | None = None) -> None:
    """T3: next_due = now if a push is to be caught up (only if the lock is free: a run that holds it sets next_due
    itself), then `sync.py pull --background` detached."""
    if cfg['sync']['mode'] == 'off':
        return
    directory = ensure_dir(log_dir)
    with try_lock(directory) as held:
        if held:
            state = _read_logged(directory, log_dir)
            if needs_catch_up(state, now):
                write_state(directory, dataclasses.replace(state, next_due=now))
    (start or _plat.start_detached)(command('pull', '--background'))


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
