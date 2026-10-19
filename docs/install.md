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

Options:

| Option | Meaning |
|---|---|
| `--components statusline,usage-clock,context-reporter,spawn-gate,soft-stop` | Install only some parts. The context reporter adds two entries (`UserPromptSubmit` and `PostToolUse`) and says nothing without the status line. |
| `--mode shadow\|enforce` | Mode of the two gates. Without it an existing installation keeps its mode; a first install starts in `shadow`. |
| `--week-ok-until YYYY-MM-DD` | Allow new blocks above the weekly threshold up to and including that day. Not kept: a run without it removes an earlier value (and says so). Needs the component `spawn-gate`. |
| `--replace-statusline` | Replace an existing status line of your own. Without it, yours is left alone, but then nothing writes the shared usage file and every hook sees the usage as `unknown`. Needs the component `statusline`. |
| `--auto-continue` | Also set `autoContinueAtUsageLimit: true` (Claude Code waits and continues after a reset). |
| `--settings PATH`, `--target DIR` | Other locations. |
| `--dry-run` | Show what would change and write nothing. |
| `--uninstall` | Remove the entries again; files stay. See [Uninstall completely](#uninstall-completely). |
| `--check` | Check an existing installation and change nothing: the entries in `settings.json`, the installed files against this checkout, `config.json`, the smoke test, the age of the usage numbers and the log folder. Every line says `OK`, `FIX` (exit code 1) or `NOTE`. Takes only `--settings`, `--target` and `--components`. |

Restart Claude Code (or open `/hooks`) after installing.

On Windows, run `python tools\install.py` instead of `python3 tools/install.py`; the installer writes the commands for
Git Bash with the Python that runs it ([`windows/README.md`](../windows/README.md)).

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
| The backups `settings.json.bak-usage-clock-*` | Delete them when you no longer need them. |

If you use `--components`, the uninstall removes only the components you name.

## Recipe: wake up when the window resets

Claude Code can schedule a one-time prompt in a running session. Ask it, for example:

> Read `~/.claude/rate-limit.json`, take `five_hour.resets_at`, and schedule a one-time prompt two minutes
> after that time: "The usage window has reset. Continue with the next step from where you stopped."

Claude then uses its scheduling tool (`CronCreate`, one-shot) or `/loop`. The session has to stay open; a
one-shot task survives `--resume`. If you only want Claude Code to wait and continue by itself, install with
`--auto-continue` instead.
