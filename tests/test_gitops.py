import os

import pytest

from agent_harness import gitops
from conftest import commit, git


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
    gitops.remove_worktree(settings, worktree, branch, force=True)
    assert (repo / "data").is_dir() and not worktree.exists()


def test_ensure_state_ignored_is_idempotent(repo):
    assert gitops.ensure_state_ignored(repo) is True
    assert gitops.ensure_state_ignored(repo) is False
    assert "/.agent-harness/state/" in (repo / ".gitignore").read_text().splitlines()


@pytest.fixture
def linked_worktree(repo, settings):
    settings.data["worktree"] = {"link": ["data"]}
    (repo / "data").mkdir()
    worktree, _base, branch = gitops.create_worktree(settings, "linked")
    return worktree, branch


def test_unsaved_work_checks_detached_head(settings):
    worktree, _base, branch = gitops.create_worktree(settings, "detached")
    git(worktree, "checkout", "--detach")
    commit(worktree, "new.py")
    assert "not merged or pushed" in "; ".join(gitops.unsaved_work(settings, worktree, branch))
    with pytest.raises(gitops.GitError, match="not merged or pushed"):
        gitops.remove_worktree(settings, worktree, branch)
    assert worktree.exists()


def test_unsaved_work_checks_recorded_branch_when_head_is_elsewhere(settings):
    worktree, _base, branch = gitops.create_worktree(settings, "detached")
    commit(worktree, "new.py")
    git(worktree, "checkout", "--detach", "main")
    assert "not merged or pushed" in "; ".join(gitops.unsaved_work(settings, worktree, branch))
    assert gitops.unsaved_work(settings, worktree) == []


def test_unsaved_work_checks_branch_even_when_kept(repo, settings):
    worktree, _base, branch = gitops.create_worktree(settings, "kept")
    commit(worktree, "new.py")
    with pytest.raises(gitops.GitError, match="not merged or pushed"):
        gitops.remove_worktree(settings, worktree, branch, delete_branch=False)
    assert worktree.exists()
    assert git(repo, "show-ref", "--verify", f"refs/heads/{branch}")


def test_unsaved_work_safe_when_head_and_branch_are_merged(settings):
    worktree, _base, branch = gitops.create_worktree(settings, "merged")
    commit(worktree, "new.py")
    gitops.merge_locally(settings, branch)
    assert gitops.unsaved_work(settings, worktree, branch) == []


def test_unsaved_work_names_missing_base_branch(settings):
    worktree, _base, branch = gitops.create_worktree(settings, "missing-base")
    settings.data["git"]["base"] = "trunk"
    commit(worktree, "new.py")
    assert "base branch 'trunk' not found" in "; ".join(gitops.unsaved_work(settings, worktree, branch))


def test_unsaved_work_requires_exact_ref_names(settings):
    worktree, _base, branch = gitops.create_worktree(settings, "nested-ref")
    commit(worktree, "new.py")
    git(worktree, "branch", "trunk/nested")
    settings.data["git"]["base"] = "trunk"
    reasons = gitops.unsaved_work(settings, worktree, branch)
    assert "base branch 'trunk' not found" in "; ".join(reasons)


def test_unsaved_work_handles_missing_recorded_branch(settings):
    worktree, _base, _branch = gitops.create_worktree(settings, "missing-branch")
    assert gitops.unsaved_work(settings, worktree, "nonexistent") == []


def test_unsaved_work_counts_regular_file_in_place_of_link(repo, settings):
    settings.data["worktree"] = {"link": ["shared"]}
    (repo / "shared").mkdir()
    worktree, _base, branch = gitops.create_worktree(settings, "replaced")
    (worktree / "shared").unlink()
    (worktree / "shared").write_text("regular file\n")
    assert gitops.unsaved_work(settings, worktree, branch) == ["1 uncommitted change (shared)"]


@pytest.mark.parametrize("query", [("status",), ("for-each-ref",), ("log",)])
def test_remove_worktree_refuses_when_safety_query_fails(repo, settings, linked_worktree, git_spy, query):
    worktree, branch = linked_worktree
    git_spy.fail(*query)
    with pytest.raises(gitops.GitError, match="injected"):
        gitops.remove_worktree(settings, worktree, branch)
    with pytest.raises(gitops.GitError, match="injected"):
        gitops.unsaved_work(settings, worktree, branch)
    assert worktree.exists() and (worktree / "data").is_symlink()
    assert git(repo, "show-ref", "--verify", f"refs/heads/{branch}")
    assert not git_spy.ran("worktree", "remove")
    assert not git_spy.ran("branch", "-D")


def test_failed_worktree_removal_restores_links_and_keeps_branch(repo, settings, linked_worktree, git_spy):
    worktree, branch = linked_worktree
    saved = os.readlink(worktree / "data")
    git_spy.fail("worktree", "remove")
    with pytest.raises(gitops.GitError, match="injected"):
        gitops.remove_worktree(settings, worktree, branch)
    assert worktree.exists() and (worktree / "data").is_symlink()
    assert os.readlink(worktree / "data") == saved
    assert (worktree / "data").resolve() == repo / "data"
    assert git(repo, "show-ref", "--verify", f"refs/heads/{branch}")
    assert not git_spy.ran("branch", "-D")
    removal = next(argv for argv in git_spy.calls if argv[1:3] == ["worktree", "remove"])
    assert "--force" not in removal


def test_remove_worktree_refuses_unsaved_work(settings, linked_worktree, git_spy):
    worktree, branch = linked_worktree
    (worktree / "new.py").write_text("unfinished\n")
    with pytest.raises(gitops.GitError, match="uncommitted change"):
        gitops.remove_worktree(settings, worktree, branch)
    assert worktree.exists() and (worktree / "data").is_symlink()
    assert not git_spy.ran("worktree", "remove")


def test_remove_worktree_force_ignores_check(repo, settings, git_spy):
    worktree, _base, branch = gitops.create_worktree(settings, "forced")
    commit(worktree, "committed.py")
    (worktree / "new.py").write_text("unfinished\n")
    gitops.remove_worktree(settings, worktree, branch, force=True)
    assert not worktree.exists()
    assert f"refs/heads/{branch}" not in git(repo, "for-each-ref", "--format=%(refname)").splitlines()
    assert not git_spy.ran("status")
    assert not git_spy.ran("for-each-ref")
    assert not git_spy.ran("log")
    assert git_spy.ran("worktree", "remove", "--force")


def test_remove_worktree_reports_branch_deletion_failure(repo, settings, git_spy):
    worktree, _base, branch = gitops.create_worktree(settings, "branch-error")
    git_spy.fail("branch", "-D")
    with pytest.raises(gitops.GitError, match="injected"):
        gitops.remove_worktree(settings, worktree, branch)
    assert not worktree.exists()
    assert git(repo, "show-ref", "--verify", f"refs/heads/{branch}")


def test_unsaved_work_summarizes_changes_and_commits(settings):
    worktree, _base, branch = gitops.create_worktree(settings, "summary")
    commit(worktree, "first.py")
    commit(worktree, "second.py")
    for filename in ("a.py", "b.py", "c.py", "d.py"):
        (worktree / filename).write_text("unfinished\n")
    reasons = gitops.unsaved_work(settings, worktree, branch)
    assert reasons[0] == "4 uncommitted changes (a.py, b.py, c.py, …)"
    assert reasons[1].startswith("2 commits not merged or pushed (")
    assert "add second.py" in reasons[1]
