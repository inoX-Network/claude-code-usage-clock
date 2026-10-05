"""Tests for tools/install.py. The script runs as a subprocess (the real interface), always against temporary
copies of settings.json and the target folder. The real ~/.claude is never touched: conftest points HOME to a
temporary folder."""
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from datetime import datetime

import pytest

from conftest import HOOKS, ROOT

TOOLS = os.path.join(ROOT, 'tools')
INSTALL = os.path.join(TOOLS, 'install.py')
EXAMPLE = os.path.join(ROOT, 'config.example.json')
RUNTIME_FILES = sorted(n for n in os.listdir(HOOKS) if n.endswith('.py'))
SCRIPT_OF = {'usage_clock.py': 'UserPromptSubmit', 'spawn_gate.py': 'PreToolUse', 'soft_stop.py': 'PreToolUse'}


def load_module():
    spec = importlib.util.spec_from_file_location('install_under_test', INSTALL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*args, script=INSTALL, cwd=None):
    return subprocess.run([sys.executable, script, *map(str, args)], capture_output=True, text=True, timeout=120, cwd=cwd)


def write_settings(tmp_path, content='{}', name='settings.json'):
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')
    return path


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def commands(settings, event):
    return [h['command'] for g in settings['hooks'][event] for h in g['hooks']]


def backups(tmp_path):
    return sorted(tmp_path.glob('settings.json.bak-usage-clock-*'))


def quoted(target, script):
    return shlex.quote(os.path.join(str(target), script))


@pytest.fixture
def repo(tmp_path):
    """A copy of the hook files and the example config that a test may break."""
    root = tmp_path / 'repo'
    root.mkdir()
    shutil.copytree(HOOKS, root / 'hooks', ignore=shutil.ignore_patterns('__pycache__', 'config.json'))
    shutil.copy2(EXAMPLE, root / 'config.example.json')
    return root


# The real install.py (so that coverage sees it), pointed at the copy by the wrapper
WRAPPER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('install_real', {script!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.SOURCE_HOOKS = {hooks!r}
module.EXAMPLE_CONFIG = {example!r}
module.SMOKE_TIMEOUT_S = {timeout}
sys.exit(module.main(sys.argv[1:]))
"""


def run_in(repo, *args, timeout=15):
    code = WRAPPER.format(script=INSTALL, hooks=str(repo / 'hooks'), example=str(repo / 'config.example.json'),
                          timeout=timeout)
    return subprocess.run([sys.executable, '-c', code, *map(str, args)], capture_output=True, text=True, timeout=120)


# --- Basic installation ----------------------------------------------------------------------------------------

def test_adds_all_components_and_leaves_everything_else_untouched(tmp_path):
    settings = write_settings(tmp_path, {'model': 'opus', 'env': {'X': '1'},
                                         'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
                                             {'type': 'command', 'command': 'python3 ~/guard.py'}]}]}})
    original = read(settings)
    target = tmp_path / 'target'
    r = run('--settings', settings, '--target', target)
    assert r.returncode == 0, r.stdout + r.stderr
    new = read(settings)
    assert new['model'] == 'opus' and new['env'] == {'X': '1'}
    assert new['hooks']['PreToolUse'][0] == original['hooks']['PreToolUse'][0]  # the foreign hook is unchanged
    assert len(new['hooks']['PreToolUse']) == 3 and len(new['hooks']['UserPromptSubmit']) == 1
    assert all(h['timeout'] == 5 and h['type'] == 'command' for g in new['hooks']['PreToolUse'][1:] for h in g['hooks'])
    assert 'autoContinueAtUsageLimit' not in new
    assert len(backups(tmp_path)) == 1
    assert not list(tmp_path.glob('*.tmp-usage-clock'))


def test_hook_commands_have_the_exact_form(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    assert run('--settings', settings, '--target', target, '--mode', 'enforce', '--week-ok-until', '2026-01-19').returncode == 0
    new = read(settings)
    assert commands(new, 'UserPromptSubmit') == [f'python3 {quoted(target, "usage_clock.py")} || true']
    groups = {g['matcher']: g['hooks'][0]['command'] for g in new['hooks']['PreToolUse']}
    assert groups['*'] == f'python3 {quoted(target, "soft_stop.py")} --mode=enforce || true'
    assert groups['Agent|Workflow|Skill'] == (
        f'python3 {quoted(target, "spawn_gate.py")} --mode=enforce --week-ok-until=2026-01-19'
        ' || { echo "spawn gate broken - start denied" >&2; exit 2; }')
    assert 'matcher' not in new['hooks']['UserPromptSubmit'][0]
    assert new['statusLine'] == {'type': 'command', 'command': f'python3 {quoted(target, "statusline.py")}',
                                 'padding': 0, 'refreshInterval': 60}


def test_default_mode_is_shadow(tmp_path):
    settings = write_settings(tmp_path)
    assert run('--settings', settings, '--target', tmp_path / 'target').returncode == 0
    pre = commands(read(settings), 'PreToolUse')
    assert len(pre) == 2 and all('--mode=shadow' in c for c in pre)


def test_invalid_mode_is_an_argument_error(tmp_path):
    settings = write_settings(tmp_path)
    r = run('--settings', settings, '--target', tmp_path / 'target', '--mode', 'scharf')
    assert r.returncode == 2 and read(settings) == {}


def test_defaults_point_to_the_claude_folder_in_the_home_directory(tmp_path):
    """No --settings and no --target: ~/.claude/settings.json and ~/.claude/hooks/usage-clock (HOME is isolated)."""
    home = tmp_path / 'home'
    r = run()
    assert r.returncode == 2 and 'folder of --settings does not exist' in r.stdout
    (home / '.claude').mkdir(parents=True)
    (home / '.claude' / 'settings.json').write_text('{}', encoding='utf-8')
    r = run()
    assert r.returncode == 0, r.stdout
    assert (home / '.claude' / 'hooks' / 'usage-clock' / 'spawn_gate.py').is_file()
    assert quoted(home / '.claude' / 'hooks' / 'usage-clock', 'spawn_gate.py') in commands(
        read(home / '.claude' / 'settings.json'), 'PreToolUse')[0]


def test_second_run_changes_nothing_and_writes_no_second_backup(tmp_path):
    settings = write_settings(tmp_path, {'statusLine': {'type': 'command', 'command': 'x', 'refreshInterval': 30}})
    args = ('--settings', settings, '--target', tmp_path / 'target', '--replace-statusline')
    assert run(*args).returncode == 0
    once = settings.read_text(encoding='utf-8')
    r = run(*args)
    assert r.returncode == 0 and 'already up to date - not written' in r.stdout
    assert settings.read_text(encoding='utf-8') == once
    assert len(backups(tmp_path)) == 1
    assert read(settings)['statusLine']['refreshInterval'] == 30  # the existing value stays


def test_switching_shadow_to_enforce_updates_instead_of_duplicating(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args).returncode == 0
    r = run(*args, '--mode', 'enforce')
    assert r.returncode == 0 and 'updated' in r.stdout
    pre = commands(read(settings), 'PreToolUse')
    assert len(pre) == 2 and all('--mode=enforce' in c for c in pre) and not any('--mode=shadow' in c for c in pre)
    assert len(backups(tmp_path)) == 2


def test_old_matcher_is_updated_without_duplicating(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args).returncode == 0
    data = read(settings)
    gate = next(g for g in data['hooks']['PreToolUse'] if 'spawn_gate.py' in g['hooks'][0]['command'])
    gate['matcher'] = 'Agent|Workflow'  # an earlier version did not cover Skill
    settings.write_text(json.dumps(data), encoding='utf-8')
    r = run(*args)
    assert r.returncode == 0 and 'matcher Agent|Workflow -> Agent|Workflow|Skill' in r.stdout, r.stdout
    gates = [g for g in read(settings)['hooks']['PreToolUse'] if 'spawn_gate.py' in g['hooks'][0]['command']]
    assert len(gates) == 1 and gates[0]['matcher'] == 'Agent|Workflow|Skill'
    assert 'spawn_gate.py already installed - unchanged' in run(*args).stdout


def test_changing_the_matcher_leaves_foreign_hooks_in_the_same_group_alone(tmp_path):
    target = tmp_path / 'target'
    old = f'python3 {quoted(target, "spawn_gate.py")} --mode=shadow'
    foreign = {'type': 'command', 'command': 'python3 /opt/other-hook.py'}
    settings = write_settings(tmp_path, {'hooks': {'PreToolUse': [
        {'matcher': 'Agent', 'hooks': [foreign, {'type': 'command', 'command': old, 'timeout': 5}]}]}})
    assert run('--settings', settings, '--target', target, '--components', 'spawn-gate').returncode == 0
    groups = read(settings)['hooks']['PreToolUse']
    assert groups[0] == {'matcher': 'Agent', 'hooks': [foreign]}
    assert groups[1]['matcher'] == 'Agent|Workflow|Skill' and 'spawn_gate.py' in groups[1]['hooks'][0]['command']
    assert groups[1]['hooks'][0]['timeout'] == 5 and len(groups) == 2


def test_entry_with_the_same_script_in_another_folder_is_not_ours_and_is_reported(tmp_path):
    """Own entries are recognised by the full path in --target. The old place stays and is named in the output."""
    settings = write_settings(tmp_path)
    assert run('--settings', settings, '--target', tmp_path / 'old place').returncode == 0
    r = run('--settings', settings, '--target', tmp_path / 'new place')
    assert r.returncode == 0, r.stdout
    data = read(settings)
    assert len(commands(data, 'PreToolUse')) == 4 and len(commands(data, 'UserPromptSubmit')) == 2
    assert sum('old place' in c for c in commands(data, 'PreToolUse') + commands(data, 'UserPromptSubmit')) == 3
    assert 'left untouched' in r.stdout and 'old place' in r.stdout
    # how to get rid of the old entries, but only if they are an old copy of this tool
    assert (f'It was left untouched; if it is an old copy of this tool, check the path and remove it with '
            f'"install.py --uninstall --target {shlex.quote(str(tmp_path / "old place"))}"; otherwise ignore this.') in r.stdout


def test_components_select_what_is_installed(tmp_path):
    settings = write_settings(tmp_path)
    r = run('--settings', settings, '--target', tmp_path / 'target', '--components', 'usage-clock, usage-clock')
    assert r.returncode == 0
    new = read(settings)
    assert list(new['hooks']) == ['UserPromptSubmit'] and 'statusLine' not in new
    only_line = write_settings(tmp_path, name='other.json')
    assert run('--settings', only_line, '--target', tmp_path / 'target', '--components', 'statusline').returncode == 0
    assert 'hooks' not in read(only_line) and 'statusLine' in read(only_line)


def test_unknown_component_and_stray_options_are_argument_errors(tmp_path):
    settings = write_settings(tmp_path)
    base = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*base, '--components', 'usage-clock,bogus').returncode == 2
    assert run(*base, '--components', '').returncode == 2
    assert run(*base, '--surprise').returncode == 2
    assert run(*base, '--week-ok-until', '2026-01-19', '--components', 'soft-stop').returncode == 2
    assert run(*base, '--replace-statusline', '--components', 'soft-stop').returncode == 2
    assert read(settings) == {} and not (tmp_path / 'target').exists()


# --- Copying the files -------------------------------------------------------------------------------------------

def test_copies_only_runtime_files_and_creates_config_from_the_example(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    r = run('--settings', settings, '--target', target)
    assert r.returncode == 0
    # a real run says what it did, only a dry run says "would be"
    assert (f'Files: {len(RUNTIME_FILES)} runtime files copied to {target}; '
            'config.json created from config.example.json.') in r.stdout, r.stdout
    assert 'would be' not in r.stdout
    r = run('--settings', settings, '--target', target)
    assert f'runtime files copied to {target}; config.json kept as it is.' in r.stdout and 'would be' not in r.stdout
    assert sorted(os.listdir(target)) == sorted(RUNTIME_FILES + ['config.json'])
    assert (target / 'config.json').read_bytes() == open(EXAMPLE, 'rb').read()
    for name in RUNTIME_FILES:
        assert (target / name).read_bytes() == open(os.path.join(HOOKS, name), 'rb').read()


def test_an_existing_config_json_is_never_overwritten(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'config.json').write_text('{"thresholds": {"deny": 99}}', encoding='utf-8')
    r = run('--settings', settings, '--target', target)
    assert r.returncode == 0 and 'kept as it is' in r.stdout
    assert (target / 'config.json').read_text(encoding='utf-8') == '{"thresholds": {"deny": 99}}'
    assert run('--settings', settings, '--target', target, '--mode', 'enforce').returncode == 0
    assert (target / 'config.json').read_text(encoding='utf-8') == '{"thresholds": {"deny": 99}}'


def test_a_second_run_refreshes_changed_hook_files(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    assert run('--settings', settings, '--target', target).returncode == 0
    (target / 'usage_clock.py').write_text('# tampered\n', encoding='utf-8')
    assert run('--settings', settings, '--target', target).returncode == 0
    assert (target / 'usage_clock.py').read_bytes() == open(os.path.join(HOOKS, 'usage_clock.py'), 'rb').read()
    assert not list(target.glob('*.tmp-usage-clock'))


def test_file_permissions_are_kept_when_copying(repo, tmp_path):
    os.chmod(repo / 'hooks' / 'statusline.py', 0o755)
    os.chmod(repo / 'hooks' / '_config.py', 0o640)
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    assert run_in(repo, '--settings', settings, '--target', target).returncode == 0
    assert stat.S_IMODE(os.stat(target / 'statusline.py').st_mode) == 0o755
    assert stat.S_IMODE(os.stat(target / '_config.py').st_mode) == 0o640


def test_missing_source_files_stop_before_anything_is_written(repo, tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    (repo / 'hooks' / '_usage.py').unlink()
    r = run_in(repo, '--settings', settings, '--target', target)
    assert r.returncode == 1 and '_usage.py' in r.stdout
    (repo / 'config.example.json').unlink()
    (repo / 'hooks' / '_usage.py').write_text('', encoding='utf-8')
    r = run_in(repo, '--settings', settings, '--target', target)
    assert r.returncode == 1 and 'config.example.json' in r.stdout
    assert read(settings) == {} and not target.exists() and not backups(tmp_path)


def test_required_files_list_matches_the_hooks_folder():
    assert sorted(load_module().REQUIRED_FILES) == RUNTIME_FILES


def test_target_that_is_a_file_fails_without_touching_settings(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    target.write_text('not a folder', encoding='utf-8')
    r = run('--settings', settings, '--target', target)
    assert r.returncode == 1 and r.stdout.startswith('Settings:') and 'Error' in r.stdout
    assert read(settings) == {}


# --- Paths in the hook commands ------------------------------------------------------------------------------------

def test_a_target_with_spaces_and_shell_characters_is_quoted_and_harmless(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'a b;touch PWNED;$(touch PWNED2) `touch PWNED3` #x'
    r = run('--settings', settings, '--target', target)
    assert r.returncode == 0, r.stdout
    assert (target / 'usage_clock.py').is_file()
    new = read(settings)
    for command in commands(new, 'UserPromptSubmit') + commands(new, 'PreToolUse'):
        done = subprocess.run(['/bin/sh', '-c', command], input='{}', capture_output=True, text=True, timeout=30,
                              cwd=tmp_path)
        assert done.returncode in (0, 2), command  # 2 only for the spawn gate refusing the empty input
    assert not [n for n in os.listdir(tmp_path) if n.startswith('PWNED')]
    assert not [n for n in os.listdir('/tmp') if n.startswith('PWNED')]


@pytest.mark.parametrize('bad', ['dir\nname', 'dir\rname', 'dir\x01name', 'dir\x7fname'])
def test_control_characters_in_the_target_are_refused(tmp_path, bad):
    settings = write_settings(tmp_path)
    r = run('--settings', settings, '--target', str(tmp_path / bad))
    assert r.returncode == 2 and 'control characters' in r.stderr
    assert read(settings) == {} and not backups(tmp_path)


def test_a_relative_target_becomes_absolute_in_the_commands(tmp_path):
    settings = write_settings(tmp_path)
    assert run('--settings', settings, '--target', 'rel/dir', cwd=tmp_path).returncode == 0
    assert quoted(tmp_path / 'rel' / 'dir', 'usage_clock.py') in commands(read(settings), 'UserPromptSubmit')[0]


def test_the_installed_commands_work_as_written(tmp_path):
    """Wiring: the commands from settings.json start the copied hooks; with the example config (model_effort_action
    `warn`) the gate reports an unknown agent in enforce instead of denying it."""
    settings = write_settings(tmp_path)
    assert run('--settings', settings, '--target', tmp_path / 'target', '--mode', 'enforce').returncode == 0
    new = read(settings)
    prompt = subprocess.run(['/bin/sh', '-c', commands(new, 'UserPromptSubmit')[0]], input='{}', capture_output=True,
                            text=True, timeout=30)
    assert prompt.returncode == 0 and 'Now:' in json.loads(prompt.stdout)['hookSpecificOutput']['additionalContext']
    gate = next(c for c in commands(new, 'PreToolUse') if 'spawn_gate.py' in c)
    payload = {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'nobody-defined-this', 'prompt': 'x'}}
    done = subprocess.run(['/bin/sh', '-c', gate], input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    assert done.returncode == 0
    output = json.loads(done.stdout)['hookSpecificOutput']
    assert 'permissionDecision' not in output and 'nobody-defined-this' in output['additionalContext']


def test_vanished_hook_files_never_block_except_the_spawn_gate(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    assert run('--settings', settings, '--target', target, '--mode', 'enforce').returncode == 0
    new = read(settings)
    shutil.rmtree(target)  # folder moved or deleted
    for command in commands(new, 'UserPromptSubmit') + commands(new, 'PreToolUse'):
        done = subprocess.run(['/bin/sh', '-c', command], input='{}', capture_output=True, text=True, timeout=20)
        if 'spawn_gate.py' in command:
            assert done.returncode == 2 and 'spawn gate broken - start denied' in done.stderr  # fail closed
        else:
            assert done.returncode == 0, command  # the display and the soft stop must never block


# --- statusLine ---------------------------------------------------------------------------------------------------

def test_a_foreign_status_line_is_left_alone_with_a_clear_message(tmp_path):
    foreign = {'type': 'command', 'command': '/opt/my-statusline.sh', 'padding': 1}
    settings = write_settings(tmp_path, {'statusLine': foreign})
    r = run('--settings', settings, '--target', tmp_path / 'target')
    assert r.returncode == 0
    assert read(settings)['statusLine'] == foreign  # not even a refreshInterval is added
    assert 'LEFT UNCHANGED' in r.stdout and '--replace-statusline' in r.stdout and 'unknown' in r.stdout


def test_replace_statusline_overwrites_a_foreign_one_and_keeps_its_refresh_interval(tmp_path):
    settings = write_settings(tmp_path, {'statusLine': {'type': 'command', 'command': '/opt/x.sh', 'refreshInterval': 7}})
    target = tmp_path / 'target'
    r = run('--settings', settings, '--target', target, '--replace-statusline')
    assert r.returncode == 0 and 'replaced' in r.stdout
    assert read(settings)['statusLine'] == {'type': 'command', 'command': f'python3 {quoted(target, "statusline.py")}',
                                            'padding': 0, 'refreshInterval': 7}


def test_replace_statusline_without_a_refresh_interval_sets_60(tmp_path):
    settings = write_settings(tmp_path, {'statusLine': 'not even an object'})
    assert run('--settings', settings, '--target', tmp_path / 'target', '--replace-statusline').returncode == 0
    assert read(settings)['statusLine']['refreshInterval'] == 60


def test_own_status_line_gets_a_missing_refresh_interval_and_a_normalised_command(tmp_path):
    target = tmp_path / 'target'
    settings = write_settings(tmp_path, {'statusLine': {'type': 'command', 'command': f'python3 {target}/statusline.py'}})
    r = run('--settings', settings, '--target', target)
    assert r.returncode == 0
    assert read(settings)['statusLine'] == {'type': 'command', 'command': f'python3 {quoted(target, "statusline.py")}',
                                            'refreshInterval': 60}


def test_status_line_with_a_tilde_path_counts_as_ours(tmp_path):
    home = tmp_path / 'home'
    settings = write_settings(tmp_path, {'statusLine': {'type': 'command', 'command': 'python3 ~/hooks/uc/statusline.py',
                                                        'refreshInterval': 30}})
    r = run('--settings', settings, '--target', home / 'hooks' / 'uc')
    assert r.returncode == 0 and 'LEFT UNCHANGED' not in r.stdout
    assert read(settings)['statusLine']['refreshInterval'] == 30


# --- autoContinueAtUsageLimit -------------------------------------------------------------------------------------

def test_auto_continue_is_only_set_on_request(tmp_path):
    settings = write_settings(tmp_path, {'autoContinueAtUsageLimit': False})
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args).returncode == 0
    assert read(settings)['autoContinueAtUsageLimit'] is False  # not touched without the switch
    r = run(*args, '--auto-continue')
    assert r.returncode == 0 and 'autoContinueAtUsageLimit = true set' in r.stdout
    assert read(settings)['autoContinueAtUsageLimit'] is True
    assert 'already true - unchanged' in run(*args, '--auto-continue').stdout


# --- --week-ok-until ----------------------------------------------------------------------------------------------

def test_week_ok_until_goes_only_to_the_spawn_gate_and_is_validated(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target', '--mode', 'enforce')
    assert run(*args, '--week-ok-until', '2026-01-19').returncode == 0
    pre = commands(read(settings), 'PreToolUse')
    assert [c for c in pre if '--week-ok-until=2026-01-19' in c] == [c for c in pre if 'spawn_gate.py' in c]
    assert '--week-ok-until' not in ' '.join(commands(read(settings), 'UserPromptSubmit'))
    before = settings.read_text(encoding='utf-8')
    for wrong in ('19.01.2026', '2026-13-01', '2026-01-19;rm', '', '2026-1-9'):
        r = run(*args, '--week-ok-until', wrong)
        assert r.returncode == 2 and '--week-ok-until' in r.stderr, wrong
    assert settings.read_text(encoding='utf-8') == before
    assert run(*args).returncode == 0  # without the option an earlier approval is removed
    assert not any('--week-ok-until' in c for c in commands(read(settings), 'PreToolUse'))


# --- Dry run ------------------------------------------------------------------------------------------------------

def test_dry_run_shows_the_changes_and_writes_nothing(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    r = run('--settings', settings, '--target', target, '--dry-run')
    assert r.returncode == 0, r.stdout
    assert 'DRY RUN - nothing written' in r.stdout and 'Smoke test: passed' in r.stdout
    assert 'spawn_gate.py added' in r.stdout and 'would be copied' in r.stdout and 'would be created' in r.stdout
    assert settings.read_text(encoding='utf-8') == '{}'
    assert not target.exists() and not backups(tmp_path) and os.listdir(tmp_path) == ['settings.json']


def test_dry_run_of_a_finished_installation_says_so(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args).returncode == 0
    r = run(*args, '--dry-run')
    assert r.returncode == 0 and 'settings.json would stay as it is' in r.stdout and 'stays untouched' in r.stdout


# --- Smoke test ---------------------------------------------------------------------------------------------------

def test_smoke_test_stops_everything_when_a_hook_crashes(repo, tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    (repo / 'hooks' / 'soft_stop.py').write_text('import does_not_exist\n', encoding='utf-8')
    r = run_in(repo, '--settings', settings, '--target', target)
    assert r.returncode == 1 and 'SMOKE TEST FAILED' in r.stdout and 'soft_stop.py: exit 1' in r.stdout
    assert read(settings) == {} and not backups(tmp_path) and not target.exists()


@pytest.mark.parametrize('script,content,expected', [
    ('usage_clock.py', 'print("not json")\n', 'output is not valid JSON'),
    ('usage_clock.py', 'print("{}")\n', 'output is not the expected JSON'),
    ('usage_clock.py', 'print(\'{"hookSpecificOutput": {"additionalContext": "Now: x"}}\')\n', 'no usage part'),
    ('usage_clock.py', 'print(\'{"hookSpecificOutput": {"additionalContext": "Usage: x"}}\')\n', 'no time part'),
    ('statusline.py', 'print()\n', 'no status line output'),
    ('soft_stop.py', 'raise SystemExit(0)\n', 'wrote no log line'),
    ('spawn_gate.py', 'import time\ntime.sleep(60)\n', 'no answer within'),
])
def test_smoke_test_rejects_hooks_that_exit_cleanly_but_do_nothing_useful(repo, tmp_path, script, content, expected):
    settings = write_settings(tmp_path)
    (repo / 'hooks' / script).write_text(content, encoding='utf-8')
    r = run_in(repo, '--settings', settings, '--target', tmp_path / 'target', timeout=2)
    assert r.returncode == 1 and 'SMOKE TEST FAILED' in r.stdout and expected in r.stdout, r.stdout
    assert read(settings) == {}


def test_a_hook_that_fails_internally_does_not_pass_on_its_log_line_alone(repo, tmp_path):
    """The spawn gate logs even when it fails internally: a log line is not enough, the decision must be sane."""
    settings = write_settings(tmp_path)
    (repo / 'hooks' / '_usage.py').write_text('def usage_path():\n    raise RuntimeError("broken")\n', encoding='utf-8')
    r = run_in(repo, '--settings', settings, '--target', tmp_path / 'target', '--components', 'spawn-gate,soft-stop')
    assert r.returncode == 1 and 'spawn_gate.py: failed internally' in r.stdout
    assert 'soft_stop.py' in r.stdout
    assert read(settings) == {}


def test_smoke_test_only_starts_the_selected_components(repo, tmp_path):
    settings = write_settings(tmp_path)
    (repo / 'hooks' / 'soft_stop.py').write_text('import does_not_exist\n', encoding='utf-8')
    r = run_in(repo, '--settings', settings, '--target', tmp_path / 'target', '--components', 'usage-clock')
    assert r.returncode == 0, r.stdout


def test_smoke_test_uses_the_chosen_mode(repo, tmp_path):
    """In enforce the hooks may print JSON (a warning or a denial); the smoke test must accept it."""
    settings = write_settings(tmp_path)
    r = run_in(repo, '--settings', settings, '--target', tmp_path / 'target', '--mode', 'enforce', '--dry-run')
    assert r.returncode == 0 and 'Smoke test: passed' in r.stdout


def test_smoke_test_never_touches_the_real_usage_file_or_log(tmp_path):
    """The smoke test runs with its own usage file and log folder (conftest points the defaults to tmp_path)."""
    settings = write_settings(tmp_path)
    assert run('--settings', settings, '--target', tmp_path / 'target', '--dry-run').returncode == 0
    assert not (tmp_path / 'usage.json').exists() and not (tmp_path / 'log').exists()


# --- settings.json handling: backup, permissions, atomic write, symlinks, bad content ------------------------------

def test_backup_holds_the_old_content_and_the_permissions_survive(tmp_path):
    settings = write_settings(tmp_path, '{"model": "opus"}')
    os.chmod(settings, 0o600)
    assert run('--settings', settings, '--target', tmp_path / 'target').returncode == 0
    assert stat.S_IMODE(os.stat(settings).st_mode) == 0o600
    (backup,) = backups(tmp_path)
    assert re.fullmatch(r'settings\.json\.bak-usage-clock-\d{8}-\d{6}-\d{6}', backup.name)
    assert backup.read_text(encoding='utf-8') == '{"model": "opus"}'
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    text = settings.read_text(encoding='utf-8')
    assert text.endswith('\n') and json.loads(text)['model'] == 'opus'


def test_output_names_the_backup_and_how_to_undo(tmp_path):
    settings = write_settings(tmp_path)
    r = run('--settings', settings, '--target', tmp_path / 'target')
    (backup,) = backups(tmp_path)
    assert f'Backup:  {backup}' in r.stdout and f'Undo:    cp {shlex.quote(str(backup))} {shlex.quote(str(settings))}' in r.stdout


def test_a_missing_settings_file_is_created_without_a_backup(tmp_path):
    settings = tmp_path / 'settings.json'
    r = run('--settings', settings, '--target', tmp_path / 'target')
    assert r.returncode == 0 and 'does not exist yet' in r.stdout
    assert 'hooks' in read(settings) and not backups(tmp_path)


def test_a_missing_folder_for_settings_is_an_argument_error(tmp_path):
    r = run('--settings', tmp_path / 'nowhere' / 'settings.json', '--target', tmp_path / 'target')
    assert r.returncode == 2 and not (tmp_path / 'target').exists()


def test_symlink_and_non_file_settings_are_refused(tmp_path):
    settings = write_settings(tmp_path)
    link = tmp_path / 'link.json'
    link.symlink_to(settings)
    r = run('--settings', link, '--target', tmp_path / 'target')
    assert r.returncode == 2 and 'symlink' in r.stdout and link.is_symlink() and read(settings) == {}
    r = run('--settings', tmp_path, '--target', tmp_path / 'target')
    assert r.returncode == 2 and 'regular file' in r.stdout


def test_a_leftover_temp_file_symlink_is_never_written_through(tmp_path):
    settings = write_settings(tmp_path)
    victim = tmp_path / 'victim.txt'
    victim.write_text('keep me', encoding='utf-8')
    (tmp_path / 'settings.json.tmp-usage-clock').symlink_to(victim)
    assert run('--settings', settings, '--target', tmp_path / 'target').returncode == 0
    assert victim.read_text(encoding='utf-8') == 'keep me' and 'hooks' in read(settings)


@pytest.mark.parametrize('content', ['{broken', '', '[]', '"text"', '{"hooks": []}', '{"hooks": {"PreToolUse": {}}}',
                                     '{"hooks": {"PreToolUse": [5]}}', '{"hooks": {"UserPromptSubmit": [{"hooks": [1]}]}}',
                                     '{"hooks": {"PreToolUse": [{"hooks": {}}]}}'])
def test_unusable_settings_are_refused_and_left_alone(tmp_path, content):
    settings = write_settings(tmp_path, content)
    for extra in ([], ['--uninstall']):
        r = run('--settings', settings, '--target', tmp_path / 'target', *extra)
        assert r.returncode == 2 and 'Error' in r.stdout and 'Traceback' not in r.stdout + r.stderr, r.stdout
    assert settings.read_text(encoding='utf-8') == content
    assert not backups(tmp_path) and not (tmp_path / 'target').exists()


def test_backup_never_overwrites_an_existing_one(tmp_path, monkeypatch):
    module = load_module()

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 14, 9, 8, 7, 123456, tzinfo=tz)

    monkeypatch.setattr(module, 'datetime', Fixed)
    settings = tmp_path / 'settings.json'
    settings.write_text('{"new": 1}', encoding='utf-8')
    first = tmp_path / 'settings.json.bak-usage-clock-20260114-090807-123456'
    first.write_text('FIRST BACKUP', encoding='utf-8')
    second = module.make_backup(str(settings))
    third = module.make_backup(str(settings))
    assert first.read_text(encoding='utf-8') == 'FIRST BACKUP'
    assert second.endswith('-123456-1') and third.endswith('-123456-2')
    assert open(second, encoding='utf-8').read() == '{"new": 1}'


def test_backup_gives_up_when_all_names_are_taken(tmp_path, monkeypatch):
    module = load_module()

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 14, 9, 8, 7, 123456, tzinfo=tz)

    monkeypatch.setattr(module, 'datetime', Fixed)
    settings = tmp_path / 'settings.json'
    settings.write_text('{}', encoding='utf-8')
    for n in range(1000):
        (tmp_path / f'settings.json.bak-usage-clock-20260114-090807-123456{"-" + str(n) if n else ""}').write_text('x')
    with pytest.raises(RuntimeError):
        module.make_backup(str(settings))


def test_hook_commands_with_broken_quoting_are_not_ours_and_stay(tmp_path):
    broken = {'matcher': '*', 'hooks': [{'type': 'command', 'command': 'python3 "unterminated soft_stop.py'}]}
    settings = write_settings(tmp_path, {'hooks': {'PreToolUse': [broken]}})
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args).returncode == 0
    assert read(settings)['hooks']['PreToolUse'][0] == broken and len(read(settings)['hooks']['PreToolUse']) == 3
    assert run(*args, '--uninstall').returncode == 0
    assert read(settings) == {'hooks': {'PreToolUse': [broken]}}


def test_log_records_survive_a_missing_or_garbled_log(tmp_path):
    module = load_module()
    assert module._log_records(str(tmp_path / 'nowhere'), 'spawn_gate') == []
    (tmp_path / 'log.jsonl').write_text('{"hook": "spawn_gate"}\nnot json\n', encoding='utf-8')
    assert module._log_records(str(tmp_path), 'spawn_gate') == []
    (tmp_path / 'log.jsonl').write_text('{"hook": "spawn_gate", "decision": "allow"}\n[1]\n', encoding='utf-8')
    assert module._log_records(str(tmp_path), 'spawn_gate') == [{'hook': 'spawn_gate', 'decision': 'allow'}]


def test_a_smoke_test_timeout_is_reported(monkeypatch):
    module = load_module()

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired('x', 1)

    monkeypatch.setattr(module.subprocess, 'run', slow)
    args = module.parse_args(['--components', 'usage-clock'])
    assert module.smoke_test(args, {}) == [f'usage_clock.py: no answer within {module.SMOKE_TIMEOUT_S} s']


# --- Uninstall ----------------------------------------------------------------------------------------------------

def test_uninstall_removes_only_our_entries_and_keeps_the_files(tmp_path):
    guard = {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'python3 ~/guard.py'}]}
    settings = write_settings(tmp_path, {'model': 'opus', 'autoContinueAtUsageLimit': True,
                                         'hooks': {'PreToolUse': [guard]}})
    target = tmp_path / 'target'
    args = ('--settings', settings, '--target', target)
    assert run(*args, '--replace-statusline').returncode == 0
    r = run(*args, '--uninstall')
    assert r.returncode == 0, r.stdout
    new = read(settings)
    assert new == {'model': 'opus', 'autoContinueAtUsageLimit': True, 'hooks': {'PreToolUse': [guard]}}
    assert sorted(os.listdir(target)) == sorted(RUNTIME_FILES + ['config.json'])  # no file is deleted
    assert len(backups(tmp_path)) == 2 and 'Backup:' in r.stdout


def test_uninstall_removes_emptied_events_and_the_hooks_key(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args).returncode == 0
    assert run(*args, '--uninstall').returncode == 0
    assert read(settings) == {}


def test_uninstall_keeps_a_foreign_status_line_and_says_so(tmp_path):
    foreign = {'type': 'command', 'command': '/opt/my-statusline.sh'}
    settings = write_settings(tmp_path, {'statusLine': foreign})
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args).returncode == 0
    r = run(*args, '--uninstall')
    assert r.returncode == 0 and 'does not run our script - left unchanged' in r.stdout
    assert read(settings)['statusLine'] == foreign


def test_uninstall_ignores_entries_that_point_to_another_folder(tmp_path):
    settings = write_settings(tmp_path)
    assert run('--settings', settings, '--target', tmp_path / 'one').returncode == 0
    before = settings.read_text(encoding='utf-8')
    r = run('--settings', settings, '--target', tmp_path / 'two', '--uninstall')
    assert r.returncode == 0 and 'not found - unchanged' in r.stdout
    assert settings.read_text(encoding='utf-8') == before and len(backups(tmp_path)) == 1


def test_uninstall_does_not_remove_a_foreign_hook_with_the_same_script_name(tmp_path):
    foreign = {'matcher': '*', 'hooks': [{'type': 'command', 'command': 'python3 /opt/mine/soft_stop.py'}]}
    settings = write_settings(tmp_path, {'hooks': {'PreToolUse': [foreign]}})
    r = run('--settings', settings, '--target', tmp_path / 'target', '--uninstall')
    assert r.returncode == 0 and read(settings) == {'hooks': {'PreToolUse': [foreign]}}


def test_uninstall_with_components_removes_only_those(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args).returncode == 0
    assert run(*args, '--uninstall', '--components', 'soft-stop,statusline').returncode == 0
    new = read(settings)
    assert 'statusLine' not in new and len(commands(new, 'PreToolUse')) == 1 and 'spawn_gate.py' in commands(new, 'PreToolUse')[0]
    assert len(commands(new, 'UserPromptSubmit')) == 1


def test_uninstall_in_a_group_with_other_hooks_keeps_the_others(tmp_path):
    target = tmp_path / 'target'
    ours = {'type': 'command', 'command': f'python3 {quoted(target, "soft_stop.py")} --mode=shadow || true'}
    other = {'type': 'command', 'command': 'python3 /opt/other.py'}
    settings = write_settings(tmp_path, {'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [other, ours]}]}})
    assert run('--settings', settings, '--target', target, '--uninstall').returncode == 0
    assert read(settings) == {'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [other]}]}}


def test_uninstall_without_anything_to_remove_writes_nothing(tmp_path):
    settings = write_settings(tmp_path, {'model': 'opus'})
    r = run('--settings', settings, '--target', tmp_path / 'target', '--uninstall')
    assert r.returncode == 0 and 'already up to date' in r.stdout
    assert read(settings) == {'model': 'opus'} and not backups(tmp_path) and not (tmp_path / 'target').exists()
    r = run('--settings', tmp_path / 'absent.json', '--target', tmp_path / 'target', '--uninstall')
    assert r.returncode == 0 and not (tmp_path / 'absent.json').exists()


def test_uninstall_dry_run_writes_nothing(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args).returncode == 0
    before = settings.read_text(encoding='utf-8')
    r = run(*args, '--uninstall', '--dry-run')
    assert r.returncode == 0 and 'removed' in r.stdout and 'DRY RUN' in r.stdout
    assert settings.read_text(encoding='utf-8') == before and len(backups(tmp_path)) == 1


# --- The mode of an existing installation is kept -----------------------------------------------------------------

def gate_commands(settings):
    return {script: c for c in commands(read(settings), 'PreToolUse') for script in ('spawn_gate.py', 'soft_stop.py') if script in c}


def test_a_first_install_without_mode_is_shadow_and_says_where_that_comes_from(tmp_path):
    settings = write_settings(tmp_path)
    r = run('--settings', settings, '--target', tmp_path / 'target')
    assert r.returncode == 0 and 'Mode:     shadow (default, nothing installed yet)' in r.stdout, r.stdout


def test_a_rerun_without_mode_keeps_enforce_and_says_so(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args, '--mode', 'enforce').returncode == 0
    r = run(*args, '--week-ok-until', '2026-01-19')
    assert r.returncode == 0, r.stdout
    assert 'Mode:     enforce (kept from the existing installation)' in r.stdout
    gates = gate_commands(settings)
    assert '--mode=enforce' in gates['spawn_gate.py'] and '--mode=enforce' in gates['soft_stop.py']
    assert not any('--mode=shadow' in c for c in commands(read(settings), 'PreToolUse'))


def test_a_rerun_without_mode_of_an_enforce_installation_is_a_no_op(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args, '--mode', 'enforce').returncode == 0
    before = settings.read_text(encoding='utf-8')
    r = run(*args)
    assert r.returncode == 0 and 'kept from the existing installation' in r.stdout
    assert 'already up to date - not written' in r.stdout
    assert settings.read_text(encoding='utf-8') == before and len(backups(tmp_path)) == 1


def test_an_explicit_mode_beats_the_kept_one_in_both_directions(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args, '--mode', 'enforce').returncode == 0
    r = run(*args, '--mode', 'shadow')
    assert 'Mode:     shadow (--mode)' in r.stdout
    assert all('--mode=shadow' in c for c in gate_commands(settings).values())
    r = run(*args, '--mode', 'enforce')
    assert 'Mode:     enforce (--mode)' in r.stdout
    assert all('--mode=enforce' in c for c in gate_commands(settings).values())


def test_every_gate_keeps_its_own_mode(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    settings.write_text(json.dumps({'hooks': {'PreToolUse': [
        {'matcher': 'Agent|Workflow|Skill', 'hooks': [{'type': 'command', 'timeout': 5, 'command':
            f'python3 {quoted(target, "spawn_gate.py")} --mode=enforce'}]},
        {'matcher': '*', 'hooks': [{'type': 'command', 'timeout': 5, 'command':
            f'python3 {quoted(target, "soft_stop.py")} --mode=shadow || true'}]}]}}), encoding='utf-8')
    r = run('--settings', settings, '--target', target)
    assert r.returncode == 0, r.stdout
    assert ('Mode:     spawn-gate enforce (kept from the existing installation); '
            'soft-stop shadow (kept from the existing installation)') in r.stdout
    gates = gate_commands(settings)
    assert '--mode=enforce' in gates['spawn_gate.py'] and '--mode=shadow' in gates['soft_stop.py']


# The installer takes over the mode the installed hook really runs in (_hookio.parse_args): the LAST --mode=
# counts, and only exactly 'enforce' is enforce.
@pytest.mark.parametrize('options,mode,source', [
    ('--mode=enforce --mode=shadow', 'shadow', 'kept from the existing installation)'),
    ('--mode=shadow --mode=enforce', 'enforce', 'kept from the existing installation)'),
    ('--mode=ENFORCE', 'shadow', 'whose hook runs as shadow with --mode=ENFORCE)'),
    ('--mode=', 'shadow', 'whose hook runs as shadow with --mode=)'),
    ('--mode enforce', 'shadow', 'whose hook runs as shadow with no --mode=)'),
    ('--mode=bogus', 'shadow', 'whose hook runs as shadow with --mode=bogus)'),
])
def test_the_kept_mode_is_the_one_the_installed_hook_runs_in(tmp_path, options, mode, source):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    settings.write_text(json.dumps({'hooks': {'PreToolUse': [
        {'matcher': 'Agent|Workflow|Skill', 'hooks': [{'type': 'command', 'command':
            f'python3 {quoted(target, "spawn_gate.py")} {options} || true'}]}]}}), encoding='utf-8')
    r = run('--settings', settings, '--target', target, '--components', 'spawn-gate')
    assert r.returncode == 0 and f'Mode:     {mode} (kept from the existing installation' in r.stdout, r.stdout
    assert source in r.stdout, r.stdout
    command = gate_commands(settings)['spawn_gate.py']
    assert command.count('--mode=') == 1 and f'--mode={mode}' in command, command
    # the hook itself agrees: the same arguments give the same mode
    import _hookio
    assert _hookio.parse_args(shlex.split(options))['mode'] == mode


def test_option_value_takes_the_last_value_like_the_hooks():
    module = load_module()
    assert module.option_value('python3 x.py --week-ok-until=2026-01-01 --week-ok-until=2026-02-02',
                               'week-ok-until') == '2026-02-02'
    assert module.option_value('python3 x.py --mode=shadow', 'week-ok-until') is None


def test_the_mode_of_an_entry_in_another_folder_is_not_taken_over(tmp_path):
    settings = write_settings(tmp_path)
    assert run('--settings', settings, '--target', tmp_path / 'old', '--mode', 'enforce').returncode == 0
    r = run('--settings', settings, '--target', tmp_path / 'new')
    assert 'Mode:     shadow (default, nothing installed yet)' in r.stdout
    assert all('--mode=shadow' in c for c in commands(read(settings), 'PreToolUse') if '/new/' in c)


def test_without_a_gate_there_is_no_mode_line(tmp_path):
    settings = write_settings(tmp_path)
    r = run('--settings', settings, '--target', tmp_path / 'target', '--components', 'usage-clock,statusline')
    assert r.returncode == 0 and 'Mode:' not in r.stdout and 'Mode shadow' not in r.stdout


def test_a_dry_run_shows_the_kept_mode(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args, '--mode', 'enforce').returncode == 0
    r = run(*args, '--dry-run')
    assert r.returncode == 0 and 'kept from the existing installation' in r.stdout


def test_the_shadow_hint_only_appears_when_a_gate_is_in_shadow(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert 'Mode shadow: nothing is blocked' in run(*args).stdout
    assert 'Mode shadow' not in run(*args, '--mode', 'enforce').stdout


# --- A rerun without --week-ok-until says that it removes the approval ----------------------------------------------

def test_a_rerun_without_week_ok_until_reports_the_removed_approval(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert run(*args, '--week-ok-until', '2026-01-19').returncode == 0
    r = run(*args, '--components', 'spawn-gate,soft-stop')
    assert r.returncode == 0, r.stdout
    assert ('spawn_gate.py: the weekly approval --week-ok-until=2026-01-19 is removed, this run has no '
            '--week-ok-until') in r.stdout
    assert not any('--week-ok-until' in c for c in commands(read(settings), 'PreToolUse'))


def test_no_approval_line_when_there_was_none_or_it_is_kept_or_the_gate_is_not_selected(tmp_path):
    settings = write_settings(tmp_path)
    args = ('--settings', settings, '--target', tmp_path / 'target')
    assert 'weekly approval' not in run(*args).stdout
    assert run(*args, '--week-ok-until', '2026-01-19').returncode == 0
    assert 'weekly approval' not in run(*args, '--week-ok-until', '2026-01-20').stdout
    r = run(*args, '--components', 'soft-stop')
    assert 'weekly approval' not in r.stdout
    assert any('--week-ok-until=2026-01-20' in c for c in commands(read(settings), 'PreToolUse'))


# --- The spawn gate fallback depends on the mode ---------------------------------------------------------------------

def test_the_shadow_command_of_the_spawn_gate_ends_with_or_true(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    assert run('--settings', settings, '--target', target).returncode == 0
    assert gate_commands(settings)['spawn_gate.py'] == f'python3 {quoted(target, "spawn_gate.py")} --mode=shadow || true'
    assert run('--settings', settings, '--target', target, '--mode', 'enforce').returncode == 0
    assert gate_commands(settings)['spawn_gate.py'].endswith(
        ' || { echo "spawn gate broken - start denied" >&2; exit 2; }')
    assert run('--settings', settings, '--target', target, '--mode', 'shadow').returncode == 0
    assert gate_commands(settings)['spawn_gate.py'].endswith(' --mode=shadow || true')


def test_in_shadow_a_broken_installation_blocks_nothing(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    assert run('--settings', settings, '--target', target).returncode == 0
    new = read(settings)
    shutil.rmtree(target)  # folder moved or deleted
    for command in commands(new, 'UserPromptSubmit') + commands(new, 'PreToolUse'):
        done = subprocess.run(['sh', '-c', command], input='{}', capture_output=True, text=True, timeout=20)
        assert done.returncode == 0, (command, done.stderr)


def test_a_script_that_dies_with_exit_2_does_not_block_in_shadow(tmp_path):
    """Not only a vanished folder: a script that ends with exit 2 must not turn into a block in shadow."""
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    assert run('--settings', settings, '--target', target).returncode == 0
    (target / 'spawn_gate.py').write_text('import sys\nsys.exit(2)\n', encoding='utf-8')
    done = subprocess.run(['sh', '-c', gate_commands(settings)['spawn_gate.py']], input='{}',
                          capture_output=True, text=True, timeout=20)
    assert done.returncode == 0


# --- Copying files never writes through a symlink -------------------------------------------------------------------

def test_a_temp_file_symlink_at_the_target_is_never_written_through(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    target.mkdir()
    victim = tmp_path / 'victim.txt'
    victim.write_text('keep me', encoding='utf-8')
    (target / '_config.py.tmp-usage-clock').symlink_to(victim)
    r = run('--settings', settings, '--target', target)
    assert r.returncode == 0, r.stdout
    assert victim.read_text(encoding='utf-8') == 'keep me'
    installed = target / '_config.py'
    assert not installed.is_symlink() and installed.read_bytes() == open(os.path.join(HOOKS, '_config.py'), 'rb').read()
    assert not list(target.glob('*.tmp-usage-clock'))


def test_a_leftover_regular_temp_file_is_replaced_not_reused(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    target.mkdir()
    (target / '_config.py.tmp-usage-clock').write_text('leftover of an aborted run', encoding='utf-8')
    r = run('--settings', settings, '--target', target)
    assert r.returncode == 0, r.stdout
    assert (target / '_config.py').read_bytes() == open(os.path.join(HOOKS, '_config.py'), 'rb').read()
    assert not list(target.glob('*.tmp-usage-clock'))


def test_a_symlink_that_appears_right_after_the_cleanup_aborts_the_copy(tmp_path, monkeypatch):
    """Race: someone plants the link between the removal of the old temp file and the exclusive creation."""
    module = load_module()
    target = tmp_path / 'target'
    victim = tmp_path / 'victim.txt'
    victim.write_text('keep me', encoding='utf-8')
    real_unlink = os.unlink

    def unlink_then_plant(path, *args, **kwargs):
        try:
            real_unlink(path, *args, **kwargs)
        finally:
            if str(path).endswith('.tmp-usage-clock') and not os.path.lexists(path):
                os.symlink(str(victim), path)

    monkeypatch.setattr(module.os, 'unlink', unlink_then_plant)
    with pytest.raises(OSError):
        module.copy_runtime_files(str(target))
    assert victim.read_text(encoding='utf-8') == 'keep me'
    assert not (target / '_config.py').exists()


def test_a_failing_copy_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    module = load_module()
    target = tmp_path / 'target'

    def broken(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(module.shutil, 'copyfileobj', broken)
    with pytest.raises(OSError):
        module.copy_runtime_files(str(target))
    assert not list(target.glob('*.tmp-usage-clock'))


def test_a_dangling_config_json_symlink_is_not_written_through(tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    target.mkdir()
    victim = tmp_path / 'not-yet-there.json'
    (target / 'config.json').symlink_to(victim)
    r = run('--settings', settings, '--target', target)
    assert r.returncode == 0 and 'kept as it is' in r.stdout, r.stdout
    assert not victim.exists() and (target / 'config.json').is_symlink()


def test_config_json_gets_the_permissions_of_the_example(repo, tmp_path):
    os.chmod(repo / 'config.example.json', 0o640)
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    assert run_in(repo, '--settings', settings, '--target', target).returncode == 0
    assert stat.S_IMODE(os.stat(target / 'config.json').st_mode) == 0o640


# --- Own entries are recognised by the full path, not by the file name ------------------------------------------------

def test_a_foreign_hook_with_the_same_script_name_stays_and_is_reported(tmp_path):
    foreign = {'matcher': 'Bash', 'hooks': [{'type': 'command',
                                             'command': 'python3 /opt/company-guard/soft_stop.py --block-rm'}]}
    settings = write_settings(tmp_path, {'hooks': {'PreToolUse': [foreign]}})
    target = tmp_path / 'target'
    args = ('--settings', settings, '--target', target)
    r = run(*args, '--components', 'soft-stop')
    assert r.returncode == 0, r.stdout
    groups = read(settings)['hooks']['PreToolUse']
    assert groups[0] == foreign and len(groups) == 2 and groups[1]['matcher'] == '*'
    assert quoted(target, 'soft_stop.py') in groups[1]['hooks'][0]['command']
    assert ('It was left untouched; if it is an old copy of this tool, check the path and remove it with '
            '"install.py --uninstall --target /opt/company-guard"; otherwise ignore this.') in r.stdout, r.stdout
    assert "'python3 /opt/company-guard/soft_stop.py --block-rm'" in r.stdout
    assert 'soft_stop.py added' in r.stdout
    # a second run updates only ours, and the uninstall takes only ours away
    assert run(*args, '--components', 'soft-stop', '--mode', 'enforce').returncode == 0
    groups = read(settings)['hooks']['PreToolUse']
    assert groups[0] == foreign and '--mode=enforce' in groups[1]['hooks'][0]['command'] and len(groups) == 2
    assert run(*args, '--uninstall').returncode == 0
    assert read(settings) == {'hooks': {'PreToolUse': [foreign]}}


def test_a_foreign_hook_without_a_folder_gets_no_uninstall_command(tmp_path):
    settings = write_settings(tmp_path, {'hooks': {'PreToolUse': [
        {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'python3 soft_stop.py'}]}]}})
    r = run('--settings', settings, '--target', tmp_path / 'target', '--components', 'soft-stop')
    assert r.returncode == 0, r.stdout
    assert ('It was left untouched; if it is an old copy of this tool, check the path and remove the entry from '
            'settings.json by hand; otherwise ignore this.') in r.stdout, r.stdout
    assert '--uninstall --target' not in r.stdout


def test_a_foreign_usage_clock_or_gate_with_the_same_name_is_not_taken_either(tmp_path):
    other = {'hooks': [{'type': 'command', 'command': 'python3 /opt/x/usage_clock.py'}]}
    gate = {'matcher': 'Agent', 'hooks': [{'type': 'command', 'command': '/opt/x/spawn_gate.py'}]}
    settings = write_settings(tmp_path, {'hooks': {'UserPromptSubmit': [other], 'PreToolUse': [gate]}})
    assert run('--settings', settings, '--target', tmp_path / 'target').returncode == 0
    new = read(settings)['hooks']
    assert new['UserPromptSubmit'][0] == other and len(new['UserPromptSubmit']) == 2
    assert new['PreToolUse'][0] == gate and len(new['PreToolUse']) == 3


def test_a_failing_config_copy_leaves_no_half_written_config_json(tmp_path, monkeypatch):
    """A config.json is never overwritten later, so a broken one must not be left behind."""
    module = load_module()
    target = tmp_path / 'target'
    target.mkdir()

    def broken(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(module.shutil, 'copyfileobj', broken)
    with pytest.raises(OSError):
        module.create_config(str(target))
    assert not (target / 'config.json').exists()


def test_the_original_error_survives_when_the_temp_file_is_already_gone(tmp_path, monkeypatch):
    """A failing os.replace after someone removed the temp file: the caller sees the replace error, not a second one."""
    module = load_module()
    target = tmp_path / 'target'

    def replace_after_loss(source, destination):
        os.unlink(source)
        raise PermissionError('replace refused')

    monkeypatch.setattr(module.os, 'replace', replace_after_loss)
    with pytest.raises(PermissionError, match='replace refused'):
        module.copy_runtime_files(str(target))
    assert not list(target.glob('*.tmp-usage-clock'))


def test_the_context_state_module_is_required_and_copied(repo, tmp_path):
    settings = write_settings(tmp_path)
    target = tmp_path / 'target'
    assert '_context_state.py' in load_module().REQUIRED_FILES
    (repo / 'hooks' / '_context_state.py').unlink()
    r = run_in(repo, '--settings', settings, '--target', target)
    assert r.returncode == 1 and '_context_state.py' in r.stdout and not target.exists()
    (repo / 'hooks' / '_context_state.py').write_text('', encoding='utf-8')
    assert run_in(repo, '--settings', settings, '--target', target).returncode == 0
    assert (target / '_context_state.py').is_file()


def test_a_broken_context_state_module_fails_the_status_line_smoke_test(repo, tmp_path):
    settings = write_settings(tmp_path)
    (repo / 'hooks' / '_context_state.py').write_text('def (:\n', encoding='utf-8')
    r = run_in(repo, '--settings', settings, '--target', tmp_path / 'target', '--components', 'statusline')
    assert r.returncode == 1 and 'SMOKE TEST FAILED' in r.stdout and 'statusline.py' in r.stdout, r.stdout
    assert read(settings) == {}
