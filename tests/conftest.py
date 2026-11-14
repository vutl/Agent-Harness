import os
import subprocess
from pathlib import Path

import pytest

from agent_harness import config


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "harness-home"
    monkeypatch.setenv("AGENT_HARNESS_HOME", str(home))
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    (root / "app.py").write_text("print('hi')\n")
    (root / ".gitignore").write_text("/data\n")
    git(root, "add", "app.py", ".gitignore")
    git(root, "commit", "-q", "-m", "init")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def settings(repo):
    return config.load(repo)


__all__ = ["git", "os", "Path"]
