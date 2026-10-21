"""Tests for the assistant of tools/install.py (0034-INSTALL-ASSISTENT): in a terminal and without any option the
installer asks for each part, builds the options from the answers and runs them through main(argv), as the command
line does.

The terminal is faked: stdin and stdout are text buffers whose isatty() says yes, the answers are lines of stdin. The
installer runs in this process with its default paths, which conftest points below tmp_path (HOME). config.json is the
one in the target, as in a real installation."""
import importlib.util
import io
import json
import os
import select
import shlex
import subprocess
import sys
import time

import pytest

import _plat
import _sync_git
import _win
import sync_format
from conftest import ROOT
from test_sync_git import ASKS, REPO, answer, git_alone, remote  # noqa: F401

INSTALL = os.path.join(ROOT, 'tools', 'install.py')
ALL = 'statusline,usage-clock,context-reporter,spawn-gate,soft-stop'
CTRL_C = object()       # an answer that is Ctrl+C

STATUSLINE = 'Status line: model, 5h/week usage and context below the prompt?'
REPLACE = 'Replace it?'
USAGE_CLOCK = 'Usage clock: one line with usage, reset times and local time in every prompt?'
CONTEXT = 'Context reporter: tells the model how full its context is (needs the status line)?'
SPAWN_GATE = 'Spawn gate: stops new agent blocks above a usage threshold?'
SOFT_STOP = 'Soft stop: slows subagents down near the five-hour limit?'
ENFORCE = 'Let the gates block for real (enforce)? No = only log (shadow).'
AUTO_CONTINUE = 'Continue automatically after a usage limit resets?'
SYNC = 'Sync across computers through a private GitHub repository?'
SYNC_REPO = 'Name of your PRIVATE repository (owner/name)'
SYNC_NAME = 'Name of this computer in the report?'
PLATFORM = sync_format.platform_name()
INSTALL_NOW = 'Install now? [Y/n]'
QUESTIONS = (STATUSLINE, REPLACE, USAGE_CLOCK, CONTEXT, SPAWN_GATE, SOFT_STOP, ENFORCE, AUTO_CONTINUE, SYNC, SYNC_REPO,
             SYNC_NAME)


def load():
    spec = importlib.util.spec_from_file_location('install_assistant_under_test', INSTALL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Terminal(io.StringIO):
    """stdin of a terminal: the answers, one per line; CTRL_C raises KeyboardInterrupt when it is read."""

    def __init__(self, answers):
        super().__init__()
        self.answers = list(answers)
        self.reads = 0

    def isatty(self):
        return True

    def readline(self, *args):
        self.reads += 1
        if not self.answers:
            return ''       # end of input
        line = self.answers.pop(0)
        if line is CTRL_C:
            raise KeyboardInterrupt
        sys.stdout.write(line + '\n')        # the echo of the terminal
        return line + '\n'


class Screen(io.StringIO):
    def isatty(self):
        return True


class Unreadable(io.StringIO):
    """stdin that fails the test as soon as anything reads it."""

    def __init__(self, tty):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty

    def read(self, *args):
        raise AssertionError('stdin was read')

    readline = readlines = read

    def __iter__(self):
        raise AssertionError('stdin was read')


class Machine:
    """The default paths of the installer below tmp_path, and the installer as a module."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.monkeypatch = tmp_path, monkeypatch
        self.home = tmp_path / 'home'
        (self.home / '.claude').mkdir(parents=True)
        self.module = load()
        monkeypatch.delenv('CCUC_CONFIG')       # the config of the installation is <target>/config.json
        if os.name == 'nt':     # the faked streams have no console behind them
            monkeypatch.setattr(_win, 'is_console', lambda stream: True)

    @property
    def settings(self):
        return self.home / '.claude' / 'settings.json'

    @property
    def target(self):
        return self.home / '.claude' / 'hooks' / 'usage-clock'

    @property
    def config(self):
        return self.target / 'config.json'

    def data(self):
        return json.loads(self.settings.read_text(encoding='utf-8'))

    def config_data(self):
        return json.loads(self.config.read_text(encoding='utf-8'))

    def ask(self, *answers):
        """The installer without options in the faked terminal: (exit code, everything it printed)."""
        stdin, screen = Terminal(answers), Screen()
        self.monkeypatch.setattr(sys, 'stdin', stdin)
        self.monkeypatch.setattr(sys, 'stdout', screen)
        try:
            code = self.module.main([])
        finally:
            self.monkeypatch.setattr(sys, 'stdout', sys.__stdout__)
        self.left = stdin.answers
        return code, screen.getvalue()

    def cli(self, *args):
        """The installer with options and its default paths, as the command line runs it."""
        screen = io.StringIO()
        self.monkeypatch.setattr(sys, 'stdout', screen)
        try:
            code = self.module.main([str(a) for a in args])
        finally:
            self.monkeypatch.setattr(sys, 'stdout', sys.__stdout__)
        return code, screen.getvalue()

    def files(self):
        """Every file below the home folder with its bytes and modification time."""
        return {str(p.relative_to(self.home)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in sorted(self.home.rglob('*')) if p.is_file()}

    def backups(self):
        return sorted(p.name for p in self.home.rglob('*.bak-usage-clock-*'))

    def foreign_statusline(self):
        self.settings.write_text(json.dumps({'statusLine': {'type': 'command', 'command': 'my-line'}}),
                                 encoding='utf-8')

    def install_sync_entries(self, mode='shadow', repo=REPO):
        """An installed sync without its clone: the two hooks in settings.json and the keys in config.json."""
        data = json.loads(self.settings.read_text(encoding='utf-8')) if self.settings.exists() else {}
        hooks = data.setdefault('hooks', {})
        for event in ('SessionStart', 'SessionEnd'):
            command = self.module.hook_command(str(self.target), 'sync', None, None, event=event)
            hooks.setdefault(event, []).append({'hooks': [{'type': 'command', 'command': command, 'timeout': 5}]})
        self.settings.write_text(json.dumps(data), encoding='utf-8')
        self.target.mkdir(parents=True, exist_ok=True)
        cfg = self.config_data() if self.config.exists() else {}
        cfg['sync'] = dict(cfg.get('sync', {}), mode=mode, repo=repo)
        self.config.write_text(json.dumps(cfg), encoding='utf-8')


@pytest.fixture
def machine(tmp_path, monkeypatch):
    return Machine(tmp_path, monkeypatch)


def runs(module, out):
    """The equivalent command lines of the summary, each as an argv list."""
    found = []
    for line in out.splitlines():
        for prefix in ('Same as: ', 'then:    '):
            if line.startswith(prefix + module.PROGRAM + ' '):
                found.append(shlex.split(line[len(prefix + module.PROGRAM) + 1:]))
    return found


def asked(out):
    return [question for question in QUESTIONS if question in out]


def sync_commands(data):
    return [h['command'] for event in ('SessionStart', 'SessionEnd') for g in data.get('hooks', {}).get(event, [])
            for h in g['hooks']]


# --- When it asks -----------------------------------------------------------------------------------------------

def test_in_a_terminal_without_options_it_asks_every_question_of_a_first_install(machine):
    code, out = machine.ask('', '', '', '', '', '', '', '', 'n')
    assert code == 0, out
    assert asked(out) == [STATUSLINE, USAGE_CLOCK, CONTEXT, SPAWN_GATE, SOFT_STOP, ENFORCE, AUTO_CONTINUE, SYNC]
    assert runs(machine.module, out) == [['--components', ALL, '--mode', 'shadow', '--sync-mode', 'off']]
    assert INSTALL_NOW in out and machine.left == []
    assert not machine.settings.exists() and not machine.target.exists()


def test_without_argv_it_reads_the_command_line_and_asks_when_it_is_empty(machine, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['install.py'])
    stdin, screen = Terminal(['n'] * 9), Screen()
    monkeypatch.setattr(sys, 'stdin', stdin)
    monkeypatch.setattr(sys, 'stdout', screen)
    assert machine.module.main() == 0
    monkeypatch.setattr(sys, 'stdout', sys.__stdout__)
    assert STATUSLINE in screen.getvalue() and stdin.reads > 0


def test_every_question_has_one_line_of_explanation_above_it(machine):
    code, out = machine.ask('', '', '', '', '', '', '', 'y', REPO, '', 'n')
    lines = out.splitlines()
    for question in (STATUSLINE, USAGE_CLOCK, CONTEXT, SPAWN_GATE, SOFT_STOP, ENFORCE, AUTO_CONTINUE, SYNC, SYNC_REPO,
                     SYNC_NAME):
        index = next(i for i, line in enumerate(lines) if question in line)
        above = lines[index - 1]
        assert above.strip() and not any(q in above for q in QUESTIONS), (question, above)
    assert 'gh repo create NAME --private' in out and 'https://github.com/new' in out


def test_the_default_is_shown_in_brackets_and_taken_with_enter(machine):
    code, out = machine.ask('', '', '', '', '', '', '', '', 'n')
    assert f'{STATUSLINE} [Y/n]' in out and f'{ENFORCE} [y/N]' in out and f'{AUTO_CONTINUE} [y/N]' in out
    assert f'{SYNC} [y/N]' in out


def option_cases(tmp_path):
    folder = tmp_path / 'other'
    folder.mkdir()
    return [['--settings', str(folder / 'settings.json')], ['--target', str(folder / 'target')],
            ['--components', 'statusline'], ['--mode', 'shadow'], ['--week-ok-until', '2026-12-31'],
            ['--replace-statusline'], ['--auto-continue'], ['--sync-repo', REPO], ['--sync-mode', 'on'],
            ['--sync-name', 'laptop'],
            ['--dry-run'], ['--uninstall'], ['--check'], ['--help']]


def test_the_cases_cover_every_option_of_the_parser(machine, tmp_path):
    options = {o for action in machine.module.build_parser()._actions for o in action.option_strings if o.startswith('--')}
    assert {case[0] for case in option_cases(tmp_path)} == options


@pytest.mark.parametrize('index', range(14))
def test_any_option_switches_it_off_and_nothing_is_read(machine, tmp_path, monkeypatch, index):
    argv = option_cases(tmp_path)[index]
    monkeypatch.setattr(sys, 'stdin', Unreadable(tty=True))
    screen = Screen()
    monkeypatch.setattr(sys, 'stdout', screen)
    try:
        machine.module.main(argv)
    except SystemExit:      # --help, or an option that needs another one
        pass
    finally:
        monkeypatch.setattr(sys, 'stdout', sys.__stdout__)
    assert STATUSLINE not in screen.getvalue()


@pytest.mark.parametrize('stdin_tty,stdout_tty', [(False, False), (False, True), (True, False)])
def test_without_a_terminal_it_runs_as_before_and_reads_nothing(machine, monkeypatch, stdin_tty, stdout_tty):
    monkeypatch.setattr(sys, 'stdin', Unreadable(tty=stdin_tty))
    screen = Screen() if stdout_tty else io.StringIO()
    monkeypatch.setattr(sys, 'stdout', screen)
    code = machine.module.main([])
    monkeypatch.setattr(sys, 'stdout', sys.__stdout__)
    assert code == 0, screen.getvalue()
    assert STATUSLINE not in screen.getvalue()
    hooks = machine.data()['hooks']
    assert 'statusLine' in machine.data() and 'SessionStart' not in hooks and len(hooks['PreToolUse']) == 2


def test_without_stdin_at_all_it_runs_as_before(machine, monkeypatch):
    monkeypatch.setattr(sys, 'stdin', None)
    screen = Screen()
    monkeypatch.setattr(sys, 'stdout', screen)
    code = machine.module.main([])
    monkeypatch.setattr(sys, 'stdout', sys.__stdout__)
    assert code == 0 and STATUSLINE not in screen.getvalue() and 'statusLine' in machine.data()


def test_a_terminal_is_a_stream_whose_isatty_says_yes(monkeypatch):
    if os.name == 'nt':
        monkeypatch.setattr(_win, 'is_console', lambda stream: True)
    assert _plat.is_terminal(Screen()) and not _plat.is_terminal(io.StringIO()) and not _plat.is_terminal(None)


# --- One test per row of the question table ---------------------------------------------------------------------

def test_row_1_no_status_line_and_then_no_context_reporter_question(machine):
    code, out = machine.ask('n', '', '', '', '', '', '', 'n')
    assert CONTEXT not in asked(out)
    assert runs(machine.module, out) == [['--components', 'usage-clock,spawn-gate,soft-stop', '--mode', 'shadow',
                                          '--sync-mode', 'off']]


def test_row_1a_a_foreign_status_line_is_replaced_on_yes(machine):
    machine.foreign_statusline()
    code, out = machine.ask('', 'y', '', '', '', '', '', '', '', 'n')
    assert asked(out)[:2] == [STATUSLINE, REPLACE] and f'{REPLACE} [y/N]' in out
    assert runs(machine.module, out) == [['--components', ALL, '--replace-statusline', '--mode', 'shadow',
                                          '--sync-mode', 'off']]


def test_row_1a_a_foreign_status_line_stays_on_enter(machine):
    machine.foreign_statusline()
    code, out = machine.ask('', '', '', '', '', '', '', '', '', 'n')
    assert REPLACE in asked(out)
    assert runs(machine.module, out) == [['--components', ALL, '--mode', 'shadow', '--sync-mode', 'off']]


def test_row_1a_is_not_asked_without_a_foreign_status_line_or_without_the_status_line(machine):
    assert REPLACE not in asked(machine.ask('', '', '', '', '', '', '', '', 'n')[1])
    machine.foreign_statusline()
    code, out = machine.ask('n', '', '', '', '', '', '', 'n')
    assert REPLACE not in asked(out) and '--replace-statusline' not in runs(machine.module, out)[0]


def test_row_2_no_usage_clock(machine):
    code, out = machine.ask('', 'n', '', '', '', '', '', '', 'n')
    assert runs(machine.module, out)[0][:2] == ['--components', 'statusline,context-reporter,spawn-gate,soft-stop']


def test_row_3_no_context_reporter(machine):
    code, out = machine.ask('', '', 'n', '', '', '', '', '', 'n')
    assert runs(machine.module, out)[0][:2] == ['--components', 'statusline,usage-clock,spawn-gate,soft-stop']


def test_row_4_no_spawn_gate(machine):
    code, out = machine.ask('', '', '', 'n', '', '', '', '', 'n')
    assert ENFORCE in asked(out)
    assert runs(machine.module, out) == [['--components', 'statusline,usage-clock,context-reporter,soft-stop',
                                          '--mode', 'shadow', '--sync-mode', 'off']]


def test_row_5_no_soft_stop(machine):
    code, out = machine.ask('', '', '', '', 'n', '', '', '', 'n')
    assert ENFORCE in asked(out)
    assert runs(machine.module, out)[0][:2] == ['--components', 'statusline,usage-clock,context-reporter,spawn-gate']


def test_row_6_is_not_asked_without_a_gate_and_no_mode_is_given(machine):
    code, out = machine.ask('', '', '', 'n', 'n', '', '', 'n')
    assert ENFORCE not in asked(out)
    assert runs(machine.module, out) == [['--components', 'statusline,usage-clock,context-reporter',
                                          '--sync-mode', 'off']]


def test_row_6_yes_is_enforce(machine):
    code, out = machine.ask('', '', '', '', '', 'y', '', '', 'n')
    assert runs(machine.module, out) == [['--components', ALL, '--mode', 'enforce', '--sync-mode', 'off']]


def test_row_7_yes_is_auto_continue(machine):
    code, out = machine.ask('', '', '', '', '', '', 'y', '', 'n')
    assert runs(machine.module, out) == [['--components', ALL, '--mode', 'shadow', '--auto-continue',
                                          '--sync-mode', 'off']]


def test_row_8_and_8a_yes_adds_the_sync_with_its_repository(machine):
    code, out = machine.ask('', '', '', '', '', '', '', 'y', REPO, '', 'n')
    assert asked(out)[-3:] == [SYNC, SYNC_REPO, SYNC_NAME]
    assert runs(machine.module, out) == [['--components', ALL + ',sync', '--mode', 'shadow', '--sync-repo', REPO,
                                          '--sync-name', PLATFORM]]


def test_row_8a_and_8b_are_not_asked_on_no(machine):
    out = machine.ask('', '', '', '', '', '', '', 'n', 'n')[1]
    assert SYNC_REPO not in asked(out) and SYNC_NAME not in asked(out)


def test_row_8a_asks_again_until_the_name_is_owner_name(machine):
    code, out = machine.ask('', '', '', '', '', '', '', 'y', 'https://github.com/owner/name', '', REPO, '', 'n')
    assert out.count(SYNC_REPO) == 3 and out.count('That is no owner/name') == 2
    assert runs(machine.module, out)[0][-4:-2] == ['--sync-repo', REPO]


def test_row_8a_three_names_that_are_no_owner_name_stop_with_exit_2(machine):
    code, out = machine.ask('', '', '', '', '', '', '', 'y', 'owner', '../name', 'owner/name/more', REPO)
    assert code == 2 and 'nothing changed' in out and INSTALL_NOW not in out and machine.left == [REPO]
    assert not machine.settings.exists() and not machine.target.exists()


# --- The answers run through the same code as the options -------------------------------------------------------

def run_and_restart(machine, answers):
    """Runs the assistant, keeps settings.json and config.json, then moves the home folder away and makes a fresh one
    with the same path: the equivalent command line runs against the same start and the same paths."""
    code, out = machine.ask(*answers)
    assert code == 0, out
    result = (machine.settings.read_bytes(), machine.config.read_bytes())
    os.rename(machine.home, machine.tmp / f'home-assistant-{time.monotonic_ns()}')
    (machine.home / '.claude').mkdir(parents=True)
    return out, result


def test_the_assistant_writes_byte_for_byte_what_its_command_line_writes(machine):
    out, assistant = run_and_restart(machine, ['', '', '', '', '', 'y', 'y', '', ''])
    commands = runs(machine.module, out)
    assert commands == [['--components', ALL, '--mode', 'enforce', '--auto-continue', '--sync-mode', 'off']]
    for argv in commands:
        assert machine.cli(*argv)[0] == 0
    assert (machine.settings.read_bytes(), machine.config.read_bytes()) == assistant


def test_the_replaced_status_line_is_byte_for_byte_the_same_as_its_command_line(machine):
    machine.foreign_statusline()
    out, assistant = run_and_restart(machine, ['', 'y', '', 'n', '', '', '', '', '', ''])
    machine.foreign_statusline()
    for argv in runs(machine.module, out):
        assert machine.cli(*argv)[0] == 0
    assert (machine.settings.read_bytes(), machine.config.read_bytes()) == assistant


def test_removing_an_installed_sync_is_byte_for_byte_its_two_command_lines(machine):
    def start():
        assert machine.cli('--components', 'usage-clock,spawn-gate,soft-stop')[0] == 0
        machine.install_sync_entries()
    start()
    out, assistant = run_and_restart(machine, ['', '', '', '', '', '', 'n', ''])
    commands = runs(machine.module, out)
    assert commands == [['--components', 'usage-clock,spawn-gate,soft-stop', '--mode', 'shadow'],
                        ['--uninstall', '--components', 'sync']]
    start()
    for argv in commands:
        assert machine.cli(*argv)[0] == 0
    assert (machine.settings.read_bytes(), machine.config.read_bytes()) == assistant
    assert sync_commands(json.loads(assistant[0])) == [] and json.loads(assistant[1])['sync']['mode'] == 'off'


def test_a_failing_first_run_stops_before_the_second(machine, monkeypatch):
    assert machine.cli('--components', 'usage-clock')[0] == 0
    machine.install_sync_entries()
    monkeypatch.setattr(machine.module, 'smoke_test', lambda *a, **k: ['usage_clock.py: broken'])
    before = machine.files()
    code, out = machine.ask('n', '', '', '', '', 'n', '')
    assert runs(machine.module, out) == [['--components', 'usage-clock'], ['--uninstall', '--components', 'sync']]
    assert code == 1 and 'SMOKE TEST FAILED' in out and machine.files() == before


# --- Second run -------------------------------------------------------------------------------------------------

def test_a_second_run_with_only_enter_changes_nothing(machine):
    assert machine.ask('', '', '', '', '', '', '', '', '')[0] == 0
    before, backups = machine.files(), machine.backups()
    runtime = {k for k in before if k.startswith(os.path.join('.claude', 'hooks')) and not k.endswith('config.json')}
    code, out = machine.ask('', '', '', '', '', '', '', '', '')
    assert code == 0, out
    after = machine.files()
    # every run copies the runtime files again (same bytes, new time); everything else is not even touched
    assert {k: v[0] for k, v in after.items()} == {k: v[0] for k, v in before.items()}
    assert {k: v for k, v in after.items() if k not in runtime} == {k: v for k, v in before.items() if k not in runtime}
    assert machine.backups() == backups
    assert 'settings.json is already up to date - not written.' in out
    assert 'config.json: sync keys already set - unchanged' in out


def test_the_defaults_of_a_second_run_are_what_is_installed(machine):
    assert machine.cli('--components', 'statusline,spawn-gate', '--mode', 'enforce', '--auto-continue',
                       '--week-ok-until', '2026-12-31')[0] == 0
    code, out = machine.ask('', '', '', '', '', '', '', '', 'n')
    assert f'{STATUSLINE} [Y/n]' in out and f'{USAGE_CLOCK} [y/N]' in out and f'{CONTEXT} [y/N]' in out
    assert f'{SPAWN_GATE} [Y/n]' in out and f'{SOFT_STOP} [y/N]' in out and f'{ENFORCE} [Y/n]' in out
    assert f'{AUTO_CONTINUE} [Y/n]' in out
    assert runs(machine.module, out) == [['--components', 'statusline,spawn-gate', '--mode', 'enforce',
                                          '--week-ok-until', '2026-12-31', '--auto-continue', '--sync-mode', 'off']]


def test_gates_in_two_modes_keep_them_on_enter(machine):
    assert machine.cli('--components', 'spawn-gate', '--mode', 'enforce')[0] == 0
    assert machine.cli('--components', 'soft-stop', '--mode', 'shadow')[0] == 0
    code, out = machine.ask('', '', '', '', '', '', '', 'n')
    assert f'{ENFORCE} [y/n, Enter keeps]' in out
    assert runs(machine.module, out) == [['--components', 'spawn-gate,soft-stop', '--sync-mode', 'off']]
    code, out = machine.ask('', '', '', '', 'y', '', '', 'n')
    assert runs(machine.module, out)[0][2:4] == ['--mode', 'enforce']


def test_gates_in_two_modes_stay_as_they_are_on_enter_and_install(machine):
    assert machine.cli('--components', 'spawn-gate', '--mode', 'enforce')[0] == 0
    assert machine.cli('--components', 'soft-stop', '--mode', 'shadow')[0] == 0
    before = machine.settings.read_bytes()
    assert machine.ask('', '', '', '', '', '', '', '')[0] == 0
    assert machine.settings.read_bytes() == before


def test_an_installed_sync_is_offered_with_its_repository_and_kept_without_a_new_check(machine, monkeypatch):
    machine.install_sync_entries(mode='on', repo='anna/usage-sync')
    monkeypatch.setattr(_sync_git, 'check_visibility', lambda *a, **k: pytest.fail('no visibility check on Enter'))
    code, out = machine.ask('', '', '', '', '', '', '', '', 'n')
    assert f'{SYNC} [Y/n]' in out and f'{SYNC_REPO} [anna/usage-sync]:' in out
    assert f'{SYNC_NAME} [empty, shown as machine-<id>]' in out
    assert runs(machine.module, out) == [['--components', 'sync']]


def test_an_installed_sync_with_another_repository_is_checked_again(machine):
    machine.install_sync_entries(repo='anna/usage-sync')
    code, out = machine.ask('', '', '', '', '', '', 'anna/other', '', 'n')
    assert runs(machine.module, out) == [['--components', 'sync', '--sync-repo', 'anna/other']]


def test_a_sync_that_is_not_installed_gets_its_repository_checked_even_if_config_json_names_it(machine):
    assert machine.cli('--components', 'usage-clock')[0] == 0
    cfg = machine.config_data()
    cfg['sync'].update(repo='anna/usage-sync')
    machine.config.write_text(json.dumps(cfg), encoding='utf-8')
    code, out = machine.ask('', '', '', '', '', 'y', '', '', 'n')
    assert f'{SYNC_REPO} [anna/usage-sync]:' in out
    assert runs(machine.module, out)[0][-4:] == ['--sync-repo', 'anna/usage-sync', '--sync-name', PLATFORM]


def test_no_on_an_installed_component_leaves_it_installed_and_says_how_to_remove_it(machine):
    assert machine.cli('--components', 'usage-clock,soft-stop')[0] == 0
    code, out = machine.ask('', 'n', '', '', '', '', '', 'n')
    assert runs(machine.module, out) == [['--components', 'soft-stop', '--mode', 'shadow', '--sync-mode', 'off']]
    assert f'{machine.module.PROGRAM} --uninstall --components usage-clock' in out


def test_no_on_auto_continue_that_is_set_says_it_stays(machine):
    assert machine.cli('--components', 'usage-clock', '--auto-continue')[0] == 0
    code, out = machine.ask('', '', '', '', 'n', '', 'n')
    assert '--auto-continue' not in runs(machine.module, out)[0]
    assert 'autoContinueAtUsageLimit stays true' in out


def test_nothing_selected_installs_nothing(machine):
    code, out = machine.ask('n', 'n', 'n', 'n', 'n', 'n')
    assert code == 0 and 'Nothing selected, nothing to install.' in out and INSTALL_NOW not in out
    assert runs(machine.module, out) == [] and not machine.settings.exists()


def test_nothing_selected_with_an_installed_sync_only_removes_the_sync(machine):
    machine.install_sync_entries()
    code, out = machine.ask('', '', '', '', '', 'n', 'n')
    assert runs(machine.module, out) == [['--uninstall', '--components', 'sync']]


# --- No at question 8 -------------------------------------------------------------------------------------------

def test_no_at_8_sets_sync_mode_off_and_the_next_run_offers_no_as_your_choice(machine):
    assert machine.cli('--components', 'usage-clock')[0] == 0
    machine.config.write_text('{"log_dir": "~/.cache/claude-usage-clock"}', encoding='utf-8')      # before 1.6.0
    code, out = machine.ask('n', '', '', '', '', '', '')
    assert code == 0, out
    assert "sync.mode (not set) -> 'off'" in out and machine.config_data()['sync'] == {'mode': 'off'}
    assert 'off (your choice)' not in out
    code, out = machine.ask('n', '', '', '', '', '', 'n')
    assert 'Now: off (your choice).' in out and f'{SYNC} [y/N]' in out


def test_a_first_install_has_no_your_choice(machine):
    assert 'your choice' not in machine.ask('', '', '', '', '', '', '', '', 'n')[1]


def test_an_unreadable_config_json_gives_no_hint_and_the_run_stops_at_it(machine):
    assert machine.cli('--components', 'usage-clock')[0] == 0
    machine.config.write_text('{not json', encoding='utf-8')
    code, out = machine.ask('n', '', '', '', '', '', '')
    assert 'your choice' not in out
    assert code == 2 and 'cannot be read as JSON' in out


# --- Input ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('yes,no', [('y', 'n'), ('Y', 'N'), ('yes', 'no'), ('YES', 'No'), (' y ', ' n ')])
def test_yes_and_no_in_any_case(machine, yes, no):
    code, out = machine.ask(no, yes, yes, no, yes, no, no, no)
    assert runs(machine.module, out) == [['--components', 'usage-clock,spawn-gate', '--mode', 'enforce',
                                          '--sync-mode', 'off']]


def test_an_answer_that_is_neither_asks_the_same_question_again(machine):
    code, out = machine.ask('maybe', 'ja', 'n', '', '', '', '', '', '', 'n')
    assert out.count(STATUSLINE) == 3 and out.count('Please answer y or n.') == 2
    assert runs(machine.module, out)[0][:2] == ['--components', 'usage-clock,spawn-gate,soft-stop']


def test_three_answers_that_are_neither_stop_with_exit_2_and_change_nothing(machine):
    code, out = machine.ask('maybe', 'ja', 'nein', 'n')
    assert code == 2 and 'Stopped (3 answers that were not y or n): nothing changed.' in out
    assert not machine.settings.exists() and not machine.target.exists() and machine.left == ['n']


def test_three_bad_answers_at_install_now_stop_with_exit_2(machine):
    code, out = machine.ask('', '', '', '', '', '', '', '', 'x', 'x', 'x', 'y')
    assert code == 2 and not machine.settings.exists() and machine.left == ['y']


@pytest.mark.parametrize('at', [0, 5, 8])
def test_end_of_input_stops_with_exit_2_and_changes_nothing(machine, at):
    code, out = machine.ask(*[''] * at)
    assert code == 2 and 'nothing changed' in out
    assert not machine.settings.exists() and not machine.target.exists()


@pytest.mark.parametrize('at', [0, 5, 8])
def test_ctrl_c_stops_with_exit_130_and_changes_nothing(machine, at):
    code, out = machine.ask(*[''] * at, CTRL_C)
    assert code == 130 and 'nothing changed' in out
    assert not machine.settings.exists() and not machine.target.exists()


def test_ctrl_c_at_the_repository_changes_nothing(machine):
    code, out = machine.ask('', '', '', '', '', '', '', 'y', CTRL_C)
    assert code == 130 and not machine.settings.exists()


def test_an_unusable_settings_json_stops_before_the_first_question(machine):
    machine.settings.write_text('[1, 2]', encoding='utf-8')
    code, out = machine.ask()
    assert code == 2 and 'the top level must be a JSON object' in out and STATUSLINE not in out


# --- Summary and end --------------------------------------------------------------------------------------------

def test_the_summary_names_the_parts_in_plain_words_before_install_now(machine):
    code, out = machine.ask('', '', 'n', '', '', '', 'y', '', 'n')
    summary = out[out.index('Summary:'):out.index(INSTALL_NOW)]
    for words in ('Status line: yes', 'Usage clock: yes', 'Context reporter: no', 'Spawn gate: yes', 'Soft stop: yes',
                  'Gates: shadow', 'Auto-continue: yes', 'Sync: no'):
        assert words in summary, words
    assert summary.index('Sync: no') < summary.index('Same as: ')


def test_no_at_install_now_ends_without_a_change_and_exit_0(machine):
    code, out = machine.ask('', '', '', '', '', '', '', '', 'n')
    assert code == 0 and 'Nothing changed.' in out
    assert not machine.settings.exists() and not machine.target.exists()


def test_at_the_end_it_says_where_the_fine_tuning_is(machine):
    code, out = machine.ask('', '', '', '', '', '', '', '', '')
    assert code == 0, out
    end = out.splitlines()[-1]
    assert str(machine.config) in end and 'docs/configuration.md' in end
    assert '--sync-mode on' not in out


def test_a_sync_in_shadow_mode_ends_with_the_command_that_switches_it_on(machine, monkeypatch, remote):
    real = _sync_git.check_visibility
    monkeypatch.setattr(_sync_git, 'check_visibility',
                        lambda repo, directory, deadline=None, execute=None: real(repo, directory, deadline,
                                                                                  answer(128, ASKS)))
    code, out = machine.ask('n', 'n', 'n', 'n', 'n', 'y', REPO, '', '')
    assert code == 0, out
    assert machine.config_data()['sync'] == dict(machine.config_data()['sync'], mode='shadow', repo=REPO,
                                                 machine_name=PLATFORM)
    assert f'{machine.module.PROGRAM} --components sync --sync-mode on' in out.splitlines()[-1]


def test_a_failed_run_ends_without_the_closing_sentence(machine, monkeypatch):
    monkeypatch.setattr(machine.module, 'smoke_test', lambda *a, **k: ['statusline.py: broken'])
    code, out = machine.ask('', '', '', '', '', '', '', '', '')
    assert code == 1 and 'docs/configuration.md' not in out and not machine.settings.exists()


# --- The parser: --sync-mode off without the component sync ------------------------------------------------------

def test_sync_mode_off_without_the_component_writes_off_and_installs_no_sync(machine):
    assert machine.cli('--components', 'usage-clock')[0] == 0
    machine.config.write_text('{}', encoding='utf-8')
    code, out = machine.cli('--components', 'usage-clock', '--sync-mode', 'off')
    assert code == 0, out
    assert machine.config_data() == {'sync': {'mode': 'off'}} and sync_commands(machine.data()) == []
    assert 'Sync mode: off (--sync-mode)' in out and len(list(machine.target.glob('config.json.bak-usage-clock-*'))) == 1
    assert 'clone' not in out


def test_sync_mode_off_without_the_component_leaves_an_explicit_off_alone(machine):
    assert machine.cli('--components', 'usage-clock')[0] == 0
    before = machine.config.read_bytes()
    code, out = machine.cli('--components', 'usage-clock', '--sync-mode', 'off')
    assert code == 0 and 'config.json: sync keys already set - unchanged' in out
    assert machine.config.read_bytes() == before and not list(machine.target.glob('config.json.bak-*'))


def test_sync_mode_off_without_the_component_in_a_dry_run_writes_nothing(machine):
    assert machine.cli('--components', 'usage-clock')[0] == 0
    machine.config.write_text('{}', encoding='utf-8')
    code, out = machine.cli('--components', 'usage-clock', '--sync-mode', 'off', '--dry-run')
    assert code == 0 and "config.json: sync.mode (not set) -> 'off' would be written" in out
    assert machine.config.read_text(encoding='utf-8') == '{}'


def test_sync_mode_off_without_the_component_refuses_an_installed_sync(machine):
    machine.install_sync_entries()
    before = machine.files()
    code, out = machine.cli('--components', 'usage-clock', '--sync-mode', 'off')
    assert code == 2 and '--uninstall --components sync' in out and machine.files() == before


@pytest.mark.posix_only
def test_sync_mode_off_without_the_component_follows_no_linked_config_json(machine, tmp_path):
    assert machine.cli('--components', 'usage-clock')[0] == 0
    elsewhere = tmp_path / 'elsewhere.json'
    elsewhere.write_text('{"sync": {"mode": "shadow"}}', encoding='utf-8')
    os.replace(machine.config, tmp_path / 'moved.json')
    os.symlink(elsewhere, machine.config)
    code, out = machine.cli('--components', 'usage-clock', '--sync-mode', 'off')
    assert code == 2 and 'symlink' in out and elsewhere.read_text(encoding='utf-8') == '{"sync": {"mode": "shadow"}}'


@pytest.mark.posix_only
def test_sync_mode_off_without_the_component_accepts_a_linked_config_json_that_is_off(machine, tmp_path):
    assert machine.cli('--components', 'usage-clock')[0] == 0
    elsewhere = tmp_path / 'elsewhere.json'
    elsewhere.write_text('{"sync": {"mode": "off"}}', encoding='utf-8')
    os.replace(machine.config, tmp_path / 'moved.json')
    os.symlink(elsewhere, machine.config)
    code, out = machine.cli('--components', 'usage-clock', '--sync-mode', 'off')
    assert code == 0 and 'config.json: sync keys already set - unchanged' in out


@pytest.mark.parametrize('mode', ['shadow', 'on'])
def test_sync_mode_shadow_or_on_still_needs_the_component(machine, monkeypatch, mode):
    screen = io.StringIO()
    monkeypatch.setattr(sys, 'stderr', screen)
    with pytest.raises(SystemExit) as stop:
        machine.module.main(['--components', 'usage-clock', '--sync-mode', mode])
    assert stop.value.code == 2 and 'need the component sync' in screen.getvalue()


def test_stdin_and_stdout_on_the_null_device_are_no_terminal(machine):
    """On Windows isatty() is true for NUL: the installer started with both on the null device must install as
    before, not ask (the questions would read the end of input and stop with exit 2)."""
    done = subprocess.run([sys.executable, INSTALL], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, timeout=120)
    assert done.returncode == 0, done.stderr
    assert 'statusLine' in machine.data()


def in_a_terminal(answers, timeout=120):
    """Starts the installer without options on a pseudo terminal (a real one: isatty and the line discipline of the
    kernel), types the answers and returns (exit code, everything the terminal showed, the typed echo included)."""
    master, slave = os.openpty()
    process = subprocess.Popen([sys.executable, INSTALL], stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    os.write(master, ''.join(f'{a}\n' for a in answers).encode())
    shown, deadline = b'', time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 1)
        if not ready:
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError:      # EIO: the installer has ended and closed the terminal
            break
        if not chunk:
            break
        shown += chunk
    os.close(master)
    return process.wait(timeout=timeout), shown.decode('utf-8', 'replace').replace('\r\n', '\n')


@pytest.mark.posix_only
def test_in_a_real_terminal_the_installer_asks_and_installs(machine):
    code, shown = in_a_terminal([''] * 9)
    assert code == 0, shown
    assert asked(shown) == [STATUSLINE, USAGE_CLOCK, CONTEXT, SPAWN_GATE, SOFT_STOP, ENFORCE, AUTO_CONTINUE, SYNC]
    assert INSTALL_NOW in shown and 'Smoke test: passed' in shown and 'docs/configuration.md' in shown
    assert machine.data()['statusLine'] and machine.config_data()['sync']['mode'] == 'off'


# --- v2: question 8b and --sync-name ------------------------------------------------------------------------------

@pytest.fixture
def private(monkeypatch, remote):
    """The visibility probe answers private; the clone comes from the local bare repository of `remote`."""
    real = _sync_git.check_visibility
    monkeypatch.setattr(_sync_git, 'check_visibility',
                        lambda repo, directory, deadline=None, execute=None: real(repo, directory, deadline,
                                                                                  answer(128, ASKS)))


def test_row_8b_a_typed_name_becomes_sync_name(machine):
    code, out = machine.ask('', '', '', '', '', '', '', 'y', REPO, 'Anna laptop', 'n')
    assert runs(machine.module, out)[0][-2:] == ['--sync-name', 'Anna laptop']
    assert 'Sync: yes, owner/name, this computer as "Anna laptop"' in out


def test_row_8b_asks_again_until_the_name_is_valid(machine):
    code, out = machine.ask('', '', '', '', '', '', '', 'y', REPO, 'a/b', 'x' * 65, 'laptop', 'n')
    assert out.count(SYNC_NAME) == 3 and out.count('At most 64 letters') == 2
    assert runs(machine.module, out)[0][-2:] == ['--sync-name', 'laptop']


def test_row_8b_three_invalid_names_stop_with_exit_2(machine):
    code, out = machine.ask('', '', '', '', '', '', '', 'y', REPO, 'a/b', 'c:d', 'e;f', 'laptop')
    assert code == 2 and machine.left == ['laptop'] and not machine.settings.exists()


def test_the_host_name_is_never_the_default(machine, monkeypatch):
    import platform
    import socket
    host = 'secret-host-4711'
    monkeypatch.setattr(socket, 'gethostname', lambda: host)
    monkeypatch.setattr(platform, 'node', lambda: host)
    monkeypatch.setenv('HOSTNAME', host)
    monkeypatch.setenv('COMPUTERNAME', host)
    code, out = machine.ask('', '', '', '', '', '', '', 'y', REPO, '', 'n')
    assert host not in out and f'{SYNC_NAME} [{PLATFORM}]' in out
    assert runs(machine.module, out)[0][-2:] == ['--sync-name', PLATFORM]


def test_the_name_of_a_second_run_is_the_one_in_config_json_and_enter_keeps_it(machine):
    machine.install_sync_entries()
    cfg = machine.config_data()
    cfg['sync']['machine_name'] = 'laptop'
    machine.config.write_text(json.dumps(cfg), encoding='utf-8')
    code, out = machine.ask('', '', '', '', '', '', '', '', 'n')
    assert f'{SYNC_NAME} [laptop]' in out and runs(machine.module, out) == [['--components', 'sync']]
    code, out = machine.ask('', '', '', '', '', '', '', 'desk', 'n')
    assert runs(machine.module, out) == [['--components', 'sync', '--sync-name', 'desk']]


def test_the_sync_with_a_name_is_byte_for_byte_its_command_line(machine, private):
    out, assistant = run_and_restart(machine, ['n', 'n', 'n', 'n', 'n', 'y', REPO, 'laptop', ''])
    commands = runs(machine.module, out)
    assert commands == [['--components', 'sync', '--sync-repo', REPO, '--sync-name', 'laptop']]
    for argv in commands:
        assert machine.cli(*argv)[0] == 0
    assert (machine.settings.read_bytes(), machine.config.read_bytes()) == assistant
    assert json.loads(assistant[1])['sync']['machine_name'] == 'laptop'


def test_sync_name_writes_the_machine_name_with_a_backup(machine):
    machine.install_sync_entries()
    code, out = machine.cli('--components', 'sync', '--sync-name', 'Anna laptop')
    assert code == 0, out
    assert machine.config_data()['sync']['machine_name'] == 'Anna laptop'
    assert "sync.machine_name '' -> 'Anna laptop'" in out
    assert len(list(machine.target.glob('config.json.bak-usage-clock-*'))) == 1
    before = machine.config.read_bytes()
    code, out = machine.cli('--components', 'sync', '--sync-name', 'Anna laptop')
    assert code == 0 and 'config.json: sync keys already set - unchanged' in out
    assert machine.config.read_bytes() == before


def test_sync_name_in_a_dry_run_writes_nothing(machine):
    machine.install_sync_entries()
    before = machine.files()
    code, out = machine.cli('--components', 'sync', '--sync-name', 'laptop', '--dry-run')
    assert code == 0 and "sync.machine_name '' -> 'laptop' would be written" in out and machine.files() == before


@pytest.mark.parametrize('bad', ['a/b', 'x' * 65, 'tab\there', 'line\n', 'c:d'])
def test_an_invalid_sync_name_is_exit_2_without_a_change(machine, monkeypatch, bad):
    machine.install_sync_entries()
    before = machine.files()
    screen = io.StringIO()
    monkeypatch.setattr(sys, 'stderr', screen)
    with pytest.raises(SystemExit) as stop:
        machine.module.main(['--components', 'sync', f'--sync-name={bad}'])
    assert stop.value.code == 2 and '--sync-name' in screen.getvalue() and machine.files() == before


@pytest.mark.parametrize('argv,text', [(['--sync-name', 'laptop'], 'need the component sync'),
                                       (['--uninstall', '--components', 'sync', '--sync-name', 'laptop'],
                                        '--uninstall takes no'),
                                       (['--check', '--components', 'sync', '--sync-name', 'laptop'],
                                        '--check changes nothing')])
def test_sync_name_needs_the_component_and_no_uninstall_or_check(machine, monkeypatch, argv, text):
    screen = io.StringIO()
    monkeypatch.setattr(sys, 'stderr', screen)
    with pytest.raises(SystemExit) as stop:
        machine.module.main(argv)
    assert stop.value.code == 2 and text in screen.getvalue()
