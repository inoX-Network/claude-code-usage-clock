"""Tests for tools/usage_report.py. Invented transcripts, histories and logs (October 2026); the clock and the time zone
are fixed (a European zone with daylight saving time), nothing here reads the real home directory."""
import builtins
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from conftest import ROOT

SCRIPT = os.path.join(ROOT, 'tools', 'usage_report.py')
ZONE = 'CET-1CEST,M3.5.0,M10.5.0/3'     # +1 h, +2 h in summer; the clocks go back on 2026-10-25
SECRET = 'hunter2-SECRET-4711'


def load_module():
    spec = importlib.util.spec_from_file_location('usage_report_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report_module = load_module()


def T(text):
    """Unix seconds of an ISO time with Z."""
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


NOW = T('2026-10-14T12:00:00Z')    # 14:00 local on 2026-10-14
DAY = '2026-10-13'


@pytest.fixture(autouse=True)
def local_zone():
    old = os.environ.get('TZ')
    os.environ['TZ'] = ZONE
    time.tzset()
    yield
    if old is None:
        os.environ.pop('TZ', None)
    else:
        os.environ['TZ'] = old
    time.tzset()


def req(ts, message='m1', model='claude-opus-5-5', inp=0, cw=0, cw1=0, cr=0, out=0, request='r1', cwd='/home/u/proj-a',
        text='hello'):
    """One assistant line of a transcript. cw = 5 minute cache write, cw1 = 1 hour cache write."""
    return {'type': 'assistant', 'timestamp': ts, 'requestId': request, 'cwd': cwd,
            'message': {'id': message, 'model': model, 'role': 'assistant',
                        'content': [{'type': 'text', 'text': text}],
                        'usage': {'input_tokens': inp, 'cache_creation_input_tokens': cw + cw1,
                                  'cache_read_input_tokens': cr, 'output_tokens': out,
                                  'cache_creation': {'ephemeral_5m_input_tokens': cw,
                                                     'ephemeral_1h_input_tokens': cw1}}}}


def dollars(n):
    """Output tokens of one opus-5-5 request that cost n dollars (20 USD per million)."""
    return round(n * 50_000)


def paid(ts, message, cost, **more):
    return req(ts, message=message, request='r-' + message, out=dollars(cost), **more)


def extras(line, thinking=None, searches=None, fetches=None):
    """The optional usage details of a request: thinking tokens (part of the output) and web tool calls."""
    usage = line['message']['usage']
    if thinking is not None:
        usage['output_tokens_details'] = {'thinking_tokens': thinking}
    if searches is not None or fetches is not None:
        usage['server_tool_use'] = {key: value for key, value in
                                    (('web_search_requests', searches),
                                     ('web_fetch_requests', fetches)) if value is not None}
    return line


def spend(world, *entries, session='aaaaaaaa-1', project='-home-u-proj-a'):
    """A transcript with one request per (time, dollars) pair."""
    world.transcript(project, session, [paid(ts, f'{session}-{i}', cost) for i, (ts, cost) in enumerate(entries)])


def usage_line(ts, five, week, age=0):
    text = (f'Usage: 5h {five} % (resets 14:00) · week {week} % (resets Mon 19 Oct 09:00) · measured {age} min ago '
            f'· Now: x {SECRET}')
    return {'type': 'attachment', 'timestamp': ts,
            'attachment': {'type': 'hook_additional_context', 'content': [text]}}


def row(ts, five, week, five_resets=None, week_resets=None):
    return {'t': ts, 'five': five, 'week': week, 'five_resets': five_resets, 'week_resets': week_resets}


class World:
    def __init__(self, tmp_path):
        self.projects = tmp_path / 'projects'
        self.log = tmp_path / 'log'
        self.root = tmp_path

    def transcript(self, project, session, lines, sub=None, meta=None):
        folder = self.projects / project
        if sub is None:
            path = folder / f'{session}.jsonl'
        else:
            path = folder / session / 'subagents' / f'agent-{sub}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(line if isinstance(line, str) else json.dumps(line) for line in lines) + '\n',
                        encoding='utf-8')
        stamps = [T(line['timestamp']) for line in lines if isinstance(line, dict) and isinstance(line.get('timestamp'), str)
                  and line['timestamp'].endswith('Z')]
        modified = max([time.time()] + stamps)      # a transcript is modified when its last line is written
        os.utime(path, (modified, modified))
        if meta is not None:
            path.with_name(f'agent-{sub}.meta.json').write_text(json.dumps(meta), encoding='utf-8')
        return path

    def history(self, rows, name='usage-history.jsonl'):
        self.log.mkdir(parents=True, exist_ok=True)
        (self.log / name).write_text('\n'.join(r if isinstance(r, str) else json.dumps(r) for r in rows) + '\n',
                                     encoding='utf-8')

    def guard_log(self, entries):
        self.log.mkdir(parents=True, exist_ok=True)
        (self.log / 'log.jsonl').write_text('\n'.join(json.dumps(e) for e in entries) + '\n', encoding='utf-8')

    def args(self, *args):
        return ['--projects-dir', str(self.projects), *args]

    def report(self, *args, now=NOW):
        self.projects.mkdir(exist_ok=True)
        code = report_module.main(self.args('--json', *args), now=now)
        assert code == 0
        return json.loads(self.capsys.readouterr().out)

    def text(self, *args, now=NOW):
        self.projects.mkdir(exist_ok=True)
        assert report_module.main(self.args(*args), now=now) == 0
        return self.capsys.readouterr().out


@pytest.fixture
def world(tmp_path, capsys):
    made = World(tmp_path)
    made.capsys = capsys
    return made


def day_report(world, *lines, args=(DAY,)):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1111-2222-3333-444444444444', list(lines))
    return world.report('--day', *args)


# --- Counting: one request, one line ----------------------------------------------------------------------------

def test_only_the_last_line_of_a_request_counts(world):
    data = day_report(world,
                      req('2026-10-13T10:00:00Z', 'm1', inp=10, out=1),
                      req('2026-10-13T10:00:01Z', 'm1', inp=10, out=5),
                      req('2026-10-13T10:00:02Z', 'm1', inp=10, out=20),
                      req('2026-10-13T10:01:00Z', 'm2', inp=3, out=7, request='r2'))
    assert data['total']['requests'] == 2
    assert data['total']['tokens'] == {'input': 13, 'cache_write_5m': 0, 'cache_write_1h': 0, 'cache_read': 0,
                                       'output': 27}


def test_of_two_lines_with_the_same_output_the_later_one_counts(world):
    data = day_report(world, req('2026-10-13T10:00:00Z', 'm1', inp=1, out=5),
                      req('2026-10-13T10:00:01Z', 'm1', inp=2, out=5))
    assert data['total']['tokens']['input'] == 2 and data['total']['requests'] == 1


def test_the_same_request_in_two_transcripts_counts_once(world):
    line = req('2026-10-13T10:00:00Z', 'm1', out=20)
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [line])
    world.transcript('-home-u-proj-b', 'bbbbbbbb-2', [line])
    assert world.report('--day', DAY)['total']['requests'] == 1


def test_the_same_message_in_two_requests_counts_twice(world):
    data = day_report(world, req('2026-10-13T10:00:00Z', 'm1', out=1, request='r1'),
                      req('2026-10-13T10:00:01Z', 'm1', out=1, request='r2'))
    assert data['total']['requests'] == 2


def test_lines_without_a_message_id_count_one_by_one(world):
    first, second = req('2026-10-13T10:00:00Z', None, out=3), req('2026-10-13T10:00:01Z', None, out=4)
    data = day_report(world, first, second)
    assert data['total']['requests'] == 2 and data['total']['tokens']['output'] == 7


def test_cost_of_every_kind_of_token(world):
    data = day_report(world, req('2026-10-13T10:00:00Z', inp=1_000_000, cw=1_000_000, cw1=1_000_000, cr=1_000_000,
                                 out=1_000_000))
    # Opus 5.5: 4 + 5 + 8 + 0.20 + 20 USD per million tokens
    assert data['total']['cost_usd'] == pytest.approx(37.2)
    assert data['total']['tokens'] == {'input': 1_000_000, 'cache_write_5m': 1_000_000, 'cache_write_1h': 1_000_000,
                                       'cache_read': 1_000_000, 'output': 1_000_000}
    opus = data['total']['models']['Opus 5.5']
    assert (opus['requests'], opus['cost_usd']) == (1,
                                                    pytest.approx(37.2)) and opus['tokens'] == data['total']['tokens']
    assert list(data['total']['models']) == ['Opus 5.5']


@pytest.mark.parametrize('model, price', [
    ('claude-opus-4-5-20251101', 5 + 6.25 + 10 + 0.5 + 25),
    ('claude-sonnet-5-5', 2 + 2.5 + 4 + 0.2 + 10),
    ('claude-sonnet-5', 2 + 2.5 + 4 + 0.2 + 10),
    ('claude-haiku-4-5-20251001', 1 + 1.25 + 2 + 0.1 + 5),
    ('claude-fable-5-1', 10 + 12.5 + 20 + 0.25 + 50),
])
def test_prices_per_model(world, model, price):
    # one million tokens of each kind would be in the long-context tier of Haiku 5.5 only
    data = day_report(world, req('2026-10-13T10:00:00Z', model=model, inp=1_000_000, cw=1_000_000, cw1=1_000_000,
                                 cr=1_000_000, out=1_000_000))
    assert data['total']['cost_usd'] == pytest.approx(price)


def test_haiku_55_costs_more_above_100k_of_context(world):
    data = day_report(world,
                      req('2026-10-13T10:00:00Z', 'low', model='claude-haiku-5-5', inp=100_000, out=1_000_000,
                          request='a'),
                      req('2026-10-13T10:01:00Z', 'high', model='claude-haiku-5-5', inp=100_001, out=1_000_000,
                          request='b'))
    low = 100_000 * 0.10 / 1e6 + 0.50          # exactly 100k is still the low tier
    high = 100_001 * 0.50 / 1e6 + 2.50
    assert data['total']['cost_usd'] == pytest.approx(low + high)


def test_the_context_of_the_tier_counts_cache_tokens_too(world):
    data = day_report(world, req('2026-10-13T10:00:00Z', model='claude-haiku-5-5', cr=60_000, cw1=60_000, out=0))
    assert data['total']['cost_usd'] == pytest.approx(60_000 * 0.05 / 1e6 + 60_000 * 1.0 / 1e6)


def test_a_model_without_a_price_is_shown_with_its_tokens_and_never_estimated(world):
    data = day_report(world, req('2026-10-13T10:00:00Z', 'm1', model='claude-mystery-9', inp=1000, out=500),
                      paid('2026-10-13T10:01:00Z', 'm2', 1.0))
    assert data['total']['cost_usd'] == pytest.approx(1.0)
    assert list(data['total']['unpriced']) == ['claude-mystery-9']
    mystery = data['total']['unpriced']['claude-mystery-9']
    assert mystery['requests'] == 1 and mystery['tokens']['input'] == 1000 and mystery['tokens']['output'] == 500
    assert data['clis'][0]['cost_usd'] == pytest.approx(1.0)
    assert data['clis'][0]['tokens']['input'] == 1000
    assert 'no price' in world.text('--day', DAY) and 'claude-mystery-9' in world.text('--day', DAY)


def test_strange_token_numbers_count_as_zero(world):
    line = req('2026-10-13T10:00:00Z', 'm1', inp=5, out=2)
    line['message']['usage'].update(input_tokens='many', cache_read_input_tokens=-4, cache_creation_input_tokens=True)
    line['message']['usage']['cache_creation'] = 'broken'
    data = day_report(world, line)
    assert data['total']['tokens'] == {'input': 0, 'cache_write_5m': 0, 'cache_write_1h': 0, 'cache_read': 0,
                                       'output': 2}


def test_a_line_without_any_tokens_is_no_request(world):
    data = day_report(world, req('2026-10-13T10:00:00Z', model='<synthetic>'), req('2026-10-13T10:01:00Z', 'm2', out=1))
    assert data['total']['requests'] == 1 and data['total']['unpriced'] == {}


# --- Thinking tokens, web tools, cache ----------------------------------------------------------------------------------

def test_thinking_tokens_and_web_calls_are_summed_per_period_model_and_cli(world):
    data = day_report(world,
                      extras(req('2026-10-13T10:00:00Z', 'm1', out=400, request='r1'), thinking=100, searches=2,
                             fetches=1),
                      extras(req('2026-10-13T10:01:00Z', 'm2', out=100, request='r2'), thinking=50, searches=1,
                             fetches=0),
                      # an older transcript: no details at all
                      req('2026-10-13T10:02:00Z', 'm3', out=200, request='r3'))
    for block in (data['total'], data['total']['models']['Opus 5.5'], data['clis'][0]):
        assert block['thinking_tokens'] == 150 and block['web_searches'] == 3 and block['web_fetches'] == 1
        assert block['thinking_share'] == pytest.approx(150 / 500)       # of the output of the requests that report it
    assert data['total']['tokens']['output'] == 700


def test_a_field_that_is_missing_everywhere_is_unknown_not_zero(world):
    data = day_report(world, req('2026-10-13T10:00:00Z', 'm1', out=400))
    for block in (data['total'], data['total']['models']['Opus 5.5'], data['clis'][0]):
        assert (block['thinking_tokens'], block['thinking_share'], block['web_searches'],
                block['web_fetches']) == (None,) * 4
    text = world.text('--day', DAY)
    assert 'thinking n/a' in text and 'web searches n/a' in text and 'web fetches n/a' in text


def test_a_reported_zero_is_a_zero(world):
    data = day_report(world, extras(req('2026-10-13T10:00:00Z', 'm1', out=400), thinking=0, searches=0, fetches=0))
    assert (data['total']['thinking_tokens'], data['total']['thinking_share']) == (0, 0.0)
    assert (data['total']['web_searches'], data['total']['web_fetches']) == (0, 0)
    assert 'thinking 0 (0 %)' in world.text('--day', DAY)


def test_the_fields_are_known_one_by_one(world):
    data = day_report(world, extras(req('2026-10-13T10:00:00Z', 'm1', out=400), searches=4))
    assert (data['total']['thinking_tokens'], data['total']['web_searches'], data['total']['web_fetches']) == (None, 4,
                                                                                                               None)


@pytest.mark.parametrize('bad', ['many', -3, True, None, [1]])
def test_a_strange_value_is_unknown(world, bad):
    line = req('2026-10-13T10:00:00Z', 'm1', out=400)
    line['message']['usage']['output_tokens_details'] = {'thinking_tokens': bad}
    line['message']['usage']['server_tool_use'] = {'web_search_requests': bad, 'web_fetch_requests': bad}
    data = day_report(world, line)
    assert (data['total']['thinking_tokens'], data['total']['web_searches'], data['total']['web_fetches']) == (None,
                                                                                                               ) * 3


def test_the_details_of_a_wrong_shape_are_unknown(world):
    first, second = req('2026-10-13T10:00:00Z', 'm1', out=400), req('2026-10-13T10:01:00Z', 'm2', out=400, request='r2')
    first['message']['usage'].update(output_tokens_details='x', server_tool_use=[1])
    second['message']['usage'].update(output_tokens_details={'other': 1}, server_tool_use={})
    data = day_report(world, first, second)
    assert (data['total']['thinking_tokens'], data['total']['web_searches'], data['total']['web_fetches']) == (None,
                                                                                                               ) * 3


def test_thinking_cannot_exceed_the_output(world):
    data = day_report(world, extras(req('2026-10-13T10:00:00Z', 'm1', out=400), thinking=900))
    assert data['total']['thinking_tokens'] == 400 and data['total']['thinking_share'] == 1.0


def test_the_details_of_the_last_line_of_a_request_count(world):
    data = day_report(world,
                      extras(req('2026-10-13T10:00:00Z', 'm1', out=10), thinking=5, searches=1),
                      extras(req('2026-10-13T10:00:01Z', 'm1', out=400), thinking=300, searches=2, fetches=3),
                      req('2026-10-13T10:01:00Z', 'm2', out=10, request='r2'),
                      extras(req('2026-10-13T10:01:01Z', 'm2', out=300, request='r2'), thinking=7))
    assert (data['total']['thinking_tokens'], data['total']['web_searches'], data['total']['web_fetches']) == (307, 2,
                                                                                                               3)
    last_lacks = day_report_other(world, extras(req('2026-10-13T11:00:00Z', 'm9', out=10, request='r9'), thinking=5),
                                  req('2026-10-13T11:00:01Z', 'm9', out=400, request='r9'))
    assert last_lacks['total']['thinking_tokens'] == 307         # the unfinished line with details lost to the complete one


def day_report_other(world, *lines):
    world.transcript('-home-u-proj-a', 'bbbbbbbb-2', list(lines))
    return world.report('--day', DAY)


def test_thinking_and_web_calls_do_not_change_the_cost_weight(world):
    plain = day_report(world, req('2026-10-13T10:00:00Z', 'm1', out=1_000_000))
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1111-2222-3333-444444444444',
                     [extras(req('2026-10-13T10:00:00Z', 'm1', out=1_000_000), thinking=900_000, searches=5,
                             fetches=5)])
    detailed = world.report('--day', DAY)
    assert detailed['total']['cost_usd'] == plain['total']['cost_usd'] == pytest.approx(20.0)


def test_unpriced_models_report_their_details_too(world):
    data = day_report(world,
                      extras(req('2026-10-13T10:00:00Z', 'm1', model='claude-mystery-9', out=400, cr=100), thinking=40,
                             fetches=2))
    mystery = data['total']['unpriced']['claude-mystery-9']
    assert (mystery['thinking_tokens'], mystery['web_fetches'], mystery['web_searches']) == (40, 2, None)
    assert mystery['cache_hit_rate'] == 1.0


def test_each_model_has_its_tokens_and_cache_hit_rate(world):
    data = day_report(world,
                      req('2026-10-13T10:00:00Z', 'm1', inp=10, cr=90, out=1, request='r1'),
                      req('2026-10-13T10:01:00Z', 'm2', model='claude-sonnet-5-5', inp=50, cw=50, out=1, request='r2'))
    models = data['total']['models']
    assert models['Opus 5.5']['tokens'] == {'input': 10, 'cache_write_5m': 0, 'cache_write_1h': 0, 'cache_read': 90,
                                            'output': 1}
    assert models['Opus 5.5']['cache_hit_rate'] == pytest.approx(0.9)
    assert models['Sonnet 5.x']['cache_hit_rate'] == 0.0
    assert data['total']['cache_hit_rate'] == pytest.approx(90 / 200)


def test_the_cold_starts_of_the_period_are_the_sum_of_those_of_the_clis(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [req('2026-10-13T10:00:00Z', 'a1', out=1),
                                                      req('2026-10-13T10:01:00Z', 'a2', cw=60_000, out=1, request='r2')])
    world.transcript('-home-u-proj-b', 'bbbbbbbb-2', [req('2026-10-13T10:00:00Z', 'b1', out=1, cwd='/home/u/b'),
                                                      req('2026-10-13T10:01:00Z', 'b2', cw1=60_000, out=1, request='r2',
                                                          cwd='/home/u/b'),
                                                      req('2026-10-13T10:02:00Z', 'b3', cw1=60_000, out=1, request='r3',
                                                          cwd='/home/u/b')])
    data = world.report('--day', DAY)
    assert data['total']['cold_starts'] == 3 and sorted(c['cold_starts'] for c in data['clis']) == [1, 2]


def test_the_cold_starts_survive_the_archive(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [req('2026-10-13T10:00:00Z', 'a', out=1),
                                                      req('2026-10-13T10:01:00Z', 'b', cw=60_000, out=1, request='r2')])
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    data = world.report('--day', DAY)
    assert data['coverage']['archive_days'] == [DAY]
    assert data['total']['cold_starts'] == 1 and data['clis'][0]['cold_starts'] == 1


def test_the_details_survive_the_archive_and_unknown_stays_unknown_over_days(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1',
                     [extras(req('2026-10-12T10:00:00Z', 'a', out=100), thinking=40, searches=3),
                      req('2026-10-13T10:00:00Z', 'b', out=100, request='r2')])
    live = world.report('--since', '2026-10-12', '--until', '2026-10-13')
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    archived = world.report('--since', '2026-10-12', '--until', '2026-10-13')
    assert archived['coverage']['archive_days'] == ['2026-10-12', '2026-10-13']
    assert archived['total'] == live['total'] and archived['clis'] == live['clis']
    assert archived['total']['thinking_tokens'] == 40 and archived['total']['web_searches'] == 3
    assert archived['total']['web_fetches'] is None
    assert archived['total']['thinking_share'] == pytest.approx(40 / 100)      # only the day that reports it is in the share


def test_the_text_names_thinking_web_cache_and_cold_starts(world):
    text = day_report_text(world)
    for needle in ('Output: thinking 40 (40 %), web searches 3, web fetches n/a', 'Cache: hit rate', 'cold starts 0',
                   'model Opus 5.5:', 'thinking 40 (40 %)'):
        assert needle in text, needle


def day_report_text(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1',
                     [extras(req('2026-10-13T10:00:00Z', 'a', inp=1, out=100), thinking=40, searches=3)])
    return world.text('--day', DAY)


# --- Sessions, subagents, CLIs ----------------------------------------------------------------------------------

def test_subagents_belong_to_their_main_session_and_are_split_by_type(world):
    session = 'aaaaaaaa-1111-2222-3333-444444444444'
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:00:00Z', 'm1', 1.0)])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:01:00Z', 'm2', 2.0),
                                                 paid('2026-10-13T10:02:00Z', 'm3', 0.5)],
                     sub='x1', meta={'agentType': 'arbeiter-bauen', 'description': SECRET})
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:03:00Z', 'm4', 0.25)], sub='x2')
    data = world.report('--day', DAY)
    assert data['total']['main'] == {'requests': 1, 'cost_usd': pytest.approx(1.0)}
    assert data['total']['subagents']['requests'] == 3 and data['total']['subagents']['cost_usd'] == pytest.approx(2.75)
    assert data['total']['subagents']['by_type'] == {'arbeiter-bauen': {'requests': 2, 'cost_usd': pytest.approx(2.5)},
                                                     'unknown': {'requests': 1, 'cost_usd': pytest.approx(0.25)}}
    assert len(data['clis']) == 1
    cli = data['clis'][0]
    assert cli['id'] == 'aaaaaaaa' and cli['cost_usd'] == pytest.approx(3.75) and cli['requests'] == 4
    assert cli['subagents'] == {'arbeiter-bauen': {'requests': 2, 'cost_usd': pytest.approx(2.5)},
                                'unknown': {'requests': 1, 'cost_usd': pytest.approx(0.25)}}


@pytest.mark.parametrize('meta, expected', [
    ({'agentType': 'bau-bericht', 'name': 'bau-bericht', 'customAgentType': 'arbeiter-bauen',
      'spawnedAgentType': 'arbeiter-bauen'}, 'arbeiter-bauen'),
    ({'agentType': 'bau-bericht', 'name': 'bau-bericht', 'customAgentType': 'architekt'}, 'architekt'),
    ({'agentType': 'general-purpose'}, 'general-purpose'),
    ({'name': 'only-a-name', 'description': 'x'}, 'unknown'),
    ({'agentType': 'bau-bericht', 'customAgentType': 'architekt', 'spawnedAgentType': 'arbeiter-bauen'},
     'arbeiter-bauen'),
    ({'agentType': 'general-purpose', 'customAgentType': None, 'spawnedAgentType': None}, 'general-purpose')])
def test_the_agent_type_is_the_spawned_then_the_custom_then_the_plain_type_and_never_the_name(world, meta, expected):
    session = 'aaaaaaaa-1111'
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:00:00Z', 'm1', 1.0)])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:01:00Z', 'm2', 1.0)], sub='x1', meta=meta)
    data = world.report('--day', DAY)
    assert list(data['total']['subagents']['by_type']) == [expected]
    assert list(data['clis'][0]['subagents']) == [expected]
    assert 'bau-bericht' not in json.dumps(data) or expected == 'bau-bericht'
    assert 'only-a-name' not in json.dumps(data)


def test_an_odd_agent_type_is_not_taken_over(world):
    session = 'aaaaaaaa-1111'
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:00:00Z', 'm1', 1.0)])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:01:00Z', 'm2', 1.0)], sub='x1',
                     meta={'agentType': f'my type {SECRET}'})
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:02:00Z', 'm3', 1.0)], sub='x2',
                     meta=['not', 'a dict'])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:03:00Z', 'm4', 1.0)], sub='x3',
                     meta={'agentType': 7})
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:04:00Z', 'm5', 1.0)], sub='x4')
    (world.projects / '-home-u-proj-a' / session / 'subagents' / 'agent-x4.meta.json').write_text('{broken',
                                                                                                  encoding='utf-8')
    data = world.report('--day', DAY)
    assert set(data['total']['subagents']['by_type']) == {'other', 'unknown'}
    assert SECRET not in json.dumps(data)


def test_the_cli_is_named_by_the_folder_of_its_cwd_its_start_and_the_first_8_characters(world):
    world.transcript('-home-u-proj-a', 'abcdef12-aaaa-bbbb-cccc-ddddeeeeffff',
                     [req('2026-10-13T08:00:00Z', 'm1', out=1, cwd='/home/u/Projekte/hub-0034/projekte/0034-katalog'),
                      req('2026-10-13T09:00:00Z', 'm2', out=1, request='r2', cwd='/tmp/elsewhere')])
    cli = world.report('--day', DAY)['clis'][0]
    assert (cli['id'], cli['label'], cli['start']) == ('abcdef12', '0034-katalog', '2026-10-13T08:00:00Z')
    assert 'abcdef12-aaaa' not in json.dumps(cli)


def test_a_session_without_a_cwd_is_labelled_with_a_question_mark(world):
    line = req('2026-10-13T08:00:00Z', 'm1', out=1)
    del line['cwd']
    assert day_report(world, line)['clis'][0]['label'] == '?'


def test_clis_are_sorted_by_cost_and_a_session_over_midnight_keeps_its_start(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1',
                     [paid('2026-10-12T20:00:00Z', 'a0', 1.0), paid('2026-10-13T08:00:00Z', 'a1', 1.0)])
    world.transcript('-home-u-proj-b', 'bbbbbbbb-2', [paid('2026-10-13T09:00:00Z', 'b1', 5.0, cwd='/home/u/proj-b')])
    clis = world.report('--day', DAY)['clis']
    assert [c['id'] for c in clis] == ['bbbbbbbb', 'aaaaaaaa']
    assert clis[1]['start'] == '2026-10-12T20:00:00Z' and clis[1]['requests'] == 1
    assert clis[1]['cost_usd'] == pytest.approx(1.0)


def test_cache_hit_rate_and_cold_starts(world):
    big = 60_000
    data = day_report(world,
                      req('2026-10-13T10:00:00Z', 'm1', inp=100, cw1=big, out=1, request='r1'),    # first: never a restart
                      req('2026-10-13T10:01:00Z', 'm2', inp=100, cw1=10, cr=big, out=1, request='r2'),
                      req('2026-10-13T10:02:00Z', 'm3', inp=100, cw=50_000, out=1, request='r3'),  # exactly 50 000: cold
                      req('2026-10-13T10:03:00Z', 'm4', inp=100, cw=49_999, out=1, request='r4'))
    cli = data['clis'][0]
    assert cli['cold_starts'] == 1
    whole_input = 4 * 100 + big + 10 + 50_000 + 49_999 + big
    assert cli['cache_hit_rate'] == pytest.approx(big / whole_input)


def test_a_subagent_start_is_no_cold_start(world):
    session = 'aaaaaaaa-1'
    world.transcript('-home-u-proj-a', session, [req('2026-10-13T10:00:00Z', 'm1', out=1)])
    world.transcript('-home-u-proj-a', session, [req('2026-10-13T10:01:00Z', 'm2', cw=90_000, out=1)], sub='x1')
    assert world.report('--day', DAY)['clis'][0]['cold_starts'] == 0


def test_a_cli_without_input_has_no_hit_rate(world):
    assert day_report(world, req('2026-10-13T10:00:00Z', out=5))['clis'][0]['cache_hit_rate'] is None


# --- Periods in local time ---------------------------------------------------------------------------------------

def test_the_day_follows_local_midnight(world):
    spend(world, ('2026-10-12T21:59:59Z', 1.0),      # 23:59:59 local on the 12th
          ('2026-10-12T22:00:00Z', 2.0),             # 00:00:00 local on the 13th
          ('2026-10-13T21:59:59Z', 4.0),             # 23:59:59 local on the 13th
          ('2026-10-13T22:00:00Z', 8.0))             # the 14th
    assert world.report('--day', '2026-10-12')['total']['cost_usd'] == pytest.approx(1.0)
    assert world.report('--day', '2026-10-13')['total']['cost_usd'] == pytest.approx(6.0)
    assert world.report('--day', '2026-10-14')['total']['cost_usd'] == pytest.approx(8.0)


def test_the_day_the_clocks_go_back_has_25_hours(world):
    spend(world, ('2026-10-24T21:59:59Z', 1.0), ('2026-10-24T22:00:00Z', 2.0), ('2026-10-25T22:59:59Z', 4.0),
          ('2026-10-25T23:00:00Z', 8.0))
    data = world.report('--day', '2026-10-25')
    assert data['total']['cost_usd'] == pytest.approx(6.0)
    assert data['period']['start'] == '2026-10-24T22:00:00Z' and data['period']['end'] == '2026-10-25T23:00:00Z'


def test_the_default_period_is_today(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1',
                     [paid('2026-10-14T09:00:00Z', 'a', 1.0), paid('2026-10-13T09:00:00Z', 'b', 2.0)])
    data = world.report()
    assert data['period']['label'] == '2026-10-14' and data['total']['cost_usd'] == pytest.approx(1.0)
    assert world.report('--day')['period']['label'] == '2026-10-14'


def test_month_and_since_until(world):
    spend(world, ('2026-09-30T21:59:59Z', 1.0), ('2026-09-30T22:00:00Z', 2.0), ('2026-10-14T10:00:00Z', 4.0),
          ('2026-10-31T22:59:59Z', 8.0), ('2026-10-31T23:00:00Z', 16.0))
    month = world.report('--month', '2026-10')
    assert month['total']['cost_usd'] == pytest.approx(2 + 4 + 8) and month['period']['label'] == '2026-10'
    assert world.report('--month')['period']['label'] == '2026-10'
    span = world.report('--since', '2026-10-01', '--until', '2026-10-14')
    assert span['total']['cost_usd'] == pytest.approx(2 + 4) and span['period']['label'] == '2026-10-01 to 2026-10-14'
    assert world.report('--since', '2026-10-14')['total']['cost_usd'] == pytest.approx(4.0)     # until = today


def test_the_week_is_the_subscription_week_from_the_reset_time(world):
    reset = T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-13T10:00:00Z', 10, 20, T('2026-10-13T15:00:00Z'), reset),
                   row('2026-10-14T10:00:00Z', 12, 30, T('2026-10-14T15:00:00Z'), reset)])
    spend(world, ('2026-10-12T07:00:00Z', 1.0),      # the exact start of the week
          ('2026-10-12T06:59:59Z', 2.0),             # one second before
          ('2026-10-14T09:00:00Z', 4.0),
          ('2026-10-19T07:00:00Z', 8.0))             # the next week
    data = world.report('--week')
    assert data['total']['cost_usd'] == pytest.approx(5.0)
    assert data['period']['start'] == '2026-10-12T07:00:00Z' and data['period']['end'] == '2026-10-19T07:00:00Z'
    assert data['period']['kind'] == 'week'


def test_a_week_that_has_passed_since_the_last_measurement_rolls_forward(world):
    world.history([row('2026-10-01T10:00:00Z', 10, 20, T('2026-10-01T15:00:00Z'), T('2026-10-05T07:00:00Z'))])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-14T09:00:00Z', 'a', 1.0)])
    data = world.report('--week')                   # 2026-10-12 07:00 to 2026-10-19 07:00 (UTC), the week of NOW
    assert data['period']['start'] == '2026-10-12T07:00:00Z' and data['total']['cost_usd'] == pytest.approx(1.0)


def test_the_week_needs_a_known_reset(world, capsys):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-14T09:00:00Z', 'a', 1.0)])
    assert report_module.main(world.args('--week'), now=NOW) == 1
    assert '--since' in capsys.readouterr().out


# --- Subscription: start and end values, windows ----------------------------------------------------------------------

def test_the_weekly_value_at_start_and_end_and_the_five_hour_windows(world):
    r1, r2, week = T('2026-10-13T13:00:00Z'), T('2026-10-13T20:00:00Z'), T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-12T20:00:00Z', 30, 40, T('2026-10-13T00:00:00Z'), week),
                   row('2026-10-13T08:00:00Z', 5, 41, r1, week), row('2026-10-13T09:00:00Z', 60, 43, r1, week),
                   row('2026-10-13T09:30:00Z', 60, 43, r1, week), row('2026-10-13T10:00:00Z', 62, 44, r1, week),
                   row('2026-10-13T16:00:00Z', 7, 45, r2, week), row('2026-10-13T17:00:00Z', 20, 47, r2, week),
                   row('2026-10-14T10:00:00Z', 1, 50, T('2026-10-14T15:00:00Z'), week)])
    abo = world.report('--day', DAY)['subscription']
    assert abo['week_start'] == {'at': '2026-10-12T20:00:00Z', 'value': 40}
    assert abo['week_end'] == {'at': '2026-10-13T17:00:00Z', 'value': 47}
    assert abo['week_resets'] == week
    assert abo['five_windows'] == [
        {'resets': r1, 'first_seen': '2026-10-13T08:00:00Z', 'peak': 62, 'peak_at': '2026-10-13T10:00:00Z'},
        {'resets': r2, 'first_seen': '2026-10-13T16:00:00Z', 'peak': 20, 'peak_at': '2026-10-13T17:00:00Z'}]


def test_a_weekly_value_before_the_period_of_an_earlier_week_is_not_the_start(world):
    old, new = T('2026-10-12T07:00:00Z'), T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-11T20:00:00Z', 30, 96, None, old), row('2026-10-12T08:00:00Z', 5, 1, None, new),
                   row('2026-10-13T08:00:00Z', 6, 4, None, new)])
    abo = world.report('--day', '2026-10-12')['subscription']
    assert abo['week_start'] == {'at': '2026-10-12T08:00:00Z', 'value': 1}


def test_without_any_measurement_the_subscription_is_empty(world):
    abo = day_report(world, paid('2026-10-13T10:00:00Z', 'a', 1.0))['subscription']
    assert abo == {'week_start': None, 'week_end': None, 'week_resets': None, 'five_windows': []}


def test_a_window_without_reset_times_starts_when_the_value_falls(world):
    world.history([row('2026-10-13T08:00:00Z', 10, 1), row('2026-10-13T09:00:00Z', 50, 2), row('2026-10-13T10:00:00Z', 4, 3),
                   row('2026-10-13T11:00:00Z', None, 3)])
    windows = world.report('--day', DAY)['subscription']['five_windows']
    assert [(w['resets'], w['peak']) for w in windows] == [(None, 50), (None, 4)]


# --- Allocation of the subscription rise ---------------------------------------------------------------------------

def test_the_rise_is_shared_by_cost_between_the_clis_and_a_section_without_requests_is_the_rest(world):
    world.history([row('2026-10-13T10:00:00Z', 10, 20), row('2026-10-13T10:10:00Z', 14, 21),
                   row('2026-10-13T10:20:00Z', 15, 21)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:05:00Z', 'a', 3.0)])
    world.transcript('-home-u-proj-b', 'bbbbbbbb-2', [paid('2026-10-13T10:06:00Z', 'b', 1.0, cwd='/home/u/proj-b')])
    data = world.report('--day', DAY)
    shares = {c['id']: c['estimated_share'] for c in data['clis']}
    assert shares['aaaaaaaa'] == {'five': pytest.approx(3.0), 'week': pytest.approx(0.75)}
    assert shares['bbbbbbbb'] == {'five': pytest.approx(1.0), 'week': pytest.approx(0.25)}
    five = data['allocation']['five']
    assert five == {'counted_points': 5, 'attributed_points': pytest.approx(4.0), 'unexplained_points': pytest.approx(1.0),
                    'skipped_sections': 0, 'skipped_points': 0}
    week = data['allocation']['week']
    assert (week['counted_points'], week['unexplained_points']) == (1,
                                                                    0) and week['attributed_points'] == pytest.approx(1.0)
    assert 'unexplained' in world.text('--day', DAY) and '~' in world.text('--day', DAY)


def test_attributed_and_unexplained_add_up_to_the_measured_rise(world):
    rows = [row(iso(T('2026-10-13T08:00:00Z') + i * 600), i * 2 % 17, i, None, None) for i in range(1, 40)]
    world.history(rows)
    base = T('2026-10-13T08:00:00Z')
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1',
                     [paid(iso(base + i * 600 + 300), f'a{i}', 1.0 + i % 3) for i in range(0, 40, 3)])
    world.transcript('-home-u-proj-b', 'bbbbbbbb-2',
                     [paid(iso(base + i * 600 + 200), f'b{i}', 0.5 + i % 4, cwd='/home/u/proj-b') for i in range(0, 40,
                                                                                                                 2)])
    data = world.report('--day', DAY)
    for metric in ('five', 'week'):
        alloc = data['allocation'][metric]
        assert alloc['attributed_points'] + alloc['unexplained_points'] == pytest.approx(alloc['counted_points'])
        assert sum(c['estimated_share'][metric] for c in data['clis']) == pytest.approx(alloc['attributed_points'])
    assert data['allocation']['week']['counted_points'] > 0 and data['allocation']['five']['skipped_sections'] > 0


def test_a_section_across_a_reset_is_not_counted(world):
    r1, r2 = T('2026-10-13T11:00:00Z'), T('2026-10-13T16:00:00Z')
    world.history([row('2026-10-13T10:50:00Z', 90, 20, r1, T('2026-10-19T07:00:00Z')),
                   row('2026-10-13T11:05:00Z', 5, 21, r2, T('2026-10-19T07:00:00Z'))])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T11:00:00Z', 'a', 3.0)])
    data = world.report('--day', DAY)
    assert data['allocation']['five'] == {'counted_points': 0, 'attributed_points': 0, 'unexplained_points': 0,
                                          'skipped_sections': 1, 'skipped_points': 0}
    assert data['allocation']['week']['counted_points'] == 1       # the week did not reset
    assert data['clis'][0]['estimated_share']['five'] == 0


def test_a_new_window_counts_as_a_reset_although_the_value_did_not_fall(world):
    world.history([row('2026-10-13T10:50:00Z', 3, 20, T('2026-10-13T11:00:00Z')),
                   row('2026-10-13T11:05:00Z', 8, 20, T('2026-10-13T16:00:00Z'))])
    five = world.report('--day', DAY)['allocation']['five']
    assert five['skipped_sections'] == 1 and five['counted_points'] == 0
    world.history([row('2026-10-13T10:50:00Z', 3, 20, T('2026-10-13T11:00:00Z')),
                   row('2026-10-13T11:05:00Z', 8, 20, T('2026-10-13T11:00:59Z'))])
    assert world.report('--day', DAY)['allocation']['five']['counted_points'] == 5      # 59 s apart: the same window


def test_a_fall_without_reset_times_counts_as_a_reset(world):
    world.history([row('2026-10-13T10:50:00Z', 90, 20), row('2026-10-13T11:05:00Z', 5, 21)])
    assert world.report('--day', DAY)['allocation']['five']['skipped_sections'] == 1


def test_a_section_longer_than_60_minutes_is_not_counted_but_its_rise_is_shown(world):
    world.history([row('2026-10-13T10:00:00Z', 10, 20), row('2026-10-13T11:00:00Z', 12, 20),
                   row('2026-10-13T12:00:01Z', 20, 21)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T11:30:00Z', 'a', 3.0)])
    five = world.report('--day', DAY)['allocation']['five']
    assert five['counted_points'] == 2 and five['skipped_sections'] == 1 and five['skipped_points'] == 8
    assert five['attributed_points'] == 0 and five['unexplained_points'] == 2


def test_a_missing_value_is_neither_counted_nor_skipped(world):
    world.history([row('2026-10-13T10:00:00Z', None, 20), row('2026-10-13T10:10:00Z', 7, 21)])
    five = world.report('--day', DAY)['allocation']['five']
    assert five == {'counted_points': 0, 'attributed_points': 0, 'unexplained_points': 0, 'skipped_sections': 0,
                    'skipped_points': 0}


def test_a_section_belongs_to_the_day_of_its_end(world):
    world.history([row('2026-10-12T21:50:00Z', 10, 20), row('2026-10-12T22:05:00Z', 13, 20),
                   row('2026-10-12T22:15:00Z', 14, 20)])
    assert world.report('--day', '2026-10-12')['allocation']['five']['counted_points'] == 0
    assert world.report('--day', '2026-10-13')['allocation']['five']['counted_points'] == 4


def test_a_calibration_stretch_belongs_to_the_day_it_ends_on(world):
    world.history([row('2026-10-12T21:55:00Z', 1, 0), row('2026-10-12T22:05:00Z', 2, 0)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-12T22:00:00Z', 'a', 1.0)])
    five = world.report('--since', '2026-10-12', '--until', '2026-10-13')['calibration']['five']
    assert list(five['per_day']) == ['2026-10-13']


def test_requests_of_unpriced_models_cannot_carry_a_share(world):
    world.history([row('2026-10-13T10:00:00Z', 10, 20), row('2026-10-13T10:10:00Z', 14, 20)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [req('2026-10-13T10:05:00Z', model='claude-mystery-9', out=1000)])
    five = world.report('--day', DAY)['allocation']['five']
    assert five['unexplained_points'] == 4 and five['attributed_points'] == 0


# --- Usage line of the transcripts for the time before the history -----------------------------------------------------

def test_before_the_history_the_usage_line_of_the_transcripts_is_the_measurement(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [
        usage_line('2026-10-13T08:00:00Z', 10, 20), paid('2026-10-13T08:05:00Z', 'a', 3.0),
        usage_line('2026-10-13T08:10:00Z', 14, 21, age=1),
        usage_line('2026-10-13T08:20:00Z', 99, 99, age=5),         # not fresh: no measurement
        usage_line('2026-10-13T08:30:00Z', 40, 90, age=2)])
    data = world.report('--day', DAY)
    assert data['allocation']['five']['counted_points'] == 4
    assert data['clis'][0]['estimated_share']['five'] == pytest.approx(4.0)
    assert data['subscription']['week_end'] == {'at': '2026-10-13T08:10:00Z', 'value': 21}


def test_unreadable_usage_lines_are_skipped(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [
        '{broken Usage: 5h "attachment"', json.dumps({'type': 'attachment', 'attachment': 'Usage: 5h 50 % x'}),
        json.dumps({'type': 'user', 'timestamp': '2026-10-13T08:00:00Z',
                    'message': 'Usage: 5h 10 % · week 20 % · measured 0 min ago "attachment"'}),
        json.dumps({'type': 'attachment', 'timestamp': '2026-10-13T08:01:00Z',
                    'attachment': 'Usage: unknown, "attachment"'}),
        usage_line('2026-10-13T08:10:00Z', 10, 20), usage_line('2026-10-13T08:20:00Z', 13, 20)])
    assert world.report('--day', DAY)['allocation']['five']['counted_points'] == 3


def test_the_german_usage_line_is_read_too(world):
    text = 'Nutzung: 5h 10 % (Reset 14:00) · 7T 20 % (Reset Mo) · Messung vor 0 min'
    later = text.replace('10 %', '16 %')
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [
        {'type': 'attachment', 'timestamp': '2026-10-13T08:00:00Z', 'attachment': {'content': [text]}},
        {'type': 'attachment', 'timestamp': '2026-10-13T08:10:00Z', 'attachment': {'content': [later]}}])
    assert world.report('--day', DAY)['allocation']['five']['counted_points'] == 6


def test_the_usage_line_is_used_only_before_the_first_line_of_the_history(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [
        usage_line('2026-10-13T08:00:00Z', 10, 20), usage_line('2026-10-13T08:10:00Z', 12, 20),      # before the history
        usage_line('2026-10-13T12:00:00Z', 30, 25), usage_line('2026-10-13T12:10:00Z', 40, 26)])    # the history covers this
    world.history([row('2026-10-13T11:50:00Z', 20, 24), row('2026-10-13T12:05:00Z', 22, 24)])
    five = world.report('--day', DAY)['allocation']['five']
    # 8:00-8:10 and 11:50-12:05 count, 8:10-11:50 is a gap
    assert five['counted_points'] == 2 + 2 and five['skipped_sections'] == 1


def test_the_usage_lines_are_not_even_parsed_when_the_history_covers_the_period(world, monkeypatch):
    def fail(raw):
        raise AssertionError('parsed')

    monkeypatch.setattr(report_module, 'parse_usage_line', fail)
    world.history([row('2026-10-01T08:00:00Z', 10, 20)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1',
                     [usage_line('2026-10-13T09:00:00Z', 10, 20), paid('2026-10-13T09:01:00Z', 'a', 1.0)])
    assert world.report('--day', DAY)['total']['requests'] == 1


def test_a_usage_line_is_not_read_when_the_history_covers_the_period(world):
    world.history([row('2026-10-01T08:00:00Z', 10, 20), row('2026-10-13T08:00:00Z', 10, 20),
                   row('2026-10-13T08:10:00Z', 11, 20)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1',
                     [usage_line('2026-10-13T09:00:00Z', 10, 20), usage_line('2026-10-13T09:10:00Z', 50, 20)])
    assert world.report('--day', DAY)['allocation']['five']['counted_points'] == 1


# --- Calibration --------------------------------------------------------------------------------------------------

def steady_sections(world, count, cost=2.0, start='2026-10-13T08:00:00Z', model='claude-opus-5-5', session='aaaaaaaa-1',
                    project='-home-u-proj-a', first=0, five_first=0):
    """`count` sections of 5 minutes, each with one request of `cost` dollars and a rise of one point."""
    base = T(start)
    rows = [row(iso(base + i * 300), five_first + i, 0) for i in range(first, first + count + 1)]
    lines = [req(iso(base + i * 300 + 150), message=f'{session}-{i}', request=f'{session}-{i}', model=model,
                 out=round(cost * 1e6 / 20 if model == 'claude-opus-5-5' else cost * 1e6 / 10))
             for i in range(first, first + count)]
    world.transcript(project, session, lines)
    return rows


def test_below_50_sections_with_one_cli_the_calibration_says_not_yet(world):
    world.history(steady_sections(world, 49))
    five = world.report('--day', DAY)['calibration']['five']
    assert five['status'] == 'not calibrated yet' and five['usd_per_point'] is None and five['sections'] == 49
    assert 'not calibrated yet' in world.text('--day', DAY)


def test_from_50_sections_the_cost_per_point_is_a_regression_through_zero(world):
    world.history(steady_sections(world, 50, cost=2.0))
    five = world.report('--day', DAY)['calibration']['five']
    assert five['status'] == 'calibrated' and five['sections'] == 50 and five['usd_per_point'] == pytest.approx(2.0)
    assert five['per_day'] == {DAY: {'sections': 50, 'usd_per_point': pytest.approx(2.0)}}
    assert five['per_model'] == {'Opus 5.5': {'sections': 50, 'usd_per_point': pytest.approx(2.0)}}


def test_the_regression_weights_larger_sections_more(world):
    # 30 sections of 1 dollar for 1 point and 30 of 4 dollars for 2 points: sum(cost^2) / sum(cost x points)
    base = T('2026-10-13T08:00:00Z')
    rows, lines, points = [row(iso(base), 0, 0)], [], 0
    for i in range(60):
        cost, rise = (1.0, 1) if i < 30 else (4.0, 2)
        points += rise
        rows.append(row(iso(base + (i + 1) * 300), points, 0))
        lines.append(paid(iso(base + i * 300 + 150), f'm{i}', cost))
    world.history(rows)
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', lines)
    expected = (30 * 1 + 30 * 16) / (30 * 1 * 1 + 30 * 4 * 2)
    assert world.report('--day', DAY)['calibration']['five']['usd_per_point'] == pytest.approx(expected)


def test_sections_with_two_clis_or_an_unpriced_model_do_not_calibrate(world):
    rows = steady_sections(world, 50)
    # the first section has two CLIs
    world.transcript('-home-u-proj-b', 'bbbbbbbb-2', [paid('2026-10-13T08:02:00Z', 'x', 1.0, cwd='/home/u/proj-b')])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1',
                     [req('2026-10-13T08:07:00Z', 'u1', model='claude-mystery-9', out=5)], sub='x1')
    world.history(rows)
    assert world.report('--day', DAY)['calibration']['five']['sections'] == 48


def test_per_day_values_come_with_their_number_of_sections(world):
    rows = steady_sections(world, 30, cost=2.0) + steady_sections(world, 30, cost=3.0, start='2026-10-14T08:00:00Z',
                                                                   session='bbbbbbbb-2', project='-home-u-proj-b')
    world.history(rows)
    five = world.report('--since', DAY, '--until', '2026-10-14')['calibration']['five']
    assert five['sections'] == 60
    assert five['per_day'] == {DAY: {'sections': 30, 'usd_per_point': pytest.approx(2.0)},
                               '2026-10-14': {'sections': 30, 'usd_per_point': pytest.approx(3.0)}}


def test_a_model_is_shown_only_with_50_sections_of_its_own(world):
    opus = steady_sections(world, 55, cost=2.0)       # five of these sections also hold a Sonnet request of a subagent
    base = T('2026-10-13T08:00:00Z')
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1',
                     [req(iso(base + i * 300 + 100), f'sub{i}', request=f'sub{i}', model='claude-sonnet-5-5', out=10)
                      for i in range(5)], sub='x9')

    def block(count, start, model, session, project):
        return steady_sections(world, count, cost=0.5, start=start, model=model, session=session, project=project)

    sonnet = block(55, '2026-10-14T08:00:00Z', 'claude-sonnet-5-5', 'bbbbbbbb-2', '-home-u-proj-b')
    more_sonnet = block(10, '2026-10-14T14:00:00Z', 'claude-sonnet-5-5', 'cccccccc-3', '-home-u-proj-c')
    haiku = block(49, '2026-10-15T08:00:00Z', 'claude-haiku-4-5', 'dddddddd-4', '-home-u-proj-d')
    fable = block(50, '2026-10-16T08:00:00Z', 'claude-fable-5-1', 'eeeeeeee-5', '-home-u-proj-e')
    world.history(opus + sonnet + more_sonnet + haiku + fable)
    five = world.report('--since', DAY, '--until', '2026-10-16')['calibration']['five']
    assert five['sections'] == 55 + 55 + 10 + 49 + 50
    # 49 sections of Haiku are too few, 50 of Fable are enough
    assert set(five['per_model']) == {'Opus 5.5', 'Sonnet 5.x', 'Fable'}
    assert five['per_model']['Opus 5.5']['sections'] == 50 and five['per_model']['Opus 5.5']['usd_per_point'] is not None
    assert five['per_model']['Sonnet 5.x']['sections'] == 65 and five['per_model']['Fable']['sections'] == 50


def test_the_weekly_calibration_is_separate(world):
    base = T('2026-10-13T08:00:00Z')
    world.history([row(iso(base + i * 300), i, i // 2) for i in range(61)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid(iso(base + i * 300 + 150), f'm{i}', 2.0) for i in range(60)])
    data = world.report('--day', DAY)['calibration']
    assert data['five']['usd_per_point'] == pytest.approx(2.0)
    assert data['week']['sections'] == 60 and data['week']['usd_per_point'] == pytest.approx(4.0)


def test_a_calibration_without_any_rise_has_no_value(world):
    base = T('2026-10-13T08:00:00Z')
    world.history([row(iso(base + i * 300), 5, 5) for i in range(61)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid(iso(base + i * 300 + 150), f'm{i}', 2.0) for i in range(60)])
    five = world.report('--day', DAY)['calibration']['five']
    assert five['status'] == 'not calibrated yet' and five['usd_per_point'] is None


# --- Guards -----------------------------------------------------------------------------------------------------------

def test_the_decisions_of_the_gates_are_counted_in_the_period(world):
    def entry(time, hook, mode, decision):
        return {'time': time, 'hook': hook, 'mode': mode, 'decision': decision, 'reason': SECRET}
    world.guard_log([entry('2026-10-13T08:00:00+00:00', 'spawn_gate', 'shadow', 'allow'),
                     entry('2026-10-13T08:00:01+00:00', 'spawn_gate', 'shadow', 'allow'),
                     entry('2026-10-13T08:00:02+00:00', 'spawn_gate', 'shadow', 'deny'),
                     entry('2026-10-13T08:00:03+00:00', 'spawn_gate', 'enforce', 'ask'),
                     entry('2026-10-13T08:00:04+00:00', 'soft_stop', 'shadow', 'warn'),
                     entry('2026-10-13T08:00:05+00:00', 'soft_stop', 'shadow', 'allow'),
                     entry('2026-10-13T08:00:06+00:00', 'config', 'shadow', 'defaults'),
                     entry('2026-10-12T08:00:00+00:00', 'spawn_gate', 'shadow', 'deny'),         # another day
                     {'time': 'broken', 'hook': 'spawn_gate', 'mode': 'shadow', 'decision': 'deny'},
                     {'time': '2026-10-13T08:00:07Z', 'hook': 'spawn_gate', 'mode': 5, 'decision': ['x']}])
    rotated = json.dumps(entry('2026-10-13T07:00:00+00:00', 'spawn_gate', 'shadow', 'warn'))
    (world.log / 'log.jsonl.1').write_text(rotated + '\n\n[1]\n{broken\n', encoding='utf-8')
    data = world.report('--day', DAY)
    assert data['guards'] == {
        'spawn_gate': {'shadow': {'allow': 2, 'deny': 1, 'warn': 1}, 'enforce': {'ask': 1}, 'unknown': {'other': 1}},
        'soft_stop': {'shadow': {'warn': 1, 'allow': 1}}}
    text = world.text('--day', DAY)
    assert 'spawn gate' in text and 'soft stop' in text and SECRET not in text


def test_without_a_gate_log_the_guard_section_is_empty(world):
    assert day_report(world, paid('2026-10-13T10:00:00Z', 'a', 1.0))['guards'] == {}


# --- Archive ------------------------------------------------------------------------------------------------------------

ARCHIVE = 'usage-days.jsonl'


def archive_days(world):
    path = world.log / ARCHIVE
    return [json.loads(line)['day'] for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def three_days(world):
    spend(world, ('2026-10-12T10:00:00Z', 1.0), ('2026-10-13T10:00:00Z', 2.0), ('2026-10-14T10:00:00Z', 4.0))


def test_the_archive_gets_every_finished_day_with_requests_and_prints_nothing(world, capsys):
    three_days(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    assert capsys.readouterr().out == ''
    assert archive_days(world) == ['2026-10-12', '2026-10-13']           # today is not finished
    line = json.loads((world.log / ARCHIVE).read_text(encoding='utf-8').splitlines()[1])
    assert line['stats']['cost'] == pytest.approx(2.0) and line['v'] == 1


def test_the_archive_writes_only_missing_days_and_overwrites_nothing(world, capsys):
    three_days(world)
    world.log.mkdir(parents=True)
    marker = json.dumps({'v': 1, 'day': '2026-10-12', 'stats': {'marker': 'old'}})
    (world.log / ARCHIVE).write_text(marker + '\n', encoding='utf-8')
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    assert archive_days(world) == ['2026-10-12', '2026-10-13']
    assert (world.log / ARCHIVE).read_text(encoding='utf-8').splitlines()[0] == marker
    first = (world.log / ARCHIVE).read_bytes()
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    assert (world.log / ARCHIVE).read_bytes() == first                    # a second run changes nothing
    assert capsys.readouterr().out == ''


def test_a_day_that_a_parallel_run_wrote_meanwhile_is_not_written_twice(world):
    world.log.mkdir(parents=True)
    (world.log / ARCHIVE).write_text(json.dumps({'v': 1, 'day': '2026-10-12', 'stats': {'marker': 1}}) + '\n',
                                     encoding='utf-8')
    report_module.write_archive(str(world.log), {'2026-10-12': {'marker': 2}, '2026-10-13': {'marker': 3}})
    lines = [json.loads(line) for line in (world.log / ARCHIVE).read_text(encoding='utf-8').splitlines()]
    assert [(line['day'], line['stats']['marker']) for line in lines] == [('2026-10-12', 1), ('2026-10-13', 3)]


def test_an_unreadable_archive_line_is_kept_and_does_not_stop_the_archive(world):
    three_days(world)
    world.log.mkdir(parents=True)
    seeded = json.dumps({'v': 1, 'day': '2026-10-12', 'stats': {}})
    (world.log / ARCHIVE).write_text('{broken\n[1]\n' + seeded + '\n', encoding='utf-8')
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    lines = (world.log / ARCHIVE).read_text(encoding='utf-8').splitlines()
    assert lines[:3] == ['{broken', '[1]', seeded] and [json.loads(line)['day'] for line in lines[3:]] == ['2026-10-13']


def test_a_cut_off_last_line_of_the_archive_does_not_swallow_the_next_one(world):
    three_days(world)
    world.log.mkdir(parents=True)
    (world.log / ARCHIVE).write_text('{"v":1,"day":"2026-10-1', encoding='utf-8')
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    lines = (world.log / ARCHIVE).read_text(encoding='utf-8').splitlines()
    assert lines[0] == '{"v":1,"day":"2026-10-1' and [json.loads(line)['day'] for line in lines[1:]] == ['2026-10-12',
                                                                                                         '2026-10-13']


@pytest.mark.parametrize('umask', [0o002, 0o022, 0o077])
def test_the_archive_is_private_whatever_the_umask(world, umask):
    three_days(world)
    old = os.umask(umask)
    try:
        assert report_module.main(world.args('--archive'), now=NOW) == 0
    finally:
        os.umask(old)
    assert stat.S_IMODE((world.log / ARCHIVE).stat().st_mode) == 0o600
    assert stat.S_IMODE(world.log.stat().st_mode) == 0o700


def test_an_archive_file_of_an_earlier_umask_becomes_private(world):
    three_days(world)
    world.log.mkdir(parents=True)
    (world.log / ARCHIVE).write_text('', encoding='utf-8')
    os.chmod(world.log / ARCHIVE, 0o666)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    assert stat.S_IMODE((world.log / ARCHIVE).stat().st_mode) == 0o600


def test_a_planted_link_as_the_archive_is_not_written_through(world, capsys):
    three_days(world)
    world.log.mkdir(parents=True)
    victim = world.root / 'victim.txt'
    victim.write_text('untouched', encoding='utf-8')
    os.symlink(victim, world.log / ARCHIVE)
    assert report_module.main(world.args('--archive'), now=NOW) == 1
    assert victim.read_text(encoding='utf-8') == 'untouched' and 'archive' in capsys.readouterr().err


def test_a_link_as_the_log_folder_is_not_followed(world, capsys):
    three_days(world)
    elsewhere = world.root / 'elsewhere'
    elsewhere.mkdir()
    os.symlink(elsewhere, world.log)
    assert report_module.main(world.args('--archive'), now=NOW) == 1
    assert list(elsewhere.iterdir()) == [] and 'archive' in capsys.readouterr().err


def test_the_archive_needs_the_transcripts_folder(world, capsys):
    assert report_module.main(world.args('--archive'), now=NOW) == 1
    assert 'No transcripts' in capsys.readouterr().out and not (world.log / ARCHIVE).exists()


def test_a_day_that_only_the_archive_knows_is_reported_from_it(world):
    three_days(world)
    before = world.report('--day', DAY)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    after = world.report('--day', DAY)
    assert after['total'] == before['total'] and after['clis'] == before['clis']
    assert after['coverage']['archive_days'] == [DAY] and before['coverage']['archive_days'] == []
    assert world.report('--day', '2026-10-01')['total']['requests'] == 0       # not in the archive: nothing


def test_a_month_mixes_live_days_and_archived_days(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1',
                     [paid('2026-10-12T10:00:00Z', 'a', 1.0), paid('2026-10-13T10:00:00Z', 'b', 2.0)])
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    (world.projects / '-home-u-proj-a' / 'aaaaaaaa-1.jsonl').unlink()
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-14T10:00:00Z', 'c', 4.0)])
    data = world.report('--month', '2026-10')
    assert data['total']['cost_usd'] == pytest.approx(7.0) and data['total']['requests'] == 3
    assert data['coverage']['archive_days'] == ['2026-10-12', '2026-10-13']
    assert len(data['clis']) == 1 and data['clis'][0]['requests'] == 3 and data['clis'][0]['id'] == 'aaaaaaaa'


def test_a_day_with_transcripts_is_computed_not_read_from_the_archive(world):
    three_days(world)
    world.log.mkdir(parents=True)
    (world.log / ARCHIVE).write_text(json.dumps({'v': 1, 'day': DAY, 'stats': {'requests': 99, 'cost': 99.0}}) + '\n',
                                     encoding='utf-8')
    data = world.report('--day', DAY)
    assert data['total']['requests'] == 1 and data['coverage']['archive_days'] == []


def test_a_broken_archived_day_is_skipped_and_counted(world):
    world.log.mkdir(parents=True)
    good = {'v': 1, 'day': '2026-10-11', 'stats': {'requests': 2, 'cost': 1.5, 'tokens': {'output': 9}}}
    bad_day = {'v': 1, 'day': 'soon', 'stats': {}}
    bad_stats = {'v': 1, 'day': '2026-10-10', 'stats': {'requests': 'many', 'clis': [1], 'models': {'x': 5}}}
    (world.log / ARCHIVE).write_text('\n'.join(json.dumps(x) for x in (good, bad_day, bad_stats)) + '\n{no json\n',
                                     encoding='utf-8')
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'b', 2.0)])
    data = world.report('--since', '2026-10-10', '--until', '2026-10-13')
    assert data['coverage']['archive_days'] == ['2026-10-11']
    assert data['coverage']['unreadable_archive_lines'] == 3
    assert data['total']['requests'] == 3 and data['total']['cost_usd'] == pytest.approx(3.5)


@pytest.mark.parametrize('stats', [
    {'requests': 3, 'cost': 1.0, 'tokens': {'input': 5}, 'clis': {'a': {'start': 1}}},
    {'tokens': 5}, {'tokens': {'input': 'x'}}, {'cost': 'x'}, {'clis': [1]}, {'clis': {'a': {'start': 1}}},
    {'clis': {'a': {'label': 5, 'start': 1}}}, {'clis': {'a': {'label': 'x', 'start': 1, 'tokens': 7}}}, {'abo': 5},
    {'abo': {'week_start': {'t': 'x', 'value': 1}}}, {'abo': {'week_resets': 'x'}},
    {'abo': {'five_windows': [{'resets': None, 'first': 'x', 'peak': 1, 'peak_at': 1}]}},
    {'abo': {'five_windows': [{'resets': 'x', 'first': 1, 'peak': 1,
                               'peak_at': 1}]}}, {'abo': {'five_windows': [{'first': 1}]}}])
def test_a_malformed_archived_day_is_skipped_and_leaves_the_rest_alone(world, stats):
    world.log.mkdir(parents=True)
    (world.log / ARCHIVE).write_text(json.dumps({'v': 1, 'day': '2026-10-12', 'stats': stats}) + '\n', encoding='utf-8')
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'b', 2.0)])
    data = world.report('--since', '2026-10-12', '--until', '2026-10-13')
    assert data['coverage']['archive_days'] == [] and data['coverage']['unreadable_archive_lines'] == 1
    assert data['total']['requests'] == 1 and data['total']['cost_usd'] == pytest.approx(2.0)


@pytest.mark.parametrize('first_peak, second_peak', [(30, 50), (50, 30)])
def test_a_window_over_midnight_is_one_window_after_merging_two_archived_days(world, first_peak, second_peak):
    reset, week = T('2026-10-13T01:00:00Z'), T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-12T20:00:00Z', 10, 40, reset, week),
                   row('2026-10-12T21:00:00Z', first_peak, 41, reset, week),
                   row('2026-10-12T22:30:00Z', second_peak, 42, reset + 5, week + 5),
                   row('2026-10-12T23:30:00Z', second_peak, 43, reset, week)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1',
                     [paid('2026-10-12T20:30:00Z', 'a', 1.0), paid('2026-10-12T23:00:00Z', 'b', 1.0)])
    live = world.report('--since', '2026-10-12', '--until', '2026-10-13')
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    (world.log / 'usage-history.jsonl').unlink()
    merged = world.report('--since', '2026-10-12', '--until', '2026-10-13')
    assert merged['coverage']['archive_days'] == ['2026-10-12', '2026-10-13']
    assert merged['subscription'] == live['subscription']
    window, = merged['subscription']['five_windows']
    assert window['first_seen'] == '2026-10-12T20:00:00Z' and window['peak'] == max(first_peak, second_peak)
    assert window['peak_at'] == ('2026-10-12T22:30:00Z' if second_peak > first_peak else '2026-10-12T21:00:00Z')
    assert merged['subscription']['week_start'] == {'at': '2026-10-12T20:00:00Z', 'value': 40}
    assert merged['subscription']['week_end'] == {'at': '2026-10-12T23:30:00Z', 'value': 43}


def test_archived_calibration_and_windows_merge_into_a_week(world):
    base = T('2026-10-12T08:00:00Z')
    week_reset, window = T('2026-10-19T07:00:00Z'), T('2026-10-12T13:00:00Z')
    rows = [row(iso(base + i * 300), i, i // 10, window, week_reset) for i in range(31)]
    world.history(rows)
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid(iso(base + i * 300 + 150), f'm{i}', 2.0) for i in range(30)])
    live = world.report('--since', '2026-10-12', '--until', '2026-10-12')
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    (world.log / 'usage-history.jsonl').unlink()
    archived = world.report('--since', '2026-10-12', '--until', '2026-10-12')
    assert archived['coverage']['archive_days'] == ['2026-10-12']
    for key in ('subscription', 'allocation', 'calibration', 'guards', 'total', 'clis'):
        assert archived[key] == live[key], key
    assert len(archived['subscription']['five_windows']) == 1


# --- Privacy ---------------------------------------------------------------------------------------------------------

def test_no_message_text_reaches_the_output_or_the_archive(world, capsys):
    session = 'aaaaaaaa-1111-2222-3333-444444444444'
    world.transcript('-home-u-proj-a', session, [
        {'type': 'user', 'timestamp': '2026-10-13T09:59:00Z',
         'message': {'role': 'user', 'content': f'my password is {SECRET}'}},
        req('2026-10-13T10:00:00Z', 'm1', out=10, text=f'the password is {SECRET}'),
        {'type': 'user', 'timestamp': '2026-10-13T10:01:00Z', 'toolUseResult': {'stdout': SECRET},
         'message': {'usage': {}}},
        usage_line('2026-10-13T10:02:00Z', 10, 20), usage_line('2026-10-13T10:12:00Z', 12, 20)])
    world.transcript('-home-u-proj-a', session, [req('2026-10-13T10:05:00Z', 'm2', out=10, request='r2', text=SECRET)],
                     sub='x1', meta={'agentType': 'arbeiter-bauen', 'prompt': SECRET})
    world.guard_log([{'time': '2026-10-13T10:00:00Z', 'hook': 'spawn_gate', 'mode': 'shadow', 'decision': 'allow',
                      'reason': SECRET}])
    outputs = [world.text('--day', DAY), world.text('--day', DAY, '--json')]
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    outputs.append((world.log / ARCHIVE).read_text(encoding='utf-8'))
    outputs.append(world.text('--month', '2026-10'))
    for output in outputs:
        assert SECRET not in output and session not in output and 'password' not in output
    assert 'aaaaaaaa' in outputs[0]


# --- Robustness of the readers ------------------------------------------------------------------------------------------

def test_broken_transcript_lines_are_skipped(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [
        '{broken "usage": 1', '[1, 2, "usage"]', '"usage"', '{"type":"assistant","message":{"usage":{"output_tokens":9}}}',
        {'type': 'assistant', 'timestamp': 'yesterday', 'message': {'id': 'x', 'usage': {'output_tokens': 9}}},
        {'type': 'assistant', 'timestamp': 5, 'message': {'id': 'y', 'usage': {'output_tokens': 9}}},
        {'type': 'assistant', 'timestamp': '2026-10-13T10:00:00Z', 'message': 'text with usage'},
        {'type': 'assistant', 'timestamp': '2026-10-13T10:00:00Z', 'message': {'id': 'z', 'usage': 'many'}},
        {'type': 'assistant', 'timestamp': '2026-10-13T10:00:00Z',
         'message': {'id': 'w', 'usage': {'output_tokens': 4}, 'model': 7}},
        {'type': 'user', 'timestamp': '2026-10-13T10:00:00Z', 'message': {'id': 'u', 'usage': {'output_tokens': 9}}},
        '', paid('2026-10-13T10:01:00Z', 'good', 1.0)])
    data = world.report('--day', DAY)
    assert data['total']['requests'] == 2 and data['total']['cost_usd'] == pytest.approx(1.0)
    assert list(data['total']['unpriced']) == ['?'] and data['total']['unpriced']['?']['tokens']['output'] == 4


def test_a_linked_transcript_is_not_followed(world):
    outside = world.root / 'outside.jsonl'
    outside.write_text(json.dumps(paid('2026-10-13T10:00:00Z', 'a', 1.0)) + '\n', encoding='utf-8')
    folder = world.projects / '-home-u-proj-a'
    folder.mkdir(parents=True)
    os.symlink(outside, folder / 'linked-1.jsonl')
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:01:00Z', 'b', 2.0)])
    assert world.report('--day', DAY)['total']['cost_usd'] == pytest.approx(2.0)


def test_a_transcript_that_cannot_be_opened_is_skipped(world, monkeypatch):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'a', 1.0)])
    world.transcript('-home-u-proj-a', 'locked-2', [paid('2026-10-13T10:01:00Z', 'b', 2.0)])
    real = builtins.open

    def refuse(path, *args, **kwargs):
        if 'locked-2' in str(path):
            raise PermissionError('denied')
        return real(path, *args, **kwargs)

    monkeypatch.setattr(report_module, 'open', refuse, raising=False)
    data = world.report('--day', DAY)
    assert data['total']['cost_usd'] == pytest.approx(1.0) and data['coverage']['transcript_files'] == 1


def test_old_files_are_not_read_for_a_recent_period(world, monkeypatch):
    old = world.transcript('-home-u-proj-a', 'old-1', [paid('2026-08-01T10:00:00Z', 'a', 1.0)])
    os.utime(old, (T('2026-08-01T11:00:00Z'),) * 2)
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'b', 2.0)])
    opened = []
    real = builtins.open
    monkeypatch.setattr(report_module, 'open', lambda path, *a, **k: (opened.append(str(path)), real(path, *a, **k))[1],
                        raising=False)
    data = world.report('--day', DAY)
    assert data['total']['cost_usd'] == pytest.approx(2.0)
    assert not any('old-1' in path for path in opened) and any('aaaaaaaa-1' in path for path in opened)
    assert data['coverage']['transcript_files'] == 1


def test_the_whole_history_is_read_even_when_lines_are_broken(world):
    world.history(['{broken', '[1]', json.dumps(row('2026-10-13T10:00:00Z', 10, 20)), json.dumps({'t': 'soon', 'five': 1}),
                   json.dumps(row('2026-10-13T10:10:00Z', 'ten', 20)), json.dumps(row('2026-10-13T10:10:00Z', 14, 21))])
    world.history([json.dumps(row('2026-10-13T09:50:00Z', 8, 19))], name='usage-history.jsonl.1')
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:05:00Z', 'a', 1.0)])
    data = world.report('--day', DAY)
    assert data['coverage']['unreadable_history_lines'] == 4
    assert data['allocation']['five']['counted_points'] == 2 + 4


def test_a_history_row_with_values_of_the_wrong_type_is_unreadable(world):
    good = {'t': '2026-10-13T10:00:00Z', 'five': 1, 'week': 1, 'five_resets': None, 'week_resets': None}
    world.history([json.dumps({**good, 'five': True}), json.dumps({**good, 'five_resets': 'x'}),
                   json.dumps({**good, 't': 5}), json.dumps({**good, 'five': float('nan')})])
    assert world.report('--day', DAY)['coverage']['unreadable_history_lines'] == 4


def test_missing_sources_are_no_error_but_no_transcripts_at_all_is(world, capsys):
    assert report_module.main(world.args('--day', DAY), now=NOW) == 1
    assert 'No transcripts' in capsys.readouterr().out
    world.projects.mkdir()
    assert report_module.main(world.args('--day', DAY), now=NOW) == 0          # a folder without sessions: an empty report
    assert 'Requests: 0' in capsys.readouterr().out


def test_without_the_projects_folder_the_archive_still_answers(world):
    three_days(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    shutil.rmtree(world.projects)
    assert world.report('--day', DAY)['total']['requests'] == 1


def test_a_broken_price_file_is_reported_not_raised(world, monkeypatch, capsys, tmp_path):
    broken = tmp_path / 'prices.json'
    rates = '"input": 1, "cache_write_5m": 1, "cache_write_1h": 1, "cache_read": 1, "output": 1'
    for content in ('{nope', '[]', '{"models": [{"match": "x"}]}', '{"models": "x"}',
                    '{"models": [{"name": "x", "match": "x", "input": "4", "cache_write_5m": 1, "cache_write_1h": 1, '
                    '"cache_read": 1, "output": 1}]}',
                    '{"models": [{"name": "x", "match": "x", ' + rates + ', "long_context": {' + rates + '}}]}'):
        broken.write_text(content, encoding='utf-8')
        monkeypatch.setattr(report_module, 'PRICES_FILE', str(broken))
        world.projects.mkdir(exist_ok=True)
        assert report_module.main(world.args('--day', DAY), now=NOW) == 1, content
        assert 'prices' in capsys.readouterr().err
    monkeypatch.setattr(report_module, 'PRICES_FILE', str(tmp_path / 'missing.json'))
    assert report_module.main(world.args('--day', DAY), now=NOW) == 1


def test_the_shipped_price_file_has_a_source_and_a_date_and_all_prices():
    with open(os.path.join(ROOT, 'tools', 'prices.json'), encoding='utf-8') as f:
        data = json.load(f)
    assert 'platform.claude.com' in data['source']
    assert 'retrieved 2026-10-01' in data['source'] and 'retrieved 2026-10-13' in data['source']
    prices = report_module.load_prices(os.path.join(ROOT, 'tools', 'prices.json'))
    assert [p['name'] for p in prices] == ['Opus 5.5', 'Opus 4.x', 'Sonnet 5.x', 'Haiku 4.5', 'Haiku 5.5', 'Fable']
    assert sum('long_context' in p for p in prices) == 1


def test_the_shipped_price_file_has_the_agreed_prices():
    prices = {p['name']: p for p in report_module.load_prices(report_module.PRICES_FILE)}

    def rates(name):
        return tuple(prices[name][kind] for kind in report_module.KINDS)

    assert rates('Opus 5.5') == (4, 5, 8, 0.20, 20) and rates('Opus 4.x') == (5, 6.25, 10, 0.50, 25)
    assert rates('Sonnet 5.x') == (2, 2.5, 4, 0.20, 10) and rates('Haiku 4.5') == (1, 1.25, 2, 0.10, 5)
    assert rates('Haiku 5.5') == (0.10, 0.125, 0.20, 0.01, 0.50) and rates('Fable') == (10, 12.5, 20, 0.25, 50)
    tier = prices['Haiku 5.5']['long_context']
    assert tier['above_tokens'] == 100_000 and tuple(tier[k] for k in report_module.KINDS) == (0.50, 0.625, 1.0, 0.05,
                                                                                               2.50)


# --- Output ---------------------------------------------------------------------------------------------------------------

def test_the_json_has_a_fixed_set_of_sections(world):
    data = day_report(world, paid('2026-10-13T10:00:00Z', 'a', 1.0))
    assert list(data) == ['period', 'coverage', 'subscription', 'total', 'clis', 'allocation', 'calibration', 'guards']
    assert set(data['period']) == {'kind', 'label', 'start', 'end'} and data['period']['kind'] == 'day'
    assert set(data['coverage']) == {'transcript_files', 'archive_days', 'unreadable_history_lines',
                                     'unreadable_archive_lines'}
    assert set(data['total']) == {'requests', 'tokens', 'cost_usd', 'main', 'subagents', 'models', 'unpriced',
                                  'cache_hit_rate', 'cold_starts', 'thinking_tokens', 'thinking_share', 'web_searches',
                                  'web_fetches'}
    assert set(data['total']['subagents']) == {'requests', 'cost_usd', 'by_type'}
    assert set(data['clis'][0]) == {'id', 'label', 'start', 'requests', 'cost_usd', 'tokens', 'models', 'subagents',
                                    'cache_hit_rate', 'cold_starts', 'estimated_share', 'thinking_tokens', 'thinking_share',
                                    'web_searches', 'web_fetches'}
    assert set(data['calibration']) == {'five', 'week'}
    assert set(data['calibration']['five']) == {'status', 'sections', 'usd_per_point', 'per_day', 'per_model'}


def test_the_text_report(world):
    session = 'aaaaaaaa-1111'
    r1, week = T('2026-10-13T13:00:00Z'), T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-13T10:00:00Z', 10, 20, r1, week), row('2026-10-13T10:10:00Z', 14, 21, r1, week)])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:05:00Z', 'a', 3.0, cwd='/home/u/Projekte/proj-a')])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:06:00Z', 'b', 1.0)], sub='x1',
                     meta={'agentType': 'arbeiter-bauen'})
    world.guard_log([{'time': '2026-10-13T10:00:00Z', 'hook': 'spawn_gate', 'mode': 'shadow', 'decision': 'allow'}])
    text = world.text('--day', DAY)
    for needle in ('Usage report 2026-10-13', 'Week: 20 % -> 21 %', '5h windows: 1', 'peak 14 %',
                   'Requests: 2 (main 1, subagents 1)', 'Cost weight: 4.00 USD', 'arbeiter-bauen', 'proj-a', 'aaaaaaaa',
                   '~4.0', 'Allocation', 'Calibration', 'not calibrated yet', 'spawn gate (shadow): allowed 1'):
        assert needle in text, needle


def test_the_example_in_the_readme_has_the_shape_of_the_real_report(world):
    with open(os.path.join(ROOT, 'README.md'), encoding='utf-8') as f:
        readme = f.read()
    example = readme.split('with invented numbers:\n\n```\n', 1)[1].split('\n```', 1)[0].splitlines()
    session = 'aaaaaaaa-1111'
    r1, week = T('2026-10-13T13:00:00Z'), T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-13T10:00:00Z', 10, 20, r1, week), row('2026-10-13T10:10:00Z', 14, 21, r1, week)])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:05:00Z', 'a', 3.0, cwd='/home/u/Projekte/proj-a')])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:06:00Z', 'b', 1.0)], sub='x1',
                     meta={'agentType': 'reviewer'})
    world.guard_log([{'time': '2026-10-13T10:00:00Z', 'hook': 'spawn_gate', 'mode': 'shadow', 'decision': 'allow'},
                     {'time': '2026-10-13T10:00:01Z', 'hook': 'soft_stop', 'mode': 'shadow', 'decision': 'warn'}])
    real = world.text('--day', DAY).splitlines()

    def headings(lines):
        return {line for line in lines if line and not line.startswith((' ', 'Usage report', 'Sources:'))}

    def labels(lines):
        return {line.split()[0] for line in lines if line.startswith(' ') and ' started ' not in line}

    assert headings(example) == headings(real)
    assert labels(example) <= labels(real)
    assert labels(real) >= {'Week:', 'Requests:', 'Tokens:', 'Models:', 'tokens', 'models', 'cache'}


def test_the_text_of_a_calibrated_period_names_days_and_models(world):
    rows = steady_sections(world, 30, cost=2.0) + steady_sections(world, 30, cost=3.0, start='2026-10-14T08:00:00Z',
                                                                   session='bbbbbbbb-2', project='-home-u-proj-b')
    world.history(rows)
    text = world.text('--since', DAY, '--until', '2026-10-14')
    assert '5h: 2.60 USD per point from 60 stretches' in text and 'week: no rise measured in 60 stretches' in text
    assert 'per day: 10-13 2.00 (30), 10-14 3.00 (30)' in text and 'per model: Opus 5.5 2.60 (60)' in text


def test_the_text_of_an_empty_period(world):
    world.projects.mkdir()
    text = world.text('--day', DAY)
    assert 'Requests: 0' in text and 'By CLI' not in text


def test_the_text_labels_the_archive_and_the_unreadable_lines(world):
    three_days(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    world.history(['{broken'])
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    text = world.text('--day', DAY)
    assert 'from the archive: 2026-10-13' in text and '1 unreadable history line' in text


# --- Arguments ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('args', [
    ['--day', '2026-13-01'], ['--day', 'yesterday'], ['--month', '2026-13'], ['--month', 'October'], ['--since',
                                                                                                      '14.10.2026'],
    ['--since', '2026-10-14', '--until', 'x'], ['--until', '2026-10-14'], ['--since', '2026-10-14', '--until',
                                                                           '2026-10-01'],
    ['--day', DAY, '--week'], ['--week', '--month'], ['--day', DAY, '--since', DAY], ['--archive', '--day',
                                                                                      DAY], ['--archive', '--json'],
    ['--nonsense'], ['--week', 'now'], ['--da', DAY]])
def test_wrong_arguments_are_exit_code_2(world, capsys, args):
    with pytest.raises(SystemExit) as stop:
        report_module.main(world.args(*args), now=NOW)
    assert stop.value.code == 2
    assert 'Traceback' not in capsys.readouterr().err


def test_help_names_every_option_and_the_exit_codes(capsys):
    with pytest.raises(SystemExit) as stop:
        report_module.main(['--help'])
    assert stop.value.code == 0
    text = capsys.readouterr().out
    for option in ('--day', '--week', '--month', '--since', '--until', '--json', '--archive', '--projects-dir',
                   '--config'):
        assert option in text, option


def run_script(*args, env=None):
    done = subprocess.run([sys.executable, SCRIPT, *map(str, args)], capture_output=True, text=True, timeout=60,
                          env=env)
    return done.returncode, done.stdout, done.stderr


def test_the_script_runs_as_a_program(world, monkeypatch):
    stamp = time.time() - 60
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid(iso(stamp), 'a', 1.0)])
    code, out, err = run_script('--projects-dir', world.projects, '--json', '--day',
                                datetime.fromtimestamp(stamp).strftime('%Y-%m-%d'))
    assert code == 0, err
    assert json.loads(out)['total']['requests'] == 1
    code, out, err = run_script('--projects-dir', world.projects, '--day', 'x')
    assert code == 2 and 'Traceback' not in err
    code, out, err = run_script('--projects-dir', world.root / 'nothing')
    assert code == 1 and 'No transcripts' in out


def test_the_log_folder_is_found_like_log_summary_finds_it(world, monkeypatch, tmp_path):
    config = tmp_path / 'mine.json'
    other = tmp_path / 'other-log'
    config.write_text(json.dumps({'log_dir': str(other)}), encoding='utf-8')
    other.mkdir()
    (other / 'usage-history.jsonl').write_text(json.dumps(row('2026-10-13T10:00:00Z', 10, 20)) + '\n', encoding='utf-8')
    world.projects.mkdir()
    code = report_module.main(world.args('--json', '--day', DAY, '--config', str(config)), now=NOW)
    data = json.loads(world.capsys.readouterr().out)
    assert code == 0 and data['subscription']['week_end']['value'] == 20


def test_a_missing_config_file_is_an_argument_error(world, tmp_path):
    with pytest.raises(SystemExit) as stop:
        report_module.main(world.args('--config', str(tmp_path / 'missing.json')), now=NOW)
    assert stop.value.code == 2
