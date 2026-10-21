# Install, update and uninstall

[README](../README.md) · [All documentation](README.md)

## Install

```sh
git clone https://github.com/inoX-Network/claude-code-usage-clock
cd claude-code-usage-clock
python3 tools/install.py               # asks y/n for each part, then installs on your yes
```

In a terminal and without any option the installer asks first, one question per part, each with a line that explains
it. Enter takes the answer in brackets: on a first install the safe recommendation, the five parts with the gates in
shadow mode, no auto-continue and no sync; on a second run what is installed now (the components in `settings.json`,
the mode of the gates, `sync.mode`, `sync.repo` and `sync.machine_name` in `config.json`), so that Enter alone changes
nothing. After the brackets each question says where its default comes from: `(current)` is what you have now, a part
that is not installed included (`Sync across computers through a private GitHub repository? [y/N] (current)`),
`(default)` is the safe start for something that has no state yet: everything on a first install, and on a second run
for example the mode of gates that are new in this run. Two gates in different modes say `[y/n, Enter keeps]`
instead. The first line of the assistant explains both words. Each of the eight questions starts with its step,
`Step 1/8` to `Step 8/8`; the number is fixed, so a question that is skipped leaves a gap (no status line: step 2 is
followed by step 4), and a follow-up question (replace, repository, name) carries the number of its question. The
answer `h`, `i`, `?`, `help` or `--help` (in any case) prints a few lines about the question, what it writes into
`settings.json` or `config.json` and a link to its section here, then asks again; it does not count as a wrong answer,
and a wrong answer says so: `Please answer y or n (i = more about this question).` At the repository and the name only
`?`, `help` and `--help` work, as `h` and `i` are answers there; a wrong answer there ends with
`(? = more about this question)`. `Install now?` has no such lines and says only `Please answer y or n.` Then it shows the parts in plain words and the equivalent command line (`Same as: python3 tools/install.py --components ... --mode shadow ...`; on Windows `python tools\install.py` with double quotes for cmd and PowerShell),
asks `Install now? [Y/n]` and runs exactly that command line. The answers are y, yes, n or no in any case; anything
else asks again, three times, then it stops without a change (exit code 2, as on the end of the input; Ctrl+C: 130).

| Question | Becomes |
|---|---|
| Status line | the component `statusline`; with a status line of another tool there, a second question: replace it (`--replace-statusline`, default no) |
| Usage clock | `usage-clock` |
| Context reporter (asked only with the status line) | `context-reporter` |
| Spawn gate, soft stop | `spawn-gate`, `soft-stop`; an installed spawn gate keeps its `--week-ok-until` if it is a date YYYY-MM-DD, any other value is left out, with a line in the summary |
| Let the gates block for real (asked only with a gate) | `--mode enforce`, no: `--mode shadow`; two gates in different modes keep theirs on Enter |
| Continue automatically after a usage limit resets | `--auto-continue` |
| Sync, then the name of your private repository and the name of this computer in the report | `sync` with `--sync-repo owner/name` (checked as the option checks it) and `--sync-name=NAME`, one word, so that a name that starts with a dash stays a name (default: the platform, `linux`, `darwin` or `windows`; on a second run the name in `config.json`; never the host name). No: `--sync-mode off`, so that the next run offers no as `off (your choice)`; with the sync installed `sync` with `--sync-mode off`, so that its hooks, `sync.repo` and `sync.machine_name` stay. Yes to an installed sync that is off: `--sync-mode shadow`, the safe start |

The assistant removes nothing. No to a part that is installed switches it off and leaves it installed: `--off` for the
usage clock, the context reporter and the gates (`modules` in `config.json`, the hook stays in `settings.json`), and
`--sync-mode off` for the sync. The summary shows it as `Spawn gate: off (stays installed; switch it on again here or in
/report → Settings)`. A part switched off is offered with `[y/N] (current)`, the line above its question ends with
`Now: off (your choice).`, and yes switches it on again (`--on`, a gate in the mode of the gate question, which counts
it as new). The gate question comes only when at least one gate stays on. The status line has no switch (the other
parts read its measurement): answered with no it stays, and the summary names `--uninstall --components statusline`.
`autoContinueAtUsageLimit` stays as well. An installed `--week-ok-until` is passed on, so that Enter keeps it. It
does not ask for the thresholds, `--week-ok-until`, `--settings`, `--target` or the transport `ssh`: they stay options
and [configuration](configuration.md#configuration). Any option, or a stdin or stdout that is no terminal (a script, the
CI or a `!` command in Claude Code), and it asks nothing: the options alone decide. Without any option it then installs
the default selection and says so in one line on stderr (the exit code is that of the run). `python3 tools/install.py --dry-run`
shows what would change, and `--components statusline,usage-clock,context-reporter,spawn-gate,soft-stop` installs the
five parts of the default selection without a question.

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
  `Sync mode:` says which mode applies and where it comes from. Unlike the gates, the sync keeps its mode, its
  repository and the name of the machine in `config.json` (`sync.mode`, `sync.repo`, `sync.machine_name`, see
  `--sync-name`). With `--mode` the installer also writes `modules.<gate>` for each gate of the run, and `--on` and
  `--off` set the switches of `modules` ([modules](configuration.md#modules-switch-parts-off)): these are the only keys
  it writes there, with one backup, and only when they change.

The sync is not part of the default selection: it is installed only with `--components ...,sync`. Without
`--components`, `--uninstall` removes its entries too and sets `sync.mode` to `off` (the status line pushes from the
mode alone, hooks or not), and `--check` checks it if its entries are installed (and names a `sync.mode` that is not
`off` without them as `FIX`). All of
it: [The sync across machines](sync.md).

Options:

| Option | Meaning |
|---|---|
| `--components statusline,usage-clock,context-reporter,spawn-gate,soft-stop,sync` | Install only some parts. Without it: all but `sync`, which is opt-in (for `--uninstall`: all). The context reporter adds two entries (`UserPromptSubmit` and `PostToolUse`) and says nothing without the status line; the sync adds two (`SessionStart` and `SessionEnd`). |
| `--mode shadow\|enforce` | Mode of the two gates. Without it an existing installation keeps its mode; a first install starts in `shadow`. Also written to `modules.spawn_gate` / `modules.soft_stop` in `config.json` for the gates of the run, which come before the hook entry. A `config.json` that is a link or cannot be read is not written: a note says so. |
| `--week-ok-until YYYY-MM-DD` | Allow new blocks above the weekly threshold up to and including that day. Not kept: a run without it removes an earlier value (and says so). Needs the component `spawn-gate`. |
| `--replace-statusline` | Replace an existing status line of your own. Without it, yours is left alone, but then nothing writes the shared usage file and every hook sees the usage as `unknown`. Needs the component `statusline`. |
| `--auto-continue` | Also set `autoContinueAtUsageLimit: true` (Claude Code waits and continues after a reset). |
| `--sync-repo OWNER/NAME` | The private GitHub repository of the sync: sets `sync.repo`, checks once that it cannot be read without logging in (public or unclear: exit code 1, no hook added and `settings.json` and `config.json` unchanged) and clones it to `<log_dir>/sync/repo/`. Run again, it lifts the block after a push found the repository public. Needs the component `sync`. See [the sync](sync.md#install). |
| `--sync-name NAME` | The name of this machine in the report of every machine: sets `sync.machine_name` in `config.json` (at most 64 letters, digits, spaces and `. + @ = - _`, not only spaces; anything else: exit code 2, nothing changed). `claude-web` is the name of the chats of Claude Code on the web (`/usage-export`), which the report does not warn about: anywhere else (without `CLAUDE_CODE_REMOTE=true`) it is refused, and the assistant asks again. Without it the name stays as it is; empty gives `machine-` and the start of the machine id, the host name is never used. Needs the component `sync`. |
| `--on COMPONENTS`, `--off COMPONENTS` | Switch installed parts on or off in `config.json`, comma-separated from `usage-clock`, `context-reporter`, `spawn-gate`, `soft-stop`; their hooks stay in `settings.json`. `--off` sets `modules.usage_clock` / `context_reporter` to `false` and `modules.spawn_gate` / `soft_stop` to `off`; `--on` sets `true` and `""` (the mode of the hook entry; with `--mode` and the gate in `--components`, that mode). The same values and the same way of writing as the Settings tab of `/report`. A part must be installed or in `--components` of the same run; the same part in both, or `--off` with a part in `--components`: exit code 2, nothing changed. Without `--components` the run installs nothing and only switches. |
| `--sync-mode off\|shadow\|on` | Mode of the sync, in `config.json`. Without it an installed sync keeps its mode; a first install starts in `shadow` (commits locally, pushes nothing). `shadow` and `on` need the component `sync`; `off` without it only writes `sync.mode` `off` into `config.json` (with the hooks of the sync installed it is an argument error: `--components sync --sync-mode off` switches it off and keeps them, `--uninstall --components sync` removes them). |
| `--settings PATH`, `--target DIR` | Other locations. |
| `--dry-run` | Show what would change and write nothing. |
| `--uninstall` | Remove the entries again; files stay. See [Uninstall completely](#uninstall-completely). |
| `--check` | Check an existing installation and change nothing: the entries in `settings.json` (for the gates with the mode and where it comes from: `from config` or `from settings.json`; a `NOTE` for each part `modules` switches off, and for a gate it sets to `shadow` while its entry says `--mode=enforce`), a `CCUC_CONFIG` that only the `env` of `settings.json` sets (`NOTE`), the installed files against this checkout, `config.json`, the smoke test, the age of the usage numbers and the log folder; with the sync its repository, clone (there and clean), remote, visibility (from the cache of the push, with its age), last success, backoff and last error, and the size of the clone (`NOTE` from 500 MB). Every line says `OK`, `FIX` (exit code 1) or `NOTE`. Takes only `--settings`, `--target` and `--components`. |

Restart Claude Code (or open `/hooks`) after installing.

On Windows, run `python tools\install.py` instead of `python3 tools/install.py`; the installer writes the commands for
Git Bash with the Python that runs it ([`windows/README.md`](../windows/README.md)).

### Update

```sh
cd claude-code-usage-clock
git pull
python3 tools/install.py --check     # names what is out of date
python3 tools/install.py             # Enter at every question keeps what is installed and copies the new version
```

Every run copies all runtime files, so an installed sync gets its new version too, and its entries stay. With
options, only `--week-ok-until` has to be given again (see above); the assistant passes it on. If you use the desktop plugin, update it as well, then restart
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
| The backups `settings.json.bak-usage-clock-*` (all of them) and `config.json.bak-usage-clock-*` (the ten newest, written whenever the installer or the Settings tab changed a key), and the lock `config.json.lock-usage-clock` | Delete them when you no longer need them. |
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
