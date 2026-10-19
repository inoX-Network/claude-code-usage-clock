"""The git work of the sync: the push (0034-SYNC-PUSH), and the clone and the fast-forward the pull (0034-SYNC-REPORT)
shares with it. Whatever is unclear ends without a push; a public repository is never written to.

The clone `<log_dir>/sync/repo` is made by the installer (`--sync-repo`); nothing here clones. Every git call in it
(`Clone.git`) runs with GIT_DIR and GIT_WORK_TREE set to the clone (never a working folder, never a repository above
it), the environment of `_sync_core.git_env` (no prompt, no askpass, ssh in batch mode, the core.sshCommand of the
clone's own config), messages in C (`LC_ALL=C`: git translates them otherwise, and errors are told apart by their
text), a fixed identity for the commit (`usage-clock`, in the environment: it beats any user.name and any GIT_AUTHOR_*
of the caller), and options that keep the user's git config from doing more than the sync asks: no hooks (core.hooksPath
is an empty folder of its own, `<log_dir>/sync/no-hooks`: `--no-verify` alone would still run post-commit,
reference-transaction and a global hooksPath of the user), no fsmonitor, no signing, no automatic gc or maintenance (a
detached gc would outlive the lock), no submodules.

A push (`push`, the runner of `sync.py push`), after the core took the lock and wrote `last_attempt`:

2. **Remote** (`open_clone`): `sync.repo` set and of the form owner/name, the clone a real folder with a `.git`
   folder, and `git remote get-url --all origin` as well as `--push --all` (insteadOf and pushInsteadOf applied) give
   exactly the one URL made of `sync.repo` and `sync.transport` (URLS). Then left-over git locks and an interrupted
   rebase of an ended run are cleared (`_heal`), and the branch checked out must be the default branch of the remote,
   the only one pushed. Else `internal`. No attributes of the repository apply (`attr.tree` is the empty tree).
3. **Visibility** (`check_visibility`), unless a result `private` for this repository is younger than 24 h
   (`<log_dir>/sync/visibility.json`): `git ls-remote` of `https://github.com/<owner>/<name>.git` without any git
   config of the user or the system, without credential helper, prompt or askpass, HOME in the sync folder (no
   .netrc), GIT_DIR on the null device (no repository put into the sync folder is read), messages in C, 15 s.
   VISIBILITY_TABLE tells exit code and error text apart: readable is public (`visibility`: the core blocks every
   push until the installer runs again), a request for a user name is private (cached), anything else is unclear
   (`network`, no push).
4. **Update** (`update_clone`, shared with the pull): fetch; behind the remote: fast-forward. A local history that
   went apart from the remote (commits of an earlier run that were not pushed: shadow mode, a failed push) is rebased
   onto it if the work tree is clean, else after the commit (step 7b).
5. **Export** (`_sync_export.export`) and **compacting** (`sync_format.compact`) of the own request months beyond
   `sync.keep_request_months`. The months that may go are taken before anything is removed and noted in
   `<log_dir>/sync/compacting.json` first (no note, no removal); the note is cleared after the commit. A run that
   ended between compacting and committing thus leaves deletions the next run may still commit.
6. **Commit guard** (`guard`): every changed path, staged or not, lies below `machines/<own id>/`, nothing is left
   that git did not take, every added or changed path is a regular file of the layout (mode 100644, a regular file in
   the work tree), and a deletion only removes a request file of a month step 5 found compactable (now or in the noted
   run). Else `internal` and no commit. No change: no commit.
7. `status.json`, then the commit `sync: <name> <time UTC>`. 7b: a history that went apart is rebased onto the remote
   (never merged); a conflict aborts the rebase (`conflict`), the own commits stay.
8. **Shadow:** no push; one log line `shadow` with the lines and bytes of this export.
9. **Push:** every outgoing commit, one by one, passes the same guard again (no merge commit); refused because
   another machine was faster (porcelain `[rejected] (fetch first)` / `(non-fast-forward)`): fetch, rebase, push once
   more, then `rejected`. Refused by the repository: `rejected`. Credentials missing or refused: `auth` (AUTH_TEXTS);
   any other failure to reach it: `network`.

Nothing exported is ever thrown away: no reset, checkout, clean or stash, also not after `conflict` or a failed guard
(the export cursor has moved on). Errors carry a fixed text, never output of git.
"""
from __future__ import annotations

import dataclasses
import json
import os
import posixpath
import re
import stat
import subprocess
import time
from collections.abc import Callable

import _config
import _context_state
import _sync_core
import _sync_export
import sync_format

CLONE_DIR = 'repo'
NO_HOOKS_DIR = 'no-hooks'
VISIBILITY_FILE = 'visibility.json'
JOURNAL_FILE = 'compacting.json'
VISIBILITY_MAX_AGE_S = 24 * 3600
VISIBILITY_TIMEOUT_S = 15
PROJECTS_DIR = '~/.claude/projects'
TOOL_VERSION = '1.6.0'          # `tool_version` of status.json: the release that brings the sync
REMOTE = 'origin'
URLS = {'https': 'https://github.com/{}.git', 'ssh': 'git@github.com:{}.git'}
PROBE_URL = 'https://github.com/{}.git'
NULL_CONFIG = '/dev/null'       # no global config; Git for Windows reads this name as NUL
REGULAR = '100644'              # the only mode of a file the sync commits
DEFAULT_BRANCH = 'main'         # the branch pushed to if the clone does not know the default branch of the remote
EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'      # SHA-1 of the empty tree: attributes from nowhere
STALE_LOCK_S = _sync_core.running_window_s()  # a git lock in the clone older than any run of the sync is left over
IDENTITY = {'GIT_AUTHOR_NAME': 'usage-clock', 'GIT_AUTHOR_EMAIL': 'usage-clock@sync.invalid',
            'GIT_COMMITTER_NAME': 'usage-clock', 'GIT_COMMITTER_EMAIL': 'usage-clock@sync.invalid'}
# Variables of the caller that would point git at another repository, index, attribute source or time, or start an
# editor
FOREIGN_ENV = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
               'GIT_COMMON_DIR', 'GIT_NAMESPACE', 'GIT_CEILING_DIRECTORIES', 'GIT_AUTHOR_DATE', 'GIT_COMMITTER_DATE',
               'GIT_SEQUENCE_EDITOR', 'GIT_ATTR_SOURCE')
# attr.tree and core.attributesFile: no .gitattributes of another machine (nor a global one) decides how the own files
# are stored or checked out (filters such as git-lfs, working-tree-encoding, eol).
SETTINGS = ('core.fsmonitor=false', 'commit.gpgSign=false', 'push.gpgSign=false', 'gc.auto=0',
            'maintenance.auto=false', 'submodule.recurse=false', 'fetch.recurseSubmodules=false',
            'push.recurseSubmodules=no', 'rebase.autoStash=false', 'rebase.updateRefs=false',
            'merge.verifySignatures=false', 'push.followTags=false', f'attr.tree={EMPTY_TREE}',
            'core.attributesFile=', 'color.ui=false', 'log.showRoot=true')
BRANCH = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}')
REQUEST_FILE = re.compile(r'requests/(?P<month>[0-9]{4}-[0-9]{2})/[A-Za-z0-9_-]{1,128}\.jsonl')

# Visibility: exit code, a text the error output must contain, the result. The first row that fits counts; none fits:
# unclear. Measured (git 2.56): public exit 0 with the refs; private and missing both exit 128 with this request for a
# user name (anonymously the two cannot be told apart, both are not public); an unknown host exit 128 with
# `Could not resolve host` (unclear).
PUBLIC, PRIVATE, UNCLEAR = 'public', 'private', 'unclear'
ASKS_FOR_USER = "could not read Username for 'https://github.com': terminal prompts disabled"
VISIBILITY_TABLE = ((0, '', PUBLIC), (128, ASKS_FOR_USER, PRIVATE))

# Error texts of a fetch or push (in C) for missing or refused credentials: `auth`. Every other failure is `network`.
AUTH_TEXTS = ('could not read Username', 'could not read Password', 'terminal prompts disabled',
              'Authentication failed', 'Permission denied', 'Repository not found', 'denied to',
              'returned error: 401', 'returned error: 403', 'Invalid username or password')
# Push refused because the remote has commits this clone lacks (`git push --porcelain`)
BEHIND_REASONS = ('(fetch first)', '(non-fast-forward)')

SAME, AHEAD, BEHIND, APART = 'same', 'ahead', 'behind', 'apart'
UP_TO_DATE, FAST_FORWARDED, REBASED, DIVERGED_DIRTY = 'up_to_date', 'fast_forwarded', 'rebased', 'diverged_dirty'

Execute = Callable[..., subprocess.CompletedProcess]


def _fail(category: str, detail: str) -> _sync_core.SyncError:
    return _sync_core.SyncError(category, detail)


# --- The clone --------------------------------------------------------------------------------------------------------

def clone_dir(log_dir: str) -> str:
    """`<log_dir>/sync/repo`, the clone of the sync repository (only the path, nothing is checked)."""
    return os.path.join(log_dir, _sync_core.SYNC_DIR, CLONE_DIR)


def remote_url(repo: str, transport: str) -> str:
    """The URL of `owner/name` for `sync.transport` (`https` or `ssh`): the one the clone's remote must have."""
    return URLS[transport].format(repo)


def configured_repo(cfg: dict) -> str:
    """`sync.repo`; `internal` if it is empty or no owner/name."""
    repo = cfg['sync']['repo']
    if not repo:
        raise _fail('internal', 'sync.repo is not set, run the installer with --sync-repo')
    if not _config.valid_sync_repo(repo):
        raise _fail('internal', 'sync.repo is not owner/name')
    return repo


@dataclasses.dataclass(frozen=True)
class Clone:
    """The clone of a run: its folder and branch; `git` runs git in it within the deadline of the run."""
    path: str
    branch: str
    deadline: _sync_core.Deadline = dataclasses.field(repr=False)
    env: dict = dataclasses.field(repr=False)
    options: tuple = dataclasses.field(repr=False)

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return _sync_core.run_command([_sync_core.GIT, *self.options, *args], self.deadline, self.env, self.path)


def _empty_hooks(directory: str) -> str:
    """`<directory>/no-hooks`, an empty folder (made if missing): the core.hooksPath of every git call in the clone."""
    path = os.path.join(directory, NO_HOOKS_DIR)
    try:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        if stat.S_ISDIR(os.lstat(path).st_mode) and not os.listdir(path):
            return path
    except OSError as error:
        raise _fail('internal', f'{NO_HOOKS_DIR} cannot be made ({type(error).__name__})') from error
    raise _fail('internal', f'{NO_HOOKS_DIR} in the sync folder is no empty folder')


def _clone_env(path: str, deadline: _sync_core.Deadline) -> dict[str, str]:
    base = {name: value for name, value in os.environ.items() if name not in FOREIGN_ENV}
    base.update(GIT_DIR=os.path.join(path, '.git'), GIT_WORK_TREE=path)
    env = _sync_core.git_env(base, deadline)      # with GIT_DIR it reads the core.sshCommand of the clone
    env.update(IDENTITY, LC_ALL='C', LANGUAGE='C', GIT_EDITOR=':',     # `:` is git's editor that does nothing
               GIT_ATTR_NOSYSTEM='1')
    return env


def open_clone(run: _sync_core.Run) -> Clone:
    """Step 2: the clone of `sync.repo`, checked. `internal` (fixed text) if sync.repo is empty or no owner/name, the
    clone is missing, a link or no git folder, no branch is checked out, or the fetch or push URL of `origin` is not
    exactly the one of sync.repo and sync.transport. Never clones."""
    repo = configured_repo(run.cfg)
    path = clone_dir(run.log_dir)
    try:
        if not all(stat.S_ISDIR(os.lstat(folder).st_mode) for folder in (path, os.path.join(path, '.git'))):
            raise _fail('internal', 'the sync clone is no git folder')      # a link or a file (a .git file, too)
    except FileNotFoundError:
        raise _fail('internal', 'the sync clone is missing, run the installer with --sync-repo') from None
    except OSError as error:
        raise _fail('internal', f'the sync clone cannot be read ({type(error).__name__})') from error
    options = tuple(arg for setting in (f'core.hooksPath={_empty_hooks(run.directory)}', *SETTINGS)
                    for arg in ('-c', setting))
    clone = Clone(path, '', run.deadline, _clone_env(path, run.deadline), options)
    expected = [remote_url(repo, run.cfg['sync']['transport'])]
    for which in ((), ('--push',)):
        done = clone.git('remote', 'get-url', '--all', *which, REMOTE)
        if done.returncode != 0 or done.stdout.splitlines() != expected:
            raise _fail('internal', 'the remote of the sync clone is not sync.repo')
    _heal(clone)
    done = clone.git('symbolic-ref', '--quiet', '--short', 'HEAD')
    branch = done.stdout.strip()
    if done.returncode != 0 or not BRANCH.fullmatch(branch):
        raise _fail('internal', 'the sync clone has no branch checked out')
    if branch != _default_branch(clone):
        raise _fail('internal', 'the sync clone has another branch checked out than the default branch of the remote')
    return dataclasses.replace(clone, branch=branch)


def _default_branch(clone: Clone) -> str:
    """The branch `origin/HEAD` names (set by the clone of the installer), else DEFAULT_BRANCH: the only one pushed."""
    done = clone.git('symbolic-ref', '--quiet', '--short', f'refs/remotes/{REMOTE}/HEAD')
    name = done.stdout.strip()
    return name[len(REMOTE) + 1:] if done.returncode == 0 and name.startswith(REMOTE + '/') else DEFAULT_BRANCH


def _stale_locks(git_dir: str) -> list[str]:
    """The lock files of git below `git_dir` (index, refs, packed-refs, HEAD, config …), objects left out."""
    found = []
    for folder, dirs, names in os.walk(git_dir):
        if folder == git_dir and 'objects' in dirs:
            dirs.remove('objects')
        found += [os.path.join(folder, name) for name in names if name.endswith('.lock')]
    return sorted(found)


def _heal(clone: Clone) -> None:
    """What a run the watchdog or a shutdown ended left in the clone. The caller holds the sync lock, so no other run
    works in it: a git lock older than STALE_LOCK_S is left over and removed; a younger one belongs to a git someone
    else started (`internal`, try later). An interrupted rebase is aborted: the branch is back on its own commits."""
    git_dir = os.path.join(clone.path, '.git')
    try:
        for lock in _stale_locks(git_dir):
            if time.time() - os.lstat(lock).st_mtime < STALE_LOCK_S:       # from the future: young, too
                raise _fail('internal', 'the sync clone is in use by another git process (a young lock file)')
            os.unlink(lock)
    except OSError as error:
        raise _fail('internal', f'a lock file of the sync clone cannot be removed ({type(error).__name__})') from error
    if _rebasing(clone) and clone.git('rebase', '--abort').returncode != 0:
        raise _fail('internal', 'an interrupted rebase in the sync clone could not be aborted')


# --- Visibility -------------------------------------------------------------------------------------------------------

def probe_env(directory: str) -> dict[str, str]:
    """The environment of the anonymous probe: no GIT_* of the caller, no global or system config, no prompt or
    askpass, HOME in `directory` (no .netrc, no ~/.gitconfig), messages in C, and no repository at all: GIT_DIR is
    the null device, so a `.git` put into `directory` or above it (its config could rewrite the URL) is never read."""
    env = {name: value for name, value in os.environ.items()
           if not name.startswith('GIT_') and name != 'XDG_CONFIG_HOME'}
    env.update(GIT_CONFIG_GLOBAL=NULL_CONFIG, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='',
               GCM_INTERACTIVE='never', LC_ALL='C', LANGUAGE='C', HOME=directory, GIT_DIR=os.devnull,
               GIT_CEILING_DIRECTORIES=os.path.dirname(os.path.abspath(directory)))
    return env


def judge(returncode: int, errors: str) -> str:
    """PUBLIC, PRIVATE or UNCLEAR for the exit code and error output of the probe, by VISIBILITY_TABLE."""
    for code, text, result in VISIBILITY_TABLE:
        if returncode == code and text in errors:
            return result
    return UNCLEAR


def check_visibility(repo: str, directory: str, deadline: _sync_core.Deadline | None = None,
                     execute: Execute | None = None) -> str:
    """Whether `owner/name` can be read on GitHub without logging in: PUBLIC, PRIVATE or UNCLEAR. One anonymous
    `git ls-remote` in `directory` (the sync folder) of at most VISIBILITY_TIMEOUT_S, less if `deadline` has less
    left; a timeout or a git that cannot be started is UNCLEAR. `execute` replaces _sync_core.run_command (tests). The
    installer runs it too."""
    seconds = VISIBILITY_TIMEOUT_S if deadline is None else min(VISIBILITY_TIMEOUT_S, deadline.left())
    argv = [_sync_core.GIT, '-c', 'credential.helper=', 'ls-remote', '--heads', PROBE_URL.format(repo)]
    try:
        done = (execute or _sync_core.run_command)(argv, _sync_core.Deadline(seconds), probe_env(directory), directory)
    except _sync_core.SyncError:
        return UNCLEAR
    return judge(done.returncode, done.stderr)


def cached_private(directory: str, repo: str, now: float) -> bool:
    """True if `<directory>/visibility.json` says that `repo` was found private less than 24 h before `now`. A file
    that is missing, broken, of another repository or from the future: False (check again)."""
    try:
        data = json.loads(_sync_core._read_small(os.path.join(directory, VISIBILITY_FILE)))
        checked = _sync_core._moment(data['checked'], 'checked') if isinstance(data, dict) else None
    except (OSError, ValueError, RecursionError, KeyError):
        return False
    return (checked is not None and data.get('repo') == repo and data.get('result') == PRIVATE
            and 0 <= now - checked < VISIBILITY_MAX_AGE_S)


def remember_private(directory: str, repo: str, now: float) -> None:
    """Notes that `repo` was found private at `now` (0600, atomic). A note that cannot be written is left out: the
    next run checks again."""
    text = json.dumps({'repo': repo, 'result': PRIVATE, 'checked': sync_format.iso_time(int(now))})
    try:
        _context_state.write_private(directory, VISIBILITY_FILE, text)
    except OSError:
        pass


def _ensure_private(run: _sync_core.Run, repo: str, execute: Execute | None) -> None:
    """Step 3."""
    if cached_private(run.directory, repo, run.started):
        return
    result = check_visibility(repo, run.directory, run.deadline, execute)
    if result == PUBLIC:
        raise _fail('visibility', 'the sync repo can be read without logging in: it is public, nothing is pushed '
                                  'until the installer runs again with --sync-repo')
    if result == UNCLEAR:
        raise _fail('network', 'whether the sync repo is public could not be told: nothing is pushed')
    remember_private(run.directory, repo, run.started)


# --- Fetch, fast-forward, rebase --------------------------------------------------------------------------------------

def _remote_error(done: subprocess.CompletedProcess, action: str) -> _sync_core.SyncError:
    if any(text in done.stderr for text in AUTH_TEXTS):
        return _fail('auth', f'the {action} was refused: credentials missing or not accepted')
    return _fail('network', f'the {action} did not reach the sync repo')


def _tracking(clone: Clone) -> str:
    return f'refs/remotes/{REMOTE}/{clone.branch}'


def _fetch(clone: Clone) -> None:
    done = clone.git('fetch', '--quiet', '--prune', '--no-tags', '--no-recurse-submodules', REMOTE,
                     f'+refs/heads/*:refs/remotes/{REMOTE}/*')
    if done.returncode != 0:
        raise _remote_error(done, 'fetch')


def _commit_of(clone: Clone, ref: str) -> str | None:
    done = clone.git('rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}')
    return done.stdout.strip() if done.returncode == 0 else None


def _is_ancestor(clone: Clone, older: str, newer: str) -> bool:
    done = clone.git('merge-base', '--is-ancestor', older, newer)
    if done.returncode not in (0, 1):
        raise _fail('internal', 'git merge-base failed in the sync clone')
    return done.returncode == 0


def _relation(clone: Clone) -> str:
    """The local branch against the fetched remote branch: SAME, AHEAD, BEHIND or APART. A remote without the branch
    (an empty repository) is behind any local commit."""
    remote, local = _commit_of(clone, _tracking(clone)), _commit_of(clone, 'HEAD')
    if remote == local:
        return SAME
    if remote is None or (local is not None and _is_ancestor(clone, remote, local)):
        return AHEAD
    if local is None or _is_ancestor(clone, local, remote):
        return BEHIND
    return APART


def _merge_ff(clone: Clone) -> None:
    if clone.git('merge', '--ff-only', '--quiet', _tracking(clone)).returncode != 0:
        raise _fail('conflict', 'the sync clone cannot be fast-forwarded to the remote')


def update_clone(run: _sync_core.Run, clone: Clone) -> str:
    """Brings the clone onto the remote, for the push (step 4) and the pull of the report. Fetch, then:
    same or ahead (also a remote without the branch): UP_TO_DATE; behind: fast-forward, FAST_FORWARDED (refused by
    git: `conflict`); apart with a clean work tree: the own commits rebased onto the remote, REBASED (a conflict
    aborts the rebase: `conflict`, the own commits stay); apart with changes in the work tree: nothing, DIVERGED_DIRTY
    (the push rebases after its commit, the report keeps the clone as it is). Never a merge, nothing thrown away. A
    fetch that fails: `auth` or `network`."""
    _fetch(clone)
    run.deadline.check()
    relation = _relation(clone)
    if relation == BEHIND:
        _merge_ff(clone)
        return FAST_FORWARDED
    if relation != APART:
        return UP_TO_DATE
    if _status(clone):
        return DIVERGED_DIRTY
    _rebase(clone)
    return REBASED


def _rebasing(clone: Clone) -> bool:
    return any(os.path.isdir(os.path.join(clone.path, '.git', name)) for name in ('rebase-merge', 'rebase-apply'))


def _rebase(clone: Clone) -> None:
    """The own commits onto the fetched remote. A conflict aborts the rebase (`conflict`): the clone is as before,
    with its own commits."""
    if clone.git('rebase', '--quiet', _tracking(clone)).returncode == 0:
        return
    if not _rebasing(clone):
        raise _fail('internal', 'the rebase in the sync clone could not start')
    if clone.git('rebase', '--abort').returncode != 0:
        raise _fail('internal', 'a failed rebase in the sync clone could not be aborted')
    raise _fail('conflict', 'the own commits do not fit onto the remote: the rebase was aborted, nothing is merged')


# --- The commit guard -------------------------------------------------------------------------------------------------

def parse_status(text: str) -> list[tuple[str, str]]:
    """(XY, path) of `git status --porcelain=v1 -z`; a rename or copy gives both its paths."""
    found, fields = [], text.split('\0')
    position = 0
    while position < len(fields):
        field = fields[position]
        position += 1
        if not field:
            continue
        code = field[:2]
        found.append((code, field[3:]))
        if 'R' in code or 'C' in code:          # the path it came from follows
            found.append((code, fields[position]))
            position += 1
    return found


def parse_raw(text: str) -> list[tuple[str, str, str]]:
    """(status letter, new mode, path) of `git diff --raw -z --no-renames`."""
    fields = text.split('\0')
    found = []
    for meta, path in zip(fields[0::2], fields[1::2]):
        parts = meta[1:].split(' ')
        if not meta.startswith(':') or len(parts) != 5:
            raise _fail('internal', 'git diff gave a line that is not --raw')
        found.append((parts[4][:1], parts[1], path))
    return found


def below(own: str, path: str) -> bool:
    """`path` (from git, with `/`) is a normal path inside the folder `own`: no `.`, `..` or empty part, no
    backslash."""
    return (path.startswith(own + '/') and '\\' not in path and posixpath.normpath(path) == path
            and not any(part in ('', '.', '..') for part in path.split('/')))


def request_month(own: str, path: str) -> str | None:
    """The month of a request file of the own folder (`requests/<YYYY-MM>/<session>.jsonl`), else None."""
    match = REQUEST_FILE.fullmatch(path[len(own) + 1:]) if below(own, path) else None
    return match['month'] if match else None


def _of_layout(own: str, path: str) -> bool:
    return (path in (f'{own}/machine.json', f'{own}/status.json')
            or (below(own, path) and sync_format.LAYOUT_LINES.fullmatch(path) is not None))


def check_entry(kind: str, mode: str, path: str, own: str, deletable: Callable[[str], bool],
                root: str | None = None) -> None:
    """One changed path of a commit (`kind` the status letter of `git diff --raw`, `mode` its new mode). With `root`
    an added or changed path must also be a regular file with a single link in the work tree there (as the reader
    takes them: a second link could share the file with one outside the clone). `internal` if it may not be
    committed."""
    if not below(own, path):
        raise _fail('internal', 'a changed path outside the own machine folder: nothing is committed')
    if kind == 'D':
        if not deletable(path):
            raise _fail('internal', 'a deletion that is no compacted request month: nothing is committed')
        return
    if kind not in ('A', 'M') or mode != REGULAR or not _of_layout(own, path):
        raise _fail('internal', 'a changed path that is no regular file of the layout: nothing is committed')
    if root is not None:
        try:
            info = os.lstat(os.path.join(root, *path.split('/')))
            regular = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        except OSError:
            regular = False
        if not regular:
            raise _fail('internal', 'a changed path that is no regular file of the layout: nothing is committed')


def guard(status: list[tuple[str, str]], staged: list[tuple[str, str, str]], own: str, months: set[str],
          root: str) -> None:
    """Step 6 for the paths of `git status` and the staged changes: all below `own`, nothing git did not take (an
    untracked, unstaged or unmerged path), every added or changed one a regular file of the layout, every deletion a
    request file of one of `months` (compactable before compacting). `internal` otherwise."""
    for code, path in status:
        if not below(own, path):
            raise _fail('internal', 'a changed path outside the own machine folder: nothing is committed')
        if code[1] != ' ':
            raise _fail('internal', 'a change in the own machine folder git did not take: nothing is committed')
    for kind, mode, path in staged:
        check_entry(kind, mode, path, own, lambda deleted: request_month(own, deleted) in months, root)


def _status(clone: Clone) -> list[tuple[str, str]]:
    done = clone.git('status', '--porcelain=v1', '-z', '--untracked-files=all', '--no-renames',
                     '--ignore-submodules=none')
    if done.returncode != 0:
        raise _fail('internal', 'git status failed in the sync clone')
    return parse_status(done.stdout)


def _diff(clone: Clone, *revisions: str) -> list[tuple[str, str, str]]:
    done = clone.git('diff', '--raw', '-z', '--no-renames', '--no-ext-diff', '--ignore-submodules=none', *revisions)
    if done.returncode != 0:
        raise _fail('internal', 'git diff failed in the sync clone')
    return parse_raw(done.stdout)


def _stage(clone: Clone, own: str) -> None:
    """Everything of the own folder into the index, also what a .gitignore of the repository would hide."""
    if clone.git('add', '--all', '--force', '--', own).returncode != 0:
        raise _fail('internal', 'git add failed in the sync clone')


def read_journal(directory: str, machine_id: str) -> set[str]:
    """The months of `<directory>/compacting.json` for `machine_id`: removed by a run that has not committed yet. A
    note that is missing, broken or of another machine: none."""
    try:
        data = json.loads(_sync_core._read_small(os.path.join(directory, JOURNAL_FILE)))
    except (OSError, ValueError, RecursionError):
        return set()
    if not isinstance(data, dict) or data.get('machine') != machine_id or not isinstance(data.get('months'), list):
        return set()
    return {month for month in data['months'] if isinstance(month, str) and sync_format.MONTH.fullmatch(month)}


def write_journal(directory: str, machine_id: str, months: set[str]) -> bool:
    """Notes `months` as removed and not yet committed (0600, atomic); False if it cannot be written."""
    try:
        return _context_state.write_private(directory, JOURNAL_FILE,
                                            json.dumps({'machine': machine_id, 'months': sorted(months)}))
    except OSError:
        return False


def _compact(run: _sync_core.Run, clone: Clone, machine_id: str) -> set[str]:
    """Step 5b: removes the compactable request months and returns the months whose request files may be deleted in
    the commit: these, as found before anything was removed, and those of the noted run before."""
    keep = run.cfg['sync']['keep_request_months']
    try:
        months = set(sync_format.compactable_months(clone.path, machine_id, keep, run.started))
        months |= read_journal(run.directory, machine_id)
        if months and not write_journal(run.directory, machine_id, months):
            raise _fail('internal', f'{JOURNAL_FILE} cannot be written: nothing is compacted')
        sync_format.compact(clone.path, machine_id, keep, run.started)
    except (sync_format.FormatError, OSError) as error:
        raise _fail('internal', f'the old request months cannot be compacted ({type(error).__name__})') from error
    return months


def _commit(run: _sync_core.Run, clone: Clone, machine_id: str, months: set[str]) -> bool:
    """Steps 6 and 7: True if a commit was made, False if nothing changed."""
    own = sync_format.machine_dir(machine_id)
    _stage(clone, own)
    if not _status(clone):
        _forget(run, machine_id, months)
        return False
    moment = sync_format.iso_time(int(run.started))
    try:
        sync_format.write_status(clone.path, machine_id, {'last_success': moment, 'tool_version': TOOL_VERSION,
                                                          'schema': sync_format.SCHEMA})
    except (sync_format.FormatError, OSError) as error:
        raise _fail('internal', f'status.json cannot be written ({type(error).__name__})') from error
    _stage(clone, own)
    guard(_status(clone), _diff(clone, '--cached'), own, months, clone.path)
    run.deadline.check()
    name = sync_format.display_name(machine_id, run.cfg['sync']['machine_name'])
    if clone.git('commit', '--quiet', '--no-verify', '--no-gpg-sign', '-m', f'sync: {name} {moment}').returncode:
        raise _fail('internal', 'the commit in the sync clone failed')
    _forget(run, machine_id, months)
    return True


def _forget(run: _sync_core.Run, machine_id: str, months: set[str]) -> None:
    """Clears the note of compacted months once they are committed. A note that stays allows only deletions of months
    that were found compactable."""
    if months:
        write_journal(run.directory, machine_id, set())


# --- The push ---------------------------------------------------------------------------------------------------------

def _check_outgoing(clone: Clone, own: str) -> None:
    """Every commit the push would send (those the remote lacks; all for an empty remote) one by one, not their sum:
    no merge, and each path a commit changes passes the guard (below `own`, a regular file of the layout, deletions
    only of request files). A path outside that one commit adds and a later one removes again is caught too."""
    tracking = _tracking(clone)
    revisions = f'{tracking}..HEAD' if _commit_of(clone, tracking) else 'HEAD'
    done = clone.git('log', '--format=%x01%H %P', '--raw', '-z', '--no-renames', '--no-abbrev', '--no-ext-diff',
                     revisions)
    if done.returncode != 0:
        raise _fail('internal', 'git log failed in the sync clone')
    for commit in done.stdout.split('\x01')[1:]:
        head, _, raw = commit.partition('\0')
        if len(head.split()) > 2:                   # the commit and more than one parent
            raise _fail('internal', 'a merge commit would be pushed: nothing is pushed')
        for kind, mode, path in parse_raw(raw.lstrip('\n')):
            check_entry(kind, mode, path, own, lambda deleted: request_month(own, deleted) is not None)


def _behind_refusal(output: str) -> bool:
    return any(line.startswith('!') and '[rejected]' in line and any(reason in line for reason in BEHIND_REASONS)
               for line in output.splitlines())


def _push(run: _sync_core.Run, clone: Clone, own: str) -> None:
    """Step 9: at most two attempts; between them fetch and rebase."""
    for retry in (False, True):
        _check_outgoing(clone, own)
        run.deadline.check()
        done = clone.git('push', '--porcelain', '--no-verify', REMOTE, f'HEAD:refs/heads/{clone.branch}')
        if done.returncode == 0:
            return
        if not _behind_refusal(done.stdout):
            if '[remote rejected]' in done.stdout:
                raise _fail('rejected', 'the sync repo refused the push')
            raise _remote_error(done, 'push')
        if not retry:
            run.deadline.check()
            _fetch(clone)
            _rebase(clone)
    raise _fail('rejected', 'the push was refused twice: other machines were faster')


def push(run: _sync_core.Run, projects_dir: str | None = None, execute: Execute | None = None) -> None:
    """The push of `sync.py push` (a Runner of the core): steps 2 to 9. `projects_dir` replaces ~/.claude/projects,
    `execute` the git of the visibility probe (tests). Raises SyncError with a fixed text."""
    clone = open_clone(run)
    run.deadline.check()
    _ensure_private(run, run.cfg['sync']['repo'], execute)
    run.deadline.check()
    update_clone(run, clone)
    run.deadline.check()
    try:
        machine_id = sync_format.load_machine_id(run.log_dir)
    except sync_format.FormatError as error:
        raise _fail('internal', str(error)) from error
    result = _sync_export.export(clone.path, run.log_dir, run.cfg, os.path.expanduser(projects_dir or PROJECTS_DIR),
                                 run.started, run.deadline)
    months = _compact(run, clone, machine_id)
    committed = _commit(run, clone, machine_id, months)
    run.deadline.check()
    relation = _relation(clone)
    if relation == APART:
        _rebase(clone)
    if run.mode == 'shadow':
        _sync_core.log(run.log_dir, 'shadow', 'committed, not pushed' if committed else 'nothing new, not pushed',
                       lines=sum(result.lines.values()), bytes=result.bytes)
        return
    if relation != SAME:
        _push(run, clone, sync_format.machine_dir(machine_id))
