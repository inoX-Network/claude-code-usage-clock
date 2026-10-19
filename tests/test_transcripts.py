"""Tests for hooks/_transcripts.py beyond what the usage report tests read through it (tests/test_usage_report*.py)."""
import ast
import json
import os
import sys

import _transcripts
from conftest import HOOKS, ROOT


def test_the_module_imports_the_standard_library_only():
    """Every tool that reads transcripts can take it, from hooks/ as from tools/ or an installation."""
    with open(os.path.join(HOOKS, '_transcripts.py'), encoding='utf-8') as f:
        tree = ast.parse(f.read())
    names = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    ours = {name[:-3] for folder in (HOOKS, os.path.join(ROOT, 'tools')) for name in os.listdir(folder)
            if name.endswith('.py')}
    assert names - {'__future__'} <= set(sys.stdlib_module_names) and not names & ours


def line(ts, message, request, out, cwd='/home/u/proj-a'):
    return json.dumps({'type': 'assistant', 'timestamp': ts, 'requestId': request, 'cwd': cwd,
                       'message': {'id': message, 'model': 'claude-opus-5-5', 'usage': {'output_tokens': out}}})


def test_reading_from_an_offset_takes_complete_lines_only_and_says_where_they_end(tmp_path):
    first = line('2026-10-13T08:00:00Z', 'm1', 'r1', 10) + '\n'
    second = line('2026-10-13T08:01:00Z', 'm2', 'r2', 20) + '\r\n'
    half = line('2026-10-13T08:02:00Z', 'm3', 'r3', 30)
    path = tmp_path / 's.jsonl'
    path.write_bytes((first + second + half).encode('utf-8'))
    requests, end, unreadable = _transcripts.read_requests_from(str(path), 0, 's', False)
    assert [r.tokens[4] for r in requests.values()] == [10, 20] and end == len(first + second) and unreadable == 0
    requests, end2, _ = _transcripts.read_requests_from(str(path), len(first), 's', False)
    assert list(requests) == [('m2', 'r2')] and end2 == end


def test_reading_from_an_offset_keeps_the_last_line_of_a_request_in_the_place_it_first_appeared(tmp_path):
    lines = [line('2026-10-13T08:00:00Z', 'm1', 'r1', 10), line('2026-10-13T08:00:01Z', 'm2', 'r2', 5),
             line('2026-10-13T08:00:02Z', 'm1', 'r1', 15), line('2026-10-13T08:00:03Z', 'm1', 'r1', 12),
             line('2026-10-13T08:00:04Z', None, 'r3', 1), json.dumps({'type': 'user', 'message': 'no usage'}),
             line('2026-10-13T08:00:05Z', 'm4', 123, 1)]
    path = tmp_path / 's.jsonl'
    path.write_bytes(('\n'.join(lines) + '\n').encode('utf-8'))      # no CRLF on Windows: the offsets count
    requests, _, unreadable = _transcripts.read_requests_from(str(path), 0, 's', True)
    offset = len('\n'.join(lines[:4])) + 1
    assert list(requests) == [('m1', 'r1'), ('m2', 'r2'), (None, offset)]
    assert requests[('m1', 'r1')].tokens[4] == 15 and requests[('m1', 'r1')].agent_type == 'unknown'
    assert unreadable == 1


def test_the_folder_of_a_working_directory_is_its_last_part():
    assert _transcripts.folder_of('/home/u/proj-a/') == 'proj-a' and _transcripts.folder_of('/') == ''
