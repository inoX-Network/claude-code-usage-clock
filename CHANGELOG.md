# Changelog

Each release with its date; the full notes are on [GitHub](https://github.com/inoX-Network/claude-code-usage-clock/releases).
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The line of the desktop app (plugin 0.10.0) stands in a row of its own above the prompt, as wide as the transcript
  and in colour, instead of the footer, which cut it after the week. `modules.desktop_line` in `config.json` says
  where: `full` (the default) above the prompt, `short` in the footer as before, `off` nowhere. The Settings tab of
  `/report` has a row for it; the plugin reads the key again with every measurement.

### Changed

- The plate of the active choice in the Settings tab (plugin 0.10.4) is round like the app's buttons: a frame in the
  accent colour with no padding, so that it stays as high as the other choices.
- The active choice in the cards of the Settings tab (plugin 0.10.3) stands on a plate of the accent colour again, as
  the open tab does, but without its frame and without spaces around the label, so that it stays as high as the other
  choices. The app's primary button, which marked it in 0.10.2, is white.
- The cards of the Settings tab in the desktop app (plugin 0.10.2) mark the active choice with the app's own primary
  button instead of the plate of the open tab, which made it twice as large: three choices reached past the edge of a
  half-width card. The frame of the choices wraps inside the card, and `i` stands in the head beside the state, where
  it no longer falls onto a line of its own.
- The Settings tab (plugin 0.10.1) shows no card for the status line while it is installed: it has no switch, and the
  card only said so. Without the status line a warning card says that nothing measures. The card of the desktop line
  says that the line moves after Claude's next answer.
- The Settings tab in the desktop app (plugin 0.9.3) draws its parts as the cards of the other views, two side by side
  where the pane is wide enough, the sync and its repository across the width. The app draws its own buttons and
  ignored the colour of the marked choice, so nothing showed which one was active: now the active choice of every
  group stands on the plate of the open tab, the choices in a frame with `i` beside it, and the head of each card
  names the state in effect (`ON`, `OFF`, `SHADOW`, `ENFORCE`, `NOT INSTALLED`). The sync repository has one Check,
  the button of the field (the app drew its own beside ours); the info is in normal type with `Docs: <file>#<anchor>`
  as a link. The terminal keeps its look.

### Security

- A named pipe planted as the log, `config.json`, Claude Code's `settings.json` or a soft-stop counter hung the gates
  until Claude Code's timeout, which lets the call through: a planted pipe switched the spawn gate off. Every file
  the gates read is opened without waiting and read only as a regular file below a size limit, the log is written
  only if it is a regular file, and a counter holding `Infinity` reads as zero. The installer gives the spawn gate in
  enforce `onFailure: "block"` (Claude Code 2.1.295 or newer), so a gate that still does not answer in time denies;
  `--check` names an entry without it. Run `install.py` again to add it.

### Fixed

- The info that `i` unfolds in the Settings tab broke mid-sentence, as it took the lines the installer's assistant
  breaks for a terminal: it is joined into paragraphs again, a new line only at `Writes:` and `More:` (terminal and
  desktop app; the assistant prints it as before).

## [1.7.0] — 2026-10-21 — installer assistant, Settings tab, Claude Code on the web

### Added

- Claude Code on the web: the skill `/usage-export` (`skills/usage-export/SKILL.md`) and `tools/web_export.py` put the
  usage of a chat into the sync repository at its end, as the machine `claude-web` (one machine id per chat, shadow
  mode unless `on`, the regular push of the sync). A request a continued chat exports again counts once by its key.
  The calls that never reach the transcript (the memory pass `extract_memories`) come from the debug log, numbers
  only, as the new line kinds `side` and `side_window` of the sync repository; the report shows them apart from the
  sums (`Side calls outside the transcripts`, `side_calls` in `--json`), and a session without a debug log as not
  measured, never as zero. `--machine claude-web` takes every chat, and `Sources:` shows the machines of one name as
  one part (`claude-web (12 devices) 2 h ago`). See [docs/sync.md](docs/sync.md#claude-code-on-the-web).
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

- `--on COMPONENTS` and `--off COMPONENTS` switch installed parts (`usage-clock`, `context-reporter`, `spawn-gate`,
  `soft-stop`) on and off in `modules` of `config.json`, with the values and the way of writing of the Settings tab;
  their hooks stay. A part must be installed or in `--components` of the same run; without `--components` the run
  installs nothing and only switches. See [docs/install.md](docs/install.md#install).

### Changed

- `--sync-mode off` works without the component `sync`: it only writes `sync.mode` `off` into `config.json` (the
  installer's answer no to the sync). With the sync installed it is an argument error; `--components sync --sync-mode
  off` switches it off and keeps its hooks, `--uninstall --components sync` removes it.
- The assistant's no to an installed part switches it off and leaves it installed, instead of leaving it on: `--off`
  for the usage clock, the context reporter and the gates, `sync.mode` `off` for the sync, whose hooks, repository and
  name stay (it no longer removes the sync). A part switched off is offered as no with `Now: off (your choice).`, and
  yes switches it on again (the sync in shadow mode). Only the status line, which has no switch, stays installed on
  no, with the command that removes it.

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
  works, the check now asks the GitHub API once, anonymously, without redirects: `"private": true` for this very
  repository makes it private, with the reason `credentials_injected` in the installer output, in `--check` and as
  one log line; only an explicit `"private": false` (or `visibility` public or internal) makes it public. No answer
  (a timeout, the rate limit, a redirect, an error, a name that does not fit) leaves it unclear: nothing is pushed,
  the next run asks again, and the failure names why. Without such a proxy nothing changes. See
  [docs/sync.md](docs/sync.md#install).
- A sync repository that does not exist, or that the login cannot reach, was reported as missing credentials, and
  the installer told you to sign in. The error now says `the repository does not exist or this login cannot reach it
  (check sync.repo)`, and the installer tells you to check the name or create the repository
  (`gh repo create owner/name --private`). The category stays `auth`.
- `/usage-export`: the window of a debug log whose clock jumped back ran backwards and was left out of the export, so
  the requests of that chat counted neither inside nor outside a debug log. The span is now the earliest and the
  latest time of the log. A last line that Claude Code was still writing counted with its numbers cut short; a line
  counts only with its line feed now.

### Security

- The visibility check of the sync: urllib's timeout holds for each operation of the socket, so a proxy that trickled
  its answer to `CONNECT`, the TLS handshake or the head could stretch the request to the GitHub API without end. The
  whole request now ends as `timeout` (unclear, no push) within its seconds, also in the installer and the Settings
  tab, which have no watchdog.
- `/usage-export` reads the debug log in lines of at most 64 KB (a longer one is passed over without being held in
  memory), opens `log.jsonl` never through a link and never waits on a FIFO, and prints the reason and category of a
  failed push only with printable characters. The installer and the push run with `python -I`.
- The report: the windows of the debug logs of a session are sorted and merged, so a machine that writes thousands of
  windows into the sync repository no longer makes `usage_report.py --sync` compare every request with each of them.
- [docs/security.md](docs/security.md#limits-and-threat-model) names what `/usage-export` reads and runs in the VM,
  and that the skill pins a release tag, not a commit.

The installer, the Settings tab and `modules` (part B of the security review after 1.6.0):

- A gate that `modules` of `config.json` switches off, or sets to shadow while its entry says `--mode=enforce`, now
  shows: `spawn gate off` / `soft stop shadow` in red after the week, in the status line and in the line of the desktop
  app (plugin 0.9.2), and as a `NOTE` of `install.py --check`, which also notes the usage clock or the context
  reporter switched off. `log_summary.py` lists the lines `disabled` per hook. [docs/security.md](docs/security.md#working-with-a-guard-hook)
  and [docs/configuration.md](docs/configuration.md#configuration) say that `settings_cli.py set` and
  `install.py --off`/`--on`/`--mode` write `config.json` themselves, which a guard that checks only paths does not see,
  and how to make it a lock.
- `config.json`: a backup never reads through a link, the ten newest `config.json.bak-usage-clock-*` stay (the tab
  wrote one per click), and the tab and the installer write it one at a time under `config.json.lock-usage-clock`.
- The Settings tab: a relative log folder is refused for the sync as the installer refuses it (the clone landed below
  the folder of the plugin); a failed file operation is a JSON answer with exit 1 and leaves no temporary file;
  control characters of `config.json` or of a typed name never reach the pane; a name that is no owner/name starts no
  process; Save waits as long as `set-repo` can take (360 s, checked against the budgets of the code).
- The installer: the name `claude-web` is refused outside Claude Code on the web (the report spares that name its
  warnings); a name of only spaces is refused; the assistant passes the name and `--week-ok-until` as one word, so a
  name that starts with a dash or a weekly approval that is no date no longer stops it after `Install now?`; run without
  options and without a terminal it says on stderr that it installs without a question; its `Same as` line is quoted
  for cmd and PowerShell on Windows; `--check` notes a `CCUC_CONFIG` that only the `env` of `settings.json` sets.
- A part switched off whose list of sessions is nested too deep no longer makes the spawn gate report an internal
  failure.

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

[Unreleased]: https://github.com/inoX-Network/claude-code-usage-clock/compare/v1.7.0...HEAD
[1.7.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.7.0
[1.6.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.6.0
[1.5.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.5.0
[1.4.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.4.0
[1.3.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.3.0
[1.2.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.2.0
[1.1.1]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.1.1
[1.1.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.1.0
[1.0.1]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.0.1
[1.0.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.0.0
