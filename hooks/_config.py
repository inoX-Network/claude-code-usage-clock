"""Loads config.json next to the scripts (env override CCUC_CONFIG).

Missing file: defaults. Unreadable file, wrong types or unknown keys: defaults for the affected keys,
one log line, never a crash. The defaults below must match config.example.json exactly (a test checks it).
"""
from __future__ import annotations

import copy
import json
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
    },
    'checkpoint_dir_suffix': '/agent-checkpoints',
    'spawn_rules': {
        'require_model_and_effort': True,
        'model_pattern': 'sonnet|opus|haiku|fable|inherit|claude-[a-z0-9-]+',
        'effort_values': ['low', 'medium', 'high', 'xhigh', 'max'],
        'suggested_agents': [],
        'exceptions': ['claude-code-guide', 'statusline-setup'],
        'apply_to_main_session': True,
        'skill_fork_missing_agent': 'warn',
        'model_override_in_call': 'warn',
    },
}

_MISSING = object()  # no file at all, as opposed to a file that contains `null`

# Keys whose value is a filesystem path: "~" is expanded in the loaded result (not in DEFAULTS).
_PATH_KEYS = ('usage_file', 'log_dir')

# spawn_rules keys with a fixed set of values. Any other string falls back to the default.
_CHOICES = {
    'skill_fork_missing_agent': ('warn', 'deny'),
    'model_override_in_call': ('allow', 'warn', 'deny'),
}


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
    for key, allowed in _CHOICES.items():
        if cfg['spawn_rules'][key] not in allowed:
            problems.append(f'spawn_rules.{key}: must be one of {"|".join(allowed)}, using default')
            cfg['spawn_rules'][key] = DEFAULTS['spawn_rules'][key]
    for key in _PATH_KEYS:
        cfg[key] = os.path.expanduser(cfg[key])
    if problems and log_problems:
        _report(problems, os.path.expanduser(os.environ.get('CCUC_LOG_DIR') or cfg['log_dir']))
    return cfg
