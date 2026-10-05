#!/usr/bin/env python3
"""Installs claude-code-usage-clock into a Claude Code settings.json, or removes it again.

What it does:
1. Smoke test: every selected hook is started once from a temporary copy, before anything is written.
2. Copies the runtime files from hooks/ to --target and creates config.json from config.example.json
   there, but only if it does not exist (an existing config.json is never touched).
3. Backs up settings.json, then adds the selected components:
     statusline   statusLine = python3 <target>/statusline.py (refreshInterval 60)
     usage-clock  UserPromptSubmit: usage and local time as context for the model      (... || true)
     spawn-gate   PreToolUse Agent|Workflow|Skill: usage thresholds and model rules
                  (enforce: broken -> exit 2, shadow: ... || true)
     soft-stop    PreToolUse *: soft stop for subagents near the usage limit            (... || true)
   Own entries are recognised by the full path of the script in --target, not by its file name. They are updated
   (command and matcher), never duplicated. A hook of someone else that has the same script name is left alone
   and named in the output. Everything else in settings.json stays as it is.

A first installation starts in --mode shadow: nothing is blocked, every decision is only written to the log.
--mode enforce turns the decisions on. A run without --mode keeps the mode of the entries that are already
installed (the output says where the mode comes from); only --mode changes it. --week-ok-until belongs to the
spawn gate command as well; a run without it removes an earlier value and says so.

An existing statusLine that runs something else is left alone unless --replace-statusline is given.
--uninstall removes only the entries that point to scripts in --target, and the statusLine only if it runs
<target>/statusline.py. It deletes no files. --dry-run shows the changes and writes nothing.

Exit codes: 0 ok, 1 smoke test or file operation failed (nothing written to settings.json), 2 bad arguments
or unusable settings.json.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_HOOKS = os.path.join(ROOT, 'hooks')
EXAMPLE_CONFIG = os.path.join(ROOT, 'config.example.json')

TIMEOUT_S = 5
SMOKE_TIMEOUT_S = 15  # generous: the smoke test may run on a loaded machine, the hook timeout stays 5
DEFAULT_SETTINGS = '~/.claude/settings.json'
DEFAULT_TARGET = '~/.claude/hooks/usage-clock'
COMPONENTS = ('statusline', 'usage-clock', 'spawn-gate', 'soft-stop')
REQUIRED_FILES = ('_config.py', '_context_state.py', '_hookio.py', '_spawn_check.py', '_usage.py',
                  'statusline.py', 'usage_clock.py', 'spawn_gate.py', 'soft_stop.py')
SCRIPTS = {'statusline': 'statusline.py', 'usage-clock': 'usage_clock.py',
           'spawn-gate': 'spawn_gate.py', 'soft-stop': 'soft_stop.py'}
NEVER_BLOCK = ' || true'
SPAWN_BROKEN = ' || { echo "spawn gate broken - start denied" >&2; exit 2; }'
GATES = ('spawn-gate', 'soft-stop')  # the components that have a --mode
# component -> (event, matcher). None = no matcher.
HOOKS = {'usage-clock': ('UserPromptSubmit', None),
         'spawn-gate': ('PreToolUse', 'Agent|Workflow|Skill'),
         'soft-stop': ('PreToolUse', '*')}
TOUCHED_EVENTS = ('UserPromptSubmit', 'PreToolUse')
TEMP_SUFFIX = '.tmp-usage-clock'

# One typical input per component for the smoke test
SMOKE_INPUTS = {
    'statusline': {'model': {'display_name': 'smoke-test'}},
    'usage-clock': {'hook_event_name': 'UserPromptSubmit', 'prompt': 'smoke test'},
    'spawn-gate': {'hook_event_name': 'PreToolUse', 'tool_name': 'Agent',
                   'tool_input': {'subagent_type': 'smoke-agent', 'prompt': 'smoke test'}},
    'soft-stop': {'hook_event_name': 'PreToolUse', 'agent_id': 'smoke-test', 'tool_name': 'Read',
                  'tool_input': {'file_path': '/tmp/smoke-test'}},
}


class UsageError(Exception):
    """Bad arguments or an unusable settings.json (exit code 2)."""


# --- Arguments --------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='install.py', allow_abbrev=False, description=(
        'Adds the usage-clock components to a Claude Code settings.json (or removes them).'))
    parser.add_argument('--settings', default=DEFAULT_SETTINGS, help=f'settings.json to edit (default {DEFAULT_SETTINGS})')
    parser.add_argument('--target', default=DEFAULT_TARGET, help=f'directory for the hook files (default {DEFAULT_TARGET})')
    parser.add_argument('--components', default=','.join(COMPONENTS),
                        help='comma-separated: ' + ', '.join(COMPONENTS) + ' (default: all)')
    parser.add_argument('--mode', choices=('shadow', 'enforce'),
                        help='shadow only logs, enforce blocks and warns (default: keep the mode of an existing '
                             'installation, shadow if there is none)')
    parser.add_argument('--week-ok-until', metavar='YYYY-MM-DD',
                        help='spawn gate: approve new starts despite a full weekly limit up to this day')
    parser.add_argument('--replace-statusline', action='store_true',
                        help='replace an existing statusLine that runs something else')
    parser.add_argument('--auto-continue', action='store_true', help='set autoContinueAtUsageLimit to true')
    parser.add_argument('--dry-run', action='store_true', help='show what would change, write nothing')
    parser.add_argument('--uninstall', action='store_true', help='remove the entries again (files stay)')
    return parser


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
    except UsageError as error:
        parser.error(str(error))
    return args


def validate_args(args: argparse.Namespace) -> None:
    names = [name.strip() for name in args.components.split(',')]
    unknown = [name for name in names if name not in COMPONENTS]
    if unknown:
        raise UsageError(f'--components: unknown {", ".join(repr(n) for n in unknown)}; choose from {", ".join(COMPONENTS)}')
    args.components = tuple(dict.fromkeys(names))
    if re.search(r'[\x00-\x1f\x7f]', args.target):
        raise UsageError('--target must not contain control characters or line breaks.')
    if args.week_ok_until is not None:
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.week_ok_until):
                raise ValueError
            date.fromisoformat(args.week_ok_until)
        except ValueError:
            raise UsageError('--week-ok-until must be a date YYYY-MM-DD.') from None
        if 'spawn-gate' not in args.components:
            raise UsageError('--week-ok-until needs the component spawn-gate.')
    if args.replace_statusline and 'statusline' not in args.components:
        raise UsageError('--replace-statusline needs the component statusline.')


# --- Commands ---------------------------------------------------------------------------------------------------

def core_command(target: str, script: str, *extra: str) -> str:
    return ' '.join(['python3', shlex.quote(os.path.join(target, script)), *extra])


def fallback(component: str, mode: str | None) -> str:
    """Only the spawn gate in enforce fails closed. In shadow nothing may block, not even a broken installation."""
    return SPAWN_BROKEN if component == 'spawn-gate' and mode == 'enforce' else NEVER_BLOCK


def hook_command(target: str, component: str, mode: str | None, week_ok_until: str | None,
                 with_fallback: bool = True) -> str:
    extra = []
    if component in GATES:
        extra.append(f'--mode={mode}')
    if component == 'spawn-gate' and week_ok_until:
        extra.append(f'--week-ok-until={week_ok_until}')
    command = core_command(target, SCRIPTS[component], *extra)
    return command + fallback(component, mode) if with_fallback else command


def _tokens(command) -> list[str]:
    try:
        return shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return []


def runs_script(command, script: str) -> bool:
    """Does the command run a file with this name, wherever it lives? (Only to name look-alikes, never to claim one.)"""
    return any(os.path.basename(token) == script for token in _tokens(command))


def points_to(command, path: str) -> bool:
    """Does the command run exactly this file?"""
    wanted = os.path.normpath(path)
    return any(os.path.normpath(os.path.expanduser(token)) == wanted for token in _tokens(command))


# --- settings.json ----------------------------------------------------------------------------------------------

def load_settings(path: str) -> tuple[dict, bool]:
    """(settings, existed). A missing file counts as empty and is created on writing, if its folder exists."""
    if os.path.islink(path):
        raise UsageError(f'--settings must be a regular file, not a symlink: {path}')
    if not os.path.exists(path):
        if not os.path.isdir(os.path.dirname(path)):
            raise UsageError(f'The folder of --settings does not exist: {os.path.dirname(path)}')
        return {}, False
    if not os.path.isfile(path):
        raise UsageError(f'--settings must be a regular file: {path}')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError) as error:
        raise UsageError(f'{path} cannot be read as JSON ({type(error).__name__}); nothing changed.') from None
    if not isinstance(data, dict):
        raise UsageError(f'{path}: the top level must be a JSON object.')
    hooks = data.get('hooks', {})
    if not isinstance(hooks, dict):
        raise UsageError(f'{path}: "hooks" must be an object.')
    for event in TOUCHED_EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list) or not all(
                isinstance(g, dict) and isinstance(g.get('hooks', []), list)
                and all(isinstance(h, dict) for h in g.get('hooks', [])) for g in groups):
            raise UsageError(f'{path}: "hooks.{event}" does not have the expected structure; nothing changed.')
    return data, True


def installed_command(data: dict, component: str, target: str) -> str | None:
    """The command of our installed entry of this component (script in --target), None if there is none."""
    path = os.path.join(target, SCRIPTS[component])
    for group in data.get('hooks', {}).get(HOOKS[component][0], []):
        for entry in group.get('hooks', []):
            if points_to(entry.get('command'), path):
                return entry.get('command')
    return None


def option_value(command: str | None, option: str) -> str | None:
    """The value of --option=... in the command. Given several times, the LAST one counts, as in the hooks
    (_hookio.parse_args)."""
    value = None
    for token in _tokens(command):
        if token.startswith(f'--{option}='):
            value = token.split('=', 1)[1]
    return value


def foreign_notes(groups: list, event: str, script: str, path: str) -> list[str]:
    """Hooks of someone else that run a script with our script name: not ours, so they stay as they are."""
    notes = []
    for group in groups:
        for entry in group.get('hooks', []):
            command = entry.get('command')
            if not runs_script(command, script) or points_to(command, path):
                continue
            folder = next(os.path.dirname(t) for t in _tokens(command) if os.path.basename(t) == script)
            remove = (f'remove it with "install.py --uninstall --target {shlex.quote(folder)}"' if folder
                      else 'remove the entry from settings.json by hand')
            notes.append(f'{event}: another hook runs a {script} from a different path -> {str(command)[:200]!r}. '
                         f'It was left untouched; if it is an old copy of this tool, check the path and {remove}; '
                         'otherwise ignore this.')
    return notes


def upsert_hook(hooks: dict, event: str, matcher: str | None, command: str, script: str, path: str) -> list[str]:
    """Adds or updates OUR entry (the one that runs `path`). Returns the messages."""
    groups = hooks.setdefault(event, [])
    notes = foreign_notes(groups, event, script, path)
    for group in groups:
        for index, entry in enumerate(group.get('hooks', [])):
            if not points_to(entry.get('command'), path):
                continue
            wrong_matcher = matcher is not None and group.get('matcher') != matcher
            if entry.get('command') == command and not wrong_matcher:
                return [f'{event}: {script} already installed - unchanged'] + notes
            note = f'{event}: {script} updated'
            if wrong_matcher:
                note += f' (matcher {group.get("matcher")} -> {matcher})'
                if len(group['hooks']) == 1:
                    group['matcher'] = matcher
                else:  # the group holds other hooks: they keep their matcher, ours moves to a group of its own
                    del group['hooks'][index]
                    groups.append({'matcher': matcher, 'hooks': [{**entry, 'command': command}]})
                    return [f'{note} -> {command}'] + notes
            entry['command'] = command
            return [f'{note} -> {command}'] + notes
    entry = {'type': 'command', 'command': command, 'timeout': TIMEOUT_S}
    groups.append({'matcher': matcher, 'hooks': [entry]} if matcher is not None else {'hooks': [entry]})
    return [f'{event}: {script} added' + (f' (matcher {matcher})' if matcher is not None else '') + f' -> {command}'] + notes


def plan_statusline(data: dict, target: str, replace: bool) -> list[str]:
    command = core_command(target, SCRIPTS['statusline'])
    fresh = {'type': 'command', 'command': command, 'padding': 0, 'refreshInterval': 60}
    current = data.get('statusLine')
    if current is None:
        data['statusLine'] = fresh
        return [f'statusLine: set -> {command}']
    ours = isinstance(current, dict) and points_to(current.get('command'), os.path.join(target, SCRIPTS['statusline']))
    if ours:
        notes = []
        if current.get('command') != command:
            current['command'] = command
            notes.append(f'command updated -> {command}')
        if 'refreshInterval' not in current:
            current['refreshInterval'] = 60
            notes.append('refreshInterval = 60 added')
        return [f'statusLine: {"; ".join(notes)}' if notes else 'statusLine: already installed - unchanged']
    if not replace:
        return ['statusLine: an existing status line runs something else and was LEFT UNCHANGED. Without ours the '
                'shared usage file is not written, so the hooks see the usage as unknown. Run again with '
                '--replace-statusline to use ours.']
    if isinstance(current, dict) and 'refreshInterval' in current:
        fresh['refreshInterval'] = current['refreshInterval']
    data['statusLine'] = fresh
    return [f'statusLine: replaced -> {command}']


def resolve_modes(data: dict, args: argparse.Namespace, target: str) -> dict[str, tuple[str, str]]:
    """component -> (mode, where it comes from), for the selected gates. Call before plan_install changes data."""
    modes = {}
    for component in (c for c in args.components if c in GATES):
        if args.mode:
            modes[component] = (args.mode, '--mode')
            continue
        command = installed_command(data, component, target)
        if command is None:
            modes[component] = ('shadow', 'default, nothing installed yet')
            continue
        # The mode the installed hook really runs in: only exactly 'enforce' is enforce (_hookio.parse_args)
        kept = option_value(command, 'mode')
        if kept in ('shadow', 'enforce'):
            modes[component] = (kept, 'kept from the existing installation')
        else:
            shown = 'no --mode=' if kept is None else '--mode=' + re.sub(r'[^0-9A-Za-z-]', '?', kept)[:20]
            modes[component] = ('shadow', f'kept from the existing installation, whose hook runs as shadow with {shown}')
    return modes


def describe_modes(modes: dict[str, tuple[str, str]]) -> str:
    if len(set(modes.values())) == 1:
        mode, source = next(iter(modes.values()))
        return f'{mode} ({source})'
    return '; '.join(f'{component} {mode} ({source})' for component, (mode, source) in modes.items())


def plan_install(data: dict, args: argparse.Namespace, target: str, modes: dict[str, tuple[str, str]]) -> list[str]:
    messages = []
    hook_components = [c for c in args.components if c in HOOKS]
    hooks = data.setdefault('hooks', {}) if hook_components else None
    for component in hook_components:
        event, matcher = HOOKS[component]
        mode = modes[component][0] if component in modes else None
        command = hook_command(target, component, mode, args.week_ok_until)
        earlier = option_value(installed_command(data, component, target), 'week-ok-until')
        messages += upsert_hook(hooks, event, matcher, command, SCRIPTS[component], os.path.join(target, SCRIPTS[component]))
        if component == 'spawn-gate' and earlier and not args.week_ok_until:
            shown = re.sub(r'[^0-9A-Za-z-]', '?', earlier)[:20]
            messages.append(f'{SCRIPTS[component]}: the weekly approval --week-ok-until={shown} is removed, this run '
                            'has no --week-ok-until. Pass the option again to keep it.')
    if 'statusline' in args.components:
        messages += plan_statusline(data, target, args.replace_statusline)
    if args.auto_continue:
        if data.get('autoContinueAtUsageLimit') is True:
            messages.append('autoContinueAtUsageLimit: already true - unchanged')
        else:
            data['autoContinueAtUsageLimit'] = True
            messages.append('autoContinueAtUsageLimit = true set')
    return messages


def plan_uninstall(data: dict, args: argparse.Namespace, target: str) -> list[str]:
    messages = []
    hooks = data.get('hooks', {})
    removed_any = False
    for component in (c for c in args.components if c in HOOKS):
        path = os.path.join(target, SCRIPTS[component])
        event = HOOKS[component][0]
        groups = hooks.get(event, [])
        removed = 0
        for group in list(groups):
            entries = group.get('hooks', [])
            kept = [entry for entry in entries if not points_to(entry.get('command'), path)]
            if len(kept) == len(entries):
                continue
            removed += len(entries) - len(kept)
            if kept:
                group['hooks'] = kept
            else:
                groups.remove(group)
        if removed and not groups:
            del hooks[event]
        removed_any = removed_any or bool(removed)
        messages.append(f'{event}: {SCRIPTS[component]} removed' if removed
                        else f'{event}: {SCRIPTS[component]} not found - unchanged')
    if removed_any and not hooks:
        del data['hooks']
    if 'statusline' in args.components and 'statusLine' in data:
        current = data['statusLine']
        if isinstance(current, dict) and points_to(current.get('command'), os.path.join(target, SCRIPTS['statusline'])):
            del data['statusLine']
            messages.append('statusLine: removed')
        else:
            messages.append('statusLine: does not run our script - left unchanged')
    return messages


# --- Smoke test -------------------------------------------------------------------------------------------------

def missing_sources() -> list[str]:
    missing = [name for name in REQUIRED_FILES if not os.path.isfile(os.path.join(SOURCE_HOOKS, name))]
    return missing + ([] if os.path.isfile(EXAMPLE_CONFIG) else ['config.example.json'])


def runtime_files() -> list[str]:
    return sorted(name for name in os.listdir(SOURCE_HOOKS) if name.endswith('.py'))


def _log_records(log_dir: str, hook: str) -> list[dict]:
    try:
        with open(os.path.join(log_dir, 'log.jsonl'), encoding='utf-8') as f:
            records = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []
    return [r for r in records if isinstance(r, dict) and r.get('hook') == hook]


def _check_evidence(component: str, stdout: str, log_dir: str) -> str | None:
    """Exit 0 alone proves little: the soft stop and the usage part swallow a broken import on purpose, and the
    spawn gate logs even when it fails internally. Each hook must leave a sane trace: status line text,
    context with both parts, or a log line that is not an internal failure."""
    if component == 'statusline':
        return None if 'smoke-test' in stdout else 'no status line output'
    if component == 'usage-clock':
        try:
            context = json.loads(stdout)['hookSpecificOutput']['additionalContext']
        except (ValueError, KeyError, TypeError):
            return f'output is not the expected JSON: {stdout[:100]!r}'
        if 'Usage:' not in context:
            return 'no usage part in the context (a module failed to load?)'
        return None if 'Now:' in context else 'no time part in the context'
    records = _log_records(log_dir, SCRIPTS[component][:-len('.py')])
    if not records:
        return 'the hook ran but wrote no log line (a module failed to load?)'
    for record in records:
        reason = str(record.get('reason', ''))
        if record.get('decision') == 'error' or 'internally' in reason:
            return f'failed internally ({reason[:100]})'
    return None


def smoke_test(args: argparse.Namespace, modes: dict[str, tuple[str, str]]) -> list[str]:
    """Starts each selected hook once from a temporary copy of the files that would be installed."""
    errors = []
    with tempfile.TemporaryDirectory(prefix='usage-clock-smoke-') as tmp:
        copy_dir = os.path.join(tmp, 'hooks')
        os.mkdir(copy_dir)
        for name in runtime_files():
            shutil.copy2(os.path.join(SOURCE_HOOKS, name), os.path.join(copy_dir, name))
        os.mkdir(os.path.join(tmp, 'agents'))
        with open(os.path.join(tmp, 'agents', 'smoke-agent.md'), 'w', encoding='utf-8') as f:
            f.write('---\nname: smoke-agent\nmodel: haiku\neffort: low\n---\nSmoke test agent.\n')
        env = dict(os.environ, HOME=os.path.join(tmp, 'home'), CCUC_USAGE_FILE=os.path.join(tmp, 'usage.json'),
                   CCUC_LOG_DIR=os.path.join(tmp, 'log'), CCUC_AGENT_DIRS=os.path.join(tmp, 'agents'),
                   CCUC_CONFIG=os.path.join(tmp, 'no-config.json'))
        for component in args.components:
            mode = modes[component][0] if component in modes else None
            command = hook_command(copy_dir, component, mode, args.week_ok_until, with_fallback=False)
            script = SCRIPTS[component]
            try:
                done = subprocess.run(['/bin/sh', '-c', command], input=json.dumps(dict(SMOKE_INPUTS[component], cwd=tmp)),
                                      capture_output=True, text=True, timeout=SMOKE_TIMEOUT_S, env=env, cwd=tmp)
            except subprocess.TimeoutExpired:
                errors.append(f'{script}: no answer within {SMOKE_TIMEOUT_S} s')
                continue
            if done.returncode != 0:
                errors.append(f'{script}: exit {done.returncode} - {done.stderr.strip()[:200]}')
                continue
            stdout = done.stdout.strip()
            if component != 'statusline' and stdout:
                try:
                    json.loads(stdout)
                except ValueError:
                    errors.append(f'{script}: output is not valid JSON - {stdout[:200]}')
                    continue
            problem = _check_evidence(component, stdout, env['CCUC_LOG_DIR'])
            if problem:
                errors.append(f'{script}: {problem}')
    return errors


# --- Writing ----------------------------------------------------------------------------------------------------

NO_FOLLOW = getattr(os, 'O_NOFOLLOW', 0)


def copy_exclusively(source: str, destination: str) -> None:
    """Copies source to destination through a fresh temp file, then os.replace (atomic: a hook that starts right now
    never reads a half-written file). The temp file is created with O_EXCL and O_NOFOLLOW, so nothing is ever written
    through a link that someone left or planted there. Its permissions are those of the source."""
    temp = destination + TEMP_SUFFIX
    try:
        os.unlink(temp)  # a leftover of an aborted run, possibly a symlink: remove it, never write through it
    except FileNotFoundError:
        pass
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NO_FOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as copy_file, open(source, 'rb') as origin:
            shutil.copyfileobj(origin, copy_file)
            os.fchmod(copy_file.fileno(), stat.S_IMODE(os.fstat(origin.fileno()).st_mode))
        os.replace(temp, destination)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


def copy_runtime_files(target: str) -> int:
    os.makedirs(target, exist_ok=True)
    names = runtime_files()
    for name in names:
        copy_exclusively(os.path.join(SOURCE_HOOKS, name), os.path.join(target, name))
    return len(names)


def create_config(target: str) -> bool:
    """config.json from config.example.json, only if there is none. Never overwrites, never writes through a link."""
    destination = os.path.join(target, 'config.json')
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NO_FOLLOW, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, 'wb') as config, open(EXAMPLE_CONFIG, 'rb') as origin:
            shutil.copyfileobj(origin, config)
            os.fchmod(config.fileno(), stat.S_IMODE(os.fstat(origin.fileno()).st_mode))
    except BaseException:
        os.unlink(destination)  # our own half-written file: a config.json that is never overwritten must not be broken
        raise
    return True


def config_exists(target: str) -> bool:
    return os.path.lexists(os.path.join(target, 'config.json'))


def make_backup(path: str) -> str:
    """Copies path exclusively to <path>.bak-usage-clock-<stamp with microseconds>[-n]. Never overwrites a backup."""
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    for n in range(1000):
        backup = f'{path}.bak-usage-clock-{stamp}' + (f'-{n}' if n else '')
        try:
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with open(path, 'rb') as source, os.fdopen(descriptor, 'wb') as copy_file:
            shutil.copyfileobj(source, copy_file)
        shutil.copystat(path, backup)
        return backup
    raise RuntimeError(f'No free backup name for {path}')


def write_settings(path: str, data: dict, existed: bool) -> str | None:
    """Backup (if the file exists), then temp file + os.replace. Keeps the permissions. Returns the backup path."""
    backup = make_backup(path) if existed else None
    temp = path + TEMP_SUFFIX
    try:
        os.unlink(temp)  # a leftover of an aborted run, possibly a symlink: never write through it
    except FileNotFoundError:
        pass
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write('\n')
    with open(temp, encoding='utf-8') as f:
        json.load(f)  # cross-check: valid JSON
    if existed:
        shutil.copymode(path, temp)
    os.replace(temp, path)
    return backup


# --- Main -------------------------------------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    settings_path = os.path.abspath(os.path.expanduser(args.settings))
    target = os.path.abspath(os.path.expanduser(args.target))
    data, existed = load_settings(settings_path)
    before = copy.deepcopy(data)
    print(f'Settings: {settings_path}' + ('' if existed else ' (does not exist yet)'))
    print(f'Target:   {target}')
    if args.uninstall:
        messages = plan_uninstall(data, args, target)
    else:
        missing = missing_sources()
        if missing:
            print(f'Error: files missing in the source folder {SOURCE_HOOKS}: {", ".join(missing)}')
            return 1
        modes = resolve_modes(data, args, target)
        if modes:
            print(f'Mode:     {describe_modes(modes)}')
        messages = plan_install(data, args, target, modes)
    print('\n'.join(messages))
    if not args.uninstall:
        errors = smoke_test(args, modes)
        if errors:
            print('SMOKE TEST FAILED - nothing written:')
            for error in errors:
                print('  ', error)
            return 1
        print(f'Smoke test: passed ({", ".join(args.components)}).')
    changed = data != before
    if args.dry_run:
        if not args.uninstall:
            note = 'would be created from config.example.json' if not config_exists(target) else 'exists, stays untouched'
            print(f'Files: {len(runtime_files())} runtime files would be copied to {target}; config.json {note}.')
        print('DRY RUN - nothing written.' + ('' if changed else ' settings.json would stay as it is.'))
        return 0
    if not args.uninstall:
        count = copy_runtime_files(target)
        note = 'created from config.example.json' if create_config(target) else 'kept as it is'
        print(f'Files: {count} runtime files copied to {target}; config.json {note}.')
    if not changed:
        print('settings.json is already up to date - not written.')
        return 0
    backup = write_settings(settings_path, data, existed)
    print(f'Written: {settings_path}')
    if backup:
        print(f'Backup:  {backup}')
        print(f'Undo:    cp {shlex.quote(backup)} {shlex.quote(settings_path)}')
    if not args.uninstall:
        print('Start a new Claude Code session for the changes to take effect.')
        if any(mode == 'shadow' for mode, _ in modes.values()):
            print('Mode shadow: nothing is blocked, decisions are only logged. Use --mode enforce to switch them on.')
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except UsageError as error:
        print(f'Error: {error}')
        return 2
    except OSError as error:
        print(f'Error: {error}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
