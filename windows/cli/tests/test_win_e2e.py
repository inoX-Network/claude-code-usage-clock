"""Every part started for real, as Claude Code starts it on Windows: the command line from settings.json through Git
Bash, JSON on stdin, files in a temporary home. Installed by the installer first, so the copy is what runs."""
import json
import os
import subprocess
import sys
import time

import pytest

import _win
from conftest import ROOT

INSTALL = os.path.join(ROOT, 'tools', 'install.py')
SESSION = 'sess-e2e'


@pytest.fixture
def installed(tmp_path, monkeypatch):
    settings, target = tmp_path / 'settings.json', tmp_path / 'target'
    settings.write_text('{}', encoding='utf-8')
    done = subprocess.run([sys.executable, INSTALL, '--settings', str(settings), '--target', str(target),
                           '--mode', 'enforce'], capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    agents = tmp_path / 'agents'
    agents.mkdir()
    (agents / 'worker.md').write_text('---\nname: worker\nmodel: sonnet\neffort: medium\n---\nWorker.\n', encoding='utf-8')
    monkeypatch.setenv('CCUC_AGENT_DIRS', str(agents))
    config = tmp_path / 'config.json'      # the model rule denies, so that a missing definition is visible as a denial
    config.write_text(json.dumps({'spawn_rules': {'model_effort_action': 'deny'}}), encoding='utf-8')
    monkeypatch.setenv('CCUC_CONFIG', str(config))
    return json.loads(settings.read_text(encoding='utf-8')), target


def start(command, stdin):
    done = subprocess.run([_win.find_git_bash(), '-c', command], input=json.dumps(stdin), capture_output=True,
                          text=True, encoding='utf-8', timeout=30, env=dict(os.environ))
    return done


def hook_commands(settings, event, script):
    return [h['command'] for g in settings['hooks'][event] for h in g['hooks'] if script in h['command']]


def bare(command):
    """The command as the hook runs it, without the `|| true` that would hide a failure."""
    return command.split(' || ')[0]


def log_records(tmp_path):
    path = tmp_path / 'log' / 'log.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def test_every_part_runs_and_leaves_its_trace(installed, tmp_path):
    settings, target = installed
    now = time.time()
    status_input = {
        'session_id': SESSION, 'model': {'display_name': 'Opus Ü'},
        'rate_limits': {'five_hour': {'used_percentage': 42, 'resets_at': now + 3600},
                        'seven_day': {'used_percentage': 17, 'resets_at': now + 86400}},
        'context_window': {'context_window_size': 1000000, 'total_input_tokens': 300000, 'used_percentage': 30},
        'prompt_cache': {'warm': True, 'ttl': '1h', 'expires_at': now + 3000}}

    # the status line: the line, the shared usage file, the history, the context state of the session
    done = start(settings['statusLine']['command'], status_input)
    assert done.returncode == 0 and done.stderr == ''
    assert 'Opus Ü' in done.stdout and '5h 42%' in done.stdout and 'ctx 300k/1000k' in done.stdout
    usage = json.loads((tmp_path / 'usage.json').read_text(encoding='utf-8'))
    assert usage['five_hour']['used_percentage'] == 42
    assert (tmp_path / 'log' / 'usage-history.jsonl').read_text(encoding='utf-8').count('\n') == 1
    assert json.loads((tmp_path / 'log' / 'context' / f'{SESSION}.json').read_text())['used_tokens'] == 300000

    # usage clock: usage and time as context
    command = hook_commands(settings, 'UserPromptSubmit', 'usage_clock.py')[0]
    done = start(bare(command), {'hook_event_name': 'UserPromptSubmit', 'prompt': 'hi'})
    assert done.returncode == 0 and done.stderr == ''
    context = json.loads(done.stdout)['hookSpecificOutput']['additionalContext']
    assert 'Usage: 5h 42 %' in context and 'Now: ' in context

    # context reporter, both events: the state file that the status line wrote
    command = hook_commands(settings, 'UserPromptSubmit', 'context_reporter.py')[0]
    done = start(bare(command), {'hook_event_name': 'UserPromptSubmit', 'session_id': SESSION, 'prompt': 'hi'})
    assert done.returncode == 0 and done.stderr == ''
    assert 'Context: 300k/1000k tokens (30%)' in json.loads(done.stdout)['hookSpecificOutput']['additionalContext']
    done = start(bare(command), {'hook_event_name': 'PostToolUse', 'session_id': SESSION, 'tool_name': 'Read'})
    assert done.returncode == 0 and done.stderr == ''      # below after_tool_from: silent, but it ran

    # spawn gate in enforce: an agent with model and effort passes, one without is denied
    command = hook_commands(settings, 'PreToolUse', 'spawn_gate.py')[0]
    done = start(bare(command), {'hook_event_name': 'PreToolUse', 'tool_name': 'Agent',
                                 'tool_input': {'subagent_type': 'worker', 'prompt': 'x'}})
    assert done.returncode == 0 and done.stderr == '' and 'deny' not in done.stdout
    done = start(bare(command), {'hook_event_name': 'PreToolUse', 'tool_name': 'Agent',
                                 'tool_input': {'subagent_type': 'nobody-knows', 'prompt': 'x'}})
    assert done.returncode == 0 and json.loads(done.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'

    # soft stop: a Windows path with backslashes must not break it
    command = hook_commands(settings, 'PreToolUse', 'soft_stop.py')[0]
    done = start(bare(command), {'hook_event_name': 'PreToolUse', 'agent_id': 'a1', 'tool_name': 'Read',
                                 'tool_input': {'file_path': 'C:\\Users\\x\\proj\\file.txt'}})
    assert done.returncode == 0 and done.stderr == ''

    # wake-up watcher: off by default, it says so and ends
    done = start(_win.interpreter_command(str(target / 'wake_watcher.py'), SESSION), {})
    assert done.returncode == 0 and 'disabled' in done.stdout

    hooks_logged = {record.get('hook') for record in log_records(tmp_path)}
    assert {'spawn_gate', 'soft_stop'} <= hooks_logged


@pytest.mark.parametrize('script, args, exit_codes', [
    ('usage_report.py', ['--day', '--projects-dir', 'EMPTY'], (0,)),
    ('log_summary.py', [], (0, 1)),
    ('check_agents.py', ['--project', 'EMPTY'], (0, 1)),      # 1: findings, the built-in types have no definition
])
def test_the_tools_start_from_the_checkout(installed, tmp_path, monkeypatch, script, args, exit_codes):
    empty = tmp_path / 'empty'
    empty.mkdir()
    monkeypatch.setenv('PYTHONUTF8', '1')
    done = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', script), *[str(empty) if a == 'EMPTY' else a for a in args]],
                          capture_output=True, text=True, encoding='utf-8', timeout=60, cwd=str(tmp_path))
    assert done.returncode in exit_codes, done.stdout + done.stderr
    assert 'Traceback' not in done.stderr and done.stdout.strip()
