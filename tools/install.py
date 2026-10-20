#!/usr/bin/env python3
"""Installs claude-code-usage-clock into a Claude Code settings.json, or removes it again.

What it does:
1. Smoke test: every selected hook is started once from a temporary copy, before anything is written.
2. Copies the runtime files from hooks/ to --target and creates config.json from config.example.json
   there, but only if it does not exist (an existing config.json is never touched). The usage report
   (tools/usage_report.py with log_summary.py and prices.json; hooks/_transcripts.py, which reads the transcripts,
   comes with the hooks) goes the same way, so that it runs from --target without this checkout.
3. Backs up settings.json, then adds the selected components:
     statusline   statusLine = python3 <target>/statusline.py (refreshInterval 60)
   (On Windows the command is `"<this python>" -X utf8 "<target>/statusline.py"`, in forward slashes, for Git
   Bash: Claude Code runs hook commands there. The installer needs Git Bash for its smoke test and stops without it.)
     usage-clock  UserPromptSubmit: usage and local time as context for the model      (... || true)
     context-reporter  UserPromptSubmit and PostToolUse *: the fill level of the context window
                  as context for the model, needs the status line (two entries, same command) (... || true)
     spawn-gate   PreToolUse Agent|Workflow|Skill: usage thresholds and model rules
                  (enforce: broken -> exit 2, shadow: ... || true)
     soft-stop    PreToolUse *: soft stop for subagents near the usage limit            (... || true)
     sync         SessionStart and SessionEnd: sync.py session-start / session-end, the sync across machines
                  (... || true). Opt-in: not in the default selection, see below.
   The wake-up watcher (wake_watcher.py, _wake.py) needs no settings entry and is no component: it is copied
   with the other runtime files and started by the context reporter's hint once `wake_watcher.from_k` is set.
   Own entries are recognised by the full path of the script in --target, not by its file name. They are updated
   (command and matcher), never duplicated. A hook of someone else that has the same script name is left alone
   and named in the output. Everything else in settings.json stays as it is.

A first installation starts in --mode shadow: nothing is blocked, every decision is only written to the log.
--mode enforce turns the decisions on. A run without --mode keeps the mode of the entries that are already
installed (the output says where the mode comes from); only --mode changes it. --week-ok-until belongs to the
spawn gate command as well; a run without it removes an earlier value and says so.

The sync (0034-SYNC-INSTALL) is installed only when --components names it. Its mode and repository live in config.json
(sync.mode, sync.repo), so these two keys are the only ones the installer ever writes there, with a backup, and only
when they change. --sync-repo owner/name sets sync.repo and, under the sync lock and before anything is written,
checks once that the repository cannot be read without logging in (public or unclear: exit 1, nothing installed) and
clones it into <log_dir>/sync/repo (no checkout before the size check of the push, a clone of another repository there
is never touched). Run again, it lifts the lasting block after `visibility`. --sync-mode off|shadow|on sets sync.mode;
without it the mode of an installed sync is kept and a first installation starts in shadow (the output line
`Sync mode:` says which and where it comes from). Without --components, --uninstall removes the sync entries too and
sets sync.mode to off (the status line pushes from the mode alone; the clone and the data in the remote stay), and
--check checks the sync if its entries are installed; a sync.mode that is not off without them is a FIX.

An existing statusLine that runs something else is left alone unless --replace-statusline is given.
--uninstall removes only the entries that point to scripts in --target, and the statusLine only if it runs
<target>/statusline.py. It deletes no files. --dry-run shows the changes and writes nothing.

--check checks an existing installation and changes nothing: the entries in settings.json (matcher, mode), the
files in --target (hooks and report) against this checkout, config.json as the hooks read it, Git Bash on Windows, the smoke test, the
age of the usage numbers and the log folder, and for the sync its repository, clone, remote, visibility, last success,
backoff and size. Each line is OK, FIX (something to repair) or NOTE (worth knowing).

Exit codes: 0 ok, 1 smoke test or file operation failed or, on Windows, no Git Bash, or the sync repository is public,
unclear or cannot be cloned (nothing written to settings.json), or --check found something to fix, 2 bad arguments or
an unusable settings.json or config.json.
"""
from __future__ import annotations

import argparse
import copy
import filecmp
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from typing import NamedTuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_HOOKS = os.path.join(ROOT, 'hooks')
SOURCE_TOOLS = os.path.join(ROOT, 'tools')
WINDOWS_MODULE = os.path.join(ROOT, 'windows', 'cli', '_win.py')
sys.path.append(SOURCE_HOOKS)
import _plat  # noqa: E402  the platform switch of the hooks: commands, file operations, the hook shell
import _config  # noqa: E402  --check reads config.json and the usage file as the hooks do
import _usage  # noqa: E402
import _sync_core  # noqa: E402  the sync: lock and state; the clone, visibility probe and size check of the push
import _sync_git  # noqa: E402
EXAMPLE_CONFIG = os.path.join(ROOT, 'config.example.json')

TIMEOUT_S = 5
SMOKE_TIMEOUT_S = 15  # generous: the smoke test may run on a loaded machine, the hook timeout stays 5
DEFAULT_SETTINGS = '~/.claude/settings.json'
DEFAULT_TARGET = '~/.claude/hooks/usage-clock'
COMPONENTS = ('statusline', 'usage-clock', 'context-reporter', 'spawn-gate', 'soft-stop', 'sync')
DEFAULT_COMPONENTS = COMPONENTS[:-1]        # the sync sends data off this machine: only when asked for
SYNC_MODES = ('off', 'shadow', 'on')
SYNC_COMMANDS = {'SessionStart': 'session-start', 'SessionEnd': 'session-end'}     # the argument of sync.py per event
CLONE_TIMEOUT_S = 300           # the clone of --sync-repo, under the sync lock (no push runs meanwhile)
CHECK_GIT_TIMEOUT_S = 15        # each git call of --check in the clone
CLONE_NOTE_MB = 500             # --check notes a clone from this size on (sync.max_clone_mb stops a pull at 1024)
MEGABYTE = 1024 * 1024
REQUIRED_FILES = ('_config.py', '_context_state.py', '_history.py', '_hookio.py', '_plat.py', '_spawn_check.py',
                  '_sync_core.py', '_sync_export.py', '_sync_git.py', '_transcripts.py', '_usage.py', '_wake.py',
                  'statusline.py', 'usage_clock.py', 'context_reporter.py', 'spawn_gate.py', 'soft_stop.py', 'sync.py',
                  'sync_format.py', 'wake_watcher.py')
# The usage report from tools/ and everything it loads from beside itself (its other modules come from hooks/).
REPORT_FILES = ('usage_report.py', 'log_summary.py', 'prices.json')
# The modules of hooks/ the report needs besides its own files (the sync export reads the transcripts with it too):
# copied with the hooks, named with the report.
REPORT_MODULES = ('_transcripts.py',)
SCRIPTS = {'statusline': 'statusline.py', 'usage-clock': 'usage_clock.py', 'context-reporter': 'context_reporter.py',
           'spawn-gate': 'spawn_gate.py', 'soft-stop': 'soft_stop.py', 'sync': 'sync.py'}
NEVER_BLOCK = ' || true'
SPAWN_BROKEN = ' || { echo "spawn gate broken - start denied" >&2; exit 2; }'
GATES = ('spawn-gate', 'soft-stop')  # the components that have a --mode
# component -> [(event, matcher), ...]. None = no matcher. One command per component (the sync: one per event, see
# SYNC_COMMANDS), one entry per event.
HOOKS = {'usage-clock': [('UserPromptSubmit', None)],
         'context-reporter': [('UserPromptSubmit', None), ('PostToolUse', '*')],
         'spawn-gate': [('PreToolUse', 'Agent|Workflow|Skill')],
         'soft-stop': [('PreToolUse', '*')],
         'sync': [('SessionStart', None), ('SessionEnd', None)]}
TOUCHED_EVENTS = tuple(dict.fromkeys(event for entries in HOOKS.values() for event, _ in entries))
TEMP_SUFFIX = '.tmp-usage-clock'

# One typical input per component for the smoke test. The context reporter needs a state file of its session.
SMOKE_SESSION = 'smoke-test'
SMOKE_CONTEXT = 'Context: 123k/1000k tokens (12%). Measured, not estimated.'
SMOKE_INPUTS = {
    'statusline': {'model': {'display_name': 'smoke-test'}},
    'usage-clock': {'hook_event_name': 'UserPromptSubmit', 'prompt': 'smoke test'},
    'context-reporter': {'hook_event_name': 'UserPromptSubmit', 'session_id': SMOKE_SESSION, 'prompt': 'smoke test'},
    'spawn-gate': {'hook_event_name': 'PreToolUse', 'tool_name': 'Agent',
                   'tool_input': {'subagent_type': 'smoke-agent', 'prompt': 'smoke test'}},
    'soft-stop': {'hook_event_name': 'PreToolUse', 'agent_id': 'smoke-test', 'tool_name': 'Read',
                  'tool_input': {'file_path': '/tmp/smoke-test'}},
    'sync': {'hook_event_name': 'SessionStart', 'session_id': SMOKE_SESSION, 'source': 'startup'},
}


class UsageError(Exception):
    """Bad arguments or an unusable settings.json (exit code 2)."""


# --- Arguments --------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='install.py', allow_abbrev=False, description=(
        'Adds the usage-clock components to a Claude Code settings.json (or removes them).'))
    parser.add_argument('--settings', default=DEFAULT_SETTINGS, help=f'settings.json to edit (default {DEFAULT_SETTINGS})')
    parser.add_argument('--target', default=DEFAULT_TARGET, help=f'directory for the hook files (default {DEFAULT_TARGET})')
    parser.add_argument('--components',
                        help='comma-separated: ' + ', '.join(COMPONENTS) + ' (default: all but sync, which is opt-in; '
                             '--uninstall: all)')
    parser.add_argument('--mode', choices=('shadow', 'enforce'),
                        help='shadow only logs, enforce blocks and warns (default: keep the mode of an existing '
                             'installation, shadow if there is none)')
    parser.add_argument('--week-ok-until', metavar='YYYY-MM-DD',
                        help='spawn gate: approve new starts despite a full weekly limit up to this day')
    parser.add_argument('--replace-statusline', action='store_true',
                        help='replace an existing statusLine that runs something else')
    parser.add_argument('--auto-continue', action='store_true', help='set autoContinueAtUsageLimit to true')
    parser.add_argument('--sync-repo', metavar='OWNER/NAME',
                        help='sync: the private GitHub repository to sync into; checks that it is not public and '
                             'clones it')
    parser.add_argument('--sync-mode', choices=SYNC_MODES,
                        help='sync: off, shadow (commits here, pushes nothing) or on (default: keep the mode of an '
                             'installed sync, shadow if there is none)')
    parser.add_argument('--dry-run', action='store_true', help='show what would change, write nothing')
    parser.add_argument('--uninstall', action='store_true', help='remove the entries again (files stay)')
    parser.add_argument('--check', action='store_true',
                        help='check an existing installation and change nothing (exit 1 if something needs fixing)')
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
    args.components_given = args.components is not None      # --check adds an installed sync to the default
    if args.components is None:
        args.components = ','.join(COMPONENTS if args.uninstall else DEFAULT_COMPONENTS)
    names = [name.strip() for name in args.components.split(',')]
    unknown = [name for name in names if name not in COMPONENTS]
    if unknown:
        raise UsageError(f'--components: unknown {", ".join(repr(n) for n in unknown)}; choose from {", ".join(COMPONENTS)}')
    args.components = tuple(dict.fromkeys(names))
    if args.sync_repo is not None and not _config.valid_sync_repo(args.sync_repo):
        raise UsageError('--sync-repo must be owner/name as on GitHub, for example anna/usage-sync.')
    if (args.sync_repo is not None or args.sync_mode) and 'sync' not in args.components:
        raise UsageError('--sync-repo and --sync-mode need the component sync (for example --components sync).')
    if args.uninstall and (args.sync_repo is not None or args.sync_mode):
        raise UsageError('--uninstall takes no --sync-repo or --sync-mode: it switches the sync off.')
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
    if args.check and (args.dry_run or args.uninstall or args.mode or args.week_ok_until or args.replace_statusline
                       or args.auto_continue or args.sync_repo is not None or args.sync_mode):
        raise UsageError('--check changes nothing and takes only --settings, --target and --components.')


# --- Commands ---------------------------------------------------------------------------------------------------

def core_command(target: str, script: str, *extra: str) -> str:
    return _plat.script_command(os.path.join(target, script), *extra)


def fallback(component: str, mode: str | None) -> str:
    """Only the spawn gate in enforce fails closed. In shadow nothing may block, not even a broken installation."""
    return SPAWN_BROKEN if component == 'spawn-gate' and mode == 'enforce' else NEVER_BLOCK


def hook_command(target: str, component: str, mode: str | None, week_ok_until: str | None,
                 with_fallback: bool = True, event: str | None = None) -> str:
    """`event` picks the command of the sync (SYNC_COMMANDS); the other components have one for all their events."""
    extra = []
    if component in GATES:
        extra.append(f'--mode={mode}')
    if component == 'spawn-gate' and week_ok_until:
        extra.append(f'--week-ok-until={week_ok_until}')
    if component == 'sync':
        extra.append(SYNC_COMMANDS[event])
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
    wanted = os.path.normcase(os.path.normpath(path))      # normcase: C:\x and c:/x are the same file on Windows
    return any(os.path.normcase(os.path.normpath(os.path.expanduser(token))) == wanted for token in _tokens(command))


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
    for event, _ in HOOKS[component]:
        for group in data.get('hooks', {}).get(event, []):
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
            remove = (f'remove it with "install.py --uninstall --target {_plat.quote_path(folder)}"' if folder
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
        mode = modes[component][0] if component in modes else None
        earlier = option_value(installed_command(data, component, target), 'week-ok-until')
        for event, matcher in HOOKS[component]:
            command = hook_command(target, component, mode, args.week_ok_until, event=event)
            messages += upsert_hook(hooks, event, matcher, command, SCRIPTS[component],
                                    os.path.join(target, SCRIPTS[component]))
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
        for event, _ in HOOKS[component]:
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
    missing = [name for name in runtime_required() if not os.path.isfile(source_of(name))]
    return missing + ([] if os.path.isfile(EXAMPLE_CONFIG) else ['config.example.json'])


def runtime_required() -> list[str]:
    return list(REQUIRED_FILES) + list(REPORT_FILES) + (['_win.py'] if os.name == 'nt' else [])


def runtime_files() -> list[str]:
    """The files that go to the target: hooks/*.py, the usage report from tools/, and on Windows the Windows module
    from windows/cli/."""
    return sorted([name for name in os.listdir(SOURCE_HOOKS) if name.endswith('.py')] + list(REPORT_FILES)
                  + (['_win.py'] if os.name == 'nt' else []))


def source_of(name: str) -> str:
    if name in REPORT_FILES:
        return os.path.join(SOURCE_TOOLS, name)
    return WINDOWS_MODULE if os.name == 'nt' and name == '_win.py' else os.path.join(SOURCE_HOOKS, name)


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
    if component == 'context-reporter':
        try:
            context = json.loads(stdout)['hookSpecificOutput']['additionalContext']
        except (ValueError, KeyError, TypeError):
            return f'output is not the expected JSON: {stdout[:100]!r}'
        return None if context == SMOKE_CONTEXT else f'unexpected context line: {str(context)[:100]!r}'
    if component == 'sync':
        # Without a config (the smoke test has none) the sync is off and the hook only loads its modules: an exit 0
        # proves those, as they are imported before the hook catches anything. The output of SessionStart would
        # become context of the model.
        return None if not stdout else f'printed {stdout[:100]!r}; a session hook must print nothing'
    records = _log_records(log_dir, SCRIPTS[component][:-len('.py')])
    if not records:
        return 'the hook ran but wrote no log line (a module failed to load?)'
    for record in records:
        reason = str(record.get('reason', ''))
        if record.get('decision') == 'error' or 'internally' in reason:
            return f'failed internally ({reason[:100]})'
    return None


def write_smoke_context(log_dir: str) -> None:
    """The state file the status line would have written for the smoke session: fresh, 12 % of a 1000k window."""
    folder = os.path.join(log_dir, 'context')
    os.makedirs(folder)
    state = {'session_id': SMOKE_SESSION, 'context_window_size': 1_000_000, 'used_tokens': 123_456,
             'measured_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
    with open(os.path.join(folder, f'{SMOKE_SESSION}.json'), 'w', encoding='utf-8') as f:
        json.dump(state, f)


def smoke_test(args: argparse.Namespace, modes: dict[str, tuple[str, str]], shell: str | None = None) -> list[str]:
    """Starts each selected hook once from a temporary copy of the files that would be installed, in the shell that
    runs hook commands (`shell`: /bin/sh, on Windows Git Bash)."""
    shell = shell or _plat.hook_shell()
    errors = []
    with tempfile.TemporaryDirectory(prefix='usage-clock-smoke-') as tmp:
        copy_dir = os.path.join(tmp, 'hooks')
        os.mkdir(copy_dir)
        for name in runtime_files():
            shutil.copy2(source_of(name), os.path.join(copy_dir, name))
        os.mkdir(os.path.join(tmp, 'agents'))
        with open(os.path.join(tmp, 'agents', 'smoke-agent.md'), 'w', encoding='utf-8') as f:
            f.write('---\nname: smoke-agent\nmodel: haiku\neffort: low\n---\nSmoke test agent.\n')
        if 'context-reporter' in args.components:
            write_smoke_context(os.path.join(tmp, 'log'))
        # Python on Windows takes "~" from USERPROFILE, not from HOME
        env = dict(os.environ, HOME=os.path.join(tmp, 'home'), USERPROFILE=os.path.join(tmp, 'home'),
                   CCUC_USAGE_FILE=os.path.join(tmp, 'usage.json'),
                   CCUC_LOG_DIR=os.path.join(tmp, 'log'), CCUC_AGENT_DIRS=os.path.join(tmp, 'agents'),
                   CCUC_CONFIG=os.path.join(tmp, 'no-config.json'))
        for component in args.components:
            mode = modes[component][0] if component in modes else None
            command = hook_command(copy_dir, component, mode, args.week_ok_until, with_fallback=False,
                                   event=SMOKE_INPUTS[component].get('hook_event_name'))
            script = SCRIPTS[component]
            try:
                done = subprocess.run([shell, '-c', command], input=json.dumps(dict(SMOKE_INPUTS[component], cwd=tmp)),
                                      capture_output=True, text=True, encoding='utf-8', errors='replace',
                                      timeout=SMOKE_TIMEOUT_S, env=env, cwd=tmp)
            except subprocess.TimeoutExpired:
                errors.append(f'{script}: no answer within {SMOKE_TIMEOUT_S} s')
                continue
            if done.returncode != 0:
                errors.append(f'{script}: exit {done.returncode} - {done.stderr.strip()[:200]}')
                continue
            stdout = done.stdout.strip()
            if component not in ('statusline', 'sync') and stdout:
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
            _plat.fchmod(copy_file.fileno(), stat.S_IMODE(os.fstat(origin.fileno()).st_mode))
        _plat.replace(temp, destination)
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
        copy_exclusively(source_of(name), os.path.join(target, name))
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
            _plat.fchmod(config.fileno(), stat.S_IMODE(os.fstat(origin.fileno()).st_mode))
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


def write_settings(path: str, data: dict, existed: bool, backup: bool | None = None) -> str | None:
    """Backup (if the file exists, or as `backup` says), then temp file + os.replace. Keeps the permissions. Returns
    the backup path. Writes config.json, too."""
    backup = make_backup(path) if (existed if backup is None else backup) else None
    temp = path + TEMP_SUFFIX
    try:
        os.unlink(temp)  # a leftover of an aborted run, possibly a symlink: never write through it
    except FileNotFoundError:
        pass
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8',
                   newline='\n') as f:      # no CRLF on Windows
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write('\n')
    with open(temp, encoding='utf-8') as f:
        json.load(f)  # cross-check: valid JSON
    if existed:
        shutil.copymode(path, temp)
    _plat.replace(temp, path)
    return backup


# --- Sync -------------------------------------------------------------------------------------------------------

LOCK_HELD = 'Error: a sync run holds the lock right now. Run the installer again in a minute; nothing was installed.'
CREDENTIALS = ('git has to reach the repository without asking: sign in once with a credential helper (the Git '
               'Credential Manager of Git for Windows, or `gh auth setup-git`), or use sync.transport ssh with a key '
               'whose host github.com is in known_hosts. The installer never asks for credentials itself.')


class SyncPlan(NamedTuple):
    """What a run does for the component sync. `content` is config.json with the keys of `changes` set."""
    path: str               # the config.json the hooks read
    existed: bool
    content: dict
    changes: list[str]
    mode: str
    source: str             # where the mode comes from
    repo: str
    transport: str
    max_mb: int             # sync.max_clone_mb
    log_dir: str

    @property
    def clone(self) -> str:
        return _sync_git.clone_dir(self.log_dir)


def config_file(target: str) -> str:
    """The config.json the installed hooks read: CCUC_CONFIG, else the one in --target."""
    return os.environ.get('CCUC_CONFIG') or os.path.join(target, 'config.json')


def effective_config(path: str) -> dict:
    """The configuration the hooks would use with the config.json `path`: defaults where it is missing or broken."""
    return _config.load(log_problems=False, path=path)


def log_dir_of(cfg: dict) -> str:
    return os.path.normpath(os.path.expanduser(os.environ.get('CCUC_LOG_DIR') or cfg['log_dir']))


def read_config(path: str) -> dict:
    """config.json as it is, to set the sync keys in; a missing one as create_config writes it (the example). One that
    is a link or no JSON object is an argument error: nothing is changed."""
    if os.path.islink(path):
        raise UsageError(f'config.json must be a regular file, not a symlink: {path}. Set sync.repo and sync.mode '
                         'there by hand; nothing changed.')
    try:
        with open(path if os.path.exists(path) else EXAMPLE_CONFIG, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError) as error:
        raise UsageError(f'{path} cannot be read as JSON ({type(error).__name__}), so sync.repo and sync.mode cannot '
                         'be set; nothing changed.') from None
    if not isinstance(data, dict) or not isinstance(data.get('sync', {}), dict):
        raise UsageError(f'{path}: the top level and "sync" must be JSON objects, so sync.repo and sync.mode cannot '
                         'be set; nothing changed.')
    return data


def resolve_sync_mode(data: dict, args: argparse.Namespace, target: str, current) -> tuple[str, str, bool]:
    """(mode, where it comes from, whether config.json gets it), like resolve_modes for the gates. Call before
    plan_install changes data."""
    if args.sync_mode:
        return args.sync_mode, '--sync-mode', True
    if installed_command(data, 'sync', target) is None:
        return 'shadow', 'default, nothing installed yet', True
    if current in SYNC_MODES:
        return current, 'kept from config.json', False
    return 'off', 'kept from config.json, whose sync.mode is not valid: the hooks run as off', False


def plan_sync(data: dict, args: argparse.Namespace, target: str) -> SyncPlan:
    path = config_file(target)
    content = read_config(path)
    section = content.setdefault('sync', {})
    old_repo, old_mode = section.get('repo', ''), section.get('mode', 'off')
    mode, source, write_mode = resolve_sync_mode(data, args, target, old_mode)
    cfg = effective_config(path)
    repo = args.sync_repo or cfg['sync']['repo']
    if not repo:
        raise UsageError(f'The component sync needs --sync-repo owner/name: sync.repo is not set in {path}.')
    changes = []
    if args.sync_repo and old_repo != repo:
        section['repo'] = repo
        changes.append(f'sync.repo {old_repo!r} -> {repo!r}')
    if write_mode and old_mode != mode:
        section['mode'] = mode
        changes.append(f'sync.mode {old_mode!r} -> {mode!r}')
    return SyncPlan(path, os.path.exists(path), content, changes, mode, source, repo, cfg['sync']['transport'],
                    cfg['sync']['max_clone_mb'], log_dir_of(cfg))


def sync_blocked(plan: SyncPlan) -> bool:
    """The push is blocked after `visibility` (an unreadable state blocks nothing: it counts as empty)."""
    try:
        return _sync_core.read_state(os.path.join(plan.log_dir, _sync_core.SYNC_DIR)).blocked
    except _sync_core.BrokenState:
        return False


def git_folder(path: str) -> bool:
    """A real folder with a real `.git` folder (no link, no .git file), as the push opens it."""
    try:
        return all(_plat.is_folder(os.lstat(folder)) for folder in (path, os.path.join(path, '.git')))
    except OSError:
        return False


def prepare_clone(plan: SyncPlan) -> bool:
    """--sync-repo, before anything is written and under the sync lock (a push or pull that started meanwhile would
    find a half-made clone, and its clean-up of left-over git locks would break the clone): the visibility probe of the
    push, once and shown, then the clone, unless one of this repository is there. False (with the reason printed) if
    the repository is public or unclear or cannot be cloned, or a folder of another repository is in the way."""
    url = _sync_git.remote_url(plan.repo, plan.transport)
    directory = _sync_core.ensure_dir(plan.log_dir)
    with _sync_core.try_lock(directory) as held:
        if not held:
            print(LOCK_HELD)
            return False
        result = _sync_git.check_visibility(plan.repo, directory)
        if result == _sync_git.PUBLIC:
            print(f'Error: {plan.repo} can be read without logging in, it is public: nothing was installed. The sync '
                  'writes only to a private repository; make it private, then run again.')
            return False
        if result != _sync_git.PRIVATE:
            print(f'Error: whether {plan.repo} is public could not be told (no network, or an answer this tool does '
                  'not know): nothing was installed. Run again when GitHub can be reached.')
            return False
        _sync_git.remember_private(directory, plan.repo, time.time())
        print(f'Sync: {plan.repo} is not public (reading it without logging in is refused).')
        try:
            if os.path.lexists(plan.clone):
                if git_folder(plan.clone) and _sync_git.remote_matches(
                        _sync_git.clone_at(plan.clone, _sync_core.Deadline(CHECK_GIT_TIMEOUT_S), plan.max_mb), url):
                    print(f'Sync: the clone {plan.clone} is there and points to {plan.repo} - unchanged.')
                    return True
                print(f'Sync: {plan.clone} exists but is no clone of {url}. Move it away (it may hold commits that '
                      'were never pushed), then run again. Nothing was installed.')
                return False
            _sync_git.make_clone(directory, url, plan.max_mb, _sync_core.Deadline(CLONE_TIMEOUT_S))
        except _sync_core.SyncError as error:
            print(f'Sync: {error.detail}')
            if error.category == 'auth':
                print(CREDENTIALS)
            print('Nothing was installed.')
            return False
    print(f'Sync: cloned {plan.repo} to {plan.clone}.')
    return True


def write_sync_config(plan: SyncPlan) -> None:
    if not plan.changes:
        print('config.json: sync keys already set - unchanged')
        return
    backup = write_settings(plan.path, plan.content, os.path.exists(plan.path), backup=plan.existed)
    print(f'config.json: {", ".join(plan.changes)} written to {plan.path}')
    if backup:
        print(f'Backup:  {backup}')


def plan_sync_off(target: str) -> SyncPlan | None:
    """--uninstall with the sync: sync.mode back to `off`, or the status line would go on pushing (T1 needs no hook,
    only the mode). None if there is nothing to switch off: no config.json, or one with which the hooks run as off (an
    unreadable one gives the defaults)."""
    path = config_file(target)
    if not os.path.lexists(path) or effective_config(path)['sync']['mode'] == 'off':
        return None
    content = read_config(path)
    old = content['sync']['mode']
    content['sync']['mode'] = 'off'
    cfg = effective_config(path)
    return SyncPlan(path, True, content, [f'sync.mode {old!r} -> {"off"!r}'], 'off', '--uninstall', cfg['sync']['repo'],
                    cfg['sync']['transport'], cfg['sync']['max_clone_mb'], log_dir_of(cfg))


# --- Check ------------------------------------------------------------------------------------------------------

def _find_entry(data: dict, event: str, path: str) -> tuple[dict, dict] | None:
    """(group, entry) of our entry for this event (the one that runs `path`), None if there is none."""
    for group in data.get('hooks', {}).get(event, []):
        for entry in group.get('hooks', []):
            if points_to(entry.get('command'), path):
                return group, entry
    return None


def check_entries(data: dict, args: argparse.Namespace, target: str) -> list[tuple[str, str]]:
    results = []
    for component in (c for c in args.components if c in HOOKS):
        script = SCRIPTS[component]
        for event, matcher in HOOKS[component]:
            found = _find_entry(data, event, os.path.join(target, script))
            if found is None:
                results.append(('FIX', f'{event}: {script} not installed. Run install.py again.'))
                continue
            group, entry = found
            if matcher is not None and group.get('matcher') != matcher:
                results.append(('FIX', f'{event}: {script} has the matcher {group.get("matcher")}, expected {matcher}. '
                                       'Run install.py again.'))
                continue
            details = [f'matcher {matcher}'] if matcher is not None else []
            if component in GATES:
                details.append(f'mode {"enforce" if option_value(entry.get("command"), "mode") == "enforce" else "shadow"}')
            results.append(('OK', f'{event}: {script}' + (f' ({", ".join(details)})' if details else '')))
    if 'statusline' in args.components:
        current = data.get('statusLine')
        if isinstance(current, dict) and points_to(current.get('command'), os.path.join(target, SCRIPTS['statusline'])):
            results.append(('OK', 'statusLine: runs statusline.py'))
        elif current is None:
            results.append(('FIX', 'statusLine: not installed. Run install.py again.'))
        else:
            results.append(('FIX', 'statusLine: runs something else, so nothing writes the usage numbers and the hooks '
                                   'see them as unknown. Run install.py again with --replace-statusline to use ours.'))
    return results


def check_files(target: str) -> list[tuple[str, str]]:
    missing, different = [], []
    for name in runtime_files():
        path = os.path.join(target, name)
        if not os.path.isfile(path):
            missing.append(name)
        elif not filecmp.cmp(source_of(name), path, shallow=False):
            different.append(name)
    if missing or different:
        parts = ([f'missing {", ".join(missing)}'] if missing else []) + (
            [f'different from this checkout {", ".join(different)}'] if different else [])
        return [('FIX', f'Files: {"; ".join(parts)}. Run install.py again (after git pull, to install the new version).')]
    return [('OK', f'Files: {len(runtime_files())} runtime files in {target} match this checkout')]


def check_config(path: str) -> tuple[list[tuple[str, str]], dict]:
    """The results for config.json and the configuration the hooks would use (defaults where it is broken)."""
    if not os.path.isfile(path):
        return [('FIX', f'config.json: {path} does not exist, the hooks use the defaults. Run install.py again to '
                        'create it.')], copy.deepcopy(_config.DEFAULTS)
    try:
        with open(path, encoding='utf-8') as f:
            given = json.load(f)
    except (OSError, ValueError, RecursionError) as error:
        return [('FIX', f'config.json: {path} cannot be read as JSON ({type(error).__name__}), the hooks use the '
                        'defaults.')], copy.deepcopy(_config.DEFAULTS)
    problems: list[str] = []
    cfg = _config._merge_section(_config.DEFAULTS, given, '', problems)
    if problems:
        return [('FIX', f'config.json: {"; ".join(problems)}')], cfg
    return [('OK', f'config.json: {path}')], cfg


def check_usage(cfg: dict) -> list[tuple[str, str]]:
    path = os.path.expanduser(os.environ.get('CCUC_USAGE_FILE') or cfg['usage_file'])
    usage = _usage.read_usage(path, max_age_min=cfg['thresholds']['max_age_min'])
    if usage.state == 'unknown':
        return [('NOTE', 'Usage: no measurement yet. Start Claude Code in a terminal and send a message: the status '
                         'line writes the numbers after the first answer.')]
    age = f'measured {round(usage.age_min)} min ago'
    if usage.state == 'stale':
        return [('NOTE', f'Usage: {age}, older than {cfg["thresholds"]["max_age_min"]:g} min, so the hooks see it as '
                         'STALE. Only the status line of Claude Code in a terminal writes the numbers; send a message '
                         'there to renew them.')]
    return [('OK', f'Usage: {age}')]


def check_log_dir(cfg: dict) -> list[tuple[str, str]]:
    log_dir = log_dir_of(cfg)
    if not os.path.isdir(log_dir):
        return [('NOTE', f'Log folder: {log_dir} does not exist yet; the first hook run creates it.')]
    if not os.access(log_dir, os.W_OK):
        return [('FIX', f'Log folder: {log_dir} is not writable, the hooks cannot log.')]
    return [('OK', f'Log folder: {log_dir}')]


def _hours_ago(moment: float, now: float) -> str:
    return f'{_sync_core._text(moment)} ({int((now - moment) // 3600)} h ago)'


def check_clone(path: str, url: str, repo: str) -> list[tuple[str, str]]:
    """The clone: there, a git folder, clean (`git status` without taking the index lock: changes nothing), and its
    remote exactly the URL of sync.repo and sync.transport, as the push requires."""
    again = f'install.py --components sync --sync-repo {repo}'
    if not os.path.lexists(path):
        return [('FIX', f'Sync clone: {path} is missing. Run {again}.')]
    if not git_folder(path):
        return [('FIX', f'Sync clone: {path} is no git folder. Move it away, then run {again}.')]
    clone = _sync_git.clone_at(path, _sync_core.Deadline(CHECK_GIT_TIMEOUT_S), 0)
    failed = [('FIX', f'Sync clone: git status failed in the clone {path}.')]
    try:
        status = clone.git('--no-optional-locks', 'status', '--porcelain', '--untracked-files=all')
        matches = _sync_git.remote_matches(clone, url)
    except _sync_core.SyncError:      # no git, or no answer in time
        return failed
    if status.returncode != 0:
        return failed
    results = [('OK', f'Sync clone: {path} is there and clean') if not status.stdout else
               ('NOTE', f'Sync clone: {path} has changes that are not committed (a run that was cut off); the next '
                        'push commits them or puts them back.')]
    results.append(('OK', f'Sync remote: {url}') if matches else
                   ('FIX', f'Sync remote: the remote of the clone is not {url}, so every push ends `internal`. Move '
                           f'the clone away, then run {again}.'))
    return results


def check_sync_state(directory: str, repo: str, now: float) -> list[tuple[str, str]]:
    """Visibility (from the cache of the push, with its age), last success, backoff and the category of the last
    failure. `network` and `timeout` pass with the backoff; every other category needs a hand."""
    try:
        state = _sync_core.read_state(directory)
    except _sync_core.BrokenState:
        return [('NOTE', 'Sync state: state.json cannot be read, the next run counts it as empty.')]
    checked = _sync_git.private_checked(directory, repo)
    if state.blocked:
        results = [('FIX', 'Sync visibility: the repo was found public, nothing is pushed. Make it private, then run '
                           f'install.py --components sync --sync-repo {repo}.')]
    elif checked is None or checked > now:
        results = [('NOTE', 'Sync visibility: not checked yet; the next push checks it.')]
    elif now - checked < _sync_git.VISIBILITY_MAX_AGE_S:
        results = [('OK', f'Sync visibility: {repo} is not public (checked {int((now - checked) // 3600)} h ago)')]
    else:
        results = [('NOTE', f'Sync visibility: {repo} was found not public {int((now - checked) // 3600)} h ago; the '
                            'next push checks again.')]
    results.append(('OK', f'Sync last success: {_hours_ago(state.last_success, now)}') if state.last_success else
                   ('NOTE', 'Sync last success: none yet; the first run starts when a session ends.'))
    if state.last_error is None:
        results.append(('OK', 'Sync errors: none, no backoff'))
    else:
        later = 'not before install.py --sync-repo runs again' if state.blocked else (
            f'next try at {_sync_core._text(state.next_due)}' if state.next_due else 'next try with the next trigger')
        results.append(('NOTE' if state.last_error in ('network', 'timeout') else 'FIX',
                        f'Sync errors: the last run failed with {state.last_error}, backoff level '
                        f'{state.backoff_level}, {later}. Details: log_summary.py, section Sync.'))
    return results


def _size(path: str) -> int:
    """The bytes of the regular files below `path`, without entering a link or a junction (os.walk enters a junction on
    Windows); a file a push removes meanwhile counts 0."""
    def info(folder: str, name: str) -> os.stat_result | None:
        try:
            return os.lstat(os.path.join(folder, name))
        except OSError:
            return None
    total = 0
    for folder, folders, names in os.walk(path):
        folders[:] = [name for name in folders if (found := info(folder, name)) and _plat.is_folder(found)]
        total += sum(found.st_size for name in names if (found := info(folder, name)) and stat.S_ISREG(found.st_mode))
    return total


def check_clone_size(path: str, max_mb: int) -> list[tuple[str, str]]:
    total = _size(path)
    size = f'{total / MEGABYTE:.0f} MB'
    if total < CLONE_NOTE_MB * MEGABYTE:
        return [('OK', f'Sync clone size: {size}')]
    return [('NOTE', f'Sync clone size: {size}, at least {CLONE_NOTE_MB} MB. Fewer kept request months '
                     '(sync.keep_request_months) make it grow slower; a pull stops at sync.max_clone_mb '
                     f'({max_mb} MB).')]


def check_sync(cfg: dict) -> list[tuple[str, str]]:
    sync = cfg['sync']
    if not sync['repo']:
        return [('FIX', 'Sync repo: sync.repo is not set. Run install.py --components sync --sync-repo owner/name.')]
    log_dir = log_dir_of(cfg)
    path, url = _sync_git.clone_dir(log_dir), _sync_git.remote_url(sync['repo'], sync['transport'])
    results = [('OK', f'Sync repo: {sync["repo"]} ({sync["transport"]}, mode {sync["mode"]})')]
    results += check_clone(path, url, sync['repo'])
    directory = os.path.join(log_dir, _sync_core.SYNC_DIR)
    results += check_sync_state(directory, sync['repo'], time.time())
    if git_folder(path):
        results += check_clone_size(path, sync['max_clone_mb'])
    try:
        left = sorted(name for name in os.listdir(directory) if name.startswith(_sync_git.CLONE_TEMP_PREFIX))
    except OSError:      # no sync folder yet
        left = []
    results += [('NOTE', f'Sync: {os.path.join(directory, name)} is left over from a clone that was cut off; the sync '
                         'never reads it. Remove it when no installer runs.') for name in left]
    return results


def check(args: argparse.Namespace, settings_path: str, target: str) -> int:
    """Checks an installation and changes nothing. FIX = something to repair (exit 1), NOTE = worth knowing only."""
    print(f'Settings: {settings_path}')
    print(f'Target:   {target}')
    data, existed = load_settings(settings_path)
    if not args.components_given and installed_command(data, 'sync', target) is not None:
        args.components += ('sync',)
    results = [] if existed else [('FIX', 'settings.json: does not exist. Run install.py to install.')]
    results += check_entries(data, args, target)
    results += check_files(target)
    config_results, cfg = check_config(config_file(target))
    results += config_results
    shell = _plat.hook_shell(data.get('env'))
    if shell is None:
        results.append(('FIX', 'Git Bash: not found. Claude Code runs the hooks in Git Bash on Windows; install Git for '
                               'Windows (https://git-scm.com/downloads/win).'))
    else:
        errors = smoke_test(args, resolve_modes(data, args, target), shell)
        results += ([('FIX', f'Smoke test: {error}') for error in errors] if errors
                    else [('OK', f'Smoke test: passed ({", ".join(args.components)})')])
    if 'statusline' in args.components:
        results += check_usage(cfg)
    results += check_log_dir(cfg)
    sync_mode = effective_config(config_file(target))['sync']['mode']
    if sync_mode != 'off' and installed_command(data, 'sync', target) is None:
        # Whatever --components says: the status line starts pushes from the mode alone, hooks or not
        results.append(('FIX', f'Sync: sync.mode is {sync_mode} in {config_file(target)}, but the hooks of the sync '
                               'are not installed, and the status line goes on pushing. Run install.py --uninstall '
                               '--components sync to switch it off, or install.py --components sync to install it.'))
    if 'sync' in args.components:
        results += check_sync(effective_config(config_file(target)))
    for status, text in results:
        print(f'{status:<4} {text}')
    problems = sum(status == 'FIX' for status, _ in results)
    print(f'Check: {problems} problem(s), see the FIX lines.' if problems else 'Check: no problems.')
    return 1 if problems else 0


# --- Main -------------------------------------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    settings_path = os.path.abspath(os.path.expanduser(args.settings))
    target = os.path.abspath(os.path.expanduser(args.target))
    if args.check:
        return check(args, settings_path, target)
    data, existed = load_settings(settings_path)
    before = copy.deepcopy(data)
    print(f'Settings: {settings_path}' + ('' if existed else ' (does not exist yet)'))
    print(f'Target:   {target}')
    sync = None
    if args.uninstall:
        messages = plan_uninstall(data, args, target)
        sync = plan_sync_off(target) if 'sync' in args.components else None
    else:
        missing = missing_sources()
        if missing:
            print(f'Error: files missing in the source folders {SOURCE_HOOKS} and {SOURCE_TOOLS}: {", ".join(missing)}')
            return 1
        shell = _plat.hook_shell(data.get('env'))
        if shell is None:
            print('Error: Git Bash was not found. Claude Code runs hook commands in Git Bash on Windows (in PowerShell only '
                  'if Git for Windows is missing), and the commands written here are for bash. Install Git for Windows '
                  '(https://git-scm.com/downloads/win) or, if it is installed where Claude Code cannot find it, set '
                  'CLAUDE_CODE_GIT_BASH_PATH to its bash.exe (in the environment or in the "env" of settings.json). '
                  'Nothing was changed.')
            return 1
        modes = resolve_modes(data, args, target)
        if modes:
            print(f'Mode:     {describe_modes(modes)}')
        if 'sync' in args.components:
            sync = plan_sync(data, args, target)
            print(f'Sync mode: {sync.mode} ({sync.source})')
        messages = plan_install(data, args, target, modes)
    print('\n'.join(messages))
    if not args.uninstall:
        errors = smoke_test(args, modes, shell)
        if errors:
            print('SMOKE TEST FAILED - nothing written:')
            for error in errors:
                print('  ', error)
            return 1
        print(f'Smoke test: passed ({", ".join(args.components)}).')
    changed = data != before
    if sync and not args.uninstall and not args.sync_repo:
        if not os.path.lexists(sync.clone):
            print(f'Sync: the clone {sync.clone} is missing; run again with --sync-repo {sync.repo} to make it.')
        if sync_blocked(sync):
            print(f'Sync: a push found {sync.repo} public, so nothing is pushed. Make it private, then run install.py '
                  f'--components sync --sync-repo {sync.repo} to lift the block.')
    if args.dry_run:
        if not args.uninstall:
            note = 'would be created from config.example.json' if not config_exists(target) else 'exists, stays untouched'
            print(f'Files: {len(runtime_files())} runtime files would be copied to {target}; config.json {note}.')
            print(f'Report: {", ".join(REPORT_FILES + REPORT_MODULES)} would be copied to {target}.')
        if sync:
            print(f'config.json: {", ".join(sync.changes)} would be written' if sync.changes
                  else 'config.json: sync keys already set - unchanged')
        if sync and args.sync_repo:
            print(f'Sync: would check that {sync.repo} is not public; the clone {sync.clone} stays if it points to it.'
                  if os.path.lexists(sync.clone) else
                  f'Sync: would check that {sync.repo} is not public, then clone it to {sync.clone} (at most '
                  f'{sync.max_mb} MB).')
            if sync_blocked(sync):
                print('Sync: the block after a public repo would be lifted.')
        print('DRY RUN - nothing written.' + ('' if changed else ' settings.json would stay as it is.'))
        return 0
    if sync and args.sync_repo:
        blocked = sync_blocked(sync)
        if not prepare_clone(sync):
            return 1
        if blocked:
            if not _sync_core.clear_visibility(sync.log_dir, time.time()):
                print(LOCK_HELD)
                return 1
            print('Sync: the block after a public repo is lifted, the next push is due.')
    if not args.uninstall:
        count = copy_runtime_files(target)
        note = 'created from config.example.json' if create_config(target) else 'kept as it is'
        print(f'Files: {count} runtime files copied to {target}; config.json {note}.')
    if sync:
        write_sync_config(sync)
    if not changed:
        print('settings.json is already up to date - not written.')
        return 0
    backup = write_settings(settings_path, data, existed)
    print(f'Written: {settings_path}')
    if backup:
        print(f'Backup:  {backup}')
        print(f'Undo:    cp {_plat.quote_path(backup)} {_plat.quote_path(settings_path)}')
    if not args.uninstall:
        print('Start a new Claude Code session for the changes to take effect.')
        if any(mode == 'shadow' for mode, _ in modes.values()):
            print('Mode shadow: nothing is blocked, decisions are only logged. Use --mode enforce to switch them on.')
        if sync and sync.mode == 'shadow':
            print('Sync mode shadow: commits stay on this machine, nothing is pushed. Use --sync-mode on to push.')
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
