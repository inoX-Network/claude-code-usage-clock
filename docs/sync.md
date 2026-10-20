# The sync across machines

[README](../README.md) · [All documentation](README.md)

## What it does

You use Claude Code on more than one computer, but your plan limits count for all of them together. The sync puts the
usage data of each machine into a private GitHub repository of yours, so that the report on every machine shows all
of them ([Other machines](usage-report.md#other-machines-the-sync)).

It is **off** unless you install it. A first installation starts it in **shadow mode**: every run exports and commits
to the local clone as it would for a push, but nothing leaves the machine. The log says how many lines and bytes a
push would have sent (`log_summary.py`, section `Sync:`). Switch it on with `--sync-mode on` once that looks right;
the commits gathered in shadow mode go out with the first push.

## Install

Create a **private** repository on GitHub first (for example `anna/usage-sync`), and make sure git can reach it
without asking: the sync never asks for a password. With `https` (the default) sign in once with a credential helper
(the Git Credential Manager of Git for Windows, or `gh auth setup-git`); with `"transport": "ssh"` in `config.json` an
ssh key whose host `github.com` is in your `known_hosts`. Then, on every machine:

```sh
python3 tools/install.py --components sync --sync-repo anna/usage-sync --dry-run     # shows what would change
python3 tools/install.py --components sync --sync-repo anna/usage-sync               # shadow mode
python3 tools/install.py --components sync --sync-mode on                            # later: push for real
```

The installer:

- adds the hooks `SessionStart` and `SessionEnd` (`sync.py session-start` and `session-end`) to `settings.json`,
- sets `sync.repo` and `sync.mode` in `config.json` (the only keys of that file it ever writes, with a backup), and
  says which mode applies and where it comes from (`Sync mode: shadow (default, nothing installed yet)`; a run without
  `--sync-mode` keeps the mode of an installed sync),
- checks once, and shows it, that the repository **cannot be read without logging in**. A public repository, or an
  answer that does not say (no network), stops the installation with exit code 1, and nothing is installed,
- clones the repository to `<log_dir>/sync/repo/` (default `~/.cache/claude-usage-clock/sync/repo/`, folder 0700).
  The size of the repository is checked before anything is checked out, as for every pull: above `sync.max_clone_mb`
  (default 1024 MB) or 100 000 files it stops with exit code 1 and checks nothing out. A folder that is already there
  and is a clone of this repository stays as it is; a folder of anything else stays untouched and stops the run.

Run again with the same options, it changes nothing. If a push once found the repository public, nothing is pushed
any more until you make it private and run the installer again with `--sync-repo`: that run checks again and lifts
the block. `--dry-run` shows all of this without checking or cloning. `install.py --check` shows the sync lines (see
[Install](install.md)): repository, clone, remote, visibility with the age of its last check, last success, backoff
and the last error, and the size of the clone (a `NOTE` from 500 MB on).

Every push and pull runs in the background and never blocks Claude Code: at the end of a session, when the status line
finds it due (every `sync.interval_min`, default 30 minutes, longer after errors), and as a pull when a session starts.
`python3 tools/usage_report.py --sync` pushes and pulls at once.

## What is synced

Only what the report needs, per machine below `machines/<machine id>/` ([the format](report-format.md#the-sync-repository)):

- one line per request: time, token counts, model, the type of a subagent, a key made from the message id (not the id
  itself), the session id and the project label;
- the measurements of the usage history and, with `sync.include_gates` (default `true`), the decisions of the two
  gates (time, gate, decision, mode and the kind of the reason, no text);
- the day archive of the report, with the same project labels;
- `machine.json` (name, platform, time zone offset) and `status.json` (last success, version).

No prompt, answer, file content, command or path leaves the machine, and no host name: an empty `sync.machine_name`
gives `machine-` and 6 characters of a random id.

**Project names.** With `sync.project_labels` `plain` (the default) everyone who can read the private repository sees
the names of your project folders. If you do not want that, use `alias` (names you choose in `sync.project_aliases`)
or `hash` (with `sync.label_salt`, the same on every machine) ([Configuration](configuration.md)).

**Old months.** A machine keeps its requests in the repository for `sync.keep_request_months` (default 3) full months;
older months are compacted to their day lines. For those months the report has only the split each machine made
locally, no allocation across machines.

## Limits

- The sync sends only after you installed it, and only into a private repository of yours.
- Sessions that are still running on another machine appear in the report only after that machine's next push.
- A push at the end of a session can be cut off when the computer shuts down; the next `SessionStart` on the same
  system catches it up.
- All machines should run in one time zone. Otherwise the day figures are cut at each machine's own midnight, and the
  report says so under Notable (`sync_timezone`).
- A model that can write to the clone can fake figures, and they reach every machine. The clone lies below the log
  folder, which a guard hook should block already ([Working with a guard hook](security.md#working-with-a-guard-hook)).

## If you also sync `~/.claude` with a file sync

Some people keep the whole `~/.claude` folder in step between computers with a file sync (Syncthing, "claude-sync").
That works beside this sync if you leave out three things:

- `~/.claude/rate-limit.json`: the usage file belongs to one computer.
- `~/.claude/settings.json`: the hook commands differ between Windows and Linux.
- the log folder (`log_dir`): two machines that share it share one machine id. The default
  `~/.cache/claude-usage-clock` lies outside `~/.claude`; keep it there.

Transcripts of another machine that the file sync put on this one are not counted twice by the report.

## Uninstall

`python3 tools/install.py --uninstall` removes the two hook entries (also without `--components`) and sets `sync.mode`
in `config.json` to `off` (with a backup; the output says so): the status line starts a push from the mode alone, with
or without the hooks. The clone and the data in the repository stay: see
[Uninstall completely](install.md#uninstall-completely). `install.py --check` names a `sync.mode` that is not `off`
while the hooks are missing as `FIX`, whichever `--components` you give it.
