#!/usr/bin/env python3
"""Reads the output of `pytest -q -rs` and fails a run that proves nothing.

- A run in which no test passed (nothing collected) fails.
- A run with skipped tests fails: a case that is skipped is a case nobody measures.
- Exception, only with --allow-posix-only-skips (the Windows job): a skip whose reason is exactly the one that
  conftest.py gives to tests marked `posix_only` (POSIX_ONLY_REASON). Every other skip still fails, and the skips
  that are listed must add up to the number the summary line reports.

Usage: check_pytest_output.py pytest.txt [--allow-posix-only-skips]. Exit code 0 if the run is fine, else 1.
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# "SKIPPED [3] tests/test_x.py:12: reason" (on Windows with backslashes in the path)
SKIPPED_LINE = re.compile(r'^SKIPPED \[(\d+)\] .*?:\d+: (.*)$')


def posix_only_reason() -> str:
    sys.path.insert(0, ROOT)
    from conftest import POSIX_ONLY_REASON
    return POSIX_ONLY_REASON


def count(summary: str, word: str) -> int:
    found = re.search(rf'(\d+) {word}\b', summary)
    return int(found.group(1)) if found else 0


def check(text: str, allowed_reason: str | None = None) -> list[str]:
    """The problems of this output; none means the run is fine. `allowed_reason`: the one reason that a skip may have."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    summary = lines[-1] if lines else ''
    problems = []
    if not count(summary, 'passed'):
        problems.append('No test passed - the suite collected nothing.')
    skipped = count(summary, 'skipped')
    if not skipped:
        return problems
    listed = [line for line in lines if line.startswith('SKIPPED')]
    if allowed_reason is None:
        return problems + ['Skipped cases are cases nobody measures:'] + listed
    total = 0
    for line in listed:
        found = SKIPPED_LINE.match(line)
        if found is None or found.group(2) != allowed_reason:
            problems.append(f'A skip that is not the POSIX-only skip: {line}')
        else:
            total += int(found.group(1))
    if total != skipped:
        problems.append(f'{skipped} cases were skipped but only {total} are listed as POSIX-only skips.')
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('output', help='file with the output of pytest -q -rs')
    parser.add_argument('--allow-posix-only-skips', action='store_true',
                        help='accept skips with the reason of the posix_only marker, and only those')
    args = parser.parse_args(argv)
    with open(args.output, encoding='utf-8', errors='replace') as f:
        problems = check(f.read(), posix_only_reason() if args.allow_posix_only_skips else None)
    for problem in problems:
        print(problem)
    if not problems:
        print('The run is fine.')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
