# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through GitHub: open the
**Security** tab of this repository and choose **Report a vulnerability**. Only the maintainer sees the report.

Helpful in a report: the version or commit, your operating system, the part concerned (status line, a hook, the
installer, the usage report, the desktop plugin, the sync) and the steps that show the problem.

This project is maintained on a best-effort basis, with no guaranteed response time.

## Supported versions

Fixes go into the latest release. Older releases are not patched; update to the latest one.

## What counts as a vulnerability here

The tools run locally, read Claude Code's own files and write into `~/.claude/` and `~/.cache/claude-usage-clock/`;
only the opt-in sync sends data, and only to a private GitHub repository of the user.
Examples of what is worth a report:

- a hook or the status line that can be made to run code, follow a planted link, or write outside its own files,
- the log, the history or the report writing prompt text, commands or file contents, which they must never do,
- a gate in `enforce` mode that lets through what its documentation says it denies,
- the installer changing `settings.json` entries that are not its own,
- the sync writing to a repository that was found public, sending anything beyond what
  [docs/sync.md](docs/sync.md#what-is-synced) lists, or letting the content of the sync repository run code on a
  machine (attributes, hooks, filters).

What the tools by design do not protect against is described in
[Limits and threat model](docs/security.md#limits-and-threat-model): on their own they are a seatbelt, not a lock.
A model that can write the usage file or edit `settings.json` can switch the gates off; that is a known limit, not a
vulnerability.
