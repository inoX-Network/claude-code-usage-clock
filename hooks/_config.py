"""Loads config.json next to the scripts (env override CCUC_CONFIG).

Missing file: defaults. Unreadable file, wrong types or unknown keys: defaults for the affected keys,
one log line, never a crash. The defaults below must match config.example.json exactly (a test checks it).
"""
from __future__ import annotations

import copy
import json
import math
import os
import re

DEFAULTS: dict = {
    'usage_file': '~/.claude/rate-limit.json',
    'log_dir': '~/.cache/claude-usage-clock',
    'display': {
        'show_usage': True,
        'show_time': True,
    },
    'statusline': {
        'yellow_from': 50,
        'red_from': 75,
        'week_stop_marker': True,
        'show_reset': True,
        'context_yellow_from_k': 300,
        'context_red_from_k': 500,
        'cache_yellow_below_min': 5,
    },
    'thresholds': {
        'start_block_5h': 75,
        'start_block_week': 75,
        'warn': 85,
        'checkpoint_only': 92,
        'deny': 95,
        'max_age_min': 15,
        'calls_without_checkpoint': 15,
        'unknown_usage_max_agents': 1,
        'week_action': 'warn',
        'ask_verified_in_bypass': False,
    },
    'context_reporter': {
        'after_tool_from': 50,
        'max_age_min': 15,
    },
    'wake_watcher': {
        'from_k': 0,
        'lead_min': 10,
        'routine_file': '',
    },
    'history': {
        'enabled': True,
        'heartbeat_min': 15,
        'max_kb': 1024,
    },
    'start_estimate': {
        'chars_per_token': 2.3,
    },
    'checkpoint_dir_suffix': '/agent-checkpoints',
    'spawn_rules': {
        'model_effort_action': 'warn',
        'model_pattern': 'sonnet|opus|haiku|fable|inherit|claude-[a-z0-9-]+',
        'effort_values': ['low', 'medium', 'high', 'xhigh', 'max'],
        'suggested_agents': [],
        'exceptions': ['claude-code-guide', 'statusline-setup'],
        'apply_to_main_session': True,
        'skill_fork_missing_agent': 'warn',
        'model_override_in_call': 'warn',
    },
    'sync': {
        'mode': 'off',
        'repo': '',
        'transport': 'https',
        'machine_name': '',
        'interval_min': 30,
        'backoff_max_h': 6,
        'pull_timeout_s': 5,
        'project_labels': 'plain',
        'project_aliases': {},
        'label_salt': '',
        'include_gates': True,
        'keep_request_months': 3,
    },
}

_MISSING = object()  # no file at all, as opposed to a file that contains `null`

# Keys whose value is an object of free names to texts, not a section, as `section.key`.
_MAPS = ('sync.project_aliases',)

# The name of the machine in the sync repository: the characters a project label may have, at most 64.
_MACHINE_NAME = re.compile(r'[\w .+@=-]{0,64}')

# The sync repository on GitHub as `owner/name`, in the characters GitHub allows: one slash, no `..` (nothing that
# could make another URL of it).
_SYNC_REPO = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}')

# Keys whose value is a filesystem path: "~" is expanded in the loaded result (not in DEFAULTS).
_PATH_KEYS = ('usage_file', 'log_dir')

# Keys with a fixed set of values, as (section, key). Any other string falls back to the default.
_CHOICES = {
    ('thresholds', 'week_action'): ('warn', 'ask', 'deny'),
    ('spawn_rules', 'model_effort_action'): ('off', 'warn', 'deny'),
    ('spawn_rules', 'skill_fork_missing_agent'): ('warn', 'deny'),
    ('spawn_rules', 'model_override_in_call'): ('allow', 'warn', 'deny'),
    ('sync', 'mode'): ('off', 'shadow', 'on'),
    ('sync', 'transport'): ('https', 'ssh'),
    ('sync', 'project_labels'): ('plain', 'alias', 'hash'),
}


def valid_sync_repo(value: str) -> bool:
    """`owner/name` as `sync.repo` takes it (_SYNC_REPO, no `..`, not the name `.`)."""
    return bool(_SYNC_REPO.fullmatch(value)) and '..' not in value and not value.endswith('/.')


def config_path() -> str:
    return os.environ.get('CCUC_CONFIG') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_ok(default, value) -> bool:
    """Same kind of value as the default. Numbers may be int or float, but never bool."""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if _is_number(default):
        return _is_number(value)
    if isinstance(default, str):
        return isinstance(value, str)
    return isinstance(value, list) and all(isinstance(item, str) for item in value)   # the only kind left: list[str]


def _merge_section(defaults: dict, given, path: str, problems: list[str]) -> dict:
    result = copy.deepcopy(defaults)
    if not isinstance(given, dict):
        problems.append(f'{path or "config"}: expected an object, using defaults')
        return result
    for key, value in given.items():
        name = f'{path}.{key}' if path else key
        if key not in defaults:
            problems.append(f'{name}: unknown key ignored')
        elif name in _MAPS:
            if isinstance(value, dict) and all(isinstance(item, str) for pair in value.items() for item in pair):
                result[key] = dict(value)
            else:
                problems.append(f'{name}: must be an object of texts, using default')
        elif isinstance(defaults[key], dict):
            result[key] = _merge_section(defaults[key], value, name, problems)
        elif not _type_ok(defaults[key], value):
            problems.append(f'{name}: wrong type, using default')
        else:
            result[key] = copy.deepcopy(value)
    return result


def _report(problems: list[str], log_dir: str) -> None:
    # Imported here: _hookio imports this module at the top, the log writer lives there.
    import _hookio
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    _hookio.write_log_line({'time': now, 'hook': 'config', 'decision': 'defaults',
                            'reason': '; '.join(problems)[:300]}, log_dir)


def load(log_problems: bool = True) -> dict:
    """The effective configuration: defaults, overridden by every valid key of config.json.

    log_problems=False is for the status line, which runs every few seconds: a broken config would
    otherwise fill the log. The hooks report it.
    """
    problems: list[str] = []
    given = _MISSING
    try:
        with open(config_path(), encoding='utf-8') as f:
            given = json.load(f)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, RecursionError) as exc:
        problems.append(f'config file unreadable ({type(exc).__name__}), using defaults')
    cfg = copy.deepcopy(DEFAULTS) if given is _MISSING else _merge_section(DEFAULTS, given, '', problems)
    rules = cfg['spawn_rules']
    # Right type but useless: no effort could ever match, or the checks would silently accept nothing or everything.
    if not rules['effort_values'] or not all(value.strip() for value in rules['effort_values']):
        problems.append('spawn_rules.effort_values: must be a non-empty list of non-empty strings, using default')
        rules['effort_values'] = copy.deepcopy(DEFAULTS['spawn_rules']['effort_values'])
    if not rules['model_pattern'].strip():
        problems.append('spawn_rules.model_pattern: must not be empty, using default')
        rules['model_pattern'] = DEFAULTS['spawn_rules']['model_pattern']
    # A pattern that does not compile would crash the hook that uses it.
    try:
        re.compile(rules['model_pattern'])
    except re.error:
        problems.append('spawn_rules.model_pattern: not a valid regular expression, using default')
        rules['model_pattern'] = DEFAULTS['spawn_rules']['model_pattern']
    # An empty or slash-only suffix would turn every directory into a checkpoint directory.
    if not cfg['checkpoint_dir_suffix'].strip().strip('/'):
        problems.append('checkpoint_dir_suffix: must not be empty or only "/", using default')
        cfg['checkpoint_dir_suffix'] = DEFAULTS['checkpoint_dir_suffix']
    reporter = cfg['context_reporter']
    # A percentage outside 0-100 would report always or never; NaN and infinity fail these comparisons, too.
    if not 0 <= reporter['after_tool_from'] <= 100:
        problems.append('context_reporter.after_tool_from: must be a number from 0 to 100, using default')
        reporter['after_tool_from'] = DEFAULTS['context_reporter']['after_tool_from']
    # Zero, negative or infinite would make every measurement stale or none.
    if not (math.isfinite(reporter['max_age_min']) and reporter['max_age_min'] > 0):
        problems.append('context_reporter.max_age_min: must be a positive number, using default')
        reporter['max_age_min'] = DEFAULTS['context_reporter']['max_age_min']
    waker = cfg['wake_watcher']
    # Negative or infinite would never or always arm the watcher; 0 is valid and means off.
    if not (math.isfinite(waker['from_k']) and waker['from_k'] >= 0):
        problems.append('wake_watcher.from_k: must be a number of at least 0, using default')
        waker['from_k'] = DEFAULTS['wake_watcher']['from_k']
    # Zero, negative or infinite would never fire or no longer be a lead time.
    if not (math.isfinite(waker['lead_min']) and waker['lead_min'] > 0):
        problems.append('wake_watcher.lead_min: must be a positive number, using default')
        waker['lead_min'] = DEFAULTS['wake_watcher']['lead_min']
    history = cfg['history']
    # Zero, negative or infinite would write a line every time, never again, or rotate on every line.
    for key in ('heartbeat_min', 'max_kb'):
        if not (math.isfinite(history[key]) and history[key] > 0):
            problems.append(f'history.{key}: must be a positive number, using default')
            history[key] = DEFAULTS['history'][key]
    estimate = cfg['start_estimate']
    # Zero, negative or infinite would make the estimate of the start infinite or meaningless.
    if not (math.isfinite(estimate['chars_per_token']) and estimate['chars_per_token'] > 0):
        problems.append('start_estimate.chars_per_token: must be a positive number, using default')
        estimate['chars_per_token'] = DEFAULTS['start_estimate']['chars_per_token']
    sync = cfg['sync']
    # Zero, negative or infinite would push all the time, never back off or never wait for a pull. An interval
    # below the minimum is valid here and raised by the sync core (_sync_core.MIN_INTERVAL_MIN), with its own line.
    for key in ('interval_min', 'backoff_max_h', 'pull_timeout_s'):
        if not (math.isfinite(sync[key]) and sync[key] > 0):
            problems.append(f'sync.{key}: must be a positive number, using default')
            sync[key] = DEFAULTS['sync'][key]
    # Whole months only; fewer than one would compact the month that just ended.
    months = sync['keep_request_months']
    if not (math.isfinite(months) and months >= 1 and months == int(months)):
        problems.append('sync.keep_request_months: must be a whole number of at least 1, using default')
        months = DEFAULTS['sync']['keep_request_months']
    sync['keep_request_months'] = int(months)
    # The name goes into machine.json and the commit message: no line break, no path, no quote.
    if not _MACHINE_NAME.fullmatch(sync['machine_name']):
        problems.append('sync.machine_name: at most 64 letters, digits, spaces and . + @ = - _, using default')
        sync['machine_name'] = DEFAULTS['sync']['machine_name']
    if sync['repo'] and not valid_sync_repo(sync['repo']):
        problems.append('sync.repo: must be owner/name as on GitHub, using default')
        sync['repo'] = DEFAULTS['sync']['repo']
    for (section, key), allowed in _CHOICES.items():
        if cfg[section][key] not in allowed:
            problems.append(f'{section}.{key}: must be one of {"|".join(allowed)}, using default')
            cfg[section][key] = DEFAULTS[section][key]
    for key in _PATH_KEYS:
        cfg[key] = os.path.expanduser(cfg[key])
    if problems and log_problems:
        _report(problems, os.path.expanduser(os.environ.get('CCUC_LOG_DIR') or cfg['log_dir']))
    return cfg
