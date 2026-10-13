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
  _config.py              loads config.json next to the scripts, falls back to defaults
  _usage.py               reads and merges the shared usage file
  _history.py             appends the merged usage numbers to the measurement history (<log_dir>/usage-history.jsonl)
  _context_state.py       writes the per-session context file (<log_dir>/context/<session_id>.json); the reporter reads it
  _wake.py                what reporter and watcher share: ALIVE_MIN, REST_AFTER_FIRE_MIN, the heartbeat file
  _hookio.py              hook input/output, JSONL log, directory discovery
  _spawn_check.py         model/effort checks for Agent, Workflow scripts and Skill forks
tools/
  install.py              adds/removes the hooks in a settings.json (per component, backup, dry run)
  log_summary.py          summarises the JSONL log
  check_agents.py         lists which agent definitions the model rule would report or deny (read only)
  usage_report.py         what was used per day, week, month and CLI, from the transcripts (read only; --archive writes the day archive)
  prices.json             API prices per million tokens for usage_report.py, with source and date
docs/agents.md            how to give agents model and effort, for users and for the model
tests/                    pytest, synthetic data only
config.example.json       every key with its default
```

Scripts import their siblings by inserting their own directory into `sys.path`. No packages, no third-party
runtime dependencies. Python 3.10 or newer, Linux and macOS (`fcntl` is required; Windows is not supported).

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
- Write under an exclusive `fcntl.flock` on `<log_dir>/<file name>.lock` (the tool's own folder, so a guard that
  protects it covers the lock; whoever holds the lock stops the updates), atomically via `<file>.tmp.<pid>` +
  `os.replace` (next to the file: the rename is atomic only within one file system).
  Write only when something changed. The status line appends to the measurement history under the same lock
  (see Status line).
- A file in any other format counts as empty and is rebuilt on the next status line tick.

Readers classify the measurement as `ok`, `stale` (older than `max_age_min`, or more than 1 min in the future)
or `unknown` (missing, unreadable, no valid five-hour block). `stale` and `unknown` are treated alike by all
decisions.

## Status line

`statusline.py` prints one line: `<model> | 5h 40% ↻14:00 | week 61% [↻Mon 09:00] [<- weekly limit reached] |
ctx 231k/1000k 23% | cache 54m`. Every part is left out if its input is missing or invalid (bool, string, NaN, negative): silent
rather than wrong. Exit code 0 always. The line is written as UTF-8 (the dash in `ctx –/1000k`).

- **5h / week:** from `rate_limits`, merged through the shared usage file (see above). Colours
  `statusline.yellow_from` and `red_from`; the marker appears at `thresholds.start_block_week`. With
  `statusline.show_reset` (default true) each part ends with its `resets_at` in local time, in the part's colour:
  `↻HH:MM` for the five hours, `↻<weekday> HH:MM` for the week, the latter only from `yellow_from` on. The
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

## Hooks

| Hook | Event | On internal error |
|---|---|---|
| `usage_clock.py` | UserPromptSubmit | exit 0, no output (a prompt must never be rejected) |
| `context_reporter.py` | UserPromptSubmit, PostToolUse `*` | exit 0, no output (nothing may be rejected or blocked) |
| `soft_stop.py` | PreToolUse `*` | exit 0, allow (fail open) |
| `spawn_gate.py` | PreToolUse `Agent\|Workflow\|Skill` | deny (fail closed), except while checking a Skill: warn; in `shadow` only logged |

Output formats: deny = `{"hookSpecificOutput": {"hookEventName", "permissionDecision": "deny",
"permissionDecisionReason"}}`; warning = `{"hookSpecificOutput": {"hookEventName", "additionalContext"}}`.
All texts are English. They are addressed to the model, which relays them to the user in the user's language.

In `shadow` mode nothing is denied or warned; every decision is logged with what it would have been.

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

Constants: `POLL_S` 60, `STALE_MIN` 2, `STALE_POLLS` 3, `MAX_RUN_MIN` 115 (all `wake_watcher.py`), `ALIVE_MIN` 3 and
`REST_AFTER_FIRE_MIN` 60 (`_wake.py`, shared with the reporter).

- **At the start**, in this order: `from_k` is 0: message `disabled`, nothing written. A heartbeat of state
  `watching` at most `ALIVE_MIN` minutes old (a living watcher): message `already running`, nothing written. A
  `fired` or `ended` heartbeat or a broken file does not stop a start.
- **Per poll**, the first one at once: (1) the maximum run time of `MAX_RUN_MIN` minutes is reached: write the
  heartbeat `ended` (a failure to write it is ignored, the message goes out anyway) and end. (2) read
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
- **Message** (English, one paragraph, with the session id): how long the user was away (about TTL minus the
  rest), the rest of the cache in minutes (rounded up), the context in k and the threshold; then `The user is away.
  Run your session-end routine now, without asking (save progress, commit, push, summary), then end the turn. Do
  not start a new watcher.` The other endings say why the watcher stopped; the one for a gone session adds `If you
  are a different session, there is nothing to do.` (a task started in one session wakes whichever session is
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
   (`permissionDecision: "ask"`, reason for the user, no `additionalContext`: the model took an approved start for a block). `ask` only where the
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
`<log_dir>/context/<session_id>.watch`, measurement history in `<log_dir>/usage-history.jsonl` (see Status line), day archive of the usage report in `<log_dir>/usage-days.jsonl` (see Usage report). The context reporter and the watcher log nothing. A config problem is logged as a line with hook
`config` and decision `defaults`.

## Usage report

`tools/usage_report.py` is a separate tool, not a hook. It reads and counts; the only file it writes is the day
archive. Python 3.10+, standard library only. It reuses `log_summary.resolve_log_dir` (where the log folder is) and
`_usage.NO_FOLLOW`.

**Sources.** (1) Transcripts `<projects>/<project>/<session>.jsonl` and `<project>/<session>/subagents/agent-*.jsonl`
(links are not followed; files not modified since the start of the period minus 60 minutes are not opened). Per
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

**Periods.** `--day`, `--month`, `--since/--until` are local days (`datetime(...).timestamp()` in the local zone, so a
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
= counted` always.

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
and appends them under an exclusive `flock` of the file, re-reading the file's days first (two runs never write a day
twice, a day is never overwritten). Like the measurement history the file is 0600 in a 0700 folder, opened
`O_APPEND|O_NOFOLLOW` (a link as the file or as the folder is refused, exit 1), `fchmod` on every open, and a cut-off
last line gets a line break first.

**Output.** Text (English) or `--json` with the sections `period`, `coverage`, `subscription`, `total`, `clis`,
`allocation`, `calibration`, `guards` in this order. Exit codes: 0 report, 1 no transcripts folder (and no archived day
to use; an empty folder is an empty report) or an unreadable `prices.json` or an archive that cannot be written, 2 bad arguments.

## Known limits

- `rate_limits` in the status line input is documented, but not a versioned API; it may change or vanish
  (API key, Bedrock and Vertex sessions do not get it).
- The usage file and the counters are not protected: a model with write access to them can fake them.
  Protect them with a guard if that matters to you.
- Agent discovery covers project and user agents, not plugin or managed agents.
