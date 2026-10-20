# AGENTS.md

For coding agents working on this repository. [CONTRIBUTING.md](CONTRIBUTING.md) has the rules for everyone; this file
adds what an agent tends to get wrong. [`docs/architecture.md`](docs/architecture.md) is the contract between the
parts.

## The repository

- `hooks/` (the hooks and the status line), `tools/` (installer, report, log summary), `windows/cli/` (the Windows
  module): Python 3.10 or newer, standard library only at runtime. `desktop/` is the plugin for the Claude desktop app
  (TypeScript, its own tests: [`desktop/README.md`](desktop/README.md#tests)).
- An installed copy lives in `~/.claude/hooks/usage-clock/` and writes to `~/.cache/claude-usage-clock/`. Never edit
  either: change the repository and run `tools/install.py`.

## Tests

- Write the test first and see it fail, then change the code. Every new branch of code gets a test.
- Linux and macOS: `.venv/bin/python -m pytest tests/ -q -rs`.
  Windows: `python -m pytest tests/ windows/cli/tests/ -q -rs`.
- Coverage: add `--cov` without a module name. `.coveragerc` sets the sources and `patch = subprocess`, so hooks that
  tests start as child processes are counted too.
- The CI checks the output with `.github/check_pytest_output.py` (Windows: `--allow-posix-only-skips`). It reads the
  last line as the summary: give it the pytest output unchanged, with nothing appended.
- The only skip allowed is `@pytest.mark.posix_only`, and only on Windows. Any other skip fails the CI.

## Never touch the real machine

- The root [`conftest.py`](conftest.py) walls off the real `~/.claude` and points `HOME`, `USERPROFILE`,
  `CCUC_LOG_DIR`, `CCUC_USAGE_FILE` and `CCUC_CONFIG` into `tmp_path`. That holds only for tests pytest loads with it.
- A probe or script outside `tests/` does not get it by itself: start it from the repository root with
  `PYTHONPATH=.:tests:hooks:tools python -m pytest -p conftest PROBE.py` (Windows: `PYTHONPATH='.;tests;hooks;tools'`)
  and request the fixture `git_alone` of `tests/test_sync_git.py` for anything that runs git. Check that `~` is not the
  real home before it writes anything.
- Never run the installer against the real `settings.json` while you test: pass `--settings` and `--target` into a
  temporary folder.
- No test reaches GitHub: `git_alone` points the sync URLs to local bare repositories.

## Text and files

- English in code, comments and documentation.
- Line feeds only (`.gitattributes`). On Windows, check that no editor switched a file to CRLF.
- Output of Windows programs in tests: compare bytes, not text (the code page may be 850, not UTF-8).
- The documentation says what the code does, in the same change: `tests/test_readme.py` checks the option and
  configuration tables against the code, `tests/test_doc_links.py` every relative link and anchor.

## What must hold

- Nothing is written through a planted link: files are written as `_context_state.write_private` does (temporary file
  opened without following a link, then `os.replace`), and folders that must stay inside are checked with
  `_plat.is_folder`, which also refuses Windows junctions.
- The log, the history and the report never hold prompt text, commands or file contents.
- The installer changes only its own entries in `settings.json`. It creates a missing `config.json` from
  `config.example.json`; in an existing one it changes only `sync.mode` and `sync.repo`.
- The sync ([docs/sync.md](docs/sync.md)): a repository that is public, or whose visibility is unclear, gets nothing.
  Nothing exported is thrown away (no reset, clean or stash of the own folder), nothing is merged (rebase only), and
  no attribute of the repository applies. Every git call goes through `Clone.git`, `_sync_core.run_command` or
  `_sync_core.scan_command`, never through `subprocess` directly.

## In a session where the hooks run

The hooks tell the model what to do while it works: the usage line with every prompt, the context reporter, the reason
of every gate decision, and the hint to start the wake-up watcher. When the watcher fires, the routine from
`wake_watcher.routine_file` is shown. Follow those lines. Never edit the usage file, the log folder, `config.json` or
`settings.json` to get past a gate. What each line means: [What the model sees](docs/statusline.md#what-the-model-sees)
and [the two gates](docs/gates.md#the-two-gates).
