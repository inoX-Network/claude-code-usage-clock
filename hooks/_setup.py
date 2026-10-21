"""What the installer (tools/install.py) and the Settings tab of /report (settings_cli.py) share, so that both work
alike: how an entry of this tool is recognised in settings.json, the one way config.json is written (checked, with a
backup, atomically), the clone of the sync repository, and the texts that explain each part.

config.json gets only the keys of SECTIONS from here (sync.mode, sync.repo, sync.machine_name and the four switches of
`modules`); every other key of that file is never written. Nothing here writes settings.json except write_json for the
installer, which calls it with its own plan.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import time
from datetime import datetime
from typing import NamedTuple

import _config
import _plat
import _sync_core
import _sync_git

SECTIONS = ('sync', 'modules')      # the sections of config.json whose keys are written here
SCRIPTS = {'statusline': 'statusline.py', 'usage-clock': 'usage_clock.py', 'context-reporter': 'context_reporter.py',
           'spawn-gate': 'spawn_gate.py', 'soft-stop': 'soft_stop.py', 'sync': 'sync.py'}
# component -> [(event, matcher), ...]. None = no matcher. One command per component (the sync: one per event, see
# SYNC_COMMANDS), one entry per event.
HOOKS = {'usage-clock': [('UserPromptSubmit', None)],
         'context-reporter': [('UserPromptSubmit', None), ('PostToolUse', '*')],
         'spawn-gate': [('PreToolUse', 'Agent|Workflow|Skill')],
         'soft-stop': [('PreToolUse', '*')],
         'sync': [('SessionStart', None), ('SessionEnd', None)]}
TOUCHED_EVENTS = tuple(dict.fromkeys(event for entries in HOOKS.values() for event, _ in entries))
TEMP_SUFFIX = '.tmp-usage-clock'
CLONE_TIMEOUT_S = 300           # the clone of --sync-repo, under the sync lock (no push runs meanwhile)
CHECK_GIT_TIMEOUT_S = 15        # each git call of --check in the clone
DOCS = 'https://github.com/inoX-Network/claude-code-usage-clock/blob/main/docs/'
# One sentence per part: the line above each question of the assistant, and the line of each part in the tab
SUMMARY = {
    'statusline': 'Shows the model, the 5-hour and weekly usage and the context below the prompt, and measures the '
                  'usage the other parts read.',
    'usage-clock': 'Gives the model the usage and the local time with every prompt you send.',
    'context-reporter': 'Says how full the context window is, as the status line measured it.',
    'spawn-gate': 'Checks every start of an agent, workflow or skill against the usage and the model rules.',
    'soft-stop': 'Makes subagents save a checkpoint and finish when the five-hour usage gets high.',
    'auto-continue': 'Claude Code then waits for the reset of a usage limit and goes on by itself.',
    'sync': 'Puts the usage of each computer into a private GitHub repository of yours, so that the report shows all '
            'of them; a first install starts in shadow mode and pushes nothing.',
}
# What the answer i or ? prints for a question: what the part does, what it writes, where the documentation is
INFO = {
    'statusline': (
        '  Runs statusline.py below the prompt every 60 s: model, 5h and weekly usage with reset times, context, cache.',
        '  It is the part that measures: it writes the usage file (usage_file) the other parts read.',
        '  Writes: statusLine in settings.json.',
        f'  More: {DOCS}statusline.md#what-the-status-line-shows'),
    'replace': (
        '  Yes puts our status line in place of the other one (statusLine in settings.json). The other one is not',
        '  restored on uninstall: copy it back from the backup the installer names. No keeps it, and nothing measures.',
        f'  More: {DOCS}install.md#uninstall-completely'),
    'usage-clock': (
        '  Adds one line for the model to every prompt you send: 5h and weekly usage, reset times, local time.',
        '  Writes: a UserPromptSubmit hook (usage_clock.py) in settings.json; display in config.json turns parts off.',
        f'  More: {DOCS}statusline.md#what-the-model-sees'),
    'context-reporter': (
        '  Tells the model how full its context window is, as the status line measured it: with every prompt, and',
        '  after tool calls once the window is fuller (context_reporter in config.json).',
        '  Writes: a UserPromptSubmit and a PostToolUse hook (context_reporter.py) in settings.json.',
        f'  More: {DOCS}statusline.md#the-context-window'),
    'spawn-gate': (
        '  Checks every start of an agent, workflow or skill: no new block from 75 % of the 5-hour usage, a warning or',
        '  a stop near the weekly limit, and model and effort in the agent definitions (thresholds in config.json).',
        '  Writes: a PreToolUse hook (spawn_gate.py, matcher Agent|Workflow|Skill) in settings.json.',
        f'  More: {DOCS}gates.md#the-two-gates'),
    'soft-stop': (
        '  Watches every tool call of a subagent: a warning from 85 % of the 5-hour usage, only checkpoint writes from',
        '  92 %, everything else denied from 95 % (thresholds in config.json).',
        '  Writes: a PreToolUse hook (soft_stop.py, matcher *) in settings.json.',
        f'  More: {DOCS}gates.md#the-two-gates'),
    'enforce': (
        '  Shadow: the gates decide, write the decision to the log and tell the model, but block nothing.',
        '  Enforce: they block and warn for real, and a broken spawn gate denies starts.',
        '  Writes: --mode=shadow or --mode=enforce in the commands of both gate hooks in settings.json.',
        f'  More: {DOCS}gates.md#from-shadow-to-enforce'),
    'auto-continue': (
        '  Claude Code waits when a usage limit is reached and goes on by itself once it resets.',
        '  Writes: autoContinueAtUsageLimit: true in settings.json. The installer never switches it off; remove the key',
        '  by hand.',
        f'  More: {DOCS}install.md#install'),
    'sync': (
        '  Puts the usage of this computer into a private GitHub repository of yours, so that the report of every',
        '  computer shows all of them; no prompt, file content or host name leaves it. Starts in shadow: pushes nothing.',
        '  Writes: SessionStart and SessionEnd hooks (sync.py) in settings.json; sync.mode, sync.repo and',
        '  sync.machine_name in config.json; a clone below the log folder.',
        f'  More: {DOCS}sync.md#what-it-does'),
    'repo': (
        '  owner/name of a PRIVATE repository on GitHub. The installer checks once that it cannot be read without',
        '  logging in (public: nothing installed) and clones it; git must reach it without asking for a password.',
        '  Writes: sync.repo in config.json.',
        f'  More: {DOCS}sync.md#install'),
    'name': (
        '  How this computer appears in the report of all your computers. Empty shows machine- and the start of its',
        '  id; the host name is never used. At most 64 letters, digits, spaces and . + @ = - _.',
        '  Writes: sync.machine_name in config.json.',
        f'  More: {DOCS}configuration.md#configuration'),
}

# What the Settings tab does differently from the assistant, one sentence per part (the tab switches config.json; the
# assistant writes the hook entries)
TAB = {
    'statusline': 'The status line cannot be switched off here: the other parts read its measurement.',
    'usage-clock': 'Here the switch is modules.usage_clock in config.json: off, the hook stays and adds nothing.',
    'context-reporter': 'Here the switch is modules.context_reporter in config.json: off, the hook stays and says '
                        'nothing.',
    'spawn-gate': 'Here the buttons set modules.spawn_gate in config.json, which comes before the --mode of the hook '
                  'entry; off lets every start through. Without a value the mode of the entry applies (installed).',
    'soft-stop': 'Here the buttons set modules.soft_stop in config.json, which comes before the --mode of the hook '
                 'entry; off lets every call through. Without a value the mode of the entry applies (installed).',
    'sync': 'Here the buttons set sync.mode in config.json; the hooks of the sync stay as they are.',
    'repo': 'Check runs the test of --sync-repo and writes nothing; Save sets sync.repo and clones it, as the installer '
            'does.',
}


class UsageError(Exception):
    """Bad arguments or an unusable settings.json (exit code 2)."""


# --- settings.json ----------------------------------------------------------------------------------------------

def _tokens(command) -> list[str]:
    try:
        return shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return []


def points_to(command, path: str) -> bool:
    """Does the command run exactly this file?"""
    wanted = os.path.normcase(os.path.normpath(path))      # normcase: C:\x and c:/x are the same file on Windows
    return any(os.path.normcase(os.path.normpath(os.path.expanduser(token))) == wanted for token in _tokens(command))


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


def statusline_is_ours(data: dict, target: str) -> bool:
    """The statusLine of settings.json runs <target>/statusline.py."""
    current = data.get('statusLine')
    return isinstance(current, dict) and points_to(current.get('command'), os.path.join(target, SCRIPTS['statusline']))


def installed_parts(data: dict, target: str) -> set[str]:
    """The components with an entry of ours in settings.json (the status line: ours runs), as --check sees them."""
    parts = {component for component in HOOKS if installed_command(data, component, target) is not None}
    return parts | ({'statusline'} if statusline_is_ours(data, target) else set())


# --- config.json ------------------------------------------------------------------------------------------------

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


def write_json(path: str, data: dict, existed: bool, backup: bool | None = None) -> str | None:
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


def read_config(path: str, example: str | None = None) -> dict:
    """config.json as it is, to set its keys in; a missing one as the installer creates it (`example`, its
    config.example.json; without one a missing config.json is an error). One that is a link or no JSON object, or whose
    "sync" or "modules" is no object, is an argument error: nothing is changed."""
    if os.path.islink(path):
        raise UsageError(f'config.json must be a regular file, not a symlink: {path}. Set its keys there by hand; '
                         'nothing changed.')
    if not os.path.exists(path) and example is None:
        raise UsageError(f'{path} does not exist. Run tools/install.py, which creates it; nothing changed.')
    try:
        with open(path if os.path.exists(path) else example, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError) as error:
        raise UsageError(f'{path} cannot be read as JSON ({type(error).__name__}), so its keys cannot be set; nothing '
                         'changed.') from None
    if not isinstance(data, dict) or not all(isinstance(data.get(section, {}), dict) for section in SECTIONS):
        raise UsageError(f'{path}: the top level, "sync" and "modules" must be JSON objects, so its keys cannot be set; '
                         'nothing changed.')
    return data


def write_keys(path: str, sections: dict, existed: bool, example: str | None = None) -> str | None:
    """Sets the keys of `sections` ({'sync': {'mode': 'on'}}) into config.json as it is now (read again: the clone may
    have taken minutes) and writes it with write_json, with a backup if it `existed` before the run. Returns the backup
    path."""
    content = read_config(path, example)
    for section, values in sections.items():
        content.setdefault(section, {}).update(values)
    return write_json(path, content, os.path.exists(path), backup=existed)


# --- Sync -------------------------------------------------------------------------------------------------------

LOCK_HELD = 'Error: a sync run holds the lock right now. Run the installer again in a minute; nothing was installed.'
CREDENTIALS = ('git has to reach the repository without asking: sign in once with a credential helper (the Git '
               'Credential Manager of Git for Windows, or `gh auth setup-git`), or use sync.transport ssh with a key '
               'whose host github.com is in known_hosts. The installer never asks for credentials itself.')
MISSING_REPO = ('Check the name ({repo}, owner/name). A repository that does not exist yet: create it private with '
                '`gh repo create {repo} --private` or on https://github.com/new. One that exists: give the login of git '
                'access to it (a fine-grained token needs this repository selected). Then run the installer again.')


class SyncPlan(NamedTuple):
    """What a run does for the component sync. `updates` are the keys of the section sync that `changes` describes."""
    path: str               # the config.json the hooks read
    existed: bool
    updates: dict
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
    """The log folder the hooks use, absolute: git clones with the sync folder as its working folder, so a relative
    path would land below it."""
    return os.path.abspath(os.path.expanduser(os.environ.get('CCUC_LOG_DIR') or cfg['log_dir']))


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


def clone_points_to(plan: SyncPlan) -> bool:
    """The folder of the clone (which exists) is a git folder whose remote is the URL of plan.repo, as the push
    requires. Raises SyncError if git cannot tell in time."""
    url = _sync_git.remote_url(plan.repo, plan.transport)
    return git_folder(plan.clone) and _sync_git.remote_matches(
        _sync_git.clone_at(plan.clone, _sync_core.Deadline(CHECK_GIT_TIMEOUT_S), plan.max_mb), url)


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
                if clone_points_to(plan):
                    print(f'Sync: the clone {plan.clone} is there and points to {plan.repo} - unchanged.')
                    return True
                print(f'Sync: {plan.clone} exists but is no clone of {url}. Move it away (it may hold commits that '
                      'were never pushed), then run again. Nothing was installed.')
                return False
            _sync_git.make_clone(directory, url, plan.max_mb, _sync_core.Deadline(CLONE_TIMEOUT_S))
        except _sync_core.SyncError as error:
            print(f'Sync: {error.detail}')
            if error.category == 'auth':
                print(MISSING_REPO.format(repo=plan.repo) if error.detail.endswith(_sync_git.NOT_FOUND) else
                      CREDENTIALS)
            print('Nothing was installed.')
            return False
    print(f'Sync: cloned {plan.repo} to {plan.clone}.')
    return True


def write_sync_config(plan: SyncPlan, example: str | None = None) -> None:
    if not plan.changes:
        print('config.json: sync keys already set - unchanged')
        return
    # Read again: the clone may have taken minutes, and only the keys of this run are set into what is there now
    backup = write_keys(plan.path, {'sync': plan.updates}, plan.existed, example)
    print(f'config.json: {", ".join(plan.changes)} written to {plan.path}')
    if backup:
        print(f'Backup:  {backup}')
