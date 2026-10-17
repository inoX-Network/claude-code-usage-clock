"""No test sees the Claude files of the real home folder (conftest.isolated_env), on any system."""
import os

import pytest

from conftest import REAL_HOME


@pytest.mark.parametrize('name', ['.claude', os.path.join('.claude', 'CLAUDE.md'), 'CLAUDE.md', 'CLAUDE.local.md', '.git'])
def test_the_claude_files_of_the_real_home_folder_are_walled_off(name):
    path = os.path.join(REAL_HOME, name)
    assert not os.path.exists(path) and not os.path.isfile(path) and not os.path.isdir(path)
    with pytest.raises(PermissionError):
        open(path, encoding='utf-8')
    with pytest.raises(PermissionError):
        os.stat(path)
    with pytest.raises(PermissionError):
        os.listdir(path)


def test_a_walk_from_the_temp_folder_does_not_reach_the_real_home(tmp_path):
    found = []
    folder = str(tmp_path)
    while True:
        found += [os.path.join(folder, '.claude')] if os.path.isdir(os.path.join(folder, '.claude')) else []
        if os.path.dirname(folder) == folder:
            break
        folder = os.path.dirname(folder)
    assert os.path.join(REAL_HOME, '.claude') not in found


def test_the_rest_of_the_real_home_folder_stays_readable(tmp_path):
    assert os.path.isdir(REAL_HOME) and os.path.isfile(__file__)
