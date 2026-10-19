# Architecture

This document is the contract between the parts. Code and tests follow it; when they disagree, fix one of them.

## Layout

```
hooks/                    installed as one directory, e.g. ~/.claude/hooks/usage-clock/
  statusline.py           statusLine command: shows usage, context and cache clock, merges usage into the shared file
  usage_clock.py          hook UserPromptSubmit: usage line and/or local time as context for the model
  context_reporter.py     hooks UserPromptSubmit + PostToolUse: fill level of the context window as context for the model
  spawn_gate.py           hook PreToolUse (Agent|Workflow|Skill): usage thresholds + model/effort rules
  soft_stop.py            hook PreToolUse (*): soft stop for subagents near the usage limit
  wake_watcher.py         background task, no hook: wakes an idle session shortly before its prompt cache expires
  sync.py                 sync across machines: push [--now], pull [--background], the hooks session-start and session-end
  _config.py              loads config.json next to the scripts, falls back to defaults
  _usage.py               reads and merges the shared usage file
  _history.py             appends the merged usage numbers to the measurement history (<log_dir>/usage-history.jsonl)
  _context_state.py       writes the per-session context file (<log_dir>/context/<session_id>.json); the reporter reads it
  _wake.py                what reporter and watcher share: ALIVE_MIN, REST_AFTER_FIRE_MIN, the heartbeat file
  _hookio.py              hook input/output, JSONL log, directory discovery
  _plat.py                the platform switch: replace, pread, fchmod, path and command spelling, open flags, detached start
                          and ending a process tree (POSIX or Windows)
  _spawn_check.py         model/effort checks for Agent, Workflow scripts and Skill forks
  _sync_core.py           the sync core: state, due check, lock, backoff, timeouts, triggers (<log_dir>/sync/)
  _sync_export.py         the sync export: the new local data appended to the own folder of the sync clone, with a cursor (<log_dir>/sync/cursor.json)
  _transcripts.py         reads the transcripts: requests, token counts, agent types, starts (no hook uses it: the usage report and the sync export do)
  sync_format.py          the sync repository: layout, line formats, machine id, project labels, writing (allowlist), reading (foreign data), compacting; see docs/report-format.md
tools/
  install.py              adds/removes the hooks in a settings.json (per component, backup, dry run); --check checks an installation
  log_summary.py          summarises the JSONL log (installed next to the hooks, with usage_report.py and prices.json: the report runs without the checkout)
  check_agents.py         lists which agent definitions the model rule would report or deny (read only)
  usage_report.py         what was used per day, week, month, project and CLI, from the transcripts: overview, --full, --project/--session, --json (read only; --archive writes the day archive)
  prices.json             API prices per million tokens for usage_report.py, with source and date
windows/                  everything Windows-specific, see windows/README.md
  cli/_win.py             file lock, replace retry, path normalisation, Git Bash commands (copied next to the hooks on Windows)
  cli/tests/              the Windows test track (collected on Windows only)
  wsl/                    script that runs the main suite in WSL; cowork/: findings only
desktop/                  Claude Code plugin (see desktop/README.md): the status line in the desktop app, the usage file from there, /report, the context split
  hooks/                  the hooks module of the plugin (TypeScript) and its tests
.claude-plugin/           the marketplace entry of the plugin
docs/agents.md            how to give agents model and effort, for users and for the model
docs/getting-started.md   the first install, step by step, for people new to a terminal
docs/usage-report.md      the details of the usage report: the overview, the full report, the day archive
docs/report-format.md     what every line of the usage report and every field of its JSON means (units, source, measured or estimated)
examples/wake-routine.md  a start for the file of wake_watcher.routine_file
tests/                    pytest, synthetic data only (the main suite, every system)
.github/check_pytest_output.py   the CI rule: no empty run, no skip (Windows: only the posix_only reason)
config.example.json       every key with its default
```

Scripts import their siblings by inserting their own directory into `sys.path`. No packages, no third-party
runtime dependencies. Python 3.10 or newer, Linux, macOS and Windows (Git Bash). What differs on Windows is in
`hooks/_plat.py` (the switch) and `windows/cli/_win.py`; the rest of the core does not ask which system it runs on.
`_usage.py` and `_hookio.py` do their POSIX side themselves and import `_plat` on Windows only, because the soft stop
copies them with `_config.py`. `soft_stop.py` does the same: it imports `_plat` on Windows only, inside its path check.

## Shared usage file

Default path `~/.claude/rate-limit.json` (config `usage_file`, env override `CCUC_USAGE_FILE`).

```json
{"five_hour": {"used_percentage": 40, "resets_at": 1768399200},
 "seven_day": {"used_percentage": 61, "resets_at": 1768813200},
 "measured_at": "2026-01-14T10:05:00Z"}
```

- A block is valid only with a numeric `used_percentage` (not bool) and a numeric `resets_at` in the future.
- Several Claude Code sessions write the same file. Idle sessions keep redrawing their status line with old
  numbers. Merge rules:
  - Same window (`resets_at` less than 60 s apart): the higher value wins. Usage never goes down within a window.
  - Later window: the new block wins, even with a lower value (after a reset).
  - `measured_at` is renewed only if this session contributed a valid five-hour block that did not lose.
    An idle session can therefore never make old data look fresh.
- Write under an exclusive lock (`fcntl.flock`; on Windows `msvcrt.locking` on one byte far behind the data, polled
  until it is free) on `<log_dir>/<file name>.lock` (the tool's own folder, so a guard that
  protects it covers the lock; whoever holds the lock stops the updates), atomically via `<file>.tmp.<pid>` +
  `os.replace` (next to the file: the rename is atomic only within one file system).
  Write only when something changed. The status line appends to the measurement history under the same lock
  (see Status line).
- A file in any other format counts as empty and is rebuilt on the next status line tick.

Readers classify the measurement as `ok`, `stale` (older than `max_age_min`, or more than 1 min in the future)
or `unknown` (missing, unreadable, no valid five-hour block). `stale` and `unknown` are treated alike by all
decisions.

## Status line

`statusline.py` prints one line: `<model> | 5h 40% ↻14:00 | week 61% [↻Mon 09:00] [<- week gate: agents warn|ask|blocked] |
ctx 231k/1000k 23% | cache 54m`. Every part is left out if its input is missing or invalid (bool, string, NaN, negative): silent
rather than wrong. Exit code 0 always. The line is written as UTF-8 (the dash in `ctx –/1000k`).

- **5h / week:** from `rate_limits`, merged through the shared usage file (see above). Colours
  `statusline.yellow_from` and `red_from`; the marker appears at `thresholds.start_block_week`. With
  `statusline.show_reset` (default true) each part ends with its `resets_at` in local time, in the part's colour:
  `↻HH:MM` for the five hours, `↻<weekday> HH:MM` for the week (without the weekday when the reset is later today),
  the latter only from `yellow_from` on. The
  reset comes from the same block as the value shown (merged, or the session's own if the merge failed);
  missing, broken or not in the future: no reset text.
- **ctx:** from `context_window`. Needs a valid `context_window_size` (at least 1000), otherwise no part.
  `total_input_tokens` (input + cache creation + cache read) is shown in thousands, cut not rounded; the
  percentage is `used_percentage` rounded like the limits, and is left out if it is missing or broken.
  `total_input_tokens` 0 or missing (before the first answer) shows `ctx –/1000k` in gray, never a zero.
  The colour depends on the absolute tokens only: yellow from `statusline.context_yellow_from_k` (default 300),
  red from `context_red_from_k` (default 500), both in thousands; the percentage and the window size do not matter.
- **cache:** from `prompt_cache`, which is missing until the first request. `warm: false` shows `cache cold` in
  red. `warm: true` with a numeric `expires_at` (Unix seconds) shows the minutes left, rounded up: gray
  `cache 54m`, yellow below `statusline.cache_yellow_below_min` (default 5), red `cache cold` once it is 0 or
  less. Warm without a usable `expires_at`, or a `warm` that is not a boolean: no part.

### Context state file

For the context reporter (see `context_reporter.py`), the status line writes `<log_dir>/context/<session_id>.json`:
`{"session_id", "context_window_size", "used_tokens", "measured_at"}` (`measured_at` UTC ISO as in the usage file)
and, only while `prompt_cache.warm` is `true`, `"cache_ttl"` (`"1h"` or `"5m"`) and `"cache_expires_at"` (Unix
seconds, an integer). Both keys are written together or not at all: a cold cache, a `ttl` of any other value or an
`expires_at` that is not a positive finite number leaves them out. Readers that know only the four keys stay valid.

- Written only if `session_id` matches `[A-Za-z0-9_-]{1,128}` (it becomes a file name), `total_input_tokens` is
  above 0 and the window size is valid. Never before the first answer.
- One file per session, so sessions do not mix and no lock is needed. Atomic: `<file>.tmp.<pid>` created with
  `O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600, then `os.replace`. Folder mode 0700. A link as the target is replaced,
  a link as the folder or as the temporary file is never written through.
- Every write removes regular `*.json` and `*.watch` (heartbeat of the wake-up watcher) files in that folder that
  are older than 7 days (by `lstat`; links and folders are skipped; the file just written is kept). The heartbeat
  of a live watcher is rewritten every minute, so only leftovers go.
- Any error is ignored: the line is shown regardless.

### Measurement history

For reports over days, weeks and months the status line appends the merged usage numbers to
`<log_dir>/usage-history.jsonl` (`_history.append`). One JSON line per measurement:

```json
{"t":"2026-01-14T10:05:00Z","five":40,"week":61,"five_resets":1768399200,"week_resets":1768813200}
```

`t` is UTC as `measured_at` in the usage file. `five` and `week` are the `used_percentage` of the two blocks,
`five_resets` and `week_resets` their `resets_at` (Unix seconds). Numbers are taken over as they come, whole or
decimal. A block that is missing or whose reset is not in the future is `null`; with no valid block nothing is
written. No session id, no path, nothing else.

- **Source and lock:** the content `_usage.update_file` returns, not the session's own input. `update_file`
  takes an optional `after_merge(merged)`, which the status line uses for the append, and calls it under the lock
  of the usage file after the merge and the write, also if the file did not change. One writer at a time reads the
  last line and decides, so parallel sessions never write the same measurement twice. If the merge or the write of
  the usage file fails, nothing is appended; an error in the callback is ignored.
- **When:** if `five`, `week`, `five_resets` or `week_resets` differs from the last line, or if the last line is at
  least `history.heartbeat_min` old (default 15) or from the future, or if there is no readable last line. The last
  line is read from the tail of the file (4096 bytes).
- **Rotation:** if the file is larger than `history.max_kb` (default 1024) before a write, it is renamed to
  `usage-history.jsonl.1` (a previous `.1` is replaced; a link there is replaced, not followed) and the new file
  starts with the line being written.
- **Cut-off line:** if the file is not empty and its last byte is not a line break, a line break is written in front of
  the new line (in the same write), so the new line stands alone and readers skip the cut-off one.
- **Files:** opened `O_APPEND|O_NOFOLLOW`, mode 0600 (`fchmod` on every open, so a file of an earlier umask
  becomes private), folder and the log folder above it 0700. A link as the file is never written through, a link as
  the folder is not followed.
- `history.enabled: false` switches the append off. Any error is ignored: the line is shown regardless.

## Configuration

`config.json` next to the scripts (env override `CCUC_CONFIG`). Missing file: defaults. Unreadable file,
wrong types or unknown keys: defaults for the affected keys, one log line, never a crash. Defaults are
listed in `config.example.json`.

`context_reporter.after_tool_from` must be a number from 0 to 100 and `context_reporter.max_age_min` a finite
number above 0; anything else (also NaN and infinity) falls back to the default and writes the usual one log line.
The context reporter itself does not write that line (see `context_reporter.py`), the other hooks do.

`history.heartbeat_min` and `history.max_kb` must be finite numbers above 0; anything else falls back to the
default and writes the usual one log line. `history.enabled` must be a boolean.

`wake_watcher.from_k` must be a finite number of at least 0 (0 = off, the default) and `wake_watcher.lead_min` a
finite number above 0 (default 10); anything else falls back to the default with the usual log line (the watcher
itself loads the config without logging).
`wake_watcher.routine_file` must be text (default `""` = off); `~` is expanded when it is read.

`start_estimate.chars_per_token` must be a finite number above 0 (default 2.3); anything else falls back to the default
with the usual log line. Only `tools/usage_report.py` reads it.

Two switches are deliberately NOT in the config file but in the hook command in `settings.json`:
`--mode=shadow|enforce` (default `shadow`) and `--week-ok-until=YYYY-MM-DD`. Where `settings.json` is
protected against the model, the model cannot switch itself free.

`tools/install.py` writes both. It recognises its own entries by the full path of the script in `--target`
(never by the file name alone; a component with two events, the context reporter, gets one entry per event with the
same command, and each is found, updated and removed on its own), keeps the mode of an installed entry when `--mode` is left out, and drops an
earlier `--week-ok-until` when it is left out (and says so). The command of the spawn gate ends with
`|| { echo …; exit 2; }` in `enforce` (a broken installation denies) and with `|| true` in `shadow` (nothing
may block); the other hooks always end with `|| true`. Every hook entry gets a timeout of 5 s. The status line
entry gets `refreshInterval: 60`; an update of our own entry adds it only if it is missing, and
`--replace-statusline` keeps the `refreshInterval` of the replaced status line.

`sync.mode` is `off`, `shadow` or `on` (anything else: `off`); `sync.interval_min`, `sync.backoff_max_h` and
`sync.pull_timeout_s` must be finite numbers above 0, anything else falls back to the default with the usual log line.
An interval below 10 minutes is valid in the config and raised by the sync core (see Sync core).

## Hooks

| Hook | Event | On internal error |
|---|---|---|
| `usage_clock.py` | UserPromptSubmit | exit 0, no output (a prompt must never be rejected) |
| `context_reporter.py` | UserPromptSubmit, PostToolUse `*` | exit 0, no output (nothing may be rejected or blocked) |
| `soft_stop.py` | PreToolUse `*` | exit 0, allow (fail open) |
| `spawn_gate.py` | PreToolUse `Agent\|Workflow\|Skill` | deny (fail closed), except while checking a Skill: warn; in `shadow` the shadow note instead |

Output formats: deny = `{"hookSpecificOutput": {"hookEventName", "permissionDecision": "deny",
"permissionDecisionReason"}}`; warning = `{"hookSpecificOutput": {"hookEventName", "additionalContext"}}`.
All texts are English. They are addressed to the model, which relays them to the user in the user's language.

In `shadow` mode nothing is denied, asked or warned; every decision is logged with what it would have been. For
deny, ask and warn the gate outputs a warning-format note instead (`_hookio.shadow_note`): `<Gate> (shadow mode,
information only): in enforce mode <subject> would have been denied | would have needed the user's approval |
would have come with a warning. Reason: <reason> Nothing was blocked. <tell> The gates stay in shadow mode until
the user runs tools/install.py --mode enforce.` Spawn gate: subject `this start`, tell `Tell the user in one
sentence.`; soft stop: `this call`, `Mention it in your report.`. The reason is the one enforce would give,
stripped and ended with a full stop unless it ends in `.`, `?` or `!`. The model reads a PreToolUse context next to
the tool result, hence the past tense. Allow stays silent in both modes. A spawn gate that cannot even import its
base modules still only writes to stderr in shadow mode.

### usage_clock.py

One line of context per prompt. `show_usage` and `show_time` switch the two parts independently; both off
means no output. The time part needs no usage file.
Example: `Usage: 5h 40 % (resets 14:00) · week 61 % (resets Mon 19 Jan 09:00) · measured 0 min ago ·
Now: Wed 14 Jan 2026 10:05`. Reset times today show only the time. The threshold note uses the configured
value, not a fixed number. Notes are appended only for a fresh measurement (a stale one shows `STALE`
instead): at or above `start_block_5h` ` — from N % on, no new blocks`; at or above `start_block_week` (needs a
weekly value) ` — week at or above N %: <text>`, with the text chosen by `week_action`: `warn` "agents start with a
warning", `ask` "new agents need the user's approval", `deny` "no new agents without --week-ok-until". Both
notes can stand together, the 5h note first. The hook does not know `--week-ok-until`; the note names the action
that applies without it.

### context_reporter.py

Tells the model how full its context window is. One command, two events: `UserPromptSubmit` (no matcher) and
`PostToolUse` (matcher `*`); the installer component `context-reporter` writes both entries.
Example: `Context: 168k/1000k tokens (17%). Measured, not estimated.`

- **Source:** only the context state file of the status line, `<log_dir>/context/<session_id>.json` (see
  Status line; `log_dir` as for the other hooks: `CCUC_LOG_DIR`, else config). The transcript is never read. The
  hook has no numbers of its own: without the status line, or before its first measurement (no first answer yet),
  nothing is reported.
- **Events:** `UserPromptSubmit` always reports. `PostToolUse` reports only if the exact share
  `used / window * 100` is at or above `context_reporter.after_tool_from` (default 50, a number from 0 to 100;
  compared before rounding, so 499 999 of 1 000 000 tokens stays silent although the line would show 50 %). Any
  other event, or a missing or non-text `hook_event_name`: silent.
- **Text:** `Context: <used>k/<window>k tokens (<percent>%). Measured, not estimated.` Both token numbers are cut to
  thousands, the percentage is `used * 100 / window` rounded with `%.0f` (half to even): the same cut and the
  same rounding as the `ctx` part of the status line.
- **Output:** `{"hookSpecificOutput": {"hookEventName": <event>, "additionalContext": <text>}}`, like `usage_clock.py`.
- **Silent** (no output, exit 0) when any of these is true:
  - the input is not a JSON object, or has an `agent_id` key (a subagent: its context is another one, the file
    describes the main session);
  - `session_id` is missing or does not match `_context_state.SESSION_ID` (it becomes a file name: no path tricks);
  - the file is missing, a link, not a regular file (named pipes are opened without blocking and then refused),
    not valid UTF-8 JSON (only the first 4096 bytes are read; the file is about 150 bytes) or not an object; a link
    as the `context` folder is refused, too;
  - `session_id` in the file differs from the one in the input;
  - `context_window_size` or `used_tokens` is not a finite number (not bool, not NaN), the window is below 1000
    (the status line never writes one), `used_tokens` is not above 0, or more is used than the window holds;
  - `measured_at` is missing, not text, not an ISO time with a time zone, older than `context_reporter.max_age_min`
    (default 15 minutes, a number above 0; exactly that age still counts) or more than one minute in the future
    (the same classification as the usage measurement, `_usage.MIN_AGE_MIN`).
- **Offer of the wake-up watcher:** on `UserPromptSubmit` only (never `PostToolUse`, never with an `agent_id`), if
  `wake_watcher.from_k` is above 0 and `used_tokens` is at or above `from_k * 1000`, one sentence follows the line:
  `Wake-up watcher: start it once now in the background (Bash, run_in_background true, timeout 7200000): python3
  <abs dir>/wake_watcher.py <session_id>` (the path from `__file__`, absolute, through `shlex.quote`). Not offered
  while the heartbeat file says a watcher lives (`watching`, at most `_wake.ALIVE_MIN` = 3 minutes old) or has
  fired (`fired`, at most `_wake.REST_AFTER_FIRE_MIN` = 60 minutes old): the routine the wake-up triggers would
  start the next watcher at once. `ended` (written when a watcher stops after its maximum run time) blocks nothing.
  A heartbeat more than one minute ahead of the clock counts as broken and blocks nothing. If anything in the offer
  fails (missing `_wake.py`, unreadable heartbeat), the context line goes out
  without it. With `from_k` 0 the output is byte for byte the line above.
- **Routine after a wake-up call:** on `UserPromptSubmit` only (never `PostToolUse`, never with an `agent_id`), if
  `wake_watcher.routine_file` is not empty. Claude Code submits the end of a background task as a prompt
  (`<task-notification>` with `<output-file>PATH</output-file>`; seen 14 Oct 2026, the task output itself is not in
  the prompt). For every absolute `PATH` in a prompt that is text and contains `<task-notification>`: open it with
  `O_NOFOLLOW|O_NONBLOCK`, accept only a regular file of the current user, and compare its first bytes with
  `Wake-up call for session <session_id>:` (this session; after `/clear` the call belongs to the old one). On a
  match, read the routine file (`~` expanded) with the same open and these checks, in this order: missing, a link,
  not a regular file, owned by another user, writable by group or others, larger than `ROUTINE_MAX_BYTES` = 4096,
  not UTF-8, empty after stripping. A usable file follows the context line after an empty line, with the header
  `Session-end routine (from wake_watcher.routine_file, presented because the wake-up watcher fired):`; without a
  context line (no fresh state) it is the whole output. An unusable one gives nothing and one log line
  `{"hook": "context_reporter", "decision": "no_routine", "reason": "wake_watcher.routine_file: <reason>"}`, without
  the path. Any other failure gives nothing, silently; the context line is never lost. With the field empty the
  output is byte for byte as without it.
- **On internal error:** exit 0, no output, nothing logged. Config problems are not logged either (the hook runs
  after every tool call); a wrong `context_reporter` value falls back to its default.
- **Limits:** the percentage is computed from the two stored numbers. The status line shows the percentage
  Claude Code passes in `used_percentage`; both agree as long as that is `total_input_tokens / window`.
  Subagents get nothing, and the file is an ordinary file, like the usage file: a model that can write it can
  fake it.

### wake_watcher.py

Not a hook: a script the model starts once in the background (`python3 wake_watcher.py <session_id>`,
`run_in_background`, timeout 7200000) when the context reporter offers it. It polls and ends with one printed
paragraph, which Claude Code delivers to the model as the result of the background task (a finished background
task wakes an idle session; observed, not a documented guarantee). The installer copies it (and `_wake.py`)
with the other runtime files; there is no settings entry and no component. `watch(session_id, cfg, directory, now,
sleep)` is the whole logic with injectable clock and sleep; `main` prints its result and exits 0 (invalid or
missing session id: a message and exit 2).

Constants: `POLL_S` 60, `STALE_MIN` 2, `STALE_POLLS` 3, `MAX_RUN_MIN` 115, `IDLE_FIRE_AT_MAX_RUN_MIN` 20 (all
`wake_watcher.py`), `ALIVE_MIN` 3 and
`REST_AFTER_FIRE_MIN` 60 (`_wake.py`, shared with the reporter).

- **At the start**, in this order: `from_k` is 0: message `disabled`, nothing written. A heartbeat of state
  `watching` at most `ALIVE_MIN` minutes old (a living watcher): message `already running`, nothing written. A
  `fired` or `ended` heartbeat or a broken file does not stop a start.
- **Per poll**, the first one at once: (1) the maximum run time of `MAX_RUN_MIN` minutes is reached: see **Maximum
  run time** below, then end. (2) read
  the state file through `context_reporter.read_state` (no link, no named pipe, size limit) and take it as alive if
  `measured_at` is at most `STALE_MIN` minutes old and not more than a minute in the future. A missing, stale or
  unreadable file counts one missed poll; the third in a row ends with `session ... is no longer active`; a fresh
  file resets the counter. (3) write the heartbeat `watching`. (4) check the fire condition. (5) sleep `POLL_S`.
  The poll that ends the watcher as gone writes no heartbeat. A heartbeat that cannot be written ends the watcher with
  a message (the double-start guard is then unreliable, and a watcher that cannot say it lives must not run).
- **Fire condition:** the state has plausible numbers (as the reporter requires), `used_tokens` is at least
  `from_k * 1000`, `cache_ttl` is `1h` or `5m`, `cache_expires_at` a positive number, and
  `0 < expires_at - now <= min(lead_min, ttl_minutes / 5) * 60`. So the lead is 10 minutes for `1h` and 1 minute
  for `5m` with the default `lead_min`; a bigger `lead_min` does not widen it. Before the message the heartbeat
  `fired` is written; if that fails, the watcher ends without waking anyone.
- **Maximum run time:** a restart message is a model turn; it renews the prompt cache and the idle time starts
  again while the user is gone (seen 14 Oct 2026). So at `MAX_RUN_MIN` the state file is read again with the same
  freshness rule as in the loop (`_fresh_state`), and the watcher wakes instead of restarting if the shared check
  `_warm_cache` passes (plausible numbers, `used_tokens` at least `from_k * 1000`, known `cache_ttl`, positive
  `cache_expires_at`), the cache is still warm (`rest = expires_at - now > 0`) and the idle time
  `ttl_minutes * 60 - rest` is at least `IDLE_FIRE_AT_MAX_RUN_MIN * 60` seconds (`idle_at_limit`; no lead time,
  the watcher cannot wait any longer). Then: heartbeat `fired`, and the wake-up call (facts as in `due()`). If
  that heartbeat cannot be written the call goes out anyway: the last `watching` beat blocks a second offer for
  `ALIVE_MIN`, and a fire must not be lost. In every other case (cold or unknown cache, context below `from_k`,
  idle time shorter, state stale or missing, 5-minute cache, whose idle time is under 5 minutes): heartbeat
  `ended` (a failure to write it is ignored) and the restart message. `due()` and `idle_at_limit()` share the
  check of context and cache clock, not the time window.
- **Message** (English, one paragraph, with the session id): how long the user was away (about TTL minus the
  rest), the rest of the cache in minutes (rounded up), the context in k and the threshold; then `The user is away.
  Run your session-end routine now, without asking (save progress, commit, push, summary), then end the turn. Do
  not start a new watcher.` With `wake_watcher.routine_file` set, the middle reads `without asking: it comes with
  this message (wake_watcher.routine_file); work through every step of it (if it is missing: save progress, commit,
  push, summary)`. The start `Wake-up call for session <session_id>:` is what the context reporter looks for. The
  other endings say why the watcher stopped; the one for a gone session adds `If you are a different session,
  there is nothing to do.` (a task started in one session wakes whichever session is
  current when it ends, also after `/clear`).
- **Heartbeat file** `<log_dir>/context/<session_id>.watch`: `{"session_id", "state": "watching"|"fired"|"ended",
  "at": "<UTC ISO>"}`. Written like the state file (atomic, `O_EXCL|O_NOFOLLOW`, 0600, never through a link, shared code
  `_context_state.write_private`), read with the same guards. The state file's seven-day clean-up removes old
  `*.watch` files, too.
- **Cases:** `/clear` or closing the session: the status line stops writing, the file goes stale, the watcher ends
  after three stale polls, about four to five minutes after the last write, and did not fire in between because
  a stale file never fires (a fire right after `/clear` is possible only within the two minutes in which the old file
  still counts as fresh). Sleeping or suspended computer: the polls stop with the status line; afterwards one stale
  poll is not enough to end, and an expiry that has passed in the meantime is cold and never fires. Cold or
  unknown cache: waits, never fires. The user writes: the status line writes a later `cache_expires_at`, the
  watcher waits on. The 2-hour limit of background tasks: the watcher ends itself after 115 minutes and leaves the
  heartbeat `ended`. Its message arrives as a turn of its own, which runs `UserPromptSubmit`, so that turn already
  offers the next watcher. Before `ended` existed, the last `watching` beat (seconds old) suppressed exactly this
  offer, and a session that went idle right after it stayed unwatched (seen 13 Oct 2026).
- **Limits:** the expiry is as old as the state file (the status line refreshes every `refreshInterval` = 60 s). With
  the 5-minute cache the lead is one poll interval: a poll that comes a little later than 60 s can step over it, with
  the 1-hour cache the window holds ten polls. Two watchers can start within the same second before the first heartbeat
  exists. The model decides whether it follows the wake-up call; its instructions need a session-end routine. Claude
  Code compacts an idle session by itself shortly before a 1-hour cache expires (seen with 2.1.290: compaction began
  about 6 minutes before the expiry and took 2 minutes); the watcher has to fire before that, which the default lead
  of 10 minutes does and a smaller `lead_min` may not.

### spawn_gate.py

1. Usage: 5h at or above `start_block_5h` → deny new agents/workflows. Week at or above `start_block_week`
   → warn if `--week-ok-until` is today or later, else `week_action`: `warn`, `deny`, or `ask`
   (`permissionDecision: "ask"`, reason for the user, no `additionalContext`: the model took an approved start for a block;
   the reason is the short question `WEEK N % · start <what>?`, German when `language` in `~/.claude/settings.json` is
   German, `<what>` cut to name characters because the model writes it). For an Agent start the ask also returns
   `updatedInput`: the whole tool input as it came, with the question's number in front of `description`
   (`dialog_input`), because Claude's phone app shows the description, not the reason. `ask` only where the
   prompt is shown for sure (`permission_mode` default, acceptEdits, plan); any other or a missing mode denies
   unless `ask_verified_in_bypass`. The 5h deny beats an ask. Unknown/stale usage → warn for a single agent,
   deny for more than `unknown_usage_max_agents`.
2. Model rules (unless `model_effort_action` is `off`): the agent type must resolve to a definition with `model:`
   and `effort:` in its frontmatter (project `.claude/agents` before user `~/.claude/agents`, matched by the
   frontmatter `name`). Workflow scripts: every `agent(...)` call needs literal `model` and `effort` options
   or an `agentType` with a complete definition; a value may be `'x'` or `cond ? 'x' : 'y'` with two literals.
   Anything not statically checkable is a finding, and its agents count as unknown. Skill forks (`context: fork`) are checked like agents;
   a missing agent definition only warns (`skill_fork_missing_agent`).
   The frontmatter is read without a YAML library, as far as Claude Code reads it: up to the first `---`
   after the opening line, even in the middle of a value (`description: a---b` ends it after `a`). Lines
   inside a multi-line value are no keys: the indented lines of a block scalar (`|`, `>`) are skipped, and
   inside a quoted value or a `[ ]`/`{ }` list over several lines every line is skipped until it closes. A
   frontmatter that ends while a quote or a list is open is not determinable (deny). For the fork check of a
   skill the part up to the first line that starts with `---` counts, so `context: fork` behind an inner `---`
   is still treated as a fork (checked rather than missed).
   `_spawn_check.py` only judges; `spawn_gate.py` applies `model_effort_action`: `deny` refuses the start with
   the finding, `warn` (default) starts and passes the same finding to the model as a warning. The usage part
   then still counts the agents, an unanalysable script as an unknown number.
3. `exceptions` start without the model check (Agent tool and Skill forks), with a short note. They do not
   apply to `agent(...)` calls inside workflow scripts.

Workflow calls are checked by `script`, else `scriptPath`, else the stored workflow `name`. A child workflow
(`workflow(...)` in the script) is read and checked one level deep; a child that calls `workflow(...)` itself is
not determinable. Limits (`_spawn_check.py`), beyond which a definition or script is not determinable and
therefore a finding: 1 s per search or script check, 5000 entries per agent or skill folder, 256 KB per
definition file, 1 MB (characters) per workflow script.

Discovery: agents as above; skills and commands from `~` and from the start folder (`CLAUDE_PROJECT_DIR`, else
`cwd`) and its parents up to the repo root; a worktree without its own `.claude/skills` is followed by its
main checkout. Not covered: plugins,
managed skills, `--add-dir` and folders below the start folder. `CCUC_AGENT_DIRS`, `CCUC_SKILL_DIRS` and
`CCUC_COMMAND_DIRS` (paths separated by `os.pathsep`) replace the discovery; the tests and the installer's
smoke test use them.

Config switches:

| Key | Effect |
|---|---|
| `model_effort_action` | `warn` (default): findings of the model rule are warnings. `deny`: they refuse the start. `off`: no model check; Agent and Skill fork count as one agent, workflow scripts are only counted for the usage part. |
| `apply_to_main_session: false` | Calls without `agent_id` are not checked at all (neither model nor usage) and not logged. |
| `model_override_in_call` | `model` in an Agent call overrides the frontmatter: `allow`, `warn` (default) or `deny`. |
| `skill_fork_missing_agent` | Fork without a complete agent definition: `warn` (default) or `deny`. |
| `suggested_agents` | Only used in the message of the model rule, as agents to use instead. Empty: a general hint. |
| `model_pattern` | Regular expression, matched against the whole value (`fullmatch`), for the literal `model` of `agent(...)` calls in workflow scripts. Default `sonnet\|opus\|haiku\|fable\|inherit\|claude-[a-z0-9-]+`. Agent definitions only need a non-empty `model:`. Invalid or empty: default, one log line. |
| `effort_values` | Accepted `effort` values, for agent definitions (frontmatter) and workflow calls. Default `low`, `medium`, `high`, `xhigh`, `max`. Empty: default, one log line. |
| `exceptions` | Agent types without the model check. Default `claude-code-guide`, `statusline-setup`: built-in types without a definition file, which the model rule would report or deny every time. Setting the key replaces the default list. |

### soft_stop.py

Applies only to tool calls that carry an `agent_id` (subagents). The main session is never blocked.

1. Completion tools (`StructuredOutput`) and checkpoint writes are always allowed.
2. 5h ≥ `deny`: deny, with instructions to save a checkpoint and finish with a partial result.
3. 5h ≥ `checkpoint_only`: deny everything except checkpoint writes.
4. An agent that has written a checkpoint once must write one again within `calls_without_checkpoint` calls.
5. 5h ≥ `warn`: warning to save a checkpoint soon.
6. Unknown/stale usage: fail open, warn once per agent.

A checkpoint write is a Write/Edit/MultiEdit to an absolute path without `..` whose parent directory (also
after resolving symlinks) ends with `checkpoint_dir_suffix`, or a Bash command that is nothing but an append:
`echo` with quoted arguments `>> file`; `printf` likewise, but only without options and only with `%s`, `%b` and
`%%` in the format (flags, width and precision allowed, `*` not), for example `printf '%s\n' "line" >> file` (an option like `-v` or a numeric conversion like
`%d` can run code in bash and zsh); either of them `| tee -a file`; `cat >> file <<'EOF'`. Double-quoted
arguments must not contain `$`, backticks or `\`.

On Windows (`_is_windows_checkpoint_path`) the path is first brought into the spelling of `_plat.normalize_path`
(forward slashes, capital drive letter, `/c/` and `\\?\` resolved, `..` kept). Only a drive path `X:/...` counts:
no UNC path, nothing root- or drive-relative, no `:` after the drive, no part that ends with `.` or a space (`.` and
`..` included, because Win32 drops such endings when it opens a file). Then the parent directory must end with the
suffix as written and after `realpath` (symlinks and junctions), compared case-sensitively. `_plat` is imported
inside the check, so a failed import fails open like the imports in `main()`.

## Sync core

`_sync_core.py` decides whether and when this machine pushes its usage data to the sync repo, and gives every run
its frame. It does not sync: the git work is a function handed in (`Runner`, called with a `Run`: kind, mode, config,
folders, deadline), the push from 0034-SYNC-PUSH and the pull from 0034-SYNC-REPORT. Until they exist, `sync.py` has
`git_push` and `git_pull`, which fail with the category `internal`: logged and backed off like any failure, never a
success. `sync.mode` `off` (the default) makes every entry point do nothing; `shadow` and `on` run the core alike.

- **State** `<log_dir>/sync/state.json`: `{"last_attempt", "last_success", "backoff_level", "next_due",
  "last_error"}`, times as UTC ISO like `measured_at`, `last_error` null or one of `visibility`, `auth`, `network`,
  `rejected`, `conflict`, `timeout`, `internal`. The folder is 0700 (`chmod` after creating it, so whatever the umask;
  a link or a file there is refused), the files 0600 (`fchmod`), written atomically with `_context_state.write_private`.
  A missing file is an empty state. A broken one (not UTF-8 JSON, not an object, a field of the wrong kind, a time
  without a time zone, larger than 64 KB, no regular file) counts as empty, with one log line `state_unreadable`.
- **Triggers.** T1, the status line: with `sync.mode` not `off` (else `_sync_core` is not even imported) it reads
  `state.json` after printing the line and, if the push is due (`next_due` missing or reached, no `visibility`),
  starts `sync.py push --if-due` detached. No network, no lock, no waiting; an unreadable state is logged at most
  once an hour (`<log_dir>/sync-read-error.logged` holds the time). T2, hook `SessionEnd` (`sync.py session-end`):
  starts `sync.py push` detached unless a backoff runs (`backoff_level` above 0 and `next_due` ahead) or the push is
  blocked. The last session before a shutdown has no status line tick left; a push the shutdown cuts off is caught
  up by T3. T3, hook `SessionStart` (`sync.py session-start`): if there was no success for 24 h or none at all, or
  `last_attempt` is later than `last_success` (a push that was cut off), it sets `next_due` to now, outside a backoff
  and the block, and only if the lock is free; then it starts `sync.py pull --background` detached. T4, `sync.py push
  --now`: past `next_due` and the backoff, never past `visibility` or the lock. Without an installed status line
  there is no T1: pushes then come from T2 and T4 only (the `next_due` of T3 waits for a status line tick).
- **Detached start** (`_plat.start_detached`): stdin, stdout and stderr on the null device; POSIX a session of its
  own, Windows `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`. T1, T2 and T3 start nothing while a
  push may still be running (`last_attempt` later than `last_success` and less than 60 + 5 + 10 s ago, `running`:
  deadline, kill wait and watchdog), and at most one process of a kind per 120 s (`START_GAP_S`): the mark
  `<log_dir>/sync-push.started` or `sync-pull.started` is written (0600, its modification time set to now) before the
  start, and a mark that cannot be written starts nothing. The marks lie in the log folder, not in `sync/`, so a
  broken `sync/` (a file, a link, no rights, a full disk) still cannot start a process on every tick. A mark from the
  future (the clock went back) does not block.
- **A `next_due` too far ahead** counts as due: further than `1.2 * max(backoff_max_h, interval)` from now, which no
  wait of the core ever reaches (a clock that went back, a file edited by hand). Every trigger and every push applies
  it (`bounded`).
- **Lock** `<log_dir>/sync/sync.lock` through `_usage.locked(file, wait=False)` (flock with `LOCK_NB`; on Windows the
  msvcrt byte without polling): held elsewhere raises `BlockingIOError` at once. Push and pull share it. A run that
  finds it held ends with exit 0 and changes nothing.
- **A push** (`push`): lock; read the state; blocked by `visibility`: exit 1, nothing changed; with `--if-due` and not
  due (another process pushed just before): exit 0, nothing changed; inside a backoff without `--now`: exit 0, nothing
  changed; then `last_attempt = now` is written before the runner starts. Success:
  `last_success = now`, `backoff_level = 0`, `last_error = null`, `next_due = now + interval +- jitter`, log line
  `success`. Failure: `backoff_level + 1`, `next_due = now + min(interval * 2^level, backoff_max_h) +- jitter`,
  `last_error`, log line `failure` with `category` and the runner's fixed text (never raw git output); after
  `visibility` `next_due` stays as it is and the push stays blocked until `clear_visibility` (for the installer's
  `--sync-repo`). Jitter: uniform +-20 % of the wait. Interval: `sync.interval_min`, at least 10 minutes (a smaller
  value is raised with a log line `interval_raised`). An exception of the runner that is no `SyncError` is `internal`.
- **A pull** (`pull`): the same lock, no backoff and no state change; log line `success` or `failure` with
  `action: pull`.
- **Timeouts:** a push has 60 s in total, a pull in the background 30 s, in the foreground `sync.pull_timeout_s`
  (`Deadline`). `run_command` (and `Run.git`) starts each child in a process group of its own (`_plat.GROUP_OPTIONS`);
  at the deadline it ends the whole tree (`_plat.kill_tree`: SIGKILL to the group, on Windows `taskkill /T`), so a
  hanging ssh or git-remote-https cannot keep the pipes open, and fails with `timeout`. A program that cannot be
  started (no git) is `internal`. A runner calls `run.deadline.check()` between its own steps.
- **Watchdog:** a run that hangs outside a child process (the deadline ends only those) is still there after the
  deadline plus 5 + 10 s: a timer thread ends the live children of `run_command`, records the push as `timeout`
  (backoff as for any failure), writes the log line `failure` and ends the process with `os._exit(1)`. The lock goes
  with the process, so it is never held for good.
- **Git never asks:** `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, `GIT_ASKPASS` empty (git asks an askpass
  program before the terminal; an empty one skips `core.askPass` and `SSH_ASKPASS` too) and `GIT_SSH_COMMAND` with
  `-o BatchMode=yes` appended to the ssh command git would use: the user's `GIT_SSH_COMMAND`, else the
  `core.sshCommand` of the git config (read once per run, 5 s at most; an error gives `ssh`), else `ssh`. If `GIT_SSH`
  names a program (plink, a wrapper) and there is no `GIT_SSH_COMMAND`, none is set: it would replace that program.
- **Exit codes of `sync.py`:** 0 success or nothing to do, 1 failed, blocked or ended by the watchdog, 2 bad
  arguments; the two hooks print nothing and always exit 0. That `sync/` cannot be used is logged at most once an
  hour (`<log_dir>/sync-error.logged`). Log lines have hook `sync`.

## Sync export

`_sync_export.export(clone_root, log_dir, cfg, projects_dir, now, deadline=None)` appends what is new in the local
data of this machine to `machines/<own id>/` of the checked-out sync repository; the push (0034-SYNC-PUSH) calls it
under the sync lock and commits. Every line goes through `sync_format` (`encode`, `encode_day`, `append`), so only
the fields of the format leave the machine (see docs/report-format.md); nothing is written outside the own folder.

- **Sources:** the transcripts (read with `_transcripts.read_requests_from`, the rules of the usage report: the last
  line of a request, the agent type, the project folder) as `requests/<YYYY-MM>/<session>.jsonl`, one line per
  request, `proj` after `sync.project_labels`, the project of a subagent that of its session; the measurement history
  `usage-history.jsonl` with `.1` as `history/`; the gate decisions of `log.jsonl` with `.1` as `gates/`, only with
  `sync.include_gates`; the day archive `usage-days.jsonl` as `days/`, its project names after `sync.project_labels`.
  machine.json is written if it is missing or its name or platform changed.
- **Cursor** `<log_dir>/sync/cursor.json` (0600, atomic through `_context_state.write_private`): per source file
  (a transcript by its path below the projects folder) the offset after the last complete line exported, size, id
  (`st_dev:st_ino`, none if the system gives no inode number), modification time and a hash of the first 1024 bytes;
  for a transcript also the key of its first request (`first`) and its project folder. Only complete lines from the
  offset on are read, a half last line waits. A file that got smaller, has another id or other first bytes (without
  an id: also an older modification time) is read from byte 0; the reader removes doubled requests by `k`. A rotated
  `.1` is known by id and first bytes and read on from where the old name stopped. An unchanged file (id, size,
  modification time) is not opened. The cursor of a source changes only after its lines are in the clone, and it is
  saved after each group of sources (transcripts, history, gates, day archive): a run the watchdog of the push ends
  repeats at most the group it was in. Entries of transcripts that are gone are dropped. A cursor that cannot be read
  counts as empty (log line `cursor_unreadable`).
- **Request key:** `k` from message id and request id; a line without a message id is a request of its own, its `k`
  made from its transcript and byte offset (a replaced transcript can then add it twice). Gate lines have no key
  either: a log file replaced without a rotation is read from byte 0 and its gate lines are appended twice.
- **Errors:** `project_labels: hash` without `label_salt`, a machine id that cannot be used, a clone or cursor that
  cannot be written: `SyncError('internal')`. A source line that does not fit or a file that cannot be read is
  counted (`Result.skipped`, `Result.unreadable_files`) and left out. With a deadline the export stops at the next
  file once 30 s are left for commit and push (`Result.complete` false); the next run goes on from there.

## Log

`<log_dir>/log.jsonl`, rotated to `log.jsonl.1` above 5 MB. One line per decision: UTC time, hook, mode,
decision, reason (max 300 chars). Values from the input only as short strings, cut to at most 100 characters
(`_short_text`): `tool_name`, `agent_id`, `agent_type`, `permission_mode`, `subagent_type` and `model_in_call` (the `model` of the
tool input; both are read from every tool input, in practice only an Agent call carries them). Everything else from the input only as field names (`input_keys`, `tool_input_keys`), never its
values. The soft stop adds its counter `calls_since_checkpoint`. The `reason` carries quoted values masked:
every quoted part (`"…"`, `'…'`, `„…“`, backticks) is replaced by `…` before it is written (`mask_quoted` in
`_hookio.py`); the deny text that goes to the model keeps the values. Never prompts, commands or script text.
The log folder is created 0700 and `log.jsonl` 0600 (`fchmod` on every write, so a log of an earlier version
becomes private too), opened with `O_NOFOLLOW`. Per-agent call counters in `<log_dir>/counters/<agent_id>.json`
(written with `_context_state.write_private`: 0600 in a 0700 folder, never through a link), per-session context state in
`<log_dir>/context/<session_id>.json` (see Status line), heartbeat of the wake-up watcher in
`<log_dir>/context/<session_id>.watch`, measurement history in `<log_dir>/usage-history.jsonl` (see Status line), day archive of the usage report in `<log_dir>/usage-days.jsonl` (see Usage report), the context split of each session
in `<log_dir>/context-breakdown/<session_id>.json` (written by the desktop plugin, read by the usage report). The sync core keeps its state and lock in `<log_dir>/sync/`, its start marks and notes in the log folder itself (see Sync core). The context reporter and the watcher log nothing. A config problem is logged as a line with hook
`config` and decision `defaults`.

## Usage report

`tools/usage_report.py` is a separate tool, not a hook. It reads and counts; the only file it writes is the day
archive. Python 3.10+, standard library only. It reuses `log_summary.resolve_log_dir` (where the log folder is) and
`_usage.NO_FOLLOW`, and reads the transcripts with `hooks/_transcripts.py`, a module of its own so that other tools can read
them by the same rules; it imports no other module of the tool and is installed with the hooks.

**Sources.** (1) Transcripts `<projects>/<project>/<session>.jsonl` and `<project>/<session>/subagents/agent-*.jsonl`,
read by `_transcripts.read_transcripts` into `Request`s (links are not followed; files not modified since the start of the period minus 60 minutes are not opened). Per
assistant line only `timestamp`, `cwd`, `requestId`, `message.id`, `message.model` and `message.usage` (with
`output_tokens_details.thinking_tokens` and `server_tool_use.web_search_requests` / `web_fetch_requests`) are used; of
the lines with one `(message.id, requestId)` the last counts (it has the complete `output_tokens`; with equal output the
later one). A subagent belongs to the session whose folder it is in; its type is `spawnedAgentType`, else `customAgentType`, else
`agentType` of `agent-*.meta.json` (for a named subagent `agentType` holds only its name: if it equals `name` and no spawned or custom type is given, the
type is `unknown`; the value must match `[A-Za-z0-9_.:-]{1,64}`, else `other`; none: `unknown`). (2) The measurement history (`usage-history.jsonl` and `.1`);
unreadable lines are skipped and counted. For the time before the history began, the usage line of the usage clock in
the transcripts (`Usage: 5h N % ... week M % ... measured K min ago`, or the old German form, only with K at most 1, and
only in an attachment of type `hook_additional_context`) stands in; it carries no reset times. Measurements without
reset times pass `drop_noise`, 5h and week judged apart: a fall that stays above half of the last good value is a stale
number of an idle session if a value at least as high comes back within 15 minutes (`RECOVERY_S`) and the last good value
is at most `MAX_GAP_S` old; it is then replaced by the last good value (a measurement with only stale values is left
out). Any other fall is a new window, so a real reset without reset times does not cascade. Where the anchor (a known weekly
reset time) puts two measurements in one weekly window, any fall of the weekly value is stale at once and the whole
measurement is left out. A request that stands in two transcripts (a continued session) is counted once and
belongs to the file modified first, then the one that sorts first. A line that has the shape of a request but a
`requestId` that is not text, or a number above 10^12, is left out and counted (`unreadable_transcript_lines`). (3) The gate log `log.jsonl` (`.1`): `spawn_gate` and `soft_stop` decisions
by hook, mode and decision. (4) The day archive. Nothing else of a transcript is read, and no text is printed.

**Cost weight.** Per request `sum(tokens x price) / 1e6` with the prices of the first `prices.json` entry whose
`match` is part of the lower-case model name. Cache creation is split into 5 minute and 1 hour writes
(`cache_creation_input_tokens` minus `cache_creation.ephemeral_1h_input_tokens` is the 5 minute part). An entry with
`long_context` uses those prices when input, cache write and cache read together exceed `above_tokens` (Haiku 5.5:
100 000). No matching entry: the request has no price; its tokens are listed, its cost is not estimated.

**Thinking and web calls.** `thinking_tokens` (part of `output_tokens`, capped at them), `web_search_requests` and
`web_fetch_requests` are optional. A request that lacks one does not report it; it is unknown, not zero. The figures
keep, per level (period, model, CLI), how many requests report each detail (`*_n`), its sum, and for thinking the output
tokens of the reporting requests (`thinking_out`); the share is `thinking / thinking_out`. No reporting request: `null`
in the JSON, `n/a` in the text. They never enter the cost weight (thinking is part of the output price).

**Weekly windows.** The weekly value is shown per window, never as a difference across a weekly reset. A measurement's
window ends at its `week_resets` if recorded, else at the next reset in the 7-day rhythm of the anchor (the latest
`week_resets` of the history, else `seven_day.resets_at` of the usage file), else it is unknown. Consecutive measurements
with a known value are in one window unless the value fell or the reset times differ by 60 s or more; the first window
starts at the last value before the period if that is in the same window. Five hour windows without reset times are one window if together they last less than five hours and the value did not
fall from one to the other; this is also how the windows of two archived days join. `subscription.week_segments` lists them as
`start`, `end` (`at`, `value`) and `reset_at`. In the figures (`abo.weeks`) segments of several days join if they have
the same reset time, or no reset time and no fall of the value between them.

**Periods.** `--day`, `--month`, `--year` (`YYYY-01-01` up to `YYYY-12-31`, the current year up to today), `--since/--until` are local days (`datetime(...).timestamp()` in the local zone, so a
day of daylight saving change has 23 or 25 hours); `--week` is `[R - 7 days, R)` with `R` the latest `week_resets` of the
history, else `seven_day.resets_at` of the usage file (found like the log folder: `--config`, `CCUC_USAGE_FILE`, `usage_file`
of the configuration), moved on by whole weeks until it lies after now; with neither, exit 1. Everything is computed on half open intervals of Unix seconds.

**Allocation.** A section is the time between two consecutive measurements (history rows, else usage lines); it
belongs to the period of its end. For each metric (`five`, `week`) a section counts if both values exist, the value did
not fall, the reset times (if both known) differ by less than 60 s, and the section is at most 3600 s long. A section
with a reset (or a fall) or longer than 3600 s adds to `skipped_sections` (a long one also its rise to `skipped_points`).
A counted section adds its rise to `counted`. The requests after the first measurement up to the second are summed by
CLI (cost weight, 8 character session prefix); with a total above zero the rise is shared by cost share
(`attributed`, per CLI `five_pts`, `week_pts`), otherwise it goes to `rest` (`unexplained_points`). So `attributed + rest
= counted` always. The same shares are also summed by agent in `_section_costs` (`None` for the main sessions, else the
type of the subagent, the keys of `Request.agent_type`) into `stats['points']`: `main` and `types`, each with `five` and
`week`; they add up to `attributed`. An archived day of an earlier version has no `points` key: `main` then computes
the points of that day from the transcripts (`_split_from_transcripts`) and names the day in
`coverage.archive_days_without_agent_split` if they hold less than the archive's `alloc` says was attributed.

**Calibration.** A counted section with exactly one CLI, no unpriced request and a total above zero adds to the sums
`n`, `c2 = sum(cost^2)`, `cd = sum(cost x rise)` of the whole period, of its day and, if all its requests have the same
price entry, of that model. USD per point is `c2 / cd` (regression of the rise on the cost through zero, inverted: the noise of the whole points is in the rise); it is
shown from 50 sections (`MIN_SECTIONS`), per model only from 50 sections of that model, per day always with its count.

**Mergeable figures and the archive.** All figures of a period are one dict of numbers (`new_stats`), so the figures of
several days add up (`merge_stats`). `<log_dir>/usage-days.jsonl` has one line per finished local day:
`{"v":1,"day":"YYYY-MM-DD","stats":{...}}` with those figures (session ids cut to 8 characters, no text). A report
takes every whole finished day (before today) from the archive if the archive has it, whatever the transcripts still hold; the rest of the
period is computed from the transcripts. `parse_archive_line` accepts a line only if its figures merge into empty figures, so a line in another format is
unreadable (counted, and `--archive` writes its day again). A day whose figures do not fit the live figures when merged is
computed from the transcripts instead and counted; a day is never half merged. `--archive` computes every finished day (before today, local) with requests that the file does not have,
and appends them under an exclusive lock of the file (`_usage.locked`: `flock`, `msvcrt` on Windows), re-reading the file's days first (two runs never write a day
twice, a day is never overwritten). Like the measurement history the file is 0600 in a 0700 folder, opened
`O_APPEND|O_NOFOLLOW` (a link as the file or as the folder is refused, exit 1), `fchmod` on every open, and a cut-off
last line gets a line break first.

**Projects.** A project is the folder name of a CLI (its `label`), for everyone and without a setting; CLIs of the same label
are one project. The report knows no paths: folders of the same name in different places fall together. The grouping is made
when the report is built, so the archive holds no projects. The table shortens a name longer than `NAME_WIDTH` (30) in the
middle (`_shorten`: the first `NAME_WIDTH // 2` characters, `…`, the last `NAME_WIDTH - 1 - NAME_WIDTH // 2`); names whose short forms
are equal stay whole (`short_names`). Only the table is shortened: the JSON, `--full` and the `Details:` line have full names.

**Overview.** `build_overview(period, report, start_block_5h)` makes `report['overview']` from the other sections of the same
report and `render_overview` prints that object alone, so the JSON object is the text. Projects are summed from their
CLIs (cost, estimated points, tokens for the cache hit rate), sorted by `(-cost_usd, name)`; the first `MAX_PROJECTS` (5) are rows, the
others are the sum `more`. Points and costs below the display thresholds are formatted by `points_text` (`0`, `<1`,
else the number with one decimal) and `usd_text`. **Notable** has five rules, always evaluated in this order and each
on its own: (1) per gate (`spawn_gate`, `soft_stop`) and mode, `deny` and `ask` counts above 0; (2) 5h windows whose
`peak` is at least `thresholds.start_block_5h` (one line); (3) projects whose cache hit rate is below `CACHE_LOW` (0.9),
one line, rates rounded down; (4) the first project if its share is above 0.5; (5) `allocation.*.unexplained_points`
above 0 for the week or the 5h limit. A rule that hits adds the lines to `overview.notable` (`rule`, `text` and its
numbers); no hit, no `Notable` block.

**Gate reasons.** `read_guard_log` keeps, for `warn`, `ask` and `deny`, `reason_kind(reason)`: the text before the first
colon, `\d+(\.\d+)? ?%` replaced by `N %`, characters outside `[\w .,…()/+%-]` replaced by `?`, cut to 60 characters,
the first letter in lower case; empty gives `no reason`. They are counted in `stats['guard_reasons']`
(`hook -> mode -> decision -> reason -> count`), a second figure set next to `guards`, mergeable and archived with the
day. An archived day of an earlier version has no `guard_reasons`: it merges (the key is optional) and its decisions
show as `reason not recorded` (decisions minus the sum of the reasons, per gate, mode and decision; `reason_counts`).
The reason text is the gate's own log text; a log line never carries values of the input (see Log), so this is no text
of a transcript.

**Details.** `--project NAME` matches a project whose name is `NAME` or starts with it (an exact name wins over longer names it
starts; several fit: an error with the candidates, exit 2); `--session ID` matches a CLI whose 8-character id starts
with `ID` or is a prefix of `ID` (a full session id works), case-sensitive, and must be unique among the CLIs left by
`--project` (`select_clis` raises `SelectionError`, which `main` turns into `parser.error`, exit 2: none, several with
the candidates, empty text). The output is the head of `--full`, the `By CLI` blocks of the selected CLIs (the same
function `_cli_block` as `--full`) and `Sum of N CLIs`: the raw figures of the selected CLIs merged with `_merge_cli` and
printed as one block. `--json` takes none of `--full`, `--project`, `--session` (exit 2), and `--archive` none of them.

**Startup context.** `Request.source` is the transcript a request stands in (never printed). `Data.set_requests` takes the first
request of every source in time order as a start (`_transcripts.start_indices`; `start_indices`, `start_times`), after the deduplication of
requests, so a request that stands in two transcripts is a start of the file that owns it. `_starts_between` turns the
starts inside an interval into `stats['starts']`: a list of `{t, session, type, model, input, cache_write, cache_read}`
(`type` is `None` for a main session, else the agent type of the report; no path, no content), mergeable by
concatenation (`_merge_starts` rejects a start of another shape, which makes the archive line unreadable) and archived with the day. An archived day of an
earlier version has no `starts` key: `main` then computes its starts from the transcripts (`_starts_between` for the day), which
gives none once Claude Code has deleted them. `_start_report` sorts the starts by time and builds the JSON section `startup`;
`startup_groups` (also used by `select_clis` for the starts of the selected sessions) makes the groups: count, median
(`statistics.median`, rounded), minimum, maximum, `warm_starts` (cache read above 0) for the main sessions, all subagents, each
type (most starts first) and each local day. `_overview_startup` is `overview.startup`; `_split_lines` prints `Start:`
from it. `estimate_start` (only for `--full` and `--json`) reads only sizes: `claude_md_files` (the user folder's
CLAUDE.md and rules, then per folder from the outermost down to the working folder CLAUDE.md, CLAUDE.local.md,
.claude/CLAUDE.md and .claude/rules; rules with `paths:` in the frontmatter are skipped; a file counts once by its real path), `memory_file`
(`<projects dir>/<path with every character outside A-Za-z0-9 as "-">/memory/MEMORY.md` for the path of `repository_root` (the main working tree of the git repository: a linked worktree has a `.git` file naming `<main>/.git/worktrees/<name>`), else for the folder itself, cut at `MEMORY_LINES` (200) lines
and `MEMORY_CHARS` (25 KB)), `agent_definitions` and `skill_definitions` (name plus folded `description` from
`description_length`; the `project_folders` from the working folder up to the root of its git working tree before the
user folder, the first definition of a name wins), at most `MAX_FILE_CHARS`
characters read per file, `NOT_ESTIMABLE` named and not guessed, and `omitClaudeMd` (`true`, any case) of the agent definition
for the subagent types of the period. `estimate_folder` picks the working folder: that of the latest start of the period
that exists. File names are made harmless (`FILE_NAME_CHARS`).

**Output.** Text (English) or `--json` with the sections `schema_version`, `period`, `coverage`, `overview`, `subscription`,
`total`, `startup`, `clis`, `allocation`, `calibration`, `guards`, `guard_reasons` in this order (`SCHEMA_VERSION` changes
only when a field is renamed or removed). The text is, without a switch, the overview; `--full` is the former report with
these differences: a 5h window names weekday and date of its peak; estimates below one point read `<1` (`~` only for
a number from 1); the per-day calibration lists days with at least `MIN_DAY_SECTIONS` (20) stretches; the `Gates` heading has
no sentence about the mode, each gate says its mode and gives its reasons. Exit codes: 0 report, 1 no transcripts folder (and no archived day
to use; an empty folder is an empty report) or an unreadable `prices.json` or an archive that cannot be written, 2 bad arguments.
`docs/report-format.md` explains every line and field for readers.

## Known limits

- `rate_limits` in the status line input is documented, but not a versioned API; it may change or vanish
  (API key, Bedrock and Vertex sessions do not get it).
- The usage file and the counters are not protected: a model with write access to them can fake them.
  Protect them with a guard if that matters to you.
- Agent discovery covers project and user agents, not plugin or managed agents.
