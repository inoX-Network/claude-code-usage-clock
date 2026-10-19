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

import _transcripts
from conftest import ROOT, tzset

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
    tzset()
    yield
    if old is None:
        os.environ.pop('TZ', None)
    else:
        os.environ['TZ'] = old
    tzset()


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
        """The full text report (--full), the report these tests were written for; --json is passed through."""
        return self.overview(*args, *(() if '--json' in args else ('--full',)), now=now)

    def overview(self, *args, now=NOW):
        """What the command prints, with exactly the given arguments."""
        self.projects.mkdir(exist_ok=True)
        assert report_module.main(self.args(*args), now=now) == 0
        return self.capsys.readouterr().out

    def configure(self, monkeypatch, **sections):
        """A config file with the given sections, found through CCUC_CONFIG (the log folder stays the test's)."""
        path = self.root / 'report-config.json'
        path.write_text(json.dumps(sections), encoding='utf-8')
        monkeypatch.setenv('CCUC_CONFIG', str(path))


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
    assert data['total']['main'] == {'requests': 1, 'cost_usd': pytest.approx(1.0), 'week_points': 0, 'five_points': 0}
    assert data['total']['subagents']['requests'] == 3 and data['total']['subagents']['cost_usd'] == pytest.approx(2.75)
    assert data['total']['subagents']['by_type'] == {
        'arbeiter-bauen': {'requests': 2, 'cost_usd': pytest.approx(2.5), 'week_points': 0, 'five_points': 0},
        'unknown': {'requests': 1, 'cost_usd': pytest.approx(0.25), 'week_points': 0, 'five_points': 0}}
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


@pytest.mark.posix_only
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


@pytest.mark.posix_only
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
    out = capsys.readouterr().out
    assert '--since' in out and 'usage file' in out and 'history' in out


def put_usage_file(world, resets, name='usage.json'):
    (world.root / name).write_text(json.dumps({'five_hour': None, 'seven_day': {'used_percentage': 40, 'resets_at': resets},
                                               'measured_at': '2026-10-14T11:00:00Z'}), encoding='utf-8')


def test_without_a_history_the_week_ends_at_the_reset_time_of_the_usage_file(world):
    put_usage_file(world, T('2026-10-19T07:00:00Z'))
    spend(world, ('2026-10-12T07:00:00Z', 1.0), ('2026-10-12T06:59:59Z', 2.0), ('2026-10-14T09:00:00Z', 4.0),
          ('2026-10-19T07:00:00Z', 8.0))
    data = world.report('--week')
    assert data['total']['cost_usd'] == pytest.approx(5.0)
    assert data['period']['start'] == '2026-10-12T07:00:00Z' and data['period']['end'] == '2026-10-19T07:00:00Z'


def test_the_history_comes_before_the_usage_file(world):
    put_usage_file(world, T('2026-10-20T07:00:00Z'))
    world.history([row('2026-10-13T10:00:00Z', 10, 20, T('2026-10-13T15:00:00Z'), T('2026-10-19T07:00:00Z'))])
    assert world.report('--week')['period']['end'] == '2026-10-19T07:00:00Z'


def test_a_reset_of_the_usage_file_that_has_passed_rolls_forward(world):
    put_usage_file(world, T('2026-10-05T07:00:00Z'))
    assert world.report('--week')['period']['start'] == '2026-10-12T07:00:00Z'


def test_a_history_without_weekly_reset_times_falls_back_to_the_usage_file(world):
    put_usage_file(world, T('2026-10-19T07:00:00Z'))
    world.history([row('2026-10-13T10:00:00Z', 10, 20)])
    assert world.report('--week')['period']['end'] == '2026-10-19T07:00:00Z'


def test_the_usage_file_comes_from_the_configuration_of_the_standard_installation(world, monkeypatch, tmp_path):
    for name in ('CCUC_USAGE_FILE', 'CCUC_CONFIG'):
        monkeypatch.delenv(name)
    folder = tmp_path / 'home' / '.claude' / 'hooks' / 'usage-clock'
    folder.mkdir(parents=True)
    (folder / 'config.json').write_text(json.dumps({'usage_file': str(tmp_path / 'installed-usage.json')}), encoding='utf-8')
    put_usage_file(world, T('2026-10-23T07:00:00Z'), name='installed-usage.json')
    assert world.report('--week')['period']['end'] == '2026-10-23T07:00:00Z'


@pytest.mark.parametrize('content', ['{broken', '[1]', '{"seven_day": null}', '{"seven_day": {"resets_at": "soon"}}',
                                     '{"seven_day": {"resets_at": true}}', '{"seven_day": {"used_percentage": 3}}'])
def test_a_usage_file_without_a_usable_reset_time_does_not_help(world, capsys, content):
    (world.root / 'usage.json').write_text(content, encoding='utf-8')
    world.projects.mkdir()
    assert report_module.main(world.args('--week'), now=NOW) == 1
    assert '--since' in capsys.readouterr().out


def test_the_usage_file_is_found_like_the_log_folder(world, monkeypatch, tmp_path):
    world.projects.mkdir()
    config = tmp_path / 'mine.json'
    config.write_text(json.dumps({'usage_file': str(tmp_path / 'mine-usage.json')}), encoding='utf-8')
    put_usage_file(world, T('2026-10-21T07:00:00Z'), name='mine-usage.json')
    put_usage_file(world, T('2026-10-19T07:00:00Z'))                 # the file of CCUC_USAGE_FILE
    assert report_module.main(world.args('--json', '--week', '--config', str(config)), now=NOW) == 0
    assert json.loads(world.capsys.readouterr().out)['period']['end'] == '2026-10-21T07:00:00Z'
    assert report_module.main(world.args('--json', '--week'), now=NOW) == 0
    assert json.loads(world.capsys.readouterr().out)['period']['end'] == '2026-10-19T07:00:00Z'
    monkeypatch.delenv('CCUC_USAGE_FILE')
    (tmp_path / 'home' / '.claude').mkdir(parents=True)
    (tmp_path / 'home' / '.claude' / 'rate-limit.json').write_text(
        json.dumps({'seven_day': {'resets_at': T('2026-10-22T07:00:00Z')}}), encoding='utf-8')
    assert report_module.main(world.args('--json', '--week'), now=NOW) == 0
    assert json.loads(world.capsys.readouterr().out)['period']['end'] == '2026-10-22T07:00:00Z'


# --- Subscription: start and end values, windows ----------------------------------------------------------------------

def test_the_weekly_value_at_start_and_end_and_the_five_hour_windows(world):
    r1, r2, week = T('2026-10-13T13:00:00Z'), T('2026-10-13T20:00:00Z'), T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-12T20:00:00Z', 30, 40, T('2026-10-13T00:00:00Z'), week),
                   row('2026-10-13T08:00:00Z', 5, 41, r1, week), row('2026-10-13T09:00:00Z', 60, 43, r1, week),
                   row('2026-10-13T09:30:00Z', 60, 43, r1, week), row('2026-10-13T10:00:00Z', 62, 44, r1, week),
                   row('2026-10-13T16:00:00Z', 7, 45, r2, week), row('2026-10-13T17:00:00Z', 20, 47, r2, week),
                   row('2026-10-14T10:00:00Z', 1, 50, T('2026-10-14T15:00:00Z'), week)])
    abo = world.report('--day', DAY)['subscription']
    assert abo['week_segments'] == [{'start': {'at': '2026-10-12T20:00:00Z', 'value': 40},
                                     'end': {'at': '2026-10-13T17:00:00Z', 'value': 47}, 'reset_at': week}]
    assert abo['five_windows'] == [
        {'resets': r1, 'first_seen': '2026-10-13T08:00:00Z', 'peak': 62, 'peak_at': '2026-10-13T10:00:00Z'},
        {'resets': r2, 'first_seen': '2026-10-13T16:00:00Z', 'peak': 20, 'peak_at': '2026-10-13T17:00:00Z'}]


def test_a_weekly_value_before_the_period_of_an_earlier_week_is_not_the_start(world):
    old, new = T('2026-10-12T07:00:00Z'), T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-11T20:00:00Z', 30, 96, None, old), row('2026-10-12T08:00:00Z', 5, 1, None, new),
                   row('2026-10-13T08:00:00Z', 6, 4, None, new)])
    abo = world.report('--day', '2026-10-12')['subscription']
    assert [(s['start'], s['end']['value'], s['reset_at']) for s in abo['week_segments']] == [
        ({'at': '2026-10-12T08:00:00Z', 'value': 1}, 1, new)]


def segments(data):
    return [(s['start']['value'], s['end']['value'], s['reset_at']) for s in data['subscription']['week_segments']]


def test_a_weekly_reset_in_the_period_splits_the_week_line_and_nothing_is_subtracted_across_it(world):
    old, new = T('2026-10-13T12:00:00Z'), T('2026-10-20T12:00:00Z')
    five = T('2026-10-13T17:00:00Z')
    world.history([row('2026-10-13T08:00:00Z', 5, 90, five, old), row('2026-10-13T11:00:00Z', 20, 100, five, old),
                   row('2026-10-13T12:30:00Z', 1, 0, five, new), row('2026-10-13T15:00:00Z', 9, 12, five, new)])
    assert segments(world.report('--day', DAY)) == [(90, 100, old), (0, 12, new)]
    text = world.text('--day', DAY)
    assert ('Week: 90 % -> 100 % (+10 points) until Tue 13 Oct 14:00, then 0 % -> 12 % (+12 points), resets Tue 20 Oct 14:00'
            in text)
    assert '-78' not in text and '-88' not in text


def test_without_reset_times_a_fall_of_the_weekly_value_starts_a_new_segment(world):
    world.history([row('2026-10-13T08:00:00Z', 5, 50), row('2026-10-13T09:00:00Z', 6, 60), row('2026-10-13T10:00:00Z', 7, 3),
                   row('2026-10-13T11:00:00Z', 8, 8)])
    assert segments(world.report('--day', DAY)) == [(50, 60, None), (3, 8, None)]
    assert 'Week: 50 % -> 60 % (+10 points), then 3 % -> 8 % (+5 points)' in world.text('--day', DAY)


@pytest.mark.parametrize('gap, expected', [(59, 1), (60, 2)])
def test_weekly_reset_times_a_minute_apart_are_two_windows(world, gap, expected):
    reset = T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-13T08:00:00Z', 1, 50, None, reset), row('2026-10-13T09:00:00Z', 2, 51, None, reset + gap)])
    assert len(world.report('--day', DAY)['subscription']['week_segments']) == expected


def test_a_measurement_without_reset_time_takes_the_one_of_the_rhythm_of_a_later_recorded_one(world):
    reset = T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-13T08:00:00Z', 1, 50), row('2026-10-13T09:00:00Z', 2, 51, None, reset)])
    assert segments(world.report('--day', DAY)) == [(50, 51, reset)]


@pytest.mark.parametrize('second_value, expected', [(51, [(50, 51, 1)]), (3, [(50, 50, None), (3, 3, 1)])])
def test_archived_windows_with_and_without_reset_time_join_unless_the_value_fell(world, second_value, expected):
    reset = T('2026-10-19T07:00:00Z')

    def day(name, value, resets):
        point = {'t': T(f'{name}T10:00:00Z'), 'value': value}
        stats = {'abo': {'weeks': [{'start': point, 'end': point, 'resets': resets}]}}
        return json.dumps({'v': 1, 'day': name, 'stats': stats})

    world.log.mkdir(parents=True)
    (world.log / ARCHIVE).write_text(day('2026-10-12', 50, None) + '\n' + day('2026-10-13', second_value, reset) + '\n',
                                     encoding='utf-8')
    world.projects.mkdir()
    found = segments(world.report('--since', '2026-10-12', '--until', '2026-10-13'))
    assert found == [(start, end, reset if known else None) for start, end, known in expected]


def test_the_reset_of_the_usage_file_marks_the_weekly_windows_when_the_measurements_carry_none(world):
    put_usage_file(world, T('2026-10-19T07:00:00Z'))
    world.history([row('2026-10-12T05:00:00Z', 1, 40), row('2026-10-12T06:00:00Z', 2, 41),
                   row('2026-10-12T08:00:00Z', 3, 42)])
    data = world.report('--since', '2026-10-12', '--until', '2026-10-12')
    assert segments(data) == [(40, 41, T('2026-10-12T07:00:00Z')), (42, 42, T('2026-10-19T07:00:00Z'))]
    (world.root / 'usage.json').unlink()
    assert segments(world.report('--since', '2026-10-12', '--until', '2026-10-12')) == [(40, 42, None)]


def test_a_recorded_reset_time_beats_the_derived_one(world):
    put_usage_file(world, T('2026-10-19T07:00:00Z'))
    recorded = T('2026-10-19T09:00:00Z')
    world.history([row('2026-10-12T05:00:00Z', 1, 40, None, recorded), row('2026-10-12T08:00:00Z', 3, 42, None, recorded)])
    assert segments(world.report('--since', '2026-10-12', '--until', '2026-10-12')) == [(40, 42, recorded)]


def test_the_value_before_the_period_is_the_start_only_in_the_same_derived_window(world):
    put_usage_file(world, T('2026-10-19T07:00:00Z'))
    # the weekly window changes at 07:00
    world.history([row('2026-10-18T21:00:00Z', 1, 60), row('2026-10-19T08:00:00Z', 2, 61)])
    assert segments(world.report('--day', '2026-10-19')) == [(61, 61, T('2026-10-26T07:00:00Z'))]
    world.history([row('2026-10-12T21:00:00Z', 1, 50), row('2026-10-12T23:00:00Z', 2, 51)])
    assert segments(world.report('--day', DAY)) == [(50, 51, T('2026-10-19T07:00:00Z'))]


def test_the_split_week_line_survives_the_archive_and_the_segments_of_two_days_join(world):
    old, new = T('2026-10-12T22:30:00Z'), T('2026-10-19T22:30:00Z')
    five = T('2026-10-13T01:00:00Z')
    world.history([row('2026-10-12T20:00:00Z', 5, 80, five, old), row('2026-10-12T22:00:00Z', 6, 90, five, old),
                   row('2026-10-12T23:00:00Z', 7, 2, five, new), row('2026-10-13T08:00:00Z', 8, 9, five, new)])
    live = world.report('--since', '2026-10-12', '--until', '2026-10-13')
    assert segments(live) == [(80, 90, old), (2, 9, new)]
    spend(world, ('2026-10-12T21:00:00Z', 1.0), ('2026-10-13T07:00:00Z', 1.0))
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    (world.log / 'usage-history.jsonl').unlink()
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    archived = world.report('--since', '2026-10-12', '--until', '2026-10-13')
    assert archived['coverage']['archive_days'] == ['2026-10-12', '2026-10-13']
    assert archived['subscription'] == live['subscription']


def test_a_week_with_an_archived_day_in_the_middle_stays_one_segment(world):
    # Seen 14 Oct 2026: the live part of a week spans the archived day, both start at the week's first value
    reset = T('2026-10-19T13:00:00Z')
    world.history([row('2026-10-12T14:00:00Z', 1, 0, None, reset), row('2026-10-12T20:00:00Z', 5, 5, None, reset),
                   row('2026-10-13T10:00:00Z', 9, 20, None, reset), row('2026-10-13T20:00:00Z', 9, 29, None, reset),
                   row('2026-10-14T08:00:00Z', 3, 38, None, reset)])
    spend(world, ('2026-10-12T15:00:00Z', 1.0), ('2026-10-13T11:00:00Z', 1.0), ('2026-10-14T09:00:00Z', 1.0))
    live = world.report('--week')
    assert segments(live) == [(0, 38, reset)]
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    archived = world.report('--week')
    assert archived['coverage']['archive_days'] == ['2026-10-13']
    assert archived['subscription']['week_segments'] == live['subscription']['week_segments']
    assert 'Week: 0 % -> 38 % (+38 points), resets Mon 19 Oct 15:00' in world.text('--week')


@pytest.mark.parametrize('resets', [(None, None), (1, None), (1, 1)])
def test_overlapping_segments_are_one_window_unless_their_reset_times_differ(resets):
    def seg(t0, v0, t1, v1, r):
        return {'start': {'t': t0, 'value': v0}, 'end': {'t': t1, 'value': v1}, 'resets': r}
    target = [seg(100, 0, 500, 38, resets[0])]
    report_module._merge_weeks(target, [seg(200, 0, 300, 29, resets[1])])
    assert [(s['start']['t'], s['end']['t'], s['end']['value'], s['resets']) for s in target] == [
        (100, 500, 38, 1 if 1 in resets else None)]
    other = [seg(100, 0, 500, 38, 1)]
    report_module._merge_weeks(other, [seg(200, 0, 300, 29, 1 + report_module.RESET_TOLERANCE_S)])
    assert len(other) == 2


@pytest.mark.parametrize('second_value, expected', [(51, [(50, 51, None)]), (3, [(50, 50, None), (3, 3, None)])])
def test_segments_without_reset_times_join_over_midnight_unless_the_value_fell(world, second_value, expected):
    world.history([row('2026-10-12T21:00:00Z', 1, 50), row('2026-10-12T23:00:00Z', 2, second_value)])
    spend(world, ('2026-10-12T21:30:00Z', 1.0), ('2026-10-12T23:30:00Z', 1.0))
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    (world.log / 'usage-history.jsonl').unlink()
    assert segments(world.report('--since', '2026-10-12', '--until', '2026-10-13')) == expected


def test_without_any_measurement_the_subscription_is_empty(world):
    abo = day_report(world, paid('2026-10-13T10:00:00Z', 'a', 1.0))['subscription']
    assert abo == {'week_segments': [], 'five_windows': []}


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
    text = world.text('--day', DAY)
    assert 'unexplained (no request with a price in between)' in text and '~' in text
    assert 'points counted' in text and 'measured' not in text.split('Allocation')[1].split('Calibration')[0]


def test_a_session_outside_every_counted_section_is_not_measured_rather_than_zero(world):
    # a counted section 10:00-10:10 holds the request of a; the request of b lies in a gap of more than 60 minutes
    world.history([row('2026-10-13T10:00:00Z', 10, 20), row('2026-10-13T10:10:00Z', 14, 21),
                   row('2026-10-13T12:00:00Z', 30, 30)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:05:00Z', 'a', 3.0)])
    world.transcript('-home-u-proj-b', 'bbbbbbbb-2', [paid('2026-10-13T11:00:00Z', 'b', 5.0, cwd='/home/u/proj-b')])
    data = world.report('--day', DAY)
    clis = {c['id']: c for c in data['clis']}
    assert clis['aaaaaaaa']['measured_share'] == {'five': pytest.approx(1.0), 'week': pytest.approx(1.0)}
    assert clis['bbbbbbbb']['measured_share'] == {'five': 0, 'week': 0}
    assert clis['bbbbbbbb']['estimated_share'] == {'five': 0, 'week': 0}
    projects = {p['name']: p for p in data['overview']['projects']}
    assert projects['proj-b']['measured_share'] == {'five': 0, 'week': 0}
    assert projects['proj-a']['measured_share'] == {'five': pytest.approx(1.0), 'week': pytest.approx(1.0)}
    # Not measured is '-', a measured 0 stays 0
    text = world.text('--day', DAY)
    block_b = text.split('bbbbbbbb')[1].splitlines()[0]
    assert '- 5h points  - week points' in block_b
    overview = world.overview('--day', DAY)
    table = overview.split('Where it went')[1]
    row_b = next(line for line in table.splitlines() if line.strip().startswith('proj-b'))
    assert row_b.split()[-2] == '-'         # ~week pts, before the sessions


def test_a_measured_zero_stays_zero(world):
    world.history([row('2026-10-13T10:00:00Z', 10, 20), row('2026-10-13T10:10:00Z', 10, 20)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:05:00Z', 'a', 3.0)])
    cli = world.report('--day', DAY)['clis'][0]
    assert cli['estimated_share'] == {'five': 0, 'week': 0}
    assert cli['measured_share'] == {'five': pytest.approx(1.0), 'week': pytest.approx(1.0)}
    block = world.text('--day', DAY).split('aaaaaaaa')[1].splitlines()[0]
    assert '0 5h points  0 week points' in block


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
    assert data['subscription']['week_segments'][-1]['end'] == {'at': '2026-10-13T08:10:00Z', 'value': 21}


def test_unreadable_usage_lines_are_skipped(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [
        '{broken Usage: 5h "attachment"', json.dumps({'type': 'attachment', 'attachment': 'Usage: 5h 50 % x'}),
        json.dumps({'type': 'user', 'timestamp': '2026-10-13T08:00:00Z',
                    'message': 'Usage: 5h 10 % · week 20 % · measured 0 min ago "attachment"'}),
        json.dumps({'type': 'attachment', 'timestamp': '2026-10-13T08:01:00Z',
                    'attachment': 'Usage: unknown, "attachment"'}),
        usage_line('2026-10-13T08:10:00Z', 10, 20), usage_line('2026-10-13T08:20:00Z', 13, 20),
        usage_line('yesterday', 50, 50)])
    assert world.report('--day', DAY)['allocation']['five']['counted_points'] == 3


def test_the_german_usage_line_is_read_too(world):
    text = 'Nutzung: 5h 10 % (Reset 14:00) · 7T 20 % (Reset Mo) · Messung vor 0 min'
    later = text.replace('10 %', '16 %')
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [
        {'type': 'attachment', 'timestamp': '2026-10-13T08:00:00Z',
         'attachment': {'type': 'hook_additional_context', 'content': [text]}},
        {'type': 'attachment', 'timestamp': '2026-10-13T08:10:00Z',
         'attachment': {'type': 'hook_additional_context', 'content': [later]}}])
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

    monkeypatch.setattr(_transcripts, 'parse_usage_line', fail)
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
        return {'time': time, 'hook': hook, 'mode': mode, 'decision': decision, 'reason': f'Checkpoint due: {SECRET}'}
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
@pytest.mark.posix_only
def test_the_archive_is_private_whatever_the_umask(world, umask):
    three_days(world)
    old = os.umask(umask)
    try:
        assert report_module.main(world.args('--archive'), now=NOW) == 0
    finally:
        os.umask(old)
    assert stat.S_IMODE((world.log / ARCHIVE).stat().st_mode) == 0o600
    assert stat.S_IMODE(world.log.stat().st_mode) == 0o700


@pytest.mark.posix_only
def test_an_archive_file_of_an_earlier_umask_becomes_private(world):
    three_days(world)
    world.log.mkdir(parents=True)
    (world.log / ARCHIVE).write_text('', encoding='utf-8')
    os.chmod(world.log / ARCHIVE, 0o666)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    assert stat.S_IMODE((world.log / ARCHIVE).stat().st_mode) == 0o600


@pytest.mark.posix_only
def test_a_planted_link_as_the_archive_is_not_written_through(world, capsys):
    three_days(world)
    world.log.mkdir(parents=True)
    victim = world.root / 'victim.txt'
    victim.write_text('untouched', encoding='utf-8')
    os.symlink(victim, world.log / ARCHIVE)
    assert report_module.main(world.args('--archive'), now=NOW) == 1
    assert victim.read_text(encoding='utf-8') == 'untouched' and 'archive' in capsys.readouterr().err


@pytest.mark.posix_only
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


def test_a_finished_day_that_the_archive_has_is_always_taken_from_the_archive(world):
    three_days(world)
    world.log.mkdir(parents=True)
    (world.log / ARCHIVE).write_text(json.dumps({'v': 1, 'day': DAY, 'stats': {'requests': 99, 'cost': 99.0}}) + '\n',
                                     encoding='utf-8')
    data = world.report('--day', DAY)
    assert data['total']['requests'] == 99 and data['coverage']['archive_days'] == [DAY]


def test_a_day_that_is_not_finished_is_never_taken_from_the_archive(world):
    three_days(world)
    world.log.mkdir(parents=True)
    (world.log / ARCHIVE).write_text(json.dumps({'v': 1, 'day': '2026-10-14', 'stats': {'requests': 99}}) + '\n',
                                     encoding='utf-8')
    data = world.report('--day', '2026-10-14')
    assert data['total']['requests'] == 1 and data['coverage']['archive_days'] == []


def test_a_day_whose_transcripts_were_partly_deleted_is_taken_from_the_archive(world):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'a', 1.0)])
    world.transcript('-home-u-proj-b', 'bbbbbbbb-2', [paid('2026-10-13T11:00:00Z', 'b', 2.0, cwd='/home/u/b')])
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    (world.projects / '-home-u-proj-b' / 'bbbbbbbb-2.jsonl').unlink()
    data = world.report('--day', DAY)
    assert data['total']['requests'] == 2 and data['total']['cost_usd'] == pytest.approx(3.0)


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
    {'abo': {'weeks': 5}},
    {'abo': {'weeks': [{'start': {'t': 'x', 'value': 1}, 'end': {'t': 1, 'value': 1}, 'resets': None}]}},
    {'abo': {'weeks': [{'start': {'t': 1, 'value': 1}}]}},
    {'abo': {'weeks': [{'start': {'t': 1, 'value': 1}, 'end': {'t': 1, 'value': 1}, 'resets': 'x'}]}},
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
    segment, = merged['subscription']['week_segments']
    assert segment['start'] == {'at': '2026-10-12T20:00:00Z', 'value': 40}
    assert segment['end'] == {'at': '2026-10-12T23:30:00Z', 'value': 43}


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


@pytest.mark.posix_only
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

    monkeypatch.setattr(_transcripts, 'open', refuse, raising=False)
    data = world.report('--day', DAY)
    assert data['total']['cost_usd'] == pytest.approx(1.0) and data['coverage']['transcript_files'] == 1


def test_a_transcript_that_vanishes_before_it_is_read_is_skipped(world, monkeypatch):
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'a', 1.0)])
    real = _transcripts.find_transcripts
    monkeypatch.setattr(_transcripts, 'find_transcripts',
                        lambda folder: [(str(world.projects / 'gone.jsonl'), 'gone', False), *real(folder)])
    assert world.report('--day', DAY)['total']['requests'] == 1


def test_old_files_are_not_read_for_a_recent_period(world, monkeypatch):
    old = world.transcript('-home-u-proj-a', 'old-1', [paid('2026-08-01T10:00:00Z', 'a', 1.0)])
    os.utime(old, (T('2026-08-01T11:00:00Z'),) * 2)
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'b', 2.0)])
    opened = []
    real = builtins.open
    monkeypatch.setattr(_transcripts, 'open', lambda path, *a, **k: (opened.append(str(path)), real(path, *a, **k))[1],
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
    assert report_module.main(world.args('--day', DAY, '--full'), now=NOW) == 0     # a folder without sessions: empty
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
        return tuple(prices[name][kind] for kind in _transcripts.KINDS)

    assert rates('Opus 5.5') == (4, 5, 8, 0.20, 20) and rates('Opus 4.x') == (5, 6.25, 10, 0.50, 25)
    assert rates('Sonnet 5.x') == (2, 2.5, 4, 0.20, 10) and rates('Haiku 4.5') == (1, 1.25, 2, 0.10, 5)
    assert rates('Haiku 5.5') == (0.10, 0.125, 0.20, 0.01, 0.50) and rates('Fable') == (10, 12.5, 20, 0.25, 50)
    tier = prices['Haiku 5.5']['long_context']
    assert tier['above_tokens'] == 100_000 and tuple(tier[k] for k in _transcripts.KINDS) == (0.50, 0.625, 1.0, 0.05,
                                                                                               2.50)


# --- Output ---------------------------------------------------------------------------------------------------------------

def test_the_json_has_a_fixed_set_of_sections(world):
    data = day_report(world, paid('2026-10-13T10:00:00Z', 'a', 1.0))
    assert list(data) == ['schema_version', 'period', 'coverage', 'overview', 'subscription', 'total', 'startup', 'clis',
                          'allocation', 'calibration', 'guards', 'guard_reasons']
    assert set(data['period']) == {'kind', 'label', 'start', 'end'} and data['period']['kind'] == 'day'
    assert set(data['coverage']) == {'transcript_files', 'archive_days', 'archive_days_without_agent_split',
                                     'unreadable_history_lines', 'unreadable_archive_lines',
                                     'unreadable_transcript_lines', 'unreadable_context_files'}
    assert set(data['total']) == {'requests', 'tokens', 'cost_usd', 'main', 'subagents', 'models', 'unpriced',
                                  'cache_hit_rate', 'cold_starts', 'thinking_tokens', 'thinking_share', 'web_searches',
                                  'web_fetches'}
    assert set(data['total']['subagents']) == {'requests', 'cost_usd', 'by_type'}
    assert set(data['clis'][0]) == {'id', 'label', 'start', 'requests', 'cost_usd', 'tokens', 'models', 'subagents',
                                    'cache_hit_rate', 'cold_starts', 'estimated_share', 'measured_share', 'thinking_tokens', 'thinking_share',
                                    'web_searches', 'web_fetches', 'context'}
    assert data['clis'][0]['context'] is None
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
                   '~4.0', 'Allocation', 'Calibration', 'not calibrated yet',
                   'spawn gate (shadow mode, only logged): allowed 1'):
        assert needle in text, needle


def test_the_example_in_the_docs_has_the_shape_of_the_real_report(world, tmp_path):
    with open(os.path.join(ROOT, 'docs', 'usage-report.md'), encoding='utf-8') as f:
        doc = f.read()
    example = doc.split('with invented numbers:\n\n```\n', 1)[1].split('\n```', 1)[0].splitlines()
    session = 'aaaaaaaa-1111'
    r1, week = T('2026-10-13T13:00:00Z'), T('2026-10-19T07:00:00Z')
    folder, user = tmp_path / 'Projekte' / 'proj-a', tmp_path / 'user'     # files for the estimate of the start
    for path, text in ((user / 'CLAUDE.md', 'a'), (user / 'rules' / 'style.md', 'b'), (folder / 'CLAUDE.md', 'c'),
                       (user / 'agents' / 'reviewer.md', '---\nname: reviewer\ndescription: x\n---\n'),
                       (user / 'skills' / 's' / 'SKILL.md', '---\ndescription: x\n---\n'),
                       (world.projects / re.sub('[^A-Za-z0-9]', '-', str(folder)) / 'memory' / 'MEMORY.md', 'd')):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    world.history([row('2026-10-13T10:00:00Z', 10, 20, r1, week), row('2026-10-13T10:10:00Z', 14, 21, r1, week)])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:05:00Z', 'a', 3.0, cwd=str(folder))])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:06:00Z', 'b', 1.0)], sub='x1',
                     meta={'agentType': 'reviewer'})
    world.guard_log([gate('2026-10-13T10:00:00Z', 'spawn_gate', 'shadow', 'allow'),
                     gate('2026-10-13T10:00:01Z', 'spawn_gate', 'shadow', 'warn', 'Call model: overrides'),
                     gate('2026-10-13T10:00:02Z', 'spawn_gate', 'shadow', 'deny', '5h window at 78 %: no new blocks'),
                     gate('2026-10-13T10:00:03Z', 'soft_stop', 'shadow', 'warn', 'Checkpoint due: save')])
    real = world.text('--day', DAY, '--claude-dir', str(user)).splitlines()

    def headings(lines):
        return {line for line in lines if line and not line.startswith((' ', 'Usage report', 'Sources:'))}

    def labels(lines):       # the first word of each detail line; the weekday of a day is not one
        return {line.split()[0] for line in lines if line.startswith(' ') and ' started ' not in line
                and line.split()[0] not in report_module.WEEKDAYS}

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


# --- Findings of the independent review ---------------------------------------------------------------------------------

@pytest.mark.parametrize('meta, expected', [
    ({'agentType': 'bau-bericht', 'name': 'bau-bericht'}, 'unknown'),            # a named subagent: agentType holds the name
    ({'agentType': 'reviewer', 'name': 'bau-bericht'}, 'reviewer'),
    ({'spawnedAgentType': 'arbeiter-bauen', 'agentType': 'arbeiter-bauen', 'name': 'arbeiter-bauen'}, 'arbeiter-bauen'),
    ({'customAgentType': 'architekt', 'name': 'architekt'}, 'architekt'),
    ({'agentType': 'general-purpose'}, 'general-purpose')])
def test_an_agent_type_that_is_only_the_name_is_unknown(world, meta, expected):
    session = 'aaaaaaaa-1111'
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:00:00Z', 'm1', 1.0)])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:01:00Z', 'm2', 1.0)], sub='x1', meta=meta)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    outputs = [world.text('--day', DAY), json.dumps(world.report('--day', DAY)),
               (world.log / ARCHIVE).read_text(encoding='utf-8')]
    assert list(world.report('--day', DAY)['total']['subagents']['by_type']) == [expected]
    if expected == 'unknown':
        assert all('bau-bericht' not in output for output in outputs)


def test_an_attachment_of_another_kind_is_no_measurement(world):
    line = ('Usage: 5h 40 % (resets 14:00) · week 61 % (resets Mon 19 Jan 09:00) · measured 0 min ago '
            '· Now: Wed 14 Jan 2026 10:05')
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [
        usage_line('2026-10-13T08:00:00Z', 10, 20), usage_line('2026-10-13T08:10:00Z', 12, 20),
        {'type': 'attachment', 'timestamp': '2026-10-13T08:20:00Z',
         'attachment': {'type': 'edited_text_file', 'filename': 'README.md', 'snippet': line}},
        {'type': 'attachment', 'timestamp': '2026-10-13T08:30:00Z', 'attachment': {'content': [line]}},
        {'type': 'attachment', 'timestamp': '2026-10-13T08:40:00Z', 'attachment': line}])
    data = world.report('--day', DAY)
    assert data['allocation']['five']['counted_points'] == 2 and len(data['subscription']['five_windows']) == 1
    assert len(data['subscription']['week_segments']) == 1


def test_small_falls_without_reset_times_are_noise_and_halving_is_a_new_window(world):
    # stale values of another session (7 and 34) between the real ones, then a real reset to 3 and 12
    values = [(10, 40), (12, 41), (7, 34), (14, 42), (3, 12), (5, 14)]
    world.history([row(f'2026-10-13T08:{10 * i:02d}:00Z', five, week) for i, (five, week) in enumerate(values)])
    data = world.report('--day', DAY)
    assert data['allocation']['five'] == {'counted_points': 6, 'attributed_points': 0, 'unexplained_points': 6,
                                          'skipped_sections': 1, 'skipped_points': 0}
    assert segments(data) == [(40, 42, None), (12, 14, None)]
    assert [(w['peak'], w['resets']) for w in data['subscription']['five_windows']] == [(14, None), (5, None)]


def test_a_small_fall_with_recorded_reset_times_is_still_a_reset(world):
    five, week = T('2026-10-13T13:00:00Z'), T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-13T08:00:00Z', 10, 40, five, week), row('2026-10-13T09:00:00Z', 7, 38, five, week)])
    data = world.report('--day', DAY)
    assert data['allocation']['five']['skipped_sections'] == 1 and data['allocation']['week']['skipped_sections'] == 1


def test_noise_is_judged_against_the_last_value_that_counted(world):
    world.history([row('2026-10-13T08:00:00Z', 20, 40), row('2026-10-13T08:05:00Z', 15, 39),
                   row('2026-10-13T08:10:00Z', 14, 38), row('2026-10-13T08:15:00Z', 22, 41)])
    five = world.report('--day', DAY)['allocation']['five']
    assert five['counted_points'] == 2 and five['skipped_sections'] == 0       # 15 and 14 are noise next to 20


def test_an_archived_day_in_the_old_window_format_is_unreadable_and_written_again(world):
    three_days(world)
    old_window = {'resets': None, 'first': 1.0, 'peak': 5, 'peak_at': 1.0}
    old = json.dumps({'v': 1, 'day': '2026-10-12', 'stats': {'requests': 7, 'abo': {'five_windows': [old_window]}}})
    world.log.mkdir(parents=True)
    (world.log / ARCHIVE).write_text(old + '\n', encoding='utf-8')
    data = world.report('--day', '2026-10-12')
    assert data['coverage']['archive_days'] == [] and data['coverage']['unreadable_archive_lines'] == 1
    assert data['total']['requests'] == 1                                          # computed from the transcript
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    assert archive_days(world) == ['2026-10-12', '2026-10-12', '2026-10-13']
    again = world.report('--day', '2026-10-12')
    assert again['coverage']['archive_days'] == ['2026-10-12'] and again['total']['requests'] == 1


def test_an_archived_day_that_does_not_fit_the_live_figures_is_computed_live(world):
    spend(world, ('2026-10-12T10:00:00Z', 1.0), ('2026-10-13T10:00:00Z', 2.0))
    world.guard_log([{'time': '2026-10-13T10:00:00Z', 'hook': 'spawn_gate', 'mode': 'enforce', 'decision': 'allow'}])
    conflict = {'guards': {'spawn_gate': 5}}               # well formed alone, but the live figures have a dict there
    (world.log / ARCHIVE).write_text(json.dumps({'v': 1, 'day': '2026-10-12', 'stats': conflict}) + '\n', encoding='utf-8')
    data = world.report('--since', '2026-10-12', '--until', '2026-10-13')
    assert data['total']['requests'] == 2 and data['total']['cost_usd'] == pytest.approx(3.0)
    assert data['coverage']['archive_days'] == [] and data['coverage']['unreadable_archive_lines'] == 1


@pytest.mark.parametrize('bad_line', [
    '{"v": 0, "day": "2026-10-12", "stats": {}}', '{"v": 1, "day": ["2026-10-12"], "stats": {}}',
    '{"v": 1, "day": "2026-10-12", "stats": 5}', '{"v": 1, "day": "soon", "stats": {}}', '{"day": "2026-10-12"}'])
def test_a_line_the_reader_rejects_does_not_count_as_a_known_day_for_the_writer(world, bad_line):
    three_days(world)
    world.log.mkdir(parents=True)
    (world.log / ARCHIVE).write_text(bad_line + '\n', encoding='utf-8')
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    data = world.report('--since', '2026-10-12', '--until', '2026-10-13')
    assert data['coverage']['archive_days'] == ['2026-10-12', '2026-10-13'] and data['total']['requests'] == 2


@pytest.mark.parametrize('rows, windows', [
    ([('2026-10-12T20:00:00Z', 10), ('2026-10-12T21:30:00Z', 20), ('2026-10-12T23:00:00Z', 30)], 1),      # over midnight
    ([('2026-10-12T20:00:00Z', 10), ('2026-10-12T21:30:00Z', 20), ('2026-10-13T02:30:00Z', 30)], 2),      # 5 hours apart
    ([('2026-10-12T20:00:00Z', 60), ('2026-10-12T21:30:00Z', 70), ('2026-10-12T23:00:00Z', 20)], 2)])     # the value halved
def test_a_five_hour_window_without_reset_times_is_one_window_over_midnight(world, rows, windows):
    world.history([row(t, five, 0) for t, five in rows])
    spend(world, ('2026-10-12T20:30:00Z', 1.0), ('2026-10-12T23:30:00Z', 1.0))
    live = world.report('--since', '2026-10-12', '--until', '2026-10-13')
    assert len(live['subscription']['five_windows']) == windows
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    (world.log / 'usage-history.jsonl').unlink()
    archived = world.report('--since', '2026-10-12', '--until', '2026-10-13')
    assert archived['coverage']['archive_days'] == ['2026-10-12', '2026-10-13']
    assert archived['subscription'] == live['subscription']


@pytest.mark.parametrize('args', [['--month', '0000-01'], ['--month', '9999-12'], ['--day', '9999-12-31'],
                                  ['--day', '0001-01-01'], ['--since', '0001-01-01'],
                                  ['--since', '2026-01-01', '--until', '9999-12-31'],
                                  ['--month', '1969-12'], ['--day', '1900-01-01']])
def test_dates_at_the_edge_of_the_calendar_are_argument_errors(world, capsys, args):
    with pytest.raises(SystemExit) as stop:
        report_module.main(world.args(*args), now=NOW)
    assert stop.value.code == 2 and 'Traceback' not in capsys.readouterr().err


def test_a_request_id_of_the_wrong_type_or_a_huge_number_discards_the_line_and_counts_it(world):
    good = paid('2026-10-13T10:00:00Z', 'good', 1.0)
    listed, dicted = paid('2026-10-13T10:01:00Z', 'b1', 1.0), paid('2026-10-13T10:02:00Z', 'b2', 1.0)
    listed['requestId'], dicted['requestId'] = ['r'], {'r': 1}
    huge = ('{"type":"assistant","timestamp":"2026-10-13T10:03:00Z","message":{"id":"h","model":"x","usage":'
            '{"output_tokens":' + '9' * 400 + '}}}')
    huge_details = paid('2026-10-13T10:04:00Z', 'b3', 1.0)
    huge_details['message']['usage']['output_tokens_details'] = {'thinking_tokens': 10 ** 30}
    data = day_report(world, good, listed, dicted, huge, huge_details)
    assert data['total']['requests'] == 1 and data['coverage']['unreadable_transcript_lines'] == 4
    assert '4 unreadable transcript line(s) skipped' in world.text('--day', DAY)


def test_a_huge_number_in_the_history_is_unreadable_not_a_crash(world):
    huge = {'t': '2026-10-13T10:00:00Z', 'five': 10 ** 400, 'week': 1, 'five_resets': None, 'week_resets': None}
    world.history([json.dumps(huge),
                   json.dumps(row('2026-10-13T10:00:00Z', 1, 1))])
    assert world.report('--day', DAY)['coverage']['unreadable_history_lines'] == 1


def test_a_real_window_change_without_reset_times_is_not_taken_for_noise_and_does_not_cascade(world):
    world.history([row('2026-10-13T08:00:00Z', 10, 40), row('2026-10-13T08:10:00Z', 14, 41),
                   row('2026-10-13T14:10:00Z', 8, 42), row('2026-10-13T14:20:00Z', 9, 43),
                   row('2026-10-13T14:30:00Z', 10, 44), row('2026-10-13T14:40:00Z', 13, 45),
                   row('2026-10-13T14:50:00Z', 16, 46)])
    data = world.report('--day', DAY)
    five, week = data['allocation']['five'], data['allocation']['week']
    assert (five['counted_points'], five['skipped_sections']) == (4 + 1 + 1 + 3 + 3, 1)
    assert (week['counted_points'], week['skipped_sections'], week['skipped_points']) == (1 + 4, 1, 1)
    assert [w['peak'] for w in data['subscription']['five_windows']] == [14, 16]
    assert segments(data) == [(40, 46, None)]


def test_a_fall_that_nothing_recovers_from_is_a_new_window(world):
    world.history([row('2026-10-13T08:00:00Z', 14, 40), row('2026-10-13T08:10:00Z', 8, 41),
                   row('2026-10-13T08:20:00Z', 9, 42), row('2026-10-13T08:30:00Z', 10, 43)])
    data = world.report('--day', DAY)
    assert data['allocation']['five']['counted_points'] == 2 and data['allocation']['five']['skipped_sections'] == 1
    assert [w['peak'] for w in data['subscription']['five_windows']] == [14, 10]


def test_a_fall_in_the_last_measurement_has_nothing_coming_back_and_a_missing_value_does_not_count_as_coming_back(world):
    world.history([row('2026-10-13T08:00:00Z', 20, 40), row('2026-10-13T08:05:00Z', 15, 41)])
    assert len(world.report('--day', DAY)['subscription']['five_windows']) == 2
    world.history([row('2026-10-13T08:00:00Z', 20, 40), row('2026-10-13T08:05:00Z', 15, 41),
                   row('2026-10-13T08:06:00Z', None, 42), row('2026-10-13T08:07:00Z', 22, 43)])
    assert len(world.report('--day', DAY)['subscription']['five_windows']) == 1


def test_a_fall_of_the_weekly_value_inside_a_known_weekly_window_is_always_stale(world):
    put_usage_file(world, T('2026-10-19T07:00:00Z'))
    world.history([row('2026-10-13T08:00:00Z', 10, 60), row('2026-10-13T08:30:00Z', 11, 40),
                   row('2026-10-13T09:00:00Z', 12, 61)])
    data = world.report('--day', DAY)
    assert segments(data) == [(60, 61, T('2026-10-19T07:00:00Z'))] and data['allocation']['week']['counted_points'] == 1
    assert data['allocation']['five']['counted_points'] == 2
    (world.root / 'usage.json').unlink()                     # without the known window the fall is a window change
    assert segments(world.report('--day', DAY)) == [(60, 60, None), (40, 61, None)]


def test_a_measurement_whose_weekly_value_is_certainly_stale_is_stale_as_a_whole(world):
    put_usage_file(world, T('2026-10-19T07:00:00Z'))
    world.history([row('2026-10-13T08:00:00Z', 20, 60), row('2026-10-13T08:30:00Z', 5, 40),
                   row('2026-10-13T09:00:00Z', 22, 61)])
    data = world.report('--day', DAY)
    assert len(data['subscription']['five_windows']) == 1 and data['allocation']['five']['counted_points'] == 2


def test_a_real_five_hour_reset_is_a_new_window_also_where_the_weekly_window_is_known(world):
    put_usage_file(world, T('2026-10-19T07:00:00Z'))
    world.history([row('2026-10-13T08:00:00Z', 20, 60), row('2026-10-13T08:05:00Z', 8, 61),
                   row('2026-10-13T08:10:00Z', 9, 62)])
    data = world.report('--day', DAY)
    assert len(data['subscription']['five_windows']) == 2 and segments(data) == [(60, 62, T('2026-10-19T07:00:00Z'))]


def test_a_fall_of_the_weekly_value_across_a_known_weekly_reset_is_real(world):
    put_usage_file(world, T('2026-10-19T07:00:00Z'))
    world.history([row('2026-10-19T06:50:00Z', 10, 90), row('2026-10-19T07:10:00Z', 11, 70)])
    assert segments(world.report('--day', '2026-10-19')) == [(90, 90, T('2026-10-19T07:00:00Z')),
                                                              (70, 70, T('2026-10-26T07:00:00Z'))]


def test_the_value_coming_back_may_be_just_as_high_as_before(world):
    world.history([row('2026-10-13T08:00:00Z', 20, 40), row('2026-10-13T08:05:00Z', 15, 41),
                   row('2026-10-13T08:10:00Z', 20, 42)])
    assert len(world.report('--day', DAY)['subscription']['five_windows']) == 1


def test_a_measurement_without_the_value_is_not_the_value_coming_back(world):
    world.history([row('2026-10-13T08:00:00Z', 20, 40), row('2026-10-13T08:05:00Z', 15, 41),
                   row('2026-10-13T08:06:00Z', None, 42)])
    assert len(world.report('--day', DAY)['subscription']['five_windows']) == 2


def test_a_real_weekly_reset_is_kept_when_the_five_hour_value_of_that_measurement_is_stale(world):
    world.history([row('2026-10-13T08:00:00Z', 20, 60), row('2026-10-13T08:05:00Z', 15, 3),
                   row('2026-10-13T08:10:00Z', 22, 5)])
    data = world.report('--day', DAY)
    assert segments(data) == [(60, 60, None), (3, 5, None)] and len(data['subscription']['five_windows']) == 1


def test_a_real_five_hour_reset_is_kept_when_the_weekly_value_of_that_measurement_is_stale(world):
    world.history([row('2026-10-13T08:00:00Z', 20, 60), row('2026-10-13T08:05:00Z', 8, 55),
                   row('2026-10-13T08:10:00Z', 9, 61)])
    data = world.report('--day', DAY)
    assert len(data['subscription']['five_windows']) == 2 and segments(data) == [(60, 61, None)]


def test_a_measurement_with_only_stale_values_is_left_out_so_that_the_requests_around_it_share_the_rise(world):
    world.history([row('2026-10-13T08:00:00Z', 20, 40), row('2026-10-13T08:05:00Z', 15, 35),
                   row('2026-10-13T08:10:00Z', 22, 41)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T08:01:00Z', 'a', 3.0)])
    world.transcript('-home-u-proj-b', 'bbbbbbbb-2', [paid('2026-10-13T08:07:00Z', 'b', 1.0, cwd='/home/u/b')])
    shares = {c['id']: c['estimated_share']['five'] for c in world.report('--day', DAY)['clis']}
    assert shares == {'aaaaaaaa': pytest.approx(1.5), 'bbbbbbbb': pytest.approx(0.5)}


def test_the_age_of_the_last_good_value_is_that_of_the_last_good_measurement_not_of_a_stale_one(world):
    world.history([row('2026-10-13T08:00:00Z', 20, 40), row('2026-10-13T08:59:00Z', 15, 41),
                   row('2026-10-13T09:02:00Z', 14, 42), row('2026-10-13T09:10:00Z', 22, 43)])
    assert len(world.report('--day', DAY)['subscription']['five_windows']) == 2


@pytest.mark.parametrize('recovery_s, noise', [(900, True), (901, False)])
def test_a_value_has_15_minutes_to_come_back(world, recovery_s, noise):
    after = iso(T('2026-10-13T08:05:00Z') + recovery_s)
    world.history([row('2026-10-13T08:00:00Z', 20, 40), row('2026-10-13T08:05:00Z', 15, 41), row(after, 22, 42)])
    assert len(world.report('--day', DAY)['subscription']['five_windows']) == (1 if noise else 2)


@pytest.mark.parametrize('gap_s, noise', [(3600, True), (3601, False)])
def test_a_fall_after_more_than_an_hour_is_never_noise(world, gap_s, noise):
    start = T('2026-10-13T08:00:00Z')
    world.history([row(iso(start), 20, 40), row(iso(start + gap_s), 15, 41), row(iso(start + gap_s + 60), 22, 42)])
    assert len(world.report('--day', DAY)['subscription']['five_windows']) == (1 if noise else 2)


def test_the_weekly_value_of_a_measurement_with_a_stale_five_hour_value_is_kept(world):
    world.history([row('2026-10-13T08:00:00Z', 20, 40), row('2026-10-13T08:05:00Z', 15, 43),
                   row('2026-10-13T08:10:00Z', 22, 44)])
    allocation = world.report('--day', DAY)['allocation']
    assert allocation['five']['counted_points'] == 2 and allocation['week']['counted_points'] == 4


def test_the_five_hour_value_of_a_measurement_with_a_stale_weekly_value_is_kept(world):
    world.history([row('2026-10-13T08:00:00Z', 20, 40), row('2026-10-13T08:05:00Z', 24, 30),
                   row('2026-10-13T08:10:00Z', 26, 44)])
    allocation = world.report('--day', DAY)['allocation']
    assert allocation['five']['counted_points'] == 6 and allocation['week']['counted_points'] == 4


def test_a_fall_next_to_a_measurement_with_a_reset_time_but_none_of_its_own_is_still_noise(world):
    world.history([row('2026-10-13T08:00:00Z', 20, 40, T('2026-10-13T13:00:00Z'), T('2026-10-19T07:00:00Z')),
                   row('2026-10-13T08:10:00Z', 15, 39), row('2026-10-13T08:20:00Z', 22, 41)])
    assert world.report('--day', DAY)['allocation']['five']['counted_points'] == 2


def window(first, last, first_value, last_value, resets=None, peak=None):
    return {'resets': resets, 'first': first, 'last': last, 'first_value': first_value, 'last_value': last_value,
            'peak': peak or max(first_value, last_value), 'peak_at': last}


def test_windows_join_one_after_the_other_and_the_last_value_is_that_of_the_chain():
    joined = []
    report_module._merge_windows(joined, [window(0, 1000, 10, 10)])
    report_module._merge_windows(joined, [window(2000, 3000, 30, 30)])
    report_module._merge_windows(joined, [window(3500, 4000, 15, 15)])        # a fall from 30: a window of its own
    assert [(w['first'], w['last'], w['last_value']) for w in joined] == [(0, 3000, 30), (3500, 4000, 15)]


def test_a_fall_to_exactly_half_is_a_new_window_and_a_five_hour_span_is_two_windows(world):
    world.history([row('2026-10-13T08:00:00Z', 20, 40), row('2026-10-13T08:10:00Z', 10, 20),
                   row('2026-10-13T08:15:00Z', 22, 45)])         # a value as high comes back, but half is a reset
    assert segments(world.report('--day', DAY)) == [(40, 40, None), (20, 45, None)]
    assert len(world.report('--day', DAY)['subscription']['five_windows']) == 2
    world.history([row('2026-10-13T08:00:00Z', 10, 0), row('2026-10-13T12:59:59Z', 20, 0),
                   row('2026-10-13T13:00:00Z', 30, 0)])
    assert [w['peak'] for w in world.report('--day', DAY)['subscription']['five_windows']] == [20, 30]


def test_the_days_at_the_edge_of_the_allowed_years_work(world):
    for args in (['--day', '2000-01-01'], ['--day', '2999-12-31'], ['--month', '2000-01'], ['--month', '2999-12'],
                 ['--year', '2000'], ['--year', '2999']):
        assert world.report(*args)['total']['requests'] == 0


def test_a_count_of_a_trillion_is_read_and_one_more_is_not(world):
    data = day_report(world, req('2026-10-13T10:00:00Z', 'a', out=10 ** 12),
                      req('2026-10-13T10:01:00Z', 'b', out=10 ** 12 + 1, request='r2'))
    assert data['total']['tokens']['output'] == 10 ** 12 and data['coverage']['unreadable_transcript_lines'] == 1


def test_reset_times_beyond_the_year_2100_are_not_measurements(world):
    world.history([row('2026-10-13T10:00:00Z', 1, 1, None, 10 ** 12), row('2026-10-13T10:01:00Z', 1, 1, 10 ** 12, None),
                   row('2026-10-13T10:02:00Z', 1, 1, 4_102_444_800, 4_102_444_800)])
    assert world.report('--day', DAY)['coverage']['unreadable_history_lines'] == 2
    (world.log / 'usage-history.jsonl').unlink()
    put_usage_file(world, 10 ** 12)
    assert report_module.main(world.args('--week'), now=NOW) == 1


def test_the_cache_write_of_one_hour_cannot_exceed_the_cache_write(world):
    line = req('2026-10-13T10:00:00Z', 'm1', out=1)
    line['message']['usage'].update(cache_creation_input_tokens=100,
                                    cache_creation={'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 500})
    tokens = day_report(world, line)['total']['tokens']
    assert (tokens['cache_write_5m'], tokens['cache_write_1h']) == (0, 100)


def test_a_model_name_with_odd_characters_is_not_shown(world):
    data = day_report(world, req('2026-10-13T10:00:00Z', 'm1', model=f'claude odd\n{SECRET}', out=7),
                      req('2026-10-13T10:01:00Z', 'm2', model='claude-fine-1.2:x/y', out=7, request='r2'))
    assert set(data['total']['unpriced']) == {'?', 'claude-fine-1.2:x/y'}
    assert SECRET not in json.dumps(data) and SECRET not in world.text('--day', DAY)


@pytest.mark.parametrize('first_newer', [True, False])
def test_a_request_in_two_main_sessions_belongs_to_the_session_file_modified_first(world, first_newer):
    line = paid('2026-10-13T10:00:00Z', 'm1', 1.0)
    older = world.transcript('-home-u-proj-a', 'zzzzzzzz-1', [line])
    # the continuation: its name sorts first, its file is newer
    newer = world.transcript('-home-u-proj-a', 'aaaaaaaa-2', [line])
    if first_newer:
        older, newer = newer, older
    os.utime(older, (T('2026-10-13T12:00:00Z'),) * 2)
    os.utime(newer, (T('2026-10-13T13:00:00Z'),) * 2)
    data = world.report('--day', DAY)
    assert data['total']['requests'] == 1
    assert [c['id'] for c in data['clis']] == [older.stem[:8]]


def test_the_unpriced_only_section_is_called_unexplained_with_the_right_words(world):
    world.history([row('2026-10-13T10:00:00Z', 10, 20), row('2026-10-13T10:10:00Z', 14, 20)])
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [req('2026-10-13T10:05:00Z', model='claude-mystery-9', out=1000)])
    assert 'unexplained (no request with a price in between)' in world.text('--day', DAY)


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
    done = subprocess.run([sys.executable, SCRIPT, *map(str, args)], capture_output=True, text=True, encoding='utf-8', timeout=60,
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
    assert code == 0 and data['subscription']['week_segments'][-1]['end']['value'] == 20


def test_a_missing_config_file_is_an_argument_error(world, tmp_path):
    with pytest.raises(SystemExit) as stop:
        report_module.main(world.args('--config', str(tmp_path / 'missing.json')), now=NOW)
    assert stop.value.code == 2


def test_a_segment_that_starts_where_the_other_ends_is_the_same_window():
    # One measurement can end one day's segment and start the next one's: it cannot belong to two windows
    target = [{'start': {'t': 100, 'value': 10}, 'end': {'t': 500, 'value': 38}, 'resets': None}]
    report_module._merge_weeks(target, [{'start': {'t': 500, 'value': 0}, 'end': {'t': 600, 'value': 2}, 'resets': None}])
    assert len(target) == 1 and target[0]['end']['t'] == 600
    report_module._merge_weeks(target, [{'start': {'t': 601, 'value': 0}, 'end': {'t': 700, 'value': 1}, 'resets': None}])
    assert len(target) == 2


def test_a_later_segment_that_starts_at_the_same_value_is_the_same_window():
    target = [{'start': {'t': 100, 'value': 10}, 'end': {'t': 500, 'value': 38}, 'resets': None}]
    report_module._merge_weeks(target, [{'start': {'t': 900, 'value': 38}, 'end': {'t': 950, 'value': 40}, 'resets': None}])
    assert [(s['start']['value'], s['end']['value']) for s in target] == [(10, 40)]


# --- The overview: a world of a week ---------------------------------------------------------------------------------------

WEEK_RESET = T('2026-10-19T07:00:00Z')      # Mon 19 Oct 09:00 local
FIVE_RESET = T('2026-10-13T15:00:00Z')


def home(name):
    return f'/home/u/Projekte/{name}'


def sonnet(ts, message, out, **more):
    return req(ts, message=message, request='r-' + message, model='claude-sonnet-5-5', out=out, **more)


def haiku(ts, message, out, **more):
    return req(ts, message=message, request='r-' + message, model='claude-haiku-5-5', out=out, **more)


def gate(time, hook, mode, decision, reason=''):
    return {'time': time, 'hook': hook, 'mode': mode, 'decision': decision, 'reason': reason}


def overview_world(world):
    """Eight sessions in seven projects (named by their folders), a week of measurements, gate
    decisions. Costs in USD: 0016 60.276, 0034 28.96, 0022 6, 0050 3, 0051 1.5, 0052 0.5, 0053 0.25; total 100.486."""
    home16, home34 = home('0016'), home('0034')
    world.transcript('-p16', 'aaaaaaaa-1', [
        extras(paid('2026-10-13T10:10:00Z', 'a1', 40.0, cwd=home16), thinking=500_000),
        req('2026-10-13T10:12:00Z', 'a2', inp=20_000, cr=980_000, request='r-a2', cwd=home16)])
    world.transcript('-p16', 'aaaaaaaa-1', [paid('2026-10-13T10:15:00Z', 'a3', 20.0, cwd=home16)], sub='s1',
                     meta={'agentType': 'stripe-meister'})
    world.transcript('-p34', 'bbbbbbbb-2', [
        paid('2026-10-13T11:10:00Z', 'b1', 15.0, cwd=home34),
        req('2026-10-13T11:14:00Z', 'b2', inp=200_000, cr=800_000, request='r-b2', cwd=home34)])
    world.transcript('-p34', 'bbbbbbbb-2', [paid('2026-10-13T11:12:00Z', 'b3', 5.0, cwd=home34)], sub='s1',
                     meta={'agentType': 'arbeiter-bauen'})
    world.transcript('-p34', 'cccccccc-3', [sonnet('2026-10-13T11:20:00Z', 'c1', 800_000, cwd=home('0034'))])
    world.transcript('-p22', 'dddddddd-4', [paid('2026-10-13T12:10:00Z', 'd1', 6.0, cwd=home('0022'))])
    world.transcript('-p50', 'eeeeeeee-5', [paid('2026-10-13T12:12:00Z', 'e1', 3.0, cwd=home('0050'))])
    world.transcript('-p51', 'ffffffff-6', [paid('2026-10-13T12:14:00Z', 'f1', 1.5, cwd=home('0051'))])
    world.transcript('-p52', 'gggggggg-7', [haiku('2026-10-13T12:16:00Z', 'g1', 1_000_000, cwd=home('0052'))])
    world.transcript('-p53', 'hhhhhhhh-8', [haiku('2026-10-13T12:18:00Z', 'h1', 500_000, cwd=home('0053'))])
    world.history([row(f'2026-10-13T{clock}:00Z', five, week, FIVE_RESET, WEEK_RESET) for clock, five, week in
                   (('10:00', 10, 0), ('10:30', 60, 20), ('11:00', 60, 20), ('11:30', 80, 38), ('12:00', 80, 38),
                    ('12:30', 82, 39))])
    checkpoint, usage_pause = 'Checkpoint due: 15 calls without a checkpoint', 'Usage pause: 5h window at 90 %'
    world.guard_log(
        [gate(f'2026-10-13T09:0{i}:00Z', 'soft_stop', 'enforce', 'deny', checkpoint) for i in range(3)]
        + [gate('2026-10-13T09:05:00Z', 'soft_stop', 'enforce', 'deny', usage_pause)]
        + [gate(f'2026-10-13T09:1{i}:00Z', 'soft_stop', 'shadow', 'deny', checkpoint) for i in range(2)]
        + [gate('2026-10-13T09:20:00Z', 'spawn_gate', 'enforce', 'ask', '7-day window at 82 %: ask first'),
           gate('2026-10-13T09:21:00Z', 'spawn_gate', 'enforce', 'deny', '5h window at 78 %: no new blocks'),
           gate('2026-10-13T09:22:00Z', 'spawn_gate', 'enforce', 'deny', '5h window at 80 %: no new blocks'),
           gate('2026-10-13T09:23:00Z', 'spawn_gate', 'enforce', 'allow'),
           gate('2026-10-13T09:24:00Z', 'soft_stop', 'enforce', 'warn', 'Checkpoint due: soon')])


OVERVIEW_OF_THE_WEEK = """\
Week to Mon 19 Oct 09:00 · 39 % of the weekly limit used (+39 points) · 100 USD at API prices
5h windows: 1, highest 82 % (Tue 13 Oct 14:30)

Where it went            USD   share  ~week pts  sessions
  0016                    60    60 %       20.0         1
  0034                    29    29 %       18.0         2
  0022                   6.0     6 %         <1         1
  0050                   3.0     3 %         <1         1
  0051                   1.5     1 %         <1         1
  2 more                0.75    <1 %         <1         2

Main 75 USD · subagents 25 USD (top: stripe-meister 20 USD, ~6.6 week pts)
Opus 92 USD · Sonnet 8.0 USD · Haiku 0.75 USD · thinking 25 % of output · cache hits 89 %
Start: main 0 tokens (median), subagents 0 (median per type)

Notable
  spawn gate denied 2 agent starts (5h window at N %: 2)
  spawn gate asked before 1 agent start (7-day window at N %: 1)
  soft stop denied 4 subagent tool calls (checkpoint due: 3, usage pause: 1)
  soft stop would have denied 2 subagent tool calls (checkpoint due: 2; shadow mode, nothing was blocked)
  one 5h window reached 82 % (Tue 13 Oct)
  cache hits under 90 %: 0034 80 %
  0016 made 60 % of the cost (60 USD)

Details: --project 0016 · --session aaaaaaaa · --full
"""


def test_the_overview_of_a_week_exactly(world):
    overview_world(world)
    text = world.overview('--week')
    assert text == OVERVIEW_OF_THE_WEEK
    assert len(text.splitlines()) <= 25


def test_the_overview_is_the_default_for_every_period(world):
    overview_world(world)
    for args, title in ((('--day', DAY), 'Day 2026-10-13 · '), (('--month', '2026-10'), 'Month 2026-10 · '),
                        (('--since', DAY, '--until', '2026-10-14'), 'Days 2026-10-13 to 2026-10-14 · '),
                        (('--week',), 'Week to Mon 19 Oct 09:00 · ')):
        text = world.overview(*args)
        assert text.startswith(title) and 'Where it went' in text and 'Usage report' not in text, args
    assert world.overview().startswith('Day 2026-10-14 · ')           # no switch at all: today


def test_the_overview_says_so_when_nothing_was_measured_or_requested(world):
    world.projects.mkdir()
    text = world.overview('--day', DAY)
    assert text == ('Day 2026-10-13 · no weekly measurement · 0.00 USD at API prices\n'
                    '5h windows: none measured\n\nNo requests in this period.\n\nDetails: --full\n')


def test_the_overview_json_carries_the_same_numbers(world):
    overview_world(world)
    data = world.report('--week')
    assert data['schema_version'] == 1 and list(data)[:4] == ['schema_version', 'period', 'coverage', 'overview']
    view = data['overview']
    assert view['title'] == 'Week to Mon 19 Oct 09:00' and view['cost_usd'] == pytest.approx(100.486)
    assert view['week'] == {'used_percent': 39, 'rise_points': 39, 'windows': 1, 'reset_at': WEEK_RESET}
    assert view['five_hour'] == {'windows': 1, 'highest_percent': 82, 'highest_at': '2026-10-13T12:30:00Z'}
    assert [p['name'] for p in view['projects']] == ['0016', '0034', '0022', '0050', '0051', '0052', '0053']
    first = view['projects'][0]
    assert first['cost_usd'] == pytest.approx(60.276) and first['share'] == pytest.approx(60.276 / 100.486)
    assert first['week_points'] == pytest.approx(20.0) and first['sessions'] == 1 and first['session_ids'] == ['aaaaaaaa']
    assert first['cache_hit_rate'] == pytest.approx(0.98)
    assert view['projects'][1]['session_ids'] == ['bbbbbbbb', 'cccccccc'] and view['projects'][2]['cache_hit_rate'] is None
    assert view['max_projects'] == 5
    assert view['more'] == {'projects': 2, 'cost_usd': pytest.approx(0.75), 'share': pytest.approx(0.75 / 100.486),
                            'week_points': pytest.approx(0.0667, abs=1e-3),
                            'five_points': pytest.approx(0.0667 * 2, abs=1e-2),
                            'measured_share': {'five': pytest.approx(1.0), 'week': pytest.approx(1.0)}, 'sessions': 2}
    assert view['split']['main_usd'] == pytest.approx(75.486) and view['split']['subagents_usd'] == pytest.approx(25)
    assert view['split']['top_subagent'] == {'type': 'stripe-meister', 'cost_usd': pytest.approx(20),
                                             'week_points': pytest.approx(6.636, abs=1e-3)}
    assert [(m['name'], round(m['cost_usd'], 2)) for m in view['models']['families']] == [
        ('Opus', 91.74), ('Sonnet', 8.0), ('Haiku', 0.75)]
    assert view['models']['unpriced_requests'] == 0 and view['thinking_share'] == pytest.approx(0.25)
    assert view['cache_hit_rate'] == pytest.approx(0.89)
    assert [n['rule'] for n in view['notable']] == ['gate_denied', 'gate_asked', 'gate_denied', 'gate_denied',
                                                    'five_hour_window', 'cache_hits_low', 'project_dominates']
    assert view['notable'][2] == {'rule': 'gate_denied', 'gate': 'soft_stop', 'mode': 'enforce', 'count': 4,
                                  'reasons': {'checkpoint due': 3, 'usage pause': 1}, 'text': view['notable'][2]['text']}
    assert view['details'] == {'project': '0016', 'session': 'aaaaaaaa'}
    # the text is made of this object and nothing else: the printed overview is the overview of the printed JSON
    text = world.overview('--week')
    assert report_module.render_overview(json.loads(json.dumps(data))) + '\n' == text
    for needle in ('39 %', '+39 points', '100 USD', 'highest 82 %', '2 more'):
        assert needle in text


def test_the_projects_are_the_folder_names(world):
    spend(world, ('2026-10-13T10:00:00Z', 2.0), session='aaaaaaaa-1', project='-home-u-proj-a')
    data = world.report('--day', DAY)
    assert [p['name'] for p in data['overview']['projects']] == ['proj-a']
    assert data['clis'][0]['label'] == 'proj-a'


# --- Projects ---------------------------------------------------------------------------------------------------------

def folders_world(world, *names):
    for number, name in enumerate(names):
        world.transcript('-p', f'{number:08d}-s', [paid(f'2026-10-13T10:0{number}:00Z', f'm{number}', 1.0, cwd=home(name))])


def project_names(world):
    return [p['name'] for p in world.report('--day', DAY)['overview']['projects']]


def test_a_project_is_the_folder_name_and_sessions_of_one_folder_are_one_row(world):
    folders_world(world, '0016-billing', '0016-docs', '0034-agents')
    world.transcript('-p', 'zzzzzzzz-s', [paid('2026-10-13T10:09:00Z', 'm9', 3.0, cwd=home('0016-billing'))])
    view = world.report('--day', DAY)['overview']
    assert [(p['name'], p['sessions'], p['cost_usd']) for p in view['projects']] == [
        ('0016-billing', 2, 4.0), ('0016-docs', 1, 1.0), ('0034-agents', 1, 1.0)]


# --- Top 5 and "N more" -----------------------------------------------------------------------------------------------

def many_projects(world, count):
    for number in range(count):
        world.transcript('-p', f'{number:08d}-s', [paid(f'2026-10-13T10:{number:02d}:00Z', f'm{number}', 10.0 - number,
                                                         cwd=home(f'proj{number}'))])


@pytest.mark.parametrize('count, more', [(1, None), (5, None), (6, 1), (8, 3)])
def test_at_most_five_projects_are_shown_and_the_rest_is_one_line(world, count, more):
    many_projects(world, count)
    text = world.overview('--day', DAY)
    rows = text.split('Where it went', 1)[1].split('\n\n', 1)[0].splitlines()[1:]
    other = [row for row in rows if row.split()[1:2] == ['more']]
    rows = [row for row in rows if row not in other]
    assert len(rows) == min(count, 5) and [r.split()[0] for r in rows] == [f'proj{i}' for i in range(min(count, 5))]
    if more is None:
        assert other == []
    else:
        assert len(other) == 1 and other[0].split()[:2] == [str(more), 'more']
        view = world.report('--day', DAY)['overview']
        assert view['more']['projects'] == more and len(view['projects']) == count
        assert view['more']['cost_usd'] == pytest.approx(sum(10.0 - n for n in range(5, count)))


def test_projects_with_the_same_cost_are_ordered_by_name(world):
    for number, name in enumerate(('zeta', 'alpha', 'mid')):
        world.transcript('-p', f'{number:08d}-s', [paid(f'2026-10-13T10:0{number}:00Z', f'm{number}', 1.0, cwd=home(name))])
    assert project_names(world) == ['alpha', 'mid', 'zeta']


# --- Points below one -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('value, decimals, expected', [(0, 1, '0'), (0.04, 1, '<1'), (0.96, 1, '<1'), (0.999999, 2, '<1'),
                                                       (1.0, 1, '1.0'), (1.04, 1, '1.0'), (16.44, 1, '16.4'),
                                                       (3.456, 2, '3.46')])
def test_points_below_one_are_shown_as_less_than_one(value, decimals, expected):
    assert report_module.points_text(value, decimals) == expected


@pytest.mark.parametrize('value, expected', [(0, '0.00'), (0.004, '0.00'), (0.56, '0.56'), (0.994, '0.99'), (1.0, '1.0'),
                                             (9.94, '9.9'), (9.96, '10'), (99.4, '99'), (268.4, '268'), (1234.5, '1234')])
def test_usd_text_keeps_two_decimals_below_one_one_below_ten_and_none_above(value, expected):
    assert report_module.usd_text(value) == expected


def test_the_full_report_shows_tiny_estimates_as_less_than_one(world):
    r1, week = T('2026-10-13T13:00:00Z'), T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-13T10:00:00Z', 10, 20, r1, week), row('2026-10-13T10:10:00Z', 12, 21, r1, week)])
    world.transcript('-p', 'aaaaaaaa-1', [paid('2026-10-13T10:05:00Z', 'a', 1.0, cwd=home('big'))])
    world.transcript('-p', 'bbbbbbbb-2', [paid('2026-10-13T10:06:00Z', 'b', 0.03, cwd=home('small'))])
    text = world.text('--day', DAY)
    big = next(line for line in text.splitlines() if line.startswith('  big'))
    small = next(line for line in text.splitlines() if line.startswith('  small'))
    assert big.endswith('~1.9 5h points  <1 week points')       # 0.97 week points are not "~0.97" and not "~1.0"
    assert small.endswith('<1 5h points  <1 week points') and '~0.0' not in big + small and '~1.0 ' not in big + small


# --- Notable: every rule on its own -----------------------------------------------------------------------------------

def notable(world, *args):
    lines = world.overview(*args).split('\nNotable\n', 1)
    return [line.strip() for line in lines[1].split('\n\n', 1)[0].splitlines()] if len(lines) == 2 else []


def test_a_quiet_period_has_no_notable_block(world):
    two_projects(world, 1.0, 1.0)
    text = world.overview('--day', DAY)
    assert 'Notable' not in text and world.report('--day', DAY)['overview']['notable'] == []


def two_projects(world, first, second, **more):
    world.transcript('-p1', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'a', first, cwd=home('alpha'), **more)])
    world.transcript('-p2', 'bbbbbbbb-2', [paid('2026-10-13T10:01:00Z', 'b', second, cwd=home('beta'))])


@pytest.mark.parametrize('first, second, expected', [
    (5.0, 5.0, []), (5.01, 4.99, ['alpha made 50 % of the cost (5.0 USD)']),
    (9.0, 1.0, ['alpha made 90 % of the cost (9.0 USD)']),
    (3.0, 7.0, ['beta made 70 % of the cost (7.0 USD)'])])
def test_a_project_with_more_than_half_of_the_cost_is_notable(world, first, second, expected):
    two_projects(world, first, second)
    assert notable(world, '--day', DAY) == expected


def test_the_dominating_project_is_notable_even_when_it_is_the_only_one(world):
    spend(world, ('2026-10-13T10:00:00Z', 2.0))
    assert notable(world, '--day', DAY) == ['proj-a made 100 % of the cost (2.0 USD)']


def cached(world, name, session, inp, cr):
    world.transcript('-p', session, [req('2026-10-13T10:00:00Z', 'm-' + session, inp=inp, cr=cr, request='r-' + session,
                                         cwd=home(name))])


@pytest.mark.parametrize('inp, cr, fires', [(100_000, 900_000, False), (100_001, 899_999, True), (10, 990, False),
                                            (400_000, 600_000, True), (0, 5, False)])
def test_a_project_below_ninety_percent_cache_hits_is_notable(world, inp, cr, fires):
    cached(world, 'alpha', 'aaaaaaaa-1', inp, cr)
    spend(world, ('2026-10-13T10:00:00Z', 1.0), session='bbbbbbbb-2', project='-p2')       # a project without cache numbers
    lines = [line for line in notable(world, '--day', DAY) if line.startswith('cache')]
    rate = cr / (inp + cr)
    assert lines == ([f'cache hits under 90 %: alpha {int(100 * rate)} %'] if fires else [])


def test_the_projects_below_ninety_percent_are_listed_in_one_line_by_cost(world):
    world.transcript('-p1', 'aaaaaaaa-1', [req('2026-10-13T10:00:00Z', 'a', inp=500, cr=500, out=dollars(1.0), request='ra',
                                               cwd=home('alpha'))])
    world.transcript('-p2', 'bbbbbbbb-2', [req('2026-10-13T10:01:00Z', 'b', inp=700, cr=300, out=dollars(3.0), request='rb',
                                               cwd=home('beta'))])
    world.transcript('-p3', 'cccccccc-3', [req('2026-10-13T10:02:00Z', 'c', inp=10, cr=990, out=dollars(2.0), request='rc',
                                               cwd=home('gamma'))])
    assert [line for line in notable(world, '--day', DAY) if line.startswith('cache')] == [
        'cache hits under 90 %: beta 30 %, alpha 50 %']


def sections_world(world, rise_five, rise_week, with_request, five_resets=FIVE_RESET):
    """Two measurements with a rise and (or without) a request in between."""
    world.history([row('2026-10-13T10:00:00Z', 10, 20, five_resets, WEEK_RESET),
                   row('2026-10-13T10:30:00Z', 10 + rise_five, 20 + rise_week, five_resets, WEEK_RESET)])
    cwd = home('alpha')
    world.transcript('-p1', 'aaaaaaaa-1', [paid('2026-10-13T10:15:00Z' if with_request else '2026-10-13T09:00:00Z', 'a', 1.0,
                                                 cwd=cwd)])
    world.transcript('-p2', 'bbbbbbbb-2', [paid('2026-10-13T10:16:00Z' if with_request else '2026-10-13T09:01:00Z', 'b', 1.0,
                                                 cwd=home('beta'))])


@pytest.mark.parametrize('rise_five, rise_week, with_request, expected', [
    (7, 3, False, ['unexplained rise (no request with a price in between): week 3.0 points, 5h 7.0 points']),
    (0, 3, False, ['unexplained rise (no request with a price in between): week 3.0 points']),
    (7, 0, False, ['unexplained rise (no request with a price in between): 5h 7.0 points']),
    (0, 0, False, []),
    (7, 3, True, [])])
def test_a_rise_without_a_request_is_notable(world, rise_five, rise_week, with_request, expected):
    sections_world(world, rise_five, rise_week, with_request)
    assert [line for line in notable(world, '--day', DAY) if line.startswith('unexplained')] == expected
    entries = [n for n in world.report('--day', DAY)['overview']['notable'] if n['rule'] == 'unexplained_rise']
    assert [e['text'] for e in entries] == expected
    if expected:
        assert entries[0]['week_points'] == rise_week and entries[0]['five_points'] == rise_five


def window_world(world, peaks):
    """One 5h window per peak value, each with its own reset time, on the days 13 and 14."""
    rows = []
    for number, peak in enumerate(peaks):
        reset = T(f'2026-10-13T{10 + 5 * number:02d}:00:00Z') + 3600
        rows += [row(f'2026-10-13T{9 + 5 * number:02d}:00:00Z', peak - 1, 20, reset, WEEK_RESET),
                 row(f'2026-10-13T{9 + 5 * number:02d}:30:00Z', peak, 20, reset, WEEK_RESET)]
    world.history(rows)
    spend(world, ('2026-10-13T09:10:00Z', 1.0))


@pytest.mark.parametrize('peaks, expected', [
    ([74], []), ([75], ['one 5h window reached 75 % (Tue 13 Oct)']), ([85], ['one 5h window reached 85 % (Tue 13 Oct)']),
    ([40, 80], ['one 5h window reached 80 % (Tue 13 Oct)']),
    ([76, 90], ['2 5h windows reached 75 % or more (highest 90 %, Tue 13 Oct)']),
    ([76, 90, 80], ['3 5h windows reached 75 % or more (highest 90 %, Tue 13 Oct)'])])
def test_a_five_hour_window_from_the_start_block_threshold_is_notable(world, peaks, expected):
    window_world(world, peaks)
    assert [line for line in notable(world, '--day', DAY) if '5h window' in line] == expected


def test_the_threshold_of_the_five_hour_rule_is_the_one_of_the_spawn_gate(world, monkeypatch):
    window_world(world, [60])
    assert [line for line in notable(world, '--day', DAY) if '5h window' in line] == []
    world.configure(monkeypatch, thresholds={'start_block_5h': 60})
    lines = [line for line in notable(world, '--day', DAY) if '5h window' in line]
    assert lines == ['one 5h window reached 60 % (Tue 13 Oct)']


def gates_world(world, entries):
    spend(world, ('2026-10-13T10:00:00Z', 1.0))
    world.guard_log(entries)


def test_a_denial_or_a_question_of_a_gate_is_notable_with_its_reasons(world):
    gates_world(world, [gate('2026-10-13T08:00:00Z', 'soft_stop', 'enforce', 'deny', 'Checkpoint due: save first')] * 64)
    assert notable(world, '--day', DAY)[:1] == ['soft stop denied 64 subagent tool calls (checkpoint due: 64)']
    gates_world(world, [gate('2026-10-13T08:00:00Z', 'soft_stop', 'enforce', 'deny', 'Checkpoint due: x')])
    assert notable(world, '--day', DAY)[:1] == ['soft stop denied 1 subagent tool call (checkpoint due: 1)']
    gates_world(world, [gate('2026-10-13T08:00:00Z', 'spawn_gate', 'enforce', 'ask', 'Usage pause: 81 %')] * 2)
    assert notable(world, '--day', DAY)[:1] == ['spawn gate asked before 2 agent starts (usage pause: 2)']
    gates_world(world, [gate('2026-10-13T08:00:00Z', 'spawn_gate', 'shadow', 'ask', 'Usage pause: x')])
    assert notable(world, '--day', DAY)[:1] == [
        'spawn gate would have asked before 1 agent start (usage pause: 1; shadow mode, nothing was blocked)']


@pytest.mark.parametrize('decision', ['allow', 'warn', 'other'])
def test_allowed_and_warned_decisions_alone_are_not_notable(world, decision):
    gates_world(world, [gate('2026-10-13T08:00:00Z', hook, 'enforce', decision, 'Checkpoint due: x')
                        for hook in ('soft_stop', 'spawn_gate')])
    assert not [line for line in notable(world, '--day', DAY) if 'gate' in line or 'soft stop' in line]


def test_a_reason_is_the_text_before_the_first_colon_with_percentages_replaced(world):
    gates_world(world, [gate('2026-10-13T08:00:00Z', 'spawn_gate', 'enforce', 'deny', reason) for reason in (
        '5h window at 78 %: no new blocks', '5h window at 80 %: no new blocks', 'Agent …: model unknown',
        'No colon in this one', '', ': leading colon', 'X' * 200 + ': long', 'Tab\tand\x07bell: odd')])
    reasons = world.report('--day', DAY)['guard_reasons']['spawn_gate']['enforce']['deny']
    assert reasons == {'5h window at N %': 2, 'agent …': 1, 'no colon in this one': 1, 'no reason': 2, 'x' + 'X' * 59: 1,
                       'tab?and?bell': 1}


def test_more_than_three_reasons_are_shortened_and_unrecorded_ones_are_named(world):
    reasons = ['Alpha: a'] * 4 + ['Beta: b'] * 3 + ['Gamma: c'] * 2 + ['Delta: d', 'Epsilon: e']
    gates_world(world, [gate('2026-10-13T08:00:00Z', 'soft_stop', 'enforce', 'deny', reason) for reason in reasons])
    assert notable(world, '--day', DAY)[0] == 'soft stop denied 11 subagent tool calls (alpha: 4, beta: 3, gamma: 2, other: 2)'


def test_the_notable_lines_come_in_a_fixed_order(world):
    overview_world(world)
    lines = notable(world, '--week')
    assert [line.split()[0] for line in lines] == ['spawn', 'spawn', 'soft', 'soft', 'one', 'cache', '0016']


# --- Details: --project and --session ---------------------------------------------------------------------------------

def cli_blocks(text):
    """The blocks of the "By CLI" section: a line with two spaces of indent starts one, deeper lines belong to it."""
    lines = text.split('\nBy CLI', 1)[1].split('\n', 1)[1].split('\n\n', 1)[0].splitlines()
    blocks = []
    for line in lines:
        if line.startswith('  ') and not line.startswith('    '):
            blocks.append([])
        blocks[-1].append(line)
    return blocks


def sum_block(text):
    return text.split('\n\nSum of ', 1)[1].split('\n\n', 1)[0].splitlines()


def test_a_project_shows_the_blocks_of_the_full_report_for_its_sessions_and_their_sum(world):
    overview_world(world)
    full = world.overview('--week', '--full')
    detail = world.overview('--week', '--project', '0034')
    wanted = [block for block in cli_blocks(full) if block[0].split()[1] in ('bbbbbbbb', 'cccccccc')]
    assert len(wanted) == 2 and cli_blocks(detail) == wanted
    assert sum_block(detail)[0] == '2 CLIs'
    assert sum_block(detail)[1] == '  28.96 USD  4 requests  ~20.0 5h points  ~18.00 week points'
    assert [line.strip() for line in sum_block(detail)[2:]] == [
        'tokens input 200k, cache write 5m 0, cache write 1h 0, cache read 800k, output 1.8M',
        'models Opus 5.5 20.96, Sonnet 5.x 8.00', 'output thinking n/a, web searches n/a, web fetches n/a',
        'subagents arbeiter-bauen 5.00 USD (1)', 'cache hits 80.0 %, cold starts 0']
    head = detail.splitlines()[:2]
    assert head[0] == 'Usage report week to Mon 19 Oct 09:00 (local time, UTC+02:00) - project 0034'
    assert head[1].startswith('Sources: ') and 'Where it went' not in detail and 'Notable' not in detail


def test_a_session_prefix_shows_that_session_and_a_longer_text_than_the_id_works_too(world):
    overview_world(world)
    full = world.overview('--week', '--full')
    wanted = [block for block in cli_blocks(full) if block[0].split()[1] == 'bbbbbbbb']
    for given in ('b', 'bbbb', 'bbbbbbbb', 'bbbbbbbb-2', 'bbbbbbbb-2-and-more-than-an-id'):
        detail = world.overview('--week', '--session', given)
        assert cli_blocks(detail) == wanted, given
        assert sum_block(detail)[0] == '1 CLI' and detail.splitlines()[0].endswith(' - session bbbbbbbb')


def test_project_and_session_together_narrow_down_and_the_periods_combine(world):
    overview_world(world)
    detail = world.overview('--day', DAY, '--project', '0034', '--session', 'c')
    assert [block[0].split()[1] for block in cli_blocks(detail)] == ['cccccccc']
    assert detail.splitlines()[0].endswith('- project 0034, session cccccccc')


def selection_error(world, capsys, *args):
    world.projects.mkdir(exist_ok=True)
    with pytest.raises(SystemExit) as stop:
        report_module.main(world.args(*args), now=NOW)
    assert stop.value.code == 2
    return capsys.readouterr().err


def ambiguous_world(world):
    world.transcript('-p1', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'a', 1.0, cwd=home('alpha'))])
    world.transcript('-p2', 'aaaaaaab-2', [paid('2026-10-13T10:01:00Z', 'b', 2.0, cwd=home('beta'))])


def test_an_ambiguous_session_prefix_names_the_candidates(world, capsys):
    ambiguous_world(world)
    error = selection_error(world, capsys, '--day', DAY, '--session', 'aaaaaaa')
    assert "--session 'aaaaaaa' matches 2 sessions: aaaaaaab (beta), aaaaaaaa (alpha)" in error
    assert 'give more characters' in error and 'Traceback' not in error
    assert cli_blocks(world.overview('--day', DAY, '--session', 'aaaaaaaa'))[0][0].split()[1] == 'aaaaaaaa'


def test_an_unknown_session_or_project_is_an_error_with_what_exists(world, capsys):
    ambiguous_world(world)
    error = selection_error(world, capsys, '--day', DAY, '--session', 'zz')
    assert "--session 'zz' matches no session in this period" in error
    error = selection_error(world, capsys, '--day', DAY, '--project', 'gamma')
    assert "--project 'gamma' matches no project in this period (projects: beta, alpha)" in error
    assert "--session 'aaaaaaaa' matches no session in project beta" in selection_error(
        world, capsys, '--day', DAY, '--project', 'beta', '--session', 'aaaaaaaa')
    assert 'must not be empty' in selection_error(world, capsys, '--day', DAY, '--session', '')
    assert 'must not be empty' in selection_error(world, capsys, '--day', DAY, '--project', '')


def test_a_project_name_is_the_full_name_or_a_unique_start_of_it(world, capsys):
    ambiguous_world(world)
    for given in ('alpha', 'alph', 'a'):
        detail = world.overview('--day', DAY, '--project', given)
        assert [block[0].split()[0] for block in cli_blocks(detail)] == ['alpha']
        assert detail.splitlines()[0].endswith(' - project alpha')
    assert 'matches no project' in selection_error(world, capsys, '--day', DAY, '--project', 'Alpha')
    assert 'matches no project' in selection_error(world, capsys, '--day', DAY, '--project', 'lpha')


def test_an_ambiguous_start_of_a_project_name_names_the_candidates_and_the_full_name_wins(world, capsys):
    world.transcript('-p1', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'a', 1.0, cwd=home('shop'))])
    world.transcript('-p2', 'bbbbbbbb-2', [paid('2026-10-13T10:01:00Z', 'b', 2.0, cwd=home('shop-docs'))])
    world.transcript('-p3', 'cccccccc-3', [paid('2026-10-13T10:02:00Z', 'c', 3.0, cwd=home('shell'))])
    error = selection_error(world, capsys, '--day', DAY, '--project', 'sh')
    assert "--project 'sh' matches 3 projects: shell, shop-docs, shop - give more characters" in error
    assert 'Traceback' not in error
    assert 'matches 2 projects: shop-docs, shop' in selection_error(world, capsys, '--day', DAY, '--project', 'sho')
    # the whole name of a project is chosen even though it starts another one
    detail = world.overview('--day', DAY, '--project', 'shop')
    assert [block[0].split()[0] for block in cli_blocks(detail)] == ['shop']
    assert [block[0].split()[0] for block in cli_blocks(world.overview('--day', DAY, '--project', 'shop-'))] == ['shop-docs']


def test_in_an_empty_period_a_selection_finds_nothing(world, capsys):
    world.projects.mkdir()
    error = selection_error(world, capsys, '--day', DAY, '--project', 'x')
    assert 'matches no project in this period (projects: none)' in error


@pytest.mark.parametrize('args', [('--json', '--full'), ('--json', '--project', 'x'), ('--json', '--session', 'x'),
                                  ('--archive', '--full'), ('--archive', '--project', 'x'), ('--archive', '--session', 'x')])
def test_the_choices_that_do_not_go_together_are_bad_arguments(world, capsys, args):
    error = selection_error(world, capsys, *args)
    assert ('--json' in error and 'all projects and sessions' in error) or ('--archive' in error)


def test_the_new_options_are_in_the_help(capsys):
    with pytest.raises(SystemExit):
        report_module.main(['--help'])
    text = capsys.readouterr().out
    for option in ('--full', '--project', '--session'):
        assert option in text


# --- The full report: the old report with its corrections -------------------------------------------------------------

def readme_world(world):
    session = 'aaaaaaaa-1111'
    r1, week = T('2026-10-13T13:00:00Z'), T('2026-10-19T07:00:00Z')
    world.history([row('2026-10-13T10:00:00Z', 10, 20, r1, week), row('2026-10-13T10:10:00Z', 14, 21, r1, week)])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:05:00Z', 'a', 3.0, cwd='/home/u/Projekte/proj-a')])
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:06:00Z', 'b', 1.0)], sub='x1',
                     meta={'agentType': 'arbeiter-bauen'})
    world.guard_log([gate('2026-10-13T10:00:00Z', 'spawn_gate', 'shadow', 'allow'),
                     gate('2026-10-13T10:00:01Z', 'soft_stop', 'enforce', 'warn', 'Checkpoint due: save'),
                     gate('2026-10-13T10:00:02Z', 'soft_stop', 'enforce', 'deny', 'Checkpoint due: save'),
                     gate('2026-10-13T10:00:03Z', 'soft_stop', 'enforce', 'deny', 'Usage pause: 5h window at 90 %')])


FULL_REPORT = """\
Usage report 2026-10-13 (local time, UTC+02:00)
Sources: 2 transcript file(s) read

Subscription
  Week: 20 % -> 21 % (+1 points), resets Mon 19 Oct 09:00
  5h windows: 1
    reset 15:00, peak 14 % at Tue 13 Oct 12:10 (first seen 12:00)

Total
  Requests: 2 (main 1, subagents 1)
  Tokens: input 0, cache write 5m 0, cache write 1h 0, cache read 0, output 200k
  Cost weight: 4.00 USD at API prices (main 3.00, subagents 1.00)
  Output: thinking n/a, web searches n/a, web fetches n/a
  Cache: hit rate n/a, cold starts 0
  Main session: <1 pts ~3.0 pts 5h
  Subagents by type: arbeiter-bauen 1.00 USD (1) <1 pts ~1.0 pts 5h
  Models:
    model Opus 5.5: 4.00 USD, 2 requests; thinking n/a, web searches n/a, web fetches n/a; cache hits n/a

Startup context
  Measured, the first request of a session or subagent (input + cache write + cache read):
    main: 1 start, median 0 tokens, min 0, max 0, 0 warm (0 %)
    subagents by type:
      arbeiter-bauen: 1 start, median 0 tokens, min 0, max 0, 0 warm (0 %)
    per day:
      Tue 13 Oct: main 0 tokens (1 start), subagents 0 tokens (1 start)
  Estimate (~): not available, no folder of a start in this period exists any more

By CLI (main session and its subagents, most expensive first; ~ = estimate)
  proj-a  aaaaaaaa  started Tue 13 Oct 12:05  4.00 USD  2 requests  ~4.0 5h points  ~1.00 week points
    tokens input 0, cache write 5m 0, cache write 1h 0, cache read 0, output 200k
    models Opus 5.5 4.00
    output thinking n/a, web searches n/a, web fetches n/a
    subagents arbeiter-bauen 1.00 USD (1)
    cache hits n/a, cold starts 0

Allocation of the subscription rise (estimate)
  5h: 4 points counted, ~4.0 shared by cost between the CLIs, 0 unexplained (no request with a price in between)
      0 stretch(es) not counted (a reset, or over 60 minutes between measurements), 0 points rise in them
  week: 1 points counted, ~1.0 shared by cost between the CLIs, 0 unexplained (no request with a price in between)
      0 stretch(es) not counted (a reset, or over 60 minutes between measurements), 0 points rise in them

Calibration (USD per point, from stretches in which only one CLI was active)
  5h: not calibrated yet (1 of 50 stretches)
  week: not calibrated yet (1 of 50 stretches)

Gates
  spawn gate (shadow mode, only logged): allowed 1, warned 0, asked 0, denied 0
  soft stop (enforce mode): warnings 1, denied 2
      warned by reason: checkpoint due: 1
      denied by reason: checkpoint due: 1, usage pause: 1"""


def test_the_full_report_is_the_old_report_with_its_corrections(world):
    readme_world(world)
    assert world.overview('--day', DAY, '--full').rstrip('\n') == FULL_REPORT


def test_the_five_hour_windows_name_weekday_and_date_of_the_peak(world):
    readme_world(world)
    assert 'reset 15:00, peak 14 % at Tue 13 Oct 12:10 (first seen 12:00)' in world.text('--day', DAY)


def test_the_calibration_names_only_days_with_enough_stretches(world):
    rows = (steady_sections(world, 19, cost=2.0) + steady_sections(world, 20, cost=3.0, start='2026-10-14T08:00:00Z',
                                                                    session='bbbbbbbb-2', project='-home-u-proj-b')
            + steady_sections(world, 31, cost=4.0, start='2026-10-15T08:00:00Z', session='cccccccc-3',
                              project='-home-u-proj-c'))
    world.history(rows)
    text = world.text('--since', DAY, '--until', '2026-10-15')
    assert 'per day: 10-14 3.00 (20), 10-15 4.00 (31)' in text and '10-13' not in text.split('Calibration')[1]
    days = world.report('--since', DAY, '--until', '2026-10-15')['calibration']['five']['per_day']
    assert sorted(days) == ['2026-10-13', '2026-10-14', '2026-10-15']          # the JSON keeps every day with its count


def test_a_single_day_with_enough_stretches_is_not_listed_per_day(world):
    rows = steady_sections(world, 19, cost=2.0) + steady_sections(world, 40, cost=3.0, start='2026-10-14T08:00:00Z',
                                                                   session='bbbbbbbb-2', project='-home-u-proj-b')
    world.history(rows)
    assert 'per day' not in world.text('--since', DAY, '--until', '2026-10-14').split('Calibration')[1]


def test_the_gates_have_no_shadow_sentence_in_the_heading_and_name_the_mode_of_each(world):
    readme_world(world)
    text = world.text('--day', DAY)
    assert '\nGates\n' in text and 'nothing is blocked' not in text
    assert 'spawn gate (shadow mode, only logged): allowed 1' in text
    assert 'soft stop (enforce mode): warnings 1, denied 2' in text


# --- Reasons of the gates ---------------------------------------------------------------------------------------------

def test_the_json_counts_the_reasons_by_gate_mode_and_decision(world):
    readme_world(world)
    data = world.report('--day', DAY)
    assert data['guards']['soft_stop'] == {'enforce': {'warn': 1, 'deny': 2}}
    assert data['guard_reasons'] == {'soft_stop': {'enforce': {'warn': {'checkpoint due': 1},
                                                               'deny': {'checkpoint due': 1, 'usage pause': 1}}}}


def test_the_reasons_survive_the_archive(world):
    readme_world(world)
    live = world.report('--day', DAY)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    (world.log / 'log.jsonl').unlink()
    archived = world.report('--day', DAY)
    assert archived['coverage']['archive_days'] == [DAY]
    assert archived['guard_reasons'] == live['guard_reasons']
    assert archived['overview']['notable'] == live['overview']['notable']


def test_an_archive_line_without_reasons_still_merges_and_says_so(world):
    readme_world(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    path = world.log / ARCHIVE
    lines = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    for line in lines:
        del line['stats']['guard_reasons']
    path.write_text('\n'.join(json.dumps(line) for line in lines) + '\n', encoding='utf-8')
    for source in world.projects.rglob('*.jsonl'):
        source.unlink()
    (world.log / 'log.jsonl').write_text(json.dumps(gate('2026-10-14T08:00:00Z', 'soft_stop', 'enforce', 'deny',
                                                          'Checkpoint due: save')) + '\n', encoding='utf-8')
    data = world.report('--since', DAY, '--until', '2026-10-14')
    assert data['coverage']['archive_days'] == [DAY] and data['coverage']['unreadable_archive_lines'] == 0
    assert data['guards']['soft_stop']['enforce']['deny'] == 3
    assert data['overview']['notable'][0]['reasons'] == {'checkpoint due': 1, 'reason not recorded': 2}
    assert data['overview']['notable'][0]['text'] == (
        'soft stop denied 3 subagent tool calls (reason not recorded: 2, checkpoint due: 1)')


def test_an_ask_question_archived_by_version_1_4_0_counts_as_the_weekly_threshold(world):
    readme_world(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    path = world.log / ARCHIVE
    lines = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    for line in lines:
        line['stats']['guard_reasons']['spawn_gate'] = {'enforce': {'ask': {
            'wOCHE N % ? arbeiter-mechanisch starten?': 9, 'wEEK N % ? start x?': 1, '7-day window at N %': 5,
            'week N % ? start x?': 2}}}
    path.write_text('\n'.join(json.dumps(line) for line in lines) + '\n', encoding='utf-8')
    reasons = world.report('--day', DAY)['guard_reasons']['spawn_gate']['enforce']['ask']
    assert reasons == {'7-day window at N %': 15, 'week N % ? start x?': 2}


def test_gate_entries_from_the_future_or_other_hooks_carry_no_reasons(world):
    gates_world(world, [gate('2026-10-13T08:00:00Z', 'config', 'enforce', 'deny', 'x: y'),
                        gate('2026-10-13T08:00:00Z', 'soft_stop', 'enforce', 'allow', 'x: y'),
                        {'time': '2026-10-13T08:00:00Z', 'hook': 'soft_stop', 'mode': 'enforce', 'decision': 'deny'}])
    assert world.report('--day', DAY)['guard_reasons'] == {'soft_stop': {'enforce': {'deny': {'no reason': 1}}}}


# --- The overview: the headline and the edges -------------------------------------------------------------------------

def test_the_headline_of_a_period_with_a_weekly_reset_adds_the_rises_of_both_windows(world):
    old, new = T('2026-10-13T12:00:00Z'), T('2026-10-20T12:00:00Z')
    five = T('2026-10-13T17:00:00Z')
    world.history([row('2026-10-13T08:00:00Z', 5, 90, five, old), row('2026-10-13T11:00:00Z', 20, 100, five, old),
                   row('2026-10-13T12:30:00Z', 1, 0, five, new), row('2026-10-13T15:00:00Z', 9, 12, five, new)])
    spend(world, ('2026-10-13T10:00:00Z', 2.0))
    assert world.overview('--day', DAY).splitlines()[0] == (
        'Day 2026-10-13 · weekly limit at 12 % now · +22 points over 2 weekly windows · 2.0 USD at API prices')
    assert world.report('--day', DAY)['overview']['week'] == {'used_percent': 12, 'rise_points': 22, 'windows': 2,
                                                              'reset_at': new}


def test_a_weekly_value_without_a_reset_time_has_no_reset_in_the_overview(world):
    world.history([row('2026-10-13T08:00:00Z', 5, 50), row('2026-10-13T09:00:00Z', 6, 60.5)])
    spend(world, ('2026-10-13T08:30:00Z', 2.0))
    first = world.overview('--day', DAY).splitlines()[0]
    assert first.startswith('Day 2026-10-13 · 60.5 % of the weekly limit used (+10.5 points)')
    assert world.report('--day', DAY)['overview']['week']['reset_at'] is None


def test_a_model_without_a_price_is_counted_in_the_models_line_of_the_overview(world):
    world.transcript('-p', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'a', 2.0, cwd=home('alpha')),
                                          req('2026-10-13T10:01:00Z', 'm2', model='claude-mystery-9', out=10, request='r2',
                                              cwd=home('alpha'))])
    assert 'Opus 2.0 USD · 1 request(s) without a price · thinking n/a · cache hits n/a' in world.overview('--day', DAY)
    assert world.report('--day', DAY)['overview']['models']['unpriced_requests'] == 1


def test_without_any_price_there_are_no_shares(world):
    for number in range(6):
        world.transcript('-p', f'{number:08d}-s', [req(f'2026-10-13T10:0{number}:00Z', f'm{number}', out=10,
                                                       model='claude-mystery-9', request=f'r{number}',
                                                       cwd=home(f'proj{number}'))])
    view = world.report('--day', DAY)['overview']
    assert all(p['share'] is None for p in view['projects']) and view['more']['share'] is None
    text = world.overview('--day', DAY)
    assert text.count('n/a %') == 0 and text.count('  n/a') >= 6 and 'Notable' not in text
    assert 'Opus' not in text and text.splitlines()[0].endswith('0.00 USD at API prices')


def test_the_subagent_line_names_the_most_expensive_type_only_when_there_is_one(world):
    spend(world, ('2026-10-13T10:00:00Z', 2.0))
    assert 'Main 2.0 USD · subagents 0.00 USD\n' in world.overview('--day', DAY)
    assert world.report('--day', DAY)['overview']['split']['top_subagent'] is None
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1',
                     [paid('2026-10-13T10:01:00Z', 'x', 1.0), paid('2026-10-13T10:02:00Z', 'y', 3.0)],
                     sub='s1', meta={'agentType': 'big'})
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [paid('2026-10-13T10:03:00Z', 'z', 1.0)], sub='s2',
                     meta={'agentType': 'small'})
    assert 'Main 2.0 USD · subagents 5.0 USD (top: big 4.0 USD, 0 week pts)\n' in world.overview('--day', DAY)


def test_a_thinking_share_of_the_whole_output_is_a_hundred_percent(world):
    line = extras(paid('2026-10-13T10:00:00Z', 'a', 2.0), thinking=1_000_000)
    world.transcript('-p', 'aaaaaaaa-1', [line])
    assert 'thinking 100 % of output' in world.overview('--day', DAY)


# --- Long project names ----------------------------------------------------------------------------------------------------

def table_rows(text):
    return text.split('Where it went', 1)[1].split('\n\n', 1)[0].splitlines()[1:]


@pytest.mark.parametrize('name, shown', [
    ('a' * 30, 'a' * 30),                                                       # exactly 30 characters: whole
    ('abcdefghijklmnopqrstuvwxyz01234', 'abcdefghijklmno…rstuvwxyz01234'),       # 31: the start keeps the odd character
    ('my-company-frontend-admin-dashboard', 'my-company-fron…dmin-dashboard'),
    ('inox-hub-0016-stripe-checkout', 'inox-hub-0016-stripe-checkout'),          # seen 14 Oct: 29 fits
    ('x' * 64, 'x' * 15 + '…' + 'x' * 14),                                       # 64 is the longest name the report keeps
    ('short', 'short')])
def test_a_project_name_longer_than_30_characters_loses_its_middle_in_the_table(world, name, shown):
    world.transcript('-p', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'a', 2.0, cwd=home(name))])
    rows = table_rows(world.overview('--day', DAY))
    assert rows[0].split()[0] == shown and len(shown) <= 30 and len(rows) == 1
    assert rows[0].startswith('  ' + shown + ' ')
    # everywhere else the name is whole
    data = world.report('--day', DAY)
    assert data['overview']['projects'][0]['name'] == name and data['clis'][0]['label'] == name
    assert f'  {name}  aaaaaaaa  started' in world.text('--day', DAY)
    assert f'Details: --project {name} ' in world.overview('--day', DAY)


def test_the_table_columns_stay_aligned_with_a_shortened_name(world):
    world.transcript('-p1', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'a', 5.0, cwd=home('a' * 60))])
    world.transcript('-p2', 'bbbbbbbb-2', [paid('2026-10-13T10:01:00Z', 'b', 1.0, cwd=home('b'))])
    lines = world.overview('--day', DAY).splitlines()
    assert lines[3].index('USD') + 3 == lines[4].index('5.0') + 3 == lines[5].index('1.0') + 3


def test_two_projects_with_the_same_short_form_are_both_shown_whole(world):
    first, second = 'my-company-frontend-dashboard-one', 'my-company-frontend-legacy-d-old-dashboard-one'
    assert report_module._shorten(first) == report_module._shorten(second)
    world.transcript('-p1', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'a', 5.0, cwd=home(first))])
    world.transcript('-p2', 'bbbbbbbb-2', [paid('2026-10-13T10:01:00Z', 'b', 1.0, cwd=home(second))])
    world.transcript('-p3', 'cccccccc-3', [paid('2026-10-13T10:02:00Z', 'c', 0.5, cwd=home('x' * 36))])
    rows = table_rows(world.overview('--day', DAY))
    assert [row.split()[0] for row in rows] == [first, second, 'x' * 15 + '…' + 'x' * 14]


def test_the_short_forms_of_a_list_of_names(world):
    assert report_module.short_names(['x' * 36, 'ab', 'x' * 36 + 'y']) == [
        'x' * 15 + '…' + 'x' * 14, 'ab', 'x' * 15 + '…' + 'x' * 13 + 'y']
    assert report_module.short_names(['x' * 36, 'x' * 37]) == ['x' * 36, 'x' * 37]
    assert report_module.short_names([]) == []


# --- Edges found by the mutation probe -------------------------------------------------------------------------------------

def test_the_cache_hit_rate_of_a_project_counts_all_its_sessions(world):
    world.transcript('-p1', 'aaaaaaaa-1', [req('2026-10-13T10:00:00Z', 'a', inp=100, cr=900, request='ra', cwd=home('alpha'),
                                               out=dollars(2.0))])
    world.transcript('-p2', 'bbbbbbbb-2', [req('2026-10-13T10:01:00Z', 'b', inp=900, cr=100, request='rb', cwd=home('alpha'),
                                               out=dollars(1.0))])
    first = world.report('--day', DAY)['overview']['projects'][0]
    assert first['sessions'] == 2 and first['cache_hit_rate'] == pytest.approx(0.5)
    assert 'cache hits under 90 %: alpha 50 %' in world.overview('--day', DAY)


def test_the_line_for_the_rest_adds_up_the_sessions_of_the_projects_in_it(world):
    for number in range(8):
        for session in range(2):
            world.transcript('-p', f'{number}{session}000000-s', [paid(f'2026-10-13T{10 + number}:0{session}:00Z',
                                                                       f'm{number}{session}', 8.0 - number,
                                                                       cwd=home(f'proj{number}'))])
    view = world.report('--day', DAY)['overview']
    assert view['more']['projects'] == 3 and view['more']['sessions'] == 6 and view['projects'][5]['sessions'] == 2
    assert next(line for line in world.overview('--day', DAY).splitlines() if ' more ' in line).split()[-1] == '6'


def test_a_share_of_exactly_one_percent_is_shown_as_one_percent(world):
    two_projects(world, 1.0, 99.0)
    rows = world.overview('--day', DAY).split('Where it went', 1)[1].split('\n\n', 1)[0].splitlines()[1:]
    assert [row.split()[-4:-2] for row in rows] == [['99', '%'], ['1', '%']]


def test_a_cli_without_any_measurement_shows_a_dash_not_zero(world):
    spend(world, ('2026-10-13T10:00:00Z', 2.0))
    assert '  2.00 USD  1 requests  - 5h points  - week points' in world.text('--day', DAY)


def test_exactly_one_unrecorded_decision_is_named(world):
    readme_world(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    path = world.log / ARCHIVE
    lines = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    for line in lines:
        line['stats']['guard_reasons']['soft_stop']['enforce']['deny'] = {'checkpoint due': 1}      # of 2 denials
    path.write_text('\n'.join(json.dumps(line) for line in lines) + '\n', encoding='utf-8')
    notes = world.report('--day', DAY)['overview']['notable']
    assert notes[0]['reasons'] == {'checkpoint due': 1, 'reason not recorded': 1}


def test_exactly_three_reasons_are_all_named_and_a_fourth_is_other(world):
    names = ['Alpha', 'Beta', 'Gamma']
    gates_world(world, [gate('2026-10-13T08:00:00Z', 'soft_stop', 'enforce', 'deny', f'{name}: x') for name in names])
    assert notable(world, '--day', DAY)[0] == 'soft stop denied 3 subagent tool calls (alpha: 1, beta: 1, gamma: 1)'
    gates_world(world, [gate('2026-10-13T08:00:00Z', 'soft_stop', 'enforce', 'deny', f'{name}: x')
                        for name in names + ['Delta']])
    assert notable(world, '--day', DAY)[0] == 'soft stop denied 4 subagent tool calls (alpha: 1, beta: 1, delta: 1, other: 1)'


def test_the_highest_window_is_the_one_with_the_highest_peak(world):
    window_world(world, [76, 90, 80])
    assert world.report('--day', DAY)['overview']['five_hour'] == {'windows': 3, 'highest_percent': 90,
                                                                  'highest_at': '2026-10-13T14:30:00Z'}
    assert '5h windows: 3, highest 90 % (Tue 13 Oct 16:30)' in world.overview('--day', DAY)


def test_every_unpriced_request_is_counted(world):
    world.transcript('-p', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'a', 2.0, cwd=home('alpha'))]
                     + [req(f'2026-10-13T10:0{n}:00Z', f'm{n}', model='claude-mystery-9', out=10, request=f'r{n}',
                            cwd=home('alpha')) for n in (1, 2)])
    assert world.report('--day', DAY)['overview']['models']['unpriced_requests'] == 2
    assert '2 request(s) without a price' in world.overview('--day', DAY)


def test_the_details_line_suggests_the_largest_session_of_the_largest_project(world):
    world.transcript('-p1', 'aaaaaaaa-1', [paid('2026-10-13T10:00:00Z', 'a', 1.0, cwd=home('alpha'))])
    world.transcript('-p2', 'bbbbbbbb-2', [paid('2026-10-13T10:01:00Z', 'b', 5.0, cwd=home('alpha'))])
    world.transcript('-p3', 'cccccccc-3', [paid('2026-10-13T10:02:00Z', 'c', 4.0, cwd=home('beta'))])
    assert world.report('--day', DAY)['overview']['details'] == {'project': 'alpha', 'session': 'bbbbbbbb'}
    assert world.overview('--day', DAY).endswith('Details: --project alpha · --session bbbbbbbb · --full\n')


# --- The format document -------------------------------------------------------------------------------------------------

REPORT_FORMAT = os.path.join(ROOT, 'docs', 'report-format.md')
DATA_KEYED = {'.total.models', '.total.unpriced', '.total.subagents.by_type', '.startup.subagents.by_type', '.clis[].models', '.clis[].subagents',
              '.calibration.five.per_day', '.calibration.week.per_day', '.calibration.five.per_model',
              '.calibration.week.per_model', '.guards', '.guard_reasons', '.overview.notable[].reasons'}


def json_keys(value, path=''):
    """The field names of a report: where a dict is keyed by data (model names, days, reasons) the key is not a field."""
    if isinstance(value, dict):
        for key, child in value.items():
            fixed = path not in DATA_KEYED and not re.match(r'\.guards(\.[\w*]){0,2}$|\.guard_reasons(\.[\w*]){0,3}$', path)
            if fixed:
                yield key
            yield from json_keys(child, f'{path}.{key if fixed else "*"}')
    elif isinstance(value, list):
        for child in value:
            yield from json_keys(child, path + '[]')


def test_the_format_document_explains_every_json_field_and_every_line_of_the_overview(world):
    overview_world(world)
    world.transcript('-p', 'iiiiiiii-9', [req('2026-10-13T12:40:00Z', 'u1', model='claude-mystery-9', out=10, request='ru',
                                               cwd=home('0054'))])
    world.history([row(f'2026-10-13T{clock}:00Z', five, week, FIVE_RESET, WEEK_RESET) for clock, five, week in
                   (('10:00', 10, 0), ('10:30', 60, 20), ('11:00', 60, 20), ('11:30', 80, 38), ('12:00', 80, 38),
                    ('12:30', 82, 39), ('13:00', 83, 41))])
    data = world.report('--week')
    assert {n['rule'] for n in data['overview']['notable']} == {
        'gate_denied', 'gate_asked', 'five_hour_window', 'cache_hits_low', 'project_dominates', 'unexplained_rise'}
    assert data['total']['unpriced'] and data['overview']['more']
    with open(REPORT_FORMAT, encoding='utf-8') as f:
        document = f.read()
    keys = set(json_keys(data))
    assert {'schema_version', 'overview', 'guard_reasons', 'project', 'session_ids', 'unpriced_requests', 'rise_points'} <= keys
    assert [key for key in sorted(keys) if f'`{key}`' not in document] == []
    for needle in ('Where it went', '`~`', '~week pts', 'sessions', 'N more', 'Main', 'top:', 'of output', 'cache hits',
                   'Notable', 'Details:', '5h windows', 'at API prices', 'weekly limit used', 'no weekly measurement',
                   'none measured', 'without a price', 'unexplained rise', 'cache hits under 90 %', 'would have denied',
                   'shadow mode, nothing was blocked', 'reason not recorded', '`<1`', '--project', '--session', '--full',
                   '--json', '30 characters', 'same name in different places', 'thresholds.start_block_5h', 'gate_denied', 'gate_asked',
                   'five_hour_window', 'cache_hits_low', 'project_dominates', 'unexplained_rise', 'not your bill',
                   'measured', 'estimate', 'point'):
        assert needle in document, needle


# --- Findings of the independent review (14 Oct 2026) ------------------------------------------------------------------

@pytest.mark.parametrize('reason, kind', [
    ('Usage stale and 3 agents in the block: wait', 'usage stale and N agents in the block'),
    ('Usage stale and 12 agents in the block: wait', 'usage stale and N agents in the block'),
    ('5h window at 78 %: above', '5h window at N %'),
    ('7-day window at 85.5%: above', '7-day window at N %'),
    ('WOCHE 92 % · arbeiter-mechanisch starten?', '7-day window at N %'),      # the ask dialog, one kind per language
    ('WEEK 93 % · start workflow review?', '7-day window at N %'),               # and whatever starts
    ('Week 93 % · start x?', 'week N % ? start x?'),                              # only the gate's own label
    ('Agent "secret-name" has no model: x', 'agent … has no model'),
    ("Skill 'x y' forks: z", 'skill … forks'),
    ('Checkpoint due: 15 calls: since your last', 'checkpoint due'),          # only the text before the first colon
    ('Rule v2.1 and retry-3 failed: x', 'rule v2.1 and retry-3 failed'),             # parts of words stay
    ('', 'no reason')])
def test_the_kind_of_a_reason_drops_numbers_and_quoted_names(reason, kind):
    assert report_module.reason_kind(reason) == kind


def test_full_does_not_combine_with_a_selection(world, capsys):
    spend(world, ('2026-10-13T10:00:00Z', 1.0))
    with pytest.raises(SystemExit) as stop:
        report_module.main(world.args('--day', DAY, '--project', 'proj-a', '--full'), now=NOW)
    assert stop.value.code == 2 and '--full shows everything' in capsys.readouterr().err


def test_a_day_with_enough_stretches_but_no_rise_has_no_calibration_value(world):
    # 25 single-CLI stretches of 10 minutes, the weekly and 5h values never move: no USD per point for that day
    rows = [row(f'2026-10-13T{8 + i // 6:02d}:{i % 6 * 10:02d}:00Z', 10, 20) for i in range(26)]
    world.history(rows)
    spend(world, *[(f'2026-10-13T{8 + i // 6:02d}:{i % 6 * 10 + 5:02d}:00Z', 1.0) for i in range(25)])
    data = world.report('--day', DAY)
    assert data['calibration']['five']['per_day']['2026-10-13']['usd_per_point'] is None
    assert data['calibration']['five']['per_day']['2026-10-13']['sections'] >= 20
    assert 'per day' not in world.text('--day', DAY).split('Calibration', 1)[1].split('\n\n')[0]



# --- Points by agent type and the year --------------------------------------------------------------------------------

def agents_world(world):
    """One stretch (10:00 to 10:10, 5h 10 -> 18, week 20 -> 24) with two sessions: main 2 USD, subagents of type
    arbeiter-bauen 3 + 1 USD, architekt 1 USD and one of unknown type 1 USD; a stretch after it without any request."""
    world.history([row('2026-10-13T10:00:00Z', 10, 20), row('2026-10-13T10:10:00Z', 18, 24),
                   row('2026-10-13T10:20:00Z', 20, 25)])
    world.transcript('-p-a', 'aaaaaaaa-1', [paid('2026-10-13T10:02:00Z', 'a1', 2.0)])
    world.transcript('-p-a', 'aaaaaaaa-1', [paid('2026-10-13T10:03:00Z', 'a2', 3.0)], sub='x1',
                     meta={'agentType': 'arbeiter-bauen'})
    world.transcript('-p-a', 'aaaaaaaa-1', [paid('2026-10-13T10:04:00Z', 'a3', 1.0)], sub='x2',
                     meta={'agentType': 'architekt'})
    world.transcript('-p-b', 'bbbbbbbb-2', [paid('2026-10-13T10:05:00Z', 'b1', 1.0, cwd='/home/u/proj-b')], sub='y1',
                     meta={'agentType': 'arbeiter-bauen'})
    world.transcript('-p-b', 'bbbbbbbb-2', [paid('2026-10-13T10:06:00Z', 'b2', 1.0, cwd='/home/u/proj-b')], sub='y2')


def split_points(data, metric):
    """(main, {type: points}) of the report for 'five' or 'week'."""
    key = metric + '_points'
    return (data['total']['main'][key],
            {kind: entry[key] for kind, entry in data['total']['subagents']['by_type'].items()})


def test_the_points_of_a_stretch_are_shared_between_main_and_the_agent_types_by_cost(world):
    agents_world(world)
    data = world.report('--day', DAY)
    main, types = split_points(data, 'five')
    assert main == pytest.approx(2.0)      # rise 8 points, total cost 8 USD: one point per USD
    assert types == {'arbeiter-bauen': pytest.approx(4.0), 'architekt': pytest.approx(1.0),
                     'unknown': pytest.approx(1.0)}
    main, types = split_points(data, 'week')
    assert main == pytest.approx(1.0)      # rise 4 points: half a point per USD
    assert types == {'arbeiter-bauen': pytest.approx(2.0), 'architekt': pytest.approx(0.5),
                     'unknown': pytest.approx(0.5)}
    # the types stay in the order of the cost, ties by name
    assert list(data['total']['subagents']['by_type']) == ['arbeiter-bauen', 'architekt', 'unknown']


def test_main_and_all_types_add_up_to_the_attributed_points(world):
    agents_world(world)
    data = world.report('--day', DAY)
    for metric in ('five', 'week'):
        main, types = split_points(data, metric)
        attributed = data['allocation'][metric]['attributed_points']
        assert round(main + sum(types.values()), 2) == round(attributed, 2)
        assert data['allocation'][metric]['unexplained_points'] > 0       # the last stretch had no request
        assert sum(c['estimated_share'][metric] for c in data['clis']) == pytest.approx(main + sum(types.values()))


def test_two_types_in_one_stretch_get_the_points_in_the_ratio_of_their_cost(world):
    world.history([row('2026-10-13T10:00:00Z', 0, 0), row('2026-10-13T10:10:00Z', 30, 9)])
    session = 'aaaaaaaa-1'
    world.transcript('-p-a', session, [paid('2026-10-13T10:02:00Z', 'm1', 1.0)], sub='x1', meta={'agentType': 'big'})
    world.transcript('-p-a', session, [paid('2026-10-13T10:03:00Z', 'm2', 2.0), paid('2026-10-13T10:04:00Z', 'm3', 1.0)],
                     sub='x2', meta={'agentType': 'small'})
    data = world.report('--day', DAY)
    _, week = split_points(data, 'week')
    _, five = split_points(data, 'five')
    assert week == {'small': pytest.approx(9 * 3 / 4), 'big': pytest.approx(9 / 4)}
    assert five == {'small': pytest.approx(30 * 3 / 4), 'big': pytest.approx(30 / 4)}
    assert split_points(data, 'week')[0] == 0 and split_points(data, 'five')[0] == 0       # no main request


def test_a_type_can_have_points_without_requests_in_the_period(world):
    # the stretch begins before midnight: its subagent request lies on the day before, its points fall to this day
    world.history([row('2026-10-12T21:50:00Z', 10, 20), row('2026-10-12T22:10:00Z', 14, 22)])
    world.transcript('-p-a', 'aaaaaaaa-1', [paid('2026-10-12T22:05:00Z', 'a1', 1.0)])
    world.transcript('-p-a', 'aaaaaaaa-1', [paid('2026-10-12T21:55:00Z', 'a2', 1.0)], sub='x1',
                     meta={'agentType': 'architekt'})
    data = world.report('--day', DAY)
    assert data['total']['subagents']['by_type'] == {
        'architekt': {'requests': 0, 'cost_usd': 0.0, 'week_points': pytest.approx(1.0),
                      'five_points': pytest.approx(2.0)}}
    assert split_points(data, 'week')[0] == pytest.approx(1.0)
    assert 'architekt 0.00 USD (0) ~1.00 pts ~2.0 pts 5h' in world.text('--day', DAY)


def test_a_stretch_without_a_price_or_without_a_request_gives_no_agent_a_point(world):
    world.history([row('2026-10-13T10:00:00Z', 10, 20), row('2026-10-13T10:10:00Z', 14, 22),
                   row('2026-10-13T10:20:00Z', 16, 23)])
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T10:12:00Z', 'u1', model='claude-mystery-9', out=1000)],
                     sub='x1', meta={'agentType': 'architekt'})
    data = world.report('--day', DAY)
    assert data['total']['main']['week_points'] == 0 and data['total']['main']['five_points'] == 0
    assert data['total']['subagents']['by_type'] == {
        'architekt': {'requests': 1, 'cost_usd': 0.0, 'week_points': 0, 'five_points': 0}}
    assert data['allocation']['week']['attributed_points'] == 0 and data['allocation']['week']['unexplained_points'] == 3


def test_the_full_text_gives_the_points_of_main_and_of_each_type(world):
    agents_world(world)
    text = world.text('--day', DAY)
    assert '  Main session: ~1.00 pts ~2.0 pts 5h\n' in text
    assert ('  Subagents by type: arbeiter-bauen 4.00 USD (2) ~2.00 pts ~4.0 pts 5h, '
            'architekt 1.00 USD (1) <1 pts ~1.0 pts 5h, unknown 1.00 USD (1) <1 pts ~1.0 pts 5h\n') in text
    assert text.index('Main session:') < text.index('Subagents by type:')
    # the line of the sessions further down has no points for the types
    assert '    subagents arbeiter-bauen 3.00 USD (1), architekt 1.00 USD (1)' in text


def test_small_points_of_an_agent_type_are_written_below_one(world):
    world.history([row('2026-10-13T10:00:00Z', 0, 0), row('2026-10-13T10:10:00Z', 10, 1)])
    world.transcript('-p-a', 'aaaaaaaa-1', [paid('2026-10-13T10:02:00Z', 'a1', 1.0)], sub='x1',
                     meta={'agentType': 'tiny'})
    world.transcript('-p-a', 'aaaaaaaa-1', [paid('2026-10-13T10:03:00Z', 'a2', 9.0)])
    text = world.text('--day', DAY)
    assert 'Subagents by type: tiny 1.00 USD (1) <1 pts ~1.0 pts 5h' in text
    assert 'Main session: <1 pts ~9.0 pts 5h' in text


def test_a_day_without_subagents_has_the_main_line_and_no_type_line(world):
    world.history([row('2026-10-13T10:00:00Z', 0, 0), row('2026-10-13T10:10:00Z', 4, 2)])
    spend(world, ('2026-10-13T10:05:00Z', 1.0))
    text = world.text('--day', DAY)
    assert 'Main session: ~2.00 pts ~4.0 pts 5h' in text and 'Subagents by type' not in text


def test_no_agent_name_reaches_the_points(world):
    agents_world(world)
    world.transcript('-p-a', 'aaaaaaaa-1', [paid('2026-10-13T10:07:00Z', 'a9', 1.0)], sub='x9',
                     meta={'agentType': f'my type {SECRET}', 'name': SECRET})
    data = world.report('--day', DAY)
    assert 'other' in data['total']['subagents']['by_type'] and SECRET not in json.dumps(data)
    assert SECRET not in world.text('--day', DAY)


def test_the_points_of_the_types_survive_the_archive(world):
    agents_world(world)
    live = world.report('--day', DAY)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    archived = world.report('--day', DAY)
    assert archived['coverage']['archive_days'] == [DAY]
    assert archived['coverage']['archive_days_without_agent_split'] == []
    assert archived['total'] == live['total']


def test_two_archived_days_add_up_their_points(world):
    agents_world(world)
    world.history([row('2026-10-13T10:00:00Z', 10, 20), row('2026-10-13T10:10:00Z', 18, 24),
                   row('2026-10-14T10:00:00Z', 20, 25), row('2026-10-14T10:10:00Z', 22, 26)])
    world.transcript('-p-a', 'aaaaaaaa-3', [paid('2026-10-14T10:05:00Z', 'c1', 1.0)], sub='z1',
                     meta={'agentType': 'architekt'})
    one = world.report('--since', '2026-10-13', '--until', '2026-10-14', now=T('2026-10-15T12:00:00Z'))
    assert report_module.main(world.args('--archive'), now=T('2026-10-15T12:00:00Z')) == 0
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    two = world.report('--since', '2026-10-13', '--until', '2026-10-14', now=T('2026-10-15T12:00:00Z'))
    assert two['total'] == one['total']
    assert one['total']['subagents']['by_type']['architekt']['week_points'] == pytest.approx(0.5 + 1.0)


def strip_points(world):
    path = world.log / ARCHIVE
    lines = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    for line in lines:
        del line['stats']['points']
    path.write_text('\n'.join(json.dumps(line) for line in lines) + '\n', encoding='utf-8')


def test_a_day_archived_without_points_is_split_from_the_transcripts_while_they_exist(world):
    agents_world(world)
    live = world.report('--day', DAY)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    strip_points(world)
    data = world.report('--day', DAY)
    assert data['coverage']['archive_days'] == [DAY] and data['coverage']['archive_days_without_agent_split'] == []
    assert data['total'] == live['total']


def test_a_day_archived_without_points_and_without_transcripts_is_named(world):
    agents_world(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    strip_points(world)
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    data = world.report('--day', DAY)
    assert data['coverage']['archive_days_without_agent_split'] == [DAY]
    assert data['total']['main']['week_points'] == 0 and data['total']['main']['five_points'] == 0
    assert {kind: (v['week_points'], v['five_points']) for kind, v in data['total']['subagents']['by_type'].items()} == {
        'arbeiter-bauen': (0, 0), 'architekt': (0, 0), 'unknown': (0, 0)}      # the requests are in the archive
    assert data['allocation']['week']['attributed_points'] > 0       # the rise is there, only its split is not
    text = world.text('--day', DAY)
    assert f'1 archived day(s) without the split of points by agent type: {DAY}' in text
    assert 'without the split' not in world.text('--day', '2026-10-01')


def test_a_day_archived_without_points_is_named_when_a_stretch_of_it_has_lost_its_transcripts(world):
    agents_world(world)
    world.transcript('-p-c', 'cccccccc-3', [paid('2026-10-13T10:15:00Z', 'c1', 2.0, cwd='/home/u/proj-c')])
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    strip_points(world)
    (world.projects / '-p-c' / 'cccccccc-3.jsonl').unlink()
    data = world.report('--day', DAY)
    assert data['coverage']['archive_days_without_agent_split'] == [DAY]      # less than was attributed: not whole
    main, types = split_points(data, 'week')
    assert main + sum(types.values()) == pytest.approx(4.0) and data['allocation']['week']['attributed_points'] == 5


@pytest.mark.parametrize('points', [{'main': 5}, {'types': {'x': {'five': 'a'}}}, [1], {'types': 3}])
def test_an_archive_line_with_malformed_points_is_not_an_archive_line(world, points):
    agents_world(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    path = world.log / ARCHIVE
    lines = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    for line in lines:
        line['stats']['points'] = points
    path.write_text('\n'.join(json.dumps(line) for line in lines) + '\n', encoding='utf-8')
    data = world.report('--day', DAY)
    assert data['coverage']['archive_days'] == [] and data['coverage']['unreadable_archive_lines'] == 1
    assert data['total']['subagents']['by_type']['arbeiter-bauen']['week_points'] == pytest.approx(2.0)


def test_the_overview_names_the_week_points_of_the_top_type_and_stays_short(world):
    overview_world(world)
    text = world.overview('--week')
    assert 'Main 75 USD · subagents 25 USD (top: stripe-meister 20 USD, ~6.6 week pts)' in text
    assert len(text.splitlines()) <= 25
    data = world.report('--week')
    top = data['overview']['split']['top_subagent']
    assert top == {'type': 'stripe-meister', 'cost_usd': pytest.approx(20), 'week_points': pytest.approx(20 * 20 / 60.276)}
    assert top['week_points'] == data['total']['subagents']['by_type']['stripe-meister']['week_points']
    assert report_module.render_overview(json.loads(json.dumps(data))) + '\n' == text


@pytest.mark.parametrize('points, expected', [(0.0, '0 week pts'), (0.4, '<1 week pts'), (12.34, '~12.3 week pts')])
def test_the_top_type_of_the_overview_says_its_small_points_like_the_table(world, points, expected):
    overview_world(world)
    view = world.report('--week')
    view['overview']['split']['top_subagent']['week_points'] = points
    assert f'(top: stripe-meister 20 USD, {expected})' in report_module.render_overview(view)


def test_the_overview_without_subagents_has_no_top_type(world):
    spend(world, ('2026-10-13T10:00:00Z', 1.0))
    assert 'top:' not in world.overview('--day', DAY)


def test_the_overview_of_a_week_with_many_types_is_one_line_longer_for_nobody(world):
    overview_world(world)
    for number in range(12):
        world.transcript('-p16', 'aaaaaaaa-1', [paid(f'2026-10-13T10:2{number % 10}:30Z', f'z{number}', 0.1,
                                                      cwd=home('0016'))], sub=f't{number}',
                         meta={'agentType': f'type-{number}'})
    text = world.overview('--week')
    assert len(text.splitlines()) <= 25 and text.count('top:') == 1


# --year

def year_world(world):
    """March and October of 2026: the day in March only in the archive, 12 and 13 October in the archive and in the
    transcripts, today (14 October) and the days of the year before and after only in the transcripts."""
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [
        paid('2025-12-31T22:59:59Z', 'y0', 100.0), paid('2025-12-31T23:00:00Z', 'y1', 1.0),
        paid('2026-03-05T10:00:00Z', 'y2', 2.0), paid('2026-10-12T10:00:00Z', 'y3', 4.0),
        paid('2026-10-13T10:00:00Z', 'y4', 8.0)])
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    world.transcript('-home-u-proj-a', 'aaaaaaaa-1', [
        paid('2026-10-13T10:00:00Z', 'y4', 8.0), paid('2026-10-14T21:59:59Z', 'y5', 16.0),
        paid('2026-10-14T22:00:00Z', 'y6', 200.0)])


def test_the_year_is_the_days_of_that_year_up_to_today_from_the_archive_and_the_transcripts(world):
    year_world(world)
    data = world.report('--year', '2026')
    assert data['period'] == {'kind': 'year', 'label': '2026', 'start': iso(T('2025-12-31T23:00:00Z')),
                              'end': iso(T('2026-10-14T22:00:00Z'))}
    assert data['total']['cost_usd'] == pytest.approx(1 + 2 + 4 + 8 + 16)
    assert data['coverage']['archive_days'] == ['2026-01-01', '2026-03-05', '2026-10-12', '2026-10-13']
    assert data['overview']['title'] == 'Year 2026'
    assert data['total']['requests'] == 5


def test_the_year_without_a_value_is_this_year(world):
    year_world(world)
    assert world.report('--year') == world.report('--year', '2026')
    assert world.overview('--year').startswith('Year 2026 · ')


def test_a_year_that_has_passed_is_whole_and_one_to_come_is_empty(world):
    year_world(world)
    before = world.report('--year', '2025')
    assert before['total']['cost_usd'] == pytest.approx(100.0) and before['period']['end'] == iso(
        T('2025-12-31T23:00:00Z'))
    later = world.report('--year', '2027')
    assert later['total']['requests'] == 0 and later['period']['label'] == '2027'
    assert later['period']['end'] == iso(T('2027-12-31T23:00:00Z'))


def test_the_year_is_the_same_as_since_and_until(world):
    year_world(world)
    year = world.report('--year', '2026')
    span = world.report('--since', '2026-01-01', '--until', '2026-10-14')
    assert year['total'] == span['total'] and year['coverage'] == span['coverage']
    assert year['period']['start'] == span['period']['start'] and year['period']['end'] == span['period']['end']
    old = world.report('--year', '2025')
    assert old['total'] == world.report('--since', '2025-01-01', '--until', '2025-12-31')['total']


def test_the_year_alone_in_the_archive_needs_no_transcripts(world):
    year_world(world)
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    data = world.report('--year', '2026')
    assert data['total']['cost_usd'] == pytest.approx(1 + 2 + 4 + 8) and data['coverage']['archive_days'] == [
        '2026-01-01', '2026-03-05', '2026-10-12', '2026-10-13']


@pytest.mark.parametrize('text', ['26', '20266', 'abcd', '1999', '3000', '2026-01', '-2026', '٢٠٢٦'])
def test_a_bad_year_is_an_error_with_exit_code_2(world, capsys, text):
    world.projects.mkdir()
    with pytest.raises(SystemExit) as stop:
        report_module.main(world.args('--year', text), now=NOW)
    assert stop.value.code == 2 and '--year must be a year like 2026, from 2000 to 2999' in capsys.readouterr().err


@pytest.mark.parametrize('other, message', [
    (('--day',), 'choose one of --day, --year'), (('--week',), 'choose one of --week, --year'),
    (('--month',), 'choose one of --month, --year'), (('--since', '2026-01-01'), 'choose one of --year, --since'),
    (('--archive',), '--archive takes no period')])
def test_the_year_does_not_combine_with_another_period(world, capsys, other, message):
    world.projects.mkdir()
    with pytest.raises(SystemExit) as stop:
        report_module.main(world.args('--year', '2026', *other), now=NOW)
    assert stop.value.code == 2 and message in capsys.readouterr().err


def test_until_still_needs_since_with_a_year(world, capsys):
    world.projects.mkdir()
    with pytest.raises(SystemExit) as stop:
        report_module.main(world.args('--year', '2026', '--until', '2026-02-01'), now=NOW)
    assert stop.value.code == 2 and '--until needs --since' in capsys.readouterr().err


def test_the_help_names_the_year(capsys):
    assert '--year' in report_module.build_parser().format_help()
