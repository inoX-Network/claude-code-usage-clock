# Contributing

[README](README.md) · [All documentation](docs/README.md)

Maintained on a best-effort basis. Issues welcome, no guaranteed response time.

If you open a pull request, it is ready when:

- the tests below pass on Linux, and on Windows if it touches anything platform-specific,
- the documentation says what the code does: `tests/test_readme.py` checks the option and configuration tables and the
  key texts against the code, and `tests/test_doc_links.py` checks every relative link and anchor,
- the text stays in English, like the rest of the repository.

Security problems go through [SECURITY.md](SECURITY.md), not through a public issue.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q --cov=hooks --cov=tools
```

Run the tests as a normal user. As root (for example in a container) the tests that need an unreadable file are
skipped, because root can read every file.

On Windows: `python -m pytest tests/ windows/cli/tests/ -q -rs`. The tests that need
symlinks, FIFOs, file modes, user ids or POSIX time zone rules carry `@pytest.mark.posix_only` and are skipped there, with
one fixed reason that the CI check accepts and no other; on Linux and macOS no test may be skipped
([`windows/README.md`](windows/README.md#tests)).

The desktop plugin has tests of its own, run with the Claude Code CLI (no login needed); the CI runs them on Linux and
Windows (job `plugin`) with the Claude Code version pinned in `.github/workflows/tests.yml`. The commands are in
[`desktop/README.md`](desktop/README.md#tests).

[`docs/architecture.md`](docs/architecture.md) is the contract between the parts.
