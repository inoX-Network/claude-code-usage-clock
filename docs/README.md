# Documentation

[README](../README.md)

## Start here

- [Getting started](getting-started.md): a first install, step by step, for people new to Claude Code.
- [Install, update and uninstall](install.md): every installer option, `--check`, what an uninstall leaves behind,
  and how to wake up when the usage window resets.

## How it works

- [The status line and what the model sees](statusline.md): the usage line with every prompt, the context reporter,
  the wake-up watcher, the status line, the Claude desktop app and the files the status line writes.
- [Spawn gate and soft stop](gates.md): what the two gates decide, checkpoint writes, and the way from shadow to
  enforce.
- [Giving your agents a model and an effort](agents.md): what the model rule of the spawn gate expects from an agent
  definition.
- [Configuration reference](configuration.md): every key of `config.json` with its default, and the rules for model
  names, effort values and exceptions.
- [The sync across machines](sync.md): the optional sync of the usage data of several computers through a private
  GitHub repository: install, shadow mode, what it sends, its limits, and a file sync of `~/.claude` beside it.

## Reports

- [Usage reports](reports.md): `usage_report.py`, the overview, the `/report` pane, what a session started with, and
  asking your agent about your usage.
- [The usage report in detail](usage-report.md): the full report, the allocation estimate, calibration and the day
  archive.
- [The report format](report-format.md): every line of the text report and every JSON field.

## Security and internals

- [Security: limits, threat model and guard hooks](security.md): what the tools cannot protect against, and which
  paths a guard hook should block.
- [Architecture](architecture.md): the contract between the parts.

## Other folders

- [`windows/`](../windows/README.md): commands, differences and limits on Windows.
- [`desktop/`](../desktop/README.md): the plugin for the Claude desktop app and the `/report` pane.
