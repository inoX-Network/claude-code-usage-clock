#!/usr/bin/env python3
"""Lists which agents the model rule of the spawn gate would report or deny. Changes nothing.

Answers "what breaks if I switch to enforce?" before it happens. It reads every agent definition the spawn gate
would find, in the same order (project `.claude/agents` of --project, then `~/.claude/agents`; CCUC_AGENT_DIRS
replaces both, as in the hooks), and applies the same rule: a definition needs `model:` and an `effort:` from
`spawn_rules.effort_values`. It also names the built-in agent types of Claude Code, which have no definition
file and are always a finding unless they are listed in `spawn_rules.exceptions`. Workflow scripts and skill
forks are only checked when they start.

The rules come from a config.json found like log_summary.py finds it: --config, else CCUC_CONFIG, else the
config.json of the standard installation (STANDARD_CONFIG below), else the defaults.

Usage: python3 check_agents.py [--project DIR] [--config PATH]
Exit codes: 0 nothing the gate would act on, 1 findings, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hooks'))
import _config  # noqa: E402
import _hookio  # noqa: E402
import _spawn_check  # noqa: E402

STANDARD_CONFIG = '~/.claude/hooks/usage-clock/config.json'
# Agent types built into Claude Code 2.1 that have no definition file.
BUILT_IN = ('general-purpose', 'Explore', 'Plan', 'statusline-setup', 'claude-code-guide')
EFFECT = {'deny': 'would be denied', 'warn': 'would start with a warning', 'off': 'are not checked (rule is off)'}


def load_rules(config: str | None) -> _spawn_check.Rules:
    chosen = config or os.environ.get('CCUC_CONFIG') or os.path.expanduser(STANDARD_CONFIG)
    with mock.patch.dict(os.environ, {'CCUC_CONFIG': chosen}):  # a missing or broken file gives the defaults
        return _spawn_check.Rules.from_config(_config.load(log_problems=False)['spawn_rules'])


def definitions(directories: list[str]) -> list[tuple[str, str, dict[str, str]]]:
    """(name, path, frontmatter) of every .md file with a `name:`, in the search order of the spawn gate."""
    found = []
    for base in directories:
        if not os.path.isdir(base):
            continue
        for root, folders, files in os.walk(base):   # like the gate: symlinked subfolders are not entered
            folders.sort()
            for name in sorted(files):
                if name.endswith('.md'):
                    path = os.path.join(root, name)
                    head = _spawn_check._read_head(path)
                    if head and head.get('name'):
                        found.append((head['name'], path, head))
    return found


def finding(head: dict[str, str], rules: _spawn_check.Rules) -> str | None:
    """What the model rule would say about this definition, None if it is complete."""
    if head.get('_unclear'):
        return f'{head["_unclear"]}; model and effort cannot be determined'
    missing = [] if head.get('model') else ['no model:']
    effort = head.get('effort')
    if not effort:
        missing.append('no effort:')
    elif effort not in rules.effort_values:
        missing.append(f'effort "{effort}" is not in spawn_rules.effort_values')
    return ', '.join(missing) or None


def report(directories: list[str], rules: _spawn_check.Rules) -> tuple[list[str], int]:
    """(output lines, number of findings the gate would act on)."""
    lines = [f'Model rule: model_effort_action = {rules.model_effort_action}; agents with a finding '
             f'{EFFECT[rules.model_effort_action]}.', 'Searched, in this order: ' + ', '.join(directories), '']
    seen: dict[str, str] = {}
    findings, ok, ignored = [], 0, []
    for name, path, head in definitions(directories):
        if name in seen:
            ignored.append(f'  {name}: {path} (the gate uses {seen[name]})')
            continue
        seen[name] = path
        problem = finding(head, rules)
        if problem and name not in rules.exceptions:
            findings.append(f'  {name}: {problem} - {path}')
        else:
            ok += 1
    builtin = [name for name in BUILT_IN if name not in seen and name not in rules.exceptions]
    for name in builtin:
        findings.append(f'  {name}: built-in type without a definition file - define an agent of your own with '
                        'model: and effort:, or add it to spawn_rules.exceptions')
    lines.append(f'{len(seen)} definition(s), {ok} without a finding.')
    if findings:
        lines += ['', 'Findings:'] + findings
    if ignored:
        lines += ['', 'Ignored, an earlier definition has the same name:'] + ignored
    exceptions = sorted(rules.exceptions)
    lines += ['', 'Exceptions (start without the model rule): ' + (', '.join(exceptions) if exceptions else 'none')]
    return lines, (0 if rules.model_effort_action == 'off' else len(findings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='check_agents.py', allow_abbrev=False, description=(
        'Lists which agents the model rule of the spawn gate would report or deny. Changes nothing.'))
    parser.add_argument('--project', metavar='DIR', default=os.getcwd(),
                        help='project whose .claude/agents is searched first (default: the current folder)')
    parser.add_argument('--config', metavar='PATH',
                        help='config.json with the spawn_rules (default: CCUC_CONFIG, else the config.json of the '
                             'standard installation, else the defaults)')
    args = parser.parse_args(argv)
    config = os.path.expanduser(args.config) if args.config else None
    if config is not None and not os.path.isfile(config):
        parser.error(f'--config: not a file: {args.config}')
    if not os.path.isdir(args.project):
        parser.error(f'--project: not a folder: {args.project}')
    with mock.patch.dict(os.environ, {'CLAUDE_PROJECT_DIR': os.path.abspath(args.project)}):
        directories = _hookio.agent_dirs({'cwd': os.path.abspath(args.project)})
    lines, count = report(directories, load_rules(config))
    print('\n'.join(lines))
    return 1 if count else 0


if __name__ == '__main__':
    sys.exit(main())
