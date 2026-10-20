# Install, update and uninstall

[README](../README.md) · [All documentation](README.md)

## Install

```sh
git clone https://github.com/inoX-Network/claude-code-usage-clock
cd claude-code-usage-clock
python3 tools/install.py --dry-run     # shows what would change
python3 tools/install.py               # installs all five parts, gates in shadow mode
```

The installer copies the scripts to `~/.claude/hooks/usage-clock/` (the usage report with them, see
[Reports](reports.md#reports-what-was-used)), creates `config.json` there if it does not
exist yet, and adds the hooks and the status line to `~/.claude/settings.json`. Before writing it runs each
hook it installs once as a smoke test and keeps a backup (`settings.json.bak-usage-clock-<timestamp>`). Running it again
updates the entries instead of adding duplicates.

The installer recognises its own entries by the full path of the script in `--target`, not by the file name.
A hook of someone else that happens to run a script with the same name is left alone and named in the output.
If you move `--target`, the entries of the old place stay too; remove them with `--uninstall --target OLD`.

Every run rewrites the entries of the selected components from its options. Two options behave differently
when you leave them out:

- `--mode`: the mode of the entries that are already installed is kept (`shadow` on a first install). The
  output line `Mode:` says which mode applies and where it comes from.
- `--week-ok-until`: it is **not** kept. A run without it removes an earlier approval, and the output says
  so. Pass it again to keep it.
- `--sync-mode`: the mode of an installed sync is kept (`shadow` on a first install of the sync). The output line
  `Sync mode:` says which mode applies and where it comes from. Unlike the gates, the sync keeps its mode and its
  repository in `config.json` (`sync.mode`, `sync.repo`): these two keys are the only ones the installer writes
  there, with a backup, and only when they change.

The sync is not part of the default selection: it is installed only with `--components ...,sync`. Without
`--components`, `--uninstall` removes its entries too and sets `sync.mode` to `off` (the status line pushes from the
mode alone, hooks or not), and `--check` checks it if its entries are installed (and names a `sync.mode` that is not
`off` without them as `FIX`). All of
it: [The sync across machines](sync.md).

Options:

| Option | Meaning |
|---|---|
| `--components statusline,usage-clock,context-reporter,spawn-gate,soft-stop,sync` | Install only some parts. Without it: all but `sync`, which is opt-in (for `--uninstall`: all). The context reporter adds two entries (`UserPromptSubmit` and `PostToolUse`) and says nothing without the status line; the sync adds two (`SessionStart` and `SessionEnd`). |
| `--mode shadow\|enforce` | Mode of the two gates. Without it an existing installation keeps its mode; a first install starts in `shadow`. |
| `--week-ok-until YYYY-MM-DD` | Allow new blocks above the weekly threshold up to and including that day. Not kept: a run without it removes an earlier value (and says so). Needs the component `spawn-gate`. |
| `--replace-statusline` | Replace an existing status line of your own. Without it, yours is left alone, but then nothing writes the shared usage file and every hook sees the usage as `unknown`. Needs the component `statusline`. |
| `--auto-continue` | Also set `autoContinueAtUsageLimit: true` (Claude Code waits and continues after a reset). |
| `--sync-repo OWNER/NAME` | The private GitHub repository of the sync: sets `sync.repo`, checks once that it cannot be read without logging in (public or unclear: exit code 1, no hook added and `settings.json` and `config.json` unchanged) and clones it to `<log_dir>/sync/repo/`. Run again, it lifts the block after a push found the repository public. Needs the component `sync`. See [the sync](sync.md#install). |
| `--sync-mode off\|shadow\|on` | Mode of the sync, in `config.json`. Without it an installed sync keeps its mode; a first install starts in `shadow` (commits locally, pushes nothing). Needs the component `sync`. |
| `--settings PATH`, `--target DIR` | Other locations. |
| `--dry-run` | Show what would change and write nothing. |
| `--uninstall` | Remove the entries again; files stay. See [Uninstall completely](#uninstall-completely). |
| `--check` | Check an existing installation and change nothing: the entries in `settings.json`, the installed files against this checkout, `config.json`, the smoke test, the age of the usage numbers and the log folder; with the sync its repository, clone (there and clean), remote, visibility (from the cache of the push, with its age), last success, backoff and last error, and the size of the clone (`NOTE` from 500 MB). Every line says `OK`, `FIX` (exit code 1) or `NOTE`. Takes only `--settings`, `--target` and `--components`. |

Restart Claude Code (or open `/hooks`) after installing.

On Windows, run `python tools\install.py` instead of `python3 tools/install.py`; the installer writes the commands for
Git Bash with the Python that runs it ([`windows/README.md`](../windows/README.md)).

### Update

```sh
cd claude-code-usage-clock
git pull
python3 tools/install.py --check     # names what is out of date
python3 tools/install.py             # copies the new version, keeps the mode of the gates and of the sync
```

Every run copies all runtime files, so an installed sync gets its new version too, and its entries stay. Only
`--week-ok-until` has to be given again (see above). If you use the desktop plugin, update it as well, then restart
Claude Code:

```sh
claude plugin marketplace update usage-clock
claude plugin update usage-clock-display@usage-clock
```

What changed in each release: [CHANGELOG.md](../CHANGELOG.md) and the
[release notes](https://github.com/inoX-Network/claude-code-usage-clock/releases).

### Uninstall completely

`python3 tools/install.py --uninstall` removes the hook entries and the status line entry that point to the
scripts in `--target` (with a backup of `settings.json`). It deletes no files and does not undo everything else
the installer did. What stays, and how to remove it:

| What stays | How to remove it |
|---|---|
| The `--target` folder (default `~/.claude/hooks/usage-clock/`) with the scripts and your `config.json` | `rm -r ~/.claude/hooks/usage-clock` (or the folder you gave with `--target`) |
| The log folder (`log_dir`, default `~/.cache/claude-usage-clock/`): `log.jsonl`, the per-agent counters, the per-session context files (`context/`), the context split the desktop plugin writes after the first answer (`context-breakdown/`), the measurement history (`usage-history.jsonl` and `usage-history.jsonl.1`), the report archive (`usage-days.jsonl`) and the lock of the usage file (`rate-limit.json.lock`) | `rm -r ~/.cache/claude-usage-clock` |
| The shared usage file `~/.claude/rate-limit.json` (the `usage_file` in your config); versions before October 2026 also left its lock file next to it | `rm ~/.claude/rate-limit.json ~/.claude/rate-limit.json.lock` |
| `autoContinueAtUsageLimit: true`, if you installed with `--auto-continue` | Remove the key from `settings.json` by hand, or set it to `false`. |
| Your earlier status line, if you installed with `--replace-statusline`: it is **not** restored | Copy it back from the backup `settings.json.bak-usage-clock-<timestamp>` that the installing run printed (the `Undo:` line). |
| The backups `settings.json.bak-usage-clock-*` (and `config.json.bak-usage-clock-*`, if the installer set the sync keys) | Delete them when you no longer need them. |
| The local clone of the sync, if you installed it: `<log_dir>/sync/` (the clone in `repo/`, its state, the cache of the visibility check and the machine id) | Part of the log folder: `rm -r ~/.cache/claude-usage-clock/sync`, or the whole log folder as above. Commits of shadow mode that were never pushed go with it. |
| The data of this machine in the sync repository on GitHub (`machines/<machine id>/`): the uninstall never touches the remote | Delete that folder in the repository yourself (a commit that removes it), or the whole repository on GitHub. |

If you use `--components`, the uninstall removes only the components you name. With the sync among them it also sets
`sync.mode` in `config.json` to `off` (backup `config.json.bak-usage-clock-<timestamp>`), the only key it changes there:
the status line would push on otherwise.

## Recipe: wake up when the window resets

Claude Code can schedule a one-time prompt in a running session. Ask it, for example:

> Read `~/.claude/rate-limit.json`, take `five_hour.resets_at`, and schedule a one-time prompt two minutes
> after that time: "The usage window has reset. Continue with the next step from where you stopped."

Claude then uses its scheduling tool (`CronCreate`, one-shot) or `/loop`. The session has to stay open; a
one-shot task survives `--resume`. If you only want Claude Code to wait and continue by itself, install with
`--auto-continue` instead.
