"""Everything Windows-specific in one place. The core (hooks/, tools/) calls it only behind `os.name == 'nt'`.

What differs from Linux and macOS, and what is done about it:

- File lock: there is no flock. `locked()` takes msvcrt.locking on one byte far behind the data and waits for
  it like flock(LOCK_EX) does (msvcrt's own waiting gives up after ten seconds). The lock is mandatory on Windows,
  so it must not sit on bytes that a reader without a lock (the reports) still wants to read.
- Replacing a file: os.replace fails with PermissionError while another process has the target open, which is
  normal here (a hook reads a file just as the status line writes it). `replace()` tries again for a short time.
- Paths that come out of hook input arrive with backslashes, small drive letters or in the Git Bash spelling
  (/c/Users/...). `normalize_path()` gives one spelling: forward slashes, drive letter in capitals.
- Hook command lines run in Git Bash, not in cmd or PowerShell. `interpreter_command()` builds one for the Python
  that runs now (the `python3` of Windows is a Microsoft Store placeholder), and `find_git_bash()` finds Git Bash
  the way Claude Code does (variable CLAUDE_CODE_GIT_BASH_PATH, else git.exe on the PATH).
- `pread()`: Windows has no os.pread.
- A terminal: isatty() is true for every character device, the NUL device too (`< NUL`, a scheduled task).
  `is_console()` asks the console itself, so the installer does not put its questions to the null device.
- Child processes of the sync: there are no sessions and no process groups to signal. `start_detached()` starts
  with DETACHED_FLAGS (no console window, not ended with the parent), `kill_tree()` ends a child and everything it
  started with taskkill /T.

Only the standard library. Importable on every system (msvcrt is loaded when the lock is taken), so its pure
functions can be looked at anywhere; only the lock needs Windows.
"""
from __future__ import annotations

import contextlib
import errno
import os
import re
import shlex
import shutil
import sys
import time

# The lock sits at this byte of the file, far behind any data (2 GiB - 1: still inside the 32-bit range that
# msvcrt.locking takes). Locking a region beyond the end of a file is allowed on Windows.
LOCK_OFFSET = 0x7FFFFFFF
LOCK_POLL_S = 0.02
# 19 pauses of 25 ms: about half a second, far below the 5 s timeout of a hook
REPLACE_ATTEMPTS = 20
REPLACE_DELAY_S = 0.025
GIT_BASH_ENV = 'CLAUDE_CODE_GIT_BASH_PATH'
# What Claude Code accepts as the file name of a bash given in the variable (docs, "Environment variables")
BASH_NAMES = ('bash.exe', 'sh.exe', 'bash', 'sh')
# Process creation flags of the Win32 API. subprocess has these names on Windows only; as numbers they can be checked
# on every system.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
# A start that outlives its parent (the status line) and never flashes a console window
DETACHED_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
# A child whose whole tree can be ended after a timeout, without a console window
GROUP_FLAGS = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
KILL_TREE_TIMEOUT_S = 10


# --- Lock and replace ---------------------------------------------------------------------------------------------

def _locking(descriptor: int, mode: int) -> None:
    """msvcrt.locking on the lock byte; the file position is what it was before."""
    import msvcrt
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    os.lseek(descriptor, LOCK_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(descriptor, mode, 1)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


@contextlib.contextmanager
def locked(file, wait: bool = True):
    """Holds an exclusive lock on the open file for the block and waits for it as long as it takes.

    Only "somebody else holds it" is waited for; every other error is raised. With wait=False that case raises
    BlockingIOError at once, like flock with LOCK_NB. What was written to `file` is flushed before the lock goes, so
    the next holder reads it (flock lets go only when the file is closed, which flushes first). A lock of a process
    that dies is released by the system.
    """
    import msvcrt
    descriptor = file.fileno()
    while True:
        try:
            _locking(descriptor, msvcrt.LK_NBLCK)
            break
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EDEADLOCK):
                raise
            if not wait:
                raise BlockingIOError(errno.EWOULDBLOCK, 'the lock is held by another process') from error
            time.sleep(LOCK_POLL_S)
    try:
        yield
    finally:
        try:
            file.flush()
        finally:
            _locking(descriptor, msvcrt.LK_UNLCK)


def replace(source: str, destination: str) -> None:
    """os.replace, tried again for about half a second while Windows refuses it (PermissionError). Other
    errors are raised at once; the last PermissionError is raised when the attempts are used up."""
    for _ in range(REPLACE_ATTEMPTS - 1):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            time.sleep(REPLACE_DELAY_S)
    os.replace(source, destination)


def pread(descriptor: int, size: int, offset: int) -> bytes:
    """os.pread for Windows: moves the file position (a descriptor opened with O_APPEND still appends)."""
    os.lseek(descriptor, offset, os.SEEK_SET)
    return os.read(descriptor, size)


# --- Paths and commands -------------------------------------------------------------------------------------------

_DRIVE = re.compile(r'[A-Za-z]:')
_GIT_BASH_DRIVE = re.compile(r'/([A-Za-z])(?:/|$)')


def normalize_path(path: str) -> str:
    """One spelling for the paths of hook input: forward slashes, a capital drive letter, no doubled or trailing
    separator (except on a drive root), no long-path prefix; `/c/Users/x` (Git Bash) becomes `C:/Users/x`.
    Nothing is resolved: `..` stays, so a caller can still refuse it."""
    text = path.replace('\\', '/')
    if text.startswith('//?/'):      # the long-path prefix, also as //?/UNC/server/share
        text = text[4:]
        if text[:4].upper() == 'UNC/':
            text = '//' + text[4:]
    unc = text.startswith('//') and not text.startswith('///')
    drive = _GIT_BASH_DRIVE.match(text)
    if drive:
        text = f'{drive.group(1)}:/' + text[drive.end():]
    text = re.sub('/{2,}', '/', text)
    if unc:
        text = '/' + text
    if _DRIVE.match(text):
        text = text[0].upper() + text[1:]
    if len(text) > 1 and text.endswith('/') and not re.fullmatch(r'[A-Za-z]:/', text):
        text = text.rstrip('/') or '/'
    return text


def quote(text: str) -> str:
    """The text as one word for bash: in double quotes, which is how the hooks of this machine are written; in
    single quotes if it holds a character that the double quotes would still interpret."""
    return shlex.quote(text) if re.search(r'["$`\\]', text) else f'"{text}"'


def interpreter() -> str:
    """The Python that runs now; from a venv the one it was made from, if that is still there. The hooks need only
    the standard library, and a venv that is deleted or rebuilt must not silence them."""
    if sys.prefix != sys.base_prefix:
        base = os.path.join(sys.base_prefix, 'python.exe')
        if os.path.isfile(base):
            return base
    return sys.executable


def interpreter_command(script: str, *extra: str) -> str:
    """`"<python>" -X utf8 "<script>" <extra>` for a hook command that Git Bash runs. The Python is `interpreter()`,
    with forward slashes. UTF-8 mode, because Claude Code talks UTF-8 on stdin and stdout and the
    default encoding of Windows is not. `extra` are plain arguments (options, a session id), not quoted."""
    return ' '.join([quote(normalize_path(interpreter())), '-X', 'utf8', quote(normalize_path(script)), *extra])


def find_git_bash(env=None, settings_env=None) -> str | None:
    """The bash.exe of Git for Windows, or None.

    Like Claude Code: the variable CLAUDE_CODE_GIT_BASH_PATH (in `settings_env`, the "env" of settings.json, or in
    the process environment) if it names an existing file called bash.exe, sh.exe, bash or sh; else the Git for
    Windows that git.exe on the PATH belongs to (bash.exe is in <root>/bin, git.exe in <root>/cmd, <root>/bin or
    <root>/mingw64/bin). bash.exe of the PATH is not taken: C:\\Windows\\System32\\bash.exe is the launcher of WSL.
    """
    env = os.environ if env is None else env
    configured = [settings_env.get(GIT_BASH_ENV)] if isinstance(settings_env, dict) else []
    for candidate in configured + [env.get(GIT_BASH_ENV)]:
        if candidate and os.path.basename(candidate).lower() in BASH_NAMES and os.path.isfile(candidate):
            return candidate
    git = shutil.which('git', path=env.get('PATH', ''))
    if git is None:
        return None
    folder = os.path.dirname(git)
    for _ in range(3):
        candidate = os.path.join(folder, 'bin', 'bash.exe')
        if os.path.isfile(candidate):
            return candidate
        folder = os.path.dirname(folder)
    return None


def is_console(stream) -> bool:
    """The stream is a console window: GetConsoleMode answers for its handle (not for NUL, a file or a pipe)."""
    import ctypes
    import msvcrt
    try:
        handle = msvcrt.get_osfhandle(stream.fileno())
    except (OSError, ValueError):      # no descriptor of its own (a buffer), or a closed one
        return False
    kernel32 = ctypes.WinDLL('kernel32')
    kernel32.GetConsoleMode.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    return bool(kernel32.GetConsoleMode(handle, ctypes.byref(ctypes.c_ulong())))


# --- Child processes ----------------------------------------------------------------------------------------------

def start_detached(argv: list[str], popen=None):
    """Starts `argv` so that it outlives its parent and never shows a console window: stdin, stdout and stderr on
    the null device, DETACHED_FLAGS. Returns the Popen object. `popen` replaces subprocess.Popen (tests).

    The running Python of a venv becomes `interpreter()`: its python.exe is a launcher that starts the real one as a
    child, and without a console of its own that child gets a new, visible one (CREATE_NO_WINDOW does not reach it)."""
    import subprocess
    if argv and argv[0] == sys.executable:
        argv = [interpreter(), *argv[1:]]
    return (popen or subprocess.Popen)(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, close_fds=True, creationflags=DETACHED_FLAGS)


def kill_tree(process) -> None:
    """Ends the process and every process it started (git starts ssh or git-remote-https): taskkill /T from
    System32, not from the PATH; process.kill() afterwards, in case taskkill could not run."""
    import subprocess
    taskkill = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'taskkill.exe')
    try:
        subprocess.run([taskkill, '/F', '/T', '/PID', str(process.pid)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                       timeout=KILL_TREE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass
