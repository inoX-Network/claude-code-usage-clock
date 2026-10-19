"""Tests for hooks/_transcripts.py beyond what the usage report tests read through it (tests/test_usage_report*.py)."""
import ast
import os
import sys

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
