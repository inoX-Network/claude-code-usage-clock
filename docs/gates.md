# Spawn gate and soft stop

[README](../README.md) · [All documentation](README.md)

## The two gates

**Spawn gate** (Agent, Workflow and forked Skill starts, in the main session and in subagents). It has four brakes,
and each has its own setting in `config.json` (or in the Settings tab of `/report`): `off` (the brake does nothing),
`shadow` (the start goes through and the model is told that `deny` would have stopped it, so you see what the brake
would do before you rely on it), `warn`, `ask` (the 5h and the weekly brake) or `deny`. The defaults are the
behaviour of earlier versions. When several brakes act, the strongest wins: a deny beats an ask, an ask a warning.

- 5h usage at or above `start_block_5h` (75 %): `thresholds.five_hour_action` decides; `deny` (default): no new
  block until the window resets.
- Weekly usage at or above `start_block_week` (75 %): `thresholds.week_action` decides, unless `--week-ok-until`
  covers today (then it only warns). `warn` (default) starts with a warning, `deny` refuses, and `ask` shows
  Claude Code's permission prompt, so you confirm each start yourself; only a person can answer it, the model
  cannot fake a yes. The question is short, number first: `WEEK 83 % · start arbeiter-bauen?`, in German
  (`WOCHE 83 % · arbeiter-bauen starten?`) when `language` in Claude Code's `~/.claude/settings.json` is German, in
  English for every other language. Claude's phone app does not show that question, only the agent's description,
  so for an Agent start the gate also puts the number in front of the description (`WOCHE 83 % · Check the logs`);
  the started agent keeps that description. "Yes, and don't ask again" in the prompt does not switch the gate off:
  the next start asks again (tested 18 Oct 2026). Reasons and notes for the model stay English. In permission modes that may skip the prompt (`bypassPermissions`, `dontAsk`, `auto`, or
  no mode in the hook input) `ask` refuses instead, until you have seen the prompt appear in your setup and set
  `thresholds.ask_verified_in_bypass: true`. The same holds for `five_hour_action: ask` (`5H 80 % · start
  arbeiter-bauen?`); when both windows ask, one question names both numbers (`WEEK 85 % / 5H 80 % · start
  arbeiter-bauen?`).
- Unknown or stale usage: a single agent may start (with a warning). A larger block (more than
  `unknown_usage_max_agents`) is what `thresholds.unknown_usage_action` says; `deny` (default) refuses it.
- Model rule: every agent should have `model:` and `effort:` in its definition's frontmatter. Built-in types
  without a definition file, such as `general-purpose`, never do; define your own agents instead, or list types
  in `spawn_rules.exceptions`. In workflow scripts each `agent(...)` call needs literal `model` and `effort`
  options, or an `agentType` with a complete definition; `cond ? 'a' : 'b'` with two literals is fine,
  anything the gate cannot check statically counts as a finding.
- `spawn_rules.model_effort_action` decides what a finding does: `warn` (default) starts the agent and tells the
  model what is missing, `shadow` starts it too and adds that `deny` would refuse it, `deny` refuses the start,
  `off` skips the model rule and keeps only the usage part.
  The level covers the whole model rule: with `warn`, `skill_fork_missing_agent` and `model_override_in_call`
  set to `deny` also only warn.
  [`docs/agents.md`](agents.md) explains how to give your agents a model and an effort.
- It fails closed: if the gate itself breaks, starts are denied (in `enforce` mode). The one exception is a
  skill: an internal error while checking it only warns.

Why the model rule exists: two audit runs started agents without a model set, so most of them ran on the most
expensive model, simple counting jobs included. Together the two runs used about a third of the weekly limit
(34 %, measured from the session transcripts). The spawn gate makes sure that does not happen silently again.

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
counts only without options and only with `%s`, `%b` and `%%` in its format (flags, a width and a precision
such as `%-8s` are fine, `*` is not): an option such as `-v` or a
numeric conversion such as `%d` can run code in bash and zsh, so such a command is no checkpoint write.
On Windows the path must be a drive path (`C:\...`, `C:/...` or `/c/...`, no network share); the exact rules are in
[`windows/README.md`](../windows/README.md#soft-stop).
An agent that has saved once must save again at least every 15 calls, whatever the usage. Tell your agents in
their prompt where their checkpoint file is. Unknown or stale usage never triggers the three levels above; the
agent gets one warning instead.

### From shadow to enforce

```sh
python3 tools/log_summary.py                 # what the gates decided, and what they would have denied
python3 tools/log_summary.py --since=2026-01-14T00:00
python3 tools/check_agents.py                # which agents the model rule would report or deny
python3 tools/install.py --mode enforce      # when the log looks right
```

With `model_effort_action: deny`, `enforce` refuses every agent without `model:` and `effort:`, including
built-in types such as `general-purpose` and `Explore`. Run `tools/check_agents.py` first; it changes nothing.
It searches the agents of the current folder first (`--project DIR` for another one), reads the `spawn_rules`
of your installation (`--config PATH` for another file) and exits with 1 if it reports anything.

`log_summary.py` finds the log in this order: `log_dir` from `--config PATH`, else `CCUC_LOG_DIR`, else
`log_dir` from the config file in `CCUC_CONFIG` or, without it, of your installation
(`~/.claude/hooks/usage-clock/config.json`), else the default folder. Or pass the path of a log file.

The log (`~/.cache/claude-usage-clock/log.jsonl`) records, per decision: the time, the hook, the decision and
its mode, and a reason. Values from the input appear only as short strings of at most 100 characters: the tool
name, the agent id, the agent type (`agent_type`), the permission mode (`permission_mode`), and the subagent type (`subagent_type`) and model
(`model_in_call`) of the tool input, which in practice only an Agent call carries. Everything else from the input appears only as the
*names* of the input fields, never with its values. In the reason every quoted value (between `"…"`, `'…'`,
`„…“` or backticks) is replaced by `…`, so it says what was wrong, for example
`agent call 1: model "…" unknown`, but not the value. The log never records prompts, commands or script text.
The log folder is private to you (0700), and so are the log, the per-agent counters and the lock in it (0600),
whatever your umask; a log left readable by an earlier version is made private on its next write.
The answer to the model is not masked: the model sees the value it has to fix.
