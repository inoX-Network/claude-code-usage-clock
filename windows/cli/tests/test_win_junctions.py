"""Junctions (reparse points) in the folders of the sync: Windows only. os.lstat takes a junction for a folder
(S_ISDIR, no link; only st_file_attributes has FILE_ATTRIBUTE_REPARSE_POINT), so every place that must not be led
elsewhere refuses one, as it refuses a link on POSIX."""
import os
import sys

import _winapi
import pytest

import _plat
import _sync_core
import _sync_git
from conftest import ROOT

sys.path.insert(0, os.path.join(ROOT, 'tests'))
from test_sync_git import REPO, Device, failure_of, git, git_alone, remote, run_of  # noqa: E402,F401
from test_install_sync import Setup, check  # noqa: E402


def junction(link, target):
    os.makedirs(target, exist_ok=True)
    os.makedirs(os.path.dirname(str(link)), exist_ok=True)
    _winapi.CreateJunction(str(target), str(link))


def test_a_junction_made_here_is_no_folder(tmp_path):
    junction(tmp_path / 'j', tmp_path / 'real')
    assert _plat.is_folder(os.lstat(tmp_path / 'real')) and not _plat.is_folder(os.lstat(tmp_path / 'j'))


def test_a_junction_as_the_sync_folder_is_refused(tmp_path):
    junction(tmp_path / 'log' / 'sync', tmp_path / 'elsewhere')
    with pytest.raises(OSError):
        _sync_core.ensure_dir(str(tmp_path / 'log'))


def test_a_junction_as_the_clone_is_no_git_folder(tmp_path, remote):
    device = Device(tmp_path / 'a', clone=False)
    real = tmp_path / 'real-clone'
    git('clone', '--quiet', _sync_git.remote_url(REPO, 'https'), str(real))
    junction(device.clone, real)
    assert failure_of(_sync_git.open_clone, run_of(device)) == ('internal', 'the sync clone is no git folder')


def test_a_junction_as_the_git_folder_of_the_clone_is_no_git_folder(tmp_path, remote):
    device = Device(tmp_path / 'a')
    os.rename(os.path.join(device.clone, '.git'), tmp_path / 'real-git')
    junction(os.path.join(device.clone, '.git'), tmp_path / 'real-git')
    assert failure_of(_sync_git.open_clone, run_of(device)) == ('internal', 'the sync clone is no git folder')


def test_a_junction_as_the_hook_folder_is_refused(tmp_path, remote):
    device = Device(tmp_path / 'a')
    run = run_of(device)
    junction(os.path.join(run.directory, _sync_git.NO_HOOKS_DIR), tmp_path / 'hooks-elsewhere')
    assert failure_of(_sync_git.open_clone, run) == ('internal', 'no-hooks in the sync folder is no empty folder')


def test_a_junction_as_the_rebase_state_aborts_nothing(tmp_path, remote):
    device = Device(tmp_path / 'a')
    keep = tmp_path / 'keep'
    junction(os.path.join(device.clone, '.git', 'rebase-merge'), keep)
    (keep / 'precious.txt').write_text('stays\n', encoding='utf-8')
    assert failure_of(_sync_git.open_clone, run_of(device)) == (
        'internal', 'the rebase state of the sync clone is no folder: nothing is aborted')
    assert (keep / 'precious.txt').read_text(encoding='utf-8') == 'stays\n'


@pytest.fixture
def setup(tmp_path, capsys, monkeypatch, remote):
    return Setup(tmp_path, capsys, monkeypatch)


def test_the_installer_takes_a_junction_in_place_of_the_clone_for_no_clone(setup, tmp_path):
    real = tmp_path / 'real-clone'
    git('clone', '--quiet', _sync_git.remote_url(REPO, 'https'), str(real))
    _sync_core.ensure_dir(str(setup.log))
    junction(setup.clone, real)
    code, out = setup.sync()
    assert code == 1 and 'exists but is no clone' in out, out
    assert (real / 'README.md').is_file() and setup.data() == {}


def test_check_names_a_junction_as_the_clone_and_does_not_walk_into_it(setup, tmp_path):
    assert setup.sync()[0] == 0
    os.rename(setup.clone, tmp_path / 'moved')
    junction(setup.clone, tmp_path / 'moved')
    code, out = check(setup, '--components', 'sync')
    assert code == 1 and f'FIX  Sync clone: {setup.clone} is no git folder' in out, out
    assert 'Sync clone size' not in out
