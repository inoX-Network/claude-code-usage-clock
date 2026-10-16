# usage-clock on Windows

Everything Windows-specific lives in this folder. The core (`hooks/`, `tools/`) has a few `os.name == 'nt'` switches and
calls `windows/cli/_win.py` (through `hooks/_plat.py`); it does not spread Windows logic anywhere else.

| Track | Where it runs | State |
|---|---|---|
| [`cli/`](cli/) | Claude Code in the Windows terminal, hooks in Git Bash | Built and tested: Windows 11 (Python 3.14) and `windows-latest` (Python 3.13) in CI. The soft stop is not ported yet, see below. |
| [`wsl/`](wsl/) | Claude Code inside WSL | Nothing to port: it is Linux. The folder holds a script that runs the main suite in WSL as the proof. |
| [`cowork/`](cowork/) | Cowork (Claude desktop app) | Findings only, no code, nothing promised. |

## What you need

- Windows 10 1809 or newer (the minimum of Claude Code itself), tested on Windows 11.
- **Git for Windows.** Claude Code runs hook commands in Git Bash on Windows and only falls back to PowerShell if Git
  for Windows is missing ([hooks reference](https://code.claude.com/docs/en/hooks)). The commands written here are
  for bash, so the installer stops with a clear message without Git Bash. It finds Git Bash the way Claude Code
  does: the variable `CLAUDE_CODE_GIT_BASH_PATH` (in the environment or in the `env` of `settings.json`) if it names
  an existing `bash.exe`, else the Git for Windows that `git.exe` on the `PATH` belongs to. If Git is installed where
  neither leads to it, set the variable.
- Python 3.10 or newer, run as `python tools\install.py` (or `py -3 tools\install.py`). The installer writes the
  interpreter that runs it, with its full path, into the hook commands; the `python3` of Windows is a Microsoft Store
  placeholder and is never used.

```powershell
git clone https://github.com/inoX-Network/claude-code-usage-clock
cd claude-code-usage-clock
python tools\install.py --dry-run     # shows the commands that would be written
python tools\install.py               # installs all five parts, gates in shadow mode
```

The commands look like the hooks that Windows users write by hand:

```
"C:/Users/you/AppData/Local/Programs/Python/Python313/python.exe" -X utf8 "C:/Users/you/.claude/hooks/usage-clock/statusline.py"
"C:/Users/you/.../python.exe" -X utf8 "C:/Users/you/.claude/hooks/usage-clock/usage_clock.py" || true
```

Forward slashes, because bash reads a backslash as an escape; `-X utf8` because Claude Code talks UTF-8 on stdin and
stdout and the Windows default is the ANSI code page; `|| true` as on Linux (Git Bash). A path with `$`, a backtick or
a `"` is put in single quotes. The installer also copies `_win.py` next to the hooks (`windows/cli/_win.py`, the only
file that is not in `hooks/`). `--uninstall` removes the entries and needs no Git Bash. The undo hint after a run is
`cp "<backup>" "<settings.json>"`, which works in Git Bash and in PowerShell.

## How it differs from Linux

| Topic | Linux / macOS | Windows |
|---|---|---|
| File lock | `fcntl.flock`, waits | `msvcrt.locking` on one byte at offset 2 GiB - 1, polled every 20 ms until it is free (msvcrt's own waiting gives up after ten seconds). The byte is far behind the data, because the lock is mandatory on Windows and readers without a lock must still read the file. A process that dies lets go of it. |
| Replacing a file | `os.replace` | Tried again for about half a second while Windows refuses it with `PermissionError` (another process has the target open). The history file is closed before it is rotated: Windows cannot rename an open file. |
| File modes | `0600` files, `0700` folders, `fchmod` | None. `fchmod` does nothing, `makedirs(mode=)` is ignored. The folders are in your profile, which its ACL protects from other users. |
| Owner and write bits of the routine file | must be yours, not writable by group or others | Not checked: Windows has no uid, and it reports every writable file as writable for group and others. |
| Links | `O_NOFOLLOW`, `islink` | There is no `O_NOFOLLOW`. Creating a symlink needs a privilege, **a directory junction does not, and `os.path.islink` does not see a junction**: a junction planted as the log folder would be followed. The link tests are POSIX-only and do not run here. |
| Binary files | no difference | Every file that is read or written byte-wise is opened with `O_BINARY`: otherwise the C runtime translates line breaks and takes Ctrl-Z for the end of the file (a Ctrl-Z in a skill file would hide its `context: fork`). |
| Paths from hook input | forward slashes | Backslashes. `gitdir: C:/...` in a worktree's `.git` file is compared in one spelling (`_win.normalize_path`). |
| Path lists | `:` | `;` (`CCUC_AGENT_DIRS`, `CCUC_SKILL_DIRS`, `CCUC_COMMAND_DIRS`, as `os.pathsep`). |
| Wake-up watcher command | `python3 '<path>' <session>` | `"<python>" -X utf8 "<path>" <session>`, in the offer the context reporter gives. |
| Time zone (`usage_report.py`) | the system zone | The system zone of Windows, through the C runtime. |

Where the core was found to assume a POSIX path, the place and what was done are listed in the pull request that
brought this folder.

## Limits

- **Soft stop (`hooks/soft_stop.py`):** not ported yet. Its checkpoint exception compares paths with `/` and its
  parser for `echo ... >> path` does not accept a backslash path, so near the usage limit a subagent would not be
  allowed to write its checkpoint on Windows. Keep it in shadow mode, or leave it out with
  `--components statusline,usage-clock,context-reporter,spawn-gate`. `tests/test_soft_stop.py` is left out of the
  Windows CI job until then.
- **Workflow `scriptPath`:** the spawn gate reads `workflow({scriptPath: '...'})` only as a literal without a
  backslash. A Windows path has to be written with forward slashes (`'C:/Users/you/wf.js'`); with backslashes the
  gate refuses it (fail closed) and says so.
- **Junctions** are not detected (see Links above).
- **Without Git Bash** (hooks run in PowerShell then) the commands do not work; the installer refuses.
- The reports (`tools/usage_report.py`) print characters outside ASCII. In a console that is fine; to write them to
  a file or pipe them, use `python -X utf8 tools\usage_report.py ... > report.txt`.

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest tests/ windows/cli/tests/ --ignore=tests/test_soft_stop.py -q -rs
```

- `tests/` is the main suite; it runs on every system. About 60 tests need symlinks, FIFOs, file modes, user ids or
  POSIX time zone rules and carry `@pytest.mark.posix_only`: on Windows they are skipped, with exactly one reason
  (`POSIX_ONLY_REASON` in `conftest.py`). The CI check (`.github/check_pytest_output.py`) accepts that reason and no
  other: any other skip fails the run, and on Linux and macOS no skip is allowed at all.
- `windows/cli/tests/` is the Windows track: the lock between two processes, the replace retry against a real open
  file, path and command spelling, Git Bash discovery, the installer on Windows, every hook started for real through
  Git Bash. It is collected on Windows only.
- `windows/wsl/run-suite.ps1` runs the main suite in WSL (see [`wsl/README.md`](wsl/README.md)).
