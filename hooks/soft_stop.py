#!/usr/bin/env python3
"""Soft stop for tool calls of subagents (PreToolUse, matcher "*").

Acts only when the input carries an agent_id ("present only when the hook fires inside a subagent call").
The main session is never blocked. Fail open: every internal error lets the call through, a failed import
included. The hook always exits 0, because exit code 2 would block a PreToolUse call.
Completion tools (StructuredOutput) are always allowed.
In shadow mode (the default) decisions are only logged.

Levels, all from config.json (thresholds.*), all measured on the five-hour window:
  warn             a warning: save a checkpoint soon
  checkpoint_only  everything except checkpoint writes is denied
  deny             everything except checkpoint writes is denied, with the instruction to finish now
See decide() for the exact order and the per-agent counter.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EVENT = 'PreToolUse'
DEFAULT_SUFFIX = '/agent-checkpoints'
# Tools an agent uses to hand in its result. Never denied: a denied completion tool would lose the very
# partial result the soft stop is meant to save (workflow agent() calls with a schema end with it).
COMPLETION_TOOLS = ('StructuredOutput',)


@dataclass(frozen=True)
class Decision:
    action: str  # 'allow' | 'warn' | 'deny'
    reason: str = ''


def normalize_suffix(value) -> str:
    """The directory suffix from the config in the form '/name' or '/a/b'.

    A suffix that would match every directory (empty, '/', not a string) must not open the checkpoint
    exception for arbitrary paths, so it falls back to the default.
    """
    if not isinstance(value, str):
        return DEFAULT_SUFFIX
    text = '/' + value.strip().strip('/')
    return text if text != '/' and '\0' not in text else DEFAULT_SUFFIX


def save_form(suffix: str) -> str:
    """The forms that count as a checkpoint write, spelled out for the model."""
    target = f'<absolute path>{suffix}/<file>'
    return (f'Write/Edit/MultiEdit to {target}, or a Bash command that is nothing but an append: '
            f"cat >> {target} <<'EOF' ... EOF, echo '<text>' >> {target}, or echo '<text>' | tee -a {target}")


def _is_checkpoint_path(path, suffix: str) -> bool:
    """Absolute, no '..', parent directory ends with the suffix. Also after resolving symlinks."""
    if not isinstance(path, str) or not path or '\0' in path or '..' in path.split('/'):
        return False
    norm = os.path.normpath(path)
    if not os.path.isabs(norm):
        return False
    return all(os.path.dirname(p).endswith(suffix) for p in (norm, os.path.realpath(norm)))


# Path token: single-quoted, double-quoted without $ ` \ or unquoted from harmless characters
_PATH = r"""('[^'\n]+'|"[^"$`\\\n]+"|[A-Za-z0-9_./@%+,:=-]+)"""
# Arguments of echo/printf: quoted only, double-quoted without $ ` \
# echo options (-n, -e, -E) run no code in bash or zsh; they stay allowed ('- item done' is a common line).
_ARG = r"""(?:'[^']*'|"[^"$`\\]*")"""
# printf only without options and only with %s, %b and %%: `printf -v 'a[$(cmd)]'` runs cmd (bash and zsh), and
# zsh evaluates the argument of a numeric conversion (%d, %x, %f, %*s, ...) or of %n as arithmetic, $(cmd) in an
# array subscript included. The format therefore must not start with '-', quoted or not.
_CONVERSION = r'%(?:%|[-+ #0-9.]*[sb])'
_FORMAT = rf"""(?:'(?!-)(?:[^'%]|{_CONVERSION})*'|"(?!-)(?:[^"$`\\%]|{_CONVERSION})*")"""
_ECHO = rf'(?:echo(?:[ \t]+{_ARG})+|printf[ \t]+{_FORMAT}(?:[ \t]+{_ARG})*)'
_APPEND = re.compile(rf'\s*{_ECHO}[ \t]*>>[ \t]*{_PATH}\s*')
_TEE = re.compile(rf'\s*{_ECHO}[ \t]*\|[ \t]*tee[ \t]+(?:-a|--append)[ \t]+{_PATH}\s*')
_HEREDOC = re.compile(rf"""\s*cat[ \t]+>>[ \t]*{_PATH}[ \t]+<<-?[ \t]*(['"])([A-Za-z_][A-Za-z0-9_]*)\2[ \t]*\n(.*)\n\3\n?""",
                      re.DOTALL)


def _unquote(token: str) -> str:
    return token[1:-1] if token[:1] in ('"', "'") else token


def _is_bash_append(command: str, suffix: str) -> bool:
    """Only if the WHOLE command is a plain append to a checkpoint file."""
    for pattern in (_APPEND, _TEE):
        m = pattern.fullmatch(command)
        if m:
            return _is_checkpoint_path(_unquote(m.group(1)), suffix)
    m = _HEREDOC.fullmatch(command)
    if not m:
        return False
    marker, body = m.group(3), m.group(4)
    # If the marker already stands on a line of its own earlier, the heredoc ends there and the rest would run as a command
    if any(line.lstrip('\t') == marker for line in body.split('\n')):
        return False
    return _is_checkpoint_path(_unquote(m.group(1)), suffix)


def is_checkpoint_write(tool_name: str, tool_input: dict, suffix: str = DEFAULT_SUFFIX) -> bool:
    """Does this call write into a checkpoint directory? Strict: anything unclear is NOT a checkpoint."""
    if tool_name in ('Write', 'Edit', 'MultiEdit'):
        return _is_checkpoint_path(tool_input.get('file_path'), suffix)
    if tool_name == 'Bash':
        command = tool_input.get('command')
        return isinstance(command, str) and _is_bash_append(command, suffix)
    return False


def _num(value: float) -> str:
    return f'{value:g}'


def decide(usage, tool_name: str, tool_input: dict, calls_since_checkpoint: int, has_checkpoint: bool,
           cfg: dict) -> Decision:
    """The soft stop for one tool call of a subagent.

    usage: object with .state ('ok'|'stale'|'unknown') and .five_hour (see _usage.Usage).
    calls_since_checkpoint / has_checkpoint: the per-agent counter.

    The counter is opt-in: it only forces a checkpoint from agents that wrote one before. The hook does not know
    the agent's task. Ad-hoc agents that were never asked for checkpoints would otherwise be blocked after N calls
    for no reason. Consequence: an agent that ignores the instruction and never saves is not forced by the counter;
    only the usage levels apply to it.
    """
    if tool_name in COMPLETION_TOOLS:
        return Decision('allow')
    suffix = normalize_suffix(cfg.get('checkpoint_dir_suffix'))
    if is_checkpoint_write(tool_name, tool_input, suffix):
        return Decision('allow')
    limits = cfg['thresholds']
    form = save_form(suffix)
    five = usage.five_hour if usage.state == 'ok' else None
    if five is not None:
        if five >= limits['deny']:
            return Decision('deny', f'Usage pause: the 5-hour window is at {five:.0f} % (limit {_num(limits["deny"])} %). '
                                    f'Save your checkpoint now ({form}), then finish immediately with StructuredOutput '
                                    'or your partial result and state clearly that it is incomplete. '
                                    'Make no further tool calls.')
        if five >= limits['checkpoint_only']:
            return Decision('deny', f'Usage pause: the 5-hour window is at {five:.0f} % (limit {_num(limits["checkpoint_only"])} %). '
                                    f'Only saving a checkpoint is still allowed ({form}). '
                                    'Then finish with StructuredOutput or your partial result and state clearly '
                                    'that it is incomplete.')
    if has_checkpoint and calls_since_checkpoint >= limits['calls_without_checkpoint']:
        return Decision('deny', f'Checkpoint due: {calls_since_checkpoint} calls since your last checkpoint '
                                f'(limit {_num(limits["calls_without_checkpoint"])}). Append your partial result to your '
                                f'checkpoint file first ({form}), then continue.')
    if five is not None and five >= limits['warn']:
        return Decision('warn', f'Usage: 5-hour window at {five:.0f} %. Save a checkpoint now; from '
                                f'{_num(limits["checkpoint_only"])} % only checkpoint writes are allowed.')
    if usage.state != 'ok':
        # Fail open with a warning, and only on the agent's first call, otherwise it would stand in every call
        if calls_since_checkpoint == 0 and not has_checkpoint:
            return Decision('warn', f'Usage {usage.state}: the stop levels are not active (fail open). '
                                    'Save your checkpoint regularly.')
        return Decision('allow', f'Usage {usage.state} (fail open).')
    return Decision('allow')


class Counter:
    """Calls since the last checkpoint, per agent_id, in a small JSON file below <log_dir>/counters/."""

    def __init__(self, directory: str):
        self.directory = directory

    def _path(self, agent_id: str) -> str:
        # Everything but a harmless character becomes '_': the id can never leave the directory
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', agent_id)[:120]
        return os.path.join(self.directory, f'{safe}.json')

    def read(self, agent_id: str) -> tuple[int, bool]:
        try:
            with open(self._path(agent_id), encoding='utf-8') as f:
                data = json.load(f)
            return int(data.get('since', 0)), bool(data.get('has_checkpoint', False))
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            return 0, False

    def book(self, agent_id: str, was_checkpoint: bool) -> None:
        since, has = self.read(agent_id)
        new = {'since': 0 if was_checkpoint else since + 1, 'has_checkpoint': has or was_checkpoint}
        os.makedirs(self.directory, exist_ok=True)
        tmp = f'{self._path(agent_id)}.tmp.{os.getpid()}'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(new, f)
        os.replace(tmp, self._path(agent_id))


def main() -> int:
    try:
        import _config
        import _hookio
        import _usage
    except Exception:  # a missing or broken sibling file: let the call through silently
        return 0
    try:
        args = _hookio.parse_args(sys.argv[1:])
        hook_input = _hookio.read_input()
        agent_id = hook_input.get('agent_id')
        if not agent_id:
            return 0  # main session: never block
        try:
            cfg = _config.load(log_problems=True)
            tool = str(hook_input.get('tool_name') or '')
            raw = hook_input.get('tool_input')
            tool_input = raw if isinstance(raw, dict) else {}
            counter = Counter(os.path.join(_hookio.log_dir(), 'counters'))
            since, has = counter.read(str(agent_id))
            usage = _usage.read_usage(_usage.usage_path(), None, cfg['thresholds']['max_age_min'])
            decision = decide(usage, tool, tool_input, since, has, cfg)
            effective = decision.action if args['mode'] == 'enforce' else 'allow'
            if effective != 'deny':
                counter.book(str(agent_id), is_checkpoint_write(tool, tool_input, normalize_suffix(cfg.get('checkpoint_dir_suffix'))))
        except Exception as error:  # fail open
            _hookio.log_decision('soft_stop', args['mode'], hook_input, 'error', type(error).__name__)
            return 0
        _hookio.log_decision('soft_stop', args['mode'], hook_input, decision.action, decision.reason,
                             calls_since_checkpoint=since)
        if args['mode'] != 'enforce':
            return 0
        if decision.action == 'deny':
            _hookio.emit_deny(EVENT, decision.reason)
        elif decision.action == 'warn':
            _hookio.emit_context(EVENT, decision.reason)
    except Exception:  # even an output or log error must never block
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
