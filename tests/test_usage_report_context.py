"""Tests for the context split in tools/usage_report.py: the file the desktop plugin writes after the first answer of a
session (`<log_dir>/context-breakdown/<session id>.json`), `clis[].context` in the JSON and the block of `--session`.
Invented files (October 2026); nothing here reads the real home directory."""
import copy
import json
import os

import pytest

from conftest import ROOT
from test_usage_report import (DAY, NOW, REPORT_FORMAT, SECRET, json_keys, local_zone, paid, report_module,  # noqa: F401
                               world)

SESSION = 'aaaaaaaa-1111-2222-3333-444444444444'
FOLDER = 'context-breakdown'


def context_data(session=SESSION):
    """The file of one session in the format of schema_version 1, with every part filled."""
    return {
        'schema_version': 1, 'session_id': session, 'at': '2026-10-13T10:00:30Z', 'cwd': '/home/u/Projekte/proj-a',
        'model': 'claude-opus-5-5', 'detail': 'full', 'total_tokens': 31_700, 'max_tokens': 1_000_000,
        'categories': [{'name': 'System prompt', 'tokens': 3_200, 'kind': 'system'},
                       {'name': 'MCP tools', 'tokens': 9_000, 'kind': 'mcp'},
                       {'name': 'Free space', 'tokens': 968_500, 'kind': 'free'}],
        'memory_files': [{'path': os.path.join(os.path.expanduser('~'), '.claude', 'CLAUDE.md'), 'type': 'user',
                          'tokens': 1_500},
                         {'path': '/srv/other/CLAUDE.md', 'type': 'project', 'tokens': 700}],
        'mcp_tools': [{'server': 'github', 'name': 'create_issue', 'tokens': 900, 'loaded': True},
                      {'server': 'github', 'name': 'list_issues', 'tokens': 1_100, 'loaded': True},
                      {'server': 'github', 'name': 'deferred_tool', 'tokens': 0, 'loaded': False},
                      {'server': 'docs', 'name': 'query', 'tokens': 400, 'loaded': True}],
        'skills': {'tokens': 1_200, 'included': 2, 'total': 3,
                   'items': [{'name': 'testing-tdd', 'tokens': 500}, {'name': 'api-design', 'tokens': 700}]},
        'agents': [{'type': 'architekt', 'source': 'user', 'tokens': 300},
                   {'type': 'arbeiter-bauen', 'source': 'user', 'tokens': 450}]}


def put_context(world, data, name=None, folder=FOLDER):
    path = world.log / folder / ((name or data['session_id']) + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def one_session(world, session=SESSION):
    world.transcript('-home-u-proj-a', session, [paid('2026-10-13T10:00:00Z', f'{session}-m1', 1.0)])


def context_of(world, *args):
    return world.report('--day', DAY, *args)['clis'][0]['context']


# --- clis[].context ---------------------------------------------------------------------------------------------------------

def test_a_session_has_the_content_of_its_file_without_the_session_id(world):
    one_session(world)
    data = context_data()
    put_context(world, data)
    expected = {key: value for key, value in data.items() if key != 'session_id'}
    assert context_of(world) == expected


def test_a_session_without_a_file_has_null_and_nothing_unreadable(world):
    one_session(world)
    report = world.report('--day', DAY)
    assert report['clis'][0]['context'] is None and report['coverage']['unreadable_context_files'] == 0


def test_the_file_belongs_to_the_session_whose_id_starts_with_the_8_characters_of_the_cli(world):
    one_session(world)
    put_context(world, context_data('bbbbbbbb-1111'))
    assert context_of(world) is None
    put_context(world, context_data('aaaaaaaa-9999'))      # same first 8 characters as the CLI
    assert context_of(world)['total_tokens'] == 31_700


def test_each_session_gets_its_own_file(world):
    one_session(world)
    one_session(world, 'bbbbbbbb-2222')
    other = context_data('bbbbbbbb-2222')
    other['total_tokens'] = 5
    put_context(world, context_data())
    put_context(world, other)
    contexts = {cli['id']: cli['context']['total_tokens'] for cli in world.report('--day', DAY)['clis']}
    assert contexts == {'aaaaaaaa': 31_700, 'bbbbbbbb': 5}


def test_the_file_of_the_status_line_in_context_is_not_read(world):
    one_session(world)
    put_context(world, {'session_id': SESSION, 'context_window_size': 1_000_000, 'used_tokens': 5,
                        'measured_at': '2026-10-13T10:00:00Z'}, folder='context')
    report = world.report('--day', DAY)
    assert report['clis'][0]['context'] is None and report['coverage']['unreadable_context_files'] == 0


def test_a_missing_context_folder_is_no_problem(world):
    one_session(world)
    assert world.report('--day', DAY)['coverage']['unreadable_context_files'] == 0


def test_the_log_folder_of_the_configuration_is_used(world, monkeypatch, tmp_path):
    one_session(world)
    other = tmp_path / 'other-log'
    monkeypatch.setenv('CCUC_LOG_DIR', str(other))
    put_context(world, context_data())
    assert context_of(world) is None
    (other / FOLDER).mkdir(parents=True)
    (other / FOLDER / f'{SESSION}.json').write_text(json.dumps(context_data()), encoding='utf-8')
    assert context_of(world)['total_tokens'] == 31_700


# --- A file that cannot be used is counted ------------------------------------------------------------------------------------

def broken(change):
    data = context_data()
    change(data)
    return data


BAD_FILES = {
    'other version': broken(lambda d: d.update(schema_version=2)),
    'version as text': broken(lambda d: d.update(schema_version='1')),
    'version missing': broken(lambda d: d.pop('schema_version')),
    'version true': broken(lambda d: d.update(schema_version=True)),
    'no session id': broken(lambda d: d.pop('session_id')),
    'session id is another one': broken(lambda d: d.update(session_id='aaaaaaaa-0000')),
    'session id is a number': broken(lambda d: d.update(session_id=7)),
    'no time': broken(lambda d: d.pop('at')),
    'time is a number': broken(lambda d: d.update(at=5)),
    'no total': broken(lambda d: d.pop('total_tokens')),
    'total is text': broken(lambda d: d.update(total_tokens='31500')),
    'total is true': broken(lambda d: d.update(total_tokens=True)),
    'total is negative': broken(lambda d: d.update(total_tokens=-1)),
    'total is huge': broken(lambda d: d.update(total_tokens=10 ** 13)),
    'total is not finite': broken(lambda d: d.update(total_tokens=float('inf'))),
    'no window': broken(lambda d: d.pop('max_tokens')),
    'no categories': broken(lambda d: d.pop('categories')),
    'categories is a dict': broken(lambda d: d.update(categories={})),
    'category is text': broken(lambda d: d.update(categories=['System prompt'])),
    'category without name': broken(lambda d: d['categories'][0].pop('name')),
    'category name is a number': broken(lambda d: d['categories'][0].update(name=4)),
    'category without tokens': broken(lambda d: d['categories'][0].pop('tokens')),
    'category tokens are negative': broken(lambda d: d['categories'][0].update(tokens=-5)),
    'kind is a number': broken(lambda d: d['categories'][0].update(kind=3)),
    'no memory files': broken(lambda d: d.pop('memory_files')),
    'memory file without path': broken(lambda d: d['memory_files'][0].pop('path')),
    'memory file without tokens': broken(lambda d: d['memory_files'][0].pop('tokens')),
    'memory type is a number': broken(lambda d: d['memory_files'][0].update(type=1)),
    'no mcp tools': broken(lambda d: d.pop('mcp_tools')),
    'mcp tool without server': broken(lambda d: d['mcp_tools'][0].pop('server')),
    'mcp tool without name': broken(lambda d: d['mcp_tools'][0].pop('name')),
    'mcp tool without tokens': broken(lambda d: d['mcp_tools'][0].pop('tokens')),
    'loaded is a number': broken(lambda d: d['mcp_tools'][0].update(loaded=1)),
    'no skills': broken(lambda d: d.pop('skills')),
    'skills is a list': broken(lambda d: d.update(skills=[])),
    'skills without tokens': broken(lambda d: d['skills'].pop('tokens')),
    'skills without included': broken(lambda d: d['skills'].pop('included')),
    'skills without total': broken(lambda d: d['skills'].pop('total')),
    'skills without items': broken(lambda d: d['skills'].pop('items')),
    'skill item without name': broken(lambda d: d['skills']['items'][0].pop('name')),
    'skill item without tokens': broken(lambda d: d['skills']['items'][0].pop('tokens')),
    'no agents': broken(lambda d: d.pop('agents')),
    'agent without type': broken(lambda d: d['agents'][0].pop('type')),
    'agent without tokens': broken(lambda d: d['agents'][0].pop('tokens')),
    'agent source is a number': broken(lambda d: d['agents'][0].update(source=2)),
    'cwd is a number': broken(lambda d: d.update(cwd=1)),
    'model is a list': broken(lambda d: d.update(model=[])),
    'detail is a number': broken(lambda d: d.update(detail=1)),
}


@pytest.mark.parametrize('name', list(BAD_FILES))
def test_a_file_with_a_wrong_shape_is_counted_and_gives_null(world, name):
    one_session(world)
    put_context(world, BAD_FILES[name], name=SESSION)
    report = world.report('--day', DAY)
    assert report['clis'][0]['context'] is None and report['coverage']['unreadable_context_files'] == 1


@pytest.mark.parametrize('content', ['', '{broken', '[]', '"text"', '7', 'null', pytest.param('[' * 100_000, id='nested'),
                                     '{"a": NaN}'])      # an id of its own: the text would be the test name, 100 000 characters
def test_a_file_that_is_no_json_object_is_counted_and_the_exit_code_stays(world, content):
    one_session(world)
    path = put_context(world, context_data())
    path.write_text(content, encoding='utf-8')
    report = world.report('--day', DAY)      # world.report asserts exit code 0
    assert report['clis'][0]['context'] is None and report['coverage']['unreadable_context_files'] == 1


def test_a_file_in_another_encoding_is_counted(world):
    one_session(world)
    put_context(world, context_data()).write_bytes(b'\xff\xfe{"a": 1}')
    assert world.report('--day', DAY)['coverage']['unreadable_context_files'] == 1


def test_a_folder_instead_of_a_file_is_counted(world):
    one_session(world)
    (world.log / FOLDER / f'{SESSION}.json').mkdir(parents=True)
    assert world.report('--day', DAY)['coverage']['unreadable_context_files'] == 1


def test_a_file_that_is_too_large_is_counted(world, monkeypatch):
    one_session(world)
    path = put_context(world, context_data())
    monkeypatch.setattr(report_module, 'MAX_CONTEXT_BYTES', path.stat().st_size - 1)
    report = world.report('--day', DAY)
    assert report['clis'][0]['context'] is None and report['coverage']['unreadable_context_files'] == 1
    monkeypatch.setattr(report_module, 'MAX_CONTEXT_BYTES', path.stat().st_size)
    assert world.report('--day', DAY)['clis'][0]['context'] is not None


def test_only_the_files_of_the_sessions_in_the_period_are_read(world):
    one_session(world)
    put_context(world, context_data())
    other = put_context(world, context_data('bbbbbbbb-1'))
    other.write_text('{broken', encoding='utf-8')
    assert world.report('--day', DAY)['coverage']['unreadable_context_files'] == 0


def test_a_file_whose_name_is_no_json_name_is_ignored(world):
    one_session(world)
    put_context(world, context_data())
    folder = world.log / FOLDER
    (folder / f'{SESSION}.json.tmp.123').write_text('{broken', encoding='utf-8')
    (folder / 'aaaaaaaa-readme.txt').write_text('x', encoding='utf-8')
    report = world.report('--day', DAY)
    assert report['coverage']['unreadable_context_files'] == 0 and report['clis'][0]['context'] is not None


def test_two_files_of_one_cli_count_once_the_first_by_name_and_the_broken_one_is_counted(world):
    one_session(world)
    put_context(world, context_data('aaaaaaaa-0001'))
    second = context_data('aaaaaaaa-0002')
    second['total_tokens'] = 5
    put_context(world, second)
    put_context(world, context_data('aaaaaaaa-0003')).write_text('{broken', encoding='utf-8')
    report = world.report('--day', DAY)
    assert report['clis'][0]['context']['total_tokens'] == 31_700 and report['coverage']['unreadable_context_files'] == 1


# --- Only names and numbers come out --------------------------------------------------------------------------------------

def test_fields_the_format_does_not_name_are_not_taken_over(world):
    one_session(world)
    data = context_data()
    data['prompt'] = SECRET
    data['categories'][0]['content'] = SECRET
    data['memory_files'][0]['text'] = SECRET
    data['mcp_tools'][0]['description'] = SECRET
    data['skills']['body'] = SECRET
    data['skills']['items'][0]['body'] = SECRET
    data['agents'][0]['system'] = SECRET
    put_context(world, data)
    output = json.dumps(world.report('--day', DAY))
    assert SECRET not in output and 'prompt' not in output.replace('System prompt', '')
    assert SECRET not in world.overview('--day', DAY, '--session', 'aaaaaaaa')


def test_a_name_loses_control_characters_and_is_cut(world):
    one_session(world)
    data = context_data()
    data['categories'][0]['name'] = 'line\nbreak\x1b[31m\ttab\x00'
    data['mcp_tools'][0]['name'] = 'x' * 1_000
    put_context(world, data)
    context = context_of(world)
    assert context['categories'][0]['name'] == 'line?break?[31m?tab?'
    assert context['mcp_tools'][0]['name'] == 'x' * report_module.CONTEXT_TEXT_CHARS


def test_the_labels_may_be_missing_and_are_then_null(world):
    one_session(world)
    data = context_data()
    for key in ('cwd', 'model', 'detail'):
        data.pop(key)
    data['categories'][0].pop('kind')
    data['memory_files'][0].pop('type')
    data['mcp_tools'][0].pop('loaded')
    data['agents'][0].pop('source')
    put_context(world, data)
    context = context_of(world)
    assert context['cwd'] is None and context['model'] is None and context['detail'] is None
    assert context['categories'][0]['kind'] is None and context['memory_files'][0]['type'] is None
    assert context['mcp_tools'][0]['loaded'] is None and context['agents'][0]['source'] is None


def test_the_labels_may_be_null(world):
    one_session(world)
    data = context_data()
    data.update(cwd=None, model=None, detail=None)
    put_context(world, data)
    assert context_of(world)['model'] is None


def test_a_count_with_a_fraction_is_whole_and_a_window_of_zero_is_possible(world):
    one_session(world)
    data = context_data()
    data.update(total_tokens=12.0, max_tokens=0)
    put_context(world, data)
    context = context_of(world)
    assert context['total_tokens'] == 12 and isinstance(context['total_tokens'], int) and context['max_tokens'] == 0


def test_empty_lists_are_a_valid_file(world):
    one_session(world)
    data = context_data()
    data.update(categories=[], memory_files=[], mcp_tools=[], agents=[])
    data['skills'] = {'tokens': 0, 'included': 0, 'total': 0, 'items': []}
    put_context(world, data)
    assert context_of(world)['mcp_tools'] == []


def test_the_json_fields_of_the_context_are_in_the_format_document(world):
    one_session(world)
    put_context(world, context_data())
    keys = set(json_keys(world.report('--day', DAY)))
    assert {'context', 'memory_files', 'mcp_tools', 'categories', 'loaded', 'included', 'items', 'unreadable_context_files',
            'max_tokens'} <= keys
    with open(REPORT_FORMAT, encoding='utf-8') as f:
        document = f.read()
    assert [key for key in sorted(keys) if f'`{key}`' not in document] == []


def test_the_report_stays_in_schema_version_1(world):
    one_session(world)
    assert world.report('--day', DAY)['schema_version'] == 1


# --- --session: the block "Context after the first answer" -----------------------------------------------------------------

def block_of(text):
    """The lines of the block, from its heading to the next empty line."""
    return text.split('Context after the first answer', 1)[1].split('\n\n', 1)[0].splitlines()


def test_the_session_block_shows_total_categories_mcp_memory_skills_and_agents(world):
    one_session(world)
    put_context(world, context_data())
    text = world.overview('--day', DAY, '--session', 'aaaaaaaa')
    assert [line.rstrip() for line in block_of(text)] == [
        ' (session aaaaaaaa, proj-a, measured 2026-10-13T10:00:30Z)',
        '  31,700 of 1,000,000 tokens (3.2 %), model claude-opus-5-5, detail full',
        '  Categories',
        '    System prompt: 3,200',
        '    MCP tools: 9,000',
        '    Free space: 968,500',
        '  MCP servers: 2,400 tokens in 2 servers',
        '    github: 2,000 tokens, 3 tools',
        '      list_issues: 1,100',
        '      create_issue: 900',
        '      deferred_tool: 0',
        '    docs: 400 tokens, 1 tool',
        '      query: 400',
        '  Memory files: 2,200 tokens in 2 files',
        f'    ~{os.sep}.claude{os.sep}CLAUDE.md (user): 1,500',
        '    /srv/other/CLAUDE.md (project): 700',
        '  Skills: 1,200 tokens, 2 of 3 included',
        '    api-design: 700',
        '    testing-tdd: 500',
        '  Agents: 750 tokens in 2 agents',
        '    arbeiter-bauen (user): 450',
        '    architekt (user): 300']


def test_the_block_is_in_the_text_of_session_and_not_in_the_overview_or_with_project(world):
    one_session(world)
    put_context(world, context_data())
    assert 'Context after the first answer' in world.overview('--day', DAY, '--session', 'a')
    assert 'Context after the first answer' not in world.overview('--day', DAY, '--project', 'proj-a')
    assert 'Context after the first answer' not in world.overview('--day', DAY)
    assert 'Context after the first answer' not in world.text('--day', DAY)
    assert 'Context after the first answer' in world.overview('--day', DAY, '--project', 'proj-a', '--session', 'aaaa')


def test_without_a_file_the_session_says_so_in_one_line(world):
    one_session(world)
    text = world.overview('--day', DAY, '--session', 'aaaaaaaa')
    assert text.rstrip('\n').endswith('No context recorded for this session.')
    assert text.count('No context recorded for this session.') == 1


def test_a_broken_file_gives_the_same_line_and_the_sources_name_it(world):
    one_session(world)
    put_context(world, context_data()).write_text('{broken', encoding='utf-8')
    text = world.overview('--day', DAY, '--session', 'aaaaaaaa')
    assert 'No context recorded for this session.' in text
    assert '1 unreadable context file(s) skipped' in text
    assert 'unreadable context' not in world.overview('--day', DAY)


def test_the_sources_do_not_mention_context_files_when_all_are_fine(world):
    one_session(world)
    put_context(world, context_data())
    assert 'context file' not in world.overview('--day', DAY, '--session', 'aaaaaaaa')


def test_only_the_ten_dearest_tools_skills_and_agents_are_listed_and_the_rest_is_counted(world):
    one_session(world)
    data = context_data()
    data['mcp_tools'] = [{'server': 'big', 'name': f'tool-{i:02d}', 'tokens': 100 + i, 'loaded': True} for i in range(13)]
    data['skills'] = {'tokens': 9_000, 'included': 12, 'total': 12,
                      'items': [{'name': f'skill-{i:02d}', 'tokens': 10 + i} for i in range(12)]}
    data['agents'] = [{'type': f'agent-{i:02d}', 'source': 'user', 'tokens': 5 + i} for i in range(11)]
    put_context(world, data)
    lines = block_of(world.overview('--day', DAY, '--session', 'aaaaaaaa'))
    tools = [line for line in lines if line.startswith('      tool-')]
    assert [line.strip() for line in tools] == [f'tool-{i:02d}: {100 + i}' for i in range(12, 2, -1)]
    assert '      … 3 more' in lines
    skills = [line.strip() for line in lines if line.startswith('    skill-')]
    assert skills == [f'skill-{i:02d}: {10 + i}' for i in range(11, 1, -1)]
    assert '    … 2 more' in lines
    agents = [line.strip() for line in lines if line.startswith('    agent-')]
    assert agents == [f'agent-{i:02d} (user): {5 + i}' for i in range(10, 0, -1)]
    assert lines.count('    … 1 more') == 1


def test_equal_tokens_are_listed_by_name(world):
    one_session(world)
    data = context_data()
    data['mcp_tools'] = [{'server': 's', 'name': name, 'tokens': 5, 'loaded': True} for name in ('b', 'c', 'a')]
    put_context(world, data)
    lines = block_of(world.overview('--day', DAY, '--session', 'aaaaaaaa'))
    assert [line.strip() for line in lines if line.startswith('      ')] == ['a: 5', 'b: 5', 'c: 5']


def test_servers_are_listed_by_tokens_then_name(world):
    one_session(world)
    data = context_data()
    data['mcp_tools'] = [{'server': name, 'name': 't', 'tokens': tokens, 'loaded': True}
                         for name, tokens in (('beta', 10), ('alpha', 10), ('gamma', 30))]
    put_context(world, data)
    lines = block_of(world.overview('--day', DAY, '--session', 'aaaaaaaa'))
    assert [line.strip().split(':')[0] for line in lines if line.startswith('    ') and ' tokens, ' in line] == [
        'gamma', 'alpha', 'beta']


def test_a_session_without_mcp_memory_skills_and_agents_says_none(world):
    one_session(world)
    data = context_data()
    data.update(memory_files=[], mcp_tools=[], agents=[])
    data['skills'] = {'tokens': 0, 'included': 0, 'total': 0, 'items': []}
    put_context(world, data)
    lines = [line.strip() for line in block_of(world.overview('--day', DAY, '--session', 'aaaaaaaa'))]
    assert 'MCP servers: none' in lines and 'Memory files: none' in lines
    assert 'Skills: 0 tokens, 0 of 0 included' in lines and 'Agents: none' in lines


def test_without_categories_the_heading_is_left_out(world):
    one_session(world)
    data = context_data()
    data['categories'] = []
    put_context(world, data)
    assert 'Categories' not in world.overview('--day', DAY, '--session', 'aaaaaaaa')


def test_a_window_of_zero_shows_no_percentage_and_missing_labels_are_left_out(world):
    one_session(world)
    data = context_data()
    data.update(max_tokens=0, model=None, detail=None)
    data['memory_files'][0].pop('type')
    data['agents'][0].pop('source')
    put_context(world, data)
    lines = block_of(world.overview('--day', DAY, '--session', 'aaaaaaaa'))
    assert lines[1] == '  31,700 of 0 tokens'
    assert any(line.strip().startswith(f'~{os.sep}.claude{os.sep}CLAUDE.md: ') for line in lines)
    assert any(line.strip() == 'architekt: 300' for line in lines)


def test_one_label_of_model_and_detail_is_shown_alone(world):
    one_session(world)
    data = context_data()
    data['detail'] = None
    put_context(world, data)
    assert block_of(world.overview('--day', DAY, '--session', 'aaaaaaaa'))[1] == (
        '  31,700 of 1,000,000 tokens (3.2 %), model claude-opus-5-5')


def test_a_memory_path_below_the_home_folder_is_shortened_and_others_are_not(world):
    one_session(world)
    data = context_data()
    home = os.path.expanduser('~')
    data['memory_files'] = [{'path': home, 'type': 'a', 'tokens': 1}, {'path': home + 'x/CLAUDE.md', 'type': 'b', 'tokens': 2},
                            {'path': os.path.join(home, 'p', 'CLAUDE.md'), 'type': 'c', 'tokens': 3}]
    put_context(world, data)
    lines = [line.strip() for line in block_of(world.overview('--day', DAY, '--session', 'aaaaaaaa'))]
    assert '~ (a): 1' in lines and f'{home}x/CLAUDE.md (b): 2' in lines
    assert '~' + os.sep + 'p' + os.sep + 'CLAUDE.md (c): 3' in lines


def test_the_json_keeps_the_whole_path(world):
    one_session(world)
    put_context(world, context_data())
    assert context_of(world)['memory_files'][0]['path'] == os.path.join(os.path.expanduser('~'), '.claude', 'CLAUDE.md')


def test_the_help_text_names_the_block():
    assert 'context' in report_module.build_parser().format_help().lower()


def test_the_data_of_a_test_file_is_not_changed_by_the_report(world):
    one_session(world)
    data = context_data()
    before = copy.deepcopy(data)
    put_context(world, data)
    world.report('--day', DAY)
    assert data == before and json.loads((world.log / FOLDER / f'{SESSION}.json').read_text(encoding='utf-8')) == before


def test_the_doc_names_the_folder(world):
    with open(os.path.join(ROOT, 'docs', 'report-format.md'), encoding='utf-8') as f:
        assert f'{FOLDER}/' in f.read()


def test_exactly_ten_rows_have_no_more_line(world):
    one_session(world)
    data = context_data()
    data['mcp_tools'] = [{'server': 'big', 'name': f'tool-{i}', 'tokens': 10 + i, 'loaded': True} for i in range(10)]
    put_context(world, data)
    text = world.overview('--day', DAY, '--session', 'aaaaaaaa')
    assert 'tool-0: 10' in text and ' more' not in text


def test_a_file_with_a_byte_that_is_no_utf8_is_counted_not_repaired(world):
    one_session(world)
    path = put_context(world, context_data())
    path.write_bytes(path.read_bytes().replace(b'System prompt', b'System \xff prompt'))
    report = world.report('--day', DAY)
    assert report['clis'][0]['context'] is None and report['coverage']['unreadable_context_files'] == 1
