"""Tests for hooks/_hookio.py: arguments, input, directory discovery, output and the JSONL log."""
import io
import json
import os
import sys
from datetime import date

import pytest

import _hookio

CLAUDE = '.claude'


def agents_in(root):
    return os.path.join(root, CLAUDE, 'agents')


def skills_in(root):
    return os.path.join(root, CLAUDE, 'skills')


def commands_in(root):
    return os.path.join(root, CLAUDE, 'commands')


# --- Arguments -------------------------------------------------------------------------

def test_mode_defaults_to_shadow_and_only_enforce_switches_it():
    assert _hookio.parse_args([])['mode'] == 'shadow'
    assert _hookio.parse_args(['--mode=enforce'])['mode'] == 'enforce'
    assert _hookio.parse_args(['--mode=shadow'])['mode'] == 'shadow'
    for unknown in ('--mode=ENFORCE', '--mode=', '--mode=deny', '--mode=1', '--mode=enforce '):
        assert _hookio.parse_args([unknown])['mode'] == 'shadow', unknown


def test_other_arguments_are_kept_and_junk_is_ignored():
    args = _hookio.parse_args(['--mode=enforce', '--week-ok-until=2026-01-20', 'positional', '-x=1', '--flag'])
    assert args['week-ok-until'] == '2026-01-20' and args['mode'] == 'enforce'
    assert _hookio.parse_args([])['week-ok-until'] == ''
    assert _hookio.parse_args(['--a=b=c'])['a'] == 'b=c'


def test_week_ok_until_date_parsing():
    assert _hookio.parse_date('2026-01-20') == date(2026, 1, 20)
    for text in (None, '', 'tomorrow', '2026-13-01', '2026-02-30', '20260120', '2026-1-20', ' 2026-01-20', 5,
                 '2026-01-20\n'):
        assert _hookio.parse_date(text) is None, text


# --- Input -------------------------------------------------------------------------------

@pytest.mark.parametrize('text,expected', [('{"tool_name": "Agent"}', {'tool_name': 'Agent'}), ('', {}),
                                           ('{broken', {}), ('[1, 2]', {}), ('"x"', {}), ('null', {}),
                                           ('[' * 100000, {})])
def test_read_input_always_returns_a_dict(monkeypatch, text, expected):
    monkeypatch.setattr(sys, 'stdin', io.StringIO(text))
    assert _hookio.read_input() == expected


def test_read_input_survives_a_broken_stdin(monkeypatch):
    class Broken:
        def read(self, *_a):
            raise OSError('closed')
    monkeypatch.setattr(sys, 'stdin', Broken())
    assert _hookio.read_input() == {}


# --- Agent directories ---------------------------------------------------------------------

def test_agent_dirs_env_override(monkeypatch):
    monkeypatch.setenv('CCUC_AGENT_DIRS', os.pathsep.join(['/a', '', '/b']))
    assert _hookio.agent_dirs({'cwd': '/ignored'}) == ['/a', '/b']
    monkeypatch.setenv('CCUC_AGENT_DIRS', '')
    assert _hookio.agent_dirs({'cwd': '/proj'})[0] == agents_in('/proj')   # empty value = no override


def test_agent_dirs_project_root_before_cwd_before_user(tmp_path, monkeypatch):
    # The agent lives in the project root, but the cwd of the input has wandered into a subfolder.
    home = str(tmp_path / 'home')
    monkeypatch.setenv('CLAUDE_PROJECT_DIR', '/proj')
    assert _hookio.agent_dirs({'cwd': '/proj/sub/dir'}) == [agents_in('/proj'), agents_in('/proj/sub/dir'),
                                                            agents_in(home)]
    monkeypatch.delenv('CLAUDE_PROJECT_DIR')   # without the root only the cwd remains
    assert _hookio.agent_dirs({'cwd': '/proj/sub/dir'}) == [agents_in('/proj/sub/dir'), agents_in(home)]


def test_agent_dirs_same_root_and_cwd_appear_once_and_missing_cwd_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_PROJECT_DIR', '/proj')
    assert _hookio.agent_dirs({'cwd': '/proj'}).count(agents_in('/proj')) == 1
    monkeypatch.delenv('CLAUDE_PROJECT_DIR')
    monkeypatch.chdir(tmp_path)
    for bad in ({}, {'cwd': ''}, {'cwd': 5}, {'cwd': None}):
        assert _hookio.agent_dirs(bad)[0] == agents_in(os.getcwd()), bad


# --- Skill and command directories -------------------------------------------------------------

def home_of(tmp_path):
    return str(tmp_path / 'home')


def test_skill_dirs_default_personal_before_project(tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_PROJECT_DIR', '/project')
    skills, commands = _hookio.skill_dirs({'cwd': '/project/below'})
    roots = [home_of(tmp_path), '/project', '/project/below']
    assert skills == [skills_in(r) for r in roots]
    assert commands == [commands_in(r) for r in roots]


def test_skill_dirs_env_overrides(monkeypatch):
    monkeypatch.setenv('CCUC_SKILL_DIRS', os.pathsep.join(['/s1', '', '/s2']))
    monkeypatch.setenv('CCUC_COMMAND_DIRS', '/c1')
    assert _hookio.skill_dirs({'cwd': '/x'}) == (['/s1', '/s2'], ['/c1'])
    monkeypatch.delenv('CCUC_COMMAND_DIRS')          # one of the two set: the other one is empty, no fallback
    assert _hookio.skill_dirs({'cwd': '/x'}) == (['/s1', '/s2'], [])
    monkeypatch.delenv('CCUC_SKILL_DIRS')
    monkeypatch.setenv('CCUC_COMMAND_DIRS', '/c1')
    assert _hookio.skill_dirs({'cwd': '/x'}) == ([], ['/c1'])
    monkeypatch.setenv('CCUC_SKILL_DIRS', '')        # set but empty: nothing is searched
    monkeypatch.setenv('CCUC_COMMAND_DIRS', '')
    assert _hookio.skill_dirs({'cwd': '/x'}) == ([], [])


def test_skill_dirs_walk_up_to_the_repo_root(tmp_path):
    (tmp_path / 'repo' / '.git').mkdir(parents=True)
    deep = tmp_path / 'repo' / 'sub' / 'deep'
    deep.mkdir(parents=True)
    skills, _ = _hookio.skill_dirs({'cwd': str(deep)})
    assert skills == [skills_in(p) for p in (home_of(tmp_path), str(deep), str(deep.parent), str(tmp_path / 'repo'))]


def test_worktree_without_own_skills_folder_adds_the_main_checkout(tmp_path):
    (tmp_path / 'main' / '.git' / 'worktrees' / 'wt').mkdir(parents=True)
    (tmp_path / 'wt' / 'sub').mkdir(parents=True)
    (tmp_path / 'wt' / '.git').write_text(f'gitdir: {tmp_path}/main/.git/worktrees/wt\n', encoding='utf-8')
    skills, commands = _hookio.skill_dirs({'cwd': str(tmp_path / 'wt' / 'sub')})
    assert skills_in(str(tmp_path / 'main')) in skills and commands_in(str(tmp_path / 'main')) in commands
    assert skills.index(skills_in(str(tmp_path / 'wt'))) < skills.index(skills_in(str(tmp_path / 'main')))
    (tmp_path / 'wt' / CLAUDE / 'skills').mkdir(parents=True)   # own skills folder: the main checkout does not count
    skills, _ = _hookio.skill_dirs({'cwd': str(tmp_path / 'wt' / 'sub')})
    assert skills_in(str(tmp_path / 'main')) not in skills


def test_project_dir_env_is_searched_before_the_cwd(tmp_path, monkeypatch):
    (tmp_path / 'proj' / '.git').mkdir(parents=True)
    (tmp_path / 'other').mkdir()
    monkeypatch.setenv('CLAUDE_PROJECT_DIR', str(tmp_path / 'proj'))
    skills, _ = _hookio.skill_dirs({'cwd': str(tmp_path / 'other')})
    assert skills.index(skills_in(str(tmp_path / 'proj'))) < skills.index(skills_in(str(tmp_path / 'other')))


@pytest.mark.parametrize('content', ['', 'gitdir: /somewhere/else', 'nonsense', 'gitdir:'])
def test_git_file_without_a_worktree_target_adds_nothing(tmp_path, content):
    (tmp_path / 'wt').mkdir()
    (tmp_path / 'wt' / '.git').write_text(content, encoding='utf-8')
    assert _hookio._project_roots(str(tmp_path / 'wt')) == [str(tmp_path / 'wt')]


def test_unreadable_git_file_gives_no_main_checkout_and_no_crash(tmp_path):
    wt = tmp_path / 'wt'
    wt.mkdir()
    (wt / '.git').write_text('gitdir: /x/.git/worktrees/y\n', encoding='utf-8')
    (wt / '.git').chmod(0)
    try:
        assert _hookio._project_roots(str(wt)) == [str(wt)]
    finally:
        (wt / '.git').chmod(0o600)


def test_no_git_up_to_the_filesystem_root(monkeypatch):
    monkeypatch.setattr(os.path, 'exists', lambda p: False)
    assert _hookio._project_roots('/a/b/c') == ['/a/b/c']


# --- Output ------------------------------------------------------------------------------------

def test_deny_output_has_exactly_the_documented_keys(capsys):
    _hookio.emit_deny('PreToolUse', 'no ümlaut escaping')
    out = json.loads(capsys.readouterr().out)
    assert out == {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                          'permissionDecisionReason': 'no ümlaut escaping'}}


def test_context_output_has_exactly_the_documented_keys(capsys):
    _hookio.emit_context('UserPromptSubmit', 'Usage: 5h 40 %')
    raw = capsys.readouterr().out
    assert json.loads(raw) == {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                                       'additionalContext': 'Usage: 5h 40 %'}}
    assert raw.count('\n') == 1


# --- Log -----------------------------------------------------------------------------------------

def read_log(tmp_path, name='log.jsonl'):
    path = tmp_path / 'log' / name
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def test_log_line_has_keys_and_decision_but_never_values(tmp_path):
    hook_input = {'tool_name': 'Agent', 'agent_id': 'a-1', 'cwd': '/secret/cwd',
                  'tool_input': {'prompt': 'SECRET TASK TEXT', 'description': 'x', 'subagent_type': 'worker',
                                 'model': 'haiku'}}
    _hookio.log_decision('spawn_gate', 'shadow', hook_input, 'deny', 'no definition', usage_state='ok')
    (line,) = read_log(tmp_path)
    assert line['decision'] == 'deny' and line['mode'] == 'shadow' and line['hook'] == 'spawn_gate'
    assert line['tool_input_keys'] == ['description', 'model', 'prompt', 'subagent_type']
    assert line['input_keys'] == ['agent_id', 'cwd', 'tool_input', 'tool_name']
    assert line['tool_name'] == 'Agent' and line['agent_id'] == 'a-1'
    assert line['subagent_type'] == 'worker' and line['model_in_call'] == 'haiku' and line['usage_state'] == 'ok'
    raw = (tmp_path / 'log' / 'log.jsonl').read_text(encoding='utf-8')
    assert 'SECRET TASK TEXT' not in raw and '/secret/cwd' not in raw
    assert line['time'].endswith('+00:00')


def test_log_survives_odd_inputs_and_caps_texts(tmp_path):
    _hookio.log_decision('h', 'enforce', {'tool_input': 'not a dict', 'agent_id': {'x': 1}, 'tool_name': 5},
                         'allow', 'r' * 1000)
    _hookio.log_decision('h', 'enforce', {'tool_input': {'subagent_type': ['SECRET'], 'model': {'k': 'SECRET'}},
                                          'agent_type': 'y' * 500}, 'allow', '')
    first, second = read_log(tmp_path)
    assert len(first['reason']) == 300 and first['tool_input_keys'] == [] and first['agent_id'] is None
    assert first['tool_name'] is None
    assert second['subagent_type'] is None and second['model_in_call'] is None and len(second['agent_type']) == 100
    assert 'SECRET' not in (tmp_path / 'log' / 'log.jsonl').read_text(encoding='utf-8')


@pytest.mark.parametrize('reason,expected', [
    ('agent call 1: model "SECRET-sk-1" unknown', 'agent call 1: model … unknown'),
    ("Regex literal '/SECRET=hunter2/' names agent", 'Regex literal … names agent'),
    ('Wert „SECRET“ und `SECRET` und „SECRET”', 'Wert … und … und …'),
    ('Child workflow "/home/x/SECRET.js": model "SECRET" unknown', 'Child workflow …: model … unknown'),
    ('model "a"SECRET" unknown', 'model …SECRET…'),        # a stray quote cuts the rest
    ('entry "SECRET is not key: value.', 'entry …'),
    ('5h window at 80 %: no new block.', '5h window at 80 %: no new block.'),
])
def test_quoted_parts_are_masked(reason, expected):
    assert _hookio.mask_quoted(reason) == expected


def test_log_masks_the_reason_but_not_the_other_fields(tmp_path):
    _hookio.log_decision('spawn_gate', 'shadow', {'tool_name': 'Workflow'}, 'deny',
                         'Workflow script "/home/alice/SECRET.js" not readable; ' + 'x' * 400)
    (line,) = read_log(tmp_path)
    assert line['reason'].startswith('Workflow script … not readable; ') and len(line['reason']) == 300
    assert 'SECRET' not in (tmp_path / 'log' / 'log.jsonl').read_text(encoding='utf-8')
    assert line['tool_name'] == 'Workflow' and line['decision'] == 'deny'


def test_log_rotates_above_five_megabytes(tmp_path):
    limit = 5 * 1024 * 1024
    directory = tmp_path / 'log'
    directory.mkdir()
    (directory / 'log.jsonl').write_bytes(b'x' * (limit + 1))
    _hookio.write_log_line({'n': 1})
    assert (directory / 'log.jsonl.1').stat().st_size == limit + 1
    assert read_log(tmp_path) == [{'n': 1}]
    _hookio.write_log_line({'n': 2})                      # small file: no further rotation
    assert [line['n'] for line in read_log(tmp_path)] == [1, 2]


def test_log_at_exactly_the_limit_does_not_rotate(tmp_path):
    directory = tmp_path / 'log'
    directory.mkdir()
    (directory / 'log.jsonl').write_bytes(b'x' * (5 * 1024 * 1024))
    _hookio.write_log_line({'n': 1})
    assert not (directory / 'log.jsonl.1').exists()


def test_logging_errors_never_raise(tmp_path, monkeypatch):
    blocker = tmp_path / 'blocker'
    blocker.write_text('a file where a folder should be', encoding='utf-8')
    monkeypatch.setenv('CCUC_LOG_DIR', str(blocker / 'sub'))
    _hookio.write_log_line({'a': 1})
    _hookio.log_decision('h', 'shadow', {}, 'allow', 'x')
    monkeypatch.setenv('CCUC_LOG_DIR', str(tmp_path / 'ok'))
    _hookio.write_log_line({'not': {1, 2}})               # a set is not JSON serialisable
    assert not (tmp_path / 'ok' / 'log.jsonl').exists()


def test_log_dir_env_override_and_config_default(tmp_path, monkeypatch):
    assert _hookio.log_dir() == str(tmp_path / 'log')
    monkeypatch.delenv('CCUC_LOG_DIR')
    assert _hookio.log_dir() == str(tmp_path / 'home' / '.cache' / 'claude-usage-clock')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'log_dir': '~/mylogs'}), encoding='utf-8')
    monkeypatch.setenv('CCUC_CONFIG', str(config))
    assert _hookio.log_dir() == str(tmp_path / 'home' / 'mylogs')
    monkeypatch.setenv('CCUC_LOG_DIR', '~/fromenv')
    assert _hookio.log_dir() == str(tmp_path / 'home' / 'fromenv')
