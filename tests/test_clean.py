import pytest

from agent_harness import cli, config, gitops
from conftest import commit, git, make_run


def branch_exists(repo, branch):
    return f"refs/heads/{branch}" in git(repo, "for-each-ref", "--format=%(refname)").splitlines()


def add_remote(repo, tmp_path):
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    git(repo, "remote", "add", "origin", str(remote))


@pytest.mark.parametrize("filename", ["app.py", "new.py"])
def test_clean_refuses_dirty_worktree(repo, settings, capsys, filename):
    worktree, branch = make_run(settings, "dirty")
    (worktree / filename).write_text("unfinished\n")
    assert cli.main(["clean", "dirty"]) == 1
    assert worktree.exists() and branch_exists(repo, branch)
    assert (worktree / filename).read_text() == "unfinished\n"
    out = capsys.readouterr().out
    assert "skipped dirty: 1 uncommitted change" in out
    assert filename in out and "--force" in out
    assert "removed worktree" not in out


def test_clean_refuses_unpushed_unmerged_commits(repo, settings, capsys):
    worktree, branch = make_run(settings, "unpushed")
    commit(worktree, "new.py")
    assert cli.main(["clean", "unpushed"]) == 1
    assert worktree.exists() and branch_exists(repo, branch)
    assert "1 commit not merged or pushed" in capsys.readouterr().out


def test_clean_refuses_commits_only_on_other_local_branch(repo, settings):
    worktree, branch = make_run(settings, "local")
    commit(worktree, "new.py")
    git(repo, "branch", "feature", branch)
    assert cli.main(["clean", "local"]) == 1
    assert worktree.exists() and branch_exists(repo, branch)


def test_clean_refuses_pr_run_with_unpushed_commits(repo, settings, tmp_path):
    add_remote(repo, tmp_path)
    worktree, branch = make_run(settings, "pr", pr=1)
    commit(worktree, "first.py")
    git(worktree, "push", "-q", "origin", branch)
    commit(worktree, "second.py")
    assert cli.main(["clean", "pr"]) == 1
    assert worktree.exists() and branch_exists(repo, branch)


def test_clean_removes_merged_clean_run(repo, settings, capsys):
    worktree, branch = make_run(settings, "merged")
    commit(worktree, "new.py")
    head = git(worktree, "rev-parse", "HEAD")
    gitops.merge_locally(settings, branch)
    assert cli.main(["clean", "merged"]) == 0
    assert not worktree.exists() and not branch_exists(repo, branch)
    git(repo, "merge-base", "--is-ancestor", head, "main")
    assert f"removed worktree {worktree}" in capsys.readouterr().out


def test_clean_removes_run_without_commits(repo, settings):
    worktree, branch = make_run(settings, "empty")
    assert cli.main(["clean", "empty"]) == 0
    assert not worktree.exists() and not branch_exists(repo, branch)


def test_clean_removes_pushed_run(repo, settings, tmp_path):
    add_remote(repo, tmp_path)
    worktree, branch = make_run(settings, "pushed")
    commit(worktree, "new.py")
    git(worktree, "push", "-q", "origin", branch)
    assert cli.main(["clean", "pushed"]) == 0
    assert not worktree.exists() and not branch_exists(repo, branch)


@pytest.mark.parametrize("delete_branches", [False, True])
def test_clean_keeps_branch_of_pushed_pr_run(repo, settings, tmp_path, delete_branches):
    add_remote(repo, tmp_path)
    worktree, branch = make_run(settings, "pr", pr=1)
    commit(worktree, "new.py")
    git(worktree, "push", "-q", "origin", branch)
    args = ["clean", "pr", *(["--delete-branches"] if delete_branches else [])]
    assert cli.main(args) == 0
    assert not worktree.exists()
    assert branch_exists(repo, branch) is not delete_branches


@pytest.mark.parametrize("dirty", [False, True])
def test_clean_force_removes_anyway(repo, settings, dirty):
    worktree, branch = make_run(settings, "forced")
    if dirty:
        (worktree / "new.py").write_text("unfinished\n")
    else:
        commit(worktree, "new.py")
    assert cli.main(["clean", "--force", "forced"]) == 0
    assert not worktree.exists() and not branch_exists(repo, branch)


def test_clean_ignores_link_symlinks(repo):
    (repo / ".agent-harness").mkdir()
    (repo / ".agent-harness" / "config.toml").write_text('[worktree]\nlink = ["shared"]\n')
    (repo / "shared").mkdir()
    (repo / "shared" / "keep.txt").write_text("keep\n")
    settings = config.load(repo)
    worktree, branch = make_run(settings, "linked")
    assert (worktree / "shared").is_symlink()
    assert cli.main(["clean", "linked"]) == 0
    assert not worktree.exists() and not branch_exists(repo, branch)
    assert (repo / "shared" / "keep.txt").read_text() == "keep\n"


def test_clean_all_skips_unsafe_and_removes_safe(repo, settings, capsys):
    unsafe, unsafe_branch = make_run(settings, "a-unsafe")
    safe, safe_branch = make_run(settings, "b-safe")
    (unsafe / "new.py").write_text("unfinished\n")
    assert cli.main(["clean", "--all"]) == 1
    assert unsafe.exists() and branch_exists(repo, unsafe_branch)
    assert not safe.exists() and not branch_exists(repo, safe_branch)
    out = capsys.readouterr().out
    assert "skipped a-unsafe" in out and f"removed worktree {safe}" in out


def test_clean_reports_failed_safety_query(repo, settings, git_spy, capsys):
    worktree, branch = make_run(settings, "error")
    git_spy.fail("status")
    assert cli.main(["clean", "error"]) == 1
    out = capsys.readouterr().out
    assert "skipped error" in out and "injected" in out
    assert worktree.exists() and branch_exists(repo, branch)
