#!/usr/bin/env python3
"""The one way the Settings tab of /report (desktop/hooks/pane.tsx) changes the configuration: switch a part on or off
and set the mode of the gates and of the sync in config.json, and check and set the sync repository. It never writes
settings.json and adds or removes no hook: the parts read their switch from config.json on every run.

  show --json                  per part: installed (as install.py --check sees it in settings.json), its switch, the
                               texts of the assistant's `i` and a sentence for the tab; the sync: repo, mode, last
                               success, last error
  set modules.<name> <value>   usage_clock and context_reporter: true|false; spawn_gate and soft_stop: off|shadow|
                               enforce, or "" for the mode of the hook entry
  set sync.mode off|shadow|on
  check-repo OWNER/NAME        the test of install.py --sync-repo (owner/name, not readable without logging in,
                               reachable with the login of git), writes nothing
  set-repo OWNER/NAME          check-repo, and only if the repository is private and reachable what install.py
                               --components sync --sync-repo does for the sync: sync.repo, the clone under the sync
                               lock, its size limit

Each key is written as the installer writes it (_setup.write_keys: checked, with a backup, atomically), and only when it
changes. Every answer is one JSON object on stdout. Exit codes: 0 done, 1 the repository did not pass or could not be
cloned, or a file operation failed ({"error": ...}), 2 bad arguments, a part that is not installed (install.py installs it), or an unusable settings.json or
config.json.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time

import _config
import _setup
import _sync_core
import _sync_git

DEFAULT_SETTINGS = '~/.claude/settings.json'
# (id in show, component of install.py, key in modules, label)
PARTS = (('statusline', 'statusline', None, 'Status line'),
         ('usage_clock', 'usage-clock', 'usage_clock', 'Usage clock'),
         ('context_reporter', 'context-reporter', 'context_reporter', 'Context reporter'),
         ('spawn_gate', 'spawn-gate', 'spawn_gate', 'Spawn gate'),
         ('soft_stop', 'soft-stop', 'soft_stop', 'Soft stop'))
SWITCH_VALUES = ('true', 'false')
GATE_VALUES = ('off', 'shadow', 'enforce')
SYNC_MODES = ('off', 'shadow', 'on')
RESULTS = {
    'private_ok': '{repo} is private and this login reaches it.',
    'public': '{repo} can be read without logging in: it is public. The sync writes only to a private repository.',
    'unclear': 'Whether {repo} is private and reachable could not be told (no network, or an answer this tool does not '
               'know). Try again when GitHub can be reached.',
    'not_found_or_no_access': '{repo} does not exist, or this login cannot reach it: check the name, and that git is '
                              'signed in to GitHub.',
    'invalid': '{repo} is no owner/name as on GitHub, for example anna/usage-sync.',
    'other_clone': 'The tab does not change the repository while {clone} holds a clone of another one (or something '
                   'else). Move that folder away, do not delete it: it may hold commits that were never pushed. Then '
                   'save again.',
}


def answer(value: dict, code: int = 0) -> int:
    print(json.dumps(value, ensure_ascii=False))
    return code


def not_installed(component: str) -> _setup.UsageError:
    return _setup.UsageError(f'{component} is not installed. Run tools/install.py (with --components {component}) to '
                             'install it first.')


def show(data: dict, parts: set[str], target: str, path: str) -> int:
    problems: list[str] = []
    cfg = _config.load(log_problems=False, path=path, problems=problems)
    modules = []
    for part_id, component, key, label in PARTS:
        entry = {'id': part_id, 'label': label, 'installed': component in parts, 'summary': _setup.TAB_SUMMARY[component],
                 'info': '\n'.join(_setup.INFO[component]), 'tab': _setup.TAB[component]}
        if key is None:
            entry.update(key=None, value=None, choices=[])
        elif part_id in ('spawn_gate', 'soft_stop'):
            command = _setup.installed_command(data, component, target)
            entry.update(key=f'modules.{key}', value=cfg['modules'][key], choices=list(GATE_VALUES),
                         installed_mode=None if command is None else
                         'enforce' if _setup.option_value(command, 'mode') == 'enforce' else 'shadow')
        else:
            entry.update(key=f'modules.{key}', value=cfg['modules'][key], choices=list(SWITCH_VALUES))
        modules.append(entry)
    try:
        state = _sync_core.read_state(os.path.join(_setup.log_dir_of(cfg), _sync_core.SYNC_DIR))
    except _sync_core.BrokenState:
        state = _sync_core.State()
    sync = {'installed': 'sync' in parts, 'repo': cfg['sync']['repo'], 'mode': cfg['sync']['mode'],
            'choices': list(SYNC_MODES), 'summary': _setup.TAB_SUMMARY['sync'], 'last_success': _sync_core._text(state.last_success),
            'last_error': state.last_error, 'info': '\n'.join(_setup.INFO['sync']), 'tab': _setup.TAB['sync'],
            'repo_info': '\n'.join(_setup.INFO['repo']), 'repo_tab': _setup.TAB['repo']}
    return answer({'modules': modules, 'sync': sync, 'problems': problems})


def set_key(key: str, value: str, parts: set[str], path: str) -> int:
    name = key.split('.', 1)[1] if '.' in key else ''
    if key == 'sync.mode' and value in SYNC_MODES:
        section, component, stored = 'sync', 'sync', value
    elif key in ('modules.usage_clock', 'modules.context_reporter') and value in SWITCH_VALUES:
        section, component, stored = 'modules', name.replace('_', '-'), value == 'true'
    elif key in ('modules.spawn_gate', 'modules.soft_stop') and value in ('',) + GATE_VALUES:
        section, component, stored = 'modules', name.replace('_', '-'), value
    else:
        raise _setup.UsageError(f'{key} {value!r}: the tab sets modules.usage_clock and modules.context_reporter to '
                                'true|false, modules.spawn_gate and modules.soft_stop to off|shadow|enforce or "", and '
                                'sync.mode to off|shadow|on.')
    if component not in parts:
        raise not_installed(component)
    if key == 'sync.mode' and value != 'off':      # off pushes nothing, wherever the log folder is
        _setup.sync_log_dir(_config.load(log_problems=False, path=path))
    current = _setup.read_config(path).get(section, {})
    changed = name not in current or current[name] != stored
    backup = _setup.write_keys(path, {section: {name: stored}}, True) if changed else None
    return answer({'key': key, 'value': stored, 'changed': changed, 'backup': backup})


def check(repo: str, cfg: dict) -> str:
    """One of RESULTS. The probes run in a temporary folder: nothing of this tool is written."""
    if not _config.valid_sync_repo(repo):
        return 'invalid'
    with tempfile.TemporaryDirectory(prefix='usage-clock-check-') as directory:
        visibility = _sync_git.check_visibility(repo, directory)
        if visibility == _sync_git.PUBLIC:
            return 'public'
        if visibility != _sync_git.PRIVATE:
            return 'unclear'
        access = _sync_git.check_access(repo, cfg['sync']['transport'], directory)
    return 'private_ok' if access == 'reachable' else access


def checked(repo: str, result: str) -> dict:
    return {'result': result, 'repo': repo, 'text': RESULTS[result].format(repo=repo)}


def set_repo(repo: str, cfg: dict, path: str) -> int:
    log_dir = _setup.sync_log_dir(cfg)      # before the check: a relative one would clone below the folder of the tab
    result = check(repo, cfg)
    if result != 'private_ok':
        return answer(checked(repo, result), 1)
    old = cfg['sync']['repo']
    plan = _setup.SyncPlan(path, True, {'repo': repo} if old != repo else {},
                           [f'sync.repo {old!r} -> {repo!r}'] if old != repo else [], cfg['sync']['mode'], 'config.json',
                           repo, cfg['sync']['transport'], cfg['sync']['max_clone_mb'], log_dir)
    try:
        other = os.path.lexists(plan.clone) and not _setup.clone_points_to(plan)
    except _sync_core.SyncError:      # git could not tell: prepare_clone asks again and names the reason
        other = False
    if other:      # before anything is written: prepare_clone would stop there too, with the cache of the probe written
        return answer({'result': 'other_clone', 'repo': repo, 'text': RESULTS['other_clone'].format(clone=plan.clone)}, 1)
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):      # the lines of the installer become the text of the answer
        blocked = _setup.sync_blocked(plan)
        done = _setup.prepare_clone(plan)
        if done and blocked:
            done = _sync_core.clear_visibility(plan.log_dir, time.time())
            print('Sync: the block after a public repo is lifted, the next push is due.' if done else _setup.LOCK_HELD)
        if done:
            _setup.write_sync_config(plan)
    return answer({'result': 'saved' if done else 'failed', 'repo': repo, 'text': printed.getvalue().strip()},
                  0 if done else 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='settings_cli.py', description='The settings of the /report tab.')
    parser.add_argument('--settings', default=DEFAULT_SETTINGS, help=argparse.SUPPRESS)
    parser.add_argument('--target', default=os.path.dirname(os.path.abspath(__file__)), help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('show').add_argument('--json', action='store_true', required=True)
    setter = commands.add_parser('set')
    setter.add_argument('key')
    setter.add_argument('value')
    commands.add_parser('check-repo').add_argument('repo')
    commands.add_parser('set-repo').add_argument('repo')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = os.path.abspath(os.path.expanduser(args.target))
    path = _setup.config_file(target)
    try:
        data, _ = _setup.load_settings(os.path.abspath(os.path.expanduser(args.settings)))
        parts = _setup.installed_parts(data, target)
        if args.command == 'show':
            return show(data, parts, target, path)
        if args.command == 'set':
            return set_key(args.key, args.value, parts, path)
        if 'sync' not in parts:
            raise not_installed('sync')
        cfg = _config.load(log_problems=False, path=path)
        if args.command == 'check-repo':
            result = check(args.repo, cfg)
            return answer(checked(args.repo, result), 0 if result == 'private_ok' else 1)
        return set_repo(args.repo, cfg, path)
    except _setup.UsageError as error:
        return answer({'error': str(error)}, 2)
    except OSError as error:      # a file operation failed (on Windows a replace another program holds): as install.py
        return answer({'error': str(error)}, 1)


if __name__ == '__main__':
    sys.exit(main())
