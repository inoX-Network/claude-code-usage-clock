"""Tests for the one way hooks/_setup.py writes config.json and settings.json: the backup (never through a link, and of
config.json only the newest few kept) and the lock around reading, changing and writing config.json."""
import json
import os

import pytest

import _setup
from conftest import lock_is_held


def backups(path):
    return sorted(p.name for p in path.parent.glob(path.name + _setup.BACKUP_INFIX + '*'))


def switch(path, n):
    return _setup.write_keys(str(path), {'modules': {'usage_clock': n % 2 == 0}}, True)


def test_a_backup_has_the_content_and_the_time_of_the_file(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{"a": 1}', encoding='utf-8')
    os.utime(path, ns=(1_000_000_000, 2_000_000_000))
    backup = _setup.make_backup(str(path))
    assert open(backup, encoding='utf-8').read() == '{"a": 1}'
    assert os.stat(backup).st_mtime_ns == 2_000_000_000


@pytest.mark.posix_only
def test_a_backup_never_reads_through_a_link(tmp_path):
    secret = tmp_path / 'secret'
    secret.write_text('not for a backup', encoding='utf-8')
    path = tmp_path / 'config.json'
    path.symlink_to(secret)
    with pytest.raises(OSError):
        _setup.make_backup(str(path))
    assert backups(path) == []


@pytest.mark.posix_only
def test_a_backup_keeps_the_mode_of_the_file(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{}', encoding='utf-8')
    path.chmod(0o640)
    assert os.stat(_setup.make_backup(str(path))).st_mode & 0o777 == 0o640


@pytest.mark.posix_only
def test_writing_over_a_link_without_a_backup_takes_no_mode_of_the_link(tmp_path):
    target = tmp_path / 'elsewhere'
    target.write_text('{}', encoding='utf-8')
    target.chmod(0o666)
    path = tmp_path / 'config.json'
    path.symlink_to(target)
    _setup.write_json(str(path), {'a': 1}, True, backup=False)
    assert not path.is_symlink() and os.stat(path).st_mode & 0o777 == 0o600
    assert target.read_text(encoding='utf-8') == '{}'


def test_writing_config_json_keeps_the_newest_backups_and_no_file_of_anyone_else(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{}', encoding='utf-8')
    foreign = [path.name + _setup.BACKUP_INFIX + name for name in ('mine', '20260101-000000-000000.txt')]
    other = 'settings.json' + _setup.BACKUP_INFIX + '20260101-000000-000000'
    for name in foreign + [other]:
        (tmp_path / name).write_text('x', encoding='utf-8')
    written = [switch(path, n) for n in range(_setup.BACKUPS_KEPT + 3)]
    left = [name for name in backups(path) if name not in foreign]
    assert left == sorted(os.path.basename(backup) for backup in written[-_setup.BACKUPS_KEPT:])
    assert all((tmp_path / name).exists() for name in foreign + [other])


@pytest.mark.posix_only
def test_a_link_with_the_name_of_an_old_backup_is_never_removed(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{}', encoding='utf-8')
    link = tmp_path / (path.name + _setup.BACKUP_INFIX + '20000101-000000-000000')
    link.symlink_to(tmp_path / 'elsewhere')
    for n in range(_setup.BACKUPS_KEPT + 1):
        switch(path, n)
    assert os.path.islink(link)


def test_settings_json_keeps_every_backup(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{}', encoding='utf-8')
    for n in range(_setup.BACKUPS_KEPT + 2):
        _setup.write_json(str(path), {'n': n}, True)
    assert len(backups(path)) == _setup.BACKUPS_KEPT + 2


def test_a_backup_that_cannot_be_removed_stays_and_the_backup_is_made(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text('{}', encoding='utf-8')
    for n in range(_setup.BACKUPS_KEPT):
        switch(path, n)

    def refused(name):
        raise PermissionError(13, 'in use', name)
    monkeypatch.setattr(_setup.os, 'unlink', refused)
    _setup.make_backup(str(path), _setup.BACKUPS_KEPT)
    assert len(backups(path)) == _setup.BACKUPS_KEPT + 1


def test_config_json_is_read_changed_and_written_under_its_lock(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text('{"log_dir": "~/x"}', encoding='utf-8')
    lock, held = str(path) + _setup.LOCK_SUFFIX, []
    read, replace = _setup.read_config, _setup._plat.replace

    def reading(*args):
        held.append(lock_is_held(lock))
        return read(*args)

    def replacing(source, destination):
        held.append(lock_is_held(lock))
        replace(source, destination)
    monkeypatch.setattr(_setup, 'read_config', reading)
    monkeypatch.setattr(_setup._plat, 'replace', replacing)
    switch(path, 1)
    assert held == [True, True] and not lock_is_held(lock)
    assert json.loads(path.read_text(encoding='utf-8')) == {'log_dir': '~/x', 'modules': {'usage_clock': False}}
