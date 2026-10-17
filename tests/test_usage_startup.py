"""Tests for the startup context of tools/usage_report.py: what a session or subagent reads before it does anything of its
own. Invented transcripts and file trees (October 2026); nothing here reads the real home directory.

The first request of a transcript is its start: the context it carries (input + cache write + cache read) is measured.
How that splits up (CLAUDE.md files, MEMORY.md, agent and skill descriptions) is an estimate from file sizes."""
import builtins
import json
import re
import shutil

import pytest

from test_usage_report import (ARCHIVE, DAY, NOW, REPORT_FORMAT, SECRET, home, json_keys, local_zone,  # noqa: F401
                               paid, report_module, req, world)


def startup_world(world):
    """Three main sessions (15 000 and 31 000 tokens on the 13th, 50 000 on the 14th) and four subagent starts."""
    alpha, beta = home('alpha'), home('beta')
    world.transcript('-p-a', 'aaaaaaaa-1', [
        req('2026-10-13T08:00:00Z', 'a1', inp=10, cw=10_000, cw1=4_990, out=1, request='r-a1', cwd=alpha),
        req('2026-10-13T08:00:01Z', 'a1', inp=10, cw=10_000, cw1=4_990, out=5, request='r-a1', cwd=alpha),
        req('2026-10-13T08:00:02Z', 'a1', inp=10, cw=10_000, cw1=4_990, out=9, request='r-a1', cwd=alpha),
        req('2026-10-13T08:05:00Z', 'a2', cr=14_000, cw=2_000, out=3, request='r-a2', cwd=alpha)])
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:10:00Z', 's1a', inp=1_000, cr=69_000, request='r-s1a',
                                                 cwd=alpha),
                                            req('2026-10-13T08:11:00Z', 's1b', cr=200_000, request='r-s1b', cwd=alpha)],
                     sub='s1', meta={'agentType': 'agent-a'})
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:20:00Z', 's2a', cw=90_000, request='r-s2a', cwd=alpha)],
                     sub='s2', meta={'agentType': 'agent-a'})
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:30:00Z', 's3a', cr=40_000, request='r-s3a', cwd=alpha)],
                     sub='s3', meta={'spawnedAgentType': 'agent-b'})
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:40:00Z', 's4a', inp=1_000, request='r-s4a', cwd=alpha)],
                     sub='s4')
    world.transcript('-p-b', 'bbbbbbbb-2', [req('2026-10-13T09:00:00Z', 'b1', inp=1_000, cr=30_000, request='r-b1',
                                                cwd=beta)])
    world.transcript('-p-a', 'cccccccc-3', [req('2026-10-14T09:00:00Z', 'c1', cw=50_000, request='r-c1', cwd=alpha)])


def startup(world, *args):
    return world.report(*args)['startup']


# --- Measured: the first request ---------------------------------------------------------------------------------------

def test_a_start_is_the_first_request_of_a_transcript_with_the_last_line_of_its_message(world):
    startup_world(world)
    starts = startup(world, '--day', DAY)['starts']
    assert starts[0] == {'at': '2026-10-13T08:00:02Z', 'session': 'aaaaaaaa', 'type': None, 'model': 'Opus 5.5',
                         'tokens': {'input': 10, 'cache_write': 14_990, 'cache_read': 0, 'total': 15_000}}
    # the later request of the main session (16 000 tokens) is no start, the second request of a subagent neither
    assert [s['tokens']['total'] for s in starts] == [15_000, 70_000, 90_000, 40_000, 1_000, 31_000]
    assert [s['at'] for s in starts] == sorted(s['at'] for s in starts)


def test_the_first_line_that_has_no_tokens_is_no_start(world):
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:00:00Z', 'a0', model='<synthetic>', request='r0'),
                                            req('2026-10-13T08:01:00Z', 'a1', cr=500, request='r1')])
    assert [s['tokens']['total'] for s in startup(world, '--day', DAY)['starts']] == [500]


def test_the_cache_write_of_a_start_adds_both_lifetimes(world):
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:00:00Z', 'a1', cw=7, cw1=5, cr=3, inp=1, request='r1')])
    assert startup(world, '--day', DAY)['starts'][0]['tokens'] == {'input': 1, 'cache_write': 12, 'cache_read': 3,
                                                                   'total': 16}


def test_main_sessions_and_subagent_types_are_summarised_by_median_minimum_maximum_and_warm_starts(world):
    startup_world(world)
    data = startup(world, '--since', DAY, '--until', '2026-10-14')
    assert data['main'] == {'starts': 3, 'median_tokens': 31_000, 'min_tokens': 15_000, 'max_tokens': 50_000,
                            'warm_starts': 1, 'warm_share': pytest.approx(1 / 3)}
    types = data['subagents']['by_type']
    assert list(types) == ['agent-a', 'agent-b', 'unknown']                  # most starts first, then by name
    assert types['agent-a'] == {'starts': 2, 'median_tokens': 80_000, 'min_tokens': 70_000, 'max_tokens': 90_000,
                                'warm_starts': 1, 'warm_share': 0.5}
    assert types['agent-b']['warm_starts'] == 1 and types['unknown']['warm_starts'] == 0
    assert data['subagents']['all'] == {'starts': 4, 'median_tokens': 55_000, 'min_tokens': 1_000, 'max_tokens': 90_000,
                                        'warm_starts': 2, 'warm_share': 0.5}


def test_a_median_of_two_values_is_their_mean_rounded(world):
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:00:00Z', 'a', inp=1, request='ra')])
    world.transcript('-p-a', 'bbbbbbbb-1', [req('2026-10-13T08:01:00Z', 'b', inp=2, request='rb')])
    assert startup(world, '--day', DAY)['main']['median_tokens'] == 2         # 1.5 rounds to the even number


def test_a_period_without_starts_has_none_of_the_groups(world):
    startup_world(world)
    assert startup(world, '--day', '2026-10-01') == {'starts': [], 'main': None,
                                                     'subagents': {'all': None, 'by_type': {}}, 'per_day': [],
                                                     'estimate': None}


def test_there_is_one_row_per_day_and_a_start_belongs_to_the_local_day(world):
    startup_world(world)
    world.transcript('-p-b', 'dddddddd-4', [req('2026-10-12T22:30:00Z', 'd1', inp=7, request='rd')])   # 00:30 on the 13th
    days = startup(world, '--since', '2026-10-12', '--until', '2026-10-14')['per_day']
    assert [d['day'] for d in days] == ['2026-10-13', '2026-10-14']
    assert days[0]['main']['starts'] == 3 and days[0]['main']['median_tokens'] == 15_000
    assert days[0]['subagents']['starts'] == 4 and days[0]['subagents']['median_tokens'] == 55_000
    assert days[1]['main']['starts'] == 1 and days[1]['subagents'] is None


def test_a_session_that_began_before_the_period_has_its_start_there(world):
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:00:00Z', 'a1', cw=100, request='r1'),
                                            req('2026-10-14T08:00:00Z', 'a2', cr=9_000, request='r2')])
    assert startup(world, '--day', '2026-10-14')['starts'] == []
    assert [s['tokens']['total'] for s in startup(world, '--day', DAY)['starts']] == [100]


def test_a_priced_model_has_its_name_and_an_unpriced_one_its_id(world):
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:00:00Z', 'a1', model='claude-sonnet-5-5', inp=5,
                                                request='r1')])
    world.transcript('-p-a', 'bbbbbbbb-1', [req('2026-10-13T08:01:00Z', 'b1', model='claude-mystery-9', inp=5,
                                                request='r2')])
    world.transcript('-p-a', 'cccccccc-1', [req('2026-10-13T08:02:00Z', 'c1', model={'x': 1}, inp=5, request='r3')])
    assert [s['model'] for s in startup(world, '--day', DAY)['starts']] == ['Sonnet 5.x', 'claude-mystery-9', '?']


@pytest.mark.parametrize('meta, expected', [
    ({'agentType': 'bau', 'name': 'bau', 'customAgentType': 'agent-c', 'spawnedAgentType': 'agent-s'}, 'agent-s'),
    ({'agentType': 'bau', 'name': 'bau', 'customAgentType': 'agent-c'}, 'agent-c'),
    ({'agentType': 'general-purpose'}, 'general-purpose'),
    ({'agentType': 'bau', 'name': 'bau'}, 'unknown'),                  # only the name: the type is unknown
    (None, 'unknown')])                                                # no meta.json at all
def test_the_type_of_a_start_follows_the_rule_of_the_report_and_never_shows_a_name(world, meta, expected):
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:00:00Z', 'a1', inp=5, request='r1')])
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:01:00Z', 'a2', inp=5, request='r2')], sub='x1', meta=meta)
    data = startup(world, '--day', DAY)
    assert [s['type'] for s in data['starts']] == [None, expected]
    assert list(data['subagents']['by_type']) == [expected]
    assert 'bau' not in json.dumps(data) or expected == 'bau'


# --- Text: the overview line, the full section, a selection ------------------------------------------------------

FULL_STARTUP = """\
Startup context
  Measured, the first request of a session or subagent (input + cache write + cache read):
    main: 3 starts, median 31k tokens, min 15k, max 50k, 1 warm (33 %)
    subagents by type:
      agent-a: 2 starts, median 80k tokens, min 70k, max 90k, 1 warm (50 %)
      agent-b: 1 start, median 40k tokens, min 40k, max 40k, 1 warm (100 %)
      unknown: 1 start, median 1000 tokens, min 1000, max 1000, 0 warm (0 %)
    per day:
      Tue 13 Oct: main 23k tokens (2 starts), subagents 55k tokens (4 starts)
      Wed 14 Oct: main 50k tokens (1 start), subagents n/a
  Estimate (~): not available, no folder of a start in this period exists any more"""


def test_the_full_report_has_a_startup_section_after_the_total(world):
    startup_world(world)
    text = world.text('--since', DAY, '--until', '2026-10-14')
    section = 'Startup context' + text.split('Startup context', 1)[1].split('\n\n', 1)[0]
    assert section == FULL_STARTUP
    assert text.index('\nTotal\n') < text.index('\nStartup context\n') < text.index('\nBy CLI')


def test_the_full_report_says_so_without_starts(world):
    world.projects.mkdir()
    assert 'Startup context\n  no starts in this period' in world.text('--day', DAY)


def test_the_overview_has_one_line_for_the_start_after_the_models(world):
    startup_world(world)
    lines = world.overview('--since', DAY, '--until', '2026-10-14').splitlines()
    split = next(i for i, line in enumerate(lines) if line.startswith('Main '))
    assert lines[split + 1].startswith('Opus ')
    assert lines[split + 2] == 'Start: main 31k tokens (median), subagents 1000-80k (median per type)'
    assert world.report('--since', DAY, '--until', '2026-10-14')['overview']['startup'] == {
        'main_median_tokens': 31_000, 'subagent_type_median_tokens': {'min': 1_000, 'max': 80_000}}


def test_the_overview_line_for_one_subagent_type(world):
    world.transcript('-p-a', 'aaaaaaaa-1', [paid('2026-10-13T08:00:00Z', 'a1', 1.0)])
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:01:00Z', 'a2', inp=36_000, request='r2')], sub='x1',
                     meta={'agentType': 'agent-a'})
    assert 'Start: main 0 tokens (median), subagents 36k (median per type)\n' in world.overview('--day', DAY)
    assert world.report('--day', DAY)['overview']['startup'] == {
        'main_median_tokens': 0, 'subagent_type_median_tokens': {'min': 36_000, 'max': 36_000}}


def test_the_overview_line_with_subagents_only(world):
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:01:00Z', 'a2', inp=36_000, request='r2')], sub='x1',
                     meta={'agentType': 'agent-a'})
    assert 'Start: subagents 36k (median per type)\n' in world.overview('--day', DAY)
    assert world.report('--day', DAY)['overview']['startup'] == {
        'main_median_tokens': None, 'subagent_type_median_tokens': {'min': 36_000, 'max': 36_000}}


def test_the_overview_line_for_main_sessions_only_and_without_starts(world):
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:00:00Z', 'a1', cw=45_000, request='r1')])
    assert 'Start: main 45k tokens (median)\n' in world.overview('--day', DAY)
    assert world.report('--day', DAY)['overview']['startup'] == {'main_median_tokens': 45_000,
                                                                 'subagent_type_median_tokens': None}
    assert 'Start:' not in world.overview('--day', '2026-10-01')
    assert world.report('--day', '2026-10-01')['overview']['startup'] is None


def test_a_selection_shows_the_starts_of_its_sessions_only(world):
    startup_world(world)
    text = world.overview('--since', DAY, '--until', '2026-10-14', '--session', 'aaaaaaaa')
    section = text.split('Startup context of the selected sessions\n', 1)[1]
    assert 'main: 1 start, median 15k tokens' in section and 'agent-a: 2 starts' in section
    assert 'Estimate' not in section and '50k' not in section
    assert text.index('Sum of 1 CLI') < text.index('Startup context of the selected sessions')
    project = world.overview('--since', DAY, '--until', '2026-10-14', '--project', 'beta')
    section = project.split('Startup context of the selected sessions\n', 1)[1]
    assert 'main: 1 start, median 31k tokens' in section and 'subagents by type' not in section


# --- The archive keeps the starts ---------------------------------------------------------------------------------

def archived_lines(world):
    return [json.loads(line) for line in (world.log / ARCHIVE).read_text(encoding='utf-8').splitlines()]


def write_archived_lines(world, lines):
    (world.log / ARCHIVE).write_text('\n'.join(json.dumps(line) for line in lines) + '\n', encoding='utf-8')


def test_the_starts_of_a_finished_day_survive_the_archive_without_transcripts(world):
    startup_world(world)
    live = startup(world, '--day', DAY)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    for path in world.projects.rglob('*.jsonl'):
        path.unlink()
    assert startup(world, '--day', DAY) == live
    assert live['starts'][0]['tokens']['total'] == 15_000


def forget_starts(world):
    lines = archived_lines(world)
    for line in lines:
        del line['stats']['starts']                        # the archive of v1.1.1 knows no starts
    write_archived_lines(world, lines)


def test_an_archived_day_of_an_earlier_version_has_its_starts_computed_from_the_transcripts(world):
    startup_world(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    forget_starts(world)
    data = world.report('--day', DAY)
    assert data['coverage']['archive_days'] == [DAY]
    assert len(data['startup']['starts']) == 6 and data['startup']['main']['starts'] == 2


def test_an_archived_day_of_an_earlier_version_without_transcripts_has_no_starts(world):
    startup_world(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    forget_starts(world)
    for transcript in world.projects.rglob('*.jsonl'):
        transcript.unlink()
    assert startup(world, '--day', DAY)['starts'] == []


def test_the_starts_of_a_live_day_and_an_archived_day_add_up_in_time_order(world):
    startup_world(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    data = startup(world, '--since', DAY, '--until', '2026-10-14')
    assert [s['tokens']['total'] for s in data['starts']] == [15_000, 70_000, 90_000, 40_000, 1_000, 31_000, 50_000]


def break_start(start, field):
    if field == 'drop':
        del start['model']
    else:
        start[field[0]] = field[1]


@pytest.mark.parametrize('field', ['drop', ('t', 'noon'), ('t', True), ('type', 7), ('type', 'bad type!'),
                                   ('model', None), ('model', 'a\nb'), ('input', -1), ('input', 'many'),
                                   ('cache_write', 10 ** 13), ('cache_read', True), ('session', 5),
                                   ('session', '123456789')])
def test_an_archived_start_of_the_wrong_shape_makes_the_line_unreadable(world, field):
    startup_world(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    lines = archived_lines(world)
    break_start(lines[0]['stats']['starts'][0], field)
    write_archived_lines(world, lines)
    data = world.report('--day', DAY)
    assert data['coverage']['unreadable_archive_lines'] == 1 and data['coverage']['archive_days'] == []
    assert len(data['startup']['starts']) == 6                       # computed from the transcripts instead


@pytest.mark.parametrize('starts', [{'t': 1}, {}, '', 'many', [7], [[1, 2]]])
def test_archived_starts_that_are_no_list_of_starts_make_the_line_unreadable(world, starts):
    startup_world(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    lines = archived_lines(world)
    lines[0]['stats']['starts'] = starts
    write_archived_lines(world, lines)
    assert world.report('--day', DAY)['coverage']['unreadable_archive_lines'] == 1


def test_the_archive_holds_numbers_types_and_models_but_no_paths(world):
    startup_world(world)
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    stats = archived_lines(world)[0]['stats']
    assert set(stats['starts'][0]) == {'t', 'session', 'type', 'model', 'input', 'cache_write', 'cache_read'}
    assert '/home' not in json.dumps(stats['starts'])


# --- The estimate of the fixed start ------------------------------------------------------------------------------

def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def estimate_world(world, tmp_path):
    """Files of 230, 115, 460, 23 and 69 characters: 100, 50, 200, 10 and 30 tokens at 2.3 characters per token.
    Returns (user folder, working folder)."""
    user, work = tmp_path / 'user', tmp_path / 'work' / 'proj-x'
    work.mkdir(parents=True)
    put(user / 'CLAUDE.md', 'g' * 230)
    put(user / 'rules' / 'a.md', 'r' * 115)
    put(user / 'rules' / 'notes.txt', 'n' * 50)                       # not a markdown file: no rule
    put(user / 'rules' / 'sub' / 'scoped.md', f'---\npaths:\n  - "src/**"\n---\n{SECRET}' + 's' * 500)
    put(tmp_path / 'work' / 'CLAUDE.md', 'q' * 23)
    put(work / 'CLAUDE.md', 'p' * 460)
    put(world.projects / re.sub('[^A-Za-z0-9]', '-', str(work)) / 'memory' / 'MEMORY.md', 'm' * 69)
    put(user / 'agents' / 'a' / 'SKILL.md', f'---\nname: agent-a\ndescription: {"D" * 16}\nmodel: sonnet\n---\n{SECRET}')
    put(user / 'agents' / 'b.md', f'---\nname: agent-b\ndescription: >\n  {"x" * 8}\n  {"y" * 7}\nomitClaudeMd: true\n---\n')
    put(user / 'agents' / 'a' / 'ERKLAERUNG.md', f'no frontmatter {SECRET}')
    put(user / 'agents' / 'c.md', '---\nname: agent-c\n---\nno description')
    put(user / 'skills' / 'one' / 'SKILL.md', f'---\nname: skill-one\ndescription: "{"q" * 14}"\n---\n{SECRET}')
    put(user / 'skills' / 'skill-two' / 'SKILL.md', f'---\ndescription: {"z" * 14}\n---\n')
    put(user / 'skills' / 'synced' / 'deep' / 'SKILL.md', f'---\nname: deep\ndescription: {"d" * 99}\n---\n')
    put(user / 'skills' / 'plain' / 'SKILL.md', 'no frontmatter')
    cwd = str(work)
    world.transcript('-p-x', 'xxxxxxxx-1', [req('2026-10-13T08:00:00Z', 'x1', cr=30_000, request='r1', cwd=cwd)])
    for number, kind in enumerate(('agent-a', 'agent-b', 'Explore'), 1):
        world.transcript('-p-x', 'xxxxxxxx-1', [req(f'2026-10-13T08:0{number}:00Z', f'y{number}', cr=20_000,
                                                    request=f'ry{number}', cwd=cwd)], sub=f'y{number}',
                         meta={'agentType': kind})
    return user, work


def estimate(world, user, *args):
    return world.report('--day', DAY, '--claude-dir', str(user), *args)['startup']['estimate']


def test_the_estimate_counts_the_characters_of_the_files_a_start_reads(world, tmp_path):
    user, _work = estimate_world(world, tmp_path)
    data = estimate(world, user)
    assert data['project'] == 'proj-x' and data['chars_per_token'] == 2.3
    assert [part['name'] for part in data['parts']] == ['claude_md', 'memory', 'agents', 'skills']
    claude_md, memory, agents, skills = data['parts']
    assert claude_md['files'] == [{'file': '~/.claude/CLAUDE.md', 'chars': 230, 'tokens': 100},
                                  {'file': '~/.claude/rules/a.md', 'chars': 115, 'tokens': 50},
                                  {'file': '../CLAUDE.md', 'chars': 23, 'tokens': 10},
                                  {'file': 'CLAUDE.md', 'chars': 460, 'tokens': 200}]
    assert (claude_md['items'], claude_md['chars'], claude_md['tokens']) == (4, 828, 360)
    assert memory['files'] == [{'file': 'MEMORY.md', 'chars': 69, 'tokens': 30}] and memory['tokens'] == 30
    assert (agents['items'], agents['chars'], agents['tokens']) == (2, 46, 20) and 'files' not in agents
    assert (skills['items'], skills['chars'], skills['tokens']) == (2, 46, 20)
    assert data['total_tokens'] == 430
    assert data['not_estimable'] == ['system prompt', 'built-in tools', 'MCP instructions',
                                     'skills of plugins and other apps', 'output of hooks']


def test_the_chars_per_token_come_from_the_configuration(world, tmp_path, monkeypatch):
    user, _work = estimate_world(world, tmp_path)
    world.configure(monkeypatch, start_estimate={'chars_per_token': 4})
    data = estimate(world, user)
    assert data['chars_per_token'] == 4 and data['parts'][0]['tokens'] == 207 and data['parts'][1]['tokens'] == 17
    assert data['parts'][0]['files'][0]['tokens'] == 58 and data['total_tokens'] == 207 + 17 + 12 + 12


def test_a_subagent_with_omitclaudemd_has_no_claude_md_part_and_a_built_in_agent_is_not_listed(world, tmp_path):
    user, _work = estimate_world(world, tmp_path)
    assert estimate(world, user)['subagents'] == [
        {'type': 'agent-a', 'omit_claude_md': False, 'claude_md_tokens': 360},
        {'type': 'agent-b', 'omit_claude_md': True, 'claude_md_tokens': 0}]


@pytest.mark.parametrize('flag, omitted', [('true', True), ('True', True), ('TRUE', True), ('false', False),
                                           ('yes', False), ('', False)])
def test_only_the_value_true_omits_the_claude_md_files(world, tmp_path, flag, omitted):
    user, _work = estimate_world(world, tmp_path)
    put(user / 'agents' / 'b.md', f'---\nname: agent-b\ndescription: x\nomitClaudeMd: {flag}\n---\n')
    entry = [s for s in estimate(world, user)['subagents'] if s['type'] == 'agent-b'][0]
    assert entry['omit_claude_md'] is omitted and entry['claude_md_tokens'] == (0 if omitted else 360)


def test_the_estimate_follows_the_latest_start_of_the_period(world, tmp_path):
    user, _work = estimate_world(world, tmp_path)
    other = tmp_path / 'work' / 'proj-y'
    put(other / 'CLAUDE.md', 'o' * 46)
    world.transcript('-p-y', 'yyyyyyyy-1', [req('2026-10-13T12:00:00Z', 'z1', cr=5, request='rz', cwd=str(other))])
    data = estimate(world, user)
    assert data['project'] == 'proj-y' and data['parts'][0]['items'] == 4
    assert data['parts'][1] == {'name': 'memory', 'items': 0, 'files': [], 'chars': 0, 'tokens': 0}


def test_a_selection_has_no_estimate(world, tmp_path):
    user, _work = estimate_world(world, tmp_path)
    text = world.overview('--day', DAY, '--claude-dir', str(user), '--project', 'proj-x')
    assert 'Estimate' not in text and 'Startup context of the selected sessions' in text


def test_the_estimate_is_missing_when_no_start_has_a_folder_that_exists(world, tmp_path):
    user, work = estimate_world(world, tmp_path)
    shutil.rmtree(work)
    assert estimate(world, user) is None
    assert 'Estimate (~): not available, no folder of a start in this period exists any more' in world.text(
        '--day', DAY, '--claude-dir', str(user))


def test_a_start_without_a_working_folder_gives_no_estimate(world, tmp_path):
    world.transcript('-p-x', 'xxxxxxxx-1', [{**req('2026-10-13T08:00:00Z', 'x1', cr=100, request='r1'), 'cwd': None}])
    assert estimate(world, tmp_path / 'user') is None


def test_the_claude_md_chain_counts_a_file_once_and_knows_the_local_and_the_hidden_variants(world, tmp_path):
    work = tmp_path / 'work'
    user = work / '.claude'                                     # the user folder is also a project folder up the chain
    put(user / 'CLAUDE.md', 'u' * 23)
    put(work / 'CLAUDE.local.md', 'l' * 23)
    put(user / 'rules' / 'r.md', 'r' * 23)
    put(work / 'sub' / '.claude' / 'CLAUDE.md', 'h' * 23)
    world.transcript('-p-x', 'xxxxxxxx-1', [req('2026-10-13T08:00:00Z', 'x1', cr=100, request='r1', cwd=str(work / 'sub'))])
    files = estimate(world, user)['parts'][0]['files']
    assert [(f['file'], f['chars']) for f in files] == [
        ('~/.claude/CLAUDE.md', 23), ('~/.claude/rules/r.md', 23), ('../CLAUDE.local.md', 23), ('.claude/CLAUDE.md', 23)]


def test_a_rule_with_paths_in_its_frontmatter_is_not_read_at_the_start(world, tmp_path):
    user, _work = estimate_world(world, tmp_path)
    labels = [f['file'] for f in estimate(world, user)['parts'][0]['files']]
    assert not any('scoped' in label for label in labels)


def test_memory_is_cut_at_200_lines(world, tmp_path):
    user, work = estimate_world(world, tmp_path)
    memory = world.projects / re.sub('[^A-Za-z0-9]', '-', str(work)) / 'memory' / 'MEMORY.md'
    put(memory, ''.join(f'{"m" * 22}\n' for _ in range(250)))            # 23 characters a line
    assert estimate(world, user)['parts'][1]['files'] == [{'file': 'MEMORY.md', 'chars': 200 * 23, 'tokens': 2000}]


def test_memory_is_cut_at_25_kb(world, tmp_path):
    user, work = estimate_world(world, tmp_path)
    memory = world.projects / re.sub('[^A-Za-z0-9]', '-', str(work)) / 'memory' / 'MEMORY.md'
    put(memory, 'm' * 40_000)
    assert estimate(world, user)['parts'][1]['files'][0]['chars'] == 25 * 1024


def test_a_definition_in_the_project_wins_over_the_user_folder_and_adds_its_own(world, tmp_path):
    user, work = estimate_world(world, tmp_path)
    put(work / '.claude' / 'agents' / 'p.md', f'---\nname: agent-p\ndescription: {"P" * 16}\n---\n')
    put(work / '.claude' / 'agents' / 'again.md', f'---\nname: agent-a\ndescription: {"A" * 99}\n---\n')
    put(work / '.claude' / 'skills' / 's' / 'SKILL.md', f'---\nname: skill-one\ndescription: {"S" * 99}\n---\n')
    data = estimate(world, user)
    assert (data['parts'][2]['items'], data['parts'][2]['chars']) == (3, 106 + 23 + 23)
    assert (data['parts'][3]['items'], data['parts'][3]['chars']) == (2, 108 + 23)


@pytest.mark.parametrize('front, chars', [
    ('description: plain text', 10 + 10),
    ('description: "quoted text"', 10 + 11),
    ("description: 'single quoted'", 10 + 13),
    ('description: >\n  folded one\n  folded two\n', 10 + 21),
    ('description: |-\n  literal one\n\n  literal two\n', 10 + 23),
    ('description: starts here\n  and goes on\nmodel: x', 10 + 23),
    ('description:\n  on the next line\nmodel: x', 10 + 16),
    ('model: x\ndescription: after a key\n', 10 + 11),
    ('description: ""', None),
    ('model: x', None)])
def test_the_length_of_a_description_in_its_spellings(world, tmp_path, front, chars):
    user, work = tmp_path / 'user', tmp_path / 'work'
    work.mkdir()
    put(user / 'skills' / 'name-99999' / 'SKILL.md', f'---\n{front}\n---\nbody')       # the name has 10 characters
    world.transcript('-p-x', 'xxxxxxxx-1', [req('2026-10-13T08:00:00Z', 'x1', cr=100, request='r1', cwd=str(work))])
    skills = estimate(world, user)['parts'][3]
    assert (skills['items'], skills['chars']) == ((1, chars) if chars else (0, 0))


def test_what_cannot_be_read_counts_nothing_and_a_bad_byte_counts_as_one_character(world, tmp_path):
    user, work = estimate_world(world, tmp_path)
    (work / 'CLAUDE.md').unlink()
    (work / 'CLAUDE.md').mkdir()                                       # a folder where a file should be
    (user / 'rules' / 'bin.md').write_bytes(b'\xff\xfe')
    (user / 'agents' / 'bad.md').write_bytes(b'---\nname: \xff\xfe\ndescription: x\n---\n')
    data = estimate(world, user)
    assert [f['file'] for f in data['parts'][0]['files']] == ['~/.claude/CLAUDE.md', '~/.claude/rules/a.md',
                                                              '~/.claude/rules/bin.md', '../CLAUDE.md']
    assert data['parts'][0]['files'][2]['chars'] == 2 and data['parts'][2]['items'] == 2


def test_a_huge_file_is_read_only_up_to_a_limit(world, tmp_path):
    user, work = estimate_world(world, tmp_path)
    put(work / 'CLAUDE.md', 'p' * (report_module.MAX_FILE_CHARS + 5))
    assert estimate(world, user)['parts'][0]['files'][-1]['chars'] == report_module.MAX_FILE_CHARS


def test_the_text_of_the_estimate(world, tmp_path):
    user, _work = estimate_world(world, tmp_path)
    text = world.text('--day', DAY, '--claude-dir', str(user))
    section = text.split('Startup context\n', 1)[1].split('\n\n', 1)[0]
    head = '  Estimate (~) from the files as they are now, for the folder of proj-x, at 2.3 characters per token:\n'
    assert section.split(head)[1] == (
        '    CLAUDE.md files: ~360 tokens (4 files)\n'
        '      ~/.claude/CLAUDE.md: ~100\n'
        '      ~/.claude/rules/a.md: ~50\n'
        '      ../CLAUDE.md: ~10\n'
        '      CLAUDE.md: ~200\n'
        '    MEMORY.md: ~30 tokens (1 file)\n'
        '    agent descriptions: ~20 tokens (2 agents)\n'
        '    skill descriptions: ~20 tokens (2 skills)\n'
        '    together ~430 tokens; not estimable: system prompt, built-in tools, MCP instructions, '
        'skills of plugins and other apps, output of hooks\n'
        '    subagents with their own definition: agent-a: CLAUDE.md files ~360 tokens; '
        'agent-b: omitClaudeMd, no CLAUDE.md files')


def test_the_text_says_one_file_one_agent_one_skill(world, tmp_path):
    user, work = tmp_path / 'user', tmp_path / 'work'
    put(work / 'CLAUDE.md', 'p' * 23)
    put(user / 'agents' / 'a.md', '---\nname: a\ndescription: x\n---\n')
    put(user / 'skills' / 's' / 'SKILL.md', '---\ndescription: x\n---\n')
    world.transcript('-p-x', 'xxxxxxxx-1', [req('2026-10-13T08:00:00Z', 'x1', cr=100, request='r1', cwd=str(work))])
    text = world.text('--day', DAY, '--claude-dir', str(user))
    for needle in ('CLAUDE.md files: ~10 tokens (1 file)', 'MEMORY.md: ~0 tokens (0 files)',
                   'agent descriptions: ~1 tokens (1 agent)', 'skill descriptions: ~1 tokens (1 skill)'):
        assert needle in text, needle
    assert 'subagents with their own definition' not in text


def test_the_user_folder_defaults_to_dot_claude_in_the_home_folder(world, tmp_path, monkeypatch):
    user, _work = estimate_world(world, tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))                  # Python on Windows takes "~" from here
    shutil.copytree(user, tmp_path / '.claude')
    assert world.report('--day', DAY)['startup']['estimate']['parts'][0]['items'] == 4


def test_the_estimate_is_only_made_for_the_full_report_and_the_json(world, tmp_path, monkeypatch):
    user, _work = estimate_world(world, tmp_path)
    called = []
    monkeypatch.setattr(report_module, 'estimate_start', lambda *a, **k: called.append(1))
    world.overview('--day', DAY, '--claude-dir', str(user))
    assert called == []


def test_no_content_and_no_path_reaches_the_output_through_the_estimate(world, tmp_path):
    user, work = estimate_world(world, tmp_path)
    outputs = [world.text('--day', DAY, '--claude-dir', str(user)),
               world.text('--day', DAY, '--claude-dir', str(user), '--json')]
    assert report_module.main(world.args('--archive'), now=NOW) == 0
    outputs.append((world.log / ARCHIVE).read_text(encoding='utf-8'))
    for output in outputs:
        assert SECRET not in output and str(tmp_path) not in output and str(work) not in output


@pytest.mark.parametrize('name', [
    pytest.param('bad\nname\x1b.md', marks=pytest.mark.posix_only),    # Windows allows no control characters in names
    'bad;name$.md'])
def test_the_name_of_a_rule_file_is_made_harmless(world, tmp_path, name):
    user, _work = estimate_world(world, tmp_path)
    put(user / 'rules' / name, 'r' * 23)
    labels = [f['file'] for f in estimate(world, user)['parts'][0]['files']]
    assert '~/.claude/rules/bad?name?.md' in labels


def test_the_format_document_explains_every_field_of_the_startup_section(world, tmp_path):
    user, _work = estimate_world(world, tmp_path)
    data = world.report('--day', DAY, '--claude-dir', str(user))
    assert data['startup']['estimate'] is not None and data['overview']['startup'] is not None
    with open(REPORT_FORMAT, encoding='utf-8') as f:
        document = f.read()
    keys = set(json_keys(data))
    assert {'startup', 'starts', 'by_type', 'per_day', 'warm_share', 'estimate', 'chars_per_token', 'omit_claude_md',
            'subagent_type_median_tokens', 'not_estimable', 'claude_md_tokens'} <= keys
    assert 'agent-a' not in keys                                 # a type is data, not a field
    assert [key for key in sorted(keys) if f'`{key}`' not in document] == []
    for needle in ('Startup context', 'Start:', '**start**', 'warm', 'cold', 'omitClaudeMd', '--claude-dir', 'not estimable',
                   'start_estimate.chars_per_token', 'median', 'continued in a new transcript'):
        assert needle in document, needle


def test_a_file_that_cannot_be_opened_counts_nothing(world, tmp_path, monkeypatch):
    user, work = estimate_world(world, tmp_path)
    real = builtins.open

    def refuse(path, *args, **kwargs):
        if str(path) == str(work / 'CLAUDE.md'):
            raise PermissionError('denied')
        return real(path, *args, **kwargs)

    monkeypatch.setattr(report_module, 'open', refuse, raising=False)
    assert [f['file'] for f in estimate(world, user)['parts'][0]['files']] == [
        '~/.claude/CLAUDE.md', '~/.claude/rules/a.md', '../CLAUDE.md']


def test_a_start_at_midnight_belongs_to_the_day_that_begins(world):
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-12T22:00:00Z', 'a1', inp=1, request='r1')])     # 00:00 on 13th
    world.transcript('-p-a', 'bbbbbbbb-1', [req('2026-10-13T21:59:59Z', 'b1', inp=2, request='r2')])     # 23:59:59 on 13th
    world.transcript('-p-a', 'cccccccc-1', [req('2026-10-13T22:00:00Z', 'c1', inp=3, request='r3')])     # 00:00 on 14th
    assert [s['tokens']['total'] for s in startup(world, '--day', DAY)['starts']] == [1, 2]
    assert [s['tokens']['total'] for s in startup(world, '--day', '2026-10-14')['starts']] == [3]
    assert startup(world, '--day', '2026-10-12')['starts'] == []


def test_the_types_are_listed_with_the_most_starts_first(world):
    world.transcript('-p-a', 'aaaaaaaa-1', [req('2026-10-13T08:00:00Z', 'a1', inp=5, request='r1')])
    for number, kind in enumerate(('alpha', 'zeta', 'zeta'), 1):
        world.transcript('-p-a', 'aaaaaaaa-1', [req(f'2026-10-13T08:0{number}:00Z', f'z{number}', inp=5,
                                                    request=f'rz{number}')], sub=f'z{number}', meta={'agentType': kind})
    assert list(startup(world, '--day', DAY)['subagents']['by_type']) == ['zeta', 'alpha']


def test_the_subagent_types_of_an_estimate_come_in_name_order_whatever_the_order_given(world, tmp_path):
    user, work = estimate_world(world, tmp_path)
    data = report_module.estimate_start('proj-x', str(work), str(user), str(world.projects), 2.3,
                                        ['agent-b', 'Explore', 'agent-a'])
    assert [s['type'] for s in data['subagents']] == ['agent-a', 'agent-b']


# --- MEMORY.md belongs to the repository, not to the worktree -------------------------------------------------------------

def memory_at(world, folder, chars):
    put(world.projects / re.sub('[^A-Za-z0-9]', '-', str(folder)) / 'memory' / 'MEMORY.md', 'm' * chars)


def one_start(world, cwd):
    world.transcript('-p-x', 'xxxxxxxx-1', [req('2026-10-13T08:00:00Z', 'x1', cr=100, request='r1', cwd=str(cwd))])


def memory_chars(world, user):
    return [f['chars'] for f in estimate(world, user)['parts'][1]['files']]


def test_a_linked_worktree_reads_the_memory_of_the_main_working_tree(world, tmp_path):
    main, linked = tmp_path / 'hub', tmp_path / 'hub-wt'
    (main / '.git' / 'worktrees' / 'wt').mkdir(parents=True)
    put(linked / '.git', f'gitdir: {main}/.git/worktrees/wt\n')
    memory_at(world, main, 46)
    memory_at(world, linked, 23)
    one_start(world, linked)
    assert memory_chars(world, tmp_path / 'user') == [46]


def test_a_subfolder_of_a_repository_reads_the_memory_of_the_repository(world, tmp_path):
    (tmp_path / 'repo' / '.git').mkdir(parents=True)
    (tmp_path / 'repo' / 'sub' / 'deeper').mkdir(parents=True)
    memory_at(world, tmp_path / 'repo', 46)
    one_start(world, tmp_path / 'repo' / 'sub' / 'deeper')
    assert memory_chars(world, tmp_path / 'user') == [46]


def test_the_memory_of_the_folder_itself_is_the_fallback(world, tmp_path):
    (tmp_path / 'repo' / '.git').mkdir(parents=True)
    (tmp_path / 'repo' / 'sub').mkdir()
    memory_at(world, tmp_path / 'repo' / 'sub', 23)
    one_start(world, tmp_path / 'repo' / 'sub')
    assert memory_chars(world, tmp_path / 'user') == [23]


@pytest.mark.parametrize('marker', ['gitdir: ../.git/modules/inner\n', 'not a git file', 'gitdir: /x/.git/worktrees/\n'])
def test_a_git_file_that_is_no_linked_worktree_makes_its_own_folder_the_root(world, tmp_path, marker):
    put(tmp_path / 'outer' / 'inner' / '.git', marker)
    (tmp_path / 'outer' / '.git').mkdir()
    memory_at(world, tmp_path / 'outer', 46)
    memory_at(world, tmp_path / 'outer' / 'inner', 23)
    one_start(world, tmp_path / 'outer' / 'inner')
    assert memory_chars(world, tmp_path / 'user') == [23]


def test_a_git_file_of_a_folder_above_names_that_folder_as_the_root(world, tmp_path):
    put(tmp_path / 'outer' / '.git', 'gitdir: ../.git/modules/outer\n')           # a submodule, not a linked worktree
    (tmp_path / 'outer' / 'sub').mkdir()
    memory_at(world, tmp_path / 'outer', 46)
    one_start(world, tmp_path / 'outer' / 'sub')
    assert memory_chars(world, tmp_path / 'user') == [46]
