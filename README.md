# claude-code-usage-clock

Let Claude Code see its own usage and the time with every prompt, and keep subagents from burning through the
five-hour window.

Claude Code knows your plan limits, but the model working for you does not. It cannot tell whether starting
ten agents at 80 % of the five-hour window is a good idea, it does not know that it is 3 a.m., and an
overnight run happily spends the whole window. This repo closes that gap with a status line and three hooks:

| Part | What it does | Maturity | Default |
|---|---|---|---|
| **Status line** | Shows model, 5h and weekly usage in colour. Several sessions merge into one shared usage file, so every session and every hook sees the same, current numbers. | stable | on |
| **Usage clock** (`UserPromptSubmit`) | Adds one line of context to every prompt: usage, reset times, and the local date and time. Both parts switch independently. | stable | on |
| **Spawn gate** (`PreToolUse`: Agent, Workflow, Skill) | Stops new agent blocks above a usage threshold, and checks that every agent has a determinable model and effort (reports by default, can deny). | available | log only |
| **Soft stop** (`PreToolUse`: all tools) | Slows subagents down near the five-hour limit: warn, then only allow saving a checkpoint, then ask them to finish with a partial result. Never blocks the main session. | available | log only |

"Log only" (`shadow` mode) means the gates decide and write their decision to a log, but block nothing. Run
them that way for a while, read the log, then switch to `enforce`.

## Requirements

- Claude Code on a **Pro or Max** subscription. The usage numbers come from the `rate_limits` field that
  Claude Code passes to the status line; API key, Bedrock and Vertex sessions do not get it, and neither does
  a session before its first response. Without it everything reports usage as `unknown`.
- Python 3.10 or newer as `python3` (the macOS system Python 3.9 is too old; install a current one).
- Linux or macOS. Windows is not supported (`fcntl`).
- No third-party packages at runtime.

## Install

```sh
git clone https://github.com/inoX-Network/claude-code-usage-clock
cd claude-code-usage-clock
python3 tools/install.py --dry-run     # shows what would change
python3 tools/install.py               # installs all four parts, gates in shadow mode
```

The installer copies the scripts to `~/.claude/hooks/usage-clock/`, creates `config.json` there if it does not
exist yet, and adds the hooks and the status line to `~/.claude/settings.json`. Before writing it runs every
hook once as a smoke test and keeps a backup (`settings.json.bak-usage-clock-<timestamp>`). Running it again
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
| `--components statusline,usage-clock,spawn-gate,soft-stop` | Install only some parts. |
| `--mode shadow\|enforce` | Mode of the two gates. Without it an existing installation keeps its mode; a first install starts in `shadow`. |
| `--week-ok-until YYYY-MM-DD` | Allow new blocks above the weekly threshold up to and including that day. Not kept: a run without it removes an earlier value (and says so). |
| `--replace-statusline` | Replace an existing status line of your own. Without it, yours is left alone. |
| `--auto-continue` | Also set `autoContinueAtUsageLimit: true` (Claude Code waits and continues after a reset). |
| `--settings PATH`, `--target DIR` | Other locations. |
| `--dry-run` | Show what would change and write nothing. |
| `--uninstall` | Remove the entries again; files stay. See [Uninstall completely](#uninstall-completely). |

Restart Claude Code (or open `/hooks`) after installing.

### Uninstall completely

`python3 tools/install.py --uninstall` removes the hook entries and the status line entry that point to the
scripts in `--target` (with a backup of `settings.json`). It deletes no files and does not undo everything else
the installer did. What stays, and how to remove it:

| What stays | How to remove it |
|---|---|
| The `--target` folder (default `~/.claude/hooks/usage-clock/`) with the scripts and your `config.json` | `rm -r ~/.claude/hooks/usage-clock` (or the folder you gave with `--target`) |
| The log folder (`log_dir`, default `~/.cache/claude-usage-clock/`): `log.jsonl` and the per-agent counters | `rm -r ~/.cache/claude-usage-clock` |
| The shared usage file `~/.claude/rate-limit.json` and its lock file `rate-limit.json.lock` (the `usage_file` in your config) | `rm ~/.claude/rate-limit.json ~/.claude/rate-limit.json.lock` |
| `autoContinueAtUsageLimit: true`, if you installed with `--auto-continue` | Remove the key from `settings.json` by hand, or set it to `false`. |
| Your earlier status line, if you installed with `--replace-statusline`: it is **not** restored | Copy it back from the backup `settings.json.bak-usage-clock-<timestamp>` that the installing run printed (the `Undo:` line). |
| The backups `settings.json.bak-usage-clock-*` | Delete them when you no longer need them. |

If you use `--components`, the uninstall removes only the components you name.

## What the model sees

With every prompt:

```
Usage: 5h 40 % (resets 14:00) · week 61 % (resets Mon 19 Jan 09:00) · measured 0 min ago · Now: Wed 14 Jan 2026 10:05
```

Above the start threshold the line says so; with a missing or old measurement it says `unknown` or `STALE` and
asks to start larger blocks only after a fresh measurement. The texts are English; the model relays them to
you in your language.

The time part is useful on its own: every message in the transcript carries its date and time, so you can
find later when something happened. Turn either part off in `config.json` (`display.show_usage`,
`display.show_time`).

## The two gates

**Spawn gate** (Agent, Workflow and forked Skill starts, in the main session and in subagents):

- 5h usage at or above `start_block_5h` (75 %): no new block until the window resets.
- Weekly usage at or above `start_block_week` (75 %): `thresholds.week_action` decides, unless `--week-ok-until`
  covers today (then it only warns). `warn` (default) starts with a warning, `deny` refuses, and `ask` shows
  Claude Code's permission prompt, so you confirm each start yourself; only a person can answer it, the model
  cannot fake a yes. In permission modes that may skip the prompt (`bypassPermissions`, `dontAsk`, `auto`, or
  no mode in the hook input) `ask` refuses instead, until you have seen the prompt appear in your setup and set
  `thresholds.ask_verified_in_bypass: true`.
- Unknown or stale usage: a single agent may start (with a warning), a larger block may not.
- Model rule: every agent should have `model:` and `effort:` in its definition's frontmatter. Built-in types
  without a definition file, such as `general-purpose`, never do; define your own agents instead, or list types
  in `spawn_rules.exceptions`. In workflow scripts each `agent(...)` call needs literal `model` and `effort`
  options, or an `agentType` with a complete definition; `cond ? 'a' : 'b'` with two literals is fine,
  anything the gate cannot check statically counts as a finding.
- `spawn_rules.model_effort_action` decides what a finding does: `warn` (default) starts the agent and tells the
  model what is missing, `deny` refuses the start, `off` skips the model rule and keeps only the usage part.
  The level covers the whole model rule: with `warn`, `skill_fork_missing_agent` and `model_override_in_call`
  set to `deny` also only warn.
  [`docs/agents.md`](docs/agents.md) explains how to give your agents a model and an effort.
- It fails closed: if the gate itself breaks, starts are denied (in `enforce` mode).

**Soft stop** (every tool call of a subagent):

| 5h usage | Effect |
|---|---|
| ≥ 85 % (`warn`) | Warning: save a checkpoint soon. |
| ≥ 92 % (`checkpoint_only`) | Only checkpoint writes are allowed. |
| ≥ 95 % (`deny`) | Everything else is denied; the agent is told to finish with a partial result. |

A checkpoint write is a Write/Edit into a directory whose path ends with `checkpoint_dir_suffix`
(default `/agent-checkpoints`), or a Bash command that is nothing but an append to a file there:
`echo 'text' >> file`, `printf '%s\n' "line" >> file`, `echo 'text' | tee -a file` or `cat >> file <<'EOF'`.
The arguments of `echo` and `printf` must be quoted (double quotes without `$`, backticks or `\`). `printf`
counts only without options and only with `%s`, `%b` and `%%` in its format: an option such as `-v` or a
numeric conversion such as `%d` can run code in bash and zsh, so such a command is no checkpoint write.
An agent that has saved once must save again at least every 15 calls. Tell your agents in their prompt where
their checkpoint file is. Unknown usage never blocks here.

### From shadow to enforce

```sh
python3 tools/log_summary.py                 # what the gates decided, and what they would have denied
python3 tools/log_summary.py --since=2026-01-14T00:00
python3 tools/check_agents.py                # which agents the model rule would report or deny
python3 tools/install.py --mode enforce      # when the log looks right
```

With `model_effort_action: deny`, `enforce` refuses every agent without `model:` and `effort:`, including
built-in types such as `general-purpose` and `Explore`. Run `tools/check_agents.py` first; it changes nothing.

`log_summary.py` finds the log the way the hooks do: `CCUC_LOG_DIR`, else `log_dir` from the `config.json` of
your installation (`~/.claude/hooks/usage-clock/config.json`), else the default folder. `--config PATH` takes
`log_dir` from another config file; or pass the path of a log file.

The log (`~/.cache/claude-usage-clock/log.jsonl`) records, per decision: the time, the hook, the decision and
its mode, and a reason. Values from the input appear only as short strings of at most 100 characters: the tool
name, the agent id, the agent type (`agent_type`), and of an Agent call the subagent type (`subagent_type`) and
the model in the call (`model_in_call`). Everything else from the input appears only as the
*names* of the input fields, never with its values. In the reason every quoted value (between `"…"`, `'…'`,
`„…“` or backticks) is replaced by `…`, so it says what was wrong, for example
`agent call 1: model "…" unknown`, but not the value. The log never records prompts, commands or script text.
The answer to the model is not masked: the model sees the value it has to fix.

## Configuration

`~/.claude/hooks/usage-clock/config.json`, every key optional; defaults in
[`config.example.json`](config.example.json). A broken file or a wrong value falls back to the default for
that key and writes one line to the log.

| Key | Default | Meaning |
|---|---|---|
| `usage_file` | `~/.claude/rate-limit.json` | Shared usage file written by the status line. |
| `log_dir` | `~/.cache/claude-usage-clock` | Log and per-agent counters. |
| `display.show_usage` / `show_time` | `true` / `true` | Parts of the prompt line. |
| `statusline.yellow_from` / `red_from` | `50` / `75` | Colours. |
| `statusline.week_stop_marker` | `true` | Show `<- weekly limit reached` at the weekly threshold. |
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

The mode and `--week-ok-until` are arguments in the hook command in `settings.json`, not in the config file.

### Model names, effort values and exceptions

The spawn gate checks the `model` and `effort` of every agent. Two keys decide what counts as valid:

- `spawn_rules.model_pattern` is a regular expression that has to match the **whole** model value
  (`re.fullmatch`). It applies to the literal `model` of every `agent(...)` call in a workflow script. (For an
  agent definition the gate only requires that a `model:` is set.) The default accepts the
  aliases `sonnet`, `opus`, `haiku`, `fable` and `inherit` and any full name that starts with `claude-`. If a
  workflow script names a model the pattern does not match, for example a Bedrock or Vertex name with dots or a
  colon, the gate denies it with `model "…" unknown`. Extend the pattern, for example
  `sonnet|opus|haiku|fable|inherit|claude-[a-z0-9-]+|my-proxy/[a-z0-9.-]+`.
- `spawn_rules.effort_values` is the list of accepted `effort` values, in the frontmatter of agent definitions
  and in workflow calls. To allow only some, list only those, for example `["low", "medium", "high"]`.

A pattern that is not a valid regular expression, or an empty list, falls back to the default and writes one
line to the log.

`spawn_rules.exceptions` lists agent types that start without the model rule. The defaults are
`claude-code-guide` and `statusline-setup`: they are built-in agent types of Claude Code without a definition
file you could add `model:` and `effort:` to, so the model rule would report or deny them every time. Exceptions apply to the Agent tool and to skill forks, not to `agent(...)` calls inside a workflow script.
Setting `exceptions` replaces the default list, so include the two defaults if you still want them.

## Recipe: wake up when the window resets

Claude Code can schedule a one-time prompt in a running session. Ask it, for example:

> Read `~/.claude/rate-limit.json`, take `five_hour.resets_at`, and schedule a one-time prompt two minutes
> after that time: "The usage window has reset. Continue with the next step from where you stopped."

Claude then uses its scheduling tool (`CronCreate`, one-shot) or `/loop`. The session has to stay open; a
one-shot task survives `--resume`. If you only want Claude Code to wait and continue by itself, install with
`--auto-continue` instead.

## Limits and threat model

- `rate_limits` is what Claude Code currently passes to the status line. It is documented, but it is not a
  versioned API; if it changes, the tools report `unknown` rather than wrong numbers.
- The usage file and the counters are ordinary files. A model that can write them can fake them. The mode and
  the weekly approval sit in `settings.json` and the thresholds next to the hooks: if you protect those paths
  from the model (for example with a guard hook), it cannot switch the gates off; if you do not, these tools
  are a seatbelt, not a lock.
- Agent discovery covers project agents (`.claude/agents`) and user agents (`~/.claude/agents`), not plugin or
  managed agents; the model rule reports or denies those unless they are listed in `spawn_rules.exceptions`.
- The date and time in the prompt line are your local time and go to the model with every prompt.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q --cov=hooks --cov=tools
```

[`docs/architecture.md`](docs/architecture.md) is the contract between the parts.

## License

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 inoX-Network.
