#!/usr/bin/env python3
"""The one way the Settings tab of /report (desktop/hooks/pane.tsx) changes the configuration: switch a part on or off
and set the mode of the gates and of the sync in config.json, set each brake of the spawn gate and its limit and the
levels of the soft stop, and check and set the sync repository. It never writes
settings.json and adds or removes no hook: the parts read their switch from config.json on every run.

  show --json                  per part: installed (as install.py --check sees it in settings.json), its switch, the
                               texts of the assistant's `i` and a sentence for the tab; the sync: repo, mode, last
                               success, last error
  set modules.<name> <value>   usage_clock and context_reporter: true|false; spawn_gate and soft_stop: off|shadow|
                               enforce, or "" for the mode of the hook entry; desktop_line: off|short|full
  set sync.mode off|shadow|on
  set thresholds.<brake>_action, spawn_rules.model_effort_action
                               a brake of the spawn gate: one of its choices in _config._CHOICES
  set thresholds.<limit> N     start_block_5h, start_block_week: 0 to 100; unknown_usage_max_agents: 0 to 100, whole;
                               warn, checkpoint_only, deny of the soft stop: 0 to 100, and warn < checkpoint_only <
                               deny with the other two as config.json has them
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
import re
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
         ('desktop_line', 'desktop-line', 'desktop_line', 'Desktop line'),
         ('usage_clock', 'usage-clock', 'usage_clock', 'Usage clock'),
         ('context_reporter', 'context-reporter', 'context_reporter', 'Context reporter'),
         ('spawn_gate', 'spawn-gate', 'spawn_gate', 'Spawn gate'),
         ('soft_stop', 'soft-stop', 'soft_stop', 'Soft stop'))
SWITCH_VALUES = ('true', 'false')
GATE_VALUES = ('off', 'shadow', 'enforce')
LINE_VALUES = ('off', 'short', 'full')
# The part whose switch needs another component: the plugin draws the desktop line and finds this script next to the
# status line
INSTALLED_WITH = {'desktop-line': 'statusline'}
SYNC_MODES = ('off', 'shadow', 'on')
# The brakes of the spawn gate, shown inside its part: (id, label, key of the action, its limit and unit or None, what it
# brakes). Their choices are those _config.py keeps (BRAKE_CHOICES)
BRAKES = (('five_hour', '5h window', 'thresholds.five_hour_action', ('thresholds.start_block_5h', '%'),
           'At or above this share of the five-hour window, new agents and workflows are braked.'),
          ('week', 'Week', 'thresholds.week_action', ('thresholds.start_block_week', '%'),
           'At or above this share of the weekly limit, new agents and workflows are braked; --week-ok-until in '
           'settings.json still approves single days.'),
          ('unknown_usage', 'Usage unknown', 'thresholds.unknown_usage_action', ('thresholds.unknown_usage_max_agents',
                                                                                 'agents'),
           'Without a fresh measurement, a block of more agents than this is braked; a smaller one starts with a '
           'warning.'),
          ('model_effort', 'Model and effort', 'spawn_rules.model_effort_action', None,
           'An agent without model and effort in its definition is braked.'))
BRAKE_CHOICES = {key: _config._CHOICES[tuple(key.split('.'))] for _, _, key, _, _ in BRAKES}
# The levels of the soft stop, shown inside its part, lowest first: (key, label, what happens from there on)
LEVELS = (('thresholds.warn', 'Warn', 'Agents are told to save a checkpoint soon.'),
          ('thresholds.checkpoint_only', 'Checkpoint only', 'Agents may only write checkpoints.'),
          ('thresholds.deny', 'Stop', 'Agents are told to finish with a partial result.'))
PERCENT_KEYS = ('thresholds.start_block_5h', 'thresholds.start_block_week') + tuple(key for key, _, _ in LEVELS)
AGENTS_KEY = 'thresholds.unknown_usage_max_agents'
MAX_AGENTS = 100
NUMBER = re.compile(r'[0-9]{1,3}(?:\.[0-9]{1,2})?')      # ASCII digits only: no sign, no exponent, no nan
CONTROL = re.compile('[\x00-\x09\x0b-\x1f\x7f-\x9f]')
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


def printable(value):
    """Every text of the answer without control characters (C0 but the line break, DEL, C1): a key of config.json or a
    typed name could carry an escape sequence the tab would draw."""
    if isinstance(value, str):
        return CONTROL.sub('?', value)
    if isinstance(value, list):
        return [printable(item) for item in value]
    if isinstance(value, dict):
        return {key: printable(item) for key, item in value.items()}
    return value


def answer(value: dict, code: int = 0) -> int:
    print(json.dumps(printable(value), ensure_ascii=False))
    return code


def not_installed(component: str) -> _setup.UsageError:
    return _setup.UsageError(f'{component} is not installed. Run tools/install.py (with --components {component}) to '
                             'install it first.')


def show(data: dict, parts: set[str], target: str, path: str) -> int:
    problems: list[str] = []
    cfg = _config.load(log_problems=False, path=path, problems=problems)
    modules = []
    for part_id, component, key, label in PARTS:
        entry = {'id': part_id, 'label': label, 'installed': INSTALLED_WITH.get(component, component) in parts, 'summary': _setup.TAB_SUMMARY[component],
                 'info': '\n'.join(_setup.INFO[component]), 'tab': _setup.TAB[component]}
        if key is None:
            entry.update(key=None, value=None, choices=[])
        elif part_id == 'desktop_line':
            entry.update(key=f'modules.{key}', value=cfg['modules'][key], choices=list(LINE_VALUES))
        elif part_id in ('spawn_gate', 'soft_stop'):
            command = _setup.installed_command(data, component, target)
            entry.update(key=f'modules.{key}', value=cfg['modules'][key], choices=list(GATE_VALUES),
                         installed_mode=None if command is None else
                         'enforce' if _setup.option_value(command, 'mode') == 'enforce' else 'shadow')
        else:
            entry.update(key=f'modules.{key}', value=cfg['modules'][key], choices=list(SWITCH_VALUES))
        if part_id == 'spawn_gate':
            entry['brakes'] = [{'id': brake_id, 'label': brake_label, 'key': key, 'value': value_of(cfg, key),
                                'choices': list(BRAKE_CHOICES[key]), 'summary': summary,
                                'limit': None if limit is None else {'key': limit[0], 'value': value_of(cfg, limit[0]),
                                                                     'unit': limit[1]}}
                               for brake_id, brake_label, key, limit, summary in BRAKES]
        elif part_id == 'soft_stop':
            entry['levels'] = [{'key': key, 'label': level_label, 'value': value_of(cfg, key), 'unit': '%',
                                'summary': summary} for key, level_label, summary in LEVELS]
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


def value_of(cfg: dict, key: str):
    section, name = key.split('.')
    return cfg[section][name]


def number(key: str, value: str, path: str) -> int | float:
    """The number `value` for `key` as config.json stores it (a whole one as int), or a UsageError."""
    if not NUMBER.fullmatch(value):
        raise _setup.UsageError(f'{key} {value!r}: a number such as 75 or 62.5 (digits, at most two decimals).')
    stored = float(value)
    stored = int(stored) if stored.is_integer() else stored
    if key == AGENTS_KEY:
        if not isinstance(stored, int) or stored > MAX_AGENTS:
            raise _setup.UsageError(f'{key} {value!r}: a whole number of agents from 0 to {MAX_AGENTS}.')
        return stored
    if stored > 100:
        raise _setup.UsageError(f'{key} {value!r}: a percentage from 0 to 100.')
    if key in (level for level, _, _ in LEVELS):
        cfg = _config.load(log_problems=False, path=path)
        levels = [stored if level == key else value_of(cfg, level) for level, _, _ in LEVELS]
        if not levels[0] < levels[1] < levels[2]:
            raise _setup.UsageError(f'{key} {value!r}: the soft stop needs warn < checkpoint_only < deny; with this '
                                    f'value they would be {" / ".join(f"{level:g}" for level in levels)}.')
    return stored


def set_key(key: str, value: str, parts: set[str], path: str) -> int:
    name = key.split('.', 1)[1] if '.' in key else ''
    if key in BRAKE_CHOICES and value in BRAKE_CHOICES[key]:
        section, component, stored = key.split('.')[0], 'spawn-gate', value
    elif key in PERCENT_KEYS + (AGENTS_KEY,):
        section, stored = 'thresholds', None      # the number is checked once the gate is known to be installed
        component = 'spawn-gate' if key in ('thresholds.start_block_5h', 'thresholds.start_block_week', AGENTS_KEY) \
            else 'soft-stop'
    elif key == 'sync.mode' and value in SYNC_MODES:
        section, component, stored = 'sync', 'sync', value
    elif key in ('modules.usage_clock', 'modules.context_reporter') and value in SWITCH_VALUES:
        section, component, stored = 'modules', name.replace('_', '-'), value == 'true'
    elif key in ('modules.spawn_gate', 'modules.soft_stop') and value in ('',) + GATE_VALUES:
        section, component, stored = 'modules', name.replace('_', '-'), value
    elif key == 'modules.desktop_line' and value in LINE_VALUES:
        section, component, stored = 'modules', INSTALLED_WITH['desktop-line'], value
    else:
        raise _setup.UsageError(f'{key} {value!r}: the tab sets modules.usage_clock and modules.context_reporter to '
                                'true|false, modules.spawn_gate and modules.soft_stop to off|shadow|enforce or "", '
                                'modules.desktop_line to off|short|full, sync.mode to off|shadow|on, a brake of the '
                                'spawn gate to one of its choices, and the limits and levels to a number.')
    if component not in parts:
        raise not_installed(component)
    if stored is None:
        stored = number(key, value, path)
    if key == 'sync.mode' and value != 'off':      # off pushes nothing, wherever the log folder is
        _setup.sync_log_dir(_config.load(log_problems=False, path=path))
    current = _setup.read_config(path).get(section, {})
    if not isinstance(current, dict):      # read_config checks only the sections the installer writes
        raise _setup.UsageError(f'{path}: "{section}" must be a JSON object, so its keys cannot be set; nothing changed.')
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
