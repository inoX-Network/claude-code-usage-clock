"""Shared test setup: import path for the hook modules and full isolation from the real home directory."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(ROOT, 'hooks')
sys.path.insert(0, HOOKS)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """No test may read or write the real ~/.claude, the real log or a config.json next to the scripts."""
    for name in list(os.environ):
        if name.startswith('CCUC_') or name == 'CLAUDE_PROJECT_DIR':
            monkeypatch.delenv(name)
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('CCUC_USAGE_FILE', str(tmp_path / 'usage.json'))
    monkeypatch.setenv('CCUC_LOG_DIR', str(tmp_path / 'log'))
    monkeypatch.setenv('CCUC_CONFIG', str(tmp_path / 'no-config.json'))
