"""The pull of the usage report against local bare repositories (0034-SYNC-REPORT): what another machine pushed
reaches the report, the own commits of a shadow run are put on top of the remote, and a history that cannot be brought
together is never merged. The machines are those of tests/test_sync_git.py (its fixtures: git without the user's
config, the URLs of the sync pointing to bare repositories below tmp_path); no test needs the network."""
import json
import os
import shutil

import _sync_git
import sync_format
from test_sync_git import NOW, Device, git, git_alone, remote  # noqa: F401
from test_usage_report import local_zone, report_module  # noqa: F401

DAY = '2026-10-13'


def report_of(device, monkeypatch, capsys, *args):
    """The JSON report of `device`, run as this machine: its log folder and its config."""
    path = device.log.parent / 'config.json'
    path.write_text(json.dumps({'sync': device.cfg['sync']}), encoding='utf-8')
    monkeypatch.setenv('CCUC_CONFIG', str(path))
    monkeypatch.setenv('CCUC_LOG_DIR', str(device.log))
    device.projects.mkdir(parents=True, exist_ok=True)
    capsys.readouterr()
    assert report_module.main(['--projects-dir', str(device.projects), '--json', '--day', DAY, *args], now=NOW) == 0
    return json.loads(capsys.readouterr().out)


def machines(root, desk_mode='on', desk_clone=True):
    """A laptop and a desk; their requests get message ids of their own (both would count from m1)."""
    laptop, desk = Device(root / 'laptop'), Device(root / 'desk', mode=desk_mode, clone=desk_clone)
    laptop.count = 1000
    return laptop, desk


def merges(device):
    return git('log', '--merges', '--oneline', cwd=device.clone).stdout


def test_the_report_pulls_what_another_machine_pushed(tmp_path, remote, monkeypatch, capsys):
    laptop, desk = machines(tmp_path)
    laptop.request(session='llllllll-1')
    assert laptop.push() == 0
    report = report_of(desk, monkeypatch, capsys)
    assert report['coverage']['sync']['pull']['status'] == 'ok'
    assert desk.head() == remote.head()
    assert [(cli['id'], cli['machine']) for cli in report['clis']] == [('llllllll', f'machine-{laptop.id[:6]}')]


def test_own_commits_of_a_shadow_run_are_put_on_top_of_what_another_machine_pushed(tmp_path, remote, monkeypatch,
                                                                                   capsys):
    laptop, desk = machines(tmp_path, 'shadow')
    desk.request(session='dddddddd-1')
    assert desk.push() == 0                      # shadow: committed in the clone, not pushed
    laptop.request(session='llllllll-1')
    assert laptop.push() == 0                    # the histories went apart
    own = desk.head()
    report = report_of(desk, monkeypatch, capsys)
    assert report['coverage']['sync']['pull']['status'] == 'ok'
    git('merge-base', '--is-ancestor', remote.head(), 'HEAD', cwd=desk.clone)
    assert desk.head() != own and merges(desk) == ''
    assert {cli['id']: cli['machine'] for cli in report['clis']} == {'dddddddd': f'machine-{desk.id[:6]}',
                                                                     'llllllll': f'machine-{laptop.id[:6]}'}
    assert report['total']['requests'] == 2


def test_a_history_that_went_apart_while_the_own_folder_has_changes_is_not_merged_and_named(tmp_path, remote,
                                                                                            monkeypatch, capsys):
    laptop, desk = machines(tmp_path, 'shadow')
    desk.request(session='dddddddd-1')
    assert desk.push() == 0
    laptop.request(session='llllllll-1')
    assert laptop.push() == 0
    # an export that is not committed yet: a change in the own folder, which opening the clone leaves alone
    line = sync_format.encode('history', {'t': '2026-10-13T08:00:00Z', 'five': 1, 'week': 2, 'five_resets': None,
                                          'week_resets': None})
    sync_format.append(desk.clone, desk.id, sync_format.history_path(desk.id, '2026-10-13T08:00:00Z'), [line])
    own = desk.head()
    report = report_of(desk, monkeypatch, capsys)
    pull = report['coverage']['sync']['pull']
    assert (pull['status'], pull['category']) == ('diverged', 'conflict')
    assert desk.head() == own and merges(desk) == ''
    assert [cli['id'] for cli in report['clis']] == ['dddddddd']           # the laptop is not in the clone
    note = next(n for n in report['overview']['notable'] if n['rule'] == 'sync_pull')
    assert note['text'] == ('the sync history could not be brought together (the sync history went apart and the '
                            'clone has changes: not updated), nothing was merged: the other machines not pulled yet')


def test_own_commits_that_do_not_fit_onto_the_remote_are_not_merged(tmp_path, remote, monkeypatch, capsys):
    desk = Device(tmp_path / 'desk', mode='shadow')
    desk.request(session='dddddddd-1')
    assert desk.push() == 0
    remote.foreign_commit(f'{desk.own}/status.json', '{"v": 1}\n')       # the same file, another content
    own = desk.head()
    report = report_of(desk, monkeypatch, capsys)
    assert report['coverage']['sync']['pull']['status'] == 'diverged'
    assert desk.head() == own and merges(desk) == ''
    assert not any(os.path.exists(os.path.join(desk.clone, '.git', name)) for name in ('rebase-merge', 'rebase-apply'))
    assert desk.status() == ''


def test_a_remote_over_the_size_limit_is_not_pulled_and_the_report_uses_the_clone_as_it_is(tmp_path, remote,
                                                                                         monkeypatch, capsys):
    laptop, desk = machines(tmp_path)
    laptop.request(session='llllllll-1')
    assert laptop.push() == 0
    assert report_of(desk, monkeypatch, capsys)['coverage']['sync']['pull']['status'] == 'ok'
    remote.foreign_commit('big.bin', b'x' * (2 * 1024 * 1024))
    desk.cfg['sync']['max_clone_mb'] = 1
    own = desk.head()
    report = report_of(desk, monkeypatch, capsys, '--sync')
    pull = report['coverage']['sync']['pull']
    assert (pull['status'], pull['category'], pull['detail']) == (
        'failed', 'internal', 'the sync repository is larger than sync.max_clone_mb (1 MB): nothing was checked out; '
                              'raise the limit if this size is expected')
    assert desk.head() == own and not os.path.exists(os.path.join(desk.clone, 'big.bin'))
    assert [cli['id'] for cli in report['clis']] == ['llllllll']          # what the clone already had


def test_without_a_clone_the_report_shows_this_machine_and_names_the_installer(tmp_path, remote, monkeypatch, capsys):
    desk = Device(tmp_path / 'desk', clone=False)
    desk.request(session='dddddddd-1')
    report = report_of(desk, monkeypatch, capsys)
    assert report['coverage']['sync']['pull'] == {
        'status': 'failed', 'category': 'internal', 'last_pull': None,
        'detail': 'the sync clone is missing, run the installer with --sync-repo'}
    assert [m['own'] for m in report['coverage']['sync']['machines']] == [True]
    assert report['total']['requests'] == 1


def test_requests_of_the_own_folder_count_once_and_stay_after_the_transcripts_are_gone(tmp_path, remote, monkeypatch,
                                                                                       capsys):
    desk = Device(tmp_path / 'desk')
    desk.request(session='dddddddd-1')
    assert desk.push() == 0
    assert report_of(desk, monkeypatch, capsys, '--sync')['total']['requests'] == 1
    shutil.rmtree(desk.projects)                    # Claude Code deleted the transcripts
    report = report_of(desk, monkeypatch, capsys, '--no-pull')
    assert report['total']['requests'] == 1 and report['clis'][0]['machine'] == f'machine-{desk.id[:6]}'
    assert _sync_git.clone_dir(str(desk.log)) == desk.clone
