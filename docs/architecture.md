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
  _config.py              loads config.json next to the scripts, falls back to defaults
  _usage.py               reads and merges the shared usage file
  _context_state.py       writes the per-session context file (<log_dir>/context/<session_id>.json); the reporter reads it
  _hookio.py              hook input/output, JSONL log, directory discovery
  _spawn_check.py         model/effort checks for Agent, Workflow scripts and Skill forks
tools/
  install.py              adds/removes the hooks in a settings.json (per component, backup, dry run)
  log_summary.py          summarises the JSONL log
  check_agents.py         lists which agent definitions the model rule would report or deny (read only)
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
- Write under an exclusive `fcntl.flock` on `<file>.lock`, atomically via `<file>.tmp.<pid>` + `os.replace`.
  Write only when something changed.
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
`{"session_id", "context_window_size", "used_tokens", "measured_at"}` (`measured_at` UTC ISO as in the usage file).

- Written only if `session_id` matches `[A-Za-z0-9_-]{1,128}` (it becomes a file name), `total_input_tokens` is
  above 0 and the window size is valid. Never before the first answer.
- One file per session, so sessions do not mix and no lock is needed. Atomic: `<file>.tmp.<pid>` created with
  `O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600, then `os.replace`. Folder mode 0700. A link as the target is replaced,
  a link as the folder or as the temporary file is never written through.
- Every write removes regular `*.json` files in that folder that are older than 7 days (by `lstat`; links and
  folders are skipped; the file just written is kept).
- Any error is ignored: the line is shown regardless.

## Configuration

`config.json` next to the scripts (env override `CCUC_CONFIG`). Missing file: defaults. Unreadable file,
wrong types or unknown keys: defaults for the affected keys, one log line, never a crash. Defaults are
listed in `config.example.json`.

`context_reporter.after_tool_from` must be a number from 0 to 100 and `context_reporter.max_age_min` a finite
number above 0; anything else (also NaN and infinity) falls back to the default and writes the usual one log line.
The context reporter itself does not write that line (see `context_reporter.py`), the other hooks do.

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
- **On internal error:** exit 0, no output, nothing logged. Config problems are not logged either (the hook runs
  after every tool call); a wrong `context_reporter` value falls back to its default.
- **Limits:** the percentage is computed from the two stored numbers. The status line shows the percentage
  Claude Code passes in `used_percentage`; both agree as long as that is `total_input_tokens / window`.
  Subagents get nothing, and the file is an ordinary file, like the usage file: a model that can write it can
  fake it.

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
`%%` in the format, for example `printf '%s\n' "line" >> file` (an option like `-v` or a numeric conversion like
`%d` can run code in bash and zsh); either of them `| tee -a file`; `cat >> file <<'EOF'`. Double-quoted
arguments must not contain `$`, backticks or `\`.

## Log

`<log_dir>/log.jsonl`, rotated to `log.jsonl.1` above 5 MB. One line per decision: UTC time, hook, mode,
decision, reason (max 300 chars). Values from the input only as short strings, cut to at most 100 characters
(`_short_text`): `tool_name`, `agent_id`, `agent_type`, `permission_mode`, `subagent_type` and `model_in_call` (the `model` of an
Agent call). Everything else from the input only as field names (`input_keys`, `tool_input_keys`), never its
values. The soft stop adds its counter `calls_since_checkpoint`. The `reason` carries quoted values masked:
every quoted part (`"…"`, `'…'`, `„…“`, backticks) is replaced by `…` before it is written (`mask_quoted` in
`_hookio.py`); the deny text that goes to the model keeps the values. Never prompts, commands or script text.
Per-agent call counters in `<log_dir>/counters/<agent_id>.json`, per-session context state in
`<log_dir>/context/<session_id>.json` (see Status line). The context reporter logs nothing. A config problem is logged as a line with hook
`config` and decision `defaults`.

## Known limits

- `rate_limits` in the status line input is not a documented stable interface; it may change or vanish
  (API key, Bedrock and Vertex sessions do not get it).
- The usage file and the counters are not protected: a model with write access to them can fake them.
  Protect them with a guard if that matters to you.
- Agent discovery covers project and user agents, not plugin or managed agents.
