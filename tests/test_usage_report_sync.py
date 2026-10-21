"""The usage report with the sync (0034-SYNC-REPORT): the pull before a report, the folders of the other machines in
the local clone merged with the local files, and how the report shows them. Invented data only; the clone is a
folder written with hooks/sync_format.py, the git work of the pull and the push is replaced by functions of the test
(the real fast forward is tested with local repositories in tests/test_sync.py). The clock and the time zone are those
of tests/test_usage_report.py."""
import json
import os
import shutil

import pytest

import _sync_core
import _sync_export
import _transcripts
import _sync_git
import sync
import sync_format
from test_usage_report import NOW, T, dollars, extras, local_zone, report_module, req, row, spend, world  # noqa: F401
from test_usage_report_golden import OUTPUTS, build, compare_golden, outputs

LAPTOP = '1aaaaaaa-0000-4000-8000-000000000001'
TABLET = '2bbbbbbb-0000-4000-9000-000000000002'
RESETS = {'five_resets': T('2026-10-13T14:00:00Z'), 'week_resets': T('2026-10-19T07:00:00Z')}


def request_line(t, message, cost, sid='llllllll-1', first=False, sub=None, proj='proj-l', **more):
    """A request line of the format, with `k` made as the export makes it from a transcript line of `paid`."""
    return {'t': t, 'k': sync_format.request_key(message, 'r-' + message), 'sid': sid, 'sub': sub, 'proj': proj,
            'model': 'claude-opus-5-5', 'in': 0, 'cw5': 0, 'cw1h': 0, 'cr': 0, 'out': dollars(cost), 'think': None,
            'ws': None, 'wf': None, 'first': first, **more}


def measured(t, five, week):
    return row(t, five, week, RESETS['five_resets'], RESETS['week_resets'])


class Clone:
    """The checked-out sync repository, written with the writers of the format."""

    def __init__(self, root):
        self.root = str(root)
        os.makedirs(self.root, exist_ok=True)

    def machine(self, machine_id, name='laptop', last_success='2026-10-14T11:00:00Z', utc_std=None):
        """machine.json and status.json of another machine; `utc_std` by default that of this machine."""
        sync_format.write_machine(self.root, machine_id, {
            'name': name, 'platform': 'linux', 'schema': 1, 'created': '2026-10-01T00:00:00Z',
            'utc_std': sync_format.utc_std() if utc_std is None else utc_std})
        if last_success is not None:
            sync_format.write_status(self.root, machine_id, {'last_success': last_success, 'tool_version': '1.0.0',
                                                             'schema': 1})

    def requests(self, machine_id, lines):
        for values in lines:
            sync_format.append(self.root, machine_id, sync_format.request_path(machine_id, values['t'], values['sid']),
                               [sync_format.encode('request', values)])

    def history(self, machine_id, rows):
        for values in rows:
            stamp = values['t'] if isinstance(values['t'], str) else report_module.iso(values['t'])
            values = {**values, 't': stamp}
            sync_format.append(self.root, machine_id, sync_format.history_path(machine_id, stamp),
                               [sync_format.encode('history', values)])

    def gates(self, machine_id, rows):
        for values in rows:
            sync_format.append(self.root, machine_id, sync_format.gate_path(machine_id, values['t']),
                               [sync_format.encode('gate', values)])

    def days(self, machine_id, lines):
        for day, stats in lines:
            raw = json.dumps({'v': 1, 'day': day, 'stats': stats})
            sync_format.append(self.root, machine_id, sync_format.day_path(machine_id, day),
                               [sync_format.encode_day(raw, 'plain', {}, '')])

    def raw(self, machine_id, relative, data):
        path = os.path.join(self.root, 'machines', machine_id, *relative.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        return path


class Synced:
    """This machine with sync.mode on: its id, the clone, and the pulls and pushes the report started."""

    def __init__(self, world, monkeypatch, tmp_path, mode='on'):
        world.configure(monkeypatch, sync={'mode': mode, 'machine_name': 'desk'})
        self.world, self.log = world, str(world.log)
        self.own = sync_format.load_machine_id(self.log)
        self.clone = Clone(_sync_git.clone_dir(self.log))
        self.pulls, self.pushes = [], []
        self.pull_error = self.push_error = None
        monkeypatch.setattr(sync, 'git_pull', self.pull)
        monkeypatch.setattr(_sync_git, 'push', self.push)

    def pull(self, run):
        self.pulls.append(run)
        if self.pull_error:
            raise _sync_core.SyncError(*self.pull_error)

    def push(self, run):
        self.pushes.append(run)
        if self.push_error:
            raise _sync_core.SyncError(*self.push_error)

    def state(self, **fields):
        _sync_core.write_state(_sync_core.ensure_dir(self.log), _sync_core.State(**fields))

    def read_state(self):
        return _sync_core.read_state(os.path.join(self.log, 'sync'))


@pytest.fixture
def synced(world, monkeypatch, tmp_path):
    return Synced(world, monkeypatch, tmp_path)


def two_machines(world, synced, laptop_history=False):
    """This machine (desk) and a laptop, active at the same time: a request on each between 10:00 and 10:10, then a
    request on the laptop only up to 10:20. The subscription rose 4 points (5h) and 2 points (week) in each stretch."""
    rows = [measured('2026-10-13T10:00:00Z', 10, 20), measured('2026-10-13T10:10:00Z', 14, 22),
            measured('2026-10-13T10:20:00Z', 18, 24)]
    world.history(rows)
    spend(world, ('2026-10-13T10:05:00Z', 1.0), session='dddddddd-1')
    synced.clone.machine(LAPTOP)
    synced.clone.requests(LAPTOP, [request_line('2026-10-13T10:05:00Z', 'l1', 1.0, first=True),
                                   request_line('2026-10-13T10:15:00Z', 'l2', 1.0)])
    if laptop_history:
        synced.clone.history(LAPTOP, [{**r, 't': T(r['t']) + 30} for r in rows])
    return rows


def by_id(report):
    return {cli['id']: cli for cli in report['clis']}


# --- Criterion: two machines at the same time ------------------------------------------------------------------

def test_a_rise_while_two_machines_were_active_is_shared_by_both_and_nothing_is_unexplained(world, synced):
    two_machines(world, synced)
    alone = world.report('--day', '2026-10-13', '--local')
    assert alone['allocation']['five']['unexplained_points'] == 4 and alone['allocation']['week']['unexplained_points'] == 2
    both = world.report('--day', '2026-10-13')
    for metric, rise in (('five', 8), ('week', 4)):
        assert both['allocation'][metric]['counted_points'] == rise
        assert both['allocation'][metric]['unexplained_points'] == 0
        assert both['allocation'][metric]['attributed_points'] == rise
    clis = by_id(both)
    assert clis['dddddddd']['estimated_share'] == {'five': 2, 'week': 1}
    assert clis['llllllll']['estimated_share'] == {'five': 6, 'week': 3}
    assert (clis['dddddddd']['machine'], clis['llllllll']['machine']) == ('desk', 'laptop')
    assert clis['llllllll']['label'] == 'proj-l' and clis['llllllll']['requests'] == 2
    assert both['total']['requests'] == 3 and both['total']['cost_usd'] == pytest.approx(3.0)


def test_only_one_cli_active_means_on_all_machines_together(world, synced):
    world.history([measured(f'2026-10-13T10:{minute:02d}:00Z', 10 + minute // 10, 20) for minute in range(0, 60, 10)])
    for minute in range(5, 60, 10):
        spend(world, (f'2026-10-13T10:{minute:02d}:00Z', 1.0), session=f'd{minute:07d}-1')
    synced.clone.machine(LAPTOP)
    synced.clone.requests(LAPTOP, [request_line('2026-10-13T10:05:00Z', 'l1', 1.0, first=True)])
    sections = world.report('--day', '2026-10-13')['calibration']['five']['sections']
    assert sections == world.report('--day', '2026-10-13', '--local')['calibration']['five']['sections'] - 1


# --- Criterion: the own folder in the clone counts nothing twice -------------------------------------------------

def test_the_own_folder_in_the_clone_adds_nothing(world, synced):
    rows = two_machines(world, synced)
    world.guard_log([{'time': '2026-10-13T10:01:00Z', 'hook': 'spawn_gate', 'mode': 'enforce', 'decision': 'deny',
                      'reason': 'week at 92 %: later'}])
    without = world.report('--day', '2026-10-13', '--no-pull')
    synced.clone.machine(synced.own, name='desk')
    synced.clone.requests(synced.own, [request_line('2026-10-13T10:05:00Z', 'dddddddd-1-0', 1.0, sid='dddddddd-1',
                                                    first=True, proj='proj-a')])
    synced.clone.history(synced.own, rows)
    synced.clone.gates(synced.own, [{'t': '2026-10-13T10:01:00Z', 'hook': 'spawn_gate', 'decision': 'deny',
                                     'mode': 'enforce', 'reason': 'week at N %'}])
    synced.clone.days(synced.own, [('2026-10-12', {'requests': 5, 'cost': 1.0})])
    assert world.report('--day', '2026-10-13', '--no-pull') == without


def test_requests_of_the_own_folder_stand_in_for_transcripts_claude_code_deleted(world, synced):
    synced.clone.machine(synced.own, name='desk')
    synced.clone.requests(synced.own, [request_line('2026-10-01T10:05:00Z', 'gone', 2.0, sid='dddddddd-1', first=True,
                                                    proj='proj-a')])
    world.projects.mkdir()
    report = world.report('--day', '2026-10-01')
    assert report['total']['requests'] == 1 and by_id(report)['dddddddd']['machine'] == 'desk'
    assert world.report('--day', '2026-10-01', '--local')['total']['requests'] == 0


# --- Criterion: requests a file sync put into the local transcripts count once ------------------------------------

def test_requests_both_in_the_local_transcripts_and_in_a_foreign_folder_count_once(world, synced):
    two_machines(world, synced)
    # a file sync of ~/.claude put the transcript of the laptop session here too
    spend(world, ('2026-10-13T10:05:00Z', 1.0), ('2026-10-13T10:15:00Z', 1.0), session='llllllll-1',
          project='-home-u-proj-l')
    synced.clone.requests(LAPTOP, [request_line('2026-10-13T10:05:00Z', 'llllllll-1-0', 1.0, first=True),
                                   request_line('2026-10-13T10:15:00Z', 'llllllll-1-1', 1.0)])
    report = world.report('--day', '2026-10-13')
    assert report['total']['requests'] == 5      # desk 1, laptop l1 and l2, the two synced requests once
    assert report['total']['cost_usd'] == pytest.approx(5.0)


def test_of_two_lines_of_one_request_the_one_with_more_output_counts_wherever_it_lies(world, synced):
    spend(world, ('2026-10-13T10:05:00Z', 1.0), session='llllllll-1', project='-home-u-proj-l')
    synced.clone.machine(LAPTOP)
    synced.clone.requests(LAPTOP, [request_line('2026-10-13T10:05:00Z', 'llllllll-1-0', 3.0, first=True)])
    report = world.report('--day', '2026-10-13')
    assert report['total']['requests'] == 1 and report['total']['cost_usd'] == pytest.approx(3.0)
    assert by_id(report)['llllllll']['machine'] == 'laptop' and by_id(report)['llllllll']['label'] == 'proj-l'


@pytest.mark.parametrize('message', [None, 'm-keyed'])
def test_the_own_export_in_the_clone_adds_nothing_also_for_lines_without_a_message_id(world, synced, message):
    world.transcript('-home-u-proj-a', 'dddddddd-1', [
        req('2026-10-13T10:05:00Z', message, out=dollars(1.0)),
        req('2026-10-13T10:06:00Z', message and message + '2', request='r2', out=dollars(1.0))])
    before = world.report('--day', '2026-10-13', '--no-pull')
    _sync_export.export(synced.clone.root, synced.log, report_module.load_config(None), str(world.projects), NOW)
    after = world.report('--day', '2026-10-13', '--no-pull')
    assert after['total'] == before['total'] and after['total']['requests'] == 2


def test_lines_without_a_message_id_stay_requests_of_this_machine(world, synced):
    world.transcript('-home-u-proj-a', 'dddddddd-1', [req('2026-10-13T10:05:00Z', None, out=dollars(1.0)),
                                                      req('2026-10-13T10:06:00Z', None, out=dollars(1.0))])
    report = world.report('--day', '2026-10-13')
    assert report['total']['requests'] == 2 and by_id(report)['dddddddd']['machine'] == 'desk'


# --- Criterion: equal measurements of two machines count once -----------------------------------------------------

def test_equal_measurements_of_two_machines_count_once(world, synced):
    two_machines(world, synced)
    alone = world.report('--day', '2026-10-13')
    two_machines(world, synced, laptop_history=True)
    together = world.report('--day', '2026-10-13')
    assert together['allocation'] == alone['allocation'] and together['subscription'] == alone['subscription']


def test_a_measurement_written_twice_by_a_repeated_push_counts_once(world, synced):
    two_machines(world, synced)
    alone = world.report('--day', '2026-10-13')
    synced.clone.history(LAPTOP, [measured('2026-10-13T10:15:00Z', 16, 23), measured('2026-10-13T10:15:00Z', 16, 23)])
    together = world.report('--day', '2026-10-13')
    assert together['allocation']['five']['counted_points'] == alone['allocation']['five']['counted_points']
    assert len(report_module.merge_measurements([report_module.Measurement(T('2026-10-13T10:15:00Z'), 1, 2, None, None)]
                                                * 2)) == 1


def test_measurements_of_other_machines_fill_the_gaps_of_this_one(world, synced):
    world.history([measured('2026-10-13T10:00:00Z', 10, 20)])
    synced.clone.machine(LAPTOP)
    synced.clone.history(LAPTOP, [measured('2026-10-13T10:10:00Z', 14, 22)])
    assert world.report('--day', '2026-10-13')['allocation']['five']['counted_points'] == 4


def test_a_stale_measurement_of_another_machine_does_not_inflate_the_rise(world, synced):
    world.history([measured('2026-10-13T10:00:00Z', 40, 20), measured('2026-10-13T10:10:00Z', 41, 20),
                   measured('2026-10-13T10:20:00Z', 42, 20)])
    spend(world, ('2026-10-13T10:01:00Z', 1.0), ('2026-10-13T10:11:00Z', 1.0), ('2026-10-13T10:19:00Z', 1.0),
          session='dddddddd-1')
    synced.clone.machine(LAPTOP)
    synced.clone.history(LAPTOP, [measured('2026-10-13T10:05:00Z', 39, 20), measured('2026-10-13T10:15:00Z', 39, 20)])
    five = world.report('--day', '2026-10-13')['allocation']['five']
    assert five['counted_points'] == 2 and five['skipped_sections'] == 0
    assert world.report('--day', '2026-10-13', '--local')['allocation']['five']['counted_points'] == 2


def test_a_measurement_of_a_window_that_already_ended_does_not_cut_the_new_one(world, synced):
    r1, r2, week = T('2026-10-13T11:00:00Z'), T('2026-10-13T16:00:00Z'), T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-13T10:50:00Z', 40, 20, r1, week), row('2026-10-13T11:10:00Z', 2, 21, r2, week),
                   row('2026-10-13T11:30:00Z', 4, 22, r2, week)])
    spend(world, ('2026-10-13T11:15:00Z', 1.0), ('2026-10-13T11:25:00Z', 1.0), session='dddddddd-1')
    synced.clone.machine(LAPTOP)
    synced.clone.history(LAPTOP, [row('2026-10-13T11:20:00Z', 41, 21, r1, week)])     # it missed the change
    five = world.report('--day', '2026-10-13')['allocation']['five']
    local = world.report('--day', '2026-10-13', '--local')['allocation']['five']
    assert five['counted_points'] == local['counted_points'] == 2 and five['skipped_sections'] == 1


def test_within_a_window_a_value_only_rises_a_new_window_starts_anew():
    m = report_module.Measurement
    kept = report_module.merge_measurements([
        m(0, 40, 20, 1000.0, 9000.0), m(100, 39, 21, 1000.0, 9000.0),      # 5h stale, week rose
        m(300, 38, 21, 1030.0, 9000.0),                                    # the same window within 60 s of its reset
        m(400, 2, 22, 19000.0, 9000.0),                                    # a new 5h window: falls
        m(500, 1, 22, None, None),                                         # no reset times: not judged
        m(600, 45, 23, 1000.0, 9000.0)])                                   # the old window: stale, the new one counts
    assert [(x.t, x.five, x.week) for x in kept] == [(0, 40, 20), (100, 40, 21), (300, 40, 21), (400, 2, 22),
                                                     (500, 1, 22), (600, 2, 23)]
    assert kept[-1].five_resets == 19000.0


def test_measurements_further_apart_than_two_minutes_stay_two(world):
    m = report_module.Measurement
    kept = report_module.merge_measurements([m(0, 1, 2, None, None), m(121, 1, 2, None, None), m(60, 1, 2, None, None),
                                             m(60, 3, 2, None, None)])
    assert [(x.t, x.five) for x in kept] == [(0, 1), (60, 3), (121, 1)]


# --- Criterion: day lines are never added to requests -------------------------------------------------------------

def archive_line(requests, cost, ident, label, five_pts=0.0, peak=None):
    stats = {'requests': requests, 'cost': cost, 'tokens': {'output': requests},
             'clis': {ident: {**report_module.new_cli(label, T('2026-10-11T08:00:00Z')), 'requests': requests,
                              'cost': cost, 'five_pts': five_pts, 'five_cov': cost}},
             'alloc': {'five': {'counted': five_pts, 'attributed': five_pts}},
             'points': {'main': {'five': five_pts}}}
    if peak is not None:
        stats['abo'] = {'five_windows': [{'resets': None, 'first': T('2026-10-11T08:00:00Z'), 'peak': peak,
                                          'peak_at': T('2026-10-11T09:00:00Z'), 'last': T('2026-10-11T09:00:00Z'),
                                          'first_value': 1, 'last_value': peak}]}
    return stats


def own_archive(world, *lines):
    world.log.mkdir(parents=True, exist_ok=True)
    with open(world.log / 'usage-days.jsonl', 'a', encoding='utf-8') as f:
        f.writelines(json.dumps({'v': 1, 'day': day, 'stats': stats}) + '\n' for day, stats in lines)


def test_on_a_day_with_requests_the_figures_come_from_the_requests_and_the_subscription_once(world, synced):
    two_machines(world, synced, laptop_history=True)
    # each machine has as many requests as its day line names: the requests count, never the lines on top
    own_archive(world, ('2026-10-13', archive_line(1, 50.0, 'dddddddd', 'proj-a', 7, peak=40)))
    synced.clone.days(LAPTOP, [('2026-10-13', archive_line(2, 40.0, 'llllllll', 'proj-l', 9, peak=30))])
    report = world.report('--day', '2026-10-13')
    assert report['total']['requests'] == 3 and report['total']['cost_usd'] == pytest.approx(3.0)
    assert report['coverage']['archive_days'] == [] and report['coverage']['sync']['days_from_lines'] == []
    assert len(report['subscription']['five_windows']) == 1 and len(report['subscription']['week_segments']) == 1
    assert report['allocation']['five']['counted_points'] == 8
    assert world.report('--day', '2026-10-13', '--local')['total']['cost_usd'] == 50.0     # the archive, as before


# --- Criterion (v7): requests or day line, per machine and day ---------------------------------------------------

def test_a_machine_with_fewer_requests_than_its_day_line_counts_the_line_while_another_has_requests(world, synced):
    world.projects.mkdir()
    own_archive(world, ('2026-10-11', archive_line(5, 2.0, 'aaaaaaaa', 'proj-a', 3, peak=40)))
    synced.clone.machine(LAPTOP)
    synced.clone.requests(LAPTOP, [request_line('2026-10-11T10:05:00Z', 'l1', 1.0, first=True)])
    report = world.report('--day', '2026-10-11')
    assert report['total']['requests'] == 6 and report['total']['cost_usd'] == pytest.approx(3.0)
    clis = by_id(report)
    assert clis['aaaaaaaa']['machine'] == 'desk' and clis['aaaaaaaa']['estimated_share'] == {'five': 0, 'week': 0}
    assert clis['aaaaaaaa']['measured_share'] == {'five': 0, 'week': 0}
    assert report['coverage']['sync']['days_from_lines'] == [{'day': '2026-10-11', 'machine': 'desk'}]
    assert report['coverage']['archive_days'] == []
    assert '  proj-a  aaaaaaaa  started Sun 11 Oct 10:00  on desk' in world.overview('--day', '2026-10-11', '--full')


def test_a_day_line_beside_requests_that_does_not_add_up_with_them_is_counted_and_left_out(world, synced):
    world.projects.mkdir()
    world.transcript('-home-u-proj-a', 'dddddddd-1', [extras(req('2026-10-11T10:05:00Z', 'd1', out=100), thinking=50)])
    synced.clone.machine(LAPTOP)
    line = archive_line(7, 4.0, 'bbbbbbbb', 'proj-b')
    line['extra'] = {'thinking': {'x': 1}}      # figures where the requests have a number
    synced.clone.days(LAPTOP, [('2026-10-11', line)])
    report = world.report('--day', '2026-10-11')
    assert report['total']['requests'] == 1 and report['coverage']['sync']['unreadable_day_lines'] == 1


def test_a_session_a_file_sync_put_here_counts_once_beside_the_day_line_of_its_machine(world, synced):
    # a file sync of ~/.claude put the laptop session here; the laptop has the same requests and its day line
    spend(world, ('2026-10-11T10:05:00Z', 1.0), ('2026-10-11T10:15:00Z', 1.0), ('2026-10-11T10:25:00Z', 1.0),
          session='llllllll-1', project='-home-u-proj-l')
    synced.clone.machine(LAPTOP)
    synced.clone.requests(LAPTOP, [request_line(t, f'llllllll-1-{i}', 1.0, first=i == 0) for i, t in enumerate(
        ('2026-10-11T10:05:00Z', '2026-10-11T10:15:00Z', '2026-10-11T10:25:00Z'))])
    synced.clone.days(LAPTOP, [('2026-10-11', archive_line(3, 3.0, 'llllllll', 'proj-l'))])
    report = world.report('--day', '2026-10-11')
    assert report['total']['requests'] == 3 and report['coverage']['sync']['days_from_lines'] == []


def test_a_foreign_day_line_counts_while_a_third_machine_has_requests_also_with_machine(world, synced):
    world.projects.mkdir()
    synced.clone.machine(LAPTOP)
    synced.clone.machine(TABLET, name='tablet')
    synced.clone.days(LAPTOP, [('2026-10-11', archive_line(7, 4.0, 'bbbbbbbb', 'proj-b', 4, peak=60))])
    synced.clone.requests(TABLET, [request_line('2026-10-11T10:05:00Z', 't1', 1.0, sid='tttttttt-1', first=True)])
    assert world.report('--day', '2026-10-11')['total']['requests'] == 8
    only = world.report('--day', '2026-10-11', '--machine', 'laptop')
    assert only['total']['requests'] == 7 and [cli['id'] for cli in only['clis']] == ['bbbbbbbb']
    assert world.report('--day', '2026-10-11', '--machine', 'tablet')['total']['requests'] == 1


def test_with_one_machine_and_partly_deleted_transcripts_the_report_is_that_of_local(world, synced):
    own_archive(world, ('2026-10-11', archive_line(5, 5.0, 'aaaaaaaa', 'proj-a', 3, peak=40)))
    spend(world, ('2026-10-11T10:05:00Z', 1.0), session='aaaaaaaa-1')      # a session that outlived the others
    report, local = world.report('--day', '2026-10-11'), world.report('--day', '2026-10-11', '--local')
    for key in ('total', 'allocation', 'calibration', 'subscription', 'guards'):
        assert report[key] == local[key], key
    assert report['coverage']['archive_days'] == local['coverage']['archive_days'] == ['2026-10-11']


def test_a_day_line_of_a_day_the_period_holds_only_in_part_leaves_its_requests(world, synced):
    own_archive(world, ('2026-10-12', archive_line(5, 5.0, 'aaaaaaaa', 'proj-a')))
    world.history([measured('2026-10-13T10:00:00Z', 10, 20)])      # the week begins Mon 12 Oct 09:00, not at midnight
    spend(world, ('2026-10-12T10:05:00Z', 1.0), session='aaaaaaaa-1')
    assert world.report('--week')['total']['requests'] == 1 == world.report('--week', '--local')['total']['requests']


def test_on_a_day_without_requests_the_usage_of_every_line_adds_up_and_the_subscription_comes_from_one(world, synced):
    world.projects.mkdir()
    own_archive(world, ('2026-10-11', archive_line(5, 2.0, 'aaaaaaaa', 'proj-a', 3, peak=40)))
    synced.clone.machine(LAPTOP)
    synced.clone.days(LAPTOP, [('2026-10-11', archive_line(7, 4.0, 'bbbbbbbb', 'proj-b', 4, peak=60)),
                               ('2026-10-11', archive_line(70, 40.0, 'bbbbbbbb', 'proj-b'))])      # a copy: the first counts
    report = world.report('--day', '2026-10-11')
    assert report['total']['requests'] == 12 and report['total']['cost_usd'] == pytest.approx(6.0)
    assert report['coverage']['archive_days'] == ['2026-10-11']
    assert [w['peak'] for w in report['subscription']['five_windows']] == [40]
    assert report['allocation']['five']['counted_points'] == 3 and report['total']['main']['five_points'] == 3
    clis = by_id(report)
    assert clis['aaaaaaaa']['estimated_share']['five'] == 3 and clis['aaaaaaaa']['machine'] == 'desk'
    assert clis['bbbbbbbb']['estimated_share']['five'] == 0 and clis['bbbbbbbb']['machine'] == 'laptop'
    assert clis['bbbbbbbb']['measured_share']['five'] == 0 and clis['bbbbbbbb']['requests'] == 7


def test_without_an_own_line_the_first_other_machine_gives_the_subscription(world, synced):
    world.projects.mkdir()
    synced.clone.machine(LAPTOP)
    synced.clone.machine(TABLET, name='tablet')
    synced.clone.days(TABLET, [('2026-10-11', archive_line(7, 4.0, 'bbbbbbbb', 'proj-b', 4, peak=60))])
    synced.clone.days(LAPTOP, [('2026-10-11', archive_line(5, 2.0, 'cccccccc', 'proj-c', 2, peak=30))])
    report = world.report('--day', '2026-10-11')
    assert [w['peak'] for w in report['subscription']['five_windows']] == [30]     # the smaller id
    assert report['total']['requests'] == 12


def test_a_day_line_that_does_not_add_up_with_the_others_is_counted_and_left_out(world, synced):
    world.projects.mkdir()
    first = archive_line(5, 2.0, 'aaaaaaaa', 'proj-a')
    first['extra'] = {'thinking': 5}
    own_archive(world, ('2026-10-11', first))
    other = archive_line(7, 4.0, 'bbbbbbbb', 'proj-b')
    other['extra'] = {'thinking': {'x': 1}}     # figures where the first line has a number: they cannot be added
    synced.clone.machine(LAPTOP)
    synced.clone.days(LAPTOP, [('2026-10-11', other)])
    report = world.report('--day', '2026-10-11')
    assert report['total']['requests'] == 5 and report['coverage']['sync']['unreadable_day_lines'] == 1


def test_a_foreign_day_line_is_checked_like_the_own_archive(world, synced):
    world.projects.mkdir()
    synced.clone.machine(LAPTOP)
    synced.clone.days(LAPTOP, [('2026-10-11', {'requests': 'many'}), ('2026-10-10', {'requests': 3})])
    report = world.report('--since', '2026-10-10', '--until', '2026-10-11')
    assert report['total']['requests'] == 3 and report['coverage']['sync']['unreadable_day_lines'] == 1
    assert report['coverage']['archive_days'] == ['2026-10-10']
    text = world.overview('--since', '2026-10-10', '--until', '2026-10-11', '--full')
    assert '1 unreadable day line(s) of other machines skipped' in text


def test_a_day_line_of_an_earlier_version_keeps_its_rules(world, synced):
    world.projects.mkdir()
    own_archive(world, ('2026-10-11', {'requests': 2, 'cost': 1.0, 'alloc': {'five': {'attributed': 1}}}))
    synced.clone.machine(LAPTOP)
    synced.clone.days(LAPTOP, [('2026-10-11', archive_line(7, 4.0, 'bbbbbbbb', 'proj-b'))])
    report = world.report('--day', '2026-10-11')
    assert report['coverage']['archive_days_without_agent_split'] == ['2026-10-11']
    assert report['total']['requests'] == 9 and report['startup']['starts'] == []


# --- Criterion: a hostile day line of another machine is skipped and counted, never a crash -----------------------

def hostile(world, synced, change):
    """This machine's day line (5 requests, 2 USD) and a laptop line for the same day changed by `change`."""
    world.projects.mkdir()
    own_archive(world, ('2026-10-11', archive_line(5, 2.0, 'aaaaaaaa', 'proj-a', 3, peak=40)))
    synced.clone.machine(LAPTOP)
    line = archive_line(7, 4.0, 'bbbbbbbb', 'proj-b', 4, peak=60)
    change(line)
    synced.clone.days(LAPTOP, [('2026-10-11', line)])


def set_in(path, value):
    def change(line):
        target = line
        for key in path[:-1]:
            target = target.setdefault(key, {})
        target[path[-1]] = value
    return change


@pytest.mark.parametrize('change', [
    set_in(('cost',), 10 ** 400),                                            # no float can hold it
    set_in(('requests',), -1000),                                            # negative
    set_in(('cost',), -500.0),
    set_in(('tokens', 'output'), 10 ** 13),                                  # above any real count
    set_in(('agent_types',), {'reviewer': 7}),                               # not the shape the report reads
    set_in(('models',), {'Opus 5.5': {'requests': 1, 'cost': 1.0}}),         # a model without its tokens
    set_in(('unpriced',), {'x': {'requests': 1}}),
    set_in(('clis',), {'bbbbbbbb': 7}),
    set_in(('clis',), {'not an id!': report_module.new_cli('proj-b', 1.0)}),
    set_in(('clis', 'bbbbbbbb', 'models'), {'Opus 5.5': {'cost': 1}}),
    set_in(('clis', 'bbbbbbbb', 'subs'), {'reviewer': 7}),
    set_in(('clis', 'bbbbbbbb', 'label'), 'proj-\x1b[2J\x1b]0;pwned\x07'),     # control characters
    set_in(('clis', 'bbbbbbbb', 'label'), 'proj/<b>'),                       # not a label of the format
    set_in(('agent_types',), {'agent\x1b[5m': {'requests': 7, 'cost': 4.0}}),
    set_in(('models',), {'evil\x1b[31mmodel': {'requests': 7, 'cost': 4.0, 'tokens': {}}}),
    set_in(('guard_reasons', 'soft_stop', 'enforce', 'deny'), {'why\x07': 1}),
    set_in(('unpriced',), {'x': {'requests': 1, 'tokens': {**dict.fromkeys(_transcripts.KINDS, 0), 'output': {}}}}),
    set_in(('abo',), {'five_windows': [{'resets': None, 'first': 1.0, 'peak': 1, 'peak_at': 10 ** 12, 'last': 2.0,
                                        'first_value': 1, 'last_value': 1}]}),   # passes the checks, fails the report
], ids=['huge', 'negative-count', 'negative-cost', 'too-many-tokens', 'agent-types-shape', 'models-shape',
        'unpriced-shape', 'cli-shape', 'cli-id', 'cli-models', 'cli-subs', 'label-escape', 'label-pattern',
        'agent-type-escape',
        'model-escape', 'reason-escape', 'net-tokens', 'probe'])
def test_a_hostile_foreign_day_line_is_skipped_and_counted(world, synced, change):
    hostile(world, synced, change)
    report = world.report('--day', '2026-10-11')
    assert report['total']['requests'] == 5 and report['total']['cost_usd'] == pytest.approx(2.0)
    assert report['coverage']['sync']['unreadable_day_lines'] == 1
    for args in (('--full',), (), ('--json',)):
        text = world.overview('--day', '2026-10-11', *args)
        assert not report_module.CONTROL_CHARS.search(text.replace('\n', ''))


def test_the_check_of_a_foreign_day_line_runs_on_its_own_day(world, synced, monkeypatch):
    """Windows refuses local times near 1970 (OSError from fromtimestamp): the net must not use such a time, or it
    throws every foreign day line away."""
    class Windows(report_module.datetime):
        @classmethod
        def fromtimestamp(cls, ts, tz=None):
            if ts < 86400 * 365:
                raise OSError(22, 'Invalid argument')
            return super().fromtimestamp(ts, tz)
    monkeypatch.setattr(report_module, 'datetime', Windows)
    world.projects.mkdir()
    synced.clone.machine(LAPTOP)
    synced.clone.days(LAPTOP, [('2026-10-11', archive_line(7, 4.0, 'bbbbbbbb', 'proj-b', 4, peak=60))])
    report = world.report('--day', '2026-10-11')
    assert report['total']['requests'] == 7 and report['coverage']['sync']['unreadable_day_lines'] == 0


def test_a_foreign_day_line_with_a_number_json_does_not_know_is_skipped(world, synced):
    world.projects.mkdir()
    synced.clone.machine(LAPTOP)
    good = json.dumps({'v': 1, 'day': '2026-10-10', 'stats': {'requests': 3}})
    synced.clone.raw(LAPTOP, 'days/2026-10.jsonl', (good + '\n' + good.replace('3', 'NaN').replace('10-10', '10-11')
                                                    + '\n').encode('utf-8'))
    report = world.report('--since', '2026-10-10', '--until', '2026-10-11')
    assert report['total']['requests'] == 3 and report['coverage']['sync']['unreadable_day_lines'] == 1


@pytest.mark.parametrize('field, value', [('agent_types', {'reviewer': 7}), ('models', {'x': {'requests': 1}}),
                                          ('clis', {'aaaaaaaa': {**report_module.new_cli('p', 1.0), 'subs': {'x': 7}}})])
def test_an_own_archive_line_with_a_table_the_report_cannot_read_is_skipped(world, synced, field, value):
    world.projects.mkdir()
    bad = archive_line(5, 2.0, 'aaaaaaaa', 'proj-a')
    bad[field] = value
    own_archive(world, ('2026-10-11', bad))
    synced.clone.machine(LAPTOP)
    synced.clone.days(LAPTOP, [('2026-10-11', archive_line(7, 4.0, 'bbbbbbbb', 'proj-b', 4, peak=60))])
    assert world.report('--day', '2026-10-11', '--local')['coverage']['unreadable_archive_lines'] == 1
    report = world.report('--day', '2026-10-11')
    assert report['coverage']['unreadable_archive_lines'] == 1 and report['total']['requests'] == 7


def test_a_number_no_float_holds_is_no_archive_line_also_locally(world):
    world.projects.mkdir()
    bad = archive_line(5, 2.0, 'aaaaaaaa', 'proj-a')
    bad['cost'] = 10 ** 400
    own_archive(world, ('2026-10-11', bad))
    assert world.report('--day', '2026-10-11')['coverage']['unreadable_archive_lines'] == 1


# --- Criterion: the pull waits 5 minutes, --sync pulls always ------------------------------------------------------

def test_a_second_report_within_5_minutes_of_a_pull_does_not_pull_and_sync_always_does(world, synced):
    world.projects.mkdir()
    world.report('--day', '2026-10-13')
    assert len(synced.pulls) == 1 and synced.read_state().last_pull == NOW
    world.report('--day', '2026-10-13', now=NOW + 299)
    assert len(synced.pulls) == 1
    world.report('--day', '2026-10-13', '--sync', now=NOW + 299)
    assert len(synced.pulls) == 2
    world.report('--day', '2026-10-13', now=NOW + 299 + 300)
    assert len(synced.pulls) == 3
    assert synced.pulls[0].deadline.left() <= 5 and synced.pulls[0].kind == 'pull'


def test_the_pane_reading_four_periods_pulls_once(world, synced):
    two_machines(world, synced)
    for period in ('--day', '--week', '--month', '--year'):
        world.report(period)
    assert len(synced.pulls) == 1


def test_no_pull_and_sync_off_do_not_pull(world, synced, monkeypatch):
    world.projects.mkdir()
    sync_part = world.report('--no-pull')['coverage']['sync']
    assert synced.pulls == [] and sync_part['pull'] == {'status': 'skipped', 'category': None, 'detail': None,
                                                         'last_pull': None}
    world.configure(monkeypatch, sync={'mode': 'off'})
    assert 'sync' not in world.report('--no-pull')['coverage'] and synced.pulls == []


def test_a_held_lock_reads_the_clone_as_it_is(world, synced):
    two_machines(world, synced)
    with _sync_core.try_lock(_sync_core.ensure_dir(synced.log)):
        report = world.report('--day', '2026-10-13', '--sync')
    assert synced.pulls == [] and synced.pushes == [] and report['total']['requests'] == 3
    assert report['coverage']['sync']['pull']['status'] == 'locked'
    assert report['coverage']['sync']['push'] == {'status': 'locked', 'category': None, 'detail': None}
    assert 'another sync run holds the lock' in report['coverage']['sync']['text']


# --- Criterion: --sync pushes inside a backoff, never after visibility ---------------------------------------------

def test_sync_pushes_inside_a_backoff(world, synced):
    world.projects.mkdir()
    synced.state(backoff_level=2, next_due=NOW + 3600, last_error='network', last_success=NOW - 7200)
    report = world.report('--sync')
    assert len(synced.pushes) == 1 and synced.read_state().backoff_level == 0
    assert report['coverage']['sync']['push'] == {'status': 'ok', 'category': None, 'detail': None}
    assert 'push ok' in report['coverage']['sync']['text']


def test_sync_does_not_push_after_visibility_and_says_so(world, synced):
    world.projects.mkdir()
    synced.state(backoff_level=1, last_error='visibility', last_success=NOW - 600)
    text = world.overview('--sync', '--full')
    assert synced.pushes == [] and len(synced.pulls) == 1
    assert 'no push: the sync repository is public (visibility)' in text
    overview = world.overview('--sync')
    assert 'sync of this machine blocked: the sync repository is public (visibility)' in overview
    assert synced.read_state().last_error == 'visibility'


def test_the_push_of_sync_reports_its_own_run_not_a_change_of_the_state(world, synced, monkeypatch):
    world.projects.mkdir()

    def another_run_holds_the_lock(log_dir, cfg, runner, **kwargs):
        synced.state(backoff_level=1, next_due=NOW + 1800, last_error='network')     # that run failed meanwhile
        return _sync_core.Outcome('locked')
    monkeypatch.setattr(_sync_core, 'push_outcome', another_run_holds_the_lock)
    assert world.report('--sync')['coverage']['sync']['push'] == {'status': 'locked', 'category': None,
                                                                  'detail': None}


def test_a_failed_push_of_sync_is_named_and_the_report_goes_on(world, synced):
    world.projects.mkdir()
    synced.push_error = ('auth', 'no credentials')
    report = world.report('--sync')
    assert report['coverage']['sync']['push'] == {'status': 'failed', 'category': 'auth', 'detail': 'no credentials'}
    assert 'push failed (auth)' in report['coverage']['sync']['text'] and len(synced.pulls) == 1


def test_a_sync_folder_that_cannot_be_used_fails_push_and_pull_but_not_the_report(world, synced):
    world.projects.mkdir()
    shutil.rmtree(world.log / 'sync')
    (world.log / 'sync').write_text('', encoding='utf-8')
    sync_part = world.report('--sync')['coverage']['sync']
    assert sync_part['push'] == {'status': 'failed', 'category': 'internal', 'detail': 'the sync folder cannot be used'}
    assert sync_part['pull']['status'] == 'failed' and sync_part['pull']['category'] == 'internal'


# --- Criterion: --local prints what the report printed before -----------------------------------------------------

@pytest.mark.parametrize('mode', ['on', 'shadow'])
def test_local_prints_the_saved_output_byte_for_byte(world, monkeypatch, tmp_path, mode):
    machine = Synced(world, monkeypatch, tmp_path, mode)
    build(world)
    monkeypatch.setattr(sync_format.time, 'timezone', -3600)
    machine.clone.machine(LAPTOP, utc_std=-480)      # another time zone changes nothing here
    machine.clone.requests(LAPTOP, [request_line('2026-10-13T10:05:00Z', 'l1', 1.0, first=True)])
    monkeypatch.setattr(report_module, 'PRICES_FILE', os.path.join(os.path.dirname(__file__), 'golden', 'prices.json'))
    for name, args in OUTPUTS.items():
        compare_golden(name, world.overview(*args, '--local', now=NOW))
    assert machine.pulls == []


def test_with_sync_off_the_saved_output_stays_byte_for_byte(world, monkeypatch):
    world.configure(monkeypatch, sync={'mode': 'off', 'machine_name': 'desk'})
    build(world)
    for name, text in outputs(world, monkeypatch).items():
        compare_golden(name, text)


# --- Criterion: a failed pull ends with exit 0 and says why -------------------------------------------------------

def test_a_failed_pull_uses_the_clone_as_it_was_and_names_the_reason(world, synced):
    two_machines(world, synced)
    synced.state(last_pull=NOW - 7200)
    synced.pull_error = ('network', 'could not reach the remote')
    report = world.report('--day', '2026-10-13')
    assert report['total']['requests'] == 3
    assert report['coverage']['sync']['pull'] == {'status': 'failed', 'category': 'network',
                                                  'detail': 'could not reach the remote',
                                                  'last_pull': '2026-10-14T10:00:00Z'}
    note = next(n for n in report['overview']['notable'] if n['rule'] == 'sync_pull')
    assert note['text'] == 'pull failed (network: could not reach the remote): the other machines as of 2 h ago'
    text = world.overview('--day', '2026-10-13', '--full')
    assert 'pull failed (network), last pull 2 h ago' in text
    assert synced.read_state().last_pull == NOW - 7200


def test_a_pull_that_never_succeeded_says_so(world, synced):
    world.projects.mkdir()
    synced.pull_error = ('timeout', 'the run took too long')
    note = next(n for n in world.report()['overview']['notable'] if n['rule'] == 'sync_pull')
    assert note['text'] == 'pull failed (timeout: the run took too long): the other machines not pulled yet'


# --- Criterion: without a fast forward nothing is merged, and the report says so ------------------------------------

def test_without_a_fast_forward_the_report_names_the_differing_history(world, synced):
    two_machines(world, synced)
    synced.state(last_pull=NOW - 600)
    synced.pull_error = ('conflict', 'no fast forward')
    report = world.report('--day', '2026-10-13')
    assert report['coverage']['sync']['pull']['status'] == 'diverged' and report['total']['requests'] == 3
    note = next(n for n in report['overview']['notable'] if n['rule'] == 'sync_pull')
    assert note['text'] == ('the sync history could not be brought together (no fast forward), nothing was merged: '
                            'the other machines as of 10 min ago')
    assert 'sync history not brought together, nothing merged, last pull 10 min ago' in \
        report['coverage']['sync']['text']


# --- Criterion: what the reader skips in foreign folders is counted ----------------------------------------------

def test_oversized_files_unknown_versions_and_broken_lines_are_skipped_and_counted(world, synced):
    two_machines(world, synced)
    big = synced.clone.raw(LAPTOP, 'requests/2026-10/big.jsonl', b'')
    with open(big, 'r+b') as f:
        f.truncate(8 * 1024 * 1024 + 1)
    good = json.dumps({**request_line('2026-10-13T10:25:00Z', 'l3', 1.0), 'v': 1})
    synced.clone.raw(LAPTOP, 'requests/2026-10/odd.jsonl', (json.dumps({**json.loads(good), 'v': 2}) + '\n{broken\n'
                                                            + good + '\n').encode('utf-8'))
    report = world.report('--day', '2026-10-13')
    skipped = report['coverage']['sync']['skipped']
    assert (skipped['oversized'], skipped['unknown_version'], skipped['broken_lines']) == (1, 1, 1)
    assert report['total']['requests'] == 4
    text = world.overview('--day', '2026-10-13', '--full')
    assert '3 item(s) of the sync repository skipped (1 oversized, 1 unknown version, 1 broken lines)' in text


@pytest.mark.posix_only
def test_a_link_in_a_foreign_folder_is_skipped_and_counted(world, synced, tmp_path):
    two_machines(world, synced)
    outside = tmp_path / 'outside.jsonl'
    outside.write_text(json.dumps({**request_line('2026-10-13T10:25:00Z', 'x', 50.0), 'v': 1}) + '\n', encoding='utf-8')
    os.symlink(outside, os.path.join(synced.clone.root, 'machines', LAPTOP, 'requests', '2026-10', 'link.jsonl'))
    report = world.report('--day', '2026-10-13')
    assert report['coverage']['sync']['skipped']['symlinks'] == 1 and report['total']['requests'] == 3


# --- What the report shows --------------------------------------------------------------------------------------

def test_sources_names_every_machine_with_the_age_of_its_last_push_and_the_pull(world, synced):
    two_machines(world, synced)
    synced.clone.machine(TABLET, name='tablet', last_success=None)
    synced.state(last_success=NOW - 240)
    text = world.overview('--day', '2026-10-13', '--full')
    assert ('Sync: 3 devices · desk 4 min ago · laptop 1 h ago · tablet never pushed · pull ok') in text
    assert 'proj-l  llllllll  started Tue 13 Oct 12:05  on laptop' in text


def test_the_overview_notes_machines_without_a_push_for_a_day_and_the_own_backoff(world, synced):
    two_machines(world, synced)
    synced.clone.machine(LAPTOP, last_success='2026-10-11T12:00:00Z')
    synced.state(backoff_level=2, next_due=NOW + 1800, last_error='network', last_success=NOW - 600)
    report = world.report('--day', '2026-10-13')
    notes = {n['rule']: n for n in report['overview']['notable']}
    assert notes['sync_stale']['text'] == 'no push for over 24 h: laptop 3 d ago'
    assert notes['sync_stale']['machines'] == [{'name': 'laptop', 'last_success': '2026-10-11T12:00:00Z'}]
    assert notes['sync_backoff']['text'] == 'sync of this machine backs off after an error (network), next push Wed 14 Oct 14:30'
    assert (notes['sync_backoff']['category'], notes['sync_backoff']['next_due']) == ('network', '2026-10-14T12:30:00Z')
    assert report['coverage']['sync']['backoff'] == {'level': 2, 'last_error': 'network',
                                                    'next_due': '2026-10-14T12:30:00Z'}
    overview = world.overview('--day', '2026-10-13')
    assert '  no push for over 24 h: laptop 3 d ago' in overview


def test_a_machine_that_never_pushed_is_notable(world, synced):
    world.projects.mkdir()
    note = next(n for n in world.report()['overview']['notable'] if n['rule'] == 'sync_stale')
    assert note['text'] == 'no push for over 24 h: desk never'


# --- Machines in different time zones (FORMAT v8: utc_std in machine.json) -----------------------------------------

def time_zone_notes(report):
    return [n for n in report['overview']['notable'] if n['rule'] == 'sync_timezone']


def test_machines_in_one_time_zone_are_not_notable(world, synced, monkeypatch):
    monkeypatch.setattr(sync_format.time, 'timezone', -3600)
    world.projects.mkdir()
    synced.clone.machine(LAPTOP, utc_std=60)
    synced.clone.machine(TABLET, name='tablet', utc_std=60)
    assert time_zone_notes(world.report()) == []


def test_this_machine_in_another_time_zone_than_the_other_is_notable(world, synced, monkeypatch):
    monkeypatch.setattr(sync_format.time, 'timezone', -3600)
    world.projects.mkdir()
    synced.clone.machine(LAPTOP, utc_std=-480)
    report = world.report()
    assert time_zone_notes(report) == [{
        'rule': 'sync_timezone', 'machines': [{'name': 'desk', 'utc_std': 60}, {'name': 'laptop', 'utc_std': -480}],
        'text': "machines in different time zones (desk UTC+1, laptop UTC-8): day lines are cut at each machine's "
                'local midnight'}]
    assert [m['utc_std'] for m in report['coverage']['sync']['machines']] == [60, -480]
    assert ("  machines in different time zones (desk UTC+1, laptop UTC-8): day lines are cut at each machine's "
            'local midnight') in world.overview()


def test_two_other_machines_in_different_time_zones_are_notable_with_every_known_one(world, synced, monkeypatch):
    monkeypatch.setattr(sync_format.time, 'timezone', 0)
    world.projects.mkdir()
    synced.clone.machine(LAPTOP, name='laptop', utc_std=0)
    synced.clone.machine(TABLET, name='tablet', utc_std=330)
    synced.clone.raw('3ccccccc-0000-4000-a000-000000000003', 'machine.json', b'{"v":1,')
    synced.clone.machine('4ddddddd-0000-4000-b000-000000000004', name='phone', utc_std=-210)
    [note] = time_zone_notes(world.report())
    assert note['text'] == ("machines in different time zones (desk UTC+0, laptop UTC+0, phone UTC-3:30, tablet "
                            "UTC+5:30): day lines are cut at each machine's local midnight")
    assert [m['name'] for m in note['machines']] == ['desk', 'laptop', 'phone', 'tablet']


def test_a_machine_without_a_readable_machine_json_does_not_count(world, synced, monkeypatch):
    monkeypatch.setattr(sync_format.time, 'timezone', -3600)
    world.projects.mkdir()
    synced.clone.raw(LAPTOP, 'machine.json', b'{"v":1,"name":"laptop","platform":"linux","schema":1,'
                                             b'"created":"2026-10-01T00:00:00Z"}')
    synced.clone.raw(TABLET, 'machine.json', b'{"v":1,"name":"tablet","platform":"linux","schema":1,'
                                             b'"created":"2026-10-01T00:00:00Z","utc_std":61.5}')
    report = world.report()
    assert time_zone_notes(report) == []
    assert [m['utc_std'] for m in report['coverage']['sync']['machines']] == [60, None, None]


def test_the_json_carries_the_machine_of_every_cli_and_the_sync_coverage(world, synced):
    two_machines(world, synced)
    synced.state(last_success=NOW - 60, last_pull=NOW - 1000)
    sync_part = world.report('--day', '2026-10-13')['coverage']['sync']
    assert sync_part['mode'] == 'on' and sync_part['machine'] is None and sync_part['push'] is None
    assert sync_part['clone'] == 'read'
    assert sync_part['machines'] == [
        {'id': synced.own, 'name': 'desk', 'platform': sync_format.platform_name(), 'own': True,
         'last_success': '2026-10-14T11:59:00Z', 'utc_std': sync_format.utc_std()},
        {'id': LAPTOP, 'name': 'laptop', 'platform': 'linux', 'own': False, 'last_success': '2026-10-14T11:00:00Z',
         'utc_std': sync_format.utc_std()}]
    assert sync_part['pull'] == {'status': 'ok', 'category': None, 'detail': None, 'last_pull': '2026-10-14T12:00:00Z'}
    assert sync_part['skipped'] == dict.fromkeys(sync_format.COUNTERS, 0) and sync_part['unreadable_day_lines'] == 0
    assert sync_part['text'] == 'Sync: 2 devices · desk 1 min ago · laptop 1 h ago · pull ok'


def test_without_the_sync_the_json_has_no_sync_fields(world, synced):
    two_machines(world, synced)
    report = world.report('--day', '2026-10-13', '--local')
    assert 'sync' not in report['coverage'] and all('machine' not in cli for cli in report['clis'])


def test_the_recent_pull_is_named_with_its_age(world, synced):
    world.projects.mkdir()
    synced.state(last_pull=NOW - 120)
    assert world.report()['coverage']['sync']['text'].endswith('· last pull 2 min ago')


def test_gate_decisions_of_other_machines_count_once(world, synced):
    two_machines(world, synced)
    deny = {'t': '2026-10-13T10:07:00Z', 'hook': 'soft_stop', 'decision': 'deny', 'mode': 'enforce',
            'reason': 'checkpoint due'}
    synced.clone.gates(LAPTOP, [deny, deny, {**deny, 't': '2026-10-13T10:08:00Z', 'decision': 'allow'}])
    report = world.report('--day', '2026-10-13')
    assert report['guards'] == {'soft_stop': {'enforce': {'deny': 1, 'allow': 1}}}
    assert report['guard_reasons'] == {'soft_stop': {'enforce': {'deny': {'checkpoint due': 1}}}}


def test_starts_of_other_machines_are_their_first_requests(world, synced):
    world.projects.mkdir()
    synced.clone.machine(LAPTOP)
    synced.clone.requests(LAPTOP, [
        request_line('2026-10-13T10:05:00Z', 'l1', 1.0, first=True),
        request_line('2026-10-13T10:06:00Z', 'l2', 1.0),
        request_line('2026-10-13T10:07:00Z', 's1', 1.0, sub='arbeiter-bauen', first=True)])
    report = world.report('--day', '2026-10-13')
    assert [(s['session'], s['type']) for s in report['startup']['starts']] == [('llllllll', None),
                                                                                 ('llllllll', 'arbeiter-bauen')]
    assert report['total']['subagents']['by_type']['arbeiter-bauen']['requests'] == 1


def test_machine_names_from_the_clone_are_made_harmless():
    assert report_module.machine_name('lap\x1b[31mtop\u0085') == 'lap?[31mtop?'
    assert report_module.machine_name('x' * 80) == 'x' * 64 and report_module.machine_name('') == '?'


def test_a_machine_without_machine_json_is_named_by_its_id(world, synced):
    world.projects.mkdir()
    synced.clone.requests(LAPTOP, [request_line('2026-10-13T10:05:00Z', 'l1', 1.0, first=True)])
    report = world.report('--day', '2026-10-13')
    assert by_id(report)['llllllll']['machine'] == 'machine-1aaaaa'
    assert report['coverage']['sync']['machines'][1] == {'id': LAPTOP, 'name': 'machine-1aaaaa', 'platform': None,
                                                         'own': False, 'last_success': None, 'utc_std': None}


def test_an_unusable_machine_id_leaves_the_clone_unread(world, synced):
    two_machines(world, synced)
    (world.log / 'sync' / 'machine_id').write_text('not a uuid\n', encoding='utf-8')
    report = world.report('--day', '2026-10-13')
    assert report['total']['requests'] == 1
    assert [m['name'] for m in report['coverage']['sync']['machines']] == ['desk']
    assert report['coverage']['sync']['machines'][0]['id'] is None
    assert report['coverage']['sync']['clone'] == 'unread'
    assert report['coverage']['sync']['text'].endswith(
        '· pull ok · clone not read: the machine id of this machine cannot be used')
    note = next(n for n in report['overview']['notable'] if n['rule'] == 'sync_machine_id')
    assert note['text'] == ('the machine id of this machine (sync/machine_id in the log folder) cannot be used: the '
                            'other machines are not read and this one does not push')
    assert '  the machine id of this machine' in world.overview('--day', '2026-10-13')


def test_a_machine_that_never_synced_reads_the_clone_by_its_configured_name(world, synced, monkeypatch):
    two_machines(world, synced)
    os.remove(world.log / 'sync' / 'machine_id')
    assert world.report('--day', '2026-10-13')['coverage']['sync']['machines'][0]['name'] == 'desk'
    world.configure(monkeypatch, sync={'mode': 'on'})
    report = world.report('--day', '2026-10-13')
    assert report['coverage']['sync']['machines'][0]['name'] == 'this machine' and report['total']['requests'] == 3


def test_without_a_clone_only_this_machine_is_shown(world, synced):
    two_machines(world, synced)
    os.rename(synced.clone.root, world.root / 'elsewhere')
    report = world.report('--day', '2026-10-13')
    assert report['total']['requests'] == 1 and len(report['coverage']['sync']['machines']) == 1
    assert report['coverage']['sync']['clone'] == 'missing'
    assert report['coverage']['sync']['text'].endswith('· no clone of the sync repository')


# --- --machine ------------------------------------------------------------------------------------------------------

def test_machine_shows_the_requests_of_one_machine_with_the_shares_of_all(world, synced):
    two_machines(world, synced)
    world.guard_log([{'time': '2026-10-13T10:01:00Z', 'hook': 'spawn_gate', 'mode': 'enforce', 'decision': 'allow'}])
    synced.clone.gates(LAPTOP, [{'t': '2026-10-13T10:07:00Z', 'hook': 'soft_stop', 'decision': 'warn',
                                 'mode': 'shadow', 'reason': 'checkpoint due'}])
    report = world.report('--day', '2026-10-13', '--machine', 'lap')
    assert [cli['id'] for cli in report['clis']] == ['llllllll']
    assert report['clis'][0]['estimated_share'] == {'five': 6, 'week': 3}
    assert report['total']['requests'] == 2 and report['total']['main']['five_points'] == 6
    assert report['allocation']['five']['counted_points'] == 8          # the subscription stays that of the account
    assert report['guards'] == {'soft_stop': {'shadow': {'warn': 1}}}
    assert [s['session'] for s in report['startup']['starts']] == ['llllllll']
    assert report['coverage']['sync']['machine'] == 'laptop'
    assert report['overview']['title'] == 'Day 2026-10-13 · machine laptop'
    assert report['coverage']['sync']['text'].endswith('· only laptop')
    own = world.report('--day', '2026-10-13', '--machine', 'desk')
    assert [cli['id'] for cli in own['clis']] == ['dddddddd'] and own['guards'] == {
        'spawn_gate': {'enforce': {'allow': 1}}}


def test_machine_takes_only_the_day_lines_of_that_machine(world, synced):
    world.projects.mkdir()
    own_archive(world, ('2026-10-11', archive_line(5, 2.0, 'aaaaaaaa', 'proj-a', 3, peak=40)))
    synced.clone.machine(LAPTOP)
    synced.clone.days(LAPTOP, [('2026-10-11', archive_line(7, 4.0, 'bbbbbbbb', 'proj-b', 4, peak=60))])
    report = world.report('--day', '2026-10-11', '--machine', 'laptop')
    assert report['total']['requests'] == 7 and [w['peak'] for w in report['subscription']['five_windows']] == [60]


@pytest.mark.parametrize('name, message', [
    ('', '--machine must not be empty'),
    ('phone', "--machine 'phone' matches no machine (machines: desk, laptop, laptop-2)"),
    ('lap', "--machine 'lap' matches 2 machines (machines: desk, laptop, laptop-2)"),
])
def test_machine_that_fits_none_or_several_exits_2(world, synced, capsys, name, message):
    world.projects.mkdir()
    synced.clone.machine(LAPTOP)
    synced.clone.machine(TABLET, name='laptop-2')
    with pytest.raises(SystemExit) as stop:
        report_module.main(world.args('--machine', name), now=NOW)
    assert stop.value.code == 2 and message in capsys.readouterr().err
    assert report_module.main(world.args('--json', '--machine', 'laptop'), now=NOW) == 0


# --- The switches -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('args, message', [
    (('--local', '--no-pull'), '--local reads this machine only'),
    (('--local', '--machine', 'x'), '--local reads this machine only'),
    (('--local', '--sync'), '--local reads this machine only'),
    (('--sync', '--no-pull'), '--sync pulls: not with --no-pull'),
    (('--archive', '--local'), '--archive writes the archive of this machine'),
    (('--archive', '--sync'), '--archive writes the archive of this machine'),
])
def test_switches_that_do_not_go_together_exit_2(world, synced, capsys, args, message):
    world.projects.mkdir()
    with pytest.raises(SystemExit) as stop:
        report_module.main(world.args(*args), now=NOW)
    assert stop.value.code == 2 and message in capsys.readouterr().err


@pytest.mark.parametrize('args', [('--machine', 'desk'), ('--sync',)])
def test_machine_and_sync_need_the_sync(world, capsys, args):
    world.projects.mkdir()
    with pytest.raises(SystemExit) as stop:
        report_module.main(world.args(*args), now=NOW)
    assert stop.value.code == 2 and 'need the sync: sync.mode shadow or on' in capsys.readouterr().err


def test_the_archive_stays_that_of_this_machine(world, synced):
    two_machines(world, synced)
    synced.clone.days(LAPTOP, [('2026-10-12', archive_line(7, 4.0, 'bbbbbbbb', 'proj-b'))])
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    lines = [json.loads(line) for line in (world.log / 'usage-days.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [(line['day'], line['stats']['requests']) for line in lines] == [('2026-10-13', 1)]
    assert synced.pulls == []


def test_age_texts():
    assert [report_module.age_text(s) for s in (0, 59, 60, 3599, 3600, 48 * 3600 - 1, 48 * 3600)] == [
        'just now', 'just now', '1 min ago', '59 min ago', '1 h ago', '47 h ago', '2 d ago']


# --- Claude Code on the web: one machine per chat, side calls (tools/web_export.py) -------------------------------

WEB1 = '3ccccccc-0000-4000-8000-000000000003'
WEB2 = '4ddddddd-0000-4000-a000-000000000004'
WEB_SESSION = 'wwwwwwww-1'


def side_run(t, cost_cr=0, sid=WEB_SESSION, model='claude-opus-5-5', **more):
    """A side run of the format; `cost_cr` dollars of cache read (0.20 USD per million for opus-5-5)."""
    return {'t': t, 'k': sync_format.side_key(sid, 'extract_memories', t), 'sid': sid, 'src': 'extract_memories',
            'model': model, 'calls': 2, 'in': 1685, 'cw': 0, 'cr': round(cost_cr * 5_000_000), 'out': 0, **more}


def window(t, start=None, end=None, sid=WEB_SESSION, unmeasured=0):
    return {'t': t, 'sid': sid, 'log': start is not None, 'from': start, 'to': end, 'unmeasured': unmeasured}


def web_machine(clone, machine_id, requests=(), runs=(), windows=()):
    clone.machine(machine_id, name='claude-web')
    clone.requests(machine_id, requests)
    for values in runs:
        sync_format.append(clone.root, machine_id, sync_format.side_path(machine_id, values['t']),
                           [sync_format.encode('side', values)])
    for values in windows:
        sync_format.append(clone.root, machine_id, sync_format.side_window_path(machine_id, values['t']),
                           [sync_format.encode('side_window', values)])


def test_a_chat_that_went_on_in_a_new_vm_counts_each_request_once(world, synced):
    """Every chat of Claude Code on the web is a machine of its own; a chat that was interrupted brings its transcript
    into the new VM, whose export writes the same requests again under another machine id. `k` counts them once."""
    first = [request_line('2026-10-13T10:05:00Z', 'w1', 1.0, sid=WEB_SESSION, first=True),
             request_line('2026-10-13T10:15:00Z', 'w2', 1.0, sid=WEB_SESSION)]
    web_machine(synced.clone, WEB1, first)
    web_machine(synced.clone, WEB2, first + [request_line('2026-10-13T10:25:00Z', 'w3', 2.0, sid=WEB_SESSION)])
    report = world.report('--day', '2026-10-13')
    assert report['total']['requests'] == 3 and report['total']['cost_usd'] == pytest.approx(4.0)
    assert by_id(report)['wwwwwwww']['requests'] == 3


def test_side_calls_are_shown_apart_from_the_sums_and_a_missing_log_is_never_zero(world, synced):
    requests = [request_line('2026-10-13T10:05:00Z', 'w1', 1.0, sid=WEB_SESSION, first=True),
                request_line('2026-10-13T10:15:00Z', 'w2', 1.0, sid=WEB_SESSION)]
    run = side_run('2026-10-13T10:06:00Z', cost_cr=0.5)
    web_machine(synced.clone, WEB1, requests, [run], [window('2026-10-13T10:12:00Z', '2026-10-13T10:00:00Z',
                                                             '2026-10-13T10:10:00Z', unmeasured=1)])
    web_machine(synced.clone, WEB2, [request_line('2026-10-13T10:20:00Z', 'x1', 1.0, sid='xxxxxxxx-1', first=True)],
                [run], [window('2026-10-13T10:30:00Z', sid='xxxxxxxx-1')])     # the same run again: counts once
    report = world.report('--day', '2026-10-13')
    assert report['total']['cost_usd'] == pytest.approx(3.0)            # side calls are not in the sums
    side = report['side_calls']
    expected_cost = (1685 * 4 + run['cr'] * 0.20) / 1e6
    assert side == {
        'sources': [{'source': 'extract_memories', 'runs': 1, 'calls': 2, 'cost_usd': pytest.approx(expected_cost),
                     'tokens': {'input': 1685, 'cache_write': 0, 'cache_read': run['cr'], 'output': 0},
                     'unpriced_runs': 0}],
        'cost_usd': pytest.approx(expected_cost), 'sessions': 2, 'sessions_without_log': 1,
        'requests_not_measured': 2, 'unmeasured_calls': 1}
    assert report['overview']['side_calls'] == {'cost_usd': pytest.approx(expected_cost), 'runs': 1,
                                                'requests_not_measured': 2}
    text = world.text('--day', '2026-10-13')
    assert 'Side calls outside the transcripts (Claude Code on the web; not in the sums above)' in text
    assert '  extract_memories  1 run, 2 calls' in text
    assert 'not measured: 2 requests outside a debug log (1 session without any), 1 call without token counts' in text
    assert 'Side calls outside the transcripts: ~' in world.overview('--day', '2026-10-13')


def test_a_side_run_without_a_known_model_has_no_price(world, synced):
    web_machine(synced.clone, WEB1, [request_line('2026-10-13T10:05:00Z', 'w1', 1.0, sid=WEB_SESSION, first=True)],
                [side_run('2026-10-13T10:06:00Z', 1.0, model=None),
                 side_run('2026-10-13T10:07:00Z', 1.0, model='claude-mystery-9')],
                [window('2026-10-13T10:12:00Z', '2026-10-13T10:00:00Z', '2026-10-13T10:10:00Z')])
    source, = world.report('--day', '2026-10-13')['side_calls']['sources']
    assert (source['runs'], source['unpriced_runs'], source['cost_usd']) == (2, 2, 0)
    assert '2 runs without a price' in world.text('--day', '2026-10-13')


def test_side_calls_follow_the_period_and_the_machine(world, synced):
    web_machine(synced.clone, WEB1, [request_line('2026-10-13T10:05:00Z', 'w1', 1.0, sid=WEB_SESSION, first=True)],
                [side_run('2026-10-13T10:06:00Z', 1.0), side_run('2026-10-14T10:06:00Z', 1.0)],
                [window('2026-10-14T10:12:00Z', '2026-10-13T10:00:00Z', '2026-10-14T10:10:00Z')])
    synced.clone.machine(LAPTOP)
    synced.clone.requests(LAPTOP, [request_line('2026-10-13T10:05:00Z', 'l1', 1.0, first=True)])
    assert world.report('--day', '2026-10-13')['side_calls']['sources'][0]['runs'] == 1
    assert 'side_calls' not in world.report('--day', '2026-10-13', '--machine', 'laptop')
    assert 'side_calls' not in world.report('--day', '2026-10-13', '--local')


def test_without_side_lines_the_report_has_no_side_calls(world, synced):
    two_machines(world, synced)
    report = world.report('--day', '2026-10-13')
    assert 'side_calls' not in report and 'side_calls' not in report['overview']
    assert 'Side calls' not in world.text('--day', '2026-10-13')


# --- Gegenprüfung #81: the devices of claude-web as one ------------------------------------------------------------

def three_web_devices(synced):
    web_machine(synced.clone, WEB1, [request_line('2026-10-13T10:05:00Z', 'w1', 1.0, sid=WEB_SESSION, first=True)],
                [side_run('2026-10-13T10:06:00Z', 1.0)],
                [window('2026-10-13T10:12:00Z', '2026-10-13T10:00:00Z', '2026-10-13T10:10:00Z')])
    web_machine(synced.clone, WEB2, [request_line('2026-10-13T11:05:00Z', 'x1', 2.0, sid='xxxxxxxx-1', first=True)],
                [side_run('2026-10-13T11:06:00Z', 1.0, sid='xxxxxxxx-1')],
                [window('2026-10-13T11:12:00Z', '2026-10-13T11:00:00Z', '2026-10-13T11:10:00Z', sid='xxxxxxxx-1')])
    synced.clone.machine(LAPTOP)
    synced.clone.requests(LAPTOP, [request_line('2026-10-13T10:05:00Z', 'l1', 4.0, first=True)])


@pytest.mark.parametrize('name', ['claude-web', 'claude'])
def test_machine_takes_every_device_of_that_name(world, synced, name):
    three_web_devices(synced)
    report = world.report('--day', '2026-10-13', '--machine', name)
    assert report['total']['requests'] == 2 and report['total']['cost_usd'] == pytest.approx(3.0)
    assert report['side_calls']['sources'][0]['runs'] == 2
    sync_part = report['coverage']['sync']
    assert sync_part['machine'] == 'claude-web' and sync_part['text'].endswith(' · only claude-web')
    assert world.report('--day', '2026-10-13', '--machine', 'laptop')['total']['requests'] == 1


def test_the_sources_line_shows_the_devices_of_one_name_as_one(world, synced):
    three_web_devices(synced)
    synced.clone.machine(WEB2, name='claude-web', last_success='2026-10-14T11:30:00Z')
    text = world.report('--day', '2026-10-13')['coverage']['sync']['text']
    assert ' · claude-web (2 devices) 30 min ago · ' in text and text.startswith('Sync: 4 devices · desk ')
    machines = world.report('--day', '2026-10-13')['coverage']['sync']['machines']
    assert [m['name'] for m in machines].count('claude-web') == 2          # the JSON keeps each device


# --- Gegenprüfung #81 Runde 2 (Befund D): Notable treats the devices of claude-web as one ---------------------------

def test_notable_names_no_claude_web_device_as_stale_or_in_another_time_zone(world, synced, monkeypatch):
    """A chat of Claude Code on the web that ended never pushes again, and its VM always runs in UTC: neither is a
    fault, so `sync_stale` and `sync_timezone` leave every device named claude-web out."""
    monkeypatch.setattr(sync_format.time, 'timezone', -3600)
    world.projects.mkdir()
    for key in (WEB1, WEB2):
        synced.clone.machine(key, name=sync_format.WEB_NAME, last_success='2026-10-01T12:00:00Z', utc_std=0)
    synced.state(last_success=NOW - 60)
    notes = {n['rule']: n for n in world.report()['overview']['notable']}
    assert 'sync_stale' not in notes and 'sync_timezone' not in notes
    synced.clone.machine(LAPTOP, last_success='2026-10-11T12:00:00Z', utc_std=-480)
    notes = {n['rule']: n for n in world.report()['overview']['notable']}
    assert notes['sync_stale']['machines'] == [{'name': 'laptop', 'last_success': '2026-10-11T12:00:00Z'}]
    assert notes['sync_timezone']['machines'] == [{'name': 'desk', 'utc_std': 60}, {'name': 'laptop', 'utc_std': -480}]


def test_the_web_name_is_the_one_the_export_gives():
    import importlib.util
    spec = importlib.util.spec_from_file_location('web_export_name', os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'web_export.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.MACHINE_NAME == sync_format.WEB_NAME == 'claude-web'
