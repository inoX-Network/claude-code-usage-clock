"""Tests for hooks/soft_stop.py: the decision logic, the per-agent counter and the real hook as a subprocess.

The subprocess tests send JSON in and check the exit code and the exact output keys: a wrong key would
stay silently ineffective in Claude Code.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import _config
import _usage
import soft_stop
from conftest import HOOKS

SCRIPT = os.path.join(HOOKS, 'soft_stop.py')
CFG = _config.DEFAULTS
NOW = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
CUSTOM = '/work/notes'
SUFFIXES = ['/agent-checkpoints', CUSTOM]


def config_with(**changes):
    cfg = copy.deepcopy(CFG)
    for key, value in changes.items():
        if key == 'thresholds':
            cfg['thresholds'].update(value)
        else:
            cfg[key] = value
    return cfg


def usage(five, age=1.0, week=20.0):
    return _usage.Usage(five, week, age, max_age_min=15.0)


UNKNOWN = _usage.Usage(None, None, None)
READ = ('Read', {'file_path': '/srv/app/a.py'})


def save(suffix='/agent-checkpoints'):
    return ('Write', {'file_path': f'/srv/proj{suffix}/a.md'})


def decide(five, tool=READ, since=0, has=False, cfg=CFG, age=1.0):
    return soft_stop.decide(usage(five, age), tool[0], tool[1], since, has, cfg)


# --- Levels ----------------------------------------------------------------------------------------

def test_below_the_warning_level_allows():
    assert decide(84.9) == soft_stop.Decision('allow')
    assert decide(0) == soft_stop.Decision('allow')


def test_warning_from_the_warn_level_without_denial():
    d = decide(85)
    assert d.action == 'warn' and 'checkpoint' in d.reason and '92 %' in d.reason
    assert decide(91.9).action == 'warn'


def test_from_checkpoint_only_everything_but_a_checkpoint_write_is_denied():
    assert decide(92).action == 'deny'
    assert decide(94.9).action == 'deny'
    assert decide(92, save()).action == 'allow'
    assert decide(94.9, ('Bash', {'command': 'ls'})).action == 'deny'


def test_from_deny_level_the_reason_tells_the_model_to_finish_with_a_partial_result():
    d = decide(99)
    assert d.action == 'deny'
    assert 'StructuredOutput' in d.reason and 'partial result' in d.reason and 'incomplete' in d.reason
    assert 'no further tool calls' in d.reason.lower()
    assert decide(95).action == 'deny'
    assert 'finish immediately' in decide(95).reason and 'finish immediately' not in decide(94.9).reason
    assert 'Only saving a checkpoint is still allowed' in decide(94.9).reason
    assert decide(99, save()).action == 'allow'


def test_levels_and_counter_limit_come_from_the_config():
    cfg = config_with(thresholds={'warn': 50, 'checkpoint_only': 60, 'deny': 70, 'calls_without_checkpoint': 3})
    assert decide(49.9, cfg=cfg).action == 'allow'
    warn = decide(50, cfg=cfg)
    assert warn.action == 'warn' and '60 %' in warn.reason
    only = decide(60, cfg=cfg)
    assert only.action == 'deny' and '60 %' in only.reason
    deny = decide(70, cfg=cfg)
    assert deny.action == 'deny' and '70 %' in deny.reason
    assert decide(10, since=3, has=True, cfg=cfg).action == 'deny'
    assert decide(10, since=2, has=True, cfg=cfg).action == 'allow'


def test_completion_tool_is_never_denied():
    assert 'StructuredOutput' in soft_stop.COMPLETION_TOOLS
    for five in (10, 86, 93, 96, 100):
        assert decide(five, ('StructuredOutput', {'result': 'x'})).action == 'allow', five
    assert decide(10, ('StructuredOutput', {}), since=15, has=True).action == 'allow'


# --- Counter rule ------------------------------------------------------------------------------------

def test_counter_forces_a_checkpoint_only_after_opt_in():
    d = decide(10, since=15, has=True)
    assert d.action == 'deny' and 'Checkpoint due' in d.reason and '15 calls' in d.reason
    assert decide(10, since=15, has=False).action == 'allow'
    assert decide(10, since=14, has=True).action == 'allow'
    assert decide(10, save(), since=99, has=True).action == 'allow'


def test_counter_denial_beats_the_warning_level():
    assert decide(90, since=15, has=True).action == 'deny'


def test_a_forged_checkpoint_does_not_reset_the_due_checkpoint():
    d = decide(10, ('Bash', {'command': 'echo "see >/agent-checkpoints/"'}), since=15, has=True)
    assert d.action == 'deny'


# --- Unknown and stale measurements --------------------------------------------------------------------

def test_unknown_usage_warns_on_the_first_call_only_and_never_denies():
    d = soft_stop.decide(UNKNOWN, *READ, 0, False, CFG)
    assert d.action == 'warn' and 'fail open' in d.reason and 'unknown' in d.reason
    assert soft_stop.decide(UNKNOWN, *READ, 3, False, CFG).action == 'allow'
    assert soft_stop.decide(UNKNOWN, *READ, 0, True, CFG).action == 'allow'


def test_stale_usage_never_triggers_the_stop_levels():
    d = decide(99, age=60)
    assert d.action == 'warn' and 'stale' in d.reason
    assert decide(99, age=60, since=7, has=True).action == 'allow'
    assert decide(99, age=-30).action == 'warn'  # a timestamp from the future counts as stale


def test_counter_still_applies_when_usage_is_unknown():
    assert soft_stop.decide(UNKNOWN, *READ, 15, True, CFG).action == 'deny'


# --- What counts as a checkpoint write ----------------------------------------------------------------------

def bash(command):
    return {'command': command}


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_checkpoint_writes_are_recognised(suffix):
    z = f'/srv/proj{suffix}/x.md'
    for tool in ('Write', 'Edit', 'MultiEdit'):
        assert soft_stop.is_checkpoint_write(tool, {'file_path': z}, suffix)
    assert soft_stop.is_checkpoint_write('Bash', bash(f"cat >> {z} <<'EOF'\n- item 3 done\nEOF"), suffix)
    assert soft_stop.is_checkpoint_write('Bash', bash(f"cat >> {z} <<'EOF'\n- item 3 done\nEOF\n"), suffix)
    assert soft_stop.is_checkpoint_write('Bash', bash(f"cat >> {z} <<-\"END\"\n\tline\nEND"), suffix)
    assert soft_stop.is_checkpoint_write('Bash', bash(f"echo 'new' | tee -a {z}"), suffix)
    assert soft_stop.is_checkpoint_write('Bash', bash(f"echo 'new' | tee --append {z}"), suffix)
    assert soft_stop.is_checkpoint_write('Bash', bash(f"echo 'new' >> {z}"), suffix)
    assert soft_stop.is_checkpoint_write('Bash', bash(f'echo "no dollar sign" >> {z}'), suffix)
    assert soft_stop.is_checkpoint_write('Bash', bash(f"printf '%s\\n' \"line\" >> '/srv/proj{suffix}/a b.md'"), suffix)
    assert soft_stop.is_checkpoint_write('Bash', bash(f'  echo \'x\' >> "{z}"  \n'), suffix)
    assert soft_stop.is_checkpoint_write('Bash', bash(f"printf 'plain text\\n' >> {z}"), suffix)
    assert soft_stop.is_checkpoint_write('Bash', bash(f"printf '%-10s|%b|%%\\n' 'a' \"b\" >> {z}"), suffix)
    assert soft_stop.is_checkpoint_write('Bash', bash(f"printf \"%s\" '-v' | tee -a {z}"), suffix)  # not the format
    assert soft_stop.is_checkpoint_write('Bash', bash(f"echo '- item 3 done' >> {z}"), suffix)   # echo runs no code
    assert soft_stop.is_checkpoint_write('Bash', bash(f"echo '-n' 'x' >> {z}"), suffix)


def test_other_calls_are_not_checkpoint_writes():
    assert not soft_stop.is_checkpoint_write('Write', {'file_path': '/srv/proj/src/app.ts'})
    assert not soft_stop.is_checkpoint_write('Bash', bash('cat /srv/proj/agent-checkpoints/notes.md'))
    assert not soft_stop.is_checkpoint_write('Read', {'file_path': '/srv/proj/agent-checkpoints/notes.md'})
    assert not soft_stop.is_checkpoint_write('Glob', {'file_path': '/srv/proj/agent-checkpoints/notes.md'})
    assert not soft_stop.is_checkpoint_write('Write', {})
    assert not soft_stop.is_checkpoint_write('Bash', {})


def forgeries(suffix):
    """Commands that look like a checkpoint write but are something else, or do more."""
    z = f'/srv/proj{suffix}/x.md'
    return [
        f'rm -rf /tmp/scratch/junk  # >{suffix}/',
        f'rm -rf /tmp/scratch/junk  # >> {z}',
        f'ls; echo ok >> {z}',
        f"echo 'x' >> {z}; rm -rf /tmp/a",
        f"echo 'x' >> {z} && rm -rf /tmp/a",
        f"echo 'x' >> {z} || rm -rf /tmp/a",
        f'python3 long_run.py 2>/srv/proj{suffix}/log',
        f'python3 long_analysis.py > {z}',
        f'echo "see >{suffix}/"',
        f'echo "$(rm -rf /tmp/a)" >> {z}',
        f'echo `id` >> {z}',
        f'echo x $HOME >> {z}',
        f"echo 'x' > {z}",
        f'cat /tmp/secret | tee -a {z}',
        f"echo 'x' | tee -a {z} | sh",
        f"cat >> {z} <<EOF\n$(rm -rf /tmp/a)\nEOF",
        f"cat >> {z} <<'EOF' && rm -rf /tmp/a\nx\nEOF",
        f"cat >> {z} <<'EOF'\nx\nEOF\nrm -rf /tmp/a\nEOF",
        f"cat >> {z} <<'EOF'\nx\nEOF\nrm -rf /tmp/a",
        f"echo 'x' >> /srv/proj{suffix}/../../src/app.py",
        f"echo 'x' >> {suffix.lstrip('/')}/x.md",
        f"echo 'x' >> ~{suffix}/x.md",
        # printf -v assigns and runs $(...) in the array subscript (bash and zsh), quoted or not
        f"printf '-v' 'a[$(touch /tmp/pwned)]' 'x' >> {z}",
        f'printf "-v" \'a[$(touch /tmp/pwned)]\' \'x\' >> {z}',
        f"printf '-v' 'a[$(touch /tmp/pwned)]' 'x' | tee -a {z}",
        f"printf '--' 'x' >> {z}",
        # zsh evaluates the argument of a numeric conversion or of %n as arithmetic, $(...) included
        f"printf '%d' 'path[$(touch /tmp/pwned)]' >> {z}",
        f"printf '%s%x' 'x' 'argv[$(touch /tmp/pwned)]' >> {z}",
        f"printf '%*s' 'path[$(touch /tmp/pwned)]' 'x' >> {z}",
        f"printf '%.*s' 'path[$(touch /tmp/pwned)]' 'x' >> {z}",
        f"printf '%n' 'path[$(touch /tmp/pwned)]' >> {z}",
        f"printf '%f' 'path[$(touch /tmp/pwned)]' | tee -a {z}",
        f"printf '100%' >> {z}",
    ]


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_forged_checkpoints_are_not_checkpoints_and_are_denied_at_the_deny_level(suffix):
    cfg = config_with(checkpoint_dir_suffix=suffix)
    for command in forgeries(suffix):
        assert not soft_stop.is_checkpoint_write('Bash', bash(command), suffix), command
        assert decide(96, ('Bash', bash(command)), cfg=cfg).action == 'deny', command


def bad_paths(suffix):
    tail = suffix.lstrip('/')
    return [f'/srv/proj{suffix}/../../src/app.py', f'/srv/proj{suffix}', f'/srv/proj{suffix}-new/x.md',
            f'/srv/proj{suffix}/sub/x.md', f'/srv/proj/my{tail}/x.md', f'{tail}/x.md', '', None, 5, ['x'],
            f'proj{suffix}/x.md',  # relative, although the parent directory ends with the suffix
            f'/srv/proj{suffix}/../{tail}/x.md']  # a detour through '..' is refused even if it ends up inside


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_bad_paths_are_not_checkpoints(suffix):
    for path in bad_paths(suffix):
        for tool in ('Write', 'Edit', 'MultiEdit'):
            assert not soft_stop.is_checkpoint_write(tool, {'file_path': path}, suffix), (tool, path)
    assert not soft_stop.is_checkpoint_write('Write', {'file_path': f'/srv/{suffix}/x\0.md'}, suffix)
    assert not soft_stop.is_checkpoint_write('Bash', {'command': ['echo', 'x']}, suffix)


def test_a_multi_level_suffix_needs_all_its_levels():
    assert not soft_stop.is_checkpoint_write('Write', {'file_path': '/var/notes/x.md'}, CUSTOM)
    assert not soft_stop.is_checkpoint_write('Write', {'file_path': '/usr/lib/notes/x.py'}, CUSTOM)
    assert not soft_stop.is_checkpoint_write('Write', {'file_path': '/srv/proj/agent-checkpoints/x.md'}, CUSTOM)
    assert soft_stop.is_checkpoint_write('Write', {'file_path': '/srv/proj/work/notes/x.md'}, CUSTOM)


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_a_checkpoint_directory_that_is_a_symlink_to_elsewhere_is_no_checkpoint(tmp_path, suffix):
    target = tmp_path / 'src'
    target.mkdir()
    link = tmp_path / suffix.lstrip('/')
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    assert not soft_stop.is_checkpoint_write('Write', {'file_path': str(link / 'app.py')}, suffix)
    assert soft_stop.is_checkpoint_write('Write', {'file_path': str(tmp_path / 'other' / suffix.lstrip('/') / 'a.md')}, suffix)


def test_the_default_suffix_is_used_when_no_configured_one_is_given():
    assert soft_stop.is_checkpoint_write('Write', {'file_path': '/srv/proj/agent-checkpoints/a.md'})
    assert not soft_stop.is_checkpoint_write('Write', {'file_path': f'/srv/proj{CUSTOM}/a.md'})


def test_the_configured_suffix_replaces_the_default():
    cfg = config_with(checkpoint_dir_suffix=CUSTOM)
    assert decide(96, ('Write', {'file_path': f'/srv/proj{CUSTOM}/a.md'}), cfg=cfg).action == 'allow'
    assert decide(96, ('Write', {'file_path': '/srv/proj/agent-checkpoints/a.md'}), cfg=cfg).action == 'deny'


@pytest.mark.parametrize('value,expected', [('/agent-checkpoints', '/agent-checkpoints'), ('agent-checkpoints', '/agent-checkpoints'),
                                            ('/agent-checkpoints/', '/agent-checkpoints'), (' /work/notes ', '/work/notes'),
                                            ('work/notes/', '/work/notes'), ('/checkpoints', '/checkpoints'),
                                            ('', '/agent-checkpoints'), ('/', '/agent-checkpoints'), ('  ', '/agent-checkpoints'),
                                            (None, '/agent-checkpoints'), (5, '/agent-checkpoints'),
                                            ('a\0b', '/agent-checkpoints')])
def test_suffix_normalisation(value, expected):
    assert soft_stop.normalize_suffix(value) == expected


def test_an_empty_suffix_does_not_turn_every_path_into_a_checkpoint():
    cfg = config_with(checkpoint_dir_suffix='')
    assert decide(96, ('Write', {'file_path': '/srv/app/src/main.py'}), cfg=cfg).action == 'deny'
    assert decide(96, ('Write', {'file_path': '/srv/proj/agent-checkpoints/a.md'}), cfg=cfg).action == 'allow'


# --- Messages tell the model the exact way to save -----------------------------------------------------------------

@pytest.mark.parametrize('suffix', SUFFIXES)
def test_denials_name_the_configured_save_form(suffix):
    cfg = config_with(checkpoint_dir_suffix=suffix)
    for d in (decide(93, cfg=cfg), decide(96, cfg=cfg), decide(10, since=15, has=True, cfg=cfg)):
        assert d.action == 'deny'
        assert f'<absolute path>{suffix}/<file>' in d.reason
        assert 'cat >>' in d.reason and "<<'EOF'" in d.reason and 'tee -a' in d.reason and 'Write/Edit' in d.reason
    other = '/agent-checkpoints' if suffix != '/agent-checkpoints' else CUSTOM
    assert other not in decide(96, cfg=cfg).reason


def test_the_messages_are_english_and_free_of_internal_wording():
    texts = [decide(93).reason, decide(96).reason, decide(86).reason, decide(10, since=15, has=True).reason,
             soft_stop.decide(UNKNOWN, *READ, 0, False, CFG).reason]
    for text in texts:
        assert text.isascii(), text


# --- Counter ------------------------------------------------------------------------------------------------------------

def test_counter_counts_and_resets_at_a_checkpoint(tmp_path):
    c = soft_stop.Counter(str(tmp_path))
    assert c.read('agent-1') == (0, False)
    for _ in range(3):
        c.book('agent-1', was_checkpoint=False)
    assert c.read('agent-1') == (3, False)
    c.book('agent-1', was_checkpoint=True)
    assert c.read('agent-1') == (0, True)
    c.book('agent-1', was_checkpoint=False)
    assert c.read('agent-1') == (1, True)


def test_counter_keeps_agents_apart(tmp_path):
    c = soft_stop.Counter(str(tmp_path))
    c.book('a', True)
    c.book('a', False)
    assert c.read('a') == (1, True) and c.read('b') == (0, False)


@pytest.mark.parametrize('content', ['{broken', '[1, 2]', '"x"', 'null', '{"since": "x"}', '{"since": null}', '', '[' * 100000])
def test_counter_survives_broken_files(tmp_path, content):
    (tmp_path / 'x.json').write_text(content, encoding='utf-8')
    c = soft_stop.Counter(str(tmp_path))
    assert c.read('x') == (0, False)
    c.book('x', False)
    assert c.read('x') == (1, False)


@pytest.mark.parametrize('agent_id', ['../../etc/passwd', '..', '.', 'a/../../b', '/abs/path', 'a\0b', 'x' * 500,
                                      'ü/ß', 'a b\nc'])
def test_counter_file_can_never_leave_its_directory(tmp_path, agent_id):
    directory = tmp_path / 'counters'
    c = soft_stop.Counter(str(directory))
    c.book(agent_id, False)
    assert c.read(agent_id) == (1, False)
    path = c._path(agent_id)
    assert os.path.dirname(path) == str(directory)
    assert len(os.path.basename(path)) <= 125
    assert not (tmp_path / 'etc').exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['counters']
    assert all(name.endswith('.json') for name in os.listdir(directory))


def test_counter_leaves_no_temporary_file_behind(tmp_path):
    c = soft_stop.Counter(str(tmp_path))
    for _ in range(3):
        c.book('agent-1', False)
    assert os.listdir(tmp_path) == ['agent-1.json']


# --- The real hook as a subprocess ----------------------------------------------------------------------------------------

def write_usage(tmp_path, five, week=20, age_min=1):
    now = datetime.now(timezone.utc)
    block = lambda value: None if value is None else {'used_percentage': value, 'resets_at': int(now.timestamp()) + 3600}
    measured = (now - timedelta(minutes=age_min)).strftime('%Y-%m-%dT%H:%M:%SZ')
    (tmp_path / 'usage.json').write_text(json.dumps({'five_hour': block(five), 'seven_day': block(week),
                                                    'measured_at': measured}), encoding='utf-8')


def call(tool='Read', agent_id='agent-1', **tool_input):
    return {'hook_event_name': 'PreToolUse', 'tool_name': tool, 'tool_input': tool_input or {'file_path': '/srv/app/a.py'},
            'agent_id': agent_id, 'agent_type': 'reviewer'}


def run_raw(script, text, *args):
    r = subprocess.run([sys.executable, str(script), *args], input=text, capture_output=True, text=True, timeout=20)
    return r.returncode, (json.loads(r.stdout) if r.stdout.strip() else None)


def run(payload, *args):
    code, out = run_raw(SCRIPT, json.dumps(payload), *args)
    assert code == 0
    return out


ENFORCE = '--mode=enforce'


def is_deny(out):
    body = out['hookSpecificOutput'] if out else {}
    return body.get('hookEventName') == 'PreToolUse' and body.get('permissionDecision') == 'deny' \
        and bool(body.get('permissionDecisionReason')) and set(body) == {'hookEventName', 'permissionDecision', 'permissionDecisionReason'}


def is_warning(out):
    body = out['hookSpecificOutput'] if out else {}
    return body.get('hookEventName') == 'PreToolUse' and bool(body.get('additionalContext')) \
        and set(body) == {'hookEventName', 'additionalContext'}


def log_lines(tmp_path):
    path = tmp_path / 'log' / 'log.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def counter_file(tmp_path, agent_id='agent-1'):
    return json.loads((tmp_path / 'log' / 'counters' / f'{agent_id}.json').read_text(encoding='utf-8'))


@pytest.fixture
def set_config(tmp_path, monkeypatch):
    def apply(data):
        path = tmp_path / 'config.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        monkeypatch.setenv('CCUC_CONFIG', str(path))
    return apply


def test_main_session_is_never_blocked(tmp_path):
    write_usage(tmp_path, 99)
    main_call = {'tool_name': 'Read', 'tool_input': {'file_path': '/srv/x'}}
    assert run(main_call, ENFORCE) is None
    assert run({**main_call, 'agent_id': ''}, ENFORCE) is None
    assert run({**main_call, 'agent_id': None}, ENFORCE) is None
    assert log_lines(tmp_path) == []


def test_subagent_is_denied_from_the_deny_level_with_the_finish_instruction(tmp_path):
    write_usage(tmp_path, 96)
    out = run(call(), ENFORCE)
    assert is_deny(out)
    reason = out['hookSpecificOutput']['permissionDecisionReason']
    assert 'incomplete' in reason and 'partial result' in reason and '<absolute path>/agent-checkpoints/<file>' in reason


def test_checkpoint_write_is_allowed_at_the_deny_level(tmp_path):
    write_usage(tmp_path, 96)
    assert run(call('Write', file_path='/srv/proj/agent-checkpoints/a.md', content='x'), ENFORCE) is None
    ok = call('Bash', command="cat >> /srv/proj/agent-checkpoints/a.md <<'EOF'\n- item done\nEOF")
    assert run(ok, ENFORCE) is None


def test_forged_checkpoints_are_denied_at_the_deny_level(tmp_path):
    write_usage(tmp_path, 96)
    for command in ('rm -rf /tmp/scratch/junk  # >/agent-checkpoints/', 'ls; echo ok >> /srv/proj/agent-checkpoints/x',
                    'python3 long_run.py 2>/srv/proj/agent-checkpoints/log'):
        assert is_deny(run(call('Bash', command=command), ENFORCE)), command
    assert is_deny(run(call('Write', file_path='/srv/proj/agent-checkpoints/../../src/app.py', content='x'), ENFORCE))


def test_printf_with_an_option_or_a_numeric_conversion_is_denied_and_not_booked(tmp_path):
    write_usage(tmp_path, 97)
    target = '/srv/proj/agent-checkpoints/f.md'
    for command in (f"printf '-v' 'a[$(touch {tmp_path}/pwned)]' 'x' >> {target}",
                    f"printf '%d' 'path[$(touch {tmp_path}/pwned)]' >> {target}"):
        out = run(call('Bash', command=command), ENFORCE)
        assert is_deny(out) and 'Usage pause' in out['hookSpecificOutput']['permissionDecisionReason'], command
    assert not (tmp_path / 'log' / 'counters' / 'agent-1.json').exists()   # a denied call resets no counter


def test_warning_from_the_warn_level_has_the_context_key_and_no_decision(tmp_path):
    write_usage(tmp_path, 86)
    assert is_warning(run(call(), ENFORCE))


def test_between_warn_and_checkpoint_only_the_counter_is_the_only_denial(tmp_path):
    write_usage(tmp_path, 90)
    assert run(call('Write', file_path='/srv/proj/agent-checkpoints/a.md', content='x'), ENFORCE) is None
    for _ in range(15):
        assert is_warning(run(call(), ENFORCE))
    assert is_deny(run(call(), ENFORCE))


def test_counter_forces_a_checkpoint_end_to_end(tmp_path):
    write_usage(tmp_path, 10)
    save_call = call('Write', file_path='/srv/proj/agent-checkpoints/a.md', content='x')
    assert run(save_call, ENFORCE) is None
    for _ in range(15):
        assert run(call(), ENFORCE) is None
    out = run(call(), ENFORCE)
    assert is_deny(out) and 'Checkpoint due' in out['hookSpecificOutput']['permissionDecisionReason']
    assert run(save_call, ENFORCE) is None
    assert run(call(), ENFORCE) is None


def test_denied_calls_are_not_counted(tmp_path):
    write_usage(tmp_path, 10)
    run(call('Write', file_path='/srv/proj/agent-checkpoints/a.md', content='x'), ENFORCE)
    for _ in range(15):
        run(call(), ENFORCE)
    for _ in range(4):
        assert is_deny(run(call(), ENFORCE))
    assert counter_file(tmp_path) == {'since': 15, 'has_checkpoint': True}


def test_counter_file_lives_under_the_log_directory_and_agents_are_kept_apart(tmp_path):
    write_usage(tmp_path, 10)
    run(call('Write', agent_id='a', file_path='/srv/proj/agent-checkpoints/a.md', content='x'), ENFORCE)
    for _ in range(15):
        run(call(agent_id='a'), ENFORCE)
    assert is_deny(run(call(agent_id='a'), ENFORCE))
    assert run(call(agent_id='b'), ENFORCE) is None
    assert sorted(os.listdir(tmp_path / 'log' / 'counters')) == ['a.json', 'b.json']


def test_forged_checkpoint_does_not_reset_the_counter_end_to_end(tmp_path):
    write_usage(tmp_path, 10)
    run(call('Write', file_path='/srv/proj/agent-checkpoints/a.md', content='x'), ENFORCE)
    for _ in range(15):
        run(call(), ENFORCE)
    assert is_deny(run(call('Bash', command='echo "see >/agent-checkpoints/"'), ENFORCE))
    assert counter_file(tmp_path)['since'] == 15


def test_completion_tool_is_allowed_at_the_deny_level_and_when_a_checkpoint_is_due(tmp_path):
    write_usage(tmp_path, 96)
    assert run(call('StructuredOutput', agent_id='x', result='done'), ENFORCE) is None
    write_usage(tmp_path, 10)
    run(call('Write', file_path='/srv/proj/agent-checkpoints/a.md', content='x'), ENFORCE)
    for _ in range(15):
        run(call(), ENFORCE)
    assert is_deny(run(call(), ENFORCE))
    assert run(call('StructuredOutput', result='x'), ENFORCE) is None


def test_unknown_usage_warns_once_per_agent_and_never_denies(tmp_path):
    # no usage file at all
    assert is_warning(run(call(agent_id='new'), ENFORCE))
    assert run(call(agent_id='new'), ENFORCE) is None
    assert is_warning(run(call(agent_id='other'), ENFORCE))


def test_stale_usage_is_fail_open(tmp_path):
    write_usage(tmp_path, 99, age_min=40)
    assert is_warning(run(call(), ENFORCE))
    assert run(call(), ENFORCE) is None


def test_usage_without_a_five_hour_block_is_unknown(tmp_path):
    write_usage(tmp_path, None)
    assert is_warning(run(call(), ENFORCE))


def test_shadow_is_the_default_and_only_logs(tmp_path):
    write_usage(tmp_path, 96)
    assert run(call()) is None
    assert run(call(), '--mode=shadow') is None
    assert run(call(), '--mode=bogus') is None
    lines = log_lines(tmp_path)
    assert [line['decision'] for line in lines] == ['deny', 'deny', 'deny']
    assert all(line['mode'] == 'shadow' and line['hook'] == 'soft_stop' for line in lines)


def test_shadow_still_counts_so_that_switching_to_enforce_continues_the_count(tmp_path):
    write_usage(tmp_path, 10)
    run(call('Write', file_path='/srv/proj/agent-checkpoints/a.md', content='x'))
    for _ in range(15):
        run(call())
    assert is_deny(run(call(), ENFORCE))


def test_log_line_has_the_decision_the_keys_and_the_counter_but_no_values(tmp_path):
    write_usage(tmp_path, 96)
    run(call('Bash', command='echo SECRET-COMMAND-TEXT'), ENFORCE)
    raw = (tmp_path / 'log' / 'log.jsonl').read_text(encoding='utf-8')
    assert 'SECRET-COMMAND-TEXT' not in raw
    line = json.loads(raw.splitlines()[-1])
    assert line['decision'] == 'deny' and line['mode'] == 'enforce' and line['tool_name'] == 'Bash'
    assert line['agent_id'] == 'agent-1' and line['tool_input_keys'] == ['command']
    assert line['calls_since_checkpoint'] == 0 and 'Usage pause' in line['reason']


def test_custom_suffix_from_the_config_end_to_end(tmp_path, set_config):
    set_config({'checkpoint_dir_suffix': CUSTOM})
    write_usage(tmp_path, 96)
    assert run(call('Write', file_path=f'/srv/proj{CUSTOM}/a.md', content='x'), ENFORCE) is None
    ok = call('Bash', command=f"echo 'x' | tee -a /srv/proj{CUSTOM}/a.md")
    assert run(ok, ENFORCE) is None
    out = run(call('Write', file_path='/srv/proj/agent-checkpoints/a.md', content='x'), ENFORCE)
    assert is_deny(out)
    assert f'<absolute path>{CUSTOM}/<file>' in out['hookSpecificOutput']['permissionDecisionReason']


def test_custom_thresholds_from_the_config_end_to_end(tmp_path, set_config):
    set_config({'thresholds': {'warn': 30, 'checkpoint_only': 40, 'deny': 50, 'calls_without_checkpoint': 2}})
    write_usage(tmp_path, 35)
    assert is_warning(run(call(), ENFORCE))
    write_usage(tmp_path, 45)
    assert is_deny(run(call(), ENFORCE))
    write_usage(tmp_path, 10)
    run(call('Write', file_path='/srv/proj/agent-checkpoints/a.md', content='x'), ENFORCE)
    assert run(call(), ENFORCE) is None
    assert run(call(), ENFORCE) is None
    assert is_deny(run(call(), ENFORCE))


def test_max_age_from_the_config_decides_between_ok_and_stale(tmp_path, set_config):
    set_config({'thresholds': {'max_age_min': 60}})
    write_usage(tmp_path, 96, age_min=40)
    assert is_deny(run(call(), ENFORCE))


def test_broken_config_falls_back_to_defaults_and_is_logged(tmp_path, set_config):
    set_config({'checkpoint_dir_suffix': 5, 'thresholds': {'deny': 'high'}, 'bogus': 1})
    write_usage(tmp_path, 96)
    assert run(call('Write', file_path='/srv/proj/agent-checkpoints/a.md', content='x'), ENFORCE) is None
    assert is_deny(run(call(), ENFORCE))
    assert any(line['hook'] == 'config' for line in log_lines(tmp_path))


def test_an_empty_configured_suffix_is_no_free_pass(tmp_path, set_config):
    set_config({'checkpoint_dir_suffix': ''})
    write_usage(tmp_path, 96)
    assert is_deny(run(call('Write', file_path='/srv/app/src/main.py', content='x'), ENFORCE))


@pytest.mark.parametrize('text', ['', '{broken', '[1]', 'null', '"x"', '[' * 100000])
def test_unusable_input_exits_zero_without_output(text):
    assert run_raw(SCRIPT, text, ENFORCE) == (0, None)


def test_odd_but_valid_input_is_handled(tmp_path):
    write_usage(tmp_path, 96)
    assert is_deny(run({'agent_id': 'x'}, ENFORCE))  # no tool name: not a checkpoint, not a completion tool
    assert is_deny(run({'agent_id': 'x', 'tool_name': 'Bash', 'tool_input': 'ls'}, ENFORCE))
    assert is_deny(run({'agent_id': 'x', 'tool_name': 'Write', 'tool_input': {'file_path': ['a']}}, ENFORCE))
    assert is_deny(run({'agent_id': 7, 'tool_name': 'Read'}, ENFORCE))
    assert is_deny(run({'agent_id': '../../evil', 'tool_name': 'Read'}, ENFORCE))
    assert not (tmp_path / 'evil.json').exists()


def test_a_counter_that_cannot_be_written_fails_open_and_is_logged(tmp_path):
    write_usage(tmp_path, 10)
    (tmp_path / 'log').mkdir()
    (tmp_path / 'log' / 'counters').write_text('not a directory', encoding='utf-8')
    assert run(call(), ENFORCE) is None
    line = log_lines(tmp_path)[-1]
    assert line['decision'] == 'error' and line['reason'] == 'FileExistsError'


def test_an_unwritable_log_directory_never_disturbs_the_hook(tmp_path, monkeypatch):
    write_usage(tmp_path, 96)
    (tmp_path / 'blocked').write_text('a file', encoding='utf-8')
    monkeypatch.setenv('CCUC_LOG_DIR', str(tmp_path / 'blocked' / 'log'))
    assert is_deny(run(call(), ENFORCE))


def copy_hooks(tmp_path, broken=(), missing=()):
    target = tmp_path / 'copy'
    target.mkdir(exist_ok=True)
    for name in ('soft_stop.py', '_config.py', '_usage.py', '_hookio.py'):
        if name not in missing:
            shutil.copy(os.path.join(HOOKS, name), target / name)
    for name in broken:
        (target / name).write_text('def (:\n', encoding='utf-8')
    return target


@pytest.mark.parametrize('missing,broken', [(('_hookio.py',), ()), (('_usage.py',), ()), (('_config.py',), ()),
                                            ((), ('_hookio.py',)), ((), ('_usage.py',)), ((), ('_config.py',))])
def test_missing_or_broken_sibling_files_let_the_call_through(tmp_path, missing, broken):
    write_usage(tmp_path, 99)
    folder = copy_hooks(tmp_path, broken=broken, missing=missing)
    assert run_raw(folder / 'soft_stop.py', json.dumps(call()), ENFORCE) == (0, None)


def test_the_copied_hook_works_when_nothing_is_missing(tmp_path):
    write_usage(tmp_path, 99)
    folder = copy_hooks(tmp_path)
    code, out = run_raw(folder / 'soft_stop.py', json.dumps(call()), ENFORCE)
    assert code == 0 and is_deny(out)


def test_an_internal_error_in_the_decision_lets_the_call_through(tmp_path):
    write_usage(tmp_path, 99)
    folder = copy_hooks(tmp_path)
    source = (folder / 'soft_stop.py').read_text(encoding='utf-8')
    (folder / 'soft_stop.py').write_text(source.replace('decision = decide(', 'decision = boom(', 1), encoding='utf-8')
    assert run_raw(folder / 'soft_stop.py', json.dumps(call()), ENFORCE) == (0, None)
    assert log_lines(tmp_path)[-1]['decision'] == 'error'
    assert log_lines(tmp_path)[-1]['reason'] == 'NameError'


def test_an_output_error_never_turns_into_a_nonzero_exit(tmp_path):
    write_usage(tmp_path, 99)
    folder = copy_hooks(tmp_path)
    (folder / '_hookio.py').write_text(
        (folder / '_hookio.py').read_text(encoding='utf-8').replace(
            'def emit_deny(event: str, reason: str) -> None:', 'def emit_deny(event: str, reason: str) -> None:\n    raise RuntimeError("x")'),
        encoding='utf-8')
    assert run_raw(folder / 'soft_stop.py', json.dumps(call()), ENFORCE) == (0, None)


def test_main_in_process_fails_open_on_import_and_output_errors(tmp_path, monkeypatch, capsys):
    import io
    import _hookio
    write_usage(tmp_path, 99)
    monkeypatch.setattr(sys, 'argv', ['soft_stop.py', ENFORCE])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(call())))
    monkeypatch.setitem(sys.modules, '_usage', None)  # import raises ImportError
    assert soft_stop.main() == 0
    assert capsys.readouterr().out == ''
    monkeypatch.undo()
    monkeypatch.setenv('CCUC_USAGE_FILE', str(tmp_path / 'usage.json'))
    monkeypatch.setenv('CCUC_LOG_DIR', str(tmp_path / 'log'))
    monkeypatch.setenv('CCUC_CONFIG', str(tmp_path / 'none.json'))
    monkeypatch.setattr(sys, 'argv', ['soft_stop.py', ENFORCE])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(call())))

    def boom(*_args):
        raise RuntimeError('output failed')
    monkeypatch.setattr(_hookio, 'emit_deny', boom)
    assert soft_stop.main() == 0
    assert capsys.readouterr().out == ''
