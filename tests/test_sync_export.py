"""Tests for hooks/_sync_export.py: the new local data appended to the own folder of the sync clone, with its cursor.

Invented transcripts, histories, logs and day archives only (the world of the usage report tests, tests/golden for the
fixture that touches every rule of reading transcripts); the clone is a folder, no git."""
import copy
import json
import os
import stat
from datetime import date

import pytest

import _config
import _context_state
import _sync_core
import _sync_export
import _transcripts
import sync_format
from test_usage_report import (NOW, SECRET, T, local_zone, report_module, req, row,  # noqa: F401
                               usage_line, world)
from test_usage_report_golden import DAY, GOLDEN, build, modified

PROJECT = '-home-u-proj-a'
SESSION = 'aaaaaaaa-1'


def config(**sync):
    cfg = copy.deepcopy(_config.DEFAULTS)
    cfg['sync'].update(sync)
    return cfg


@pytest.fixture
def clone(tmp_path):
    path = tmp_path / 'clone'
    path.mkdir()
    return path


def run(world, clone, cfg=None, deadline=None):
    return _sync_export.export(str(clone), str(world.log), cfg or config(), str(world.projects), NOW, deadline)


def own_id(world):
    return sync_format.load_machine_id(str(world.log))


def files(root):
    """Every file below `root`: relative path with `/` -> bytes."""
    found = {}
    for folder, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            with open(path, 'rb') as f:
                found[os.path.relpath(path, root).replace(os.sep, '/')] = f.read()
    return found


def exported(world, clone):
    skipped = dict.fromkeys(sync_format.COUNTERS, 0)
    machine = sync_format.read_machine(str(clone), own_id(world), skipped)
    assert not any(skipped.values()), skipped
    return machine


def request_files(world, clone):
    return {path: content for path, content in files(clone).items() if '/requests/' in path}


def write_lines(path, lines, mode='w'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding='utf-8', newline='\n') as f:
        f.write(''.join((line if isinstance(line, str) else json.dumps(line)) + '\n' for line in lines))


def transcript_path(world, session=SESSION, project=PROJECT):
    return world.projects / project / f'{session}.jsonl'


def sums(requests):
    """Requests, tokens and output by `k`, after the rule of the reader (`latest_by_key`)."""
    latest = sync_format.latest_by_key(requests).values()
    return (len(latest), sum(r['in'] + r['cw5'] + r['cw1h'] + r['cr'] for r in latest), sum(r['out'] for r in latest))


# --- The criteria of the node -----------------------------------------------------------------------------------------

def archive(world):
    assert report_module.main(world.args('--archive'), now=NOW) == 0


def test_a_second_run_without_new_data_changes_nothing_in_the_clone(world, clone, monkeypatch):
    monkeypatch.setattr(report_module, 'PRICES_FILE', os.path.join(GOLDEN, 'prices.json'))
    build(world)
    archive(world)
    first = run(world, clone)
    before = files(clone)
    assert first.lines['request'] and first.lines['history'] and first.lines['gate'] and first.lines['day']
    second = run(world, clone)
    assert files(clone) == before
    assert second.lines == dict.fromkeys(_sync_export.KINDS, 0) and second.bytes == 0
    assert not second.machine_written and second.complete


def test_a_growing_transcript_appends_only_its_new_requests_and_a_changed_one_again(world, clone):
    path = transcript_path(world)
    write_lines(path, [req('2026-10-13T08:00:00Z', 'm1', request='r1', out=10),
                       req('2026-10-13T08:01:00Z', 'm2', request='r2', out=20)])
    other = world.transcript(PROJECT, 'bbbbbbbb-1', [req('2026-10-13T08:00:00Z', 'm9', request='r9', out=5)])
    run(world, clone)
    before = request_files(world, clone)
    write_lines(path, [req('2026-10-13T08:02:00Z', 'm2', request='r2', out=25),       # a later line of r2
                       req('2026-10-13T08:03:00Z', 'm3', request='r3', out=30)], mode='a')
    result = run(world, clone)
    after = request_files(world, clone)
    name = next(p for p in after if p.endswith(f'/{SESSION}.jsonl'))
    assert after[name].startswith(before[name])
    added = [json.loads(line) for line in after[name][len(before[name]):].decode('utf-8').splitlines()]
    assert [(r['k'], r['out']) for r in added] == [(sync_format.request_key('m2', 'r2'), 25),
                                                   (sync_format.request_key('m3', 'r3'), 30)]
    assert [r['first'] for r in added] == [False, False]      # the first request of the transcript was m1
    assert result.lines['request'] == 2
    assert {p: c for p, c in after.items() if p != name} == {p: c for p, c in before.items() if p != name}
    assert other.exists()


def replace_file(path, content):
    fresh = path.with_name(path.name + '.new')
    fresh.write_bytes(content)
    os.replace(fresh, path)


def shorten_file(path, content):
    with open(path, 'r+b') as f:
        f.truncate(len(content.split(b'\n', 1)[0]) + 1)


@pytest.mark.parametrize('change', [replace_file, shorten_file], ids=['replaced', 'shortened'])
def test_a_replaced_or_shortened_transcript_gives_the_same_sums(world, clone, change):
    path = transcript_path(world)
    write_lines(path, [req('2026-10-13T08:00:00Z', 'm1', request='r1', inp=3, out=10),
                       req('2026-10-13T08:01:00Z', 'm2', request='r2', cr=500, out=20),
                       req('2026-10-13T08:02:00Z', None, request='r3', cw=70, out=30)])
    run(world, clone)
    before = sums(exported(world, clone).requests)
    change(path, path.read_bytes())
    result = run(world, clone)
    assert result.lines['request'] >= 1          # read from byte 0 again
    assert sums(exported(world, clone).requests) == before


def test_a_half_last_line_waits_until_it_is_complete(world, clone):
    path = transcript_path(world)
    whole = json.dumps(req('2026-10-13T08:00:00Z', 'm1', request='r1', out=10)) + '\n'
    half = json.dumps(req('2026-10-13T08:01:00Z', 'm2', request='r2', out=20))
    path.parent.mkdir(parents=True)
    path.write_bytes((whole + half[:40]).encode('utf-8'))
    run(world, clone)
    assert [r['out'] for r in exported(world, clone).requests] == [10]
    with open(path, 'ab') as f:
        f.write((half[40:] + '\n').encode('utf-8'))
    run(world, clone)
    assert [r['out'] for r in exported(world, clone).requests] == [10, 20]


def test_a_half_last_line_of_the_history_waits_too(world, clone):
    path = world.log / 'usage-history.jsonl'
    whole, half = json.dumps(history_row(0, 1)) + '\n', json.dumps(history_row(1, 2))
    path.parent.mkdir(parents=True)
    path.write_bytes((whole + half).encode('utf-8'))
    run(world, clone)
    assert [r['five'] for r in exported(world, clone).history] == [1]
    with open(path, 'ab') as f:
        f.write(b'\n')
    run(world, clone)
    assert [r['five'] for r in exported(world, clone).history] == [1, 2]


def report_sums(report):
    """(counts, costs) of the report: what must be equal, and the cost weights, equal but for the rounding."""
    total = report['total']
    models = {name: entry['requests'] for name, entry in {**total['models'], **total['unpriced']}.items()}
    types = total['subagents']['by_type']
    counts = (total['requests'], total['main']['requests'], total['tokens'], models,
              {name: entry['requests'] for name, entry in types.items()})
    return counts, {'total': total['cost_usd'], **{name: entry['cost_usd'] for name, entry in types.items()}}


def export_sums(requests, prices):
    """The figures of report_sums from the exported requests, by the prices and model names of the report."""
    tokens = dict.fromkeys(_transcripts.KINDS, 0)
    main, models, types, costs = 0, {}, {}, {'total': 0.0}
    for request in requests:
        counts = (request['in'], request['cw5'], request['cw1h'], request['cr'], request['out'])
        entry = report_module.find_price(prices, request['model'])
        price = report_module.request_cost(entry, counts) if entry else 0.0
        costs['total'] += price
        for kind, count in zip(_transcripts.KINDS, counts):
            tokens[kind] += count
        name = report_module._model_name(entry, request['model'])
        models[name] = models.get(name, 0) + 1
        if request['sub'] is None:
            main += 1
        else:
            types[request['sub']] = types.get(request['sub'], 0) + 1
            costs[request['sub']] = costs.get(request['sub'], 0.0) + price
    return (len(requests), main, tokens, models, types), costs


@pytest.mark.parametrize('order', [list, lambda lines: lines[::-1]], ids=['as-read', 'reversed'])
def test_the_exported_requests_sum_up_like_the_local_report(world, clone, monkeypatch, order):
    """The golden fixture: last line of a request, lines without a message id, agent types, a request in two
    transcripts with different output, lines that cannot be requests. Its old transcript gets the modification time of
    its request, so that the report reads it as the export does (the report does not open files older than the
    period). The sums do not depend on the order in which the lines are read."""
    prices_file = os.path.join(GOLDEN, 'prices.json')
    monkeypatch.setattr(report_module, 'PRICES_FILE', prices_file)
    build(world)
    modified(world.projects / '-golden-u-proj-a' / 'dddddddd-1.jsonl', '2026-10-13T07:00:00Z')
    report = world.report('--day', DAY)
    result = run(world, clone)
    start, end = report_module.day_bounds(date.fromisoformat(DAY))
    requests = [r for r in sync_format.latest_by_key(order(exported(world, clone).requests)).values()
                if start <= sync_format.parse_time(r['t']) < end]
    counts, costs = export_sums(requests, report_module.load_prices(prices_file))
    expected_counts, expected_costs = report_sums(report)
    assert counts == expected_counts and costs == pytest.approx(expected_costs, abs=1e-9)
    by_session = {}
    for request in requests:      # a request in two transcripts belongs to the session it came from, as in the report
        by_session[request['sid'][:8]] = by_session.get(request['sid'][:8], 0) + 1
    assert by_session == {cli['id']: cli['requests'] for cli in report['clis']}
    assert result.skipped == report['coverage']['unreadable_transcript_lines'] == 2


def test_nothing_outside_the_own_machine_folder_is_created_or_changed(world, clone, monkeypatch):
    monkeypatch.setattr(report_module, 'PRICES_FILE', os.path.join(GOLDEN, 'prices.json'))
    other = 'bbbbbbbb-2222-4333-8444-555555555555'
    write_lines(clone / 'README.md', ['the sync repository'])
    write_lines(clone / 'machines' / other / 'requests' / '2026-10' / 'x.jsonl', ['{}'])
    build(world)
    archive(world)
    before = files(clone)
    run(world, clone)
    after = files(clone)
    own = f'machines/{own_id(world)}/'
    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    assert changed and all(path.startswith(own) for path in changed)


FORBIDDEN = (SECRET, 'hunter2', 'SECRET', 'golden-nowhere', 'nowhere', 'golden-u', '/u/proj', 'subagents', 'agent-s',
             'meta.json', 'ZEBRA', 'rm -rf', 'secret-dir', 'start later', 'checkpoint', 'passwd', 'loose', 'notes')


def test_no_prompt_path_or_command_of_the_fixture_is_in_the_export(world, clone, monkeypatch):
    monkeypatch.setattr(report_module, 'PRICES_FILE', os.path.join(GOLDEN, 'prices.json'))
    build(world)
    command = {'type': 'assistant', 'timestamp': '2026-10-13T15:00:00Z', 'requestId': 'req_ZEBRA_1',
               'cwd': '/golden-nowhere/u/proj-a',
               'message': {'id': 'msg_ZEBRA_1', 'model': 'claude-opus-5-5',
                           'content': [{'type': 'tool_use', 'name': 'Bash',
                                        'input': {'command': 'cat /etc/passwd ZEBRA'}}],
                           'usage': {'input_tokens': 1, 'output_tokens': 2}}}
    world.transcript('-golden-u-proj-a', 'ffffffff-1', [command])
    with open(world.log / 'log.jsonl', 'a', encoding='utf-8') as f:
        f.write(json.dumps({'time': '2026-10-13T15:00:00Z', 'hook': 'spawn_gate', 'mode': 'enforce',
                            'decision': 'deny', 'reason': 'model "rm -rf /secret-dir" unknown: start later'}) + '\n')
    archive(world)
    run(world, clone)
    content = b''.join(files(clone).values()).decode('utf-8')
    names = '\n'.join(files(clone))
    assert exported(world, clone).gates and exported(world, clone).days
    for text in FORBIDDEN:
        assert text not in content and text not in names, text


# --- Labels, gates, rotation ------------------------------------------------------------------------------------------

@pytest.mark.parametrize('mode, extra', [('alias', {'project_aliases': {'proj-a': 'kunde-1'}}),
                                         ('hash', {'label_salt': 'pepper'})])
def test_alias_and_hash_replace_the_folder_names_in_request_and_day_lines(world, clone, monkeypatch, mode, extra):
    monkeypatch.setattr(report_module, 'PRICES_FILE', os.path.join(GOLDEN, 'prices.json'))
    build(world)
    archive(world)
    run(world, clone, config(project_labels=mode, **extra))
    machine = exported(world, clone)
    expected = {sync_format.project_label(name, mode, extra.get('project_aliases', {}), extra.get('label_salt', ''))
                for name in ('proj-a', 'proj-b')}
    assert {r['proj'] for r in machine.requests} == expected
    assert {cli['label'] for day in machine.days for cli in day['stats']['clis'].values()} == expected
    content = b''.join(files(clone).values()).decode('utf-8')
    assert 'proj-a' not in content and 'proj-b' not in content


def test_with_plain_labels_a_day_line_is_the_archive_line_byte_for_byte(world, clone, monkeypatch):
    monkeypatch.setattr(report_module, 'PRICES_FILE', os.path.join(GOLDEN, 'prices.json'))
    build(world)
    archive(world)
    run(world, clone)
    line = (world.log / 'usage-days.jsonl').read_bytes()
    assert files(clone)[f'machines/{own_id(world)}/days/2026-10.jsonl'] == line
    assert {r['proj'] for r in exported(world, clone).requests} == {'proj-a', 'proj-b'}


def test_hash_without_a_salt_stops_before_anything_is_written(world, clone):
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])
    with pytest.raises(_sync_core.SyncError) as raised:
        run(world, clone, config(project_labels='hash'))
    assert raised.value.category == 'internal' and 'label_salt' in raised.value.detail
    assert files(clone) == {} and not (world.log / 'sync' / 'cursor.json').exists()


def gates(world, entries, name='log.jsonl', mode='w'):
    write_lines(world.log / name, entries, mode)


GATE = {'time': '2026-10-13T09:00:00Z', 'hook': 'spawn_gate', 'mode': 'enforce', 'decision': 'allow'}


def test_without_include_gates_no_gate_line_leaves_the_machine(world, clone):
    gates(world, [GATE, {**GATE, 'hook': 'soft_stop', 'decision': 'warn', 'reason': '5h window at 81 %: x'}])
    result = run(world, clone, config(include_gates=False))
    assert result.lines['gate'] == 0 and exported(world, clone).gates == []
    result = run(world, clone)
    assert result.lines['gate'] == 2
    assert [g['reason'] for g in exported(world, clone).gates] == ['', '5h window at N %']


def history_row(minute, five):
    return row(f'2026-10-13T08:{minute:02d}:00Z', five, 30)


def gate_row(minute, five):
    return {**GATE, 'time': f'2026-10-13T08:{minute:02d}:00Z', 'decision': f'd{"x" * five}'}


ROTATING = [('usage-history.jsonl', history_row, 'history', lambda r: r['five']),
            ('log.jsonl', gate_row, 'gates', lambda r: len(r['decision']) - 1)]


@pytest.mark.parametrize('with_id', [True, False], ids=['file-id', 'no-file-id'])
@pytest.mark.parametrize('name, make, folder, value', ROTATING, ids=['history', 'gates'])
def test_a_rotated_file_is_read_on_where_its_old_name_stopped(world, clone, monkeypatch, name, make, folder, value,
                                                               with_id):
    if not with_id:
        monkeypatch.setattr(_sync_export, 'file_id', lambda info: None)
    write_lines(world.log / (name + '.1'), [make(0, 1)])
    write_lines(world.log / name, [make(1, 2), make(2, 3)])
    run(world, clone)
    write_lines(world.log / name, [make(3, 4)], mode='a')
    os.replace(world.log / name, world.log / (name + '.1'))      # the rotation: the old .1 goes
    write_lines(world.log / name, [make(4, 5)])
    run(world, clone)
    write_lines(world.log / name, [make(5, 6)], mode='a')
    run(world, clone)
    records = getattr(exported(world, clone), folder)
    assert [value(r) for r in records] == [1, 2, 3, 4, 5, 6]


def test_two_rotations_between_two_runs_read_the_unknown_file_from_the_start(world, clone):
    write_lines(world.log / 'usage-history.jsonl', [history_row(0, 1)])
    run(world, clone)
    write_lines(world.log / 'usage-history.jsonl', [history_row(1, 2)], mode='a')
    os.replace(world.log / 'usage-history.jsonl', world.log / 'usage-history.jsonl.1')
    write_lines(world.log / 'usage-history.jsonl', [history_row(2, 3)])
    os.replace(world.log / 'usage-history.jsonl', world.log / 'usage-history.jsonl.1')      # a second rotation
    write_lines(world.log / 'usage-history.jsonl', [history_row(3, 4)])
    run(world, clone)
    assert [r['five'] for r in exported(world, clone).history] == [1, 3, 4]      # the line of 08:01 went with the .1


# --- The transcripts in detail ----------------------------------------------------------------------------------------

def test_the_first_request_of_a_transcript_stays_the_first_when_its_line_changes(world, clone):
    path = transcript_path(world)
    write_lines(path, [req('2026-10-13T08:00:00Z', 'm1', request='r1', out=10),
                       req('2026-10-13T08:01:00Z', 'm2', request='r2', out=20)])
    run(world, clone)
    write_lines(path, [req('2026-10-13T08:02:00Z', 'm1', request='r1', out=15)], mode='a')
    run(world, clone)
    assert [(r['out'], r['first']) for r in exported(world, clone).requests] == [(10, True), (20, False), (15, True)]


def test_a_transcript_without_requests_at_first_gets_its_first_request_later(world, clone):
    path = transcript_path(world)
    write_lines(path, [{'type': 'user', 'timestamp': '2026-10-13T08:00:00Z', 'message': {'content': 'x'}}])
    run(world, clone)
    write_lines(path, [req('2026-10-13T08:01:00Z', 'm1', request='r1', out=10),
                       req('2026-10-13T08:02:00Z', 'm2', request='r2', out=20)], mode='a')
    run(world, clone)
    assert [r['first'] for r in exported(world, clone).requests] == [True, False]


def test_a_subagent_has_the_project_of_its_session(world, clone):
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', 'm1', cwd='/home/u/proj-a', out=10)])
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:01:00Z', 's1', cwd='/home/u/proj-a/sub', out=10)],
                     sub='x1', meta={'agentType': 'Explore'})
    world.transcript(PROJECT, 'cccccccc-1', [req('2026-10-13T08:01:00Z', 's2', cwd='/home/u/elsewhere', out=10)],
                     sub='x2', meta={'agentType': 'Explore'})      # no main transcript: its own folder
    run(world, clone)
    assert {(r['sid'], r['sub'], r['proj']) for r in exported(world, clone).requests} == {
        (SESSION, None, 'proj-a'), (SESSION, 'Explore', 'proj-a'), ('cccccccc-1', 'Explore', 'elsewhere')}


def test_the_project_folder_is_kept_when_later_lines_have_another_one(world, clone):
    path = transcript_path(world)
    write_lines(path, [req('2026-10-13T08:00:00Z', 'm1', cwd='/home/u/proj-a', out=10)])
    run(world, clone)
    write_lines(path, [req('2026-10-13T08:01:00Z', 'm2', cwd='/home/u/proj-a/deeper', out=10)], mode='a')
    run(world, clone)
    assert [r['proj'] for r in exported(world, clone).requests] == ['proj-a', 'proj-a']


def test_a_request_line_the_format_does_not_allow_is_counted_and_left_out(world, clone):
    world.transcript(PROJECT, 'bad.session', [req('2026-10-13T08:00:00Z', out=10)])      # not a session id
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', 'm2', out=10)])
    result = run(world, clone)
    assert result.skipped == 1 and result.lines['request'] == 1


def test_lines_without_a_message_id_are_requests_of_their_own(world, clone):
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', None, out=10),
                                        req('2026-10-13T08:00:01Z', None, out=11)])
    run(world, clone)
    assert sums(exported(world, clone).requests)[0] == 2


def test_a_removed_transcript_leaves_the_cursor(world, clone):
    path = world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])
    run(world, clone)
    key = _sync_export.TRANSCRIPTS + f'{PROJECT}/{SESSION}.jsonl'
    assert key in _sync_export.read_cursor(str(world.log / 'sync'), str(world.log))
    path.unlink()
    run(world, clone)
    assert key not in _sync_export.read_cursor(str(world.log / 'sync'), str(world.log))


def test_without_a_file_id_size_and_first_bytes_decide(world, clone, monkeypatch):
    monkeypatch.setattr(_sync_export, 'file_id', lambda info: None)
    path = transcript_path(world)
    write_lines(path, [req('2026-10-13T08:00:00Z', 'm1', request='r1', out=10)])
    run(world, clone)
    write_lines(path, [req('2026-10-13T08:01:00Z', 'm2', request='r2', out=20)], mode='a')
    assert run(world, clone).lines['request'] == 1                 # grown: read on
    replace_file(path, json.dumps(req('2026-10-13T08:05:00Z', 'm7', request='r7', out=70)).encode() + b'\n'
                 + path.read_bytes())
    assert run(world, clone).lines['request'] == 3                 # other first bytes: from the start
    assert sums(exported(world, clone).requests)[2] == 10 + 20 + 70


def test_without_a_file_id_an_older_modification_time_means_another_file(world, clone, monkeypatch):
    monkeypatch.setattr(_sync_export, 'file_id', lambda info: None)
    path = transcript_path(world)
    write_lines(path, [req('2026-10-13T08:00:00Z', 'm1', request='r1', out=10)])
    run(world, clone)
    write_lines(path, [req('2026-10-13T08:01:00Z', 'm2', request='r2', out=20)], mode='a')
    os.utime(path, (T('2026-10-01T00:00:00Z'), T('2026-10-01T00:00:00Z')))
    assert run(world, clone).lines['request'] == 2


def test_the_file_id_is_device_and_inode_and_none_without_an_inode_number():
    assert _sync_export.file_id(os.stat_result((0, 7, 5, 1, 0, 0, 0, 0, 0, 0))) == '5:7'
    assert _sync_export.file_id(os.stat_result((0, 0, 5, 1, 0, 0, 0, 0, 0, 0))) is None


# --- machine.json, cursor, errors -------------------------------------------------------------------------------------

def test_machine_json_is_written_once_and_again_for_a_new_name(world, clone):
    assert run(world, clone).machine_written
    assert not run(world, clone).machine_written
    assert run(world, clone, config(machine_name='laptop')).machine_written
    machine = exported(world, clone).machine
    assert machine['name'] == 'laptop' and machine['platform'] == sync_format.platform_name()


def test_machine_json_carries_the_standard_offset_and_is_written_again_after_a_move(world, clone, monkeypatch):
    monkeypatch.setattr(sync_format.time, 'timezone', 28800)
    assert run(world, clone).machine_written
    assert exported(world, clone).machine['utc_std'] == -480
    assert not run(world, clone).machine_written
    monkeypatch.setattr(sync_format.time, 'timezone', -3600)
    assert run(world, clone).machine_written
    assert exported(world, clone).machine['utc_std'] == 60


@pytest.mark.posix_only
def test_the_cursor_is_private_and_written_atomically(world, clone):
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])
    run(world, clone)
    folder = world.log / 'sync'
    assert stat.S_IMODE(os.stat(folder / 'cursor.json').st_mode) == 0o600
    assert sorted(os.listdir(folder)) == ['cursor.json', 'machine_id']


def test_a_cursor_larger_than_its_limit_counts_as_empty(world, clone, monkeypatch):
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])
    run(world, clone)
    monkeypatch.setattr(_sync_export, 'MAX_CURSOR_BYTES', 10)
    assert _sync_export.read_cursor(str(world.log / 'sync'), str(world.log)) == {}


def test_a_broken_cursor_counts_as_empty_with_one_log_line_and_the_sums_stay(world, clone):
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', 'm1', out=10), req('2026-10-13T08:01:00Z', 'm2',
                                                                                         request='r2', out=20)])
    run(world, clone)
    before = sums(exported(world, clone).requests)
    (world.log / 'sync' / 'cursor.json').write_text('{broken', encoding='utf-8')
    assert run(world, clone).lines['request'] == 2
    assert sums(exported(world, clone).requests) == before
    lines = [json.loads(line) for line in (world.log / 'log.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [line['decision'] for line in lines] == ['cursor_unreadable']


@pytest.mark.parametrize('content', [b'[]', b'{"v": 2, "files": {}}', b'{"v": 1}'])
def test_a_cursor_of_another_shape_counts_as_empty(world, content):
    folder = world.log / 'sync'
    folder.mkdir(parents=True)
    (folder / 'cursor.json').write_bytes(content)
    assert _sync_export.read_cursor(str(folder), str(world.log)) == {}


def test_an_entry_that_does_not_fit_is_left_out(world):
    folder = world.log / 'sync'
    folder.mkdir(parents=True)
    good = {'o': 1, 's': 2, 'm': 3, 'h': 'x', 'id': None}
    entries = {'good': good, 'bool': {**good, 'o': True}, 'negative': {**good, 's': -1}, 'id': {**good, 'id': 5},
               'folder': {**good, 'folder': 5}, 'first': {**good, 'first': ['k']}, 'list': [1]}
    (folder / 'cursor.json').write_text(json.dumps({'v': 1, 'files': entries}), encoding='utf-8')
    assert _sync_export.read_cursor(str(folder), str(world.log)) == {'good': good}


def test_lines_are_in_the_clone_before_their_cursor_moves(world, clone):
    """A run that fails at the gates keeps the cursor of the transcripts and the history, not of the log: the next run
    appends no request again and brings the gates."""
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])
    write_lines(world.log / 'usage-history.jsonl', [history_row(0, 1)])
    gates(world, [GATE])
    machine = f'machines/{own_id(world)}'
    os.makedirs(clone / machine)
    (clone / machine / 'gates').write_text('in the way', encoding='utf-8')
    with pytest.raises(_sync_core.SyncError) as raised:
        run(world, clone)
    assert raised.value.category == 'internal' and 'clone cannot be written' in raised.value.detail
    cursor = _sync_export.read_cursor(str(world.log / 'sync'), str(world.log))
    assert 'usage-history.jsonl' in cursor and 'log.jsonl' not in cursor
    (clone / machine / 'gates').unlink()
    result = run(world, clone)
    assert result.lines == {'request': 0, 'history': 0, 'gate': 1, 'day': 0, 'side': 0}


def test_a_clone_that_cannot_be_written_is_internal(world, clone, monkeypatch):
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])

    def refuse(*args):
        raise PermissionError('no')
    monkeypatch.setattr(sync_format, 'append', refuse)
    with pytest.raises(_sync_core.SyncError) as raised:
        run(world, clone)
    assert (raised.value.category, raised.value.detail) == ('internal', 'the clone cannot be written (PermissionError)')


def test_a_machine_folder_that_is_a_file_is_internal(world, clone):
    (clone / 'machines').mkdir()
    (clone / 'machines' / own_id(world)).write_text('x', encoding='utf-8')
    with pytest.raises(_sync_core.SyncError) as raised:
        run(world, clone)
    assert raised.value.detail == 'machine.json cannot be written (FormatError)'


def test_a_machine_id_that_cannot_be_used_is_internal(world, clone):
    (world.log / 'sync').mkdir(parents=True)
    (world.log / 'sync' / 'machine_id').write_text('not an id', encoding='utf-8')
    with pytest.raises(_sync_core.SyncError) as raised:
        run(world, clone)
    assert raised.value.category == 'internal' and files(clone) == {}


@pytest.mark.parametrize('written', [False, PermissionError('no')])
def test_a_cursor_that_cannot_be_written_is_internal(world, clone, monkeypatch, written):
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])

    def write(*args):
        if isinstance(written, Exception):
            raise written
        return written
    monkeypatch.setattr(_context_state, 'write_private', write)
    with pytest.raises(_sync_core.SyncError) as raised:
        run(world, clone)
    assert raised.value.detail.startswith('cursor.json cannot be written')


def test_source_lines_that_do_not_fit_are_counted_and_left_out(world, clone):
    write_lines(world.log / 'usage-history.jsonl', ['{broken', row('no time', 1, 2), row('2026-10-13T08:00:00Z', -1, 2),
                                                    history_row(1, 5)])
    gates(world, ['{broken', {'hook': 'sync', 'decision': 'success'}, GATE])
    write_lines(world.log / 'usage-days.jsonl', ['{"v": 2, "day": "2026-10-13", "stats": {}}',
                                                 '{"v": 1, "day": "2026-10-13", "stats": {}}'])
    result = run(world, clone)
    assert result.skipped == 3 + 1 + 1
    assert result.lines == {'request': 0, 'history': 1, 'gate': 1, 'day': 1, 'side': 0}


def test_a_source_that_is_no_regular_file_or_cannot_be_read_is_counted(world, clone, monkeypatch):
    (world.log / 'usage-history.jsonl').mkdir(parents=True)
    path = world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])
    gates(world, [GATE])
    real_lstat, real_read = os.lstat, _transcripts.read_requests_from

    def refuse(*args):
        raise PermissionError('no')

    def lstat(name, *args, **kwargs):
        if str(name).endswith('log.jsonl'):
            raise PermissionError('no')
        return real_lstat(name, *args, **kwargs)
    monkeypatch.setattr(os, 'lstat', lstat)
    monkeypatch.setattr(_transcripts, 'read_requests_from', refuse)
    result = run(world, clone)
    assert result.unreadable_files == 3 and result.lines == dict.fromkeys(_sync_export.KINDS, 0)
    monkeypatch.setattr(_transcripts, 'read_requests_from', real_read)
    assert run(world, clone).lines['request'] == 1      # nothing of it was marked as read
    assert path.exists()


def test_a_log_file_that_cannot_be_read_is_counted_and_read_next_time(world, clone, monkeypatch):
    write_lines(world.log / 'usage-history.jsonl', [history_row(0, 1)])

    def refuse(*args):
        raise PermissionError('no')
    real = _sync_export._complete_lines
    monkeypatch.setattr(_sync_export, '_complete_lines', refuse)
    assert run(world, clone).unreadable_files == 1
    monkeypatch.setattr(_sync_export, '_complete_lines', real)
    assert run(world, clone).lines['history'] == 1


def test_a_transcript_that_vanishes_during_the_walk_is_no_error(world, clone, monkeypatch):
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])
    real_lstat = os.lstat

    def lstat(name, *args, **kwargs):
        if str(name).endswith(f'{SESSION}.jsonl'):
            raise FileNotFoundError(name)
        return real_lstat(name, *args, **kwargs)
    monkeypatch.setattr(os, 'lstat', lstat)
    result = run(world, clone)
    assert result.unreadable_files == 0 and result.lines['request'] == 0


class Deadline:
    """`left` is plenty for `ticks` calls, then nothing."""

    def __init__(self, ticks):
        self.ticks = ticks

    def left(self):
        self.ticks -= 1
        return 1000 if self.ticks >= 0 else 0


@pytest.mark.parametrize('ticks', [0, 1, 2, 6, 7])
def test_the_deadline_stops_the_export_at_the_next_file_and_the_next_run_goes_on(world, clone, ticks):
    """Calls of `left`: one per transcript (2), one per log file (history .1 and base, log .1 and base), one before
    the archive, one before the side calls: 8 in all."""
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', 'm1', out=10)])
    world.transcript(PROJECT, 'bbbbbbbb-1', [req('2026-10-13T08:00:00Z', 'm2', request='r2', out=20)])
    write_lines(world.log / 'usage-history.jsonl', [history_row(0, 1)])
    gates(world, [GATE])
    write_lines(world.log / 'usage-days.jsonl', ['{"v":1,"day":"2026-10-13","stats":{}}'])
    write_lines(world.log / _sync_export.SIDE_FILE, [SIDE_WINDOW])
    run(world, clone)                          # a first run, then new lines in every source
    write_lines(world.log / 'usage-history.jsonl', [history_row(1, 2)], mode='a')
    gates(world, [GATE], mode='a')
    write_lines(world.log / 'usage-days.jsonl', ['{"v":1,"day":"2026-10-14","stats":{}}'], mode='a')
    write_lines(world.log / _sync_export.SIDE_FILE, [SIDE_RUN], mode='a')
    write_lines(transcript_path(world, 'bbbbbbbb-1'), [req('2026-10-13T08:01:00Z', 'm3', request='r3', out=30)],
                mode='a')
    stopped = run(world, clone, deadline=Deadline(ticks))
    assert not stopped.complete
    rest = run(world, clone)
    total = {kind: stopped.lines[kind] + rest.lines[kind] for kind in _sync_export.KINDS}
    assert total == {'request': 1, 'history': 1, 'gate': 1, 'day': 1, 'side': 1}
    assert run(world, clone, deadline=Deadline(8)).complete


# --- Gegenprüfung #63 -------------------------------------------------------------------------------------------------

def test_requests_are_in_the_clone_before_the_cursor_of_their_transcript_moves(world, clone):
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])
    machine = f'machines/{own_id(world)}'
    os.makedirs(clone / machine)
    (clone / machine / 'requests').write_text('in the way', encoding='utf-8')
    with pytest.raises(_sync_core.SyncError):
        run(world, clone)
    cursor = _sync_export.read_cursor(str(world.log / 'sync'), str(world.log))
    assert not any(key.startswith(_sync_export.TRANSCRIPTS) for key in cursor)
    (clone / machine / 'requests').unlink()
    assert run(world, clone).lines['request'] == 1
    assert [r['out'] for r in exported(world, clone).requests] == [10]


class Left:
    """A deadline with always the same time left."""

    def __init__(self, seconds):
        self.seconds = seconds

    def left(self):
        return self.seconds


@pytest.mark.parametrize('left, complete', [(31, True), (30, False), (29, False)])
def test_the_export_keeps_30_seconds_for_commit_and_push(world, clone, left, complete):
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])
    result = run(world, clone, deadline=Left(left))
    assert result.complete is complete and result.lines['request'] == (1 if complete else 0)


def test_the_cursor_is_saved_after_each_group_of_sources(world, clone, monkeypatch):
    """A run the watchdog ends (os._exit: no finally) repeats at most the group it was in: before a group starts, the
    cursor of the groups before it is on disk."""
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])
    write_lines(world.log / 'usage-history.jsonl', [history_row(0, 1)])
    gates(world, [GATE])
    write_lines(world.log / 'usage-days.jsonl', ['{"v":1,"day":"2026-10-13","stats":{}}'])
    events = []
    real_write, real_line_file = _sync_export.write_cursor, _sync_export._Export.line_file

    def write(directory, entries):
        events.append(('saved', sorted(entries)))
        real_write(directory, entries)

    def line_file(self, key, *args):
        events.append(('reads', key))
        real_line_file(self, key, *args)
    monkeypatch.setattr(_sync_export, 'write_cursor', write)
    monkeypatch.setattr(_sync_export._Export, 'line_file', line_file)
    run(world, clone)
    transcript = _sync_export.TRANSCRIPTS + f'{PROJECT}/{SESSION}.jsonl'
    assert events == [('saved', [transcript]),
                      ('reads', 'usage-history.jsonl.1'), ('reads', 'usage-history.jsonl'),
                      ('saved', [transcript, 'usage-history.jsonl']),
                      ('reads', 'log.jsonl.1'), ('reads', 'log.jsonl'),
                      ('saved', ['log.jsonl', transcript, 'usage-history.jsonl']),
                      ('reads', 'usage-days.jsonl'),
                      ('saved', ['log.jsonl', transcript, 'usage-days.jsonl', 'usage-history.jsonl']),
                      ('reads', 'side-calls.jsonl')]


def test_a_transcript_shortened_and_then_grown_past_the_old_offset_loses_nothing(world, clone):
    """Larger than the 1024 bytes of the hash, with a first line longer than that: only the size tells the shortened
    file from the old one."""
    path = transcript_path(world)
    write_lines(path, [req('2026-10-13T08:00:00Z', 'm1', request='r1', out=10, text='x' * 2000),
                       req('2026-10-13T08:01:00Z', 'm2', request='r2', out=20),
                       req('2026-10-13T08:02:00Z', 'm3', request='r3', out=30)])
    run(world, clone)
    old_size = path.stat().st_size
    shorten_file(path, path.read_bytes())       # only the first line is left
    run(world, clone)
    write_lines(path, [req('2026-10-13T08:03:00Z', f'n{number}', request=f'q{number}', out=100 + number)
                       for number in range(6)], mode='a')
    assert path.stat().st_size > old_size
    run(world, clone)
    outs = {r['out'] for r in exported(world, clone).requests}
    assert outs == {10, 20, 30} | {100 + number for number in range(6)}


# --- Side calls of Claude Code on the web (side-calls.jsonl, written by tools/web_export.py) ------------------------

SIDE_T = '2026-10-13T08:05:00.500Z'
SIDE_RUN = {'kind': 'side', 't': SIDE_T, 'k': sync_format.side_key(SESSION, 'extract_memories', SIDE_T),
            'sid': SESSION, 'src': 'extract_memories', 'model': 'claude-opus-5-5', 'calls': 2, 'in': 1685, 'cw': 1700,
            'cr': 182693, 'out': 451}
SIDE_WINDOW = {'kind': 'side_window', 't': '2026-10-13T09:00:00Z', 'sid': SESSION, 'log': True,
               'from': '2026-10-13T07:00:00Z', 'to': '2026-10-13T08:59:00Z', 'unmeasured': 0}


def without_kind(line):
    return {'v': 1, **{key: value for key, value in line.items() if key != 'kind'}}


def test_side_calls_and_their_window_go_to_their_own_files(world, clone):
    write_lines(world.log / _sync_export.SIDE_FILE, [SIDE_RUN, SIDE_WINDOW])
    result = run(world, clone)
    assert result.lines == {'request': 0, 'history': 0, 'gate': 0, 'day': 0, 'side': 2}
    machine = exported(world, clone)
    assert machine.side == [without_kind(SIDE_RUN)] and machine.side_windows == [without_kind(SIDE_WINDOW)]
    own = sync_format.machine_dir(own_id(world))
    assert {path for path in files(clone) if '/side' in path} == {f'{own}/side/2026-10.jsonl',
                                                                   f'{own}/side_windows/2026-10.jsonl'}


def test_side_calls_are_read_on_from_where_the_last_run_stopped(world, clone):
    path = world.log / _sync_export.SIDE_FILE
    write_lines(path, [SIDE_RUN])
    run(world, clone)
    assert run(world, clone).lines['side'] == 0
    later = dict(SIDE_RUN, t='2026-10-13T08:15:00Z', k=sync_format.side_key(SESSION, 'extract_memories',
                                                                         '2026-10-13T08:15:00Z'), out=300)
    write_lines(path, [later, SIDE_WINDOW], mode='a')
    assert run(world, clone).lines['side'] == 2
    machine = exported(world, clone)
    assert [r['out'] for r in machine.side] == [451, 300] and len(machine.side_windows) == 1


@pytest.mark.parametrize('line', [
    '{broken', dict(SIDE_RUN, kind='request'), dict(SIDE_RUN, kind='other'), dict(SIDE_RUN, src='Extract'),
    dict(SIDE_RUN, cr=-1), dict(SIDE_WINDOW, log=False), {key: v for key, v in SIDE_RUN.items() if key != 'kind'},
    ['side'], dict(SIDE_RUN, t='yesterday')],
    ids=['no-json', 'request-kind', 'unknown-kind', 'source', 'negative', 'no-log-with-span', 'no-kind', 'a-list',
         'bad-time'])
def test_a_side_line_that_does_not_fit_is_counted_and_left_out(world, clone, line):
    write_lines(world.log / _sync_export.SIDE_FILE, [line, SIDE_RUN])
    result = run(world, clone)
    assert (result.skipped, result.lines['side']) == (1, 1)
    assert exported(world, clone).side == [without_kind(SIDE_RUN)]


def test_without_side_calls_nothing_of_them_is_written(world, clone):
    world.transcript(PROJECT, SESSION, [req('2026-10-13T08:00:00Z', out=10)])
    result = run(world, clone)
    assert result.lines['side'] == 0 and not [path for path in files(clone) if '/side' in path]
