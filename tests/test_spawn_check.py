"""Tests for hooks/_spawn_check.py: definition search, workflow script analysis, skill forks and the rules
from the configuration. Every script here is synthetic; no test reads real session or workflow files."""
import os
import threading
import time

import pytest

import _config
import _spawn_check as sc

OPT = "{ model: 'haiku', effort: 'low' }"


@pytest.fixture(autouse=True)
def fresh_index(monkeypatch):
    """The definition cache lives per process; every test starts with an empty one."""
    monkeypatch.setattr(sc, '_INDEX', {})


def rules(**overrides):
    return sc.Rules.from_config({**_config.DEFAULTS['spawn_rules'], **overrides})


def script(text, directories=(), cwd=None, rule_set=sc.DEFAULT_RULES):
    return sc.check_workflow_script(text, list(directories), cwd, rule_set)


@pytest.fixture
def agents(tmp_path):
    base = tmp_path / 'agents'

    def create(name, head, file_name='SKILL.md', bom=False):
        (base / name).mkdir(parents=True, exist_ok=True)
        text = ('\ufeff' if bom else '') + f'---\nname: {name}\n{head}\n---\nText\n'
        (base / name / file_name).write_text(text, encoding='utf-8')
    create('full-def', 'description: Test\nmodel: opus\neffort: high')
    return base, create


# --- Scanner evasion (alias, property, optional call, regex literals, escapes) ----------------------

@pytest.mark.parametrize('code', [
    "const a = agent; await a('x')",
    "await globalThis.agent('x')",
    "await agent?.('x')",
    "await (agent)('x')",
    "agent.call(null, 'x')",
    "const {agent: a} = globalThis",
    "const {agent: a} = ctx; await a('x', " + OPT + ")",
    "await x.agent('x', " + OPT + ")",
    "await \\u0061gent('x', " + OPT + ")",
    "function agent(p, o) { return 1 }",
    "const w = workflow; await w({scriptPath: '/x/k.js'})",
    "await this['agent']('x')",
    "await Function('return ag' + 'ent')()('x')",
    "await [].map['constructor']('return 1')()",
])
def test_evasion_is_denied(code):
    p = script(code)
    assert not p.allowed, code
    assert p.reason


# Each case is allowed without its rule: the reason proves which rule denied it.
@pytest.mark.parametrize('code,expected', [
    # '/' after 'of' may start a regex: here it would swallow agent(...) into a string token
    ("for (const s of /(')/) agent('p', " + OPT + ")//'))", '"/" after "of": ambiguous regex literal'),
    # a string with an escape is no readable literal: JavaScript sees another value
    ("agent('p', {model: 'opu\\x73', effort: 'low'})", 'model is not a string literal'),
    ("agent('p', {model: 'opus', effort: 'lo\\u0077'})", 'effort is not a string literal'),
    ("agent('p', {agentType: 'full\\x2ddef'})", 'agentType is not a string literal'),
    # ?? in a condition: one side may be a variable
    ("agent('p', {model: c ? x ?? 'opus' : 'haiku', effort: 'low'})", 'model is not a string literal'),
    ("agent('p', {model: c ?? d ? 'opus' : 'haiku', effort: 'low'})", 'model is not a string literal'),
    # optional chaining is a property access: x?.agent may be any function
    ("x?.agent('p', " + OPT + ")", '"agent" as a property'),
    ("x?.workflow({scriptPath: '/x/k.js'})", '"workflow" as a property'),
])
def test_fail_closed_rules_deny_with_their_reason(code, expected):
    p = script(code)
    assert not p.allowed and expected in p.reason, (code, p.reason)


def test_workflow_name_or_script_path_with_an_escape_is_not_read_literally(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'a\\x2fb.js').write_text("await agent('p', " + OPT + ")\n", encoding='utf-8')  # the literal name
    (tmp_path / 'a').mkdir()
    (tmp_path / 'a' / 'b.js').write_text("for (;;) await agent('evil')\n", encoding='utf-8')  # what JS would load
    for code in (f"await workflow({{scriptPath: '{tmp_path}/a\\x2fb.js'}})", "await workflow('go\\x6fd')"):
        p = script(code, cwd=str(tmp_path))
        assert not p.allowed and 'can only be checked with a literal name' in p.reason, (code, p.reason)


# Listed here as well: an entry removed from the code must still be tested
FORBIDDEN_NAMES = {'eval', 'Function', 'globalThis', 'global', 'window', 'self', 'this', 'arguments', 'Reflect',
                   'require', 'import', 'constructor', '__proto__', 'with'}
FORBIDDEN_KEYS = {'constructor', '__proto__', 'prototype', 'agent', 'workflow'}


@pytest.mark.parametrize('name', sorted(FORBIDDEN_NAMES | sc._FORBIDDEN))
def test_every_forbidden_identifier_is_denied(name):
    p = script(f"{name}['ag' + 'ent']('p', {OPT})")
    assert not p.allowed and f'Identifier "{name}" in the script allows dynamic access' in p.reason, p.reason


@pytest.mark.parametrize('key', sorted(FORBIDDEN_KEYS | sc._FORBIDDEN_KEYS))
def test_every_forbidden_key_is_denied(key):
    p = script(f"x['{key}']")
    assert not p.allowed and f'Access x["{key}"] in the script' in p.reason, p.reason


# --- Characters JavaScript reads differently from the tokenizer --------------------------------------

@pytest.mark.parametrize('code,char', [
    ("await agent\ufeff('x', {model: 'opus'})", 'U+FEFF'),         # JavaScript: a space, so agent(...) runs
    ("await agent\u00a0('x', {model: 'opus'})", 'U+00A0'),
    ("await agent \u00a0('x', " + OPT + ")", 'U+00A0'),                # after an ASCII space, too
    ("await agent(\n\u2003'x', " + OPT + ")", 'U+2003'),
    ("await\u3000agent('x', " + OPT + ")", 'U+3000'),
    ("\u200bawait agent('x', " + OPT + ")", 'U+200B'),
    ("await agent('x', " + OPT + ")\u2028", 'U+2028'),
    ("// note\u2029agent('x')", 'U+2029'),                          # the comment ends at U+2029
    ("const a\u00ad = 1", 'U+00AD'),
])
def test_invisible_and_unicode_space_characters_are_not_determinable(code, char):
    p = script(code)
    assert not p.allowed and char in p.reason and 'script cannot be checked' in p.reason, (code, p.reason)


def test_unicode_spaces_inside_strings_comments_and_regex_literals_stay_allowed():
    p = script("/* a\u00a0b\ufeff\u2028 */ // c\u00a0d\nconst s = 'x\u00a0y\ufeff'; const t = `\u2028`\n"
               "const r = /\u00a0/\nawait agent('p', " + OPT + ")")
    assert p.allowed and p.agent_count == 1, p.reason


@pytest.mark.parametrize('code', ["// note\ragent('x')", "#!node\ragent('x')"])
def test_line_comment_ends_at_a_carriage_return(code):
    p = script(code)
    assert not p.allowed and 'model and effort missing' in p.reason, p.reason
    assert script(code.replace("agent('x')", "agent('x', " + OPT + ")")).agent_count == 1


def test_comment_between_name_and_bracket_is_a_direct_call():
    assert not script("agent/**/('x')").allowed
    p = script("await agent/* comment */('x', " + OPT + ")")
    assert p.allowed and p.agent_count == 1


def test_double_quotes_are_recognised():
    p = script('await agent("x")')
    assert not p.allowed and 'model and effort missing' in p.reason


def test_regex_literal_with_quote_swallows_no_code():
    assert not script("const r = /'/; await agent('x'); const s = 'y'").allowed
    p = script("const r = /'/; await agent('x', " + OPT + "); const s = 'y'")
    assert p.allowed and p.agent_count == 1


def test_regex_literal_with_escaped_slashes():
    assert not script('const r = /\\/\\//; await agent("x")').allowed
    p = script('const r = /\\/\\//g; const d = 4 / 2 / 1; await agent("x", ' + OPT + ')')
    assert p.allowed and p.agent_count == 1


# --- Regex/division confusion that swallowed agent(...) -----------------------------------------------

@pytest.mark.parametrize('code', [
    "if (1) /'/; agent('x'); //'",
    'while (0) /"/; agent(\'y\'); //"',
    "export const meta = { name: 'x', description: 'y' }\nif (1) /'/; await agent('free'); //'",
    "let q = {} / 1; agent('z'); let w = 1 /1",
    "let a = 1; a++ /1; agent('q'); let b = 2/2",
    "<!-- `\nagent(\"x\")\n//`",
    "x = 1\n--> `\nagent('x')\n//`",
    "if (x) {} /'/; agent('x'); //'",
    "for await (const x of y) /'/; agent('x'); //'",
    "for (const x of /'/; agent('x'); //'",
    "let of = 4; of / \"'/\"; agent('x'); //'",
    "const r = x.return / \"'/\"; agent('x'); //'",
])
def test_regex_division_repro_cases_denied(code):
    p = script(code)
    assert not p.allowed, code


@pytest.mark.parametrize('code', [
    "const r = /agent/",
    "const r = /\\u0061gent/",
    "const r = /x/; const s = /work\\x66low/",
    "const r = /(^|\\s)eval(/",
])
def test_regex_naming_agent_or_a_forbidden_identifier_is_ambiguous(code):
    p = script(code)
    assert not p.allowed and 'ambiguous' in p.reason, (code, p.reason)


def test_regex_division_heuristic_legitimate():
    for code in ("if (ok) /x/.test(s); await agent('a', " + OPT + ")",
                 "const d = f(1) / 2 / g(3); await agent('a', " + OPT + ")",
                 "let i = 1; i++ / 2; await agent('a', " + OPT + ")",
                 "const t = 1 + /x/.source.length; await agent('a', " + OPT + ")",
                 "const o = {a: 1}.a / 2; const e = {} / 2\nawait agent('a', " + OPT + ")",
                 "const x = a.if(1) / 2 / 3; await agent('a', " + OPT + ")",
                 "while (i --> 0) {}\nawait agent('a', " + OPT + ")",
                 "const r = /it's/; await agent('a', " + OPT + ")"):
        p = script(code)
        assert p.allowed and p.agent_count == 1, (code, p.reason)


def test_agent_only_in_strings_and_comments_does_not_count():
    p = script("// agent('x')\nconst s = 'agent(1)'; const t = `no agent( here`; /* agent */")
    assert p.allowed and p.agent_count == 0


def test_nested_template_expressions():
    p = script("log(`a ${`b ${await agent('x', " + OPT + ")} c`} d`)")
    assert p.allowed and p.agent_count == 1
    assert not script("log(`a ${`b ${await agent('x')} c`} d`)").allowed


def test_incomplete_syntax_cannot_be_checked():
    for code in ("await agent('x', " + OPT, "const s = 'open", "/* open", "log(`${agent('x')`)"):
        assert not script(code).allowed, code


@pytest.mark.parametrize('code,expected', [
    ("const r = /\\u{FFFFFFFF}/", 'ambiguous'),              # escape outside the Unicode range
    ("const r = (/\n/)", 'Regex literal not readable'),
    ("f(]", 'Brackets do not match'),
    ("}", 'Brackets do not match'),
    ("agent('x', {model, effort: 'low'})", 'model is not a string literal'),     # shorthand {model}
    ("agent('x', {model: m : 'haiku', effort: 'low'})", 'model is not a string literal'),
    ("agent('x', {model: a ? 'haiku' : 'opus' : 'x', effort: 'low'})", 'model is not a string literal'),
])
def test_tokenizer_and_literal_edge_cases_are_denied(code, expected):
    p = script(code)
    assert not p.allowed and expected in p.reason, (code, p.reason)


def test_template_with_several_expressions():
    p = script("log(`${a} and ${await agent('x', " + OPT + ")} and ${b}`)")
    assert p.allowed and p.agent_count == 1


def test_unreadable_definition_file_is_skipped(agents):
    base, create = agents
    create('locked', 'model: opus\neffort: high')
    (base / 'locked' / 'SKILL.md').chmod(0)
    try:
        assert sc.find_definition('locked', [str(base)]) is None
        assert sc.find_definition('full-def', [str(base)]) is not None
    finally:
        (base / 'locked' / 'SKILL.md').chmod(0o600)


def test_shebang_line_is_skipped():
    assert script("#!/usr/bin/env node\nawait agent('x', " + OPT + ")").agent_count == 1
    assert script('#!only a shebang').agent_count == 0


# --- Faked options (prompt text, comments, spread, duplicate keys) ------------------------------------

@pytest.mark.parametrize('code', [
    "await agent('use model: \"haiku\", effort: \"low\"')",
    "agent('x', {/* model: 'haiku', effort: 'low' */})",
    "const o = {model: 'opus', effort: 'max'}; agent('x', {model: 'haiku', effort: 'low', ...o})",
    "agent('x', {effort: 'low', model: 'haiku', effort: args.e})",
    "agent('x', {model: 'whatever', effort: 'low'})",
    "agent('agentType: \"full-def\"')",
    "agent('x', {model: 'haiku', effort: 'low', 'mod\\u0065l': 'opus'})",
    "agent('x', {model: 'haiku', [k]: 'low'})",
    "agent('x', {model: 'haiku', get effort() { return 'low' }})",
    "agent('x', {model: `hai${'ku'}`, effort: 'low'})",
    "agent('x', {model: 'haiku', effort: 'mega'})",
])
def test_faked_options_are_denied(code, agents):
    base, _ = agents
    assert not script(code, [str(base)]).allowed, code


def test_options_as_variable_need_an_object_literal():
    for code in ("const O = {model: 'haiku', effort: 'low'}; agent('x', O)",
                 "const M = 'haiku'; agent('x', {model: M, effort: 'low'})"):
        p = script(code)
        assert not p.allowed and sc.OPTIONS_FIXED in p.reason, code


def test_quoted_keys_and_template_without_expression_allowed():
    p = script("agent('x', {'model': 'haiku', \"effort\": `low`, schema: S, label: `l:${n}`},)")
    assert p.allowed and p.agent_count == 1


def test_options_only_from_the_second_argument():
    assert not script("agent({model: 'haiku', effort: 'low'})").allowed
    assert not script("agent('x', {}, {model: 'haiku', effort: 'low'})").allowed


def test_model_pattern(agents):
    for m in ('sonnet', 'opus', 'haiku', 'fable', 'inherit', 'claude-opus-5-5'):
        assert script(f"agent('x', {{model: '{m}', effort: 'high'}})").allowed, m
    assert not script("agent('x', {model: 'Claude Opus', effort: 'high'})").allowed


def test_agent_type_with_definition(agents):
    base, _ = agents
    p = script("agent('x', {agentType: 'full-def'})", [str(base)])
    assert p.allowed and p.agent_count == 1
    assert not script("agent('x', {agentType: 'missing'})", [str(base)]).allowed


# --- Child workflows and stored workflows -------------------------------------------------------------

def test_child_workflow_by_script_path_is_checked(tmp_path):
    child = tmp_path / 'child.js'
    child.write_text("await agent('k')\n", encoding='utf-8')
    p = script(f"await workflow({{scriptPath: '{child}'}})")
    assert not p.allowed and 'Child workflow' in p.reason
    child.write_text("await agent('k', " + OPT + ")\nawait agent('l', " + OPT + ")\n", encoding='utf-8')
    p = script(f"await agent('e', {OPT}); await workflow({{scriptPath: '{child}'}}, {{a: 1}})")
    assert p.allowed and p.agent_count == 3


def test_child_workflow_problem_is_appended_to_the_parent_problem(tmp_path):
    child = tmp_path / 'child.js'
    child.write_text("await agent('k')\n", encoding='utf-8')
    p = script(f"await agent('e'); await workflow({{scriptPath: '{child}'}})")
    assert not p.allowed and 'agent call 1: model and effort missing' in p.reason and 'Child workflow' in p.reason


def test_child_workflow_must_not_call_a_workflow(tmp_path):
    grandchild = tmp_path / 'grandchild.js'
    grandchild.write_text("await agent('k', " + OPT + ")\n", encoding='utf-8')
    child = tmp_path / 'child.js'
    child.write_text(f"await workflow({{scriptPath: '{grandchild}'}})\n", encoding='utf-8')
    p = script(f"await workflow({{scriptPath: '{child}'}})")
    assert not p.allowed and 'depth 1' in p.reason


@pytest.mark.parametrize('code', [
    "await workflow('foreign')",
    "await workflow(target)",
    "await workflow({scriptPath: path})",
    "await workflow({scriptPath: '/does/not/exist.js'})",
    "await workflow({scriptPath: '/x/k.js', extra: 1})",
])
def test_unresolvable_workflow_is_denied(code, tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    assert not script(code, cwd=str(tmp_path)).allowed, code


def test_stored_workflow_by_name_is_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    folder = tmp_path / '.claude' / 'workflows'
    folder.mkdir(parents=True)
    (folder / 'good.mjs').write_text("await agent('g', " + OPT + ")\n", encoding='utf-8')
    (folder / 'bad.js').write_text("await agent('s')\n", encoding='utf-8')
    cwd = str(tmp_path)
    assert script("await workflow('good')", cwd=cwd).allowed
    assert not script("await workflow('bad')", cwd=cwd).allowed
    assert not script("await workflow('../good')", cwd=cwd).allowed
    p = sc.check_workflow({'name': 'good'}, [], cwd=cwd)
    assert p.allowed and p.agent_count == 1
    assert not sc.check_workflow({'name': 'bad'}, [], cwd=cwd).allowed


def test_stored_workflow_in_the_user_folder(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    (home / '.claude' / 'workflows').mkdir(parents=True)
    (home / '.claude' / 'workflows' / 'mine.js').write_text("await agent('g', " + OPT + ")\n", encoding='utf-8')
    assert sc.check_workflow({'name': 'mine'}, [], cwd=None).allowed


def test_workflow_tool_with_only_an_unknown_name_denied(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    p = sc.check_workflow({'script': ' ', 'name': 'x'}, [], cwd=str(tmp_path))
    assert not p.allowed and 'not found' in p.reason
    assert not sc.check_workflow({}, []).allowed


def test_workflow_tool_script_path_relative_to_cwd(tmp_path):
    (tmp_path / 'w.js').write_text("await agent('x', " + OPT + ")\n", encoding='utf-8')
    p = sc.check_workflow({'scriptPath': 'w.js'}, [], cwd=str(tmp_path))
    assert p.allowed and p.agent_count == 1


def test_unreadable_and_too_large_scripts_cannot_be_checked(tmp_path, monkeypatch):
    p = sc.check_workflow({'scriptPath': str(tmp_path / 'missing.js')}, [])
    assert not p.allowed and 'not readable' in p.reason
    (tmp_path / 'latin.js').write_bytes('// caf\xe9\n'.encode('latin-1'))
    assert 'not readable' in sc.check_workflow({'scriptPath': str(tmp_path / 'latin.js')}, []).reason
    monkeypatch.setattr(sc, 'MAX_SCRIPT', 10)
    (tmp_path / 'big.js').write_text('const a = 1;\n' * 5, encoding='utf-8')
    assert 'too large' in sc.check_workflow({'scriptPath': str(tmp_path / 'big.js')}, []).reason


def test_fifo_or_folder_as_script_path_is_denied_without_blocking(tmp_path):
    fifo = tmp_path / 'wf.js'
    os.mkfifo(fifo)
    results = []
    worker = threading.Thread(target=lambda: results.append(sc.check_workflow({'scriptPath': str(fifo)}, [])),
                              daemon=True)
    start = time.monotonic()
    worker.start()
    worker.join(1.5)
    duration = time.monotonic() - start
    if worker.is_alive():                      # regression: the open blocks; release it so the suite goes on
        try:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        worker.join(5)
    assert duration < 1.5 and results, 'the FIFO blocked the check'
    assert not results[0].allowed and 'is not a regular file' in results[0].reason, results[0].reason
    p = sc.check_workflow({'scriptPath': str(tmp_path)}, [])
    assert not p.allowed and 'is not a regular file' in p.reason, p.reason


# --- Run time O(length * depth) → timeout → fail open ---------------------------------------------------

def _nested(depth, size=524_000):
    piece = '`${' * depth + '1' + '}`' * depth + ';\n'
    return piece * (size // len(piece)) + 'await agent("x", ' + OPT + ')\n'


def test_deep_nesting_is_denied_quickly():
    start = time.monotonic()
    p = script(_nested(360))
    assert not p.allowed and 'Nesting' in p.reason
    assert time.monotonic() - start < 1.0


def test_bracket_nesting_is_limited_too():
    p = script('(' * (sc.MAX_DEPTH + 1) + ')' * (sc.MAX_DEPTH + 1))
    assert not p.allowed and 'Nesting' in p.reason


def test_large_script_with_allowed_depth_stays_linear():
    start = time.monotonic()
    p = script(_nested(40))
    duration = time.monotonic() - start
    assert duration < sc.TIME_BUDGET_S, duration
    assert p.allowed and p.agent_count == 1


def test_time_budget_exceeded_is_denied(monkeypatch):
    monkeypatch.setattr(sc, 'TIME_BUDGET_S', -1.0)
    p = script('const a = 1;\n' * 2000 + "await agent('x', " + OPT + ")")
    assert not p.allowed and 'longer than' in p.reason


# --- Frontmatter parser (long head, inline comment, BOM) -----------------------------------------------

def test_frontmatter_long_head_comment_and_bom(agents):
    base, create = agents
    create('long-desc', 'description: ' + 'x' * 5000 + '\nmodel: sonnet\neffort: high')
    create('with-comment', 'description: Test\nmodel: haiku  # cheap\neffort: low # enough')
    create('with-bom', 'description: Test\nmodel: opus\neffort: high', bom=True)
    create('quoted', 'description: "Test # no comment"\nmodel: \'sonnet\'\neffort: "medium"')
    for name in ('long-desc', 'with-comment', 'with-bom', 'quoted'):
        assert sc.check_agent({'subagent_type': name}, [str(base)]).allowed, name
    head = sc.find_definition('quoted', [str(base)])
    assert head['description'] == 'Test # no comment' and head['effort'] == 'medium'
    assert sc.find_definition('with-comment', [str(base)])['effort'] == 'low'


def test_frontmatter_without_start_or_end_marker_is_empty(tmp_path):
    folder = tmp_path / 'ag'
    folder.mkdir()
    (folder / 'a.md').write_text('name: a\nmodel: opus\neffort: high\n', encoding='utf-8')
    (folder / 'b.md').write_text('---\nname: b\nmodel: opus\neffort: high\n', encoding='utf-8')
    (folder / 'c.md').write_text('---\nname: c\n: no key\n---\n', encoding='utf-8')
    (folder / 'notes.txt').write_text('---\nname: d\nmodel: opus\neffort: high\n---\n', encoding='utf-8')
    for name in ('a', 'b', 'd'):
        assert sc.find_definition(name, [str(folder)]) is None, name
    assert sc.find_definition('c', [str(folder)]) == {'name': 'c'}


def test_broken_md_file_does_not_stop_the_search(agents):
    base, _ = agents
    (base / 'broken.md').write_bytes(b'\xff\xfe\x00not utf-8')
    assert sc.find_definition('full-def', [str(base)]) is not None


# Read line by line, the lines inside a multi-line value look like keys. For YAML (PyYAML checked) they belong
# to the value: there is no model: and no effort:, so the definition is incomplete.
@pytest.mark.parametrize('head', [
    'description: "does things\nmodel: haiku\neffort: low\n"',
    "description: 'does things\nmodel: haiku\neffort: low\n'",
    'description: "x\\"\nmodel: haiku\neffort: low\n"',               # an escaped quote does not close
    "description: 'it''s\nmodel: haiku\neffort: low\n'",               # '' does not close
    'description: "abc\\\nmodel: haiku\neffort: low\n"',              # an escaped line break does not close
    'description: !!str "x\nmodel: haiku\neffort: low\n"',            # a tag in front of the quote
    'meta:\n  note: "x\nmodel: haiku\neffort: low\n"',                # quoted value of a nested key
    'tools:\n  - "x\nmodel: haiku\neffort: low\n"',                   # quoted list item
    'tools: ["x\nmodel: haiku\neffort: low\n"]',                      # quoted value in a [ ] list
    'tools: [a,\nmodel: haiku,\neffort: low]',                        # a [ ] list over several lines
])
def test_lines_inside_a_multi_line_value_are_no_keys(agents, head):
    base, create = agents
    create('sneaky', head)
    head_read = sc.find_definition('sneaky', [str(base)])
    assert 'model' not in head_read and 'effort' not in head_read and '_unclear' not in head_read, head_read
    p = sc.check_agent({'subagent_type': 'sneaky'}, [str(base)])
    assert not p.allowed and 'Agent "sneaky": definition without model: and/or effort:' in p.reason, p.reason
    p = script("agent('x', {agentType: 'sneaky'})", [str(base)])
    assert not p.allowed and 'agentType "sneaky"' in p.reason, p.reason


# A plain value goes on in the next more indented line: Claude Code reads 'low more', which is no valid effort,
# and would start the agent with the session's effort. So the value is not read.
@pytest.mark.parametrize('head', [
    'model: haiku\neffort: low\n  more',
    'model: haiku\n  more\neffort: low',
    'description: |\n  text\nmodel: haiku\neffort: low\n  more',
])
def test_a_plain_value_that_goes_on_in_the_next_line_is_not_read(agents, head):
    base, create = agents
    create('continued', head)
    p = sc.check_agent({'subagent_type': 'continued'}, [str(base)])
    assert not p.allowed and 'definition without model: and/or effort:' in p.reason, p.reason


@pytest.mark.parametrize('head', [
    'model: haiku\n  # a comment line\neffort: low',
    'description: first line\n  second line\nmodel: haiku\neffort: low',
])
def test_comment_lines_and_continued_other_keys_do_not_hide_model_and_effort(agents, head):
    base, create = agents
    create('fine', head)
    assert sc.check_agent({'subagent_type': 'fine'}, [str(base)]).allowed


# The indented lines of a block scalar are skipped; a key in column 0 ends the block. PyYAML reads the same
# model and effort.
@pytest.mark.parametrize('head', [
    'description: |\n  text\n  model: sonnet\nmodel: opus\neffort: high',
    'description: >-\n  text\n\n  more\nmodel: opus\neffort: high',
    'description: !!str >-\n  text\nmodel: opus\neffort: high',
    'description: !!str |\n  text\nmodel: opus\neffort: high',
    'description: |- # comment\n  "text\n  more\nmodel: opus\neffort: high',   # a quote inside the block opens nothing
    'meta:\n  note: |\n    text\n    model: sonnet\nmodel: opus\neffort: high',   # block of a nested key
    'description: |\n  text\n\n  "quoted line\nmodel: opus\neffort: high',     # a blank line does not end the block
    'description: >\n  text\n\n  [not a list\nmodel: opus\neffort: high',
])
def test_block_scalars_are_skipped_and_the_keys_after_them_are_read(agents, head):
    base, create = agents
    create('block', head)
    head_read = sc.find_definition('block', [str(base)])
    assert head_read['model'] == 'opus' and head_read['effort'] == 'high' and '_unclear' not in head_read, head_read
    assert head_read.get('description', '') == ''
    assert sc.check_agent({'subagent_type': 'block'}, [str(base)]).allowed
    assert script("agent('x', {agentType: 'block'})", [str(base)]).allowed


@pytest.mark.parametrize('head', [
    'model: haiku\neffort: low\ndescription: "never closed',
    "model: haiku\neffort: low\ndescription: 'never closed\nmore text",
    'model: haiku\neffort: low\ntools: [a, b',
    'model: haiku\neffort: low\ndescription: "a---b"',        # Claude Code ends the frontmatter at the ---
])
def test_frontmatter_that_ends_inside_a_value_is_not_determinable(agents, head):
    base, create = agents
    create('open', head)
    assert sc.find_definition('open', [str(base)])['_unclear'] == sc.OPEN_VALUE
    p = sc.check_agent({'subagent_type': 'open'}, [str(base)])
    assert not p.allowed and 'Agent "open": the frontmatter ends inside a quoted value' in p.reason, p.reason
    assert 'Close the value' in p.reason
    p = script("agent('x', {agentType: 'open'})", [str(base)])
    assert not p.allowed and 'agentType "open": the frontmatter ends inside a quoted value' in p.reason, p.reason


def test_quoted_values_closed_on_their_line_stay_readable(agents):
    base, create = agents
    create('plain', 'description: "say \\"hi\\" # no comment"\nmodel: \'opus\'\neffort: high\nnote: \'it\'\'s fine\'')
    assert sc.check_agent({'subagent_type': 'plain'}, [str(base)]).allowed
    assert '_unclear' not in sc.find_definition('plain', [str(base)])


# Quotes, | and > inside a plain value, in a comment or in a closed list open nothing
@pytest.mark.parametrize('head', [
    "description: Zoë's agent", 'description: He said "hi there', 'description: >5 files', 'description: a | b',
    'description: x # it\'s "open', "# it's a comment", 'tools: ["Read", "Grep"]', 'tools: [\n  "Read",\n  "Grep"\n]',
    'description: "line one\n  line two"', 'description: Use when: the user asks "x"',
])
def test_quotes_inside_plain_values_comments_and_closed_lists_open_nothing(agents, head):
    base, create = agents
    create('fine', head + '\nmodel: haiku\neffort: low')
    head_read = sc.find_definition('fine', [str(base)])
    assert (head_read['model'], head_read['effort']) == ('haiku', 'low') and '_unclear' not in head_read, head_read


# Claude Code 2.1 ends the frontmatter at the first '---' anywhere (/^---\s*\n([\s\S]*?)---\s*\n?/):
# `description: a---b` leaves it with name and description 'a' only.
def test_frontmatter_ends_at_the_first_dashes_like_claude_code(agents):
    base, create = agents
    create('dashy', 'description: a---b\nmodel: haiku\neffort: low')
    create('dashes-late', 'model: haiku\neffort: low\ndescription: a---b')
    assert sc.find_definition('dashy', [str(base)]) == {'name': 'dashy', 'description': 'a'}
    p = sc.check_agent({'subagent_type': 'dashy'}, [str(base)])
    assert not p.allowed and 'Agent "dashy": definition without model: and/or effort:' in p.reason, p.reason
    assert sc.check_agent({'subagent_type': 'dashes-late'}, [str(base)]).allowed
    folder = base.parent / 'other'
    folder.mkdir()
    (folder / 'a.md').write_text('---\nname: a\nmodel: haiku\neffort: low---\n', encoding='utf-8')
    (folder / 'b.md').write_text('----\nname: b\nmodel: haiku\neffort: low\n---\n', encoding='utf-8')
    assert sc.find_definition('a', [str(folder)]) == {'name': 'a', 'model': 'haiku', 'effort': 'low'}
    assert sc.find_definition('b', [str(folder)]) is None   # '----' does not open a frontmatter


# --- find_definition limits, cache and followlinks ------------------------------------------------------

def _many_md(folder, count):
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (folder / f'd{i}.md').write_text(f'---\nname: n{i}\n---\n', encoding='utf-8')


def test_too_many_files_not_determinable_and_fast(tmp_path):
    _many_md(tmp_path / 'ag', sc.MAX_FILES + 1)
    start = time.monotonic()
    code = '; '.join(f"agent('p', {{agentType: 'u{i}'}})" for i in range(45))
    p = script(code, [str(tmp_path / 'ag')])
    assert not p.allowed and 'more than' in p.reason
    assert not sc.check_agent({'subagent_type': 'n1'}, [str(tmp_path / 'ag')]).allowed
    assert time.monotonic() - start < 1.0


def test_definition_search_once_per_process(tmp_path, monkeypatch):
    _many_md(tmp_path / 'ag', 50)
    calls = []
    real = os.walk
    monkeypatch.setattr(sc.os, 'walk', lambda *a, **k: calls.append(1) or real(*a, **k))
    code = '; '.join(f"agent('p', {{agentType: 'u{i}'}})" for i in range(45))
    assert not script(code, [str(tmp_path / 'ag')]).allowed
    assert sc.find_definition('n7', [str(tmp_path / 'ag')])['name'] == 'n7'
    assert len(calls) == 1


def test_definition_search_time_budget(tmp_path, monkeypatch):
    _many_md(tmp_path / 'ag', 3)
    monkeypatch.setattr(sc, 'TIME_BUDGET_S', -1.0)
    p = sc.check_agent({'subagent_type': 'n1'}, [str(tmp_path / 'ag')])
    assert not p.allowed and 'longer than' in p.reason
    p = script("agent('x', {agentType: 'n1'})", [str(tmp_path / 'ag2')])
    assert not p.allowed


def test_definition_search_shares_the_deadline_with_the_tokenizer(tmp_path):
    _many_md(tmp_path / 'ag', 3)
    with pytest.raises(sc.Unclear):
        sc.find_definition('n1', [str(tmp_path / 'ag')], time.monotonic() - 1)


def test_definition_search_symlinks_and_limits(tmp_path, agents):
    base, create = agents
    root_link = tmp_path / 'root-link'
    root_link.symlink_to(base)
    assert sc.find_definition('full-def', [str(root_link)]) is not None      # the root may be a symlink
    foreign = tmp_path / 'foreign'
    (foreign / 'x').mkdir(parents=True)
    (foreign / 'x' / 'SKILL.md').write_text('---\nname: via-link\nmodel: opus\neffort: high\n---\n', encoding='utf-8')
    create('large', 'model: opus\neffort: high\ndescription: ' + 'x' * (sc.MAX_FILE_BYTES + 10))
    (base / 'link-folder').symlink_to(foreign)
    os.mkfifo(base / 'fifo.md')
    start = time.monotonic()
    assert sc.find_definition('via-link', [str(base)]) is None               # symlinked subfolder not entered
    assert sc.find_definition('large', [str(base)]) is None                  # > 256 KB not read
    assert sc.find_definition('full-def', [str(base)]) is not None
    assert time.monotonic() - start < 1.0                                    # the FIFO does not block
    assert not sc.check_agent({'subagent_type': 'large'}, [str(base)]).allowed


def test_first_directory_wins_and_missing_directories_are_skipped(tmp_path):
    for folder, effort in (('project', 'low'), ('user', 'high')):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / 'same.md').write_text(f'---\nname: same\nmodel: opus\neffort: {effort}\n---\n',
                                                   encoding='utf-8')
    order = [str(tmp_path / 'missing'), str(tmp_path / 'project'), str(tmp_path / 'user')]
    assert sc.find_definition('same', order)['effort'] == 'low'


# --- Agent counting: loops, map and parallel count as at least two ----------------------------------------

@pytest.mark.parametrize('code', [
    "await parallel([1,2,3,4,5,6].map(i => () => agent('p', " + OPT + ")))",
    "for (let i = 0; i < 50; i++) await agent('p', " + OPT + ")",
    "for (const x of xs) { await agent('p', " + OPT + ") }",
    "for await (const x of xs) await agent('p', " + OPT + ")",
    "while (more) { await agent('p', " + OPT + ") }",
    "do await agent('p', " + OPT + "); while (more)",
    "do { await agent('p', " + OPT + ") } while (more)",
    "async function run(x) { return agent('p', " + OPT + ") }\nawait run(1)",
    "const run = async (x) => { return agent('p', " + OPT + ") }",
    "const run = x => agent('p', " + OPT + ")",
    "xs.forEach(function (x) { agent('p', " + OPT + ") })",
    "await Promise.all([agent('p', " + OPT + ")])",
    "await pipeline(xs, agent('p', " + OPT + "))",
    "const o = { async run() { await agent('p', " + OPT + ") } }",
    "for (const x of xs) if (x) { await agent('p', " + OPT + ") }",
])
def test_agent_in_loop_or_function_counts_more_than_once(code):
    p = script(code)
    assert p.allowed, (code, p.reason)
    assert p.agent_count >= 2, code


@pytest.mark.parametrize('code', [
    "await agent('p', " + OPT + ")",
    "const f = () => 1; await agent('p', " + OPT + ")",
    "const f = async () => { return 1 }\nawait agent('p', " + OPT + ")",
    "if (x) { await agent('p', " + OPT + ") } else { log(1) }",
    "try { await agent('p', " + OPT + ") } catch (e) { log(e) }",
    "try { x() } catch (e) { await agent('p', " + OPT + ") }",
    "for (const x of xs) log(x); await agent('p', " + OPT + ")",
    "switch (m) { case 1: await agent('p', " + OPT + ") }",
    "log(`${await agent('p', " + OPT + ")}`)",
])
def test_single_agent_stays_one(code):
    p = script(code)
    assert p.allowed and p.agent_count == 1, (code, p.agent_count)


def test_child_workflow_in_loop_counts_more_than_once(tmp_path):
    child = tmp_path / 'child.js'
    child.write_text("await agent('k', " + OPT + ")\n", encoding='utf-8')
    assert script(f"await workflow({{scriptPath: '{child}'}})").agent_count == 1
    p = script(f"for (const x of xs) await workflow({{scriptPath: '{child}'}})")
    assert p.allowed and p.agent_count == 2


def test_child_workflow_without_agents_in_a_loop_counts_none(tmp_path):
    child = tmp_path / 'empty.js'
    child.write_text("log('nothing')\n", encoding='utf-8')
    p = script(f"for (const x of xs) {{ await workflow({{scriptPath: '{child}'}}) }}")
    assert p.allowed and p.agent_count == 0, p.agent_count


def test_repetition_does_not_change_whether_it_is_allowed():
    assert not script("for (;;) await agent('p')").allowed
    assert not script("xs.map(x => agent('p', {model: 'haiku'}))").allowed


# --- Legitimate scripts stay allowed (synthetic stand-ins for real multi-phase workflows) -----------------

LINEAR_FOUR = """export const meta = { name: 'phase-one', description: 'Four steps in a row' }
const inventory = await agent(`List ${args.files.length} files`, { label: 'list', model: 'haiku', effort: 'low' })
const review = await agent('Review the list', { agentType: 'reviewer', schema: S })
const plan = await agent('Plan', { model: deep ? 'opus' : 'sonnet', effort: deep ? 'high' : 'medium' })
const verdict = await agent("Judge", { label: `judge ${plan.id}`, agentType: strict ? 'reviewer' : 'worker-fast' })
"""

MULTI_PHASE = """export const meta = { name: 'multi-phase', description: 'Fan-out with a child workflow' }
const files = args.files
const found = await parallel(files.map(f => () => agent(`Check ${f}`, { agentType: 'worker-fast' })))
const merged = await pipeline([
  () => agent('Merge', { model: 'sonnet', effort: 'high' }),
  () => agent('Judge', { model: 'opus', effort: big ? 'max' : 'high' }),
])
for (const group of groups) {
  await agent(`Group ${group}`, { model: 'haiku', effort: 'low', schema: S })
}
await workflow({ scriptPath: 'CHILD' })
"""


@pytest.fixture
def workflow_agents(tmp_path):
    folder = tmp_path / 'wf-agents'
    folder.mkdir()
    for name, head in (('reviewer', 'model: opus\neffort: high'), ('worker-fast', 'model: haiku\neffort: low')):
        (folder / f'{name}.md').write_text(f'---\nname: {name}\ndescription: Test\n{head}\n---\n', encoding='utf-8')
    return [str(folder)]


def test_synthetic_linear_script_allowed_with_four_call_sites(tmp_path, workflow_agents):
    path = tmp_path / 'phase-one.js'
    path.write_text(LINEAR_FOUR, encoding='utf-8')
    p = sc.check_workflow({'scriptPath': str(path)}, workflow_agents)
    assert p.allowed, p.reason
    assert p.agent_count == 4       # four agent(...) call sites in the source


def test_synthetic_multi_phase_script_allowed_and_counted_as_a_block(tmp_path, workflow_agents):
    child = tmp_path / 'child.js'
    child.write_text("await agent('Child', { agentType: 'reviewer' })\n", encoding='utf-8')
    path = tmp_path / 'multi-phase.js'
    path.write_text(MULTI_PHASE.replace('CHILD', str(child)), encoding='utf-8')
    p = sc.check_workflow({'scriptPath': str(path)}, workflow_agents)
    assert p.allowed, p.reason
    assert p.agent_count >= 2 and p.agent_count == 5
    # one side of a ternary without a definition makes the whole script fail
    path.write_text(LINEAR_FOUR.replace("'worker-fast'", "'unknown-agent'"), encoding='utf-8')
    p = sc.check_workflow({'scriptPath': str(path)}, workflow_agents)
    assert not p.allowed and 'agent call 4: model and effort missing (agentType "unknown-agent")' in p.reason


def test_script_without_agent_calls_gives_a_note():
    p = script("export const meta = { name: 'x', description: 'y' }\nlog(1)")
    assert p.allowed and p.agent_count == 0 and p.notes == ['No agent(...) call found.']


# --- Agent tool: hints, overrides and configured values --------------------------------------------------

def test_agent_without_definition_gets_the_general_hint_by_default(agents):
    base, _ = agents
    p = sc.check_agent({}, [str(base)])
    assert not p.allowed and 'Agent "general-purpose": no definition' in p.reason
    assert 'Define an agent with model: and effort:' in p.reason


def test_agent_without_definition_names_the_suggested_agents(agents):
    base, create = agents
    create('half-def', 'model: sonnet')
    p = sc.check_agent({'subagent_type': 'half-def'}, [str(base)], rules(suggested_agents=['worker-fast', 'reviewer']))
    assert not p.allowed and 'definition without model: and/or effort:' in p.reason
    assert 'Use one of these agents (worker-fast, reviewer)' in p.reason


@pytest.mark.parametrize('setting,allowed,notes', [('allow', True, 0), ('warn', True, 1), ('deny', False, 0)])
def test_model_override_in_the_call(agents, setting, allowed, notes):
    base, _ = agents
    p = sc.check_agent({'subagent_type': 'full-def', 'model': 'haiku'}, [str(base)],
                       rules(model_override_in_call=setting))
    assert p.allowed is allowed and len(p.notes) == notes and p.agent_count == 1
    if setting == 'warn':
        assert p.notes[0] == 'Call model "haiku" overrides the frontmatter model "opus" of "full-def".'
    if setting == 'deny':
        assert 'overrides' in p.reason and 'model_override_in_call' in p.reason
    same = sc.check_agent({'subagent_type': 'full-def', 'model': 'opus'}, [str(base)],
                          rules(model_override_in_call=setting))
    assert same.allowed and same.notes == []


def test_custom_effort_values_and_model_pattern(agents):
    base, create = agents
    create('fast-def', 'model: local-small\neffort: quick')
    custom = rules(effort_values=['quick', 'thorough'], model_pattern='local-[a-z]+')
    assert sc.check_agent({'subagent_type': 'fast-def'}, [str(base)], custom).allowed
    assert not sc.check_agent({'subagent_type': 'fast-def'}, [str(base)]).allowed
    assert not sc.check_agent({'subagent_type': 'full-def'}, [str(base)], custom).allowed   # effort: high unknown
    assert script("agent('x', {model: 'local-big', effort: 'thorough'})", rule_set=custom).allowed
    p = script("agent('x', {model: 'haiku', effort: 'quick'})", rule_set=custom)
    assert not p.allowed and 'model "haiku" unknown' in p.reason
    p = script("agent('x', {model: 'local-big', effort: 'low'})", rule_set=custom)
    assert not p.allowed and 'effort "low" invalid' in p.reason


def test_rules_from_the_defaults_are_the_default_rules():
    assert rules() == sc.DEFAULT_RULES
    assert sc.DEFAULT_RULES.require and sc.DEFAULT_RULES.exceptions == frozenset({'claude-code-guide',
                                                                                  'statusline-setup'})


# --- Model check off: only counting ---------------------------------------------------------------------

def test_model_check_off_counts_without_judging():
    off = rules(require_model_and_effort=False)
    p = script("await agent('a'); await agent('b', {model: 'nonsense'})", rule_set=off)
    assert p.allowed and p.reason == '' and p.agent_count == 2
    p = script("for (const x of xs) await agent('a')", rule_set=off)
    assert p.allowed and p.agent_count == 2


@pytest.mark.parametrize('tool_input', [{'script': "const a = agent; await a('x')"}, {'script': 'agent(('},
                                        {'scriptPath': '/does/not/exist.js'}, {}])
def test_model_check_off_and_script_not_analysable_leaves_the_count_unknown(tool_input):
    off = rules(require_model_and_effort=False)
    p = sc.check_workflow(tool_input, [], rules=off)
    assert p.allowed and p.agent_count is None and p.notes == []
    assert not sc.check_workflow(tool_input, []).allowed


def test_model_check_off_still_resolves_child_workflows(tmp_path):
    child = tmp_path / 'child.js'
    child.write_text("await agent('k')\nawait agent('l')\n", encoding='utf-8')
    p = script(f"await agent('e'); await workflow({{scriptPath: '{child}'}})",
               rule_set=rules(require_model_and_effort=False))
    assert p.allowed and p.agent_count == 3


# --- Skill search and skill forks ---------------------------------------------------------------------------

def skill_file(path, head, raw=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(f'---\n{head}\n---\nText\n', encoding='utf-8')


def test_too_many_skill_entries_count_as_not_determinable(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, 'MAX_FILES', 2)
    for n in ('a', 'b', 'c'):
        (tmp_path / n).mkdir()
    p = sc.check_skill({'skill': 'x'}, [str(tmp_path)], [], [])
    assert p.allowed and p.agent_count == 1 and 'cannot be determined' in p.notes[0]


def test_skill_search_deadline_folder_name_and_direct_precedence(tmp_path):
    base = tmp_path / 'skills'
    skill_file(base / 'Large' / 'SKILL.md', 'name: other\ncontext: fork')
    skill_file(base / 'deploy' / 'SKILL.md', 'name: deploy\ndescription: inline')
    skill_file(base / 'aaa' / 'SKILL.md', 'name: deploy\ncontext: fork')
    late = 1e12
    assert sc.find_skill('large', [str(base)], [], late)['name'] == 'other'             # folder name, other case
    assert sc.find_skill('deploy', [str(base)], [], late)['description'] == 'inline'    # folder name before name:
    with pytest.raises(sc.Unclear):
        sc.find_skill('missing', [str(base)], [], 0.0)                                  # deadline passed


def test_protected_paths_of_the_skill_search(tmp_path):
    (tmp_path / 'skills' / 'folder' / 'SKILL.md').mkdir(parents=True)  # not a regular file
    assert sc.find_skill('folder', [str(tmp_path / 'skills')], [], 1e12) is None
    assert sc.find_skill('apps/missing:deploy', [], [str(tmp_path)], 1e12, cwd=str(tmp_path)) is None


def test_unreadable_skill_file_and_unlistable_folder_are_skipped(tmp_path):
    base = tmp_path / 'skills'
    skill_file(base / 'locked' / 'SKILL.md', 'name: locked\ncontext: fork')
    (base / 'locked' / 'SKILL.md').chmod(0)
    closed = tmp_path / 'closed'
    closed.mkdir()
    closed.chmod(0o300)                 # can be entered, not listed
    try:
        assert sc.find_skill('locked', [str(base)], [], 1e12) is None
        assert sc.find_skill('x', [str(closed)], [], 1e12) is None
    finally:
        (base / 'locked' / 'SKILL.md').chmod(0o600)
        closed.chmod(0o700)


def test_frontmatter_name_matches_in_any_case(tmp_path):
    base = tmp_path / 'skills'
    skill_file(base / 'folder-a' / 'SKILL.md', 'name: Quick\ncontext: fork')
    assert sc.find_skill('quick', [str(base)], [], 1e12)['name'] == 'Quick'
    assert sc.find_skill('QUICK', [str(base)], [], 1e12)['_fork'] == 'yes'


def test_fork_skill_rules(tmp_path, agents):
    base, create = agents
    skills = tmp_path / 'skills'
    skill_file(skills / 'full' / 'SKILL.md', 'name: full\ncontext: fork\nagent: full-def')
    skill_file(skills / 'bare' / 'SKILL.md', 'name: bare\ncontext: fork')
    skill_file(skills / 'guide' / 'SKILL.md', 'name: guide\ncontext: fork\nagent: claude-code-guide')
    skill_file(skills / 'model' / 'SKILL.md', 'name: model\ncontext: fork\nagent: full-def\nmodel: haiku')
    skill_file(skills / 'inline' / 'SKILL.md', 'name: inline\ndescription: knowledge')

    def run(name, rule_set=sc.DEFAULT_RULES):
        return sc.check_skill({'skill': name}, [str(skills)], [], [str(base)], rule_set)

    assert run('full') == sc.Check(True, agent_count=1)
    assert run('inline') is None and run('gone') is None
    bare = run('bare')
    assert bare.allowed and 'general-purpose' in bare.notes[0] and 'set agent: to an agent' in bare.notes[0]
    denied = run('bare', rules(skill_fork_missing_agent='deny'))
    assert not denied.allowed and denied.agent_count == 1 and 'Skill "bare" (context: fork)' in denied.reason
    assert 'exception' in run('guide').notes[0]
    assert run('model').notes == ['Skill "model": Call model "haiku" overrides the frontmatter model "opus" of "full-def".']
    assert run('model', rules(model_override_in_call='allow')).notes == []
    over = run('model', rules(model_override_in_call='deny'))
    assert not over.allowed and over.reason.startswith('Skill "model": ')
    off = rules(require_model_and_effort=False, skill_fork_missing_agent='deny')
    assert run('bare', off) == sc.Check(True, agent_count=1) and run('guide', off).notes == []


def test_fork_skill_with_an_open_value_cannot_name_its_agent(tmp_path, agents):
    base, _ = agents
    skills = tmp_path / 'skills'
    skill_file(skills / 'tricky' / 'SKILL.md', 'name: tricky\ncontext: fork\nagent: full-def\ndescription: "x')
    p = sc.check_skill({'skill': 'tricky'}, [str(skills)], [], [str(base)])
    assert p.allowed and p.agent_count == 1 and 'the frontmatter ends inside a quoted value' in p.notes[0], p.notes
    p = sc.check_skill({'skill': 'tricky'}, [str(skills)], [], [str(base)], rules(skill_fork_missing_agent='deny'))
    assert not p.allowed and 'Skill "tricky" (context: fork): the frontmatter ends inside' in p.reason, p.reason
    # agent: inside a closed multi-line value is no key: the fork goes to general-purpose
    skill_file(skills / 'hidden' / 'SKILL.md', 'name: hidden\ncontext: fork\ndescription: "x\nagent: full-def\n"')
    p = sc.check_skill({'skill': 'hidden'}, [str(skills)], [], [str(base)])
    assert p.allowed and '"general-purpose"' in p.notes[0], p.notes


# Claude Code ends the frontmatter at the first '---': context and agent behind it do not count for Claude Code,
# but a context: fork there is still treated as a suspected fork (checked rather than missed).
def test_fork_behind_inner_dashes_is_still_suspected(tmp_path, agents):
    base, _ = agents
    skills = tmp_path / 'skills'
    skill_file(skills / 'dashy' / 'SKILL.md', 'name: dashy\ndescription: a---b\ncontext: fork\nagent: full-def')
    head = sc.find_skill('dashy', [str(skills)], [], 1e12)
    assert head['_fork'] == 'yes' and 'context' not in head and 'agent' not in head, head
    p = sc.check_skill({'skill': 'dashy'}, [str(skills)], [], [str(base)])
    assert p.allowed and p.agent_count == 1 and '"general-purpose"' in p.notes[0], p.notes


@pytest.mark.parametrize('head', ['context: "f\\x6frk"', 'context: "\\u0066ork"', '"cont\\x65xt": fork',
                                  'x: &a fork\ncontext: *a', 'x: &k context\n*k : fork',
                                  'context: !<tag:yaml.org,2002:str> fork', 'context: &a !!str fork',
                                  'context: "fo\\\n  rk"', 'context: "\\U00110000"'])
def test_escaped_aliased_and_tagged_fork_is_suspected(tmp_path, head):
    skill_file(tmp_path / 'skills' / 'v' / 'SKILL.md', f'name: v\n{head}')
    assert sc.find_skill('v', [str(tmp_path / 'skills')], [], 1e12)['_fork'] == 'yes', head


# For YAML none of these is context: fork (a fork only in a comment, another value, a longer word)
@pytest.mark.parametrize('head', ['context: # fork', 'context: # c\n  inline', 'context: forking',
                                  'context: # c\n  # fork\n  inline'])
def test_fork_in_a_comment_or_as_part_of_a_word_is_no_fork(tmp_path, head):
    skill_file(tmp_path / 'skills' / 'v' / 'SKILL.md', f'name: v\n{head}')
    assert '_fork' not in sc.find_skill('v', [str(tmp_path / 'skills')], [], 1e12), head


def test_fork_suspect_stays_linear_on_long_gaps():
    for text in ('context:' + ' |' * 30000 + 'x', 'context:' + '#\n' * 30000 + 'x', 'context:' + ' !a' * 20000 + 'x'):
        start = time.monotonic()
        assert not sc.FORK_SUSPECT.search(text)
        assert time.monotonic() - start < 0.5


def test_frontmatter_beyond_the_read_limit_is_suspected_a_short_open_one_is_not(tmp_path):
    base = tmp_path / 'skills'
    skill_file(base / 'big' / 'SKILL.md', 'name: big\ndescription: ' + 'x' * (sc.HEAD_CHARS + 10) + '\ncontext: fork')
    skill_file(base / 'open' / 'SKILL.md', None, b'---\nname: open\ndescription: short file without an end marker\n')
    skill_file(base / 'escaped' / 'SKILL.md', 'name: escaped\ndescription: "say \\"hi\\" \\u00e9 *not an alias*"')
    assert sc.find_skill('big', [str(base)], [], 1e12)['_fork'] == 'yes'
    for name in ('open', 'escaped'):
        assert '_fork' not in sc.find_skill(name, [str(base)], [], 1e12), name


def test_the_agent_name_in_the_override_note_is_masked_in_the_log():
    import _hookio
    note = sc._override('opus', {'model': 'haiku'}, 'private-agent-name')
    assert '"private-agent-name"' in note
    assert 'private-agent-name' not in _hookio.mask_quoted(note)
