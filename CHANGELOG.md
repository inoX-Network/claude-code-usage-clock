# Changelog

Each release with its date; the full notes are on [GitHub](https://github.com/inoX-Network/claude-code-usage-clock/releases).
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The installer asks: run in a terminal without any option, `tools/install.py` asks y/n for each part (status line,
  usage clock, context reporter, the two gates and their mode, auto-continue, the sync with its repository and the
  name of this computer). Each question says where its default comes from: `(current)` for what you have now,
  `(default)` for the safe start of something new; it starts with its step (`Step 4/8`), and the answer `h`, `i`, `?`,
  `help` or `--help` explains it (at the repository and the name `?`, `help` or `--help`, as `h` and `i` are answers
  there); a wrong answer names that way. It shows a summary with the equivalent command line and runs
  exactly that on your yes. Any option, or a stdin or stdout that is no terminal, and it asks nothing, as before. See
  [docs/install.md](docs/install.md#install).
- `--sync-name NAME` sets `sync.machine_name`, the name of this machine in the report (checked as `config.json`
  checks it; the host name is never used). It is the third key the installer writes into `config.json`.
- `modules` in `config.json`: `modules.usage_clock` and `modules.context_reporter` (`true`/`false`) switch those hooks
  off, `modules.spawn_gate` and `modules.soft_stop` (`off`/`shadow`/`enforce`) set the mode of the gates before the
  `--mode` of their hook entry; empty, as by default, the entry decides. Every hook reads it on its next run, no
  restart, `settings.json` untouched. `install.py --mode` writes it for the gates of the run, `--check` says where
  the mode comes from. See [docs/configuration.md](docs/configuration.md#modules-switch-parts-off).
- The Settings tab of `/report` (key `h`, plugin 0.9.0): switch the parts on and off, set the mode of the gates and of
  the sync, and check and save the sync repository (Save only after the check found it private and reachable). It
  writes through the new `hooks/settings_cli.py`, which the installer installs next to the hooks, with the check and
  backup of the installer. See [docs/reports.md](docs/reports.md#the-settings-tab).

### Changed

- `--sync-mode off` works without the component `sync`: it only writes `sync.mode` `off` into `config.json` (the
  installer's answer no to the sync). With the sync installed it is an argument error; `--uninstall --components sync`
  removes it.

### Fixed

- The Settings tab in the terminal (plugin 0.9.1): each part's name stands on a line of its own, with its sentence, its
  buttons and its info in one indented block below it, so that wrapped lines stay indented and every row of buttons
  starts in the same column; the link of the info has a line of its own. The status line has its reason under its
  sentence. The last success of the sync is a local time, as the report writes its times. The repository field has a
  frame, and Check and Save stand on a plate like the marked choice. The sentence of the sync leaves out what a first
  install does (the installer's assistant still says it). After a button the tab stays as it is while it reads the
  settings again, so that the focus stays on the button; only the first read says loading.
- The sync behind a proxy that adds credentials to every request (a Claude cloud container): the anonymous check read
  the private repository through the proxy, called it public and stopped the installation. When the anonymous read
  works, the check now asks the GitHub API once, anonymously, without redirects; only `"private": true` for this very
  repository makes it private, with the reason `credentials_injected` in the installer output, in `--check` and as
  one log line. Every other answer counts as public, as before; without such a proxy nothing changes. See
  [docs/sync.md](docs/sync.md#install).
- A sync repository that does not exist, or that the login cannot reach, was reported as missing credentials, and
  the installer told you to sign in. The error now says `the repository does not exist or this login cannot reach it
  (check sync.repo)`, and the installer tells you to check the name or create the repository
  (`gh repo create owner/name --private`). The category stays `auth`.

## [1.6.0] — 2026-10-20 — sync across machines

### Added

- The sync across machines, opt-in: the installer component `sync` (`--components sync --sync-repo owner/name`,
  `--sync-mode`) puts the usage data of each machine into a private GitHub repository, starting in shadow mode. A
  public repository, or an answer that does not say, stops the installation. `--check` and `log_summary.py` show the
  sync; `--uninstall` sets `sync.mode` to `off`. See [docs/sync.md](docs/sync.md).
- The report across machines: `usage_report.py` reads every machine of the sync repository (`--local`, `--machine`,
  `--no-pull`, `--sync`), and the `/report` pane names the machine of each session (plugin 0.8.0).
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), this changelog, and the test
  `tests/test_doc_links.py`: every relative link and anchor in the documentation resolves.
- [Update](docs/install.md#update) in the install page.

### Changed

- The README is now a start page. The details moved, word for word, to pages under [`docs/`](docs/README.md):
  install, status line, gates, reports, configuration and security. Links to the old README sections still work.

### Fixed

- Two status lines started side by side could write the same measurement twice into the history when a second
  boundary fell between them. A last line less than a minute ahead now counts as current.

### Security

- The sync never applies attributes of the repository (filters, line endings), refuses links and Windows junctions in
  its folders, and stops a clone or pull above `sync.max_clone_mb` (1024 MB) or 100 000 files before anything is
  checked out.

## [1.5.0] — 2026-10-19 — the report as tiles

## [1.4.0] — 2026-10-18 — the report inside Claude Code

## [1.3.0] — 2026-10-17 — the startup context

## [1.2.0] — 2026-10-17 — Windows and the desktop app

## [1.1.1] — 2026-10-14 — a report anyone can read

## [1.1.0] — 2026-10-14 — usage reports

## [1.0.1] — 2026-10-13 — private log folder, tests as root

## [1.0.0] — 2026-10-13 — first release

[Unreleased]: https://github.com/inoX-Network/claude-code-usage-clock/compare/v1.6.0...HEAD
[1.6.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.6.0
[1.5.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.5.0
[1.4.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.4.0
[1.3.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.3.0
[1.2.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.2.0
[1.1.1]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.1.1
[1.1.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.1.0
[1.0.1]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.0.1
[1.0.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.0.0
