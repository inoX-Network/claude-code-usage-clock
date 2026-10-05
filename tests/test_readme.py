"""The README must say what the code does: every installer option is in the option table and the other way round,
and the defaults in the configuration table are the ones in config.example.json."""
import importlib.util
import json
import os
import re

from conftest import ROOT

README = os.path.join(ROOT, 'README.md')
EXAMPLE = os.path.join(ROOT, 'config.example.json')
INSTALL = os.path.join(ROOT, 'tools', 'install.py')
LOG_SUMMARY = os.path.join(ROOT, 'tools', 'log_summary.py')


def readme_text():
    with open(README, encoding='utf-8') as f:
        return f.read()


def installer_options():
    spec = importlib.util.spec_from_file_location('install_for_readme_test', INSTALL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {option for action in module.build_parser()._actions for option in action.option_strings
            if option.startswith('--')} - {'--help'}


def table_after(text, marker):
    """Rows (list of cells, without header and separator) of the first table from the first line that starts with
    `marker` on."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(marker))
    rows = []
    for line in lines[start:]:  # the marker line itself may be the header row
        if line.startswith('|'):
            rows.append([cell.strip().replace('\\|', '|') for cell in re.split(r'(?<!\\)\|', line)[1:-1]])
        elif rows:
            break
    assert rows, f'no table after {marker!r}'
    return [row for row in rows if not set(row[0]) <= set('-: ')][1:]  # without the separator and the header


def section(text, heading):
    """The text from the heading up to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip('#'))
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        match = re.match(r'(#+) ', lines[i])
        if match and len(match.group(1)) <= level:
            end = i
            break
    return '\n'.join(lines[start:end])


# --- Installer options ------------------------------------------------------------------------------------------

def test_every_installer_option_is_in_the_readme_table_and_the_other_way_round():
    documented = {option for row in table_after(readme_text(), 'Options:') for option in re.findall(r'--[a-z][a-z-]*', row[0])}
    real = installer_options()
    assert real - documented == set(), f'missing in the README table: {sorted(real - documented)}'
    assert documented - real == set(), f'in the README table but not in install.py: {sorted(documented - real)}'


def test_the_options_used_in_the_readme_commands_exist():
    real = installer_options()
    used = set()
    for line in readme_text().splitlines():
        if 'tools/install.py' in line:
            used |= set(re.findall(r'(?<![\w-])--[a-z][a-z-]*', line))
    assert used and used <= real, sorted(used - real)


def test_every_log_summary_option_is_mentioned_in_the_readme():
    with open(LOG_SUMMARY, encoding='utf-8') as f:
        options = set(re.findall(r"add_argument\('(--[a-z-]+)'", f.read()))
    assert options >= {'--since', '--config'}
    text = readme_text()
    assert [o for o in sorted(options) if o not in text] == []


# --- Configuration table ----------------------------------------------------------------------------------------

def flatten(data, prefix=''):
    flat = {}
    for key, value in data.items():
        if isinstance(value, dict):
            flat.update(flatten(value, f'{prefix}{key}.'))
        else:
            flat[f'{prefix}{key}'] = value
    return flat


def parse_default(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def readme_defaults():
    found = {}
    for key_cell, default_cell, _meaning in table_after(readme_text(), '| Key |'):
        keys = re.findall(r'`([^`]+)`', key_cell)
        prefix = keys[0].rsplit('.', 1)[0] + '.' if '.' in keys[0] else ''
        keys = [keys[0]] + [prefix + k for k in keys[1:]]
        defaults = [part.strip().strip('`') for part in default_cell.split(' / ')]
        assert len(keys) == len(defaults), f'{key_cell!r} has {len(keys)} keys but {len(defaults)} defaults'
        for key, default in zip(keys, defaults):
            assert key not in found, f'{key} appears twice in the README table'
            found[key] = parse_default(default)
    return found


def test_the_readme_configuration_table_matches_config_example_json():
    with open(EXAMPLE, encoding='utf-8') as f:
        real = flatten(json.load(f))
    documented = readme_defaults()
    assert set(real) - set(documented) == set(), f'not in the README table: {sorted(set(real) - set(documented))}'
    assert set(documented) - set(real) == set(), f'not in config.example.json: {sorted(set(documented) - set(real))}'
    wrong = {k: (documented[k], real[k]) for k in real if (type(documented[k]), documented[k]) != (type(real[k]), real[k])}
    assert wrong == {}, f'README default vs config.example.json: {wrong}'


def test_the_table_parser_sees_the_pipe_in_the_model_pattern_and_the_list_defaults():
    documented = readme_defaults()
    assert documented['spawn_rules.model_pattern'].count('|') >= 4
    assert documented['spawn_rules.effort_values'] == ['low', 'medium', 'high', 'xhigh', 'max']
    assert documented['thresholds.deny'] == 95 and documented['display.show_time'] is True


# --- What the text has to explain --------------------------------------------------------------------------------

def test_the_readme_says_that_week_ok_until_is_not_kept_but_the_mode_is():
    text = readme_text()
    week_row = next(row for row in table_after(text, 'Options:') if '--week-ok-until' in row[0])
    assert 'removes an earlier value' in week_row[1]
    mode_row = next(row for row in table_after(text, 'Options:') if '--mode' in row[0])
    assert 'keeps its mode' in mode_row[1] and 'first install' in mode_row[1]
    assert 'removes an earlier approval' in text


def test_the_uninstall_section_names_everything_that_stays():
    body = section(readme_text(), '### Uninstall completely')
    for needle in ('--target', 'config.json', '~/.cache/claude-usage-clock', 'rate-limit.json', 'rate-limit.json.lock',
                   'autoContinueAtUsageLimit', '--replace-statusline', 'settings.json.bak-usage-clock-', 'rm -r'):
        assert needle in body, needle


def test_the_readme_explains_model_pattern_effort_values_and_the_default_exceptions():
    body = section(readme_text(), '### Model names, effort values and exceptions')
    for needle in ('spawn_rules.model_pattern', 'fullmatch', 'spawn_rules.effort_values', 'spawn_rules.exceptions',
                   'claude-code-guide', 'statusline-setup', 'my-proxy'):
        assert needle in body, needle
    with open(EXAMPLE, encoding='utf-8') as f:
        rules = json.load(f)['spawn_rules']
    assert all(name in body for name in rules['exceptions'])


def test_the_readme_describes_what_the_log_contains_and_that_quoted_values_are_masked():
    body = section(readme_text(), '### From shadow to enforce')
    for needle in ('agent id', 'names* of the input fields', 'reason', 'replaced by `…`', 'never records prompts, commands or script text'):
        assert needle in body, needle


# --- Checkpoint appends and the log fields: the text must match the hooks ---------------------------------------

ARCHITECTURE = os.path.join(ROOT, 'docs', 'architecture.md')


def architecture_text():
    with open(ARCHITECTURE, encoding='utf-8') as f:
        return f.read()


def flat(text):
    """The text with every line break and run of spaces as one space: a phrase may be wrapped."""
    return ' '.join(text.split())


def append_examples(text):
    """The echo/printf examples in backticks that append to `file`."""
    return [c for c in re.findall(r'`([^`\n]+)`', text)
            if c.startswith(('echo ', 'printf ')) and (c.endswith('>> file') or c.endswith('| tee -a file'))]


def test_the_documented_append_examples_are_checkpoint_writes(tmp_path):
    import soft_stop
    target = str(tmp_path / 'agent-checkpoints' / 'notes.md')
    readme = append_examples(section(readme_text(), '## The two gates'))
    architecture = append_examples(section(architecture_text(), '### soft_stop.py'))
    assert any(c.startswith('printf ') for c in readme) and any(c.startswith('printf ') for c in architecture)
    for command in readme + architecture:
        real = command[:-len('file')] + target
        assert soft_stop.is_checkpoint_write('Bash', {'command': real}), command
    # what the text rules out is no checkpoint write either
    for command in (f"printf -v x '%s' \"a\" >> {target}", f"printf '%d' \"5\" >> {target}",
                    f"echo \"$HOME\" >> {target}"):
        assert not soft_stop.is_checkpoint_write('Bash', {'command': command}), command
    for needle in ('`%s`, `%b` and `%%`', 'without options'):
        assert needle in flat(readme_text()) and needle in flat(architecture_text()), needle


def test_the_log_fields_with_values_are_named_with_their_length_limit():
    import _hookio
    with open(os.path.join(ROOT, 'hooks', '_hookio.py'), encoding='utf-8') as f:
        valued = set(re.findall(r"'(\w+)': _short_text\(", f.read()))
    assert valued == {'tool_name', 'agent_id', 'agent_type', 'permission_mode', 'subagent_type', 'model_in_call'}
    assert _hookio._short_text('x' * 500) == 'x' * 100
    readme = section(readme_text(), '### From shadow to enforce')
    architecture = section(architecture_text(), '## Log')
    for name in ('agent_type', 'permission_mode', 'subagent_type', 'model_in_call'):
        assert f'`{name}`' in readme and f'`{name}`' in architecture, name
    assert all(f'`{name}`' in architecture for name in valued)
    assert 'at most 100 characters' in flat(readme) and '*names* of the input fields' in flat(readme)
    assert 'at most 100 characters' in flat(architecture) and 'only as field names' in flat(architecture)
    assert 'quoted values masked' in flat(architecture)


# --- Status line: the text must match the code ------------------------------------------------------------------

def test_the_status_line_sections_name_the_new_keys_the_state_file_and_the_clean_up_age():
    import _context_state
    with open(EXAMPLE, encoding='utf-8') as f:
        keys = [k for k in json.load(f)['statusline'] if k.startswith(('context_', 'cache_'))]
    assert keys == ['context_yellow_from_k', 'context_red_from_k', 'cache_yellow_below_min']
    readme = section(readme_text(), '## What the status line shows')
    architecture = section(architecture_text(), '## Status line')
    for key in keys:
        assert key in readme and key in architecture, key
    assert 'ctx –/1000k' in flat(readme) and 'ctx –/1000k' in flat(architecture)
    assert 'context/<session_id>.json' in readme and 'context/<session_id>.json' in architecture
    assert f'{_context_state.MAX_AGE_S // 86400} days' in readme and f'{_context_state.MAX_AGE_S // 86400} days' in architecture
    assert '[A-Za-z0-9_-]{1,128}' in architecture and _context_state.SESSION_ID.pattern in architecture
    assert '_context_state.py' in architecture_text()      # in the file layout


def test_the_weekly_notes_of_the_usage_line_are_documented_word_for_word():
    import usage_clock
    assert set(usage_clock.WEEK_NOTES) == {'warn', 'ask', 'deny'}
    for text in usage_clock.WEEK_NOTES.values():
        assert text in flat(readme_text()) and text in flat(architecture_text()), text
