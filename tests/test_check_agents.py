"""Tests for tools/check_agents.py: the same rule as the spawn gate, applied to every definition, changing nothing."""
import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT

SCRIPT = os.path.join(ROOT, 'tools', 'check_agents.py')


def run(*args, env=None):
    done = subprocess.run([sys.executable, SCRIPT, *map(str, args)], capture_output=True, text=True, timeout=30,
                          env=env or dict(os.environ))
    return done.returncode, done.stdout + done.stderr


def agent_file(path, head):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\n{head}\n---\nText\n', encoding='utf-8')


def config(tmp_path, **spawn_rules):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'spawn_rules': spawn_rules}), encoding='utf-8')
    return path


@pytest.fixture
def agents(tmp_path):
    base = tmp_path / 'agents'
    agent_file(base / 'full' / 'SKILL.md', 'name: full\nmodel: opus\neffort: high')
    agent_file(base / 'no-effort.md', 'name: no-effort\nmodel: sonnet')
    agent_file(base / 'no-model.md', 'name: no-model\neffort: low')
    agent_file(base / 'odd-effort.md', 'name: odd-effort\nmodel: haiku\neffort: thorough')
    agent_file(base / 'open.md', 'name: open\nmodel: opus\neffort: high\ndescription: "never closed')
    agent_file(base / 'notes.md', 'title: not an agent')
    return base


def test_lists_each_finding_with_its_path_and_the_built_in_types(tmp_path, agents, monkeypatch):
    monkeypatch.setenv('CCUC_AGENT_DIRS', str(agents))
    code, out = run('--config', config(tmp_path, model_effort_action='deny'))
    assert code == 1, out
    assert 'model_effort_action = deny; agents with a finding would be denied.' in out
    assert '5 definition(s), 1 without a finding.' in out
    assert f'no-effort: no effort: - {agents / "no-effort.md"}' in out
    assert 'no-model: no model: - ' in out
    assert 'odd-effort: effort "thorough" is not in spawn_rules.effort_values' in out
    assert 'open: the frontmatter ends inside a quoted value' in out
    assert 'full:' not in out and 'notes' not in out
    for name in ('general-purpose', 'Explore', 'Plan'):
        assert f'  {name}: built-in type without a definition file' in out
    assert 'claude-code-guide:' not in out and 'Exceptions (start without the model rule): claude-code-guide, ' \
                                               'statusline-setup' in out


def test_exceptions_and_own_definitions_of_built_in_names_count_as_fine(tmp_path, monkeypatch):
    base = tmp_path / 'agents'
    agent_file(base / 'explore.md', 'name: Explore\nmodel: haiku\neffort: low')
    agent_file(base / 'half.md', 'name: half\nmodel: haiku')
    monkeypatch.setenv('CCUC_AGENT_DIRS', str(base))
    code, out = run('--config', config(tmp_path, exceptions=['general-purpose', 'Plan', 'half', 'claude-code-guide',
                                                        'statusline-setup']))
    assert code == 0, out
    assert 'Findings' not in out and '2 definition(s), 2 without a finding.' in out
    assert 'Exceptions (start without the model rule): Plan, claude-code-guide, general-purpose, half, ' \
           'statusline-setup' in out


def test_the_first_definition_of_a_name_wins_like_in_the_gate(tmp_path, monkeypatch):
    project, user = tmp_path / 'project', tmp_path / 'user'
    agent_file(project / 'worker.md', 'name: worker\nmodel: opus\neffort: high')
    agent_file(user / 'worker.md', 'name: worker\nmodel: opus')
    monkeypatch.setenv('CCUC_AGENT_DIRS', os.pathsep.join([str(project), str(user)]))
    code, out = run('--config', config(tmp_path, exceptions=['general-purpose', 'Explore', 'Plan',
                                                                     'claude-code-guide', 'statusline-setup']))
    assert code == 0, out
    assert f'worker: {user / "worker.md"} (the gate uses {project / "worker.md"})' in out


@pytest.mark.parametrize('level,effect,code', [('warn', 'would start with a warning', 1),
                                               ('off', 'are not checked (rule is off)', 0)])
def test_the_level_decides_the_wording_and_the_exit_code(tmp_path, agents, monkeypatch, level, effect, code):
    monkeypatch.setenv('CCUC_AGENT_DIRS', str(agents))
    got, out = run('--config', config(tmp_path, model_effort_action=level))
    assert got == code and f'agents with a finding {effect}.' in out and 'Findings:' in out


def test_without_config_the_defaults_apply_and_the_project_is_searched_first(tmp_path, monkeypatch):
    monkeypatch.setenv('CCUC_CONFIG', str(tmp_path / 'missing.json'))
    project = tmp_path / 'repo'
    agent_file(project / '.claude' / 'agents' / 'local.md', 'name: local\nmodel: opus')
    code, out = run('--project', project)
    home_agents = os.path.join(os.environ['HOME'], '.claude', 'agents')
    assert code == 1 and 'model_effort_action = warn' in out
    assert f'Searched, in this order: {project / ".claude" / "agents"}, {home_agents}' in out
    assert 'local: no effort:' in out


def test_changes_nothing(tmp_path, agents, monkeypatch):
    monkeypatch.setenv('CCUC_AGENT_DIRS', str(agents))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in agents.rglob('*') if p.is_file()}
    run()
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in agents.rglob('*') if p.is_file()} == before
    assert not (tmp_path / 'log').exists()


@pytest.mark.parametrize('args,text', [(['--config', '/does/not/exist.json'], '--config: not a file'),
                                       (['--project', '/does/not/exist'], '--project: not a folder'),
                                       (['--unknown'], 'unrecognized arguments')])
def test_bad_arguments_exit_with_2(args, text):
    code, out = run(*args)
    assert code == 2 and text in out
