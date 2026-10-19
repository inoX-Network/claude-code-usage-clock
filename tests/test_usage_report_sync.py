"""The usage report with the sync (0034-SYNC-REPORT): the pull before a report, the folders of the other machines in
the local clone merged with the local files, and how the report shows them. Invented data only; the clone is a
folder written with hooks/sync_format.py, the git work of the pull and the push is replaced by functions of the test
(the real fast forward is tested with local repositories in tests/test_sync.py). The clock and the time zone are those
of tests/test_usage_report.py."""
import json
import os

import pytest

import _sync_core
import sync
import sync_format
from test_usage_report import NOW, T, dollars, local_zone, report_module, req, row, spend, world  # noqa: F401
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

    def machine(self, machine_id, name='laptop', last_success='2026-10-14T11:00:00Z'):
        sync_format.write_machine(self.root, machine_id, {'name': name, 'platform': 'linux', 'schema': 1,
                                                          'created': '2026-10-01T00:00:00Z'})
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
        self.clone = Clone(tmp_path / 'clone')
        self.pulls, self.pushes = [], []
        self.pull_error = self.push_error = None
        monkeypatch.setattr(sync, 'clone_dir', lambda log_dir, cfg: self.clone.root)
        monkeypatch.setattr(sync, 'git_pull', self.pull)
        monkeypatch.setattr(sync, 'git_push', self.push)

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
        for day, stats in lines:
            f.write(json.dumps({'v': 1, 'day': day, 'stats': stats}) + '\n')


def test_on_a_day_with_requests_the_figures_come_from_the_requests_and_the_subscription_once(world, synced):
    two_machines(world, synced, laptop_history=True)
    own_archive(world, ('2026-10-13', archive_line(99, 50.0, 'dddddddd', 'proj-a', 7, peak=40)))
    synced.clone.days(LAPTOP, [('2026-10-13', archive_line(77, 40.0, 'llllllll', 'proj-l', 9, peak=30))])
    report = world.report('--day', '2026-10-13')
    assert report['total']['requests'] == 3 and report['total']['cost_usd'] == pytest.approx(3.0)
    assert report['coverage']['archive_days'] == []
    assert len(report['subscription']['five_windows']) == 1 and len(report['subscription']['week_segments']) == 1
    assert report['allocation']['five']['counted_points'] == 8
    assert world.report('--day', '2026-10-13', '--local')['total']['requests'] == 99     # the archive, as before


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
    first['agent_types'] = {'reviewer': {'requests': 5, 'cost': 2.0}}
    own_archive(world, ('2026-10-11', first))
    other = archive_line(7, 4.0, 'bbbbbbbb', 'proj-b')
    other['agent_types'] = {'reviewer': 7}      # a number where the first line has figures: they cannot be added
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
    assert synced.pulls == [] and sync_part['pull'] == {'status': 'skipped', 'category': None, 'last_pull': None}
    world.configure(monkeypatch, sync={'mode': 'off'})
    assert 'sync' not in world.report('--no-pull')['coverage'] and synced.pulls == []


def test_a_held_lock_reads_the_clone_as_it_is(world, synced):
    two_machines(world, synced)
    with _sync_core.try_lock(_sync_core.ensure_dir(synced.log)):
        report = world.report('--day', '2026-10-13', '--sync')
    assert synced.pulls == [] and synced.pushes == [] and report['total']['requests'] == 3
    assert report['coverage']['sync']['pull']['status'] == 'locked'
    assert report['coverage']['sync']['push'] == {'status': 'locked', 'category': None}
    assert 'another sync run holds the lock' in report['coverage']['sync']['text']


# --- Criterion: --sync pushes inside a backoff, never after visibility ---------------------------------------------

def test_sync_pushes_inside_a_backoff(world, synced):
    world.projects.mkdir()
    synced.state(backoff_level=2, next_due=NOW + 3600, last_error='network', last_success=NOW - 7200)
    report = world.report('--sync')
    assert len(synced.pushes) == 1 and synced.read_state().backoff_level == 0
    assert report['coverage']['sync']['push'] == {'status': 'ok', 'category': None}
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


def test_a_failed_push_of_sync_is_named_and_the_report_goes_on(world, synced):
    world.projects.mkdir()
    synced.push_error = ('auth', 'no credentials')
    report = world.report('--sync')
    assert report['coverage']['sync']['push'] == {'status': 'failed', 'category': 'auth'}
    assert 'push failed (auth)' in report['coverage']['sync']['text'] and len(synced.pulls) == 1


def test_a_sync_folder_that_cannot_be_used_fails_push_and_pull_but_not_the_report(world, synced):
    world.projects.mkdir()
    os.remove(world.log / 'sync' / 'machine_id')
    os.rmdir(world.log / 'sync')
    (world.log / 'sync').write_text('', encoding='utf-8')
    sync_part = world.report('--sync')['coverage']['sync']
    assert sync_part['push'] == {'status': 'failed', 'category': 'internal'}
    assert sync_part['pull']['status'] == 'failed' and sync_part['pull']['category'] == 'internal'


# --- Criterion: --local prints what the report printed before -----------------------------------------------------

@pytest.mark.parametrize('mode', ['on', 'shadow'])
def test_local_prints_the_saved_output_byte_for_byte(world, monkeypatch, tmp_path, mode):
    machine = Synced(world, monkeypatch, tmp_path, mode)
    build(world)
    machine.clone.machine(LAPTOP)
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
                                                  'last_pull': '2026-10-14T10:00:00Z'}
    note = next(n for n in report['overview']['notable'] if n['rule'] == 'sync_pull')
    assert note['text'] == 'pull failed (network): the other machines as of 2 h ago'
    text = world.overview('--day', '2026-10-13', '--full')
    assert 'pull failed (network), last pull 2 h ago' in text
    assert synced.read_state().last_pull == NOW - 7200


def test_a_pull_that_never_succeeded_says_so(world, synced):
    world.projects.mkdir()
    synced.pull_error = ('timeout', 'the run took too long')
    note = next(n for n in world.report()['overview']['notable'] if n['rule'] == 'sync_pull')
    assert note['text'] == 'pull failed (timeout): the other machines not pulled yet'


# --- Criterion: without a fast forward nothing is merged, and the report says so ------------------------------------

def test_without_a_fast_forward_the_report_names_the_differing_history(world, synced):
    two_machines(world, synced)
    synced.state(last_pull=NOW - 600)
    synced.pull_error = ('conflict', 'no fast forward')
    report = world.report('--day', '2026-10-13')
    assert report['coverage']['sync']['pull']['status'] == 'diverged' and report['total']['requests'] == 3
    note = next(n for n in report['overview']['notable'] if n['rule'] == 'sync_pull')
    assert note['text'] == ('no fast forward, the sync history differs, nothing was merged: the other machines as of '
                            '10 min ago')
    assert 'no fast forward, the sync history differs: nothing merged, last pull 10 min ago' in \
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


def test_the_json_carries_the_machine_of_every_cli_and_the_sync_coverage(world, synced):
    two_machines(world, synced)
    synced.state(last_success=NOW - 60, last_pull=NOW - 1000)
    sync_part = world.report('--day', '2026-10-13')['coverage']['sync']
    assert sync_part['mode'] == 'on' and sync_part['machine'] is None and sync_part['push'] is None
    assert sync_part['machines'] == [
        {'id': synced.own, 'name': 'desk', 'platform': sync_format.platform_name(), 'own': True,
         'last_success': '2026-10-14T11:59:00Z'},
        {'id': LAPTOP, 'name': 'laptop', 'platform': 'linux', 'own': False, 'last_success': '2026-10-14T11:00:00Z'}]
    assert sync_part['pull'] == {'status': 'ok', 'category': None, 'last_pull': '2026-10-14T12:00:00Z'}
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
                                                         'own': False, 'last_success': None}


def test_an_unusable_machine_id_leaves_the_clone_unread(world, synced):
    two_machines(world, synced)
    (world.log / 'sync' / 'machine_id').write_text('not a uuid\n', encoding='utf-8')
    report = world.report('--day', '2026-10-13')
    assert report['total']['requests'] == 1
    assert [m['name'] for m in report['coverage']['sync']['machines']] == ['desk']
    assert report['coverage']['sync']['machines'][0]['id'] is None


def test_a_machine_that_never_synced_reads_the_clone_by_its_configured_name(world, synced, monkeypatch):
    two_machines(world, synced)
    os.remove(world.log / 'sync' / 'machine_id')
    assert world.report('--day', '2026-10-13')['coverage']['sync']['machines'][0]['name'] == 'desk'
    world.configure(monkeypatch, sync={'mode': 'on'})
    report = world.report('--day', '2026-10-13')
    assert report['coverage']['sync']['machines'][0]['name'] == 'this machine' and report['total']['requests'] == 3


def test_without_a_clone_only_this_machine_is_shown(world, synced, monkeypatch):
    two_machines(world, synced)
    monkeypatch.setattr(sync, 'clone_dir', lambda log_dir, cfg: None)
    report = world.report('--day', '2026-10-13')
    assert report['total']['requests'] == 1 and len(report['coverage']['sync']['machines']) == 1


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
