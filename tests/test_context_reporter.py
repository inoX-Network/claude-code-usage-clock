"""Tests for hooks/context_reporter.py: the line about the context window, every reason to stay silent, the two
events, the real hook process.

The only source is the per-session state file of the status line. Every test through the real script checks the
exact output key: a wrong key would silently do nothing in Claude Code.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import _config
import _context_state
import context_reporter
import statusline
from conftest import HOOKS

SCRIPT = os.path.join(HOOKS, 'context_reporter.py')
UTC = timezone.utc
NOW = datetime(2026, 1, 14, 10, 5, tzinfo=UTC)
CFG = _config.DEFAULTS
SID = 'abc-123_X'
PROMPT = {'hook_event_name': 'UserPromptSubmit', 'session_id': SID, 'prompt': 'hello'}
TOOL = {'hook_event_name': 'PostToolUse', 'session_id': SID, 'tool_name': 'Read', 'tool_input': {}}
LINE = 'Context: 168k/1000k tokens (17%). Measured, not estimated.'


def cfg_with(**reporter):
    cfg = json.loads(json.dumps(CFG))
    cfg['context_reporter'].update(reporter)
    return cfg


def state_dir(tmp_path):
    return tmp_path / 'log' / 'context'


def save(tmp_path, session_id=SID, size=1_000_000, used=168_000, now=NOW):
    """The file as the status line writes it (the real writer), measured at `now`."""
    _context_state.save(str(state_dir(tmp_path)), session_id, size, used, now.timestamp())


def write_state(tmp_path, content, session_id=SID):
    """A state file with exactly this content (text or JSON-able), for the broken cases."""
    state_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    path = state_dir(tmp_path) / f'{session_id}.json'
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')
    return path


def state(**fields):
    base = {'session_id': SID, 'context_window_size': 1_000_000, 'used_tokens': 168_000,
            'measured_at': '2026-01-14T10:05:00Z'}
    base.update(fields)
    return {key: value for key, value in base.items() if value is not ...}     # `...` removes the field


def build(tmp_path, hook_input=PROMPT, cfg=CFG, now=NOW):
    return context_reporter.build_context(hook_input, cfg, str(state_dir(tmp_path)), now)


def run_raw(script, stdin, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    r = subprocess.run([sys.executable, str(script)], input=stdin.encode('utf-8'), capture_output=True, env=env,
                       timeout=20)
    return r.returncode, r.stdout.decode('utf-8'), r.stderr.decode('utf-8')


def run_hook(hook_input, env_extra=None):
    """(event, context text) of the hook, or None when it printed nothing. Asserts exit 0 and the key layout."""
    code, out, err = run_raw(SCRIPT, hook_input if isinstance(hook_input, str) else json.dumps(hook_input), env_extra)
    assert code == 0, err
    if not out.strip():
        return None
    data = json.loads(out)
    assert list(data) == ['hookSpecificOutput']
    assert list(data['hookSpecificOutput']) == ['hookEventName', 'additionalContext']
    return data['hookSpecificOutput']['hookEventName'], data['hookSpecificOutput']['additionalContext']


def fresh_state(tmp_path, **kwargs):
    """A state file that is fresh for the real clock, as the process sees it."""
    save(tmp_path, now=datetime.now(UTC), **kwargs)


# --- The line ------------------------------------------------------------------------------------------------

def test_a_fresh_file_gives_the_documented_line(tmp_path):
    save(tmp_path)
    assert build(tmp_path) == ('UserPromptSubmit', LINE)


def test_the_event_name_in_the_answer_is_the_event_of_the_input(tmp_path):
    save(tmp_path, used=600_000)
    assert build(tmp_path, PROMPT)[0] == 'UserPromptSubmit'
    assert build(tmp_path, TOOL)[0] == 'PostToolUse'


@pytest.mark.parametrize('used,size,text', [
    (168_000, 1_000_000, '168k/1000k tokens (17%)'),
    (168_999, 1_000_000, '168k/1000k tokens (17%)'),          # tokens are cut, never rounded up
    (169_000, 1_000_000, '169k/1000k tokens (17%)'),
    (999_999, 1_000_000, '999k/1000k tokens (100%)'),         # the percentage is rounded, the tokens are not
    (125_000, 1_000_000, '125k/1000k tokens (12%)'),          # 12.5 rounds half to even
    (135_000, 1_000_000, '135k/1000k tokens (14%)'),          # 13.5 rounds to 14
    (500, 200_000, '0k/200k tokens (0%)'),
    (1_000, 200_000, '1k/200k tokens (0%)'),
    (123_456, 200_000, '123k/200k tokens (62%)'),
    (1_000_000, 1_000_000, '1000k/1000k tokens (100%)'),
    (50_000, 1_050_000, '50k/1050k tokens (5%)'),
    (7, 1_000, '0k/1k tokens (1%)'),
])
def test_the_numbers_are_cut_and_rounded_exactly_like_this(tmp_path, used, size, text):
    save(tmp_path, size=size, used=used)
    assert build(tmp_path)[1] == f'Context: {text}. Measured, not estimated.'


@pytest.mark.parametrize('used,size', [(168_000, 1_000_000), (168_999, 1_000_000), (125_000, 1_000_000),
                                       (135_000, 1_000_000), (999_999, 1_000_000), (1_000, 200_000),
                                       (123_456, 200_000), (199_999, 200_000), (5_000, 1_000_000),
                                       (7, 1_000), (333_333, 1_000_000), (66_666, 200_000)])
def test_status_line_and_reporter_show_the_same_numbers(tmp_path, used, size):
    """The status line feeds the state file; both must name the same k and the same percentage."""
    status = {'context_window': {'total_input_tokens': used, 'context_window_size': size,
                                 'used_percentage': used * 100 / size}}
    shown = re.search(r'ctx (\d+)k/(\d+)k (\d+)%', statusline.render('m', None, None, CFG, status, 0.0))
    save(tmp_path, size=size, used=used)
    assert build(tmp_path)[1] == f'Context: {shown.group(1)}k/{shown.group(2)}k tokens ({shown.group(3)}%). Measured, not estimated.'


def test_floats_in_the_file_are_cut_to_whole_tokens(tmp_path):
    write_state(tmp_path, state(used_tokens=168_999.9, context_window_size=1e6))
    assert build(tmp_path)[1] == LINE


# --- Which events report -------------------------------------------------------------------------------------

def test_a_prompt_always_reports_even_at_one_percent(tmp_path):
    save(tmp_path, used=10_000)
    assert build(tmp_path, PROMPT) == ('UserPromptSubmit', 'Context: 10k/1000k tokens (1%). Measured, not estimated.')


@pytest.mark.parametrize('used,reports', [(499_999, False), (500_000, True), (500_001, True), (10_000, False),
                                          (1_000_000, True)])
def test_after_a_tool_it_reports_from_exactly_50_percent(tmp_path, used, reports):
    save(tmp_path, used=used)
    assert (build(tmp_path, TOOL) is not None) is reports


def test_the_limit_is_compared_with_the_exact_share_not_with_the_rounded_one(tmp_path):
    """499 999 of 1 000 000 is shown as 50 %, but is below 50: no report. The line stays true to the measurement."""
    save(tmp_path, used=499_999)
    assert build(tmp_path, TOOL) is None
    assert build(tmp_path, PROMPT)[1].startswith('Context: 499k/1000k tokens (50%)')


@pytest.mark.parametrize('limit,used,reports', [(80, 799_999, False), (80, 800_000, True), (0, 1_000, True),
                                               (100, 999_999, False), (100, 1_000_000, True),
                                               (12.5, 124_999, False), (12.5, 125_000, True)])
def test_the_limit_after_a_tool_comes_from_the_config(tmp_path, limit, used, reports):
    save(tmp_path, used=used)
    assert (build(tmp_path, TOOL, cfg_with(after_tool_from=limit)) is not None) is reports


def test_the_limit_does_not_apply_to_the_prompt(tmp_path):
    save(tmp_path, used=10_000)
    assert build(tmp_path, PROMPT, cfg_with(after_tool_from=100)) is not None


@pytest.mark.parametrize('event', ['Stop', 'SessionStart', 'PreToolUse', 'SubagentStop', 'posttooluse', '', None, 5,
                                   ['PostToolUse']])
def test_another_event_stays_silent(tmp_path, event):
    save(tmp_path, used=900_000)
    assert build(tmp_path, dict(TOOL, hook_event_name=event)) is None


def test_no_event_at_all_stays_silent(tmp_path):
    save(tmp_path, used=900_000)
    assert build(tmp_path, {'session_id': SID}) is None


# --- Subagents -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize('agent_id', ['a1b2c3', '', None, 0])
def test_a_subagent_gets_nothing_on_any_event(tmp_path, agent_id):
    """The state file is the context of the main session, not the one of the subagent."""
    save(tmp_path, used=900_000)
    assert build(tmp_path, dict(TOOL, agent_id=agent_id)) is None
    assert build(tmp_path, dict(PROMPT, agent_id=agent_id)) is None


# --- The session id decides which file is read ---------------------------------------------------------------

@pytest.mark.parametrize('session_id', ['../x', '..', '.', '', 'a/b', 'a\\b', 'a b', 'a.json', 'x' * 129, 'ä', 'a\n',
                                        '../../etc/passwd', '/abs', 'a\x00b', None, 5, ['a'], True])
def test_an_invalid_session_id_stays_silent(tmp_path, session_id):
    save(tmp_path)
    assert build(tmp_path, dict(PROMPT, session_id=session_id)) is None


def test_a_session_id_with_path_tricks_never_reaches_a_file_outside_the_folder(tmp_path):
    """`../x` would resolve to <log>/x.json, a valid state file one level up: it must not be read."""
    (tmp_path / 'log').mkdir()
    (tmp_path / 'log' / 'x.json').write_text(json.dumps(state(session_id='../x')), encoding='utf-8')
    assert build(tmp_path, dict(PROMPT, session_id='../x')) is None


def test_a_missing_session_id_stays_silent(tmp_path):
    save(tmp_path)
    assert build(tmp_path, {'hook_event_name': 'UserPromptSubmit'}) is None


def test_the_longest_valid_session_id_works(tmp_path):
    save(tmp_path, session_id='s' * 128)
    assert build(tmp_path, dict(PROMPT, session_id='s' * 128)) == ('UserPromptSubmit', LINE)


def test_a_file_that_names_another_session_stays_silent(tmp_path):
    write_state(tmp_path, state(session_id='someone-else'))
    assert build(tmp_path) is None


@pytest.mark.parametrize('value', [..., None, 5, ['abc-123_X'], 'ABC-123_X'])
def test_a_file_without_a_matching_session_id_field_stays_silent(tmp_path, value):
    write_state(tmp_path, state(session_id=value))
    assert build(tmp_path) is None


def test_two_sessions_at_once_each_get_their_own_numbers(tmp_path):
    save(tmp_path, session_id='one', size=1_000_000, used=111_000)
    save(tmp_path, session_id='two', size=200_000, used=22_000)
    first = build(tmp_path, dict(PROMPT, session_id='one'))
    second = build(tmp_path, dict(PROMPT, session_id='two'))
    assert first[1] == 'Context: 111k/1000k tokens (11%). Measured, not estimated.'
    assert second[1] == 'Context: 22k/200k tokens (11%). Measured, not estimated.'
    save(tmp_path, session_id='one', size=1_000_000, used=555_000)
    assert build(tmp_path, dict(PROMPT, session_id='one'))[1].startswith('Context: 555k/1000k')
    assert build(tmp_path, dict(PROMPT, session_id='two'))[1].startswith('Context: 22k/200k')


# --- The file itself -----------------------------------------------------------------------------------------

def test_a_missing_file_or_folder_stays_silent(tmp_path):
    assert build(tmp_path) is None
    state_dir(tmp_path).mkdir(parents=True)
    assert build(tmp_path) is None


def test_a_folder_instead_of_the_file_stays_silent(tmp_path):
    (state_dir(tmp_path) / f'{SID}.json').mkdir(parents=True)
    assert build(tmp_path) is None


def test_a_link_instead_of_the_file_is_not_followed(tmp_path):
    other = tmp_path / 'elsewhere.json'
    other.write_text(json.dumps(state()), encoding='utf-8')
    state_dir(tmp_path).mkdir(parents=True)
    os.symlink(other, state_dir(tmp_path) / f'{SID}.json')
    assert build(tmp_path) is None
    other.unlink()                                  # and a dangling one is as silent
    assert build(tmp_path) is None


def test_a_link_instead_of_the_folder_is_not_followed(tmp_path):
    real = tmp_path / 'real-context'
    real.mkdir()
    (real / f'{SID}.json').write_text(json.dumps(state()), encoding='utf-8')
    (tmp_path / 'log').mkdir()
    os.symlink(real, state_dir(tmp_path))
    assert build(tmp_path) is None


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='needs named pipes')
def test_a_named_pipe_instead_of_the_file_stays_silent_and_does_not_hang(tmp_path):
    state_dir(tmp_path).mkdir(parents=True)
    os.mkfifo(state_dir(tmp_path) / f'{SID}.json')
    assert run_hook(PROMPT) is None


@pytest.mark.parametrize('content', ['', 'not json', '{', '[1, 2]', 'null', '"text"', '5', '{"a": ' * 5000,
                                     '\xff\xfe', '{"session_id": "' + 'x' * 100_000 + '"}'])
def test_a_broken_or_odd_file_stays_silent(tmp_path, content):
    write_state(tmp_path, content)
    assert build(tmp_path) is None


def test_a_file_that_is_not_utf8_stays_silent(tmp_path):
    state_dir(tmp_path).mkdir(parents=True)
    (state_dir(tmp_path) / f'{SID}.json').write_bytes(b'\xff\xfe{"session_id": "x"}')
    assert build(tmp_path) is None


@pytest.mark.parametrize('field', ['context_window_size', 'used_tokens'])
@pytest.mark.parametrize('value', [..., None, True, False, 'x', '1000', [1000], {}, 0, -1, -1000,
                                   float('nan'), float('inf'), float('-inf')])
def test_wrong_numbers_stay_silent(tmp_path, field, value):
    write_state(tmp_path, state(**{field: value}))      # json.dumps writes NaN and Infinity, json.load reads them
    assert build(tmp_path) is None


def test_more_used_than_the_window_stays_silent_instead_of_showing_over_100_percent(tmp_path):
    write_state(tmp_path, state(used_tokens=1_000_001))
    assert build(tmp_path) is None
    write_state(tmp_path, state(used_tokens=1_000_000))
    assert build(tmp_path) is not None


def test_a_window_below_1000_stays_silent_like_the_status_line_would(tmp_path):
    write_state(tmp_path, state(context_window_size=999, used_tokens=500))
    assert build(tmp_path) is None
    write_state(tmp_path, state(context_window_size=1000, used_tokens=500))
    assert build(tmp_path)[1] == 'Context: 0k/1k tokens (50%). Measured, not estimated.'


# --- Age of the measurement ----------------------------------------------------------------------------------

def aged(seconds):
    """`now` that is `seconds` after the measurement (negative: before it)."""
    return NOW + timedelta(seconds=seconds)


def test_age_limit_is_15_minutes_and_exactly_15_still_counts(tmp_path):
    save(tmp_path)
    assert build(tmp_path, now=aged(15 * 60)) is not None
    assert build(tmp_path, now=aged(15 * 60 + 1)) is None
    assert build(tmp_path, now=aged(14 * 60 + 59)) is not None


def test_a_measurement_up_to_one_minute_ahead_counts_more_is_silent(tmp_path):
    save(tmp_path)
    assert build(tmp_path, now=aged(-60)) is not None
    assert build(tmp_path, now=aged(-61)) is None
    assert build(tmp_path, now=aged(-3600)) is None


def test_the_age_limit_comes_from_the_config(tmp_path):
    save(tmp_path)
    assert build(tmp_path, cfg=cfg_with(max_age_min=60), now=aged(30 * 60)) is not None
    assert build(tmp_path, cfg=cfg_with(max_age_min=60), now=aged(61 * 60)) is None
    assert build(tmp_path, cfg=cfg_with(max_age_min=5), now=aged(5 * 60)) is not None
    assert build(tmp_path, cfg=cfg_with(max_age_min=5), now=aged(6 * 60)) is None
    assert build(tmp_path, cfg=cfg_with(max_age_min=0.5), now=aged(31)) is None


def test_the_age_limit_is_independent_of_the_usage_threshold(tmp_path):
    save(tmp_path)
    cfg = cfg_with(max_age_min=60)
    cfg['thresholds']['max_age_min'] = 1
    assert build(tmp_path, cfg=cfg, now=aged(30 * 60)) is not None


@pytest.mark.parametrize('measured', [..., None, '', 'yesterday', 5, 1768000000, ['2026-01-14T10:05:00Z'],
                                      '2026-01-14T10:05:00', '2026-13-45T99:99:99Z', '2026-01-14'])
def test_a_missing_or_unreadable_time_stays_silent(tmp_path, measured):
    write_state(tmp_path, state(measured_at=measured))
    assert build(tmp_path) is None


def test_a_time_with_an_offset_is_read_correctly(tmp_path):
    write_state(tmp_path, state(measured_at='2026-01-14T12:05:00+02:00'))
    assert build(tmp_path) is not None
    write_state(tmp_path, state(measured_at='2026-01-14T08:05:00+02:00'))
    assert build(tmp_path) is None


# --- Config in the real process ------------------------------------------------------------------------------

def write_config(tmp_path, content):
    path = tmp_path / 'config.json'
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')
    return {'CCUC_CONFIG': str(path)}


def test_the_hook_process_prints_the_line_for_a_prompt_and_for_a_tool(tmp_path):
    fresh_state(tmp_path, used=600_000)
    assert run_hook(PROMPT) == ('UserPromptSubmit', 'Context: 600k/1000k tokens (60%). Measured, not estimated.')
    assert run_hook(TOOL) == ('PostToolUse', 'Context: 600k/1000k tokens (60%). Measured, not estimated.')


def test_the_hook_process_is_silent_below_the_limit_after_a_tool(tmp_path):
    fresh_state(tmp_path, used=100_000)
    assert run_hook(TOOL) is None
    assert run_hook(PROMPT) is not None


def test_the_hook_process_uses_the_limits_of_the_config_file(tmp_path):
    fresh_state(tmp_path, used=100_000)
    assert run_hook(TOOL, write_config(tmp_path, {'context_reporter': {'after_tool_from': 10}})) is not None
    assert run_hook(TOOL, write_config(tmp_path, {'context_reporter': {'after_tool_from': 11}})) is None
    old = datetime.now(UTC) - timedelta(minutes=30)
    save(tmp_path, now=old)
    assert run_hook(PROMPT) is None
    assert run_hook(PROMPT, write_config(tmp_path, {'context_reporter': {'max_age_min': 60}})) is not None


def test_the_log_folder_comes_from_the_config_without_the_environment_variable(tmp_path, monkeypatch):
    monkeypatch.delenv('CCUC_LOG_DIR')
    other = tmp_path / 'other-log'
    _context_state.save(str(other / 'context'), SID, 1_000_000, 168_000, datetime.now(UTC).timestamp())
    assert run_hook(PROMPT, write_config(tmp_path, {'log_dir': str(other)}))[1] == LINE
    assert run_hook(PROMPT) is None            # the default folder under the isolated HOME is empty


@pytest.mark.parametrize('content', ['{', '[]', '"x"', '{"context_reporter": {"after_tool_from": "x", "max_age_min": -1}}',
                                     '{"context_reporter": {"after_tool_from": 500, "max_age_min": 0}}',
                                     '{"context_reporter": 5}'])
def test_a_broken_config_means_the_defaults_and_logs_nothing_on_every_tool_call(tmp_path, content):
    fresh_state(tmp_path, used=499_000)
    env = write_config(tmp_path, content)
    assert run_hook(TOOL, env) is None                    # default 50 %
    fresh_state(tmp_path, used=500_000)
    assert run_hook(TOOL, env)[1].startswith('Context: 500k/1000k')
    assert not (tmp_path / 'log' / 'log.jsonl').exists()


# --- Robustness: never blocks, never fails -------------------------------------------------------------------

@pytest.mark.parametrize('stdin', ['', 'not json', '[1, 2]', 'null', '5', '{', '{"hook_event_name": "UserPromptSubmit"}',
                                   '{"hook_event_name": 1}'])
def test_broken_or_odd_input_stays_silent_with_exit_0(tmp_path, stdin):
    fresh_state(tmp_path)
    assert run_hook(stdin) is None


def test_missing_sibling_files_give_exit_0_and_no_output(tmp_path):
    fresh_state(tmp_path)
    alone = tmp_path / 'alone'
    alone.mkdir()
    shutil.copy(SCRIPT, alone / 'context_reporter.py')
    assert run_raw(alone / 'context_reporter.py', json.dumps(PROMPT)) == (0, '', '')


def test_broken_sibling_files_give_exit_0_and_no_output(tmp_path):
    fresh_state(tmp_path)
    for broken in ('_config.py', '_hookio.py', '_context_state.py', '_usage.py'):
        folder = tmp_path / ('broken' + broken)
        folder.mkdir()
        for name in ('context_reporter.py', '_config.py', '_usage.py', '_hookio.py', '_context_state.py'):
            shutil.copy(os.path.join(HOOKS, name), folder / name)
        (folder / broken).write_text('def (:\n', encoding='utf-8')
        assert run_raw(folder / 'context_reporter.py', json.dumps(PROMPT)) == (0, '', ''), broken


def call_main(monkeypatch, capsys, stdin):
    monkeypatch.setattr(sys, 'stdin', io.StringIO(stdin if isinstance(stdin, str) else json.dumps(stdin)))
    code = context_reporter.main()
    return code, capsys.readouterr().out


def test_main_prints_one_json_line_with_the_context(monkeypatch, capsys, tmp_path):
    fresh_state(tmp_path)
    code, out = call_main(monkeypatch, capsys, PROMPT)
    assert code == 0 and out.count('\n') == 1
    assert json.loads(out) == {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': LINE}}


def test_main_prints_nothing_for_a_subagent(monkeypatch, capsys, tmp_path):
    fresh_state(tmp_path)
    assert call_main(monkeypatch, capsys, dict(PROMPT, agent_id='x')) == (0, '')


@pytest.mark.parametrize('broken', ['load', 'build', 'log_dir', 'read_input'])
def test_any_internal_error_gives_exit_0_and_no_output(monkeypatch, capsys, tmp_path, broken):
    import _hookio

    def boom(*_, **__):
        raise RuntimeError('boom')
    fresh_state(tmp_path)
    if broken == 'load':
        monkeypatch.setattr(_config, 'load', boom)
    elif broken == 'build':
        monkeypatch.setattr(context_reporter, 'build_context', boom)
    elif broken == 'log_dir':
        monkeypatch.setattr(_hookio, 'log_dir', boom)
    else:
        monkeypatch.setattr(_hookio, 'read_input', boom)
    assert call_main(monkeypatch, capsys, PROMPT) == (0, '')


@pytest.mark.skipif(not hasattr(os, 'geteuid') or os.geteuid() == 0, reason='root can read everything')
def test_a_file_that_cannot_be_read_stays_silent(tmp_path):
    path = write_state(tmp_path, state())
    path.chmod(0)
    try:
        assert build(tmp_path) is None
    finally:
        path.chmod(0o600)
    assert build(tmp_path) is not None


def test_an_error_while_reading_an_opened_file_stays_silent_and_closes_it(monkeypatch, tmp_path):
    save(tmp_path)
    closed = []
    real_close = os.close

    def boom(*_):
        raise OSError('input/output error')

    def tracking_close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)
    monkeypatch.setattr(os, 'read', boom)
    monkeypatch.setattr(os, 'close', tracking_close)
    assert build(tmp_path) is None
    assert len(closed) == 1


def test_the_hook_does_not_read_a_transcript_even_when_the_input_names_one(tmp_path):
    fresh_state(tmp_path)
    transcript = tmp_path / 'transcript.jsonl'
    transcript.write_text('{"usage": {"input_tokens": 999999}}\n', encoding='utf-8')
    context = run_hook(dict(PROMPT, transcript_path=str(transcript)))[1]
    assert context == LINE
