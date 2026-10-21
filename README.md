# claude-code-usage-clock

[![Release](https://img.shields.io/github/v/release/inoX-Network/claude-code-usage-clock)](https://github.com/inoX-Network/claude-code-usage-clock/releases/latest)
[![Tests](https://github.com/inoX-Network/claude-code-usage-clock/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/inoX-Network/claude-code-usage-clock/actions/workflows/tests.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

New to Claude Code? Start with [docs/getting-started.md](docs/getting-started.md).

Claude Code shows you your usage. This makes the model see it too, and lets two gates decide from those numbers
whether new agents may start and running ones keep working.

A first install runs the gates in shadow mode: nothing is blocked yet, and the model tells you each time a gate
would have stepped in. Switch to `enforce` once the log looks right ([From shadow to enforce](docs/gates.md#from-shadow-to-enforce)).

![The status line: model, 5h usage with reset time, weekly usage, context size and prompt cache clock](docs/images/statusline.png)

The status line in a real session: the five-hour window at 57 % (yellow from 50 %) resets at 22:40, the week is
at 23 %, the context holds 485k of 1000k tokens (yellow from 300k), and the prompt cache stays warm for another
58 minutes.

Claude Code knows your plan limits, but the model working for you does not. It cannot tell whether starting
ten agents at 80 % of the five-hour window is a good idea, it does not know that it is 3 a.m., and an
overnight run happily spends the whole window. It cannot see how full its own context window is either. This
repo closes that gap with a status line and four hooks (five parts):

| Part | What it does | Maturity | Default |
|---|---|---|---|
| **Status line** | Shows model, 5h and weekly usage, the context size and the prompt cache clock in colour. Several sessions merge into one shared usage file, so every session and every hook sees the same, current numbers. | stable | on |
| **Usage clock** (`UserPromptSubmit`) | Adds one line of context to every prompt: usage, reset times, and the local date and time. Both parts switch independently. | stable | on |
| **Context reporter** (`UserPromptSubmit`, `PostToolUse`) | Tells the model how full its context window is, from the numbers the status line measured: with every prompt, and after a tool call once the context is half full. Needs the status line; subagents get nothing. | available | on |
| **Spawn gate** (`PreToolUse`: Agent, Workflow, Skill) | Stops new agent blocks above a usage threshold, and checks that every agent has a determinable model and effort (reports by default, can deny). | available | log only |
| **Soft stop** (`PreToolUse`: all tools) | Slows subagents down near the five-hour limit: warn, then only allow saving a checkpoint, then ask them to finish with a partial result. Never blocks the main session. | available | log only |
| **Sync** (`SessionStart`, `SessionEnd`) | Optional, not one of the five: puts the usage data of each of your computers into a private GitHub repository of yours, so the report shows all of them. Opt-in, starts in shadow mode (commits locally, pushes nothing); what it sends and its limits: [docs/sync.md](docs/sync.md). | available | off |

On top of these there are two optional helpers that need no settings entry: the [wake-up
watcher](docs/statusline.md#the-wake-up-watcher), off by default, and the plugin in [`desktop/`](desktop/README.md), which draws the
status line in the Claude desktop app and adds [`/report`](docs/reports.md#the-report-pane), a pane with the usage report.

![/report in the Claude desktop app: the week at 96 % of the weekly limit, the five-hour windows, the cost at API prices, the cache, the projects, the models and the agents](docs/images/report-desktop-overview.png)

`/report` in the Claude desktop app: where the week's usage went, by project, model and agent. Every view of the pane
is described in [Usage reports](docs/reports.md#the-report-pane).

"Log only" (`shadow` mode) means the gates decide and write their decision to a log, but block nothing. When a
gate would have denied, asked or warned, the model gets a short note instead ("Spawn gate (shadow mode, information
only): in enforce mode this start would have been denied. Reason: …") and tells you, or a subagent mentions it
in its report. Run them that way for a while, read the log, then switch to `enforce`.

## Requirements

- Claude Code on a **Pro or Max** subscription. The usage numbers come from the `rate_limits` field that
  Claude Code passes to the status line; API key, Bedrock and Vertex sessions do not get it, and neither does
  a session before its first response. Without it everything reports usage as `unknown`.
- Python 3.10 or newer as `python3` (the macOS system Python 3.9 is too old; install a current one).
- Linux, macOS, or Windows 11 with [Git for Windows](https://git-scm.com/downloads/win) (Claude Code runs hooks in Git
  Bash there). Windows has its own folder, [`windows/`](windows/README.md): the commands, the differences and the
  limits. In WSL it runs as on Linux.
- No third-party packages at runtime.

## Install

```sh
git clone https://github.com/inoX-Network/claude-code-usage-clock
cd claude-code-usage-clock
python3 tools/install.py               # asks y/n for each part, then installs on your yes
```

Just run it: in a terminal, without options, the installer asks one question per part (Enter takes the answer in
brackets), shows a summary with the equivalent command line and writes nothing before you confirm. For scripts, any
option skips the questions, and so does a stdin or stdout that is no terminal: then the options alone decide (none:
the five parts of the default selection).

```sh
python3 tools/install.py --dry-run     # shows what would change
python3 tools/install.py --components statusline,usage-clock,context-reporter,spawn-gate,soft-stop
                                       # all five parts without a question, the gates keep their mode (shadow at first)
```

Restart Claude Code (or open `/hooks`) after installing. On Windows, run `python tools\install.py` instead
([`windows/README.md`](windows/README.md)). Every installer option, `--check`, [updating](docs/install.md#update)
and uninstalling: [docs/install.md](docs/install.md). What is new: [CHANGELOG.md](CHANGELOG.md) and the
[releases](https://github.com/inoX-Network/claude-code-usage-clock/releases).

## Documentation

| Page | What it covers |
|---|---|
| [Getting started](docs/getting-started.md) | A first install, step by step, for people new to Claude Code. |
| [Install, update and uninstall](docs/install.md) | Every installer option, `--check`, what an uninstall leaves behind, waking up when the window resets. |
| [The status line and what the model sees](docs/statusline.md) | The usage line, the context reporter, the wake-up watcher, the status line, the desktop app, the files it writes. |
| [Spawn gate and soft stop](docs/gates.md) | What the two gates decide, checkpoints, and how to go from shadow to enforce. |
| [Usage reports](docs/reports.md) | `usage_report.py`, the overview, the `/report` pane, asking your agent about your usage. |
| [Configuration reference](docs/configuration.md) | Every key of `config.json` with its default, model names, effort values and exceptions. |
| [The sync across machines](docs/sync.md) | The optional sync: install, shadow mode, what it sends, its limits, and a file sync of `~/.claude` beside it. |
| [Security: limits, threat model and guard hooks](docs/security.md) | What the tools cannot protect against, and which paths a guard should block. |
| [All documentation](docs/README.md) | Every page, including the report details, the report format and the architecture. |

## Limits and threat model

On their own these tools are a seatbelt, not a lock. The usage file and the counters are ordinary files: a model
that can write them can fake them, and a model that can edit `settings.json` or the config can switch the gates
off. Pair them with a guard hook that blocks those paths. The usage numbers come from `rate_limits`, which Claude
Code documents but does not version; when it is missing, every part reports `unknown` instead of guessing. The report
counts what is in the transcripts: sessions started with `claude -p` count like any other, a session started with
`--no-session-persistence` writes no transcript and is not counted. The full list of limits and the paths to protect: [docs/security.md](docs/security.md).

## Contributing

How to run the tests on Linux, macOS and Windows: [CONTRIBUTING.md](CONTRIBUTING.md). Security problems:
[SECURITY.md](SECURITY.md). Changes per release: [CHANGELOG.md](CHANGELOG.md).

## Maintenance

Maintained on a best-effort basis. Issues welcome, no guaranteed response time.

## License

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 inoX-Network.

## Moved sections

This README used to hold every detail. Links to its old sections still lead to the right place:

- <a name="uninstall-completely"></a>[Uninstall completely](docs/install.md#uninstall-completely)
- <a name="what-the-model-sees"></a>[What the model sees](docs/statusline.md#what-the-model-sees)
- <a name="the-context-window"></a>[The context window](docs/statusline.md#the-context-window)
- <a name="the-wake-up-watcher"></a>[The wake-up watcher](docs/statusline.md#the-wake-up-watcher)
- <a name="what-the-status-line-shows"></a>[What the status line shows](docs/statusline.md#what-the-status-line-shows)
- <a name="in-the-claude-desktop-app"></a>[In the Claude desktop app](docs/statusline.md#in-the-claude-desktop-app)
- <a name="files-the-status-line-writes"></a>[Files the status line writes](docs/statusline.md#files-the-status-line-writes)
- <a name="the-two-gates"></a>[The two gates](docs/gates.md#the-two-gates)
- <a name="from-shadow-to-enforce"></a>[From shadow to enforce](docs/gates.md#from-shadow-to-enforce)
- <a name="reports-what-was-used"></a>[Reports: what was used](docs/reports.md#reports-what-was-used)
- <a name="the-overview"></a>[The overview](docs/reports.md#the-overview)
- <a name="the-report-pane"></a>[The /report pane](docs/reports.md#the-report-pane)
- <a name="what-a-session-started-with"></a>[What a session started with](docs/reports.md#what-a-session-started-with)
- <a name="ask-your-agent-about-your-usage"></a>[Ask your agent about your usage](docs/reports.md#ask-your-agent-about-your-usage)
- <a name="configuration"></a>[Configuration](docs/configuration.md#configuration)
- <a name="model-names-effort-values-and-exceptions"></a>[Model names, effort values and exceptions](docs/configuration.md#model-names-effort-values-and-exceptions)
- <a name="recipe-wake-up-when-the-window-resets"></a>[Recipe: wake up when the window resets](docs/install.md#recipe-wake-up-when-the-window-resets)
- <a name="working-with-a-guard-hook"></a>[Working with a guard hook](docs/security.md#working-with-a-guard-hook)
- <a name="development"></a>[Development](CONTRIBUTING.md#development)
