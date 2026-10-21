"""tools/web_export.py: the usage of a session of Claude Code on the web into the sync (the skill /usage-export).

The debug log is invented in the shape Claude Code writes it (measured 2026-10-21 in a cloud container); the
installer and the push are recorded, not run: their own tests cover them. Nothing reaches the network."""
import importlib.util
import json
import os
import stat
import subprocess

import pytest

import _sync_git
import sync_format
from conftest import ROOT

SCRIPT = os.path.join(ROOT, 'tools', 'web_export.py')
SESSION = '202f2c46-f3f8-597e-9050-2341e0ac2fda'
SECRET = 'geheim-7f3a-prompt-text'
NOW = 1_792_576_000.0       # 2026-10-21T09:46:40Z


def load():
    spec = importlib.util.spec_from_file_location('web_export_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def web():
    return load()


def log_line(t, text):
    return f'{t} [DEBUG] {text}'


def request(t, source, path='/v1/messages'):
    return log_line(t, f'[API REQUEST] {path} req_0123456789abcdefghijklmnopqrstuvwxyz source={source}')


def dispatch(t, model='claude-opus-5-5'):
    return log_line(t, f'[API:timing] dispatching to firstParty model={model}')


def finished(t, source, messages=2, inp=1685, out=451, read=182693, create=1700):
    return log_line(t, f'Forked agent [{source}] finished: {messages} messages, types=[assistant, assistant], '
                       f'totalUsage: input={inp} output={out} cacheRead={read} cacheCreate={create}')


DEBUG_LOG = [
    '======================',
    log_line('2026-10-21T07:06:13.534Z', 'Args: [--output-format=stream-json --verbose --debug]'),
    log_line('2026-10-21T07:07:01.000Z', f'User prompt: {SECRET} source=extract_memories'),
    dispatch('2026-10-21T07:07:11.486Z'),
    request('2026-10-21T07:07:11.500Z', 'sdk'),
    request('2026-10-21T07:07:12.000Z', 'count_tokens', '/v1/messages/count_tokens'),
    request('2026-10-21T07:07:13.000Z', 'unknown', '/v1/me'),
    log_line('2026-10-21T07:08:00.000Z', '[engine] turn 1 end (turns=1 usage in=10379 out=26918 cost=$2.5897)'),
    dispatch('2026-10-21T07:09:30.000Z', 'claude-opus-5-5'),
    request('2026-10-21T07:09:31.438Z', 'extract_memories'),
    request('2026-10-21T07:09:35.000Z', 'extract_memories'),
    finished('2026-10-21T07:09:39.057Z', 'extract_memories'),
    log_line('2026-10-21T07:10:00.000Z', f'Forked agent [extract_memories] finished: {SECRET} totalUsage: input=x'),
    dispatch('2026-10-21T08:04:00.000Z', 'claude-sonnet-5-5'),
    request('2026-10-21T08:04:10.000Z', 'extract_memories'),
    finished('2026-10-21T08:04:49.531Z', 'extract_memories', 12, 3587, 591, 662948, 5168),
    request('2026-10-21T08:20:00.000Z', 'away_summary'),
    request('2026-10-21T08:24:00.000Z', 'extract_memories'),
    log_line('2026-10-21T08:26:37.479Z', 'Stream started'),
]


def write_log(path, lines=DEBUG_LOG):
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


# --- Reading the debug log: numbers only ----------------------------------------------------------------------------

def test_the_debug_log_gives_each_run_with_its_calls_model_and_tokens(web, tmp_path):
    found = web.read_debug_log(str(write_log(tmp_path / 'claude-code.log')))
    assert found.runs == [
        {'t': '2026-10-21T07:09:39.057Z', 'src': 'extract_memories', 'model': 'claude-opus-5-5', 'calls': 2,
         'in': 1685, 'cw': 1700, 'cr': 182693, 'out': 451},
        {'t': '2026-10-21T08:04:49.531Z', 'src': 'extract_memories', 'model': 'claude-sonnet-5-5', 'calls': 1,
         'in': 3587, 'cw': 5168, 'cr': 662948, 'out': 591}]
    assert (found.first, found.last) == ('2026-10-21T07:06:13.534Z', '2026-10-21T08:26:37.479Z')
    assert found.unmeasured == 2           # away_summary has no counts, the last extract_memories has not finished


def test_nothing_but_numbers_times_and_names_comes_out_of_the_debug_log(web, tmp_path):
    found = web.read_debug_log(str(write_log(tmp_path / 'claude-code.log')))
    assert SECRET not in json.dumps(found._asdict())


@pytest.mark.parametrize('lines, runs, unmeasured', [
    ([], 0, 0),
    ([request('2026-10-21T07:00:00Z', 'sdk')], 0, 0),
    ([request('2026-10-21T07:00:00Z', 'extract_memories', '/v1/messages/count_tokens')], 0, 0),
    ([request('2026-10-21T07:00:00Z', 'Extract')], 0, 0),
    ([finished('2026-10-21T07:00:00Z', 'extract_memories')], 1, 0),       # a run whose requests the log missed
    ([log_line('2026-10-21T07:00:00Z', 'x [API REQUEST] /v1/messages y source=extract_memories')], 0, 0),
    (['07:00 [DEBUG] ' + request('2026-10-21T07:00:00Z', 'extract_memories')], 0, 0),
    ([finished('2026-10-21T07:00:00Z', 'extract_memories').replace('input=1685', 'input=1685.5')], 0, 0),
    ([finished('2026-10-21T07:00:00Z', 'extract_memories').replace('input=1685', 'input=' + '9' * 13)], 0, 0),
], ids=['empty', 'sdk', 'count-tokens', 'upper-case', 'run-alone', 'not-at-the-start', 'no-time-first',
        'decimal', 'too-large'])
def test_only_lines_of_the_known_shapes_count(web, tmp_path, lines, runs, unmeasured):
    found = web.read_debug_log(str(write_log(tmp_path / 'log', lines)))
    assert (len(found.runs), found.unmeasured) == (runs, unmeasured)
    if runs:
        assert found.runs[0]['model'] is None and found.runs[0]['calls'] == 0


def test_a_missing_debug_log_is_none_never_zero(web, tmp_path):
    assert web.read_debug_log(str(tmp_path / 'missing.log')) is None


def test_a_debug_log_that_is_a_link_or_no_file_is_not_read(web, tmp_path):
    real = write_log(tmp_path / 'real.log')
    os.symlink(real, tmp_path / 'link.log')
    assert web.read_debug_log(str(tmp_path / 'link.log')) is None
    assert web.read_debug_log(str(tmp_path)) is None


def test_a_debug_log_without_a_single_time_has_no_span(web, tmp_path):
    found = web.read_debug_log(str(write_log(tmp_path / 'log', ['======================', 'text'])))
    assert (found.first, found.last, found.runs) == (None, None, [])


# --- The lines for side-calls.jsonl ---------------------------------------------------------------------------------

def test_the_side_lines_carry_the_session_a_key_per_run_and_one_window(web, tmp_path):
    found = web.read_debug_log(str(write_log(tmp_path / 'claude-code.log')))
    lines = web.side_lines(SESSION, found, NOW)
    runs, (window,) = lines[:-1], lines[-1:]
    assert [line['kind'] for line in runs] == ['side', 'side']
    assert runs[0] == {'kind': 'side', 'sid': SESSION, 'k': sync_format.side_key(SESSION, 'extract_memories',
                                                                                 '2026-10-21T07:09:39.057Z'),
                       **found.runs[0]}
    assert window == {'kind': 'side_window', 't': '2026-10-21T09:46:40Z', 'sid': SESSION, 'log': True,
                      'from': '2026-10-21T07:06:13.534Z', 'to': '2026-10-21T08:26:37.479Z', 'unmeasured': 2}
    for line in lines:
        kind = line['kind']
        sync_format.encode(kind, {key: value for key, value in line.items() if key != 'kind'})


@pytest.mark.parametrize('found', [None, 'empty'])
def test_without_a_debug_log_or_a_time_in_it_the_window_says_not_measured(web, tmp_path, found):
    if found == 'empty':
        found = web.read_debug_log(str(write_log(tmp_path / 'log', ['no time here'])))
    assert web.side_lines(SESSION, found, NOW) == [{'kind': 'side_window', 't': '2026-10-21T09:46:40Z',
                                                    'sid': SESSION, 'log': False, 'from': None, 'to': None,
                                                    'unmeasured': 0}]


def test_side_calls_are_appended_once_and_each_export_adds_its_window(web, tmp_path):
    log_dir = tmp_path / 'log'
    found = web.read_debug_log(str(write_log(tmp_path / 'claude-code.log')))
    assert web.append_side(str(log_dir), web.side_lines(SESSION, found, NOW)) == 3
    assert web.append_side(str(log_dir), web.side_lines(SESSION, found, NOW + 60)) == 1     # the window only
    path = log_dir / 'side-calls.jsonl'
    lines = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    assert [line['kind'] for line in lines] == ['side', 'side', 'side_window', 'side_window']
    if os.name != 'nt':
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(log_dir.stat().st_mode) == 0o700


def test_side_calls_are_never_written_through_a_link(web, tmp_path):
    log_dir = tmp_path / 'log'
    log_dir.mkdir()
    target = tmp_path / 'elsewhere.jsonl'
    target.write_text('', encoding='utf-8')
    os.symlink(target, log_dir / 'side-calls.jsonl')
    with pytest.raises(OSError):
        web.append_side(str(log_dir), web.side_lines(SESSION, None, NOW))
    assert target.read_text(encoding='utf-8') == ''


def test_broken_lines_of_an_earlier_side_file_do_not_stop_the_append(web, tmp_path):
    log_dir = tmp_path / 'log'
    log_dir.mkdir()
    (log_dir / 'side-calls.jsonl').write_text('{broken\n["x"]\n', encoding='utf-8')
    found = web.read_debug_log(str(write_log(tmp_path / 'claude-code.log')))
    assert web.append_side(str(log_dir), web.side_lines(SESSION, found, NOW)) == 3


# --- The run: refusals, installer, push, summary --------------------------------------------------------------------

class Steps:
    """Records the installer and the push instead of running them; `codes` are their exit codes."""

    def __init__(self, web, tmp_path, codes=(0, 0), outcome=None):
        self.web, self.tmp, self.codes, self.outcome, self.calls = web, tmp_path, list(codes), outcome, []

    def __call__(self, argv):
        self.calls.append(argv)
        code = self.codes.pop(0)
        if argv[-2:] == ['push', '--now'] and self.outcome is not None:
            log_dir = self.tmp / 'log'
            log_dir.mkdir(exist_ok=True)
            with open(log_dir / 'log.jsonl', 'a', encoding='utf-8') as f:
                f.write(json.dumps({'hook': 'sync', **self.outcome}) + '\n')
                if self.outcome['decision'] == 'shadow':        # as the sync logs it: the core ends the run
                    f.write(json.dumps({'hook': 'sync', 'decision': 'success', 'action': 'push',
                                        'mode': 'shadow'}) + '\n')
        return code


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    monkeypatch.setenv('CLAUDE_CODE_REMOTE', 'true')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', SESSION)
    monkeypatch.setenv('CCUC_LOG_DIR', str(tmp_path / 'log'))
    return tmp_path


def run_main(web, monkeypatch, steps, *args):
    monkeypatch.setattr(web, 'run', steps)
    return web.main(['--sync-repo', 'owner/name', *args], now=NOW)


def test_outside_claude_code_on_the_web_nothing_is_done(web, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv('CLAUDE_CODE_REMOTE', raising=False)
    steps = Steps(web, tmp_path)
    assert run_main(web, monkeypatch, steps) == 2
    assert steps.calls == [] and 'only in Claude Code on the web' in capsys.readouterr().out


def test_without_a_session_id_nothing_is_done(web, monkeypatch, cloud, capsys):
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', '../x')
    steps = Steps(web, cloud)
    assert run_main(web, monkeypatch, steps) == 2
    assert steps.calls == [] and 'session id' in capsys.readouterr().out


def test_the_run_installs_the_sync_as_claude_web_in_shadow_mode_then_pushes_once(web, monkeypatch, cloud, capsys):
    write_log(cloud / 'claude-code.log')
    steps = Steps(web, cloud, outcome={'decision': 'shadow', 'reason': 'committed, not pushed', 'lines': 7,
                                       'bytes': 1500})
    assert run_main(web, monkeypatch, steps, '--debug-log', str(cloud / 'claude-code.log')) == 0
    install, push = steps.calls
    target = os.path.expanduser('~/.claude/hooks/usage-clock')
    settings = str(cloud / 'log' / 'web-settings.json')
    assert install[1:] == [os.path.join(ROOT, 'tools', 'install.py'), '--settings', settings, '--target', target,
                           '--components', 'sync', '--sync-repo', 'owner/name', '--sync-mode', 'shadow',
                           '--sync-name', 'claude-web']
    assert push[1:] == [os.path.join(target, 'sync.py'), 'push', '--now']
    assert json.loads((cloud / 'log' / 'web-settings.json').read_text(encoding='utf-8')) == {}
    sides = (cloud / 'log' / 'side-calls.jsonl').read_text(encoding='utf-8').splitlines()
    assert [json.loads(line)['kind'] for line in sides] == ['side', 'side', 'side_window']
    out = capsys.readouterr().out
    assert 'extract_memories: 2 runs, 3 calls' in out and 'from 07:06:13 to 08:26:37 UTC' in out
    assert '2 calls without token counts' in out
    assert 'shadow: committed here, pushed nothing (7 lines, 1500 bytes)' in out
    assert '/usage-export on' in out and SECRET not in out


def test_on_pushes_for_real_and_says_so(web, monkeypatch, cloud, capsys):
    steps = Steps(web, cloud, outcome={'decision': 'success', 'action': 'push', 'mode': 'on'})
    assert run_main(web, monkeypatch, steps, '--sync-mode', 'on', '--debug-log', str(cloud / 'none.log')) == 0
    assert steps.calls[0][-4:] == ['--sync-mode', 'on', '--sync-name', 'claude-web']
    out = capsys.readouterr().out
    assert 'pushed to owner/name' in out
    assert 'side calls: not measured (no debug log)' in out


def test_a_settings_file_that_is_there_is_kept(web, monkeypatch, cloud):
    (cloud / 'log').mkdir()
    (cloud / 'log' / 'web-settings.json').write_text('{"hooks": {}}', encoding='utf-8')
    run_main(web, monkeypatch, Steps(web, cloud), '--debug-log', str(cloud / 'none.log'))
    assert (cloud / 'log' / 'web-settings.json').read_text(encoding='utf-8') == '{"hooks": {}}'


def test_an_installer_that_fails_stops_the_run_before_the_push(web, monkeypatch, cloud, capsys):
    steps = Steps(web, cloud, codes=(1,))
    assert run_main(web, monkeypatch, steps, '--debug-log', str(cloud / 'none.log')) == 1
    assert len(steps.calls) == 1 and 'the installer stopped' in capsys.readouterr().out
    assert not (cloud / 'log' / 'side-calls.jsonl').exists()


def test_a_push_that_fails_names_its_category(web, monkeypatch, cloud, capsys):
    os.makedirs(cloud / 'log')
    with open(cloud / 'log' / 'log.jsonl', 'w', encoding='utf-8') as f:      # an older line is not this push
        f.write(json.dumps({'hook': 'sync', 'decision': 'success', 'action': 'push'}) + '\n')
    steps = Steps(web, cloud, codes=(0, 1), outcome={'decision': 'failure', 'action': 'push', 'category': 'auth',
                                                     'reason': 'the push was refused'})
    assert run_main(web, monkeypatch, steps, '--sync-mode', 'on', '--debug-log', str(cloud / 'none.log')) == 1
    assert 'push failed (auth): the push was refused' in capsys.readouterr().out


def test_a_push_without_a_log_line_is_reported_as_unknown(web, monkeypatch, cloud, capsys):
    assert run_main(web, monkeypatch, Steps(web, cloud, codes=(0, 1)), '--debug-log', str(cloud / 'x.log')) == 1
    assert 'push: no outcome in the log' in capsys.readouterr().out


def test_the_summary_counts_what_the_clone_holds_of_this_session(web, monkeypatch, cloud, capsys, tmp_path):
    log_dir = cloud / 'log'
    machine = sync_format.load_machine_id(str(log_dir))
    clone = _sync_git.clone_dir(str(log_dir))
    os.makedirs(clone)
    t = '2026-10-21T08:00:00Z'
    line = sync_format.encode('request', {'t': t, 'k': 'a' * 16, 'sid': SESSION, 'proj': 'claude', 'model': 'm',
                                          'in': 1, 'cw5': 0, 'cw1h': 2, 'cr': 3, 'out': 4, 'first': True})
    sync_format.append(clone, machine, sync_format.request_path(machine, t, SESSION), [line])
    steps = Steps(web, cloud, outcome={'decision': 'success', 'action': 'push', 'mode': 'on'})
    assert run_main(web, monkeypatch, steps, '--sync-mode', 'on', '--debug-log', str(cloud / 'x.log')) == 0
    out = capsys.readouterr().out
    assert f'this chat is machine {machine[:8]}' in out and 'the repository holds 1 request' in out


def test_run_starts_the_command_with_this_python_and_passes_its_output_through(web, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, 'run', lambda argv, check: seen.append((argv, check)) or
                        subprocess.CompletedProcess(argv, 3))
    assert web.run(['python3', 'x.py']) == 3 and seen == [(['python3', 'x.py'], False)]


def test_a_log_folder_that_is_a_link_is_refused(web, tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    os.symlink(real, tmp_path / 'log')
    with pytest.raises(OSError, match='is not a folder'):
        web.append_side(str(tmp_path / 'log'), web.side_lines(SESSION, None, NOW))
    assert list(real.iterdir()) == []


def test_lines_of_the_log_that_cannot_be_read_are_passed_over(web, monkeypatch, cloud, capsys):
    class Broken(Steps):
        def __call__(self, argv):
            if argv[-2:] == ['push', '--now']:
                os.makedirs(self.tmp / 'log', exist_ok=True)
                with open(self.tmp / 'log' / 'log.jsonl', 'a', encoding='utf-8') as f:
                    f.write('{broken\n' + json.dumps({'hook': 'spawn_gate', 'decision': 'deny'}) + '\n')
            return super().__call__(argv)
    steps = Broken(web, cloud, outcome={'decision': 'success', 'action': 'push'})
    assert run_main(web, monkeypatch, steps, '--debug-log', str(cloud / 'x.log')) == 0
    assert 'push: done' in capsys.readouterr().out


def test_the_script_runs_as_a_program_and_refuses_outside_the_web(tmp_path):
    env = {key: value for key, value in os.environ.items() if key != 'CLAUDE_CODE_REMOTE'}
    done = subprocess.run([os.sys.executable, SCRIPT, '--sync-repo', 'owner/name'], capture_output=True, text=True,
                          env=env, cwd=str(tmp_path))
    assert done.returncode == 2 and 'only in Claude Code on the web' in done.stdout


# --- Gegenprüfung #81: impossible times, O_NOFOLLOW alone, FIFOs, nested JSON ----------------------------------------

@pytest.mark.parametrize('bad', ['2026-02-30T10:00:00Z', '2026-13-45T99:99:99Z'])
def test_a_time_that_does_not_exist_is_passed_over(web, tmp_path, bad):
    lines = [request(bad, 'extract_memories'), finished(bad, 'extract_memories'), *DEBUG_LOG]
    found = web.read_debug_log(str(write_log(tmp_path / 'claude-code.log', lines)))
    assert len(found.runs) == 2 and found.first == '2026-10-21T07:06:13.534Z'


@pytest.mark.posix_only
def test_o_nofollow_alone_refuses_a_linked_debug_log(web, tmp_path, monkeypatch):
    """The check with islink comes first; O_NOFOLLOW must hold on its own (a link set between check and open)."""
    real = write_log(tmp_path / 'real.log')
    os.symlink(real, tmp_path / 'link.log')
    monkeypatch.setattr(os.path, 'islink', lambda path: False)
    assert web.read_debug_log(str(tmp_path / 'link.log')) is None


@pytest.mark.posix_only
def test_o_nofollow_alone_refuses_to_append_through_a_link(web, tmp_path, monkeypatch):
    log_dir = tmp_path / 'log'
    log_dir.mkdir()
    target = tmp_path / 'elsewhere.jsonl'
    target.write_text('', encoding='utf-8')
    os.symlink(target, log_dir / 'side-calls.jsonl')
    monkeypatch.setattr(os.path, 'islink', lambda path: False)
    with pytest.raises(OSError):                            # reading the keys refuses the link first
        web._known_keys(str(log_dir / 'side-calls.jsonl'))
    monkeypatch.setattr(web, '_known_keys', lambda path: set())
    with pytest.raises(OSError):                            # and so does the append on its own
        web.append_side(str(log_dir), web.side_lines(SESSION, None, NOW))
    assert target.read_text(encoding='utf-8') == ''


@pytest.mark.posix_only
def test_a_side_file_that_is_a_fifo_is_refused_without_waiting_for_a_writer(web, tmp_path):
    log_dir = tmp_path / 'log'
    log_dir.mkdir()
    os.mkfifo(log_dir / 'side-calls.jsonl')
    with pytest.raises(OSError, match='not a regular file'):
        web.append_side(str(log_dir), web.side_lines(SESSION, None, NOW))


def test_a_side_file_that_cannot_be_written_stops_the_run_before_the_push(web, monkeypatch, cloud, capsys):
    (cloud / 'log').mkdir()
    (cloud / 'log' / 'side-calls.jsonl').mkdir()          # a folder in its place
    steps = Steps(web, cloud)
    assert run_main(web, monkeypatch, steps, '--debug-log', str(cloud / 'x.log')) == 1
    assert len(steps.calls) == 1 and 'side-calls.jsonl cannot be written' in capsys.readouterr().out


def test_lines_nested_too_deep_are_passed_over(web, monkeypatch, cloud, capsys):
    (cloud / 'log').mkdir()
    (cloud / 'log' / 'side-calls.jsonl').write_bytes(b'[' * 100000 + b'\n')
    found = web.read_debug_log(str(write_log(cloud / 'claude-code.log')))
    assert web.append_side(str(cloud / 'log'), web.side_lines(SESSION, found, NOW)) == 3

    class Deep(Steps):
        def __call__(self, argv):
            if argv[-2:] == ['push', '--now']:
                with open(self.tmp / 'log' / 'log.jsonl', 'ab') as f:
                    f.write(b'[' * 100000 + b'\n')
            return super().__call__(argv)
    steps = Deep(web, cloud, outcome={'decision': 'success', 'action': 'push'})
    assert run_main(web, monkeypatch, steps, '--debug-log', str(cloud / 'x.log')) == 0
    assert 'push: done' in capsys.readouterr().out


# --- Security review v1.7: times out of order, lines too long or not yet complete -------------------------------------

def test_the_span_of_the_debug_log_is_its_earliest_and_latest_time_not_its_first_and_last_line(web, tmp_path):
    """S-A2: a clock that jumps back (a VM after NTP, two writers) gave a window that runs backwards, which the export
    then passed over: the requests of the session counted as neither inside nor outside a debug log."""
    lines = [*DEBUG_LOG, log_line('2026-10-21T08:30:05.500Z', 'late'), log_line('2026-10-21T08:30:05Z', 'back'),
             log_line('2026-10-21T07:00:00Z', 'Stream started')]
    found = web.read_debug_log(str(write_log(tmp_path / 'claude-code.log', lines)))
    assert (found.first, found.last) == ('2026-10-21T07:00:00Z', '2026-10-21T08:30:05.500Z')
    window = web.side_lines(SESSION, found, NOW)[-1]
    sync_format.encode('side_window', {key: value for key, value in window.items() if key != 'kind'})


def test_a_line_too_long_is_passed_over_with_its_rest_and_the_next_line_counts(web, tmp_path):
    """S-A3: the debug log holds what others write; a line is read in pieces of at most 64 KB, a longer one passed
    over to its end, also where its rest looks like a line that counts."""
    assert web.MAX_LINE_BYTES == 64 * 1024
    long = finished('2026-10-21T07:00:00Z', 'extract_memories').replace('types=[', 'types=[' + 'x' * 70_000)
    planted = 'x' * (64 * 1024 + 1) + finished('2026-10-21T07:00:01Z', 'extract_memories')     # its rest a line
    exact = finished('2026-10-21T07:00:02Z', 'extract_memories')
    exact = exact.replace('types=[', 'types=[' + 'x' * (64 * 1024 - len(exact)))      # 64 KB before its line feed
    found = web.read_debug_log(str(write_log(tmp_path / 'claude-code.log', [long, planted, exact, *DEBUG_LOG])))
    assert [run['t'] for run in found.runs] == ['2026-10-21T07:00:02Z', '2026-10-21T07:09:39.057Z',
                                                '2026-10-21T08:04:49.531Z']


def test_a_line_too_long_never_lies_in_memory_as_a_whole(web, tmp_path):
    """S-A3: measured before, a line of 300 MB took 626 MB (the line and its copy without the line feed)."""
    import tracemalloc
    path = tmp_path / 'claude-code.log'
    with open(path, 'wb') as f:
        f.write(b'2026-10-21T07:00:00Z [DEBUG] ' + b'x' * (8 * 1024 * 1024) + b'\n')
        f.write((DEBUG_LOG[1] + '\n').encode('utf-8'))
    tracemalloc.start()
    try:
        found = web.read_debug_log(str(path))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024 and found.first == '2026-10-21T07:06:13.534Z'


def test_a_last_line_without_its_line_feed_is_not_yet_a_line(web, tmp_path):
    """S-A5: Claude Code may be writing the last line while the export reads it; `cacheCreate=12` of `12345` would
    stay, as the key of the run blocks the whole line at the next export."""
    path = tmp_path / 'claude-code.log'
    path.write_text('\n'.join([*DEBUG_LOG, finished('2026-10-21T08:30:00Z', 'extract_memories', create=12)]),
                    encoding='utf-8')
    found = web.read_debug_log(str(path))
    assert [run['t'] for run in found.runs] == ['2026-10-21T07:09:39.057Z', '2026-10-21T08:04:49.531Z']
    assert found.last == '2026-10-21T08:26:37.479Z'
