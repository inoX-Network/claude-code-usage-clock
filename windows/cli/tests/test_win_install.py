"""tools/install.py on Windows: commands for Git Bash, Git Bash as a precondition, the files that are copied."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading

import pytest

import _win
from conftest import HOOKS, ROOT

INSTALL = os.path.join(ROOT, 'tools', 'install.py')
SCRIPTS = ('statusline.py', 'usage_clock.py', 'context_reporter.py', 'spawn_gate.py', 'soft_stop.py')


def run(*args, env=None):
    return subprocess.run([sys.executable, INSTALL, *map(str, args)], capture_output=True, text=True,
                          encoding='utf-8', timeout=120, env=env)


def forward(path):
    return str(path).replace('\\', '/')


def expected(target, script, *extra):
    return _win.interpreter_command(os.path.join(str(target), script), *extra)


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


@pytest.fixture
def paths(tmp_path):
    settings = tmp_path / 'settings.json'
    settings.write_text('{}', encoding='utf-8')
    return settings, tmp_path / 'target'


def test_the_commands_run_the_current_python_in_utf8_mode_through_forward_slashes(paths):
    settings, target = paths
    done = run('--settings', settings, '--target', target)
    assert done.returncode == 0, done.stdout + done.stderr
    new = read(settings)
    assert new['statusLine']['command'] == expected(target, 'statusline.py')
    submit = [h['command'] for g in new['hooks']['UserPromptSubmit'] for h in g['hooks']]
    assert submit == [expected(target, 'usage_clock.py') + ' || true', expected(target, 'context_reporter.py') + ' || true']
    groups = {g['matcher']: g['hooks'][0]['command'] for g in new['hooks']['PreToolUse']}
    assert groups['*'] == expected(target, 'soft_stop.py', '--mode=shadow') + ' || true'
    assert groups['Agent|Workflow|Skill'] == expected(target, 'spawn_gate.py', '--mode=shadow') + ' || true'
    every = [new['statusLine']['command'], *submit, *groups.values()]
    assert all('python3' not in command and '\\' not in command for command in every)


def test_enforce_and_the_weekly_approval_end_up_in_the_spawn_gate_command(paths):
    settings, target = paths
    assert run('--settings', settings, '--target', target, '--mode', 'enforce', '--week-ok-until', '2026-01-19').returncode == 0
    command = {g['matcher']: g['hooks'][0]['command'] for g in read(settings)['hooks']['PreToolUse']}['Agent|Workflow|Skill']
    assert command == (expected(target, 'spawn_gate.py', '--mode=enforce', '--week-ok-until=2026-01-19')
                       + ' || { echo "spawn gate broken - start denied" >&2; exit 2; }')


def test_the_sync_commands_run_the_current_python_through_forward_slashes(paths):
    """The dry run of the sync (nothing is checked or cloned) shows the two commands as they would be written."""
    settings, target = paths
    done = run('--settings', settings, '--target', target, '--components', 'sync', '--sync-repo', 'owner/name',
               '--dry-run')
    assert done.returncode == 0, done.stdout + done.stderr
    for event, argument in (('SessionStart', 'session-start'), ('SessionEnd', 'session-end')):
        command = expected(target, 'sync.py', argument) + ' || true'
        assert f'{event}: sync.py added -> {command}' in done.stdout
        assert 'python3' not in command and '\\' not in command
    assert read(settings) == {} and not target.exists()


def test_the_dry_run_shows_the_windows_commands_and_writes_nothing(paths):
    settings, target = paths
    done = run('--settings', settings, '--target', target, '--dry-run')
    assert done.returncode == 0, done.stdout + done.stderr
    for script in SCRIPTS:
        assert expected(target, script, *(['--mode=shadow'] if script in ('spawn_gate.py', 'soft_stop.py') else [])) in done.stdout
    assert 'python3' not in done.stdout and 'DRY RUN' in done.stdout
    assert read(settings) == {} and not target.exists()


def test_the_runtime_files_include_the_windows_module_and_the_switch(paths):
    settings, target = paths
    assert run('--settings', settings, '--target', target).returncode == 0
    for name in ('_win.py', '_plat.py'):
        assert (target / name).is_file(), name
    assert (target / '_win.py').read_bytes() == open(os.path.join(ROOT, 'windows', 'cli', '_win.py'), 'rb').read()
    assert sorted(n for n in os.listdir(HOOKS) if n.endswith('.py')) == sorted(
        n for n in os.listdir(target) if n.endswith('.py') and n not in ('_win.py', 'usage_report.py', 'log_summary.py'))


def test_the_installed_files_run_without_the_checkout(paths):
    """Nothing in the target may reach back into the repository: the hook starts from the copy alone."""
    settings, target = paths
    assert run('--settings', settings, '--target', target).returncode == 0
    done = subprocess.run([sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import _usage; '
                           'print(_usage._plat._win.__file__)', str(target)], capture_output=True, text=True,
                          cwd=str(target.parent))
    assert done.returncode == 0, done.stderr
    assert os.path.samefile(done.stdout.strip(), target / '_win.py')


def test_settings_json_is_written_with_plain_line_feeds(paths):
    settings, target = paths
    assert run('--settings', settings, '--target', target).returncode == 0
    data = settings.read_bytes()
    assert b'\r' not in data and data.endswith(b'}\n') and data.count(b'\n') > 10


def test_a_missing_windows_module_stops_before_anything_is_written(paths, tmp_path):
    settings, target = paths
    code = ('import importlib.util, sys\n'
            f'spec = importlib.util.spec_from_file_location("install_real", {INSTALL!r})\n'
            'module = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(module)\n'
            f'module.WINDOWS_MODULE = {str(tmp_path / "gone" / "_win.py")!r}\n'
            'sys.exit(module.main(sys.argv[1:]))\n')
    done = subprocess.run([sys.executable, '-c', code, '--settings', str(settings), '--target', str(target)],
                          capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert done.returncode == 1 and 'files missing' in done.stdout and '_win.py' in done.stdout
    assert read(settings) == {} and not target.exists() and not list(tmp_path.glob('settings.json.bak*'))


def installer_module():
    spec = importlib.util.spec_from_file_location('install_under_test', INSTALL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_settings_json_is_replaced_although_another_process_has_it_open(tmp_path):
    """Windows refuses to replace a file that is open elsewhere; Claude Code reads settings.json all the time."""
    module = installer_module()
    settings = tmp_path / 'settings.json'
    settings.write_text('{}', encoding='utf-8')
    reader = open(settings, 'rb')
    threading.Timer(0.3, reader.close).start()
    try:
        module.write_settings(str(settings), {'model': 'x'}, True)
    finally:
        reader.close()
    assert json.loads(settings.read_text(encoding='utf-8')) == {'model': 'x'}


def test_a_runtime_file_is_replaced_although_it_is_open(tmp_path):
    module = installer_module()
    source, target = tmp_path / 'source.py', tmp_path / 'target.py'
    source.write_text('new', encoding='utf-8')
    target.write_text('old', encoding='utf-8')
    reader = open(target, 'rb')
    threading.Timer(0.3, reader.close).start()
    try:
        module.copy_exclusively(str(source), str(target))
    finally:
        reader.close()
    assert target.read_text(encoding='utf-8') == 'new' and not list(tmp_path.glob('*.tmp-usage-clock'))


def test_the_second_run_changes_nothing(paths):
    settings, target = paths
    assert run('--settings', settings, '--target', target).returncode == 0
    before = settings.read_text(encoding='utf-8')
    done = run('--settings', settings, '--target', target)
    assert done.returncode == 0 and 'already up to date' in done.stdout
    assert settings.read_text(encoding='utf-8') == before


def test_an_entry_written_with_backslashes_and_a_small_drive_letter_is_still_recognised_as_ours(paths):
    settings, target = paths
    assert run('--settings', settings, '--target', target).returncode == 0
    data = read(settings)
    command = data['statusLine']['command']
    old_style = re.sub(r'"([A-Za-z]):/', lambda m: '"' + m.group(1).lower() + ':\\', command).replace('/', '\\')
    assert old_style != command
    data['statusLine']['command'] = old_style
    for group in data['hooks']['UserPromptSubmit']:
        for hook in group['hooks']:
            hook['command'] = hook['command'].replace('"C:/', '"c:/').replace('"c:/', '"c:\\').replace('/', '\\')
    settings.write_text(json.dumps(data), encoding='utf-8')
    done = run('--settings', settings, '--target', target)
    assert done.returncode == 0, done.stdout
    new = read(settings)
    assert new['statusLine']['command'] == expected(target, 'statusline.py')
    assert len(new['hooks']['UserPromptSubmit']) == 2 and 'different path' not in done.stdout


def test_the_undo_hint_is_a_command_git_bash_understands(paths):
    settings, target = paths
    assert run('--settings', settings, '--target', target).returncode == 0
    settings.write_text(json.dumps({**read(settings), 'model': 'x'}), encoding='utf-8')
    done = run('--settings', settings, '--target', target, '--auto-continue')
    backup = re.search(r'^Backup:\s+(.*)$', done.stdout, re.M).group(1)
    undo = re.search(r'^Undo:\s+(.*)$', done.stdout, re.M).group(1)
    assert undo == f'cp "{forward(backup)}" "{forward(settings)}"'
    assert '\\' not in undo
    shell = _win.find_git_bash()
    assert subprocess.run([shell, '-c', undo], capture_output=True).returncode == 0
    assert 'autoContinueAtUsageLimit' not in read(settings)       # the hint really restores the backup


# --- Git Bash is a precondition ------------------------------------------------------------------------------------

def without_git(tmp_path):
    """An environment in which neither the path nor the variable leads to Git Bash."""
    env = {k: v for k, v in os.environ.items() if k.upper() != _win.GIT_BASH_ENV}
    empty = tmp_path / 'empty-path'
    empty.mkdir()
    env['PATH'] = str(empty)
    return env


def test_without_git_bash_the_installer_stops_before_it_writes_anything(paths, tmp_path):
    settings, target = paths
    done = run('--settings', settings, '--target', target, env=without_git(tmp_path))
    assert done.returncode == 1
    assert 'Git Bash' in done.stdout and _win.GIT_BASH_ENV in done.stdout and 'git-scm.com' in done.stdout
    assert read(settings) == {} and not target.exists() and not list(tmp_path.glob('settings.json.bak*'))


def test_the_dry_run_needs_git_bash_too(paths, tmp_path):
    settings, target = paths
    assert run('--settings', settings, '--target', target, '--dry-run', env=without_git(tmp_path)).returncode == 1


def test_removing_the_entries_does_not_need_git_bash(paths, tmp_path):
    settings, target = paths
    assert run('--settings', settings, '--target', target).returncode == 0
    done = run('--settings', settings, '--target', target, '--uninstall', env=without_git(tmp_path))
    assert done.returncode == 0, done.stdout
    assert 'hooks' not in read(settings) and 'statusLine' not in read(settings)


def test_the_variable_in_the_environment_is_enough(paths, tmp_path):
    settings, target = paths
    env = without_git(tmp_path)
    env[_win.GIT_BASH_ENV] = _win.find_git_bash()
    assert run('--settings', settings, '--target', target, env=env).returncode == 0


def test_the_variable_in_the_settings_env_is_enough(paths, tmp_path):
    """Claude Code reads it from there, too (docs: settings.json "env")."""
    settings, target = paths
    settings.write_text(json.dumps({'env': {_win.GIT_BASH_ENV: _win.find_git_bash()}}), encoding='utf-8')
    done = run('--settings', settings, '--target', target, env=without_git(tmp_path))
    assert done.returncode == 0, done.stdout
    assert read(settings)['env'] == {_win.GIT_BASH_ENV: _win.find_git_bash()}


def test_a_variable_that_points_nowhere_is_not_enough(paths, tmp_path):
    settings, target = paths
    env = without_git(tmp_path)
    env[_win.GIT_BASH_ENV] = str(tmp_path / 'nothing' / 'bash.exe')
    assert run('--settings', settings, '--target', target, env=env).returncode == 1
