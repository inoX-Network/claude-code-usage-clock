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
- Open flags: `NO_FOLLOW` (no symlink as the last part of the path; Windows has no such flag, 0 there),
  `O_NONBLOCK` (a named pipe must not block the open; 0 on Windows) and `O_BINARY` (Windows opens in text mode
  otherwise: line breaks are translated and Ctrl-Z ends the file; 0 elsewhere).
"""
import os
import shlex
import sys

NO_FOLLOW = getattr(os, 'O_NOFOLLOW', 0)
O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
O_BINARY = getattr(os, 'O_BINARY', 0)

if os.name == 'nt':
    try:
        import _win
    except ImportError:      # started from a checkout: the Windows module is in windows/cli/
        sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'windows', 'cli'))
        import _win

    locked = _win.locked
    pread = _win.pread
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
else:
    pread = os.pread
    quote_path = shlex.quote

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

    def script_command(script, *arguments):
        return ' '.join(['python3', shlex.quote(script), *arguments])
