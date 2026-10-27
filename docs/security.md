# Security: limits, threat model and guard hooks

[README](../README.md) · [All documentation](README.md)

## Limits and threat model

- `rate_limits` is what Claude Code currently passes to the status line. It is documented, but it is not a
  versioned API, and it has been reported missing in some Claude Code versions
  ([#40094](https://github.com/anthropics/claude-code/issues/40094),
  [#45133](https://github.com/anthropics/claude-code/issues/45133)). In all these cases the tools report
  `unknown` rather than wrong numbers.
- The context reporter is only as good as the status line: its numbers come from the per-session file the status
  line writes from the `context_window` input, which is not a versioned API either. If that input is missing, the
  reporter says nothing rather than guess. It reports the main session only; a subagent gets no line.
- The usage file and the counters are ordinary files. A model that can write them can fake them, and a model
  that can edit `settings.json` or the config can switch the gates off. On their own these tools are a
  seatbelt, not a lock. See [Working with a guard hook](#working-with-a-guard-hook).
- A planted special file does not hang a gate: every file the gates read (`config.json`, Claude Code's
  `settings.json`, the usage file, the counters) is opened without waiting and read only if it is a regular file below
  a size limit, and the log gets a line only if it is a regular file. A named pipe or a link to `/dev/zero` reads as
  missing. A hook that still does not answer within the 5 s of its entry: the spawn gate in `enforce` denies
  (`onFailure: "block"`, Claude Code 2.1.295 or newer), the soft stop lets the call through (fail open).
- Agent discovery covers project agents (`.claude/agents` of the project root and its parents up to the repo root,
  as Claude Code reads them) and user agents (`~/.claude/agents`), not plugin or managed agents; the model rule
  reports or denies those unless they are listed in `spawn_rules.exceptions`.
  Skill discovery covers your personal skills and the project folders from the start folder up to the repo
  root, not plugin or managed skills or folders added with `--add-dir`.
- The usage report counts the requests in the transcripts below `~/.claude/projects`. A session in print mode
  (`claude -p`) writes one and is counted like any other (checked with Claude Code 2.1.306: the cost matched the one
  Claude Code logs). A session started with `--no-session-persistence` writes no transcript, so the report cannot
  see it. With the [desktop plugin](../desktop/README.md) installed, `statusline.py` runs in print mode as well, at
  the end of each turn (same version).
- The date and time in the prompt line are your local time and go to the model with every prompt.
- The [sync](sync.md) sends only after you opted in, and only into a repository of yours that was found private: one
  that is public, or whose answer does not say, stops the installation, and a push never writes to one found public.
  A private result is kept for 24 hours, so a repository made public later is found within 24 hours (by the next push
  after that); until then pushes still go to it. Behind a proxy that adds credentials to every request (a Claude
  cloud container) the anonymous read works on a private repository too; the check then asks the GitHub API
  anonymously and counts the repository private only if the answer is `"private": true` for this very repository
  ([the sync](sync.md#install)); an answer that does not say either way pushes nothing and asks again next time. That answer proves that the proxy added credentials (anonymously GitHub answers 404
  for a private repository) and that GitHub calls the repository private at this moment. It does not prove who else
  can read it, it does not limit what those credentials may write elsewhere, and it relies on GitHub and the proxy
  answering truthfully: a proxy that forges answers can make any repository look private, as it could forge the
  anonymous read before. Sessions that are still running on another machine appear only
  after that machine's next push. A push at the end of a session can be cut off when the computer shuts down;
  `SessionStart` catches it up at the next start of the same system. `usage_report.py --sync` pushes and pulls at
  once.
- In [Claude Code on the web](sync.md#claude-code-on-the-web) the skill `/usage-export` clones this repository into
  the VM of the chat, installs the sync there and pushes with the credentials of that session, which the proxy of the
  cloud container adds: they reach whatever the session may reach, not only the sync repository. The export reads one
  more source, the debug log of Claude Code (`/tmp/claude-code.log`, which every process of the VM may write): only
  numbers, times and source names leave it, a line longer than 64 KB or without its line feed is passed over, and a
  planted line can fake the side calls of that chat and the span of its log, nothing else. The skill pins a release
  tag and checks that the clone stands on it, not which commit that is: whoever can move the tag on GitHub chooses
  the code that runs there with those credentials. If that matters to you, run the skill from a fork of yours whose
  tags a ruleset protects.
- The wake-up watcher is as good as the cache clock Claude Code passes to the status line (`prompt_cache`), which
  is not a versioned API either; without it the watcher never fires. It cannot wake a session whose computer is
  asleep or switched off, and it cannot know that the model will follow the wake-up call. Claude Code itself
  compacts an idle session shortly before a 1-hour cache expires (seen with 2.1.290 at about 6 minutes left). The
  default lead of 10 minutes fires before that; with a smaller `lead_min` the watcher may find nothing left to save.

### Working with a guard hook

The two jobs fit together. A guard hook such as
[claude-code-safety-guard](https://github.com/inoX-Network/claude-code-safety-guard) keeps the model from
damaging your system by accident. This repo keeps long runs from breaking off hard near the limit, so their
work is not lost. Neither depends on the other.

To make the gates a lock rather than a seatbelt, let your guard block the model from writing these paths
(the status line itself runs outside the model and keeps writing the usage file):

| Path | Why |
|---|---|
| `~/.claude/settings.json` | Mode and `--week-ok-until` live in the hook commands. |
| `~/.claude/hooks/usage-clock/` | The hooks and `config.json` with the thresholds and the switches of `modules`: a model that writes it can switch a gate `off`, so `modules`, like every key, is only as safe as this row. |
| `~/.claude/rate-limit.json` (your `usage_file`) | The shared usage numbers. |
| `~/.cache/claude-usage-clock/` | Log, per-agent checkpoint counters, context state, the context split of each session, measurement history, report archive, watcher heartbeats and the lock of the usage file (a model that holds it keeps the numbers from updating). |

The paths alone are not enough: two programs of this tool write `config.json` themselves, and a guard that checks only
paths and write commands does not see that. `settings_cli.py set` and `set-repo` (the Settings tab of `/report`, also
as the installed copy `~/.claude/hooks/usage-clock/settings_cli.py`) and `tools/install.py` with `--off`, `--on`,
`--mode`, `--uninstall` or a `--sync-*` option switch a gate off or to shadow with one plain command, and the next
start runs ungated; `settings_cli.py set` also sets a single brake of the spawn gate to `off` or `shadow` or raises its
limit, which the status line does not mark (only the weekly marker disappears with `week_action: off`). For a lock, let your guard block these calls as well, or make `config.json` read-only for your
user (then the Settings tab cannot switch anything either). Whichever you choose, the status line shows
`spawn gate off`, `soft stop off` or `... shadow` while `modules` takes a gate's teeth
([What the status line shows](statusline.md#what-the-status-line-shows)), and `install.py --check` notes it.

With the [sync](sync.md), a model that can write to its clone can fake figures, and they then land on every machine.
Write access to the whole sync folder `<log_dir>/sync/` gives more: the cached visibility result (`visibility.json`:
for up to 24 hours no push checks again whether the repository is public) and the git config of the clone
(`.git/config`: an ssh command or a filter driver that every push in the background then runs). All of it lies below
`<log_dir>` (`~/.cache/claude-usage-clock/sync/`), so the last row above covers it already. Give git access to the
sync repository only (a deploy key or a fine-grained token for that repository, see [the sync](sync.md#install)),
so that even such a command cannot write elsewhere with your account.

List these exact paths. Many guards read path lists literally, so a pattern such as `rate-limit.json*` matches
nothing at all, not even the usage file itself. The temporary file `rate-limit.json.tmp.<pid>` needs no entry: it is
created fresh for every write, a leftover or planted link there is removed first, and it is never written through.
It stays next to the usage file because the final rename is atomic only within one file system.
