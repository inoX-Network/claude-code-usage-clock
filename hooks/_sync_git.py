"""The git work of the sync: the push (0034-SYNC-PUSH), and the clone and the fast-forward the pull (0034-SYNC-REPORT)
shares with it. Whatever is unclear ends without a push; a repository found public is never written to (one made public
later is found within VISIBILITY_MAX_AGE_S, the age of the cache of a private result).

The clone `<log_dir>/sync/repo` is made by the installer (`--sync-repo`, `make_clone`); push and pull never clone.
Every git call in it (`Clone.git`) runs with GIT_DIR and GIT_WORK_TREE set to the clone (never a working folder, never a repository above
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
   exactly the one URL made of `sync.repo` and `sync.transport` (URLS). Then what an ended run left is cleared
   (`_heal`: old git locks and temporary objects, an interrupted rebase, half-written files outside the own folder),
   and the branch checked out must be the default branch of the remote,
   the only one pushed. Else `internal`. No attributes of the repository apply (`attr.tree` is the empty tree, and
   `.git/info/attributes` unsets them for a git that does not know attr.tree: `pin_attributes`).
3. **Visibility** (`check_visibility`), unless a result `private` for this repository is younger than 24 h
   (`<log_dir>/sync/visibility.json`): `git ls-remote` of `https://github.com/<owner>/<name>.git` without any git
   config of the user or the system, without credential helper, prompt or askpass, HOME in the sync folder (no
   .netrc), GIT_DIR on the null device (no repository put into the sync folder is read), messages in C, 15 s.
   VISIBILITY_TABLE tells exit code and error text apart: readable is public (`visibility`: the core blocks every
   push until the installer runs again), a request for a user name is private (cached), anything else is unclear
   (`network`, no push). Readable asks once more, anonymously, the REST API (`ask_api`, no redirect, at most 64 KB,
   within the same 15 s): behind a proxy that adds credentials to every request (a Claude cloud container) the
   ls-remote reads a private repository too. `private: true` of this very repository makes it private (noted with
   the reason `credentials_injected`, one log line); only an explicit `private: false` or `visibility` public or
   internal makes it public; any other answer, or none, is unclear, and its log line names why.
4. **Update** (`update_clone`, shared with the pull): fetch; behind the remote: fast-forward. A local history that
   went apart from the remote (commits of an earlier run that were not pushed: shadow mode, a failed push) is rebased
   onto it if the work tree is clean, else after the commit (step 7b). Before every fast-forward or rebase (also those
   of 7b and 9) the entries of the remote are counted and their sizes summed up, read in pieces (`_check_size`): above
   MAX_CLONE_FILES entries or `sync.max_clone_mb` `internal`, nothing is checked out.
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
   more, then `rejected`. Refused by the repository: `rejected`. Credentials missing or refused: `auth` (AUTH_TEXTS;
   a repository that does not exist or that the login cannot reach has its own text, NOT_FOUND); any other failure to
   reach it: `network`.

Nothing exported is ever thrown away: no reset, checkout, clean or stash of the own folder, also not after `conflict`
or a failed guard (the export cursor has moved on); only paths outside it are put back as HEAD has them (`_heal`).
Errors carry a fixed text, never output of git.
"""
from __future__ import annotations

import dataclasses
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from typing import NamedTuple

import _config
import _context_state
import _plat
import _sync_core
import _sync_export
import sync_format

CLONE_DIR = 'repo'
CLONE_TEMP_PREFIX = 'repo.tmp-'  # the folder make_clone clones into before it becomes CLONE_DIR
NO_HOOKS_DIR = 'no-hooks'
VISIBILITY_FILE = 'visibility.json'
JOURNAL_FILE = 'compacting.json'
VISIBILITY_MAX_AGE_S = 24 * 3600
VISIBILITY_TIMEOUT_S = 15
PROJECTS_DIR = '~/.claude/projects'
TOOL_VERSION = '1.9.0'         # `tool_version` of status.json: the release of this code
REMOTE = 'origin'
URLS = {'https': 'https://github.com/{}.git', 'ssh': 'git@github.com:{}.git'}
PROBE_URL = 'https://github.com/{}.git'
API_URL = 'https://api.github.com/repos/{}'      # the second stage of the visibility probe (0034-SYNC-PROXY-SICHT)
API_MAX_BYTES = 64 * 1024                        # a longer answer is no answer
API_PIECE = 16 * 1024                            # read in pieces: the clock is checked between them
CREDENTIALS_INJECTED = 'credentials_injected'    # the reason of a private result the API proved, in visibility.json
REASONS = (CREDENTIALS_INJECTED,)
INJECTED_NOTE = 'a proxy adds credentials to every request, the GitHub API says private'   # installer, --check
NULL_CONFIG = '/dev/null'       # no global config; Git for Windows reads this name as NUL
REGULAR = '100644'              # the only mode of a file the sync commits
MEGABYTE = 1024 * 1024          # of sync.max_clone_mb
MAX_CLONE_FILES = 100_000       # entries of the remote a fast-forward or rebase may check out (a real export: ~120 a
                                # machine and month); a git bomb of repeated subtrees has size 0 but millions of files
MAX_ENTRY_BYTES = 8192          # one entry of `ls-tree -r -l -z`: no path of a file system is that long
DEFAULT_BRANCH = 'main'         # the branch pushed to if the clone does not know the default branch of the remote
EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'      # SHA-1 of the empty tree: attributes from nowhere
STALE_LOCK_S = _sync_core.running_window_s()  # a git lock in the clone older than any run of the sync is left over
IDENTITY = {'GIT_AUTHOR_NAME': 'usage-clock', 'GIT_AUTHOR_EMAIL': 'usage-clock@sync.invalid',
            'GIT_COMMITTER_NAME': 'usage-clock', 'GIT_COMMITTER_EMAIL': 'usage-clock@sync.invalid'}
# Variables of the caller that would point git at another repository, index, attribute source or time, or start an
# editor
FOREIGN_ENV = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
               'GIT_COMMON_DIR', 'GIT_NAMESPACE', 'GIT_CEILING_DIRECTORIES', 'GIT_AUTHOR_DATE', 'GIT_COMMITTER_DATE',
               'GIT_SEQUENCE_EDITOR', 'GIT_ATTR_SOURCE', 'GIT_SSL_NO_VERIFY', 'GIT_SSL_CAINFO', 'GIT_SSL_CAPATH',
               'GIT_PROXY_COMMAND', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT')
FOREIGN_ENV_PREFIXES = ('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')       # the pairs of GIT_CONFIG_COUNT
TEMP_OBJECT_PREFIXES = ('tmp_pack_', 'tmp_idx_', 'tmp_obj_')         # what a fetch git ended leaves in objects/
NO_REMOTE_BRANCH = "couldn't find remote ref"
# attr.tree and core.attributesFile: no .gitattributes of another machine (nor a global one) decides how the own files
# are stored or checked out (filters such as git-lfs, working-tree-encoding, eol). core.autocrlf: nor does the config
# of the user (Git for Windows sets it to true in its system config, which checks LF files out with CRLF).
SETTINGS = ('core.fsmonitor=false', 'core.autocrlf=false', 'commit.gpgSign=false', 'push.gpgSign=false', 'gc.auto=0',
            'maintenance.auto=false', 'submodule.recurse=false', 'fetch.recurseSubmodules=false',
            'push.recurseSubmodules=no', 'rebase.autoStash=false', 'rebase.updateRefs=false',
            'merge.verifySignatures=false', 'push.followTags=false', f'attr.tree={EMPTY_TREE}',
            'core.attributesFile=', 'color.ui=false', 'log.showRoot=true', 'log.showSignature=false',
            'fetch.writeCommitGraph=false', 'transfer.fsckObjects=true', 'http.sslVerify=true')
# .git/info/attributes of the clone: every attribute that converts on checkout or runs a driver, unset for every path.
# It has the highest precedence of all attribute files in every git version (gitattributes(5), unchanged from 1.8 to
# 2.56), above every .gitattributes in the tree, also one in a sub folder or behind a macro, so it still holds where
# attr.tree is unknown (git before 2.43 ignores an unknown -c). One line without a line end: written in text mode,
# Windows would add a CR to it.
INFO_ATTRIBUTES = '* -text -eol -filter -diff -merge -ident -working-tree-encoding'
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
# Of those, the answer of GitHub to a repository that does not exist or that the login cannot reach (https:
# `remote: Repository not found.`, ssh: `ERROR: Repository not found.`): still `auth`, but its text names sync.repo.
NOT_FOUND_TEXT = 'Repository not found'
NOT_FOUND = 'the repository does not exist or this login cannot reach it (check sync.repo)'
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
    """The clone of a run: its folder and branch; `git` runs git in it within the deadline of the run. `max_mb` is
    `sync.max_clone_mb`, the most a fast-forward or rebase may check out (`_check_size`)."""
    path: str
    branch: str
    deadline: _sync_core.Deadline = dataclasses.field(repr=False)
    env: dict = dataclasses.field(repr=False)
    options: tuple = dataclasses.field(repr=False)
    max_mb: int = dataclasses.field(repr=False)

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return _sync_core.run_command([_sync_core.GIT, *self.options, *args], self.deadline, self.env, self.path)

    def scan(self, take: Callable[[bytes], bool], *args: str) -> int | None:
        """git `args` with its output handed to `take` piece by piece (`_sync_core.scan_command`)."""
        return _sync_core.scan_command([_sync_core.GIT, *self.options, *args], self.deadline, take, self.env,
                                       self.path)


def _empty_hooks(directory: str) -> str:
    """`<directory>/no-hooks`, an empty folder (made if missing): the core.hooksPath of every git call in the clone."""
    path = os.path.join(directory, NO_HOOKS_DIR)
    try:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        if _plat.is_folder(os.lstat(path)) and not os.listdir(path):
            return path
    except OSError as error:
        raise _fail('internal', f'{NO_HOOKS_DIR} cannot be made ({type(error).__name__})') from error
    raise _fail('internal', f'{NO_HOOKS_DIR} in the sync folder is no empty folder')


def clone_at(path: str, deadline: _sync_core.Deadline, max_mb: int, hooks: str | None = None) -> Clone:
    """The Clone of the folder `path`, nothing checked, no branch yet: git with SETTINGS and the environment of the
    clone, core.hooksPath `hooks` if given (`--check` of the installer runs no command that starts a hook and writes no
    hook folder)."""
    settings = (f'core.hooksPath={hooks}',) if hooks else ()
    options = tuple(arg for setting in (*settings, *SETTINGS) for arg in ('-c', setting))
    return Clone(path, '', deadline, _clone_env(path, deadline), options, max_mb)


def pin_attributes(path: str) -> None:
    """`<path>/.git/info/attributes` is INFO_ATTRIBUTES: written (atomically, never through a link or a junction) if it
    is missing or holds anything else. Only ever switching conversions off, it may be put back over whatever is there;
    an `internal` instead would stop the sync until someone acts. `internal` if it cannot be written."""
    folder = os.path.join(path, '.git', 'info')
    try:
        if not _plat.is_folder(os.lstat(folder)):      # a junction on Windows: write_private refuses only links
            raise _fail('internal', 'the info folder of the sync clone is no folder')
    except FileNotFoundError:      # made by write_private below
        pass
    except OSError as error:
        raise _fail('internal', f'the info folder of the sync clone cannot be read ({type(error).__name__})') from error
    try:
        if _sync_core._read_small(os.path.join(folder, 'attributes')) == INFO_ATTRIBUTES.encode():
            return
    except (OSError, ValueError):      # missing, or no regular file: written below
        pass
    try:
        written = _context_state.write_private(folder, 'attributes', INFO_ATTRIBUTES)
    except OSError:
        written = False
    if not written:
        raise _fail('internal', 'the attributes of the sync clone cannot be written')


def remote_matches(clone: Clone, url: str) -> bool:
    """`git remote get-url --all origin` and `--push --all` (insteadOf and pushInsteadOf applied) give exactly `url`."""
    for which in ((), ('--push',)):
        done = clone.git('remote', 'get-url', '--all', *which, REMOTE)
        if done.returncode != 0 or done.stdout.splitlines() != [url]:
            return False
    return True


def make_clone(directory: str, url: str, max_mb: int, deadline: _sync_core.Deadline) -> None:
    """For the installer (`--sync-repo`), which holds the sync lock and found the repository private: clones `url` into
    `<directory>/repo`, which must not exist. Into a temporary folder beside it first (CLONE_TEMP_PREFIX, 0700), with
    the settings and the environment of every git call of the sync (no prompt, no askpass, no hooks, core.autocrlf
    false: Git for Windows sets it to true in its system config, and a plain clone checks LF files out with CRLF), only
    the default branch, no checkout; then its remote URL (`remote_matches`), the size check of a fast-forward
    (`_check_size`), the checkout and the rename. Fails with SyncError (fixed text: `auth`, `network`, `internal`), and the temporary folder is removed: a
    clone left without its checkout would be checked out by the next push (`_restore_foreign`), past the limit."""
    temporary = tempfile.mkdtemp(prefix=CLONE_TEMP_PREFIX, dir=directory)
    try:
        clone = clone_at(temporary, deadline, max_mb, _empty_hooks(directory))
        env = {name: value for name, value in clone.env.items() if name not in ('GIT_DIR', 'GIT_WORK_TREE')}
        done = _sync_core.run_command([_sync_core.GIT, *clone.options, 'clone', '--quiet', '--single-branch',
                                       '--no-checkout', '--no-tags', '--', url, temporary], deadline, env, directory)
        if done.returncode != 0:
            raise _remote_error(done, 'clone')      # GIT_DIR of a clone that is to be made would break `git clone`
        if not remote_matches(clone, url):      # as open_clone checks it on every push; before anything is checked out
            raise _fail('internal', 'the remote of the new clone is not the URL of sync.repo (an insteadOf or '
                                    'pushInsteadOf of the git config rewrites it): nothing was kept')
        done = clone.git('symbolic-ref', '--quiet', '--short', 'HEAD')
        branch = done.stdout.strip()
        if done.returncode != 0 or not BRANCH.fullmatch(branch):
            raise _fail('internal', 'the clone has no branch checked out')
        clone = dataclasses.replace(clone, branch=branch)
        pin_attributes(temporary)
        if _commit_of(clone, _tracking(clone)):         # an empty repository has nothing to check out
            _check_size(clone)
            if clone.git('checkout', '--quiet', branch).returncode != 0:
                raise _fail('internal', 'the sync repository could not be checked out')
        os.rename(temporary, os.path.join(directory, CLONE_DIR))
    except BaseException:
        try:
            _remove_tree(temporary)
        except OSError:      # the reason the clone failed says more; --check names the folder left over
            pass
        raise


def _remove_tree(path: str) -> None:
    """Removes a folder make_clone made. Git makes its object files read-only, which Windows refuses to delete: only
    such a file (Windows, a refused unlink, a regular file and no link) is made writable and removed again; every other
    failure is raised, and nothing is changed through a link or in a folder."""
    def writable(function, name, error) -> None:
        error = error[1] if isinstance(error, tuple) else error        # onerror hands over sys.exc_info()
        if (os.name != 'nt' or function not in (os.unlink, os.remove) or not isinstance(error, PermissionError)
                or not stat.S_ISREG(os.lstat(name).st_mode)):
            raise error
        os.chmod(name, stat.S_IWRITE)
        function(name)
    shutil.rmtree(path, **{'onexc' if sys.version_info >= (3, 12) else 'onerror': writable})


def _clone_env(path: str, deadline: _sync_core.Deadline) -> dict[str, str]:
    base = {name: value for name, value in os.environ.items()
            if name not in FOREIGN_ENV and not name.startswith(FOREIGN_ENV_PREFIXES)}
    base.update(GIT_DIR=os.path.join(path, '.git'), GIT_WORK_TREE=path)
    env = _sync_core.git_env(base, deadline)      # with GIT_DIR it reads the core.sshCommand of the clone
    env.update(IDENTITY, LC_ALL='C', LANGUAGE='C', GIT_EDITOR=':',     # `:` is git's editor that does nothing
               GIT_ATTR_NOSYSTEM='1')
    if env.get('GIT_SSH_COMMAND'):      # not with a GIT_SSH program (see git_env); ssh takes the first of an option
        env['GIT_SSH_COMMAND'] += ' -o StrictHostKeyChecking=yes'
    return env


def open_clone(run: _sync_core.Run) -> Clone:
    """Step 2: the clone of `sync.repo`, checked. `internal` (fixed text) if sync.repo is empty or no owner/name, the
    clone is missing, a link or no git folder, no branch is checked out, or the fetch or push URL of `origin` is not
    exactly the one of sync.repo and sync.transport. Never clones."""
    repo = configured_repo(run.cfg)
    path = clone_dir(run.log_dir)
    try:
        if not all(_plat.is_folder(os.lstat(folder)) for folder in (path, os.path.join(path, '.git'))):
            raise _fail('internal', 'the sync clone is no git folder')      # a link or a file (a .git file, too)
    except FileNotFoundError:
        raise _fail('internal', 'the sync clone is missing, run the installer with --sync-repo') from None
    except OSError as error:
        raise _fail('internal', f'the sync clone cannot be read ({type(error).__name__})') from error
    clone = clone_at(path, run.deadline, run.cfg['sync']['max_clone_mb'], _empty_hooks(run.directory))
    if not remote_matches(clone, remote_url(repo, run.cfg['sync']['transport'])):
        raise _fail('internal', 'the remote of the sync clone is not sync.repo')
    pin_attributes(path)      # before _heal: its checkout of foreign paths takes no attribute of the tree either
    try:
        machine_id = sync_format.load_machine_id(run.log_dir, create=False)
    except sync_format.FormatError as error:
        raise _fail('internal', str(error)) from error
    _heal(clone, sync_format.machine_dir(machine_id) if machine_id else None)
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


def _left_over(git_dir: str) -> tuple[list[str], list[str]]:
    """Below `git_dir`: the lock files of git (index, refs, packed-refs, HEAD, config …) outside `objects/`, and the
    temporary files of a fetch git ended inside it (TEMP_OBJECT_PREFIXES)."""
    def real_folder(path: str) -> bool:
        try:
            return _plat.is_folder(os.lstat(path))
        except OSError:
            return False
    objects, locks, temporary = os.path.join(git_dir, 'objects'), [], []
    for folder, dirs, names in os.walk(git_dir):
        dirs[:] = [name for name in dirs if real_folder(os.path.join(folder, name))]      # os.walk enters junctions
        if folder == objects or folder.startswith(objects + os.sep):
            temporary += [os.path.join(folder, name) for name in names if name.startswith(TEMP_OBJECT_PREFIXES)]
        else:
            locks += [os.path.join(folder, name) for name in names if name.endswith('.lock')]
    return sorted(locks), sorted(temporary)


def _heal(clone: Clone, own: str | None) -> None:
    """What a run the watchdog or a shutdown ended left in the clone. The caller holds the sync lock, so no other run
    works in it: a git lock older than STALE_LOCK_S is left over and removed; a younger one belongs to a git someone
    else started (`internal`, try later). Temporary objects of an ended fetch older than that go too (regular files
    only, never through a link). An interrupted rebase is aborted: the branch is back on its own commits. Last, every
    path outside the own folder is put back as HEAD has it (`_restore_foreign`)."""
    git_dir = os.path.join(clone.path, '.git')
    locks, temporary = _left_over(git_dir)
    try:
        for lock in locks:
            if time.time() - os.lstat(lock).st_mtime < STALE_LOCK_S:       # from the future: young, too
                raise _fail('internal', 'the sync clone is in use by another git process (a young lock file)')
            os.unlink(lock)
        for path in temporary:
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and time.time() - info.st_mtime >= STALE_LOCK_S:
                os.unlink(path)
    except OSError as error:
        raise _fail('internal', f'a left-over file of the sync clone cannot be removed ({type(error).__name__})') \
            from error
    if _rebasing(clone) and clone.git('rebase', '--abort').returncode != 0:
        raise _fail('internal', 'an interrupted rebase in the sync clone could not be aborted')
    _restore_foreign(clone, own)


def _restore_foreign(clone: Clone, own: str | None) -> None:
    """Paths outside the own folder (`own`; None before the first export: every path) as HEAD has them: an ended
    fast-forward or checkout leaves them half written, and the next fast-forward would refuse. Changed or deleted files
    come back from HEAD, added ones leave the index, and files git does not know are removed. Nothing below the own
    folder is touched: that is exported data."""
    foreign = [(code, path) for code, path in _status(clone) if own is None or not below(own, path)]
    if not foreign:
        return
    added = [path for code, path in foreign if 'A' in code]
    known = [path for code, path in foreign if code != '??' and 'A' not in code]
    for paths, command in ((added, ('rm', '--cached', '--quiet')), (known, ('checkout', '--quiet', 'HEAD'))):
        if paths and not _with_paths(clone, paths, command):
            raise _fail('internal', 'the files of other machines in the sync clone cannot be put back')
    for path in [path for code, path in foreign if code == '??'] + added:
        if path.endswith('/') or posixpath.normpath(path) != path or path.startswith(('/', '../')):
            raise _fail('internal', 'an unknown folder or path outside the own machine folder of the sync clone')
        try:
            os.unlink(os.path.join(clone.path, *path.split('/')))
        except FileNotFoundError:
            pass
        except OSError as error:
            raise _fail('internal', f'a file of another machine cannot be removed ({type(error).__name__})') from error


def _with_paths(clone: Clone, paths: list[str], command: tuple[str, ...]) -> bool:
    """git `command` for `paths`, taken literally (no glob of a foreign file name) from a file in `.git` (no command
    line limit, written atomically, never through a link). True if it worked."""
    name = 'sync-paths'
    try:
        _context_state.write_private(os.path.join(clone.path, '.git'), name, '\0'.join(paths) + '\0')
    except OSError:
        return False
    return clone.git('--literal-pathspecs', *command, f'--pathspec-from-file=.git/{name}',
                     '--pathspec-file-nul').returncode == 0


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


class Visibility(NamedTuple):
    """The result of the visibility probe and why: `reason` is CREDENTIALS_INJECTED for a private result the API
    proved behind a proxy that adds credentials, why the API gave no answer for an UNCLEAR one it was asked for
    (`timeout`, `http_301`, `name_mismatch` ...), else empty."""
    result: str
    reason: str = ''


def api_opener():
    """The opener of the API request: the proxy of the environment (HTTPS_PROXY), no handler that sends credentials
    or cookies, and no redirect followed (a 3xx ends as HTTPError). urllib is loaded only here: the hooks that import
    this module never need it."""
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    return urllib.request.build_opener(NoRedirect)


def ask_api(repo: str, seconds: float, opener=None,
            clock: Callable[[], float] = time.monotonic) -> tuple[bytes | None, str]:
    """(body, '') of one anonymous `GET API_URL` (no Authorization header, no redirect) if GitHub answered 200 with at
    most API_MAX_BYTES within `seconds` in all; else (None, why): `timeout`, `network`, `http_<status>` (also every
    3xx: no redirect is followed), `status_<status>` (another 2xx) or `too_large`. urllib's timeout holds for each
    operation of the socket, so a proxy or server that trickles its answer to CONNECT, the TLS handshake or the head
    stretches it without end: the request runs in a daemon thread, and after `seconds` the answer is `timeout`,
    whatever that thread still waits for. It holds no lock and writes nothing; it ends with its socket, at the latest
    with the process (every caller is a short command: a hook, the installer, sync.py, the Settings tab). An error the
    request does not know reaches the caller as before. `opener` replaces api_opener() (tests)."""
    import threading
    outcome: list = []

    def request() -> None:
        try:
            outcome.append(_request_api(repo, seconds, opener, clock))
        except Exception as error:      # handed on to the caller below
            outcome.append(error)
    worker = threading.Thread(target=request, name='usage-clock-api', daemon=True)
    worker.start()
    worker.join(seconds)
    if not outcome:
        return None, 'timeout'
    if isinstance(outcome[0], Exception):
        raise outcome[0]
    return outcome[0]


def _request_api(repo: str, seconds: float, opener, clock: Callable[[], float]) -> tuple[bytes | None, str]:
    """The request of ask_api. read(n) waits for all n bytes: the body is read with read1 (what has arrived) and the
    clock is checked after each piece."""
    import http.client
    import socket
    import urllib.error
    import urllib.request
    end = clock() + seconds
    request = urllib.request.Request(API_URL.format(repo), headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': f'claude-code-usage-clock/{TOOL_VERSION}'})
    try:
        with (opener or api_opener()).open(request, timeout=seconds) as response:
            if response.status != 200:
                return None, f'status_{response.status}'
            body = b''
            while len(body) <= API_MAX_BYTES:
                piece = response.read1(min(API_PIECE, API_MAX_BYTES + 1 - len(body)))     # read(n) waits for n
                if not piece:
                    break
                body += piece
                if clock() > end:
                    return None, 'timeout'
    except urllib.error.HTTPError as error:
        return None, f'http_{error.code}'
    except urllib.error.URLError as error:
        return None, 'timeout' if isinstance(error.reason, (socket.timeout, TimeoutError)) else 'network'
    except (TimeoutError, socket.timeout):
        return None, 'timeout'
    except (OSError, ValueError, http.client.HTTPException):
        return None, 'network'
    return (body, '') if len(body) <= API_MAX_BYTES else (None, 'too_large')


def judge_api(repo: str, body: bytes) -> Visibility:
    """The answer of the API about `repo` (0034-SYNC-PROXY-SICHT v2): PUBLIC only if it says so (`private` the JSON
    false, or `visibility` public or internal); PRIVATE with CREDENTIALS_INJECTED for `private` the JSON true with
    `visibility` missing or "private"; both only for `full_name` this repository (in any case). Anything else is
    UNCLEAR with the reason: `no_json`, `not_an_object`, `name_mismatch`, `no_private`, `visibility_other`."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):        # no JSON, no UTF-8, nested too deep
        return Visibility(UNCLEAR, 'no_json')
    if not isinstance(data, dict):
        return Visibility(UNCLEAR, 'not_an_object')
    name = data.get('full_name')
    if not isinstance(name, str) or name.lower() != repo.lower():
        return Visibility(UNCLEAR, 'name_mismatch')
    private, visibility = data.get('private'), data.get('visibility', 'private')
    if private is False or visibility in ('public', 'internal'):
        return Visibility(PUBLIC)
    if private is not True:
        return Visibility(UNCLEAR, 'no_private')
    if visibility != 'private':
        return Visibility(UNCLEAR, 'visibility_other')
    return Visibility(PRIVATE, CREDENTIALS_INJECTED)


def probe_visibility(repo: str, directory: str, deadline: _sync_core.Deadline | None = None,
                     execute: Execute | None = None,
                     api: Callable[[str, float], tuple[bytes | None, str]] | None = None) -> Visibility:
    """Whether `owner/name` can be read on GitHub without logging in: PUBLIC, PRIVATE or UNCLEAR, with the reason.
    One anonymous `git ls-remote` in `directory` (the sync folder); a timeout or a git that cannot be started is
    UNCLEAR. Only if it could read the repository, one anonymous request to the API (`api`, default ask_api): behind
    a proxy that adds credentials to every request (a Claude cloud container) the ls-remote reads a private repository
    too, and only the API tells them apart (judge_api). PUBLIC blocks every push until the installer runs again, so
    only an explicit answer gives it; no answer, one that cannot be read or no time left is UNCLEAR, which pushes
    nothing either but asks again next time. Both together take at most VISIBILITY_TIMEOUT_S, less if `deadline` has
    less left. `execute` replaces _sync_core.run_command (tests). The installer runs it too."""
    budget = _sync_core.Deadline(VISIBILITY_TIMEOUT_S if deadline is None
                                 else min(VISIBILITY_TIMEOUT_S, deadline.left()))
    argv = [_sync_core.GIT, '-c', 'credential.helper=', 'ls-remote', '--heads', PROBE_URL.format(repo)]
    try:
        done = (execute or _sync_core.run_command)(argv, budget, probe_env(directory), directory)
    except _sync_core.SyncError:
        return Visibility(UNCLEAR)
    result = judge(done.returncode, done.stderr)
    if result != PUBLIC:
        return Visibility(result)
    left = budget.left()
    if left <= 0:
        return Visibility(UNCLEAR, 'no_time')
    body, why = (api or ask_api)(repo, left)
    return Visibility(UNCLEAR, why) if body is None else judge_api(repo, body)


def check_visibility(repo: str, directory: str, deadline: _sync_core.Deadline | None = None,
                     execute: Execute | None = None,
                     api: Callable[[str, float], tuple[bytes | None, str]] | None = None) -> str:
    """The result of probe_visibility alone (the Settings tab)."""
    return probe_visibility(repo, directory, deadline, execute, api).result


def check_access(repo: str, transport: str, directory: str, deadline: _sync_core.Deadline | None = None,
                 execute: Execute | None = None) -> str:
    """Whether this login reaches `owner/name` (the Settings tab checks a repository before it saves it, and writes
    nothing): `reachable`, `not_found_or_no_access` (GitHub says not found, or git has no credentials for it) or
    `unclear`. One `git ls-remote` with the credentials of the user, without a prompt (git_env), outside any repository
    (GIT_DIR is the null device), in `directory`, at most VISIBILITY_TIMEOUT_S. `execute` replaces run_command (tests)."""
    seconds = VISIBILITY_TIMEOUT_S if deadline is None else min(VISIBILITY_TIMEOUT_S, deadline.left())
    base = {name: value for name, value in os.environ.items()
            if name not in FOREIGN_ENV and not name.startswith(FOREIGN_ENV_PREFIXES)}
    env = _sync_core.git_env(dict(base, GIT_DIR=os.devnull, LC_ALL='C', LANGUAGE='C'), deadline)
    argv = [_sync_core.GIT, 'ls-remote', '--heads', remote_url(repo, transport)]
    try:
        done = (execute or _sync_core.run_command)(argv, _sync_core.Deadline(seconds), env, directory)
    except _sync_core.SyncError:
        return 'unclear'
    if done.returncode == 0:
        return 'reachable'
    if any(text in done.stderr for text in AUTH_TEXTS):      # Repository not found is one of them
        return 'not_found_or_no_access'
    return 'unclear'


def private_checked(directory: str, repo: str) -> float | None:
    """When `<directory>/visibility.json` says `repo` was found private (Unix seconds); None for a file that is
    missing, broken or of another repository. `--check` of the installer shows its age."""
    note = _private_note(directory, repo)
    return note[0] if note else None


def _private_note(directory: str, repo: str) -> tuple[float, object] | None:
    """(when, reason as written) of `<directory>/visibility.json` if it says `repo` was found private; None for a file
    that is missing, broken or of another repository."""
    try:
        data = json.loads(_sync_core._read_small(os.path.join(directory, VISIBILITY_FILE)))
        checked = _sync_core._moment(data['checked'], 'checked') if isinstance(data, dict) else None
    except (OSError, ValueError, RecursionError, KeyError):
        return None
    if checked is None or data.get('repo') != repo or data.get('result') != PRIVATE:
        return None
    return checked, data.get('reason')


def cached_private(directory: str, repo: str, now: float) -> bool:
    """True if `<directory>/visibility.json` says that `repo` was found private less than 24 h before `now`. A file
    that is missing, broken, of another repository or from the future: False (check again)."""
    checked = private_checked(directory, repo)
    return checked is not None and 0 <= now - checked < VISIBILITY_MAX_AGE_S


def private_reason(directory: str, repo: str) -> str:
    """The reason of a valid private note of `repo` in `<directory>/visibility.json` if it is one of REASONS, else
    empty. `--check` of the installer shows it."""
    note = _private_note(directory, repo)
    reason = note[1] if note else None
    return reason if isinstance(reason, str) and reason in REASONS else ''


def remember_private(directory: str, repo: str, now: float, reason: str = '') -> None:
    """Notes that `repo` was found private at `now` (0600, atomic), with `reason` if it is one of REASONS. A note that
    cannot be written is left out: the next run checks again."""
    note = {'repo': repo, 'result': PRIVATE, 'checked': sync_format.iso_time(int(now))}
    if reason in REASONS:
        note['reason'] = reason
    text = json.dumps(note)
    try:
        _context_state.write_private(directory, VISIBILITY_FILE, text)
    except OSError:
        pass


def _ensure_private(run: _sync_core.Run, repo: str, execute: Execute | None) -> None:
    """Step 3."""
    if cached_private(run.directory, repo, run.started):
        return
    result, reason = probe_visibility(repo, run.directory, run.deadline, execute)
    if result == PUBLIC:
        raise _fail('visibility', 'the sync repo can be read without logging in: it is public, nothing is pushed '
                                  'until the installer runs again with --sync-repo')
    if result == UNCLEAR:
        why = f' (GitHub API: {reason})' if reason else ''
        raise _fail('network', f'whether the sync repo is public could not be told{why}: nothing is pushed')
    if reason == CREDENTIALS_INJECTED:
        note_injected(run.log_dir)
    remember_private(run.directory, repo, run.started, reason)


def note_injected(log_dir: str) -> None:
    """The one log line of a visibility probe that found credentials added by a proxy."""
    _sync_core.log(log_dir, CREDENTIALS_INJECTED, 'the anonymous probe could read the sync repo because a proxy adds '
                                                  'credentials; the GitHub API says it is private')


# --- Fetch, fast-forward, rebase --------------------------------------------------------------------------------------

def _remote_error(done: subprocess.CompletedProcess, action: str) -> _sync_core.SyncError:
    if any(text in done.stderr for text in AUTH_TEXTS):
        reason = NOT_FOUND if NOT_FOUND_TEXT in done.stderr else 'credentials missing or not accepted'
        return _fail('auth', f'the {action} was refused: {reason}')
    return _fail('network', f'the {action} did not reach the sync repo')


def _tracking(clone: Clone) -> str:
    return f'refs/remotes/{REMOTE}/{clone.branch}'


def _fetch(clone: Clone) -> None:
    """Only the default branch (other branches of the repository are never fetched). A remote without it (an empty
    repository, a deleted branch): no remote-tracking branch."""
    done = clone.git('fetch', '--quiet', '--no-tags', '--no-recurse-submodules', REMOTE,
                     f'+refs/heads/{clone.branch}:{_tracking(clone)}')
    if done.returncode == 0:
        return
    if done.returncode != 128 or NO_REMOTE_BRANCH not in done.stderr:
        raise _remote_error(done, 'fetch')
    if clone.git('update-ref', '-d', _tracking(clone)).returncode != 0:
        raise _fail('internal', 'the remote-tracking branch of the sync clone cannot be removed')


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


class _Tally:
    """The entries of `git ls-tree -r -l -z` as they come, in pieces: their number (blobs and submodules) and the bytes
    of the blobs (a submodule has no size). `take` says whether to read on; `over` names the limit passed. Only the
    unfinished end of the last piece is kept."""

    def __init__(self, max_mb: int):
        self.max_mb, self.files, self.bytes, self.rest, self.over = max_mb, 0, 0, b'', ''

    def take(self, chunk: bytes) -> bool:
        *entries, self.rest = (self.rest + chunk).split(b'\0')
        if len(self.rest) > MAX_ENTRY_BYTES or any(len(entry) > MAX_ENTRY_BYTES for entry in entries):
            self.over = f'the sync repository has an entry of more than {MAX_ENTRY_BYTES} bytes: nothing was checked out'
            return False
        for entry in entries:
            _mode, kind, _object, size = entry.partition(b'\t')[0].split()
            self.files += 1
            if kind == b'blob':
                self.bytes += int(size)
        if self.files > MAX_CLONE_FILES:
            self.over = f'the sync repository has more than {MAX_CLONE_FILES} files: nothing was checked out'
        elif self.bytes > self.max_mb * MEGABYTE:
            self.over = (f'the sync repository is larger than sync.max_clone_mb ({self.max_mb} MB): nothing was '
                         'checked out; raise the limit if this size is expected')
        return not self.over


def _check_size(clone: Clone) -> None:
    """Before a fast-forward or rebase: the fetched remote has at most MAX_CLONE_FILES entries (`git ls-tree -r`,
    blobs and submodules) and its blobs together at most `sync.max_clone_mb` MB of 1024 KiB. More, an entry longer
    than MAX_ENTRY_BYTES, or a listing that cannot be read: `internal`, nothing is checked out. The listing is read in
    pieces and ended as soon as a limit is passed: neither a small fetch that unpacks to gigabytes (a zip bomb) nor
    one of repeated subtrees with millions of files (a git bomb) reaches the work tree or the memory."""
    tally = _Tally(clone.max_mb)
    code = clone.scan(tally.take, 'ls-tree', '-r', '-l', '-z', _tracking(clone))
    if tally.over:
        raise _fail('internal', tally.over)
    if code != 0:
        raise _fail('internal', 'the size of the sync repository could not be read: nothing was checked out')


def _merge_ff(clone: Clone) -> None:
    _check_size(clone)
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
    """A rebase stands in the clone. Its state must be a real folder: `rebase --abort` would empty the target of a
    link (`internal`, nothing is aborted)."""
    found = False
    for name in ('rebase-merge', 'rebase-apply'):
        try:
            info = os.lstat(os.path.join(clone.path, '.git', name))
        except FileNotFoundError:
            continue
        if not _plat.is_folder(info):
            raise _fail('internal', 'the rebase state of the sync clone is no folder: nothing is aborted')
        found = True
    return found


def _rebase(clone: Clone) -> None:
    """The own commits onto the fetched remote, if it is not too large (`_check_size`). A conflict aborts the rebase
    (`conflict`): the clone is as before, with its own commits."""
    _check_size(clone)
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
                     revisions, '--')
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
    machine_id = sync_format.load_machine_id(run.log_dir)      # open_clone refused an id that cannot be used
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
