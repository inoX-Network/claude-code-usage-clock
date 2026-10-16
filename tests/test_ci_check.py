"""Tests for .github/check_pytest_output.py: the rule that a green run must not hide skipped cases."""
import importlib.util
import os
import shutil
import subprocess
import sys

import pytest

from conftest import POSIX_ONLY_REASON, ROOT

SCRIPT = os.path.join(ROOT, '.github', 'check_pytest_output.py')
spec = importlib.util.spec_from_file_location('check_pytest_output', SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

PASSED = '2113 passed in 94.51s (0:01:34)'


def skipped(count, reason, where='tests/test_x.py:12'):
    return f'SKIPPED [{count}] {where}: {reason}'


def output(*lines):
    return '\n'.join(lines) + '\n'


def test_a_clean_run_is_fine_with_and_without_the_allowance():
    assert checker.check(output('.....', PASSED)) == []
    assert checker.check(output('.....', PASSED), POSIX_ONLY_REASON) == []


@pytest.mark.parametrize('text', ['', '\n\n', 'no tests ran in 0.01s', '1 failed in 0.1s', '0 passed in 0.1s'])
def test_a_run_in_which_nothing_passed_fails(text):
    assert checker.check(text) and checker.check(text, POSIX_ONLY_REASON)


def test_any_skip_fails_without_the_allowance_even_the_posix_only_one():
    text = output(skipped(3, POSIX_ONLY_REASON), '2110 passed, 3 skipped in 90s')
    problems = checker.check(text)
    assert problems and any('nobody measures' in p for p in problems) and skipped(3, POSIX_ONLY_REASON) in problems


def test_the_posix_only_skip_is_accepted_with_the_allowance():
    text = output(skipped(3, POSIX_ONLY_REASON), skipped(2, POSIX_ONLY_REASON, 'tests\\test_y.py:7'),
                  '2108 passed, 5 skipped in 90s')
    assert checker.check(text, POSIX_ONLY_REASON) == []


@pytest.mark.parametrize('reason', ['root can read everything', 'needs named pipes', POSIX_ONLY_REASON + ' (and more)',
                                    'POSIX only', '', POSIX_ONLY_REASON.upper()])
def test_a_skip_with_any_other_reason_fails_even_with_the_allowance(reason):
    text = output(skipped(1, reason), '2112 passed, 1 skipped in 90s')
    problems = checker.check(text, POSIX_ONLY_REASON)
    assert problems and any('not the POSIX-only skip' in p for p in problems)


def test_one_bad_skip_among_good_ones_fails():
    text = output(skipped(3, POSIX_ONLY_REASON), skipped(1, 'running as a user that ignores directory permissions'),
                  '2109 passed, 4 skipped in 90s')
    problems = checker.check(text, POSIX_ONLY_REASON)
    assert any('ignores directory permissions' in problem for problem in problems)


def test_skips_that_the_summary_counts_but_the_list_does_not_show_fail():
    text = output(skipped(3, POSIX_ONLY_REASON), '2108 passed, 5 skipped in 90s')
    problems = checker.check(text, POSIX_ONLY_REASON)
    assert problems and '5 cases were skipped but only 3' in problems[0]


def test_a_summary_without_skips_but_with_a_listed_skip_line_is_still_fine():
    """The summary decides: a SKIPPED line of an earlier part of the output (a log) does not count."""
    assert checker.check(output(skipped(1, 'x'), PASSED), POSIX_ONLY_REASON) == []


def run_script(tmp_path, text, *args):
    path = tmp_path / 'pytest.txt'
    path.write_text(text, encoding='utf-8')
    return subprocess.run([sys.executable, SCRIPT, str(path), *args], capture_output=True, text=True, encoding='utf-8',
                          cwd=str(tmp_path))


def test_the_script_exits_with_zero_for_a_fine_run_and_one_for_a_bad_one(tmp_path):
    fine = run_script(tmp_path, output('.', PASSED))
    assert fine.returncode == 0 and 'The run is fine.' in fine.stdout
    bad = run_script(tmp_path, output(skipped(1, 'x'), '2112 passed, 1 skipped in 9s'))
    assert bad.returncode == 1 and 'nobody measures' in bad.stdout


def test_the_script_takes_the_reason_from_the_conftest_of_the_repository(tmp_path):
    text = output(skipped(2, POSIX_ONLY_REASON), '2111 passed, 2 skipped in 9s')
    assert run_script(tmp_path, text).returncode == 1
    assert run_script(tmp_path, text, '--allow-posix-only-skips').returncode == 0
    other = output(skipped(2, 'something else'), '2111 passed, 2 skipped in 9s')
    assert run_script(tmp_path, other, '--allow-posix-only-skips').returncode == 1


PYTEST_INI = '[pytest]\nmarkers =\n    posix_only: x\n'
TESTS = (
    'import pytest\n\n\n'
    'def test_fine():\n    assert True\n\n\n'
    '@pytest.mark.posix_only\ndef test_posix():\n    assert True\n\n\n'
    '{extra}'
)


def real_run(tmp_path, extra):
    """A real pytest run (with the conftest of the repository) and its real output."""
    folder = tmp_path / 'project'
    folder.mkdir()
    shutil.copy(os.path.join(ROOT, 'conftest.py'), folder / 'conftest.py')
    (folder / 'pytest.ini').write_text(PYTEST_INI, encoding='utf-8')
    (folder / 'test_sample.py').write_text(TESTS.format(extra=extra), encoding='utf-8')
    done = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-rs', '-p', 'no:cacheprovider'], capture_output=True,
                          text=True, encoding='utf-8', cwd=str(folder), env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    assert done.returncode == 0, done.stdout
    return done.stdout


def test_the_real_output_of_a_posix_only_test_passes_the_check_on_windows_and_runs_elsewhere(tmp_path):
    text = real_run(tmp_path, '')
    if os.name == 'nt':
        assert '1 skipped' in text and POSIX_ONLY_REASON in text
        assert checker.check(text) and checker.check(text, POSIX_ONLY_REASON) == []
    else:
        assert 'skipped' not in text and checker.check(text) == []


def test_the_real_output_of_a_test_skipped_for_another_reason_fails_the_check(tmp_path):
    text = real_run(tmp_path, '@pytest.mark.skip(reason="some other reason")\ndef test_other():\n    assert True\n')
    assert 'some other reason' in text
    problems = checker.check(text, POSIX_ONLY_REASON)
    assert problems and any('some other reason' in p for p in problems)
