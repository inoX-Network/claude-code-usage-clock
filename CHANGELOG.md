# Changelog

Each release with its date and its notes on GitHub. Versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

- The README is now a start page. The details moved, word for word, to pages under [`docs/`](docs/README.md):
  install, status line, gates, reports, configuration and security. Links to the old README sections still work.
- New: [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and this changelog.
- New test `tests/test_doc_links.py`: every relative link and anchor in the documentation resolves.
- Fixed: two status lines started side by side could write the same measurement twice into the history when a
  second boundary fell between them. A last line less than a minute ahead now counts as current.

## [1.5.0] — 2026-10-19 — the report as tiles

## [1.4.0] — 2026-10-18 — the report inside Claude Code

## [1.3.0] — 2026-10-17 — the startup context

## [1.2.0] — 2026-10-17 — Windows and the desktop app

## [1.1.1] — 2026-10-14 — a report anyone can read

## [1.1.0] — 2026-10-14 — usage reports

## [1.0.1] — 2026-10-13 — private log folder, tests as root

## [1.0.0] — 2026-10-13 — first release

[1.5.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.5.0
[1.4.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.4.0
[1.3.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.3.0
[1.2.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.2.0
[1.1.1]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.1.1
[1.1.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.1.0
[1.0.1]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.0.1
[1.0.0]: https://github.com/inoX-Network/claude-code-usage-clock/releases/tag/v1.0.0
