"""Model/effort checks for the Agent tool, Workflow scripts and Skill forks (model part of the spawn gate).

Rules:
- Agent tool: the identity comes only from the `name` field of a definition. A definition with model: and
  effort: makes the start allowed. A model in the call overrides the frontmatter; `model_override_in_call`
  decides whether that is allowed silently, reported or denied.
- Agents without a definition (general-purpose, Explore, ...): effort cannot be set there, so they are denied,
  with a pointer to agents that have both.
- Workflow: every agent(...) call needs model + effort or an agentType with a complete definition. Options count
  only as an object literal with fixed values in the second argument. Child workflows (workflow(...)) are
  resolved and checked too, depth 1.
- Fail closed: whatever cannot be checked counts as not determinable and is denied. That includes every use of
  agent/workflow that is not a direct call (alias, property, optional call, ...).
- With `model_effort_action` off nothing here denies; scripts are only analysed to count the agents for
  the usage thresholds. `warn` and `deny` judge the same way; spawn_gate turns a denial into a warning for `warn`.
"""
from __future__ import annotations

import os
import re
import stat
import time
import unicodedata
from dataclasses import dataclass, field

import _config

TIME_BUDGET_S = 1.0          # shared by the definition search and the tokenizer
MAX_FILES = 5000             # per agent directory
MAX_FILE_BYTES = 256 * 1024
MAX_DEPTH = 50
MAX_SCRIPT = 1 << 20          # characters; Claude Code itself limits scripts to 512 KB
HEAD_CHARS = 65536
OPTIONS_FIXED = 'Options must be an object literal with fixed values'


@dataclass(frozen=True)
class Rules:
    """The `spawn_rules` section of the configuration, prepared for the checks."""
    model_effort_action: str
    model_pattern: re.Pattern
    effort_values: tuple[str, ...]
    suggested_agents: tuple[str, ...]
    exceptions: frozenset[str]
    skill_fork_missing_agent: str
    model_override_in_call: str

    @classmethod
    def from_config(cls, spawn_rules: dict) -> Rules:
        return cls(spawn_rules['model_effort_action'], re.compile(spawn_rules['model_pattern']),
                   tuple(spawn_rules['effort_values']), tuple(spawn_rules['suggested_agents']),
                   frozenset(spawn_rules['exceptions']), spawn_rules['skill_fork_missing_agent'],
                   spawn_rules['model_override_in_call'])

    @property
    def require(self) -> bool:
        """Whether the model rule judges at all; `warn` judges too, spawn_gate only softens the result."""
        return self.model_effort_action != 'off'


DEFAULT_RULES = Rules.from_config(_config.DEFAULTS['spawn_rules'])


@dataclass
class Check:
    allowed: bool
    reason: str = ''
    notes: list[str] = field(default_factory=list)
    # None: the agents could not be counted (only possible with the model check off)
    agent_count: int | None = 0


OPEN_VALUE = ('the frontmatter ends inside a quoted value or a [...] / {...} list (a closing quote or bracket is '
              'missing, or a --- inside a value ends the frontmatter early)')
# Claude Code ends the frontmatter at the FIRST '---' after the opening line, even in the middle of a line
# (regex from the Claude Code 2.1 binary: /^---\s*\n([\s\S]*?)---\s*\n?/). `description: a---b` ends it after 'a'.
_FRONTMATTER = re.compile(r'---\s*\n([\s\S]*?)---')
_KEY = re.compile(r'([A-Za-z_][\w-]*):\s*(.*?)\s*$')
_BLOCK_REST = re.compile(r'[-+0-9]*(?:\s+#.*)?\s*$')    # after | or >: indicators and a comment, nothing else


def _quote_end(line: str, quote: str, start: int) -> int:
    """Position after the closing quote, -1 if the quoted value goes on in the next line.
    "..." knows backslash escapes (a backslash at the line end escapes the line break), '...' knows '' as a quote."""
    i = start
    while i < len(line):
        if quote == '"' and line[i] == '\\':
            i += 2
            continue
        if line[i] == quote:
            if quote == "'" and line[i + 1:i + 2] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return -1


def _scan(line: str, quote: str | None, depth: int) -> tuple[str | None, int, bool]:
    """Follows one frontmatter line: (open quote, depth of [ ] / { } lists, block scalar starts).
    A quote only opens where a value starts (after 'key: ', '- ', '[', '{', ','), so it's or a "word" inside
    a plain value stays plain. A '#' after white space outside quotes starts a comment."""
    i, at_start = 0, True
    while i < len(line):
        if quote:
            end = _quote_end(line, quote, i)
            if end < 0:
                return quote, depth, False
            i, quote, at_start = end, None, False
            continue
        c, after = line[i], line[i + 1:i + 2]
        if c in ' \t':
            i += 1
        elif c == '#' and (i == 0 or line[i - 1] in ' \t'):
            break
        elif at_start and c in '"\'':
            quote, i = c, i + 1
        elif at_start and c in '!&':                            # tag or anchor: the value still starts after it
            while i < len(line) and line[i] not in ' \t':
                i += 1
        elif at_start and c in '-?' and after in ('', ' ', '\t') and not depth:
            i += 1                                              # list item or explicit key
        elif at_start and c in '|>' and not depth and _BLOCK_REST.match(line, i + 1):
            return None, depth, True
        elif (at_start or depth) and c in '[{':
            depth, i, at_start = depth + 1, i + 1, True
        elif depth and c in ']}':
            depth, i, at_start = depth - 1, i + 1, False
        elif depth and c == ',':
            i, at_start = i + 1, True
        elif c == ':' and (after in ('', ' ', '\t') or (depth and after in ',[]{}')):
            i, at_start = i + 1, True
        else:
            i, at_start = i + 1, False
    return quote, depth, False


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(' \t'))


def _head(text: str) -> dict[str, str]:
    """Top-level keys of a YAML frontmatter (simple values only, without a YAML library), in the part that Claude
    Code reads as frontmatter. Lines inside a multi-line value are no keys, even if they look like one: the
    indented lines of a block scalar (| or >) are skipped until a line that is not more indented than the one
    that opened it; inside a quoted value or a [ ] / { } list over several lines, every line is skipped until
    it closes. Such a key gets the value ''. Ends the frontmatter while a quote or a list is still open, the
    result carries '_unclear'."""
    text = text[:HEAD_CHARS].lstrip('\ufeff')
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    values = {}
    quote, depth, block = None, 0, None    # block: indentation of the line that opened a block scalar
    plain = None                           # top-level key whose plain value may go on in the next lines
    for line in match.group(1).split('\n'):
        if block is not None:
            if not line.strip() or _indent(line) > block:
                continue
            block = None
        if plain is not None and line.strip() and not line.lstrip().startswith('#'):
            if _indent(line):
                values[plain] = ''         # 'low\n  more' is the value 'low more' for YAML: not read
            plain = None
        key = _KEY.match(line) if quote is None and not depth else None
        quote, depth, opens_block = _scan(line, quote, depth)
        if opens_block:
            block = _indent(line)
        if not key:
            continue
        value = key.group(2)
        if quote or depth or opens_block:
            value = ''                     # goes on in the next lines: not read
        elif value[:1] in ('"', "'") and _quote_end(value, value[0], 1) > 0:
            value = value[1:_quote_end(value, value[0], 1) - 1]   # quoted value: a '#' inside is not a comment
        else:
            value = re.sub(r'(^|\s)#.*$', '', value).strip().strip('\'"')
            plain = key.group(1) if value else None
        values[key.group(1)] = value
    if quote or depth:
        values['_unclear'] = OPEN_VALUE
    return values


class Unclear(Exception):
    """Cannot be checked safely → deny (fail closed)."""


# Cache per process: directory list → {name: frontmatter}. Every hook call is its own process.
_INDEX: dict[tuple[str, ...], dict[str, dict[str, str]]] = {}


def _read_head(path: str) -> dict[str, str] | None:
    """Frontmatter of a regular file up to MAX_FILE_BYTES, else None. O_NONBLOCK: FIFOs do not block."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return None
        raw = os.read(fd, MAX_FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_FILE_BYTES:
        return None
    try:
        return _head(raw.decode('utf-8'))
    except ValueError:
        return None


def _build_index(directories: list[str], deadline: float) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for base in directories:
        if not os.path.isdir(base):
            continue
        count = 0
        # without followlinks: the root itself may be a symlink, symlinked subfolders are not entered
        for root, folders, files in os.walk(base):
            _check_deadline(deadline)
            folders.sort()
            count += len(files)
            if count > MAX_FILES:
                raise Unclear(f'Agent directory "{base}" has more than {MAX_FILES} files; '
                              'definition cannot be determined.')
            for name in sorted(files):
                if not name.endswith('.md'):
                    continue
                _check_deadline(deadline)
                head = _read_head(os.path.join(root, name))
                agent_name = head.get('name') if head else None
                if agent_name and agent_name not in index:
                    index[agent_name] = head
    _check_deadline(deadline)
    return index


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise Unclear(f'Definition search takes longer than {TIME_BUDGET_S:g} s; definition cannot be determined.')


def find_definition(name: str, directories: list[str], deadline: float | None = None) -> dict[str, str] | None:
    """Frontmatter of the .md file with name == name. The first hit wins (order = precedence).
    Limits exceeded (file count, time budget) → Unclear."""
    key = tuple(directories)
    index = _INDEX.get(key)
    if index is None:
        index = _build_index(directories, time.monotonic() + TIME_BUDGET_S if deadline is None else deadline)
        _INDEX[key] = index
    return index.get(name)


def _complete(head: dict[str, str] | None, rules: Rules) -> bool:
    return bool(head) and bool(head.get('model')) and head.get('effort') in rules.effort_values


def _hint(rules: Rules) -> str:
    if rules.suggested_agents:
        return (f'Use one of these agents ({", ".join(rules.suggested_agents)}) or another agent with model: and '
                'effort: in its frontmatter.')
    return 'Define an agent with model: and effort: in its frontmatter and start that one.'


def _definition(agent_type: str, directories: list[str], rules: Rules) -> tuple[dict[str, str], str | None]:
    """(frontmatter, problem). problem is None if the definition sets model and effort."""
    try:
        head = find_definition(agent_type, directories, time.monotonic() + TIME_BUDGET_S)
    except Unclear as e:
        return {}, f'Agent "{agent_type}": {e}'
    if head is not None and head.get('_unclear'):
        return {}, (f'Agent "{agent_type}": {head["_unclear"]}; model and effort cannot be determined. '
                    'Close the value in the frontmatter of the definition.')
    if not _complete(head, rules):
        missing = 'no definition' if head is None else 'definition without model: and/or effort:'
        return {}, f'Agent "{agent_type}": {missing}. Model and effort cannot be determined this way. {_hint(rules)}'
    return head, None


def _override(call_model, head: dict[str, str], agent_type: str) -> str | None:
    if call_model and call_model != head.get('model'):
        return f'Call model "{call_model}" overrides the frontmatter model "{head.get("model")}" of "{agent_type}".'
    return None


def _apply_override(override: str | None, rules: Rules, prefix: str = '') -> Check:
    """Check result for a complete definition, depending on `model_override_in_call`."""
    if override and rules.model_override_in_call == 'deny':
        return Check(False, f'{prefix}{override} A model in the call is not allowed here (model_override_in_call).',
                     agent_count=1)
    notes = [prefix + override] if override and rules.model_override_in_call == 'warn' else []
    return Check(True, notes=notes, agent_count=1)


def check_agent(tool_input: dict, directories: list[str], rules: Rules = DEFAULT_RULES) -> Check:
    agent_type = tool_input.get('subagent_type') or 'general-purpose'
    head, problem = _definition(agent_type, directories, rules)
    if problem:
        return Check(False, problem, agent_count=1)
    return _apply_override(_override(tool_input.get('model'), head, agent_type), rules)


# --- Skills with context: fork ------------------------------------------------------

# Command names: name, cmd-subfolder:name (commands/a/b.md), path/to/folder:name (nested skill)
SKILL_COMMAND = re.compile(r'(?:[A-Za-z0-9_-]{1,100}/)*[A-Za-z0-9_-]{1,100}(?::[A-Za-z0-9_-]{1,100})*')
# context: fork in YAML spellings the simple frontmatter parser does not resolve (block scalar, tags, anchors,
# quoted key, flow map, continuation line, comments before the value). When in doubt it counts as a fork: warned
# and counted, not missed. Between the colon and fork: white space, line breaks, comments up to the line end,
# tags, anchors and a block indicator, in any order ('context: !!str # c\n  fork').
_FORK_GAP = r'(?:\s|#[^\n]*\n|(?:![^\s]*|&[^\s]+)(?=\s)|[>|][-+0-9]*)*'
FORK_SUSPECT = re.compile(r'(?:^|[\s{,])["\']?context["\']?\s*:' + _FORK_GAP + r'["\']?fork\b', re.IGNORECASE)
# An alias (*name) can stand for any anchored value or key, "fork" and "context" included
_ALIAS = re.compile(r'(?:^|[:\[{,?-])[ \t]*\*[^\s,\[\]{}]', re.MULTILINE)
# Escapes of double-quoted YAML strings: \x, \u, \U, escaped line break, any other escaped character
_YAML_ESCAPE = re.compile(r'\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|\r?\n[ \t]*|(.))', re.DOTALL)


def _unescaped(text: str) -> str | None:
    """The text with YAML escapes resolved, for the fork check only ("f\\x6frk" \u2192 "fork"). None if unresolvable."""
    def one(m):
        code = m.group(1) or m.group(2) or m.group(3)
        return chr(int(code, 16)) if code else (m.group(4) or '')
    try:
        return _YAML_ESCAPE.sub(one, text)
    except (ValueError, OverflowError):
        return None


def _fork_suspected(front: str, cut: bool) -> bool:
    """Fail closed: context: fork in any spelling, an alias, escapes that cannot be resolved, or a frontmatter
    that does not end within the part that was read (cut = the file is longer than that part)."""
    plain = _unescaped(front)
    return (plain is None or cut or bool(FORK_SUSPECT.search(front) or FORK_SUSPECT.search(plain)
                                         or _ALIAS.search(front)))


def _read_skill_head(path: str) -> dict[str, str] | None:
    """Frontmatter of a skill or command file. Only the first HEAD_CHARS bytes, decoded tolerantly: size and
    encoding must not hide a fork. '_fork' = 'yes' for a fork or a suspected fork."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, HEAD_CHARS + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    text = raw[:HEAD_CHARS].decode('utf-8', errors='replace')
    head = _head(text)
    rest = text.lstrip('\ufeff')
    if rest.startswith('---'):
        end = rest.find('\n---', 3)
        if _fork_suspected(rest[3:end if end > 0 else len(rest)], end < 0 and len(raw) > HEAD_CHARS):
            head['_fork'] = 'yes'
    if (head.get('context') or '').casefold() == 'fork':
        head['_fork'] = 'yes'
    return head


def find_skill(name: str, skill_dirs: list[str], command_dirs: list[str], deadline: float,
               cwd: str | None = None) -> dict[str, str] | None:
    """Frontmatter of the skill that /name starts: skill folders before command files, each in precedence order.
    Match by folder name (exact first) or frontmatter name, case-insensitive.
    Names with ':' → nested skill (<cwd>/<path>/.claude/skills/<name>) or command subfolder (commands/a/b.md);
    plugin skills are not found this way and stay out of scope. Limits exceeded → Unclear."""
    if ':' in name:
        front, _, last = name.rpartition(':')
        if cwd:
            head = _read_skill_head(os.path.join(cwd, *front.split('/'), '.claude', 'skills', last, 'SKILL.md'))
            if head is not None:
                return head
        if '/' in name:
            return None
        for base in command_dirs:
            head = _read_skill_head(os.path.join(base, *name.split(':')[:-1], last + '.md'))
            if head is not None:
                return head
        return None
    target = name.casefold()
    for base in skill_dirs:
        if not os.path.isdir(base):
            continue
        head = _read_skill_head(os.path.join(base, name, 'SKILL.md'))
        if head is not None:
            return head
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        if len(entries) > MAX_FILES:
            raise Unclear(f'Skill directory "{base}" has more than {MAX_FILES} entries; skill cannot be determined.')
        for entry in entries:
            _check_deadline(deadline)
            head = _read_skill_head(os.path.join(base, entry, 'SKILL.md'))
            if head is not None and target in (entry.casefold(), (head.get('name') or '').casefold()):
                return head
    for base in command_dirs:
        head = _read_skill_head(os.path.join(base, name + '.md'))
        if head is not None:
            return head
    return None


def check_skill(tool_input: dict, skill_dirs: list[str], command_dirs: list[str], agent_dirs: list[str],
                rules: Rules = DEFAULT_RULES, cwd: str | None = None) -> Check | None:
    """None = not responsible (no fork, unknown, plugin skill, invalid name).
    A fork starts an agent: the usage thresholds apply (agent_count=1). A fork without a complete agent
    definition is reported by default, not denied (`skill_fork_missing_agent`)."""
    raw = tool_input.get('skill')
    parts = raw.split() if isinstance(raw, str) else []
    name = parts[0].lstrip('/') if parts else ''
    if not SKILL_COMMAND.fullmatch(name) or ('/' in name and ':' not in name):
        return None
    try:
        head = find_skill(name, skill_dirs, command_dirs, time.monotonic() + TIME_BUDGET_S,
                          cwd if isinstance(cwd, str) else None)
    except Unclear as e:
        return Check(True, notes=[f'Skill "{name}": {e}'], agent_count=1)
    if head is None or head.get('_fork') != 'yes':
        return None
    if not rules.require:
        return Check(True, agent_count=1)
    agent_type = head.get('agent') or 'general-purpose'
    if head.get('_unclear'):
        # agent: and model: of the skill itself cannot be read either
        definition, problem = {}, f'{head["_unclear"]}; the agent cannot be determined.'
    elif agent_type in rules.exceptions:
        return Check(True, notes=[f'Skill "{name}" forks to "{agent_type}" (exception).'], agent_count=1)
    else:
        definition, problem = _definition(agent_type, agent_dirs, rules)
    if problem:
        text = f'Skill "{name}" (context: fork): {problem} In the skill, set agent: to an agent with model: and effort:.'
        if rules.skill_fork_missing_agent == 'deny':
            return Check(False, text, agent_count=1)
        return Check(True, notes=[text], agent_count=1)
    return _apply_override(_override(head.get('model'), definition, agent_type), rules, f'Skill "{name}": ')


# --- Workflow scripts: tokenizer -----------------------------------------------------

# ASCII only. JavaScript also counts U+FEFF and every Unicode space as white space, Python's \s misses U+FEFF.
# Every other character reaches the name pattern, which rejects Zs, Zl, Zp and Cf (Unclear).
_RE_SPACE = re.compile(r'[ \t\n\r\v\f]+')
_INVISIBLE = ('Zs', 'Zl', 'Zp', 'Cf')
# A line comment (and the #! line) ends at LF, CR, U+2028 or U+2029, as in JavaScript
_RE_LINE_END = re.compile('[\n\r\u2028\u2029]')
_RE_NAME = re.compile(r'(?:[A-Za-z_$]|[^\x00-\x7f])(?:[\w$]|[^\x00-\x7f])*')
_RE_NUMBER = re.compile(r'(?:0[xXoObB][0-9a-fA-F_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?)n?')
_RE_STRING = {q: re.compile(q + r'[^' + q + r'\\\n\r]*(?:\\[\s\S][^' + q + r'\\\n\r]*)*' + q) for q in '\'"'}
_RE_TEMPLATE = re.compile(r'[^`\\$]*(?:(?:\\[\s\S]|\$(?!\{))[^`\\$]*)*')
_RE_REGEX = re.compile(r'/(?:[^/\\\[\n\r]|\\[^\n\r]|\[(?:[^\]\\\n\r]|\\[^\n\r])*\])+/[A-Za-z]*')
# After these characters or keywords a '/' starts a regex literal, otherwise it is a division
_BEFORE_REGEX = set('(,=:[!&|?{;+-*%<>~^') | {'=>', '...'}
_KEYWORDS_BEFORE_REGEX = {'return', 'typeof', 'instanceof', 'in', 'new', 'delete', 'void', 'throw',
                          'case', 'do', 'else', 'await', 'extends', 'default'}
# Keyword or identifier depending on context: a '/' after them is ambiguous
_AMBIGUOUS_BEFORE_REGEX = {'of', 'yield'}
_CONTROL_HEAD = {'if', 'while', 'for', 'with'}
_PAIRS = {')': '(', ']': '[', '}': '{'}
_RE_ESCAPE = re.compile(r'\\u\{([0-9a-fA-F]{1,8})\}|\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})')
_AMBIGUOUS = 'ambiguous regex literal, script cannot be checked'


def _not_property(toks, k: int) -> bool:
    return k < 1 or toks[k - 1] not in (('p', '.'), ('p', '?.'))


def _control_head(toks, opening: int) -> str | None:
    """Keyword before the bracket toks[opening] ('if', 'while', 'for', 'with'), else None. for await (...) is for."""
    if opening < 1 or toks[opening - 1][0] != 'id':
        return None
    value = toks[opening - 1][1]
    if value == 'await' and opening >= 2 and toks[opening - 2] == ('id', 'for') and _not_property(toks, opening - 2):
        return 'for'
    return value if value in _CONTROL_HEAD and _not_property(toks, opening - 1) else None


def _invisible(text: str) -> str | None:
    """The first non-ASCII space or format character in text (Unicode categories Zs, Zl, Zp, Cf), else None."""
    if text.isascii():
        return None
    return next((c for c in text if c > '\x7f' and unicodedata.category(c) in _INVISIBLE), None)


def _regex_suspicious(text: str) -> bool:
    """Fail closed: the regex content names agent/workflow or a forbidden identifier (also via escapes)."""
    try:
        plain = _RE_ESCAPE.sub(lambda m: chr(int(m.group(1) or m.group(2) or m.group(3), 16)), text)
    except (ValueError, OverflowError):
        return True
    plain = plain.replace('\\', '')
    if 'agent' in plain or 'workflow' in plain:
        return True
    return any(re.search(r'(?<![\w$])' + re.escape(w) + r'(?![\w$])', plain) for w in _FORBIDDEN)


def _tokens(q: str, deadline: float) -> tuple[list[tuple[str, str]], list[int]]:
    """Linear tokenizer with a state stack. Comments are dropped. Kinds: id, num, str, tpl (without ${}),
    tpl_a/tpl_m/tpl_e (start/middle/end of a template literal with ${}), re, p (punctuation).
    partner[i] points to the counterpart for brackets and tpl_a/tpl_e."""
    toks: list[tuple[str, str]] = []
    partner: list[int] = []
    stack: list[int] = []
    i, n, rounds = 0, len(q), 0
    if q.startswith('#!'):
        m = _RE_LINE_END.search(q)
        i = m.start() if m else n

    def new(kind: str, value: str) -> int:
        toks.append((kind, value))
        partner.append(-1)
        return len(toks) - 1

    def template_piece(j: int) -> tuple[int, str, str]:
        m = _RE_TEMPLATE.match(q, j)
        k = m.end()
        if q.startswith('${', k):
            return k + 2, m.group(), '${'
        if k < n and q[k] == '`':
            return k + 1, m.group(), '`'
        raise Unclear('Template literal not closed; script cannot be checked.')

    while i < n:
        rounds += 1
        if not rounds & 1023 and time.monotonic() > deadline:
            raise Unclear(f'Check takes longer than {TIME_BUDGET_S:g} s; script cannot be checked.')
        c = q[i]
        m = _RE_SPACE.match(q, i)
        if m:
            i = m.end()
            continue
        if q.startswith('//', i):
            m = _RE_LINE_END.search(q, i)
            i = m.start() if m else n
            continue
        if q.startswith('/*', i):
            e = q.find('*/', i + 2)
            if e < 0:
                raise Unclear('Block comment not closed; script cannot be checked.')
            i = e + 2
            continue
        if c in '\'"':
            m = _RE_STRING[c].match(q, i)
            if not m:
                raise Unclear('String not closed; script cannot be checked.')
            new('str', m.group()[1:-1])
            i = m.end()
            continue
        if c == '`':
            i, text, end = template_piece(i + 1)
            if end == '`':
                new('tpl', text)
            else:
                stack.append(new('tpl_a', text))
                if len(stack) > MAX_DEPTH:
                    raise Unclear(f'Nesting deeper than {MAX_DEPTH}; script cannot be checked.')
            continue
        if c == '}' and stack and toks[stack[-1]][0] == 'tpl_a':
            # end of a ${...} expression: back into the template literal
            i, text, end = template_piece(i + 1)
            if end == '${':
                new('tpl_m', text)
            else:
                opening = stack.pop()
                closing = new('tpl_e', text)
                partner[opening], partner[closing] = closing, opening
            continue
        if c == '/':
            before = toks[-1] if toks else None
            is_id = before is not None and before[0] == 'id' and _not_property(toks, len(toks) - 1)
            if ((before == ('p', '}') and _RE_REGEX.match(q, i))
                    or (is_id and before[1] in _AMBIGUOUS_BEFORE_REGEX and _RE_REGEX.match(q, i))):
                # end of a block or identifier/keyword: regex and division are both possible
                raise Unclear(f'"/" after "{before[1]}": {_AMBIGUOUS}.')
            if (before is None or before[0] in ('tpl_a', 'tpl_m') or (before[0] == 'p' and before[1] in _BEFORE_REGEX)
                    or (is_id and before[1] in _KEYWORDS_BEFORE_REGEX)
                    or (before == ('p', ')') and _control_head(toks, partner[-1]))):
                m = _RE_REGEX.match(q, i)
                if not m:
                    raise Unclear('Regex literal not readable; script cannot be checked.')
                if _regex_suspicious(m.group()):
                    raise Unclear(f'Regex literal {m.group()[:40]!r} names agent, workflow or a forbidden '
                                  f'identifier: {_AMBIGUOUS}.')
                new('re', m.group())
                i = m.end()
                continue
        if q.startswith('<!--', i):
            raise Unclear('HTML comment "<!--": comment or code depending on the runtime mode, cannot be checked.')
        if q.startswith('-->', i):
            line = max(q.rfind(z, 0, i) for z in ('\n', '\r', '\u2028', '\u2029'))
            before_text = q[line + 1:i]
            if not before_text.strip() or '*/' in before_text:
                raise Unclear('HTML comment "-->" at the start of a line: comment or code depending on the runtime '
                              'mode, cannot be checked.')
        if c == '\\':
            raise Unclear('Unicode escape (\\u...) in an identifier outside of strings; '
                          'model and effort cannot be determined.')
        m = _RE_NUMBER.match(q, i) if c.isdigit() or (c == '.' and q[i + 1:i + 2].isdigit()) else None
        if m:
            new('num', m.group())
            i = m.end()
            continue
        m = _RE_NAME.match(q, i)
        if m:
            # every non-ASCII character outside strings, comments and regex literals ends up here
            hidden = _invisible(m.group())
            if hidden:
                raise Unclear(f'Invisible or non-ASCII space character U+{ord(hidden):04X} outside of strings and '
                              'comments; script cannot be checked.')
            new('id', m.group())
            i = m.end()
            continue
        if q.startswith('...', i):
            new('p', '...')
            i += 3
            continue
        if q.startswith('?.', i) and not q[i + 2:i + 3].isdigit():
            new('p', '?.')
            i += 2
            continue
        if q.startswith('=>', i) or q.startswith('++', i) or q.startswith('--', i):
            new('p', q[i:i + 2])
            i += 2
            continue
        k = new('p', c)
        i += 1
        if c in '([{':
            stack.append(k)
            if len(stack) > MAX_DEPTH:
                raise Unclear(f'Nesting deeper than {MAX_DEPTH}; script cannot be checked.')
        elif c in ')]}':
            if not stack or toks[stack[-1]] != ('p', _PAIRS[c]):
                raise Unclear('Brackets do not match; script cannot be checked.')
            opening = stack.pop()
            partner[opening], partner[k] = k, opening
    if stack:
        raise Unclear('Brackets or template literal not closed; script cannot be checked.')
    return toks, partner


# --- Workflow scripts: evaluation ----------------------------------------------------

# Ways to reach agent/workflow without the name (global object, code from strings, constructor chain)
_FORBIDDEN = {'eval', 'Function', 'globalThis', 'global', 'window', 'self', 'this', 'arguments', 'Reflect',
              'require', 'import', 'constructor', '__proto__', 'with'}
_FORBIDDEN_KEYS = {'constructor', '__proto__', 'prototype', 'agent', 'workflow'}
_DEFINITION = {'function', 'const', 'let', 'var', 'class'}


# Calls whose argument may run more than once
_REPEATING_CALLS = {'parallel', 'pipeline', 'map', 'flatMap', 'forEach', 'all', 'allSettled'}


def _repeated(toks, partner) -> list[bool]:
    """Per token: is it (conservatively) inside a function/arrow function, a loop or a call of
    parallel/pipeline/map/forEach/Promise.all? One linear pass with a level stack."""
    result = [False] * len(toks)
    # level: [repeated through bracket or parent, arrow expression open, loop body without braces open]
    levels = [[False, False, False]]
    for i, t in enumerate(toks):
        level = levels[-1]
        following = toks[i + 1] if i + 1 < len(toks) else None
        if t[0] == 'tpl_a' or (t[0] == 'p' and t[1] in '([{'):
            more = any(level)
            before = toks[i - 1] if i else None
            if t == ('p', '(') and (_control_head(toks, i) in ('for', 'while')
                                    or (before and before[0] == 'id' and before[1] in _REPEATING_CALLS)):
                more = True
            elif t == ('p', '{') and before is not None:
                if before == ('p', '=>') or (before == ('id', 'do') and _not_property(toks, i - 1)):
                    more = True
                elif before == ('p', ')') and _control_head(toks, partner[i - 1]) in (None, 'for', 'while'):
                    head = toks[partner[i - 1] - 1] if partner[i - 1] > 0 else None
                    # not an if/switch/catch block, otherwise a loop or function body
                    more = more or head not in (('id', 'switch'), ('id', 'catch'))
            result[i] = more
            levels.append([more, False, False])
            continue
        if t[0] == 'tpl_e' or (t[0] == 'p' and t[1] in ')]}'):
            if len(levels) > 1:
                levels.pop()
            result[i] = any(levels[-1])
            if t == ('p', ')') and _control_head(toks, partner[i]) in ('for', 'while') and following != ('p', '{'):
                levels[-1][2] = True
            continue
        result[i] = any(level)
        if t == ('p', '=>') and following != ('p', '{'):
            level[1] = True
        elif t == ('id', 'do') and following != ('p', '{') and _not_property(toks, i):
            level[2] = True
        elif t == ('p', ','):
            level[1] = False
        elif t == ('p', ';'):
            level[1] = False
            if following != ('id', 'else'):
                level[2] = False
    return result


def _expression_end(t: tuple[str, str] | None) -> bool:
    return t is not None and (t[0] in ('id', 'num', 'str', 'tpl', 'tpl_e', 're') or t[1] in (')', ']'))


def _split(toks, partner, start: int, stop: int) -> list[tuple[int, int]]:
    """Splits toks[start:stop] at top-level commas. Inner brackets are skipped via partner."""
    parts, begin, j = [], start, start
    while j < stop:
        if partner[j] > j:
            j = partner[j] + 1
            continue
        if toks[j] == ('p', ','):
            parts.append((begin, j))
            begin = j + 1
        j += 1
    parts.append((begin, stop))
    if parts[-1][0] == parts[-1][1]:
        parts.pop()                                      # trailing comma
    return parts


def _literal(toks, start: int, stop: int) -> str | None:
    """The value if toks[start:stop] is exactly one string literal without escapes, else None."""
    if stop - start == 1 and toks[start][0] in ('str', 'tpl') and '\\' not in toks[start][1]:
        return toks[start][1]
    return None


def _literals(toks, partner, start: int, stop: int) -> list[str] | None:
    """Possible values: one string literal or exactly `condition ? 'a' : 'b'` with two string literals.
    Nested conditions, ?? and anything else → None (not determinable)."""
    single = _literal(toks, start, stop)
    if single is not None:
        return [single]
    question = colon = None
    j = start
    while j < stop:
        if partner[j] > j:
            j = partner[j] + 1
            continue
        if toks[j] == ('p', '?'):
            if question is not None:
                return None
            question = j
        elif toks[j] == ('p', ':'):
            if question is None or colon is not None:
                return None
            colon = j
        j += 1
    if question is None or colon is None or question == start:
        return None
    yes, no = _literal(toks, question + 1, colon), _literal(toks, colon + 1, stop)
    return [yes, no] if yes is not None and no is not None else None


def _object(toks, partner, start: int, stop: int) -> dict[str, tuple[int, int]]:
    """Entries of an object literal toks[start:stop] as {key: (start, stop) of the value}. Raises Unclear
    for anything that is not a fixed key (spread, computed, method, duplicate)."""
    if not (stop - start >= 2 and toks[start] == ('p', '{') and partner[start] == stop - 1):
        raise Unclear(OPTIONS_FIXED)
    entries: dict[str, tuple[int, int]] = {}
    for a, b in _split(toks, partner, start + 1, stop - 1):
        kind, value = toks[a]
        if toks[a] == ('p', '...'):
            raise Unclear(f'{OPTIONS_FIXED}; spread (...) is not allowed.')
        if kind not in ('id', 'str', 'num') or (kind == 'str' and '\\' in value):
            raise Unclear(f'{OPTIONS_FIXED}; computed or unreadable key.')
        if b - a == 1 and kind == 'id':
            value_range = (a, b)                         # shorthand {model}: the value is a variable
        elif b - a >= 3 and toks[a + 1] == ('p', ':'):
            value_range = (a + 2, b)
        else:
            raise Unclear(f'{OPTIONS_FIXED}; entry "{value}" is not key: value.')
        if value in entries:
            raise Unclear(f'{OPTIONS_FIXED}; key "{value}" appears twice.')
        entries[value] = value_range
    return entries


def _read_script(path: str, cwd: str | None) -> str:
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    unreadable = f'Workflow script "{path}" not readable; model and effort cannot be checked.'
    try:
        # O_NONBLOCK: a FIFO must not block the hook; only a regular file counts as a script
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    except (OSError, ValueError):
        raise Unclear(unreadable)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Unclear(f'Workflow script "{path}" is not a regular file; cannot be checked.')
        with os.fdopen(fd, encoding='utf-8', closefd=False) as f:
            text = f.read(MAX_SCRIPT + 1)
    except (OSError, ValueError):
        raise Unclear(unreadable)
    finally:
        os.close(fd)
    if len(text) > MAX_SCRIPT:
        raise Unclear(f'Workflow script "{path}" too large; cannot be checked.')
    return text


def _stored(name: str, cwd: str | None) -> str:
    """Path of a stored workflow from the registry folders. Not resolvable → Unclear."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
        raise Unclear(f'Stored workflow "{name}": name cannot be resolved; cannot be checked.')
    folders = ([os.path.join(cwd, '.claude', 'workflows')] if cwd else []) + [os.path.expanduser('~/.claude/workflows')]
    for base in folders:
        for extension in ('.js', '.mjs'):
            path = os.path.join(base, name + extension)
            if os.path.isfile(path):
                return path
    listed = ', '.join('"' + base + '"' for base in folders)       # quoted: the log masks it (paths from cwd)
    raise Unclear(f'Stored workflow "{name}" not found in {listed}; cannot be checked.')


def _check_agent_call(toks, partner, opening: int, directories: list[str], deadline: float, rules: Rules) -> str | None:
    """Problem text for one agent(...) call, or None if model and effort are determined."""
    arguments = _split(toks, partner, opening + 1, partner[opening])
    if len(arguments) > 2:
        return 'more than two arguments'
    options: dict[str, list[str]] = {}
    if len(arguments) == 2:
        try:
            entries = _object(toks, partner, *arguments[1])
        except Unclear as e:
            return str(e)
        for key in ('model', 'effort', 'agentType'):
            if key in entries:
                values = _literals(toks, partner, *entries[key])
                if values is None:
                    return (f'{OPTIONS_FIXED}; {key} is not a string literal '
                            "(allowed: 'x' or condition ? 'x' : 'y')")
                options[key] = values
    for model in options.get('model', [None]):
        if model is not None and not rules.model_pattern.fullmatch(model):
            return f'model "{model}" unknown'
    for effort in options.get('effort', [None]):
        if effort is not None and effort not in rules.effort_values:
            return f'effort "{effort}" invalid'
    # Every possible combination must set model and effort determinably
    for model_option in options.get('model', [None]):
        for effort_option in options.get('effort', [None]):
            for agent_type in options.get('agentType', [None]):
                model, effort = model_option, effort_option
                if agent_type:
                    # options in the call take precedence, otherwise the frontmatter of the agentType applies
                    head = find_definition(agent_type, directories, deadline) or {}
                    if head.get('_unclear'):
                        return f'agentType "{agent_type}": {head["_unclear"]}; model and effort cannot be determined'
                    model = model or head.get('model')
                    effort = effort or head.get('effort')
                missing = [part for part, ok in (('model', bool(model)), ('effort', effort in rules.effort_values))
                           if not ok]
                if missing:
                    return f'{" and ".join(missing)} missing' + (f' (agentType "{agent_type}")' if agent_type else '')
    return None


def _workflow_target(toks, partner, opening: int, cwd: str | None) -> str:
    """Path of the child script of a workflow(...) call: literal name or literal {scriptPath: '...'}."""
    arguments = _split(toks, partner, opening + 1, partner[opening])
    if arguments:
        a, b = arguments[0]
        name = _literal(toks, a, b)
        if name is not None:
            return _stored(name, cwd)
        if toks[a] == ('p', '{'):
            entries = _object(toks, partner, a, b)
            path = _literal(toks, *entries['scriptPath']) if list(entries) == ['scriptPath'] else None
            if path:
                return path
    raise Unclear("workflow(...) can only be checked with a literal name or {scriptPath: '...'}.")


def _check_source(source: str, directories: list[str], cwd: str | None, child: bool, deadline: float,
                  rules: Rules) -> Check:
    toks, partner = _tokens(source, deadline)
    repeated = _repeated(toks, partner)
    repeated_seen = False
    problems: list[str] = []
    child_problems: list[str] = []
    count = 0
    for i, (kind, value) in enumerate(toks):
        before = toks[i - 1] if i else None
        if kind == 'id' and value in _FORBIDDEN:
            raise Unclear(f'Identifier "{value}" in the script allows dynamic access; model and effort '
                          'cannot be determined.')
        if (kind in ('str', 'tpl') and value in _FORBIDDEN_KEYS and before == ('p', '[')
                and _expression_end(toks[i - 2] if i >= 2 else None)):
            raise Unclear(f'Access x["{value}"] in the script; model and effort cannot be determined.')
        if kind != 'id' or value not in ('agent', 'workflow'):
            continue
        if before in (('p', '.'), ('p', '?.')):
            raise Unclear(f'"{value}" as a property (x.{value}); only direct calls {value}(...) can be checked.')
        if before is not None and before[0] == 'id' and before[1] in _DEFINITION:
            raise Unclear(f'"{value}" is redefined in the script; cannot be checked.')
        if i + 1 >= len(toks) or toks[i + 1] != ('p', '('):
            raise Unclear(f'"{value}" is used other than as a direct call (alias, property, optional call, '
                          f'parentheses, destructuring); only {value}(...) can be checked.')
        if value == 'agent':
            count += 1
            repeated_seen = repeated_seen or repeated[i]
            problem = _check_agent_call(toks, partner, i + 1, directories, deadline, rules) if rules.require else None
            if problem:
                problems.append(f'agent call {count}: {problem}')
            continue
        if child:
            raise Unclear('Child workflow calls workflow(...) itself; only depth 1 can be checked.')
        path = _workflow_target(toks, partner, i + 1, cwd)
        result = _check_source(_read_script(path, cwd), directories, cwd, True, deadline, rules)
        count += result.agent_count
        repeated_seen = repeated_seen or (repeated[i] and result.agent_count > 0)
        if not result.allowed:
            child_problems.append(f'Child workflow "{path}": {result.reason}')
    if repeated_seen:
        count = max(count, 2)          # call inside a loop/function/parallel: may run more than once
    if problems:
        reason = ('Workflow does not set model and effort for every agent(...) call: ' + '; '.join(problems) +
                  '. Add {model, effort} as string literals to the options (object literal in the second argument)'
                  ' or an agentType with a complete definition.')
        return Check(False, ' '.join([reason] + child_problems), agent_count=count)
    if child_problems:
        return Check(False, ' '.join(child_problems), agent_count=count)
    if not count:
        return Check(True, notes=['No agent(...) call found.'])
    return Check(True, agent_count=count)


def _not_checkable(error: Unclear, rules: Rules) -> Check:
    # A script that cannot be analysed: its agents cannot be counted. With the model check off it is allowed.
    return Check(False, str(error), agent_count=None) if rules.require else Check(True, agent_count=None)


def check_workflow_script(source: str, directories: list[str], cwd: str | None = None,
                          rules: Rules = DEFAULT_RULES) -> Check:
    try:
        return _check_source(source, directories, cwd, False, time.monotonic() + TIME_BUDGET_S, rules)
    except Unclear as e:
        return _not_checkable(e, rules)


def check_workflow(tool_input: dict, directories: list[str], cwd: str | None = None,
                   rules: Rules = DEFAULT_RULES) -> Check:
    """Workflow tool: check an inline script, a script path or a stored workflow (name)."""
    deadline = time.monotonic() + TIME_BUDGET_S
    script = tool_input.get('script')
    path = tool_input.get('scriptPath')
    name = tool_input.get('name')
    try:
        if isinstance(script, str) and script.strip():
            text = script
        elif isinstance(path, str) and path:
            text = _read_script(path, cwd)
        elif isinstance(name, str) and name:
            text = _read_script(_stored(name, cwd), cwd)
        else:
            raise Unclear('Workflow call without script, script path or name; cannot be checked.')
        return _check_source(text, directories, cwd, False, deadline, rules)
    except Unclear as e:
        return _not_checkable(e, rules)
