# usage-clock on Windows

Everything Windows-specific lives in this folder. The core (`hooks/`, `tools/`) has a few `os.name == 'nt'` switches and
calls `windows/cli/_win.py` (through `hooks/_plat.py`); it does not spread Windows logic anywhere else.

| Track | Where it runs | State |
|---|---|---|
| [`cli/`](cli/) | Claude Code in the Windows terminal, hooks in Git Bash | Built and tested: Windows 11 (Python 3.14) and `windows-latest` (Python 3.13) in CI. |
| [`wsl/`](wsl/) | Claude Code inside WSL | Nothing to port: it is Linux. The folder holds a script that runs the main suite in WSL as the proof. |
| [`../desktop/`](../desktop/README.md) | Claude Code in the Claude desktop app | A plugin draws the status line under the prompt, which the app does not show. |
| [`cowork/`](cowork/) | Cowork (cloud, or a Linux VM on the machine) | Findings only, no code, nothing promised. |

![The status line in the Windows terminal: 5h 24 %, week 77 % in red with the weekly stop marker, ctx 460k in yellow, cache 60m](../docs/images/statusline-windows.png)

The status line in the Windows terminal, the same as on Linux: the week has passed the start threshold of the spawn
gate (the picture still shows the old marker `<- weekly limit reached`; it now reads `<- week gate: agents warn`, `ask`
or `blocked`), the context is past 300k.

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
  placeholder and is never used. Started from a venv, it writes the Python the venv was made from (the hooks need only
  the standard library, and a deleted or rebuilt venv must not silence them).

```powershell
git clone https://github.com/inoX-Network/claude-code-usage-clock
cd claude-code-usage-clock
python tools\install.py --dry-run     # shows the commands that would be written
python tools\install.py               # asks for each part; Enter takes the five parts without the sync, gates in shadow mode
```

The installer asks only in a console (PowerShell, cmd, Windows Terminal). In the mintty window of Git Bash without
`winpty` there is none: it asks nothing, installs the default selection and says so in one line. Run it from PowerShell
there, or as `winpty python tools/install.py`, or look first with `--dry-run`. The `Same as:` line of its summary is
quoted for cmd and PowerShell.

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
| Checkpoint paths (soft stop) | absolute, no `..`, parent folder ends with the suffix, also after `realpath` | Only a drive path, in any spelling; no UNC, no `:` after the drive, no part ending in `.` or a space. See [Soft stop](#soft-stop). |
| Path lists | `:` | `;` (`CCUC_AGENT_DIRS`, `CCUC_SKILL_DIRS`, `CCUC_COMMAND_DIRS`, as `os.pathsep`). |
| Wake-up watcher command | `python3 '<path>' <session>` | `"<python>" -X utf8 "<path>" <session>`, in the offer the context reporter gives. |
| Time zone (`usage_report.py`) | the system zone | The system zone of Windows, through the C runtime. |
| Calling the installed report | `python3 ~/.claude/hooks/usage-clock/usage_report.py --week` | `python -X utf8 "%USERPROFILE%\.claude\hooks\usage-clock\usage_report.py" --week`. The installer copies the report next to the hooks; `-X utf8` is for the characters outside ASCII. |
| Detached start of the sync | a session of its own | `DETACHED_PROCESS`, `CREATE_NEW_PROCESS_GROUP` and `CREATE_NO_WINDOW`. The `python.exe` of a venv is a launcher whose child would get a visible console window of its own, so the start takes the Python the venv was made from (`_win.interpreter()`). The process outlives the hook that starts it. |
| Line ends in the sync clone | no conversion | Git for Windows sets `core.autocrlf=true` in its system config. Every git call in the clone runs with `core.autocrlf=false`, so the files hold the same bytes as on Linux. |
| Junctions in the sync folders | a link is refused | A junction (reparse point) is a folder for `os.lstat`, not a link. The sync folder, the clone, its `.git`, the empty hooks folder and the rebase state are refused as a junction as on POSIX as a link (`_plat.is_folder`), so nothing is led out of the log folder. |
| Sync over ssh with plink | `ssh -o BatchMode=yes` | Not supported. The sync appends OpenSSH options (`-o BatchMode=yes`, `-o StrictHostKeyChecking=yes`) to the ssh command, which plink does not take, and with `GIT_SSH` set to PuTTY's `plink.exe` git passes no `-batch` (only to TortoisePlink): a question of plink would wait until the run's time is up and end as `timeout`. Use the OpenSSH of Windows or of Git for Windows, or `"transport": "https"` (the default). |
| Day archive (`usage_report.py --archive`) | cron or a systemd user timer | A task of your own user in the Task Scheduler, no admin rights: `Register-ScheduledTask` with a daily `New-ScheduledTaskTrigger` and `New-ScheduledTaskSettingsSet -StartWhenAvailable` (a PC that was off or in Linux at that time catches up after the next login), run by `pythonw.exe` so that no window opens. Windows has transcripts of its own: the archive of Linux does not hold them. |

Where the core was found to assume a POSIX path, the place and what was done are listed in the pull request that
brought this folder.

## Soft stop

Ported, and installed with the other four parts (`--components statusline,usage-clock,context-reporter,spawn-gate,soft-stop`
is the default). The levels and the Bash forms are the ones of Linux; what differs is which path counts as a checkpoint
path. A Write/Edit/MultiEdit or a Bash append goes through near the usage limit only if its path

- is a drive path: `C:\Users\you\proj\agent-checkpoints\notes.md`, also written `c:\...`, `C:/...`, with mixed
  separators, as `/c/Users/...` (Git Bash; Claude Code reads it as `C:\` too) or with the long-path prefix `\\?\C:\...`;
- is no UNC path (`\\server\share\...`: resolving a network path could outlast the 5 s hook timeout), not root-relative
  (`\Users\...`, on whichever drive is current) and not drive-relative (`C:Users\...`);
- has no `:` after the drive (an alternate data stream such as `notes.md:x`) and no part that ends with `.` or a space,
  `.` and `..` included: Win32 drops such endings when it opens a file, so the path would name another one;
- has a parent folder that ends with `checkpoint_dir_suffix` as written and after `os.path.realpath`, which resolves
  symlinks and junctions: a junction planted as the checkpoint folder is no checkpoint folder. The comparison is
  case-sensitive, stricter than NTFS: `Agent-Checkpoints` does not count for `/agent-checkpoints`;
- has no part that Git Bash follows as a link while Windows does not: a file with `!<symlink>` and the system
  attribute, a shortcut `name.lnk` that stands for `name` (both made by `ln -s` with `MSYS=winsymlinks:sys` or `:lnk`,
  no privilege needed), a WSL symlink or any other reparse point that is neither a symlink nor a junction. `realpath`
  does not see them, and an append in Bash would land where they point.

In a Bash append the path goes unquoted with forward slashes (`C:/...`, `/c/...`) or in single quotes (`'C:\...'`). In
double quotes or unquoted, a backslash is an escape for bash, so such a command is refused. The deny message tells the
model to give the path with the drive and forward slashes.

## Limits

- **Workflow `scriptPath`:** the spawn gate reads `workflow({scriptPath: '...'})` only as a literal without a
  backslash. A Windows path has to be written with forward slashes (`'C:/Users/you/wf.js'`); with backslashes the
  gate refuses it (fail closed) and says so.
- **Junctions** are not detected where the hooks write their own files (see Links above). The soft stop resolves them
  in its checkpoint check.
- **Mapped network drives:** a drive letter that names a network share is a drive path. The soft stop calls
  `realpath` only for a path whose parent folder already ends with the suffix, and an error there makes no checkpoint;
  on a slow network that one check can still take part of the hook's 5 s.
- **Hard links** and a link swapped in between the check and the write are not caught, as on Linux.
- **Without Git Bash** (hooks run in PowerShell then) the commands do not work; the installer refuses.
- The reports (`tools/usage_report.py`) print characters outside ASCII. In a console that is fine; to write them to
  a file or pipe them, use `python -X utf8 tools\usage_report.py ... > report.txt`.

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest tests/ windows/cli/tests/ -q -rs
```

- `tests/` is the main suite; it runs on every system. About 60 tests need symlinks, FIFOs, file modes, user ids or
  POSIX time zone rules and carry `@pytest.mark.posix_only`: on Windows they are skipped, with exactly one reason
  (`POSIX_ONLY_REASON` in `conftest.py`). The CI check (`.github/check_pytest_output.py`) accepts that reason and no
  other: any other skip fails the run, and on Linux and macOS no skip is allowed at all.
- `windows/cli/tests/` is the Windows track: the lock between two processes, the replace retry against a real open
  file, path and command spelling, Git Bash discovery, the installer on Windows, every hook started for real through
  Git Bash, the checkpoint paths of the soft stop. It is collected on Windows only.
- `windows/wsl/run-suite.ps1` runs the main suite in WSL (see [`wsl/README.md`](wsl/README.md)).
