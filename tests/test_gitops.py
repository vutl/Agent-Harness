import os

import pytest

from agent_harness import gitops
from conftest import git


@pytest.mark.parametrize("path,expected", [
    (".env", True), ("backend/.env", True), (".env.example", False), ("data", False),
    ("node_modules/x/index.js", True), ("apps/web/node_modules", True), ("src/key.py", False),
    ("certs/server.pem", True), (".agent-harness/state/runs/x", True),
])
def test_protected_paths(settings, path, expected):
    assert gitops.is_protected(path, settings.protect()) is expected


def test_stage_safely_refuses_symlinks_protected_and_large_files(repo, settings):
    (repo / "app.py").write_text("print('changed')\n")
    (repo / "new.py").write_text("x = 1\n")
    (repo / "backend").mkdir()
    (repo / "backend" / ".env").write_text("SECRET=1\n")
    os.symlink(repo / "app.py", repo / "link")
    (repo / "big.bin").write_bytes(b"0" * (6 * 1024 * 1024))
    result = gitops.stage_safely(settings, repo)
    assert sorted(result.staged) == ["app.py", "new.py"]
    assert dict(result.refused) == {"backend/.env": "protected path", "link": "symlink", "big.bin": "larger than 5 MB"}
    assert set(git(repo, "diff", "--cached", "--name-only").split()) == {"app.py", "new.py"}


def test_stage_safely_stages_deletions(repo, settings):
    (repo / "app.py").unlink()
    result = gitops.stage_safely(settings, repo)
    assert result.staged == ["app.py"]
    assert git(repo, "diff", "--cached", "--name-status") == "D\tapp.py"


def test_unsafe_changes_flags_committed_symlink_and_protected_path(repo, settings):
    base = git(repo, "rev-parse", "HEAD")
    os.symlink("/tmp", repo / "data")
    (repo / ".env").write_text("A=1\n")
    git(repo, "add", "-f", "data", ".env")
    git(repo, "commit", "-q", "-m", "oops")
    problems = gitops.unsafe_changes(settings, repo, base)
    assert any("data: commits a symlink" in p for p in problems)
    assert any(".env: protected path" in p for p in problems)


def test_merge_refuses_branch_that_would_replace_ignored_local_directory(repo, settings):
    """The 2026-11-14 incident: a branch committed the worktree's `data` symlink;
    merging it made git delete the ignored local data/ directory."""
    (repo / "data").mkdir()
    (repo / "data" / "app.db").write_text("precious")
    worktree, base, branch = gitops.create_worktree(settings, "incident")
    os.symlink(repo / "data", worktree / "data")
    git(worktree, "add", "-f", "data")
    git(worktree, "commit", "-q", "-m", "commit the symlink")
    with pytest.raises(gitops.GitError) as error:
        gitops.merge_locally(settings, branch)
    assert "data: commits a symlink" in str(error.value)
    assert "data: exists locally" in str(error.value)
    assert (repo / "data" / "app.db").read_text() == "precious"


def test_merge_refuses_regular_file_over_ignored_directory(repo, settings):
    (repo / "data").mkdir()
    (repo / "data" / "keep.txt").write_text("keep")
    worktree, _base, branch = gitops.create_worktree(settings, "file-over-dir")
    (worktree / "data").write_text("not a dir")
    git(worktree, "add", "-f", "data")
    git(worktree, "commit", "-q", "-m", "file named data")
    with pytest.raises(gitops.GitError, match="exists locally"):
        gitops.merge_locally(settings, branch)
    assert (repo / "data" / "keep.txt").exists()


def test_clean_branch_merges(repo, settings):
    worktree, _base, branch = gitops.create_worktree(settings, "clean")
    (worktree / "feature.py").write_text("y = 2\n")
    gitops.stage_safely(settings, worktree)
    assert gitops.commit_if_staged(worktree, "feature")
    gitops.merge_locally(settings, branch)
    assert (repo / "feature.py").read_text() == "y = 2\n"


def test_worktree_links_runtime_inputs_and_never_stages_them(repo, tmp_path):
    (repo / ".agent-harness").mkdir()
    (repo / ".agent-harness" / "config.toml").write_text('[worktree]\nlink = ["data"]\n')
    from agent_harness import config

    settings = config.load(repo)
    (repo / "data").mkdir()
    worktree, _base, branch = gitops.create_worktree(settings, "linked")
    assert (worktree / "data").is_symlink()
    (worktree / "x.py").write_text("1\n")
    result = gitops.stage_safely(settings, worktree)
    assert result.staged == ["x.py"]
    gitops.remove_worktree(settings, worktree, branch)
    assert (repo / "data").is_dir() and not worktree.exists()


def test_ensure_state_ignored_is_idempotent(repo):
    assert gitops.ensure_state_ignored(repo) is True
    assert gitops.ensure_state_ignored(repo) is False
    assert "/.agent-harness/state/" in (repo / ".gitignore").read_text().splitlines()
