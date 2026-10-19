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
- Agent discovery covers project agents (`.claude/agents`) and user agents (`~/.claude/agents`), not plugin or
  managed agents; the model rule reports or denies those unless they are listed in `spawn_rules.exceptions`.
  Skill discovery covers your personal skills and the project folders from the start folder up to the repo
  root, not plugin or managed skills or folders added with `--add-dir`.
- The date and time in the prompt line are your local time and go to the model with every prompt.
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
| `~/.claude/hooks/usage-clock/` | The hooks and `config.json` with the thresholds. |
| `~/.claude/rate-limit.json` (your `usage_file`) | The shared usage numbers. |
| `~/.cache/claude-usage-clock/` | Log, per-agent checkpoint counters, context state, the context split of each session, measurement history, report archive, watcher heartbeats and the lock of the usage file (a model that holds it keeps the numbers from updating). |

List these exact paths. Many guards read path lists literally, so a pattern such as `rate-limit.json*` matches
nothing at all, not even the usage file itself. The temporary file `rate-limit.json.tmp.<pid>` needs no entry: it is
created fresh for every write, a leftover or planted link there is removed first, and it is never written through.
It stays next to the usage file because the final rename is atomic only within one file system.
