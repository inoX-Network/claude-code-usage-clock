"""Hook command lines for Git Bash, and finding Git Bash the way Claude Code does."""
import os
import subprocess
import sys

import pytest

import _win


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('')
    return str(path)


# --- quoting ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('text, expected', [
    ('C:/Users/x/a.py', '"C:/Users/x/a.py"'),
    ('C:/Program Files/Python 313/python.exe', '"C:/Program Files/Python 313/python.exe"'),
    ("C:/Users/O'Brien/a.py", '"C:/Users/O\'Brien/a.py"'),            # a single quote is harmless inside ""
    ('C:/Users/a&b (x)/a.py', '"C:/Users/a&b (x)/a.py"'),
    ('C:/Users/cost$HOME/a.py', "'C:/Users/cost$HOME/a.py'"),         # $ would expand inside ""
    ('C:/Users/back`tick/a.py', "'C:/Users/back`tick/a.py'"),
    ('C:/Users/say"hi"/a.py', "'C:/Users/say\"hi\"/a.py'"),
    ("C:/Users/it's $HOME/a.py", "'C:/Users/it'\"'\"'s $HOME/a.py'"),  # both: single quotes with the quote spliced in
])
def test_quote(text, expected):
    assert _win.quote(text) == expected


# --- the interpreter command --------------------------------------------------------------------------------------

def test_the_command_runs_the_current_interpreter_with_utf8_mode_and_forward_slashes(monkeypatch):
    monkeypatch.setattr(sys, 'executable', 'C:\\Program Files\\Python313\\python.exe')
    command = _win.interpreter_command('C:\\Users\\Bjoer\\.claude\\hooks\\statusline.py', '--mode=enforce')
    assert command == ('"C:/Program Files/Python313/python.exe" -X utf8 '
                       '"C:/Users/Bjoer/.claude/hooks/statusline.py" --mode=enforce')


def test_the_command_has_no_python3_and_no_backslash():
    command = _win.interpreter_command(os.path.join('C:\\', 'x y', 'a.py'), 'arg')
    assert 'python3' not in command and '\\' not in command


@pytest.mark.parametrize('folder', ['plain', 'with space', 'umlaut-äöü', "o'brien", 'dollar$x'])
def test_git_bash_runs_the_command_as_written(tmp_path, folder):
    """The real thing: the string goes through Git Bash and the script sees its own path and its arguments."""
    script = tmp_path / folder / 'show.py'
    script.parent.mkdir()
    script.write_text('import sys\nprint(sys.flags.utf8_mode, sys.argv[1:], __file__)\n', encoding='utf-8')
    bash = _win.find_git_bash()
    assert bash, 'Git Bash is a precondition of the Windows track'
    done = subprocess.run([bash, '-c', _win.interpreter_command(str(script), '--mode=shadow', 'id-1')],
                          capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert done.returncode == 0, done.stderr
    flag, rest = done.stdout.split(' ', 1)
    assert flag == '1' and rest.startswith("['--mode=shadow', 'id-1'] ")
    assert os.path.samefile(rest.split("] ", 1)[1].strip(), script)


# --- Git Bash -----------------------------------------------------------------------------------------------------

def test_the_variable_names_the_bash(tmp_path):
    bash = touch(tmp_path / 'Anywhere' / 'bash.exe')
    assert _win.find_git_bash({_win.GIT_BASH_ENV: bash, 'PATH': ''}) == bash


@pytest.mark.parametrize('name', ['bash.exe', 'sh.exe', 'bash', 'sh'])
def test_the_names_claude_code_accepts(tmp_path, name):
    path = touch(tmp_path / name)
    assert _win.find_git_bash({_win.GIT_BASH_ENV: path, 'PATH': ''}) == path


def test_a_variable_with_another_file_name_or_a_missing_file_is_ignored(tmp_path):
    other = touch(tmp_path / 'notbash.txt')
    assert _win.find_git_bash({_win.GIT_BASH_ENV: other, 'PATH': ''}) is None
    assert _win.find_git_bash({_win.GIT_BASH_ENV: str(tmp_path / 'gone' / 'bash.exe'), 'PATH': ''}) is None


def test_the_settings_env_counts_too(tmp_path):
    bash = touch(tmp_path / 'S' / 'bash.exe')
    assert _win.find_git_bash({'PATH': ''}, settings_env={_win.GIT_BASH_ENV: bash}) == bash
    assert _win.find_git_bash({'PATH': ''}, settings_env='not a dict') is None


@pytest.mark.parametrize('git', ['cmd/git.exe', 'bin/git.exe', 'mingw64/bin/git.exe'])
def test_bash_is_found_next_to_git_on_the_path(tmp_path, git):
    """The three places Git for Windows puts git.exe; bash.exe is in <root>/bin."""
    root = tmp_path / 'Git'
    git_exe = touch(root / git)
    bash = touch(root / 'bin' / 'bash.exe')
    assert _win.find_git_bash({'PATH': os.path.dirname(git_exe)}) == bash


def test_no_git_on_the_path_means_no_bash(tmp_path):
    touch(tmp_path / 'Git' / 'bin' / 'bash.exe')
    assert _win.find_git_bash({'PATH': str(tmp_path / 'empty')}) is None


def test_git_without_a_bash_beside_it_means_no_bash(tmp_path):
    git_exe = touch(tmp_path / 'Shims' / 'git.exe')
    assert _win.find_git_bash({'PATH': os.path.dirname(git_exe)}) is None


def test_the_wsl_bash_in_system32_is_never_taken(tmp_path):
    """C:\\Windows\\System32\\bash.exe is the WSL launcher, not Git Bash: it only counts via the variable."""
    touch(tmp_path / 'System32' / 'bash.exe')
    assert _win.find_git_bash({'PATH': str(tmp_path / 'System32')}) is None


def test_this_machine_has_a_git_bash():
    bash = _win.find_git_bash()
    assert bash and os.path.isfile(bash) and os.path.basename(bash).lower() == 'bash.exe'
