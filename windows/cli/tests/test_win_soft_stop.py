"""The soft stop on Windows: which spellings of a checkpoint path count, which never do, and the real hook.

The main suite (tests/test_soft_stop.py) runs here too, with C:/srv/proj paths; this file adds what only Windows has.
"""
import copy
import io
import json
import os
import stat
import subprocess
import sys
import _winapi
from types import SimpleNamespace

import pytest

import _config
import _usage
import _win
import soft_stop
from conftest import ROOT

sys.path.insert(0, os.path.join(ROOT, 'tests'))
from test_soft_stop import (ENFORCE, PROJ, call, copy_hooks, forgeries, is_deny, log_lines, run, run_raw,  # noqa: E402
                            write_usage)

SUFFIXES = ['/agent-checkpoints', '/work/notes']
FILE_TOOLS = ('Write', 'Edit', 'MultiEdit')
APPENDS = ("echo 'x' >> {}", "echo 'x' | tee -a {}", "cat >> {} <<'EOF'\n- item done\nEOF", "printf '%s\\n' 'x' >> {}")


def back(path):
    return path.replace('/', '\\')


def results(path, suffix):
    return [soft_stop.is_checkpoint_write(tool, {'file_path': path}, suffix) for tool in FILE_TOOLS]


def at_deny_level(tool, tool_input, suffix):
    cfg = copy.deepcopy(_config.DEFAULTS)
    cfg['checkpoint_dir_suffix'] = suffix
    return soft_stop.decide(_usage.Usage(96, 20, 1.0, max_age_min=15.0), tool, tool_input, 0, False, cfg).action


def spellings(suffix):
    """One checkpoint file in every spelling that hook input brings on Windows."""
    return [f'C:/srv/proj{suffix}/x.md',
            back(f'C:/srv/proj{suffix}/x.md'),             # backslashes, as the file tools send them
            back(f'c:/srv/proj{suffix}/x.md'),             # a small drive letter
            'C:\\srv/proj' + back(suffix) + '/x.md',       # mixed separators
            f'/c/srv/proj{suffix}/x.md',                   # Git Bash; Claude Code reads it as C:\ too
            '\\\\?\\' + back(f'C:/srv/proj{suffix}/x.md'),  # the long-path prefix
            back(f'C:/srv//proj{suffix}//x.md')]           # doubled separators


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_every_windows_spelling_of_a_checkpoint_path_counts(suffix):
    for path in spellings(suffix):
        assert results(path, suffix) == [True, True, True], path
        assert at_deny_level('Write', {'file_path': path}, suffix) == 'allow', path


def refused(suffix):
    tail = suffix.lstrip('/')
    inside = back(f'C:/srv/proj{suffix}')
    return [
        inside + '\\..\\..\\src\\app.py',                   # '..' with backslashes
        inside + '\\..' + back(suffix) + '\\x.md',          # a detour through '..' that ends inside again
        inside + '\\.\\x.md',                               # '.' is a part that ends with a dot as well
        'C:\\.' + back(suffix) + '\\x.md',                  # ... also as the first part after the drive
        inside + '\\x.md:hidden',                           # an alternate data stream of a file inside
        inside + ':x',                                      # ... of the directory itself
        back(f'C:/srv/proj/src/app.py:x{suffix}/x.md'),     # a stream name that looks like the directory
        inside + '\\x.md.',                                 # Win32 drops a trailing dot ...
        inside + '\\x.md ',                                 # ... and a trailing space when it opens a file
        back(f'C:/srv/proj{suffix}/.. {suffix}/x.md'),      # '.. ' becomes '..' there
        back(f'C:/srv/proj. {suffix}/x.md'),
        back(f'//server/share/proj{suffix}/x.md'),          # UNC: a network path
        f'//server/share/proj{suffix}/x.md',
        '\\\\?\\UNC\\server\\share' + back(f'/proj{suffix}/x.md'),
        '\\\\.\\' + back(f'C:/srv/proj{suffix}/x.md'),      # the device namespace
        back(f'/srv/proj{suffix}/x.md'),                    # root-relative: on whichever drive is current
        f'/srv/proj{suffix}/x.md',
        f'/cygdrive/c/srv/proj{suffix}/x.md',               # no drive spelling here, so root-relative
        back(f'C:srv/proj{suffix}/x.md'),                   # drive-relative: below the current folder of C:
        back(f'proj{suffix}/x.md'),                         # relative
        back(f'C:/srv/proj{suffix}'),                       # the directory itself
        back(f'C:/srv/proj{suffix}/sub/x.md'),
        back(f'C:/srv/proj/my{tail}/x.md'),
        back(f'C:/srv/proj{suffix}-new/x.md'),
    ]


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_paths_that_are_unclear_on_windows_are_no_checkpoints(suffix):
    for path in refused(suffix):
        assert results(path, suffix) == [False, False, False], path
        assert at_deny_level('Write', {'file_path': path}, suffix) == 'deny', path


def test_the_suffix_is_compared_case_sensitively_which_is_stricter_than_the_file_system():
    assert results('C:\\srv\\proj\\Agent-Checkpoints\\x.md', '/agent-checkpoints') == [False, False, False]
    assert results('C:\\srv\\proj\\agent-checkpoints\\x.md', '/Agent-Checkpoints') == [False, False, False]


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_a_checkpoint_directory_that_is_a_junction_to_elsewhere_is_no_checkpoint(tmp_path, suffix):
    target = tmp_path / 'src'
    target.mkdir()
    junction = tmp_path / suffix.lstrip('/')
    junction.parent.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(target), str(junction))     # needs no privilege, unlike a symlink
    assert not os.path.islink(junction)                     # islink does not see it, realpath resolves it
    assert results(str(junction / 'app.py'), suffix) == [False, False, False]
    assert results(str(tmp_path / 'other' / suffix.lstrip('/') / 'a.md'), suffix) == [True, True, True]


def forward(path):
    return str(path).replace('\\', '/')


def git_bash(command, *args, kind='sys'):
    """Runs command in Git Bash; with MSYS=winsymlinks:<kind>, ln -s makes a link that only Git Bash follows:
    'sys' a file with '!<symlink>' and the system attribute, 'lnk' a shortcut. Neither needs a privilege."""
    env = {**os.environ, 'MSYS': f'winsymlinks:{kind}'}
    done = subprocess.run([_win.find_git_bash(), '-c', command, '_', *map(forward, args)], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize('kind', ['sys', 'lnk'])
@pytest.mark.parametrize('suffix', SUFFIXES)
def test_a_checkpoint_directory_that_only_git_bash_reads_as_a_link_is_no_checkpoint(tmp_path, suffix, kind):
    target = tmp_path / 'src'
    target.mkdir()
    link = tmp_path / suffix.lstrip('/')
    link.parent.mkdir(parents=True, exist_ok=True)
    git_bash('ln -s "$1" "$2"', target, link, kind=kind)
    git_bash('echo x >> "$1"', link / 'app.py')
    assert (target / 'app.py').is_file()                    # the threat: an append through it lands outside
    assert results(str(link / 'app.py'), suffix) == [False, False, False]
    assert not soft_stop.is_checkpoint_write('Bash', {'command': f"echo 'x' >> {forward(link / 'b.md')}"}, suffix)
    assert results(str(tmp_path / 'other' / suffix.lstrip('/') / 'a.md'), suffix) == [True, True, True]


@pytest.mark.parametrize('kind', ['sys', 'lnk'])
def test_a_checkpoint_file_that_only_git_bash_reads_as_a_link_is_no_checkpoint(tmp_path, kind):
    folder = tmp_path / 'agent-checkpoints'
    folder.mkdir()
    victim = tmp_path / 'app.py'
    victim.write_text('', encoding='utf-8')
    git_bash('ln -s "$1" "$2"', victim, folder / 'x.md', kind=kind)
    git_bash('echo x >> "$1"', folder / 'x.md')
    assert victim.read_text(encoding='utf-8') == 'x\n'
    assert results(str(folder / 'x.md'), '/agent-checkpoints') == [False, False, False]
    (folder / 'plain.md').write_text('', encoding='utf-8')
    for neighbour in ('plain.md', 'new.md'):                 # the rest of the folder stays a checkpoint folder
        assert results(str(folder / neighbour), '/agent-checkpoints') == [True, True, True], neighbour


def faked_lstat(monkeypatch, at, **fields):
    """os.lstat as before, except for the path at: there the fields given replace the real ones, or raise."""
    real = os.lstat

    def lstat(path, *args, **kwargs):
        if os.path.normcase(os.fspath(path)) != os.path.normcase(str(at)):
            return real(path, *args, **kwargs)
        if 'error' in fields:
            raise fields['error']
        st = real(path, *args, **kwargs)
        return SimpleNamespace(**{'st_mode': st.st_mode, 'st_file_attributes': st.st_file_attributes,
                                  'st_reparse_tag': st.st_reparse_tag, **fields})
    monkeypatch.setattr(os, 'lstat', lstat)


@pytest.mark.parametrize('fields', [
    {'st_file_attributes': stat.FILE_ATTRIBUTE_DIRECTORY | stat.FILE_ATTRIBUTE_REPARSE_POINT,
     'st_reparse_tag': 0xA000001D},                         # a WSL symlink, which realpath leaves as it is
    {'st_file_attributes': stat.FILE_ATTRIBUTE_DIRECTORY | stat.FILE_ATTRIBUTE_REPARSE_POINT,
     'st_reparse_tag': 0x8000001B},                         # an app execution alias
    {'error': PermissionError('access denied')}])           # unclear
def test_a_part_of_the_path_that_realpath_cannot_judge_makes_no_checkpoint(tmp_path, monkeypatch, fields):
    folder = tmp_path / 'proj' / 'agent-checkpoints'
    folder.mkdir(parents=True)
    faked_lstat(monkeypatch, tmp_path / 'proj', **fields)
    assert results(str(folder / 'x.md'), '/agent-checkpoints') == [False, False, False]


def test_the_first_part_after_the_drive_is_checked_too(monkeypatch):
    faked_lstat(monkeypatch, 'C:/srv', error=PermissionError('access denied'))
    assert results('C:/srv/proj/agent-checkpoints/x.md', '/agent-checkpoints') == [False, False, False]


def test_a_folder_with_the_system_attribute_stays_allowed(tmp_path, monkeypatch):
    """Windows sets it on customised folders (desktop.ini); only a file with it is a Git Bash link."""
    folder = tmp_path / 'proj' / 'agent-checkpoints'
    folder.mkdir(parents=True)
    faked_lstat(monkeypatch, tmp_path / 'proj',
                st_file_attributes=stat.FILE_ATTRIBUTE_DIRECTORY | stat.FILE_ATTRIBUTE_SYSTEM)
    assert results(str(folder / 'x.md'), '/agent-checkpoints') == [True, True, True]


def test_a_junction_earlier_in_the_path_is_resolved_and_stays_allowed(tmp_path):
    real = tmp_path / 'real'
    (real / 'agent-checkpoints').mkdir(parents=True)
    _winapi.CreateJunction(str(real), str(tmp_path / 'proj'))
    assert results(str(tmp_path / 'proj' / 'agent-checkpoints' / 'x.md'), '/agent-checkpoints') == [True, True, True]


def test_an_error_in_realpath_makes_no_checkpoint_and_other_paths_never_reach_it(monkeypatch):
    asked = []

    def hung(path, *args, **kwargs):
        asked.append(path)
        raise OSError('the network drive does not answer')
    monkeypatch.setattr(os.path, 'realpath', hung)
    assert at_deny_level('Write', {'file_path': 'Z:/share/src/app.py'}, '/agent-checkpoints') == 'deny'
    assert asked == []                                      # a hanging drive cannot delay an ordinary call
    assert results('C:/srv/proj/agent-checkpoints/x.md', '/agent-checkpoints') == [False, False, False]
    assert asked


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_bash_appends_take_windows_paths_unquoted_with_slashes_or_single_quoted(suffix):
    slash = f'C:/srv/proj{suffix}/a.md'
    for target in (slash, f"'{back(slash)}'", f'/c/srv/proj{suffix}/a.md', f'"{slash}"'):
        for form in APPENDS:
            command = form.format(target)
            assert soft_stop.is_checkpoint_write('Bash', {'command': command}, suffix), command
    # bash reads a backslash in double quotes as an escape, and drops it outside quotes: both stay refused
    for target in (f'"{back(slash)}"', back(slash)):
        for form in APPENDS:
            command = form.format(target)
            assert not soft_stop.is_checkpoint_write('Bash', {'command': command}, suffix), command


def windows_forms(command, suffix):
    """The command with its checkpoint path in the two other spellings of Windows (C:/... is the main suite's)."""
    z = f'{PROJ}{suffix}/x.md'
    return [command.replace(z, f"'{back(z)}'"), command.replace(PROJ, '/c/srv/proj')]


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_forged_checkpoints_with_windows_paths_are_still_refused(suffix):
    for command in windows_forms(f"echo 'x' >> {PROJ}{suffix}/x.md", suffix):    # the plain append is one
        assert soft_stop.is_checkpoint_write('Bash', {'command': command}, suffix), command
    for forgery in forgeries(suffix):
        for command in windows_forms(forgery, suffix):
            assert not soft_stop.is_checkpoint_write('Bash', {'command': command}, suffix), command
            assert at_deny_level('Bash', {'command': command}, suffix) == 'deny', command


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_the_denial_tells_the_model_the_windows_spelling(suffix):
    cfg = copy.deepcopy(_config.DEFAULTS)
    cfg['checkpoint_dir_suffix'] = suffix
    reason = soft_stop.decide(_usage.Usage(96, 20, 1.0, max_age_min=15.0), 'Read', {}, 0, False, cfg).reason
    assert f'C:/Users/<name>/project{suffix}/<file>' in reason and 'forward slashes' in reason
    assert reason.isascii()


def test_the_real_hook_allows_a_windows_checkpoint_and_denies_anything_else_at_the_deny_level(tmp_path):
    write_usage(tmp_path, 96)
    inside = 'C:\\srv\\proj\\agent-checkpoints'
    for allowed in (call('Write', file_path=inside + '\\a.md', content='x'),
                    call('Edit', file_path='c:/srv/proj/agent-checkpoints/a.md', old_string='a', new_string='b'),
                    call('Bash', command=f"echo '- item done' >> '{inside}\\a.md'"),
                    call('Bash', command="cat >> /c/srv/proj/agent-checkpoints/a.md <<'EOF'\n- item done\nEOF")):
        assert run(allowed, ENFORCE) is None, allowed
    for denied in (call('Write', file_path='C:\\srv\\proj\\src\\app.py', content='x'),
                   call('Write', file_path=inside + '\\..\\..\\src\\app.py', content='x'),
                   call('Write', file_path=inside + '\\a.md:x', content='x'),
                   call('Write', file_path='\\\\server\\share\\proj\\agent-checkpoints\\a.md', content='x'),
                   call('Bash', command=f'echo \'x\' >> "{inside}\\a.md"')):
        assert is_deny(run(denied, ENFORCE)), denied


@pytest.mark.parametrize('missing,broken', [(('_plat.py',), ()), (('_win.py',), ()), ((), ('_plat.py',)),
                                            ((), ('_win.py',))])
def test_a_missing_or_broken_platform_switch_lets_the_call_through(tmp_path, missing, broken):
    write_usage(tmp_path, 99)
    folder = copy_hooks(tmp_path, broken=broken, missing=missing)
    assert run_raw(folder / 'soft_stop.py', json.dumps(call()), ENFORCE) == (0, None)


def test_a_failed_import_in_the_path_check_lets_the_call_through_and_is_logged(tmp_path, monkeypatch, capsys):
    import _hookio  # noqa: F401  loaded, with its _plat, before _plat is hidden
    write_usage(tmp_path, 99)
    monkeypatch.setattr(sys, 'argv', ['soft_stop.py', ENFORCE])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(call('Write', file_path='C:\\srv\\app.py', content='x'))))
    monkeypatch.setitem(sys.modules, '_plat', None)   # _hookio and _usage hold theirs already; the path check fails
    assert soft_stop.main() == 0
    assert capsys.readouterr().out == ''
    assert log_lines(tmp_path)[-1]['decision'] == 'error' and log_lines(tmp_path)[-1]['reason'] == 'ModuleNotFoundError'
