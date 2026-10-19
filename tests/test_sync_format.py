"""sync_format.py: the layout and line formats of the sync repository, the writer's allowlist, the suspicious reader,
compacting old request months, and the sync keys of the configuration."""
import json
import os
import re
import stat
import uuid

import pytest

import _config
import sync_format as sf
from conftest import ROOT

OWN = '0b1c2d3e-4f50-4a6b-8c7d-8e9fa0b1c2d3'
OTHER = '11111111-2222-4333-8444-555555555555'
SESSION = 'aaaaaaaa-0000-4000-8000-000000000001'
NOW = 1_792_411_200     # 2026-10-19T12:00:00Z

REQUEST = {'t': '2026-10-13T12:30:00.250Z', 'k': sf.request_key('msg_01', 'req_01'), 'sid': SESSION, 'sub': None,
           'proj': 'webshop', 'model': 'claude-opus-5-5', 'in': 10, 'cw5': 20, 'cw1h': 30, 'cr': 40, 'out': 50,
           'think': 5, 'ws': None, 'wf': 1, 'first': True}
HISTORY = {'t': '2026-10-13T12:30:00Z', 'five': 40, 'week': 61.5, 'five_resets': 1_768_399_200, 'week_resets': None}
GATE = {'t': '2026-10-13T12:31:00Z', 'hook': 'soft_stop', 'decision': 'deny', 'mode': 'enforce',
        'reason': 'checkpoint due'}
MACHINE = {'name': 'laptop', 'platform': 'linux', 'schema': 1, 'created': '2026-10-01T08:00:00Z'}
STATUS = {'last_success': '2026-10-13T12:40:00Z', 'tool_version': '1.6.0', 'schema': 1}
DAY = '{"v":1,"day":"2026-10-12","stats":{"requests":3,"cost":1.5}}'
RECORDS = {'request': REQUEST, 'history': HISTORY, 'gate': GATE, 'machine': MACHINE, 'status': STATUS}


def record(kind, **changes):
    return {'v': 1, **RECORDS[kind], **changes}


def request(t, k, sid=SESSION):
    return dict(REQUEST, t=t, k=k, sid=sid)


def write_file(path, content=b''):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(content if isinstance(content, bytes) else content.encode('utf-8'))
    return path


def machine_file(root, machine_id, relative, content):
    return write_file(os.path.join(str(root), 'machines', machine_id, *relative.split('/')), content)


def all_names(root):
    for folder, dirs, files in os.walk(root):
        yield from dirs
        yield from files


# --- Round trip: writing, then reading gives the same values --------------------------------------------------------

@pytest.mark.parametrize('kind', ['request', 'history', 'gate', 'machine', 'status'])
def test_round_trip_of_every_line_kind(kind):
    line = sf.encode(kind, RECORDS[kind])
    assert line.endswith('\n') and line.count('\n') == 1
    assert sf.decode(kind, line) == record(kind)
    assert sf.decode(kind, line.encode('utf-8')) == record(kind)


def test_round_trip_of_a_day_line_keeps_the_archive_line_as_it_is():
    assert sf.encode_day(DAY + '\n') == DAY + '\n'
    assert sf.decode('day', sf.encode_day(DAY)) == json.loads(DAY)


def test_round_trip_through_the_files_of_the_layout(tmp_path):
    lines = {sf.request_path(OWN, REQUEST['t'], SESSION): sf.encode('request', REQUEST),
             sf.history_path(OWN, HISTORY['t']): sf.encode('history', HISTORY),
             sf.gate_path(OWN, GATE['t']): sf.encode('gate', GATE),
             sf.day_path(OWN, '2026-10-12'): sf.encode_day(DAY)}
    for relative, line in lines.items():
        assert sf.append(str(tmp_path), OWN, relative, [line]) == len(line.encode('utf-8'))
    assert sf.write_machine(str(tmp_path), OWN, MACHINE) is True
    sf.write_status(str(tmp_path), OWN, STATUS)
    repo = sf.read_repo(str(tmp_path))
    assert repo.skipped == dict.fromkeys(sf.COUNTERS, 0)
    machine = repo.machines[OWN]
    assert (machine.requests, machine.history, machine.gates) == ([record('request')], [record('history')],
                                                                   [record('gate')])
    assert machine.days == [json.loads(DAY)]
    assert (machine.machine, machine.status) == (record('machine'), record('status'))


def test_times_keep_their_milliseconds_and_read_back_as_the_same_moment():
    assert sf.iso_time(1_760_000_000) == '2025-10-09T08:53:20Z'
    assert sf.iso_time(1_760_000_000.25) == '2025-10-09T08:53:20.250Z'
    assert sf.parse_time(sf.iso_time(1_760_000_000.25)) == 1_760_000_000.25
    assert sf.parse_time('2025-10-09T08:53:20.250000Z') == 1_760_000_000.25


# --- The writer lets through only the fields of the format ------------------------------------------------------------

@pytest.mark.parametrize('kind', ['request', 'history', 'gate', 'machine', 'status'])
def test_an_unknown_field_is_dropped_by_the_writer(kind):
    line = sf.encode(kind, dict(RECORDS[kind], prompt='delete everything', path='/home/someone/secret'))
    assert 'prompt' not in json.loads(line) and 'path' not in json.loads(line)
    assert 'delete everything' not in line and 'secret' not in line
    assert json.loads(line) == record(kind)


def test_an_unknown_field_of_a_day_line_is_dropped_and_the_rest_kept():
    line = sf.encode_day('{"v":1,"day":"2026-10-12","prompt":"hello","stats":{"requests":3,"cost":1.5}}')
    assert json.loads(line) == json.loads(DAY) and 'hello' not in line


def test_the_writer_sets_v_itself():
    assert json.loads(sf.encode('history', dict(HISTORY, v=7)))['v'] == 1


def test_a_day_line_spread_over_lines_is_written_as_one_line():
    assert sf.encode_day('{"v":1,\n"day":"2026-10-12","stats":{}}') == '{"v":1,"day":"2026-10-12","stats":{}}\n'


@pytest.mark.parametrize('kind, changes', [
    ('request', {'k': 'MSG_01'}), ('request', {'k': '0123'}), ('request', {'sid': '../x'}), ('request', {'sid': 'a:b'}),
    ('request', {'sub': 'a b'}), ('request', {'proj': 'a/b'}), ('request', {'proj': ''}), ('request', {'in': -1}),
    ('request', {'in': 1.5}), ('request', {'in': True}), ('request', {'out': 10 ** 13}), ('request', {'think': 'x'}),
    ('request', {'first': 1}), ('request', {'t': '2026-10-13 12:30:00Z'}),
    ('request', {'t': '2026-10-13T12:30:00+00:00'}),
    ('request', {'t': '2026-02-30T12:00:00Z'}), ('request', {'t': 1_760_000_000}), ('history', {'five': float('nan')}),
    ('history', {'week': float('inf')}), ('history', {'five': True}), ('history', {'five': '40'}),
    ('history', {'week_resets': 5_000_000_000}), ('gate', {'hook': 'usage_clock'}), ('gate', {'decision': 'Deny!'}),
    ('gate', {'mode': ''}), ('gate', {'reason': 'a: b'}), ('gate', {'reason': 'x' * 61}), ('machine', {'name': ''}),
    ('machine', {'name': 'a\nb'}), ('machine', {'name': 'host/name'}), ('machine', {'platform': 'freebsd'}),
    ('machine', {'schema': 0}), ('machine', {'schema': True}), ('status', {'tool_version': '1.6.0 beta'}),
    ('status', {'last_success': None}),
])
def test_a_value_outside_the_format_is_refused_with_category_internal(kind, changes):
    with pytest.raises(sf.FormatError) as error:
        sf.encode(kind, dict(RECORDS[kind], **changes))
    assert error.value.category == 'internal'
    assert all(str(value) not in str(error.value) for value in changes.values() if str(value) not in ('', '0', '1'))


@pytest.mark.parametrize('field', ['t', 'k', 'sid', 'proj', 'model', 'in', 'cw5', 'cw1h', 'cr', 'out', 'first'])
def test_a_missing_required_field_is_refused(field):
    values = dict(REQUEST)
    del values[field]
    with pytest.raises(sf.FormatError, match=f'request.{field}: missing'):
        sf.encode('request', values)


def test_the_optional_fields_of_a_request_may_be_left_out_as_null():
    values = {key: value for key, value in REQUEST.items() if key not in ('sub', 'think', 'ws', 'wf')}
    assert json.loads(sf.encode('request', values)) == record('request', sub=None, think=None, ws=None, wf=None)


def test_a_model_name_outside_the_safe_characters_becomes_a_question_mark():
    assert json.loads(sf.encode('request', dict(REQUEST, model='<synthetic>')))['model'] == '?'
    assert json.loads(sf.encode('request', dict(REQUEST, model=None)))['model'] == '?'


def test_a_record_that_is_not_an_object_is_refused():
    with pytest.raises(sf.FormatError, match='not an object'):
        sf.encode('gate', ['t'])


@pytest.mark.parametrize('raw', ['not json', '[1]', '{"v":2,"day":"2026-10-12","stats":{}}',
                                 '{"v":true,"day":"2026-10-12","stats":{}}', '{"day":"2026-10-12","stats":{}}',
                                 '{"v":1,"day":"2026-13-01","stats":{}}', '{"v":1,"day":"12.10.2026","stats":{}}',
                                 '{"v":1,"day":20261012,"stats":{}}', '{"v":1,"day":"2026-10-12","stats":[]}'])
def test_a_line_that_is_no_archive_line_of_version_1_is_refused_as_day_line(raw):
    with pytest.raises(sf.FormatError):
        sf.encode_day(raw)


# --- Fields made from local data --------------------------------------------------------------------------------------

def test_the_request_key_is_16_hex_characters_of_sha256_of_message_and_request_id():
    import hashlib
    assert sf.request_key('msg_01', 'req_01') == hashlib.sha256(b'msg_01\nreq_01').hexdigest()[:16]
    assert sf.request_key('msg_01', 'req_02') != sf.request_key('msg_01', 'req_01')


def test_plain_labels_are_the_folder_name_as_the_report_shows_it():
    assert sf.project_label('webshop', 'plain', {}, '') == 'webshop'
    assert sf.project_label('a/b"c', 'plain', {}, '') == 'a?b?c'
    assert sf.project_label('', 'plain', {}, '') == '?'
    assert sf.project_label('x' * 80, 'plain', {}, '') == 'x' * 64


def test_alias_labels_come_from_the_aliases_or_are_unnamed_with_a_hash():
    import hashlib
    aliases = {'webshop': 'shop', 'blank': '', 'odd': 'a/b'}
    assert sf.project_label('webshop', 'alias', aliases, '') == 'shop'
    assert sf.project_label('odd', 'alias', aliases, '') == 'a?b'
    for folder in ('secret-client', 'blank'):
        assert sf.project_label(folder, 'alias', aliases, '') == \
            'unbenannt-' + hashlib.sha256(folder.encode()).hexdigest()[:8]


def test_hash_labels_need_the_salt_and_never_show_the_folder_name():
    import hashlib
    label = sf.project_label('secret-client', 'hash', {}, 'pepper')
    assert label == 'p-' + hashlib.sha256(b'pepper\nsecret-client').hexdigest()[:12]
    assert sf.LABEL.fullmatch(label) and 'secret' not in label
    assert sf.project_label('secret-client', 'hash', {}, 'other') != label


def test_hash_without_label_salt_stops_with_category_internal_and_never_falls_back_to_plain():
    with pytest.raises(sf.FormatError) as error:
        sf.project_label('secret-client', 'hash', {}, '')
    assert error.value.category == 'internal' and 'label_salt' in str(error.value)
    assert 'secret-client' not in str(error.value)


def test_an_unknown_label_mode_is_refused():
    with pytest.raises(sf.FormatError, match='project_labels'):
        sf.project_label('webshop', 'clear', {}, '')


@pytest.mark.parametrize('reason, kind', [
    ('checkpoint due: write it to /home/me/agent-checkpoints/x.md', 'checkpoint due'),
    ('5h window at 78.5 %: no new blocks', '5h window at N %'),
    ('agent "stripe-meister" has no effort', 'agent … has no effort'),
    ('WEEK 92 % · start stripe-meister?', '7-day window at N %'),
    ('WOCHE 92 % · stripe-meister starten?', '7-day window at N %'),
    ('odd <chars> & more', 'odd ?chars? ? more'),
    ('x' * 100, 'x' * 60),
    (None, ''),
])
def test_the_gate_reason_is_the_text_before_the_first_colon_without_values(reason, kind):
    assert sf.gate_reason(reason) == kind
    assert sf.REASON.fullmatch(kind)


def test_a_gate_line_from_the_local_log_takes_only_time_gate_decision_mode_and_the_short_reason():
    row = {'time': '2026-10-13T14:31:00+02:00', 'hook': 'spawn_gate', 'mode': 'shadow', 'decision': 'deny',
           'reason': 'agent "x" unknown: details', 'tool_name': 'Agent', 'agent_id': 'abc', 'input_keys': ['prompt'],
           'subagent_type': 'x', 'model_in_call': 'opus'}
    fields = sf.gate_from_log(row)
    assert fields == {'t': '2026-10-13T12:31:00Z', 'hook': 'spawn_gate', 'decision': 'deny', 'mode': 'shadow',
                      'reason': 'agent … unknown'}
    assert json.loads(sf.encode('gate', fields)) == {'v': 1, **fields}


def test_a_gate_line_never_carries_more_than_the_log_line_had():
    fields = sf.gate_from_log({'time': '2026-10-13T12:31:00Z', 'hook': 'soft_stop', 'decision': 'Allow!', 'mode': 7})
    assert fields == {'t': '2026-10-13T12:31:00Z', 'hook': 'soft_stop', 'decision': 'other', 'mode': 'unknown',
                      'reason': ''}


@pytest.mark.parametrize('row', [None, [], {'time': '2026-10-13T12:31:00Z', 'hook': 'config'},
                                 {'hook': 'soft_stop'}, {'time': 5, 'hook': 'soft_stop'},
                                 {'time': 'yesterday', 'hook': 'soft_stop'},
                                 {'time': '2026-10-13T12:31:00', 'hook': 'soft_stop'}])
def test_a_log_line_that_is_no_gate_decision_with_a_zoned_time_gives_no_gate_line(row):
    assert sf.gate_from_log(row) is None


def test_the_platform_is_one_of_three_names(monkeypatch):
    assert sf.platform_name() in sf.PLATFORMS
    for os_name, platform, expected in (('nt', 'win32', 'windows'), ('posix', 'darwin', 'darwin'),
                                        ('posix', 'linux', 'linux'), ('posix', 'freebsd14', 'linux')):
        with monkeypatch.context() as patch:
            patch.setattr(sf.os, 'name', os_name)
            patch.setattr(sf.sys, 'platform', platform)
            result = sf.platform_name()
        assert result == expected


def test_the_display_name_is_the_configured_name_or_made_from_the_id_never_the_host_name():
    import socket
    assert sf.display_name(OWN, 'laptop') == 'laptop'
    assert sf.display_name(OWN, '') == 'machine-0b1c2d'
    assert socket.gethostname() not in sf.display_name(OWN, '')


# --- The machine id ---------------------------------------------------------------------------------------------------

def test_the_machine_id_is_made_once_as_uuid4_and_read_again(tmp_path):
    log_dir = str(tmp_path / 'log')
    assert sf.load_machine_id(log_dir, create=False) is None
    machine_id = sf.load_machine_id(log_dir)
    assert uuid.UUID(machine_id).version == 4 and sf.MACHINE_ID.fullmatch(machine_id)
    assert sf.load_machine_id(log_dir) == machine_id == sf.load_machine_id(log_dir, create=False)
    assert os.listdir(os.path.join(log_dir, 'sync')) == ['machine_id']      # no temporary file left


@pytest.mark.posix_only
def test_the_machine_id_file_is_private(tmp_path):
    log_dir = str(tmp_path / 'log')
    sf.load_machine_id(log_dir)
    assert stat.S_IMODE(os.stat(os.path.join(log_dir, 'sync', 'machine_id')).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.join(log_dir, 'sync')).st_mode) == 0o700


def test_a_machine_id_made_by_another_process_at_the_same_time_wins(tmp_path, monkeypatch):
    log_dir = str(tmp_path / 'log')
    real_link = os.link

    def quicker(source, target):
        write_file(target, OTHER + '\n')
        return real_link(source, target)
    monkeypatch.setattr(sf.os, 'link', quicker)
    assert sf.load_machine_id(log_dir) == OTHER
    assert os.listdir(os.path.join(log_dir, 'sync')) == ['machine_id']


@pytest.mark.parametrize('content', [b'', b'not-a-uuid', b'11111111-2222-1333-8444-555555555555', b'\xff' * 10])
def test_a_machine_id_file_without_a_uuid4_is_refused_and_never_replaced(tmp_path, content):
    path = write_file(str(tmp_path / 'log' / 'sync' / 'machine_id'), content)
    with pytest.raises(sf.FormatError) as error:
        sf.load_machine_id(str(tmp_path / 'log'))
    assert error.value.category == 'internal'
    with open(path, 'rb') as f:
        assert f.read() == content


def test_a_machine_id_file_that_is_too_large_or_a_folder_is_refused(tmp_path):
    write_file(str(tmp_path / 'a' / 'sync' / 'machine_id'), OWN + ' ' * 100)
    os.makedirs(str(tmp_path / 'b' / 'sync' / 'machine_id'))
    for log_dir in ('a', 'b'):
        with pytest.raises(sf.FormatError, match='regular file'):
            sf.load_machine_id(str(tmp_path / log_dir))


@pytest.mark.posix_only
def test_a_machine_id_file_or_sync_folder_that_is_a_link_is_refused(tmp_path):
    elsewhere = write_file(str(tmp_path / 'elsewhere' / 'machine_id'), OWN)
    os.makedirs(str(tmp_path / 'a' / 'sync'))
    os.symlink(elsewhere, str(tmp_path / 'a' / 'sync' / 'machine_id'))
    os.makedirs(str(tmp_path / 'b'))
    os.symlink(os.path.dirname(elsewhere), str(tmp_path / 'b' / 'sync'))
    with pytest.raises(sf.FormatError, match='regular file'):
        sf.load_machine_id(str(tmp_path / 'a'))
    with pytest.raises(sf.FormatError, match='is a link'):
        sf.load_machine_id(str(tmp_path / 'b'))


# --- Paths ----------------------------------------------------------------------------------------------------------

def test_the_paths_of_the_layout():
    base = f'machines/{OWN}'
    assert sf.machine_dir(OWN) == base
    assert sf.machine_path(OWN) == f'{base}/machine.json'
    assert sf.status_path(OWN) == f'{base}/status.json'
    assert sf.request_path(OWN, '2026-10-31T23:59:59Z', SESSION) == f'{base}/requests/2026-10/{SESSION}.jsonl'
    assert sf.history_path(OWN, '2026-11-01T00:00:00Z') == f'{base}/history/2026-11.jsonl'
    assert sf.gate_path(OWN, '2026-01-05T00:00:00Z') == f'{base}/gates/2026-01.jsonl'
    assert sf.day_path(OWN, '2026-12-31') == f'{base}/days/2026.jsonl'


@pytest.mark.parametrize('call', [
    lambda: sf.machine_dir('../../etc'), lambda: sf.machine_dir('/abs'), lambda: sf.machine_dir(OWN.upper()),
    lambda: sf.machine_dir(None), lambda: sf.request_path(OWN, REQUEST['t'], '../x'),
    lambda: sf.request_path(OWN, REQUEST['t'], 'C:x'), lambda: sf.request_path(OWN, '2026-10', SESSION),
    lambda: sf.history_path(OWN, '13:00'), lambda: sf.gate_path(OWN, ''), lambda: sf.day_path(OWN, '2026-1-1'),
])
def test_a_path_from_a_value_outside_the_layout_is_refused(call):
    with pytest.raises(sf.FormatError):
        call()


def test_no_file_name_of_the_layout_has_a_colon(tmp_path):
    root = str(tmp_path)
    times = ['2026-10-13T12:30:00Z', '2026-10-13T23:59:59.999Z', '2027-01-01T00:00:00.000Z']
    for t in times:
        sf.append(root, OWN, sf.request_path(OWN, t, SESSION), [sf.encode('request', dict(REQUEST, t=t))])
        sf.append(root, OWN, sf.history_path(OWN, t), [sf.encode('history', dict(HISTORY, t=t))])
        sf.append(root, OWN, sf.gate_path(OWN, t), [sf.encode('gate', dict(GATE, t=t))])
    sf.append(root, OWN, sf.day_path(OWN, '2026-10-12'), [sf.encode_day(DAY)])
    sf.write_machine(root, OWN, MACHINE)
    sf.write_status(root, OWN, STATUS)
    names = list(all_names(root))
    assert len(names) >= 12
    assert [name for name in names if ':' in name] == []
    for relative in (sf.machine_path(OWN), sf.status_path(OWN)):
        assert ':' not in relative


# --- Writing --------------------------------------------------------------------------------------------------------

def test_append_writes_only_line_files_of_the_layout_in_the_own_folder(tmp_path):
    root = str(tmp_path)
    line = sf.encode('history', HISTORY)
    for relative in (sf.history_path(OTHER, HISTORY['t']), f'machines/{OWN}/../{OTHER}/history/2026-10.jsonl',
                     f'machines/{OWN}/history/../../{OTHER}/days/2026.jsonl', 'README.md',
                     f'machines/{OWN}/status.json', f'machines/{OWN}/history/2026-10.txt',
                     f'machines/{OWN}/requests/2026-10/a:b.jsonl', f'machines/{OWN}/notes/2026-10.jsonl'):
        with pytest.raises(sf.FormatError):
            sf.append(root, OWN, relative, [line])
    assert os.listdir(root) == []


def test_append_adds_lines_and_gives_a_cut_off_last_line_its_line_break(tmp_path):
    root, relative = str(tmp_path), sf.history_path(OWN, HISTORY['t'])
    assert sf.append(root, OWN, relative, []) == 0
    path = machine_file(root, OWN, 'history/2026-10.jsonl', '{"v":1,"t":"cut')
    line = sf.encode('history', HISTORY)
    assert sf.append(root, OWN, relative, [line, line]) == 1 + 2 * len(line)
    with open(path, encoding='utf-8') as f:
        assert f.read().split('\n') == ['{"v":1,"t":"cut', line[:-1], line[:-1], '']
    repo = sf.read_repo(root)
    assert repo.machines[OWN].history == [record('history')] * 2 and repo.skipped['broken_lines'] == 1


@pytest.mark.parametrize('lines', [['{"v":1}'], ['{"v":1}\r\n'], ['{"v":1}\n', '{"v":1}']])
def test_append_takes_only_whole_lines(tmp_path, lines):
    with pytest.raises(sf.FormatError, match='line break'):
        sf.append(str(tmp_path), OWN, sf.history_path(OWN, HISTORY['t']), lines)


@pytest.mark.posix_only
def test_append_never_writes_through_a_link_or_into_a_fifo(tmp_path):
    root = str(tmp_path / 'repo')
    target = write_file(str(tmp_path / 'target.jsonl'), '')
    history = os.path.join(root, 'machines', OWN, 'history')
    os.makedirs(history)
    os.symlink(target, os.path.join(history, '2026-10.jsonl'))
    os.mkfifo(os.path.join(history, '2026-11.jsonl'))
    line = sf.encode('history', HISTORY)
    with pytest.raises(sf.FormatError, match='a link'):
        sf.append(root, OWN, sf.history_path(OWN, '2026-10-01T00:00:00Z'), [line])
    with pytest.raises(sf.FormatError, match='not a regular file'):
        sf.append(root, OWN, sf.history_path(OWN, '2026-11-01T00:00:00Z'), [line])
    assert os.path.getsize(target) == 0


@pytest.mark.posix_only
def test_append_never_follows_a_link_as_a_folder(tmp_path):
    root = str(tmp_path / 'repo')
    outside = str(tmp_path / 'outside')
    os.makedirs(outside)
    os.makedirs(os.path.join(root, 'machines'))
    os.symlink(outside, os.path.join(root, 'machines', OWN))
    with pytest.raises(sf.FormatError, match='a link or a file on the way'):
        sf.append(root, OWN, sf.history_path(OWN, HISTORY['t']), [sf.encode('history', HISTORY)])
    assert os.listdir(outside) == []


def test_append_refuses_a_file_where_a_folder_belongs(tmp_path):
    write_file(os.path.join(str(tmp_path), 'machines', OWN, 'history'), '')
    with pytest.raises(sf.FormatError, match='a link or a file on the way'):
        sf.append(str(tmp_path), OWN, sf.history_path(OWN, HISTORY['t']), [sf.encode('history', HISTORY)])


def test_a_folder_made_by_another_process_at_the_same_time_is_used(tmp_path, monkeypatch):
    real_mkdir = os.mkdir

    def quicker(path, mode=0o777):
        real_mkdir(path, mode)
        raise FileExistsError(path)
    monkeypatch.setattr(sf.os, 'mkdir', quicker)
    sf.append(str(tmp_path), OWN, sf.history_path(OWN, HISTORY['t']), [sf.encode('history', HISTORY)])
    assert os.path.isfile(os.path.join(str(tmp_path), 'machines', OWN, 'history', '2026-10.jsonl'))


def test_machine_json_is_written_once_and_never_overwritten(tmp_path):
    root = str(tmp_path)
    assert sf.write_machine(root, OWN, MACHINE) is True
    assert sf.write_machine(root, OWN, dict(MACHINE, name='desktop')) is False
    assert sf.read_repo(root).machines[OWN].machine == record('machine')


def test_status_json_is_replaced_and_a_leftover_temporary_file_removed(tmp_path):
    root = str(tmp_path)
    sf.write_status(root, OWN, STATUS)
    write_file(os.path.join(root, 'machines', OWN, f'status.json.tmp.{os.getpid()}'), 'leftover')
    sf.write_status(root, OWN, dict(STATUS, last_success='2026-10-14T00:00:00Z'))
    assert os.listdir(os.path.join(root, 'machines', OWN)) == ['status.json']
    assert sf.read_repo(root).machines[OWN].status == record('status', last_success='2026-10-14T00:00:00Z')


def test_a_failed_status_write_leaves_no_temporary_file(tmp_path, monkeypatch):
    root = str(tmp_path)

    def broken(source, target):
        raise OSError('disk full')
    monkeypatch.setattr(sf._plat, 'replace', broken)
    with pytest.raises(OSError, match='disk full'):
        sf.write_status(root, OWN, STATUS)
    assert os.listdir(os.path.join(root, 'machines', OWN)) == []

    def no_space(path, data):
        raise OSError('no space')
    monkeypatch.setattr(sf, '_write_new', no_space)
    with pytest.raises(OSError, match='no space'):
        sf.write_status(root, OWN, STATUS)


def test_a_status_with_a_value_outside_the_format_writes_nothing(tmp_path):
    with pytest.raises(sf.FormatError):
        sf.write_status(str(tmp_path), OWN, dict(STATUS, tool_version='1.6.0\nhost'))
    assert os.listdir(os.path.join(str(tmp_path), 'machines', OWN)) == []


# --- Reading ----------------------------------------------------------------------------------------------------------

def test_the_reader_skips_the_own_machine_and_a_missing_repository_is_empty(tmp_path):
    assert sf.read_repo(str(tmp_path / 'nothing')) == sf.Repo({}, dict.fromkeys(sf.COUNTERS, 0))
    for machine_id in (OWN, OTHER):
        sf.write_status(str(tmp_path), machine_id, STATUS)
    assert list(sf.read_repo(str(tmp_path), skip=OWN).machines) == [OTHER]


@pytest.mark.posix_only
def test_the_reader_skips_and_counts_a_symlink(tmp_path):
    root = str(tmp_path / 'repo')
    outside = write_file(str(tmp_path / 'outside.jsonl'), sf.encode('history', HISTORY))
    machine_file(root, OTHER, 'history/2026-09.jsonl', sf.encode('history', HISTORY))
    os.symlink(outside, os.path.join(root, 'machines', OTHER, 'history', '2026-10.jsonl'))
    os.symlink(os.path.dirname(outside), os.path.join(root, 'machines', OTHER, 'gates'))
    os.symlink(os.path.dirname(outside), os.path.join(root, 'machines', OWN))
    repo = sf.read_repo(root)
    assert repo.skipped['symlinks'] == 3
    assert list(repo.machines) == [OTHER] and repo.machines[OTHER].history == [record('history')]


@pytest.mark.posix_only
def test_the_reader_skips_a_link_as_the_machines_folder(tmp_path):
    root = str(tmp_path / 'repo')
    machine_file(str(tmp_path / 'outside'), OTHER, 'status.json', sf.encode('status', STATUS))
    os.makedirs(root)
    os.symlink(str(tmp_path / 'outside' / 'machines'), os.path.join(root, 'machines'))
    repo = sf.read_repo(root)
    assert repo.machines == {} and repo.skipped['symlinks'] == 1


def test_the_reader_skips_and_counts_a_file_over_8_mb(tmp_path):
    root = str(tmp_path)
    path = machine_file(root, OTHER, 'history/2026-10.jsonl', sf.encode('history', HISTORY))
    with open(path, 'r+b') as f:
        f.truncate(sf.MAX_FILE_BYTES + 1)
    machine_file(root, OTHER, 'history/2026-09.jsonl', sf.encode('history', HISTORY))
    status = machine_file(root, OTHER, 'status.json', sf.encode('status', STATUS))
    with open(status, 'r+b') as f:
        f.truncate(sf.MAX_FILE_BYTES + 1)
    repo = sf.read_repo(root)
    assert repo.skipped['oversized'] == 2 and repo.machines[OTHER].history == [record('history')]
    assert repo.machines[OTHER].status is None


def test_a_file_of_exactly_8_mb_is_read(tmp_path):
    line = sf.encode('history', HISTORY).encode('utf-8')
    content = line + b' ' * (sf.MAX_FILE_BYTES - len(line))
    machine_file(str(tmp_path), OTHER, 'history/2026-10.jsonl', content)
    repo = sf.read_repo(str(tmp_path))
    assert repo.skipped['oversized'] == 0 and repo.machines[OTHER].history == [record('history')]


def test_the_reader_skips_and_counts_a_line_of_an_unknown_version(tmp_path):
    lines = [sf.encode('request', REQUEST), json.dumps(dict(record('request'), v=2)) + '\n',
             json.dumps(dict(record('request'), v=0)) + '\n']
    machine_file(str(tmp_path), OTHER, f'requests/2026-10/{SESSION}.jsonl', ''.join(lines))
    machine_file(str(tmp_path), OTHER, 'status.json', json.dumps(dict(record('status'), v=3)))
    repo = sf.read_repo(str(tmp_path))
    assert repo.skipped['unknown_version'] == 3
    assert repo.machines[OTHER].requests == [record('request')] and repo.machines[OTHER].status is None


def test_the_reader_skips_and_counts_broken_json_and_lines_that_do_not_fit(tmp_path):
    lines = [b'{"v":1,"t":', b'[1, 2]', b'{"v":"1"}', json.dumps(dict(record('gate'), hook='x')).encode(),
             b'\xff\xfe{}', b'', b'   ', sf.encode('gate', GATE).encode().strip(),
             b'[' * 100_000 + b']' * 100_000]
    machine_file(str(tmp_path), OTHER, 'gates/2026-10.jsonl', b'\n'.join(lines))
    machine_file(str(tmp_path), OTHER, 'machine.json', '{"v":1,')
    repo = sf.read_repo(str(tmp_path))
    assert repo.skipped['broken_lines'] == 7
    assert repo.machines[OTHER].gates == [record('gate')] and repo.machines[OTHER].machine is None


def test_the_reader_keeps_only_the_fields_of_the_format(tmp_path):
    line = json.dumps(dict(record('request'), prompt='secret text', path='/home/x')) + '\n'
    machine_file(str(tmp_path), OTHER, f'requests/2026-10/{SESSION}.jsonl', line)
    assert sf.read_repo(str(tmp_path)).machines[OTHER].requests == [record('request')]


def test_the_reader_ignores_executable_bits_and_never_runs_anything(tmp_path):
    marker = str(tmp_path / 'ran')
    path = machine_file(str(tmp_path / 'repo'), OTHER, 'history/2026-10.jsonl',
                        f'#!/bin/sh\ntouch {marker}\n' + sf.encode('history', HISTORY))
    os.chmod(path, 0o755)
    repo = sf.read_repo(str(tmp_path / 'repo'))
    assert repo.machines[OTHER].history == [record('history')] and not os.path.exists(marker)


def test_the_reader_skips_and_counts_names_outside_the_layout(tmp_path):
    root = str(tmp_path)
    for name in ('not-a-uuid', OTHER[:-1] + 'g', '..x', '11111111-2222-1333-8444-555555555555'):
        os.makedirs(os.path.join(root, 'machines', name))
    write_file(os.path.join(root, 'machines', 'README.md'), 'hello')
    for relative in ('notes.txt', 'history/2026-13.jsonl', 'history/2026-10.json', 'gates/x.jsonl',
                     'days/26.jsonl', f'requests/2026-10/{SESSION}.txt', 'requests/2026-10/a b.jsonl',
                     'requests/october/x.jsonl', 'requests/2026-10.jsonl'):
        machine_file(root, OTHER, relative, sf.encode('history', HISTORY))
    os.makedirs(os.path.join(root, 'machines', OTHER, 'machine.json'))
    os.makedirs(os.path.join(root, 'machines', OTHER, 'days', '2026.jsonl'))
    repo = sf.read_repo(root)
    assert list(repo.machines) == [OTHER]
    assert repo.skipped['bad_names'] == 5 + 11
    machine = repo.machines[OTHER]
    assert (machine.history, machine.gates, machine.days, machine.requests) == ([], [], [], [])


def test_the_reader_skips_a_file_as_the_machines_folder(tmp_path):
    write_file(os.path.join(str(tmp_path), 'machines'), '')
    repo = sf.read_repo(str(tmp_path))
    assert repo.machines == {} and repo.skipped['irregular'] == 1


@pytest.mark.posix_only
def test_the_reader_skips_and_counts_a_fifo(tmp_path):
    folder = os.path.join(str(tmp_path), 'machines', OTHER, 'history')
    os.makedirs(folder)
    os.mkfifo(os.path.join(folder, '2026-10.jsonl'))
    repo = sf.read_repo(str(tmp_path))
    assert repo.skipped['irregular'] == 1 and repo.machines[OTHER].history == []


def test_the_reader_counts_folders_and_files_it_cannot_read(tmp_path, monkeypatch):
    root = str(tmp_path)
    machine_file(root, OTHER, 'history/2026-10.jsonl', sf.encode('history', HISTORY))
    machine_file(root, OTHER, 'gates/2026-10.jsonl', sf.encode('gate', GATE))
    machine_file(root, OTHER, 'days/2026.jsonl', sf.encode_day(DAY))
    real_listdir, real_lstat, real_open = os.listdir, os.lstat, os.open

    def listdir(path):
        if os.path.basename(path) == 'history':
            raise PermissionError(path)
        return real_listdir(path)

    def lstat(path, *args, **kwargs):
        if os.path.basename(path) == 'gates':
            raise PermissionError(path)
        return real_lstat(path, *args, **kwargs)

    def open_(path, *args, **kwargs):
        if str(path).endswith('2026.jsonl'):
            raise PermissionError(path)
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(sf.os, 'listdir', listdir)
    monkeypatch.setattr(sf.os, 'lstat', lstat)
    monkeypatch.setattr(sf.os, 'open', open_)
    repo = sf.read_repo(root)
    assert repo.skipped['unreadable'] == 3
    assert (repo.machines[OTHER].history, repo.machines[OTHER].gates, repo.machines[OTHER].days) == ([], [], [])


def test_the_reader_never_reads_a_file_that_resolves_outside_the_root(tmp_path, monkeypatch):
    root = str(tmp_path / 'repo')
    path = machine_file(root, OTHER, 'history/2026-10.jsonl', sf.encode('history', HISTORY))
    real_realpath = os.path.realpath

    def moved(name):        # as if a folder on the way had been swapped for a link after it was checked
        return str(tmp_path / 'elsewhere') if name == path else real_realpath(name)
    monkeypatch.setattr(sf.os.path, 'realpath', moved)
    repo = sf.read_repo(root)
    assert repo.machines[OTHER].history == [] and repo.skipped['bad_names'] == 1


def test_a_file_that_changed_after_its_lstat_is_skipped(tmp_path, monkeypatch):
    skipped = dict.fromkeys(sf.COUNTERS, 0)
    small = write_file(str(tmp_path / 'small.jsonl'), 'x')
    big = write_file(str(tmp_path / 'big.jsonl'), b'x' * (sf.MAX_FILE_BYTES + 1))
    folder = str(tmp_path / 'folder')
    os.makedirs(folder)
    info = os.lstat(small)
    assert sf._file_content(str(tmp_path), big, info, skipped) is None
    assert sf._file_content(str(tmp_path), folder, info, skipped) is None      # Windows: not even opened
    assert skipped['oversized'] == 1

    def broken(descriptor, size):
        raise OSError('read error')
    monkeypatch.setattr(sf.os, 'read', broken)
    assert sf._file_content(str(tmp_path), small, info, skipped) is None
    assert skipped['unreadable'] >= 1


@pytest.mark.posix_only
def test_a_file_replaced_by_a_fifo_after_its_lstat_is_skipped(tmp_path):
    skipped = dict.fromkeys(sf.COUNTERS, 0)
    info = os.lstat(write_file(str(tmp_path / 'small.jsonl'), 'x'))
    fifo = str(tmp_path / 'fifo')
    os.mkfifo(fifo)
    assert sf._file_content(str(tmp_path), fifo, info, skipped) is None
    assert skipped['irregular'] == 1


def test_of_the_lines_with_one_key_the_one_read_last_counts():
    first, second, other = (request('2026-10-13T12:30:00Z', '0' * 16), request('2026-10-13T12:31:00Z', '0' * 16),
                            request('2026-10-13T12:32:00Z', '1' * 16))
    assert sf.latest_by_key([first, other, second]) == {'0' * 16: second, '1' * 16: other}


# --- Compacting -----------------------------------------------------------------------------------------------------

def requests_in(root, machine_id, days, session=SESSION):
    for number, day in enumerate(days):
        t = f'{day}T12:00:00Z'
        sf.append(root, machine_id, sf.request_path(machine_id, t, session),
                  [sf.encode('request', request(t, f'{number:016x}', session))])


def day_lines(root, machine_id, days):
    sf.append(root, machine_id, sf.day_path(machine_id, days[0]),
              [sf.encode_day(json.dumps({'v': 1, 'day': day, 'stats': {'requests': 1}})) for day in days])


def month_folders(root, machine_id):
    folder = os.path.join(root, 'machines', machine_id, 'requests')
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


def test_compacting_removes_only_own_months_beyond_the_limit_and_keeps_every_day_line(tmp_path):
    root = str(tmp_path)
    days = ['2026-05-10', '2026-06-10', '2026-06-20', '2026-07-01', '2026-09-30', '2026-10-19']
    for machine_id in (OWN, OTHER):
        requests_in(root, machine_id, days)
        day_lines(root, machine_id, days[:-1])
    days_file = os.path.join(root, 'machines', OWN, 'days', '2026.jsonl')
    with open(days_file, 'rb') as f:
        before = f.read()
    assert sf.old_months(root, OWN, 3, NOW) == ['2026-05', '2026-06']
    assert sf.compactable_months(root, OWN, 3, NOW) == ['2026-05', '2026-06']
    assert sf.compact(root, OWN, 3, NOW) == ['2026-05', '2026-06']
    assert month_folders(root, OWN) == ['2026-07', '2026-09', '2026-10']
    assert month_folders(root, OTHER) == ['2026-05', '2026-06', '2026-07', '2026-09', '2026-10']
    with open(days_file, 'rb') as f:
        assert f.read() == before
    assert sf.compact(root, OWN, 3, NOW) == []


def test_keep_request_months_counts_full_months_before_the_current_one(tmp_path):
    root = str(tmp_path)
    days = ['2026-06-30', '2026-07-01', '2026-08-15', '2026-09-15', '2026-10-01']
    requests_in(root, OWN, days)
    day_lines(root, OWN, days)
    assert sf.old_months(root, OWN, 1, NOW) == ['2026-06', '2026-07', '2026-08']
    assert sf.old_months(root, OWN, 4, NOW) == []
    assert sf.compact(root, OWN, 2, NOW) == ['2026-06', '2026-07']


def test_a_month_with_a_request_day_without_a_day_line_stays(tmp_path):
    root = str(tmp_path)
    requests_in(root, OWN, ['2026-05-10', '2026-05-11', '2026-06-10'])
    day_lines(root, OWN, ['2026-05-10', '2026-06-10'])
    assert sf.missing_days(root, OWN, '2026-05') == {'2026-05-11'}
    assert sf.missing_days(root, OWN, '2026-06') == set()
    assert sf.compactable_months(root, OWN, 3, NOW) == ['2026-06']
    assert sf.compact(root, OWN, 3, NOW) == ['2026-06']
    assert month_folders(root, OWN) == ['2026-05']


def test_a_month_without_any_day_lines_stays(tmp_path):
    root = str(tmp_path)
    requests_in(root, OWN, ['2026-05-10'])
    assert sf.missing_days(root, OWN, '2026-05') == {'2026-05-10'}
    assert sf.compact(root, OWN, 3, NOW) == []


@pytest.mark.parametrize('trouble', ['broken line', 'unknown version', 'odd name', 'folder inside'])
def test_a_month_that_cannot_be_judged_completely_stays(tmp_path, trouble):
    root = str(tmp_path)
    requests_in(root, OWN, ['2026-05-10'])
    day_lines(root, OWN, ['2026-05-10'])
    month = os.path.join(root, 'machines', OWN, 'requests', '2026-05')
    if trouble == 'broken line':
        sf.append(root, OWN, sf.request_path(OWN, '2026-05-11T12:00:00Z', 'b'), ['{"v":1,"t":"2026-05-11T\n'])
    elif trouble == 'unknown version':
        sf.append(root, OWN, sf.request_path(OWN, '2026-05-11T12:00:00Z', 'b'), ['{"v":2}\n'])
    elif trouble == 'odd name':
        write_file(os.path.join(month, 'notes.txt'), '2026-05-11')
    else:
        os.makedirs(os.path.join(month, 'more'))
    assert sf.missing_days(root, OWN, '2026-05') is None
    assert sf.compact(root, OWN, 3, NOW) == [] and month_folders(root, OWN) == ['2026-05']


@pytest.mark.posix_only
def test_a_month_with_a_link_in_it_stays(tmp_path):
    root = str(tmp_path / 'repo')
    os.makedirs(root)
    requests_in(root, OWN, ['2026-05-10'])
    day_lines(root, OWN, ['2026-05-10'])
    target = write_file(str(tmp_path / 'outside.jsonl'), 'keep me')
    os.symlink(target, os.path.join(root, 'machines', OWN, 'requests', '2026-05', 'b.jsonl'))
    assert sf.compact(root, OWN, 3, NOW) == []
    with open(target, encoding='utf-8') as f:
        assert f.read() == 'keep me'


def test_compacting_without_requests_does_nothing(tmp_path):
    root = str(tmp_path)
    assert sf.compact(root, OWN, 3, NOW) == []
    sf.write_status(root, OWN, STATUS)
    assert sf.old_months(root, OWN, 3, NOW) == []


def test_compacting_a_month_that_is_not_a_month_is_refused(tmp_path):
    requests_in(str(tmp_path), OWN, ['2026-05-10'])
    with pytest.raises(sf.FormatError):
        sf.missing_days(str(tmp_path), OWN, '../2026-05')


def test_the_day_of_a_request_is_its_local_day():
    from datetime import datetime
    t = '2026-05-10T23:30:00Z'
    assert sf.local_day(t) == datetime.fromtimestamp(sf.parse_time(t)).date().isoformat()


# --- The sync keys of the configuration -----------------------------------------------------------------------------

def load_config(tmp_path, monkeypatch, sync):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'sync': sync}), encoding='utf-8')
    monkeypatch.setenv('CCUC_CONFIG', str(path))
    return _config.load()


def logged(tmp_path):
    path = tmp_path / 'log' / 'log.jsonl'
    return path.read_text(encoding='utf-8') if path.exists() else ''


def test_the_sync_defaults(tmp_path, monkeypatch):
    assert _config.load()['sync'] == {'machine_name': '', 'project_labels': 'plain', 'project_aliases': {},
                                      'label_salt': '', 'keep_request_months': 3}


def test_valid_sync_values_are_taken_over(tmp_path, monkeypatch):
    sync = {'machine_name': 'Büro-PC 2', 'project_labels': 'alias', 'project_aliases': {'webshop': 'shop'},
            'label_salt': 'pepper', 'keep_request_months': 6.0}
    cfg = load_config(tmp_path, monkeypatch, sync)
    assert cfg['sync'] == dict(sync, keep_request_months=6)
    assert isinstance(cfg['sync']['keep_request_months'], int) and logged(tmp_path) == ''


@pytest.mark.parametrize('key, value', [
    ('project_labels', 'secret'), ('project_labels', 1), ('project_aliases', ['webshop']),
    ('project_aliases', {'webshop': 1}), ('project_aliases', 'shop'), ('label_salt', 5), ('keep_request_months', 0),
    ('keep_request_months', 2.5), ('keep_request_months', float('inf')), ('keep_request_months', '3'),
    ('keep_request_months', True), ('machine_name', 'a\nb'), ('machine_name', 'x' * 65), ('machine_name', 'a/b'),
    ('machine_name', 3),
])
def test_a_broken_sync_value_falls_back_to_its_default_with_one_log_line(tmp_path, monkeypatch, key, value):
    cfg = load_config(tmp_path, monkeypatch, {key: value})
    assert cfg['sync'] == _config.DEFAULTS['sync']
    assert f'sync.{key}' in logged(tmp_path) and logged(tmp_path).count('\n') == 1


# --- The documentation --------------------------------------------------------------------------------------------

def test_the_report_format_describes_every_field_and_file_of_the_sync_repository():
    with open(os.path.join(ROOT, 'docs', 'report-format.md'), encoding='utf-8') as f:
        text = f.read()
    start = text.index('## The sync repository')
    following = re.search(r'^## ', text[start + 3:], re.MULTILINE)
    section = text[start:start + 3 + following.start()] if following else text[start:]
    described = set(re.findall(r'`([^`]+)`', re.sub(r'```.*?```', '', section, flags=re.DOTALL)))
    fields = {name for kind in sf.FIELDS.values() for name in kind} | {'v', 'day', 'stats'}
    assert fields - described == set()
    layout = {'machine.json', 'status.json', 'requests/<YYYY-MM>/<session_id>.jsonl', 'history/<YYYY-MM>.jsonl',
              'gates/<YYYY-MM>.jsonl', 'days/<YYYY>.jsonl', 'sync.project_labels', 'sync.keep_request_months'}
    assert layout - described == set()
    for counter in sf.COUNTERS:
        assert f'`{counter}`' in section


def test_the_architecture_names_the_module():
    with open(os.path.join(ROOT, 'docs', 'architecture.md'), encoding='utf-8') as f:
        assert 'sync_format.py' in f.read()
