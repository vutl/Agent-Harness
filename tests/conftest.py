import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_harness import config, gitops


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


def make_run(settings, name, **extra):
    worktree, base, branch = gitops.create_worktree(settings, name)
    run_dir = settings.state_dir / "runs" / name
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "id": name, "worktree": str(worktree), "base": base, "branch": branch, **extra,
    }))
    return worktree, branch


def commit(worktree, filename):
    (worktree / filename).write_text(f"content of {filename}\n")
    git(worktree, "add", "--", filename)
    git(worktree, "commit", "-q", "-m", f"add {filename}")


@pytest.fixture
def git_spy(monkeypatch):
    calls = []
    failures = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "git" and any(tuple(argv[1:1 + len(prefix)]) == prefix for prefix in failures):
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(128, argv, output="", stderr="fatal: injected")
            return subprocess.CompletedProcess(argv, 128, "", "fatal: injected")
        return subprocess.run(argv, **kwargs)

    def ran(*prefix):
        return any(argv[0] == "git" and tuple(argv[1:1 + len(prefix)]) == prefix for argv in calls)

    # Isolate gitops from subprocess users in test setup and other harness modules.
    monkeypatch.setattr(gitops, "subprocess", SimpleNamespace(
        run=run, CalledProcessError=subprocess.CalledProcessError,
    ))
    return SimpleNamespace(calls=calls, fail=lambda *prefix: failures.append(prefix), ran=ran)


__all__ = ["git", "os", "Path"]
