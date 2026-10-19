"""The output of tools/usage_report.py, byte for byte, against saved output (tests/golden/). One invented day of
transcripts that touches every rule of reading them: the last line of a request, the agent type of a subagent, the first
request of a transcript, links and old files that are not read, lines that cannot be requests. The clock, the time zone,
the modification times and the prices are fixed (tests/golden/prices.json, not tools/prices.json: new prices are no
change of the output). A difference is a change of the report: if it is meant, the golden files change with it."""
import json
import os

import pytest

from conftest import ROOT
from test_usage_report import (NOW, SECRET, T, extras, local_zone, report_module, req, row, usage_line,  # noqa: F401
                               world)

GOLDEN = os.path.join(ROOT, 'tests', 'golden')
DAY = '2026-10-13'
CWD_A, CWD_B = '/golden-nowhere/u/proj-a', '/golden-nowhere/u/proj-b'      # folders that do not exist: no estimate
SESSION_A = 'aaaaaaaa-1111-2222-3333-444444444444'
SESSION_B = 'bbbbbbbb-1111-2222-3333-444444444444'
SESSION_C = 'cccccccc-1111-2222-3333-444444444444'
OUTPUTS = {'overview.txt': ('--day', DAY), 'full.txt': ('--day', DAY, '--full'),
           'report.json': ('--day', DAY, '--json'), 'project.txt': ('--day', DAY, '--project', 'proj-a'),
           'session.txt': ('--day', DAY, '--session', 'aaaaaaaa')}


def a(ts, message, request, **tokens):
    return req(ts, message, request=request, cwd=CWD_A, text=SECRET, **tokens)


def b(ts, message, request, **tokens):
    return req(ts, message, request=request, cwd=CWD_B, text=SECRET, **tokens)


def modified(path, ts):
    os.utime(path, (T(ts), T(ts)))


def build(world, with_link=False):
    """The transcripts, the measurement history and the gate log of the day."""
    main = world.transcript('-golden-u-proj-a', SESSION_A, [
        usage_line('2026-10-13T06:00:00Z', 2, 20), usage_line('2026-10-13T06:10:00Z', 4, 20),      # before the history
        # three lines of one request: the last counts
        a('2026-10-13T06:01:00Z', 'a1', 'ra1', inp=10, cw=60_000, out=1),
        a('2026-10-13T06:01:01Z', 'a1', 'ra1', inp=10, cw=60_000, out=5),
        extras(a('2026-10-13T06:01:02Z', 'a1', 'ra1', inp=10, cw=60_000, out=20_000), thinking=4_000),
        # the same output twice: the later line counts
        a('2026-10-13T06:05:00Z', 'a2', 'ra2', inp=3, cr=40_000, out=7_000),
        a('2026-10-13T06:05:01Z', 'a2', 'ra2', inp=4, cr=40_000, out=7_000),
        # no message id: each line is a request
        a('2026-10-13T06:07:00Z', None, 'ra3', inp=2, cr=50_000, out=3_000),
        a('2026-10-13T06:07:30Z', None, 'ra3', inp=2, cr=50_000, out=4_000),
        '{"type": "assistant", "message": {"usage": {"output_tokens": 5}',      # cut off: not a request, not counted
        {**a('2026-10-13T06:08:00Z', 'a4', 'ra4', out=9), 'requestId': 123},      # a request id, but no text: counted
        a('2026-10-13T06:09:00Z', 'a5', 'ra5', out=10 ** 13),                   # an impossible number: counted
        {'type': 'user', 'timestamp': '2026-10-13T06:09:30Z', 'message': {'content': SECRET}},
        a('2026-10-13T08:05:00Z', 'a6', 'ra6', model='claude-mystery-1', inp=500, out=900),      # no price
        extras(a('2026-10-13T08:20:00Z', 'a7', 'ra7', inp=20, cr=120_000, out=30_000), searches=2, fetches=1),
        a('2026-10-13T09:15:00Z', 'a8', 'ra8', model='claude-sonnet-5-5', cw1=70_000, cr=80_000, out=12_000),   # cold
        a('2026-10-13T11:40:00Z', 'a9', 'ra9', model='claude-haiku-5-5', inp=100_001, out=2_000),     # long context
        a('2026-10-13T12:30:00Z', 'a10', 'ra10', cr=150_000, out=25_000),
        a('2026-10-13T14:10:00Z', 'a11', 'ra11', cr=160_000, out=40_000)])
    modified(main, '2026-10-13T14:20:00Z')
    agents = [('s1', {'spawnedAgentType': 'arbeiter-bauen', 'customAgentType': 'python-meister', 'agentType': 'x'}),
              ('s2', {'customAgentType': 'python-meister', 'agentType': 'general-purpose'}),
              ('s3', {'agentType': 'Explore'}),
              ('s4', {'agentType': 'helper', 'name': 'helper'}),        # a named subagent: agentType is only its name
              ('s5', {'spawnedAgentType': 'no spaces allowed'}),        # not a type name
              ('s6', None),                                             # no meta file
              ('s7', '{broken')]                                        # a meta file that is no JSON
    for number, (agent, meta) in enumerate(agents):
        minute = 8 * number
        path = world.transcript('-golden-u-proj-a', SESSION_A, [
            a(f'2026-10-13T09:{minute:02d}:00Z', f'{agent}-1', f'r{agent}-1', inp=5 + number,
              cw=20_000 + 1_000 * number, out=2_000),
            a(f'2026-10-13T09:{minute + 4:02d}:00Z', f'{agent}-2', f'r{agent}-2', cr=25_000,
              out=3_000 + 500 * number)],
            sub=agent, meta=meta if isinstance(meta, dict) else None)
        if isinstance(meta, str):
            path.with_name(f'agent-{agent}.meta.json').write_text(meta, encoding='utf-8')
        modified(path, f'2026-10-13T10:{minute:02d}:00Z')
    shared = b('2026-10-13T10:30:00Z', 'shared', 'r-shared', cr=90_000, out=10_000)
    same = b('2026-10-13T10:40:00Z', 'same', 'r-same', cr=90_000, out=6_000)
    first = world.transcript('-golden-u-proj-b', SESSION_B, [
        b('2026-10-13T10:00:00Z', 'b1', 'rb1', cw=80_000, out=15_000), shared, same,
        b('2026-10-13T12:50:00Z', 'b2', 'rb2', cr=100_000, out=22_000)])
    modified(first, '2026-10-13T13:00:00Z')
    # continued in a new transcript: the shared lines stand there again, one of them with more output
    later = world.transcript('-golden-u-proj-a', SESSION_C, [
        {**shared, 'message': {**shared['message'], 'usage': {**shared['message']['usage'], 'output_tokens': 18_000}}},
        same, a('2026-10-13T13:30:00Z', 'c1', 'rc1', cr=110_000, out=9_000)])
    modified(later, '2026-10-13T13:40:00Z')
    old = world.transcript('-golden-u-proj-a', 'dddddddd-1', [a('2026-10-13T07:00:00Z', 'd1', 'rd1', out=50_000)])
    modified(old, '2026-10-12T20:00:00Z')        # not modified since the period began (minus an hour): not opened
    (world.projects / 'loose.jsonl').write_text(json.dumps(a('2026-10-13T07:00:00Z', 'e1', 're1', out=1)) + '\n',
                                                encoding='utf-8')         # not in a project folder
    notes = json.dumps(a('2026-10-13T07:00:00Z', 'f1', 'rf1', out=1))
    (world.projects / '-golden-u-proj-a' / 'notes.txt').write_text(notes, encoding='utf-8')      # not a transcript
    if with_link:
        os.symlink(first, world.projects / '-golden-u-proj-a' / 'eeeeeeee-link.jsonl')
        os.symlink(world.projects / '-golden-u-proj-b', world.projects / '-golden-u-proj-link')
    rows = []
    for step in range(43):           # every 10 minutes from 08:00 to 15:00, a new 5h window at 12:00
        ts = T('2026-10-13T08:00:00Z') + 600 * step
        stamp = report_module.iso(ts)
        five = 10 + 2 * step if step < 24 else 1 + step - 24
        resets = T('2026-10-13T12:00:00Z') if step < 24 else T('2026-10-13T17:00:00Z')
        rows.append(row(stamp, five, 21 + step // 2, resets, T('2026-10-19T07:00:00Z')))
    world.history(rows)
    world.guard_log([{'time': '2026-10-13T09:00:00Z', 'hook': 'spawn_gate', 'mode': 'enforce', 'decision': 'allow'},
                     {'time': '2026-10-13T09:30:00Z', 'hook': 'spawn_gate', 'mode': 'enforce', 'decision': 'deny',
                      'reason': 'week at 92 %: start later'},
                     {'time': '2026-10-13T10:00:00Z', 'hook': 'soft_stop', 'mode': 'shadow', 'decision': 'warn',
                      'reason': '5h window at 81 %: checkpoint'}])


def compare_golden(name, actual):
    with open(os.path.join(GOLDEN, name), 'rb') as f:
        expected = f.read()
    assert actual == expected.decode('utf-8'), name


def outputs(world, monkeypatch):
    monkeypatch.setattr(report_module, 'PRICES_FILE', os.path.join(GOLDEN, 'prices.json'))
    return {name: world.overview(*args, now=NOW) for name, args in OUTPUTS.items()}


def test_the_report_prints_what_it_printed_before(world, monkeypatch):
    build(world)
    for name, text in outputs(world, monkeypatch).items():
        compare_golden(name, text)


@pytest.mark.posix_only
def test_links_to_transcripts_change_nothing(world, monkeypatch):
    build(world, with_link=True)
    for name, text in outputs(world, monkeypatch).items():
        compare_golden(name, text)


def test_the_saved_output_covers_every_rule_of_reading(world, monkeypatch):
    """What the golden files must show, or they test less than they claim."""
    build(world)
    data = json.loads(outputs(world, monkeypatch)['report.json'])
    assert data['coverage']['transcript_files'] == 10 and data['coverage']['unreadable_transcript_lines'] == 2
    assert set(data['total']['subagents']['by_type']) == {'arbeiter-bauen', 'python-meister', 'Explore', 'unknown',
                                                          'other'}
    assert data['total']['subagents']['by_type']['unknown']['requests'] == 6         # s4, s6 and s7
    assert data['total']['unpriced'] and data['total']['web_searches'] == 2 and data['total']['cold_starts'] == 1
    assert len(data['startup']['starts']) == 10 and data['allocation']['five']['counted_points'] > 0
    by_id = {cli['id']: cli for cli in data['clis']}
    assert by_id['bbbbbbbb']['requests'] == 3 and by_id['cccccccc']['requests'] == 2     # one shared request each
    assert by_id['cccccccc']['tokens']['output'] == 18_000 + 9_000
    subagents = sum(5 + number for number in range(7))
    assert by_id['aaaaaaaa']['tokens']['input'] == 10 + 4 + 2 + 2 + 500 + 20 + 100_001 + subagents
