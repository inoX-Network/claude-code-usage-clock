"""The platform switch of the core: the few operations that differ between POSIX and Windows.

The modules of the core call these names and do not ask which system they run on. On Windows they come from
`_win.py` (windows/cli/): next to the hooks in an installation, in windows/cli/ of the checkout otherwise. On
Linux and macOS this file is all there is. `_usage.py` and `_hookio.py` are the exception: the soft stop
copies them with `_config.py`, so they import this file on Windows only and do the POSIX side themselves
(`_usage.locked`, `_usage.replace`).

- `replace(source, destination)`: os.replace; on Windows it tries again while another process holds the target.
- `pread(descriptor, size, offset)`: os.pread; on Windows seek and read.
- `fchmod(descriptor, mode)`: os.fchmod; on Windows it does nothing and pretends nothing: there is no 0600, the
  profile folder is protected by its ACL.
- `normalize_path(path)`: the path as it is; on Windows with forward slashes, a capital drive letter and no
  Git Bash spelling (`/c/Users`).
- `script_command(script, *arguments)`: the command line of a Python script for a hook entry or a hint, as the
  shell that runs it wants it (`python3 'x.py'`; on Windows the current interpreter for Git Bash).
- `native_path(path)`: the path in the spelling of the system (backslashes on Windows).
- `quote_path(path)`: the path as one word for that shell.
- `hook_shell(settings_env)`: the shell that runs hook commands, for the installer's smoke test: `/bin/sh`; on
  Windows the bash.exe of Git Bash, None if there is none (`settings_env` is the "env" of settings.json).
- `start_detached(argv)`: starts a process that outlives the caller and returns at once, stdin, stdout and stderr on
  the null device (the sync core). POSIX: a session of its own; Windows: DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP
  and CREATE_NO_WINDOW, so no console window flashes. Returns the Popen object.
- `GROUP_OPTIONS`: the Popen arguments for a child whose whole tree `kill_tree(process)` can end after a timeout
  (git starts ssh or git-remote-https). POSIX: a session of its own and SIGKILL to its process group; Windows: a
  process group without a window and taskkill /T.
- `is_terminal(stream)`: stdin or stdout is a terminal someone types into (the installer asks questions only then):
  isatty(); on Windows the console must answer as well, as isatty() is true for the NUL device too.
- `is_folder(info)`: an `os.lstat` result is a real folder: no link and, on Windows, no reparse point (a junction is a
  folder for lstat, only its file attributes tell it apart), so nothing that must stay inside is led elsewhere.
- Open flags: `NO_FOLLOW` (no symlink as the last part of the path; Windows has no such flag, 0 there),
  `O_NONBLOCK` (a named pipe must not block the open; 0 on Windows) and `O_BINARY` (Windows opens in text mode
  otherwise: line breaks are translated and Ctrl-Z ends the file; 0 elsewhere).
"""
import os
import shlex
import stat
import sys

NO_FOLLOW = getattr(os, 'O_NOFOLLOW', 0)
O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
O_BINARY = getattr(os, 'O_BINARY', 0)


def is_folder(info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT

if os.name == 'nt':
    try:
        import _win
    except ImportError:      # started from a checkout: the Windows module is in windows/cli/
        sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'windows', 'cli'))
        import _win

    locked = _win.locked
    pread = _win.pread
    start_detached = _win.start_detached
    kill_tree = _win.kill_tree
    GROUP_OPTIONS = {'creationflags': _win.GROUP_FLAGS}
    normalize_path = _win.normalize_path
    script_command = _win.interpreter_command

    def replace(source, destination):
        _win.replace(source, destination)

    def fchmod(descriptor, mode):
        pass

    def native_path(path):
        return os.path.normpath(_win.normalize_path(path))

    def quote_path(path):
        return _win.quote(_win.normalize_path(path))

    def hook_shell(settings_env=None):
        return _win.find_git_bash(settings_env=settings_env)

    def is_terminal(stream):
        return stream is not None and stream.isatty() and _win.is_console(stream)
else:
    pread = os.pread
    quote_path = shlex.quote
    GROUP_OPTIONS = {'start_new_session': True}

    def replace(source, destination):
        os.replace(source, destination)

    def fchmod(descriptor, mode):
        os.fchmod(descriptor, mode)

    def normalize_path(path):
        return path

    def native_path(path):
        return path

    def hook_shell(settings_env=None):
        return '/bin/sh'

    def is_terminal(stream):
        return stream is not None and stream.isatty()

    def script_command(script, *arguments):
        return ' '.join(['python3', shlex.quote(script), *arguments])

    def start_detached(argv):
        import subprocess      # here: the status line imports this module and must stay fast when nothing is due
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True, start_new_session=True)

    def kill_tree(process):
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)      # the child leads its own group (GROUP_OPTIONS)
        except OSError:      # the whole group is gone already
            pass
