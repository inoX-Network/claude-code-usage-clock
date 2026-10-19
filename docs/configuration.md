# Configuration reference

[README](../README.md) · [All documentation](README.md)

## Configuration

`~/.claude/hooks/usage-clock/config.json`, every key optional; defaults in
[`config.example.json`](../config.example.json). A broken file or a wrong value falls back to the default for
that key and writes one line to the log.

| Key | Default | Meaning |
|---|---|---|
| `usage_file` | `~/.claude/rate-limit.json` | Shared usage file written by the status line. |
| `log_dir` | `~/.cache/claude-usage-clock` | Log, per-agent counters, per-session context files, the measurement history and the report archive. |
| `display.show_usage` / `show_time` | `true` / `true` | Parts of the prompt line. |
| `statusline.yellow_from` / `red_from` | `50` / `75` | Colours. |
| `statusline.week_stop_marker` | `true` | Show the week gate marker (`<- week gate: agents warn`, `ask` or `blocked`, by `week_action`) at the weekly threshold. |
| `statusline.show_reset` | `true` | Show when the windows reset (`↻14:00`; the weekly one from yellow on). |
| `statusline.context_yellow_from_k` / `context_red_from_k` | `300` / `500` | Colour of the `ctx` part by absolute tokens in thousands, not by percentage. |
| `statusline.cache_yellow_below_min` | `5` | The `cache` clock turns yellow below this many minutes. |
| `history.enabled` / `heartbeat_min` / `max_kb` | `true` / `15` / `1024` | Measurement history of the status line: on or off; minutes after which an unchanged measurement is written anyway; size in KB above which the file is rotated to `.1`. |
| `start_estimate.chars_per_token` | `2.3` | Characters per token with which the usage report turns the size of CLAUDE.md, MEMORY.md and the agent and skill descriptions into an estimated number of start tokens. Measured on German Markdown with umlauts (2.3); English text needs about 4. |
| `context_reporter.after_tool_from` | `50` | Percent of the window from which the reporter also speaks after a tool call (0-100); with a prompt it always speaks. |
| `context_reporter.max_age_min` | `15` | A context measurement older than this many minutes (or from the future) is not reported. |
| `wake_watcher.from_k` | `0` | Context in thousands of tokens from which the wake-up watcher is offered and may fire; `0` switches it off. |
| `wake_watcher.lead_min` | `10` | Longest lead before the prompt cache expires at which the watcher fires, in minutes; never more than a fifth of the cache lifetime (10 min for the 1-hour cache, 1 min for the 5-minute cache). |
| `wake_watcher.routine_file` | `""` | Path (`~` allowed) to a file with your session-end routine; when the watcher fires, the context reporter hands it to the model with the wake-up call. Empty: off. |
| `thresholds.start_block_5h` / `start_block_week` | `75` / `75` | Spawn gate. |
| `thresholds.warn` / `checkpoint_only` / `deny` | `85` / `92` / `95` | Soft stop. |
| `thresholds.max_age_min` | `15` | Older measurements count as stale. |
| `thresholds.calls_without_checkpoint` | `15` | Soft stop checkpoint rhythm. |
| `thresholds.unknown_usage_max_agents` | `1` | Block size allowed while usage is unknown. |
| `thresholds.week_action` | `warn` | Above `start_block_week`: `warn`, `ask` (permission prompt) or `deny`. |
| `thresholds.ask_verified_in_bypass` | `false` | `true` lets `ask` prompt in modes such as `bypassPermissions`; set it only after you saw the prompt there. |
| `checkpoint_dir_suffix` | `/agent-checkpoints` | What counts as a checkpoint directory. |
| `spawn_rules.model_effort_action` | `warn` | The model rule of the spawn gate: `warn` reports, `deny` refuses the start, `off` keeps only the usage part. |
| `spawn_rules.model_pattern` | `sonnet\|opus\|haiku\|fable\|inherit\|claude-[a-z0-9-]+` | Which `model` values are accepted, see below. |
| `spawn_rules.effort_values` | `["low", "medium", "high", "xhigh", "max"]` | Which `effort` values are accepted, see below. |
| `spawn_rules.suggested_agents` | `[]` | Agents named in the message of the model rule as alternatives. Empty: a general hint. |
| `spawn_rules.exceptions` | `["claude-code-guide", "statusline-setup"]` | Agent types that start without the model rule, see below. |
| `spawn_rules.apply_to_main_session` | `true` | `false`: calls of the main session are not checked at all. |
| `spawn_rules.skill_fork_missing_agent` | `warn` | A forked skill without a complete agent definition: `warn` or `deny`. |
| `spawn_rules.model_override_in_call` | `warn` | A `model` in an Agent call overrides the definition: `allow`, `warn` or `deny`. |
| `sync.mode` | `off` | Sync across machines: `off`, `shadow` or `on`. |
| `sync.machine_name` | `""` | Name of this machine in the sync repository (letters, digits, spaces, `. + @ = - _`, at most 64). Empty: `machine-` and the first 6 characters of its id; the host name is never used. |
| `sync.interval_min` / `backoff_max_h` / `pull_timeout_s` | `30` / `6` / `5` | Minutes between two pushes (at least 10); the longest wait after failed pushes, in hours; seconds the pull before a report may take. |
| `sync.project_labels` | `plain` | How projects are named in the sync repository, in request and day lines: `plain` (the folder name, as in the report), `alias` (from `sync.project_aliases`) or `hash` (needs `sync.label_salt`). |
| `sync.project_aliases` | `{}` | Folder name to the name used for `alias`. A folder without an entry becomes `unbenannt-` and 8 hex characters. |
| `sync.label_salt` | `""` | Required for `hash`, the same on every machine. Without it the export stops. |
| `sync.include_gates` | `true` | Sync the decisions of the spawn gate and the soft stop (time, gate, decision, mode and the kind of the reason). `false`: no gate lines leave this machine. |
| `sync.keep_request_months` | `3` | Full months (at least 1) whose requests stay in the own folder of the sync repository; older months are compacted to their day lines. |

The mode and `--week-ok-until` are arguments in the hook command in `settings.json`, not in the config file.

Three environment variables override the files: `CCUC_CONFIG` (another config file), `CCUC_LOG_DIR` (the log
folder) and `CCUC_USAGE_FILE` (the shared usage file).

### Model names, effort values and exceptions

The spawn gate checks the `model` and `effort` of every agent. Two keys decide what counts as valid:

- `spawn_rules.model_pattern` is a regular expression that has to match the **whole** model value
  (`re.fullmatch`). It applies to the literal `model` of every `agent(...)` call in a workflow script. (For an
  agent definition the gate only requires that a `model:` is set.) The default accepts the
  aliases `sonnet`, `opus`, `haiku`, `fable` and `inherit` and any full name that starts with `claude-`. If a
  workflow script names a model the pattern does not match, for example a Bedrock or Vertex name with dots or a
  colon, the model rule reports it as `model "…" unknown` (and refuses the start with
  `model_effort_action: deny`). Extend the pattern, for example
  `sonnet|opus|haiku|fable|inherit|claude-[a-z0-9-]+|my-proxy/[a-z0-9.-]+`.
- `spawn_rules.effort_values` is the list of accepted `effort` values, in the frontmatter of agent definitions
  and in workflow calls. To allow only some, list only those, for example `["low", "medium", "high"]`.

A pattern that is not a valid regular expression, or an empty list, falls back to the default and writes one
line to the log.

`spawn_rules.exceptions` lists agent types that start without the model rule. The defaults are
`claude-code-guide` and `statusline-setup`: they are built-in agent types of Claude Code without a definition
file you could add `model:` and `effort:` to, so the model rule would report or deny them every time. Exceptions apply to the Agent tool and to skill forks, not to `agent(...)` calls inside a workflow script.
Setting `exceptions` replaces the default list, so include the two defaults if you still want them.
