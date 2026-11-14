from agent_harness import config
from conftest import git


def test_layers_merge_tables_and_replace_lists(repo, isolated_home):
    isolated_home.mkdir(parents=True)
    (isolated_home / "config.toml").write_text(
        '[[accounts]]\nname = "work"\ncodex_home = "~/.codex-work"\n[roles.executor]\nmodel = "user-model"\n')
    (repo / ".agent-harness").mkdir()
    (repo / ".agent-harness" / "config.toml").write_text(
        '[roles.executor]\neffort = "low"\n[tests]\ncommand = "pytest"\n[safety]\nprotect_extra = ["data"]\n')
    settings = config.load(repo)
    executor = settings.role("executor")
    assert executor["model"] == "user-model"          # user layer
    assert executor["effort"] == "low"                # project layer
    assert executor["engine"] == "codex"              # package default kept
    assert settings.data["accounts"][0]["name"] == "work"
    assert "data" in settings.protect() and ".env" in settings.protect()
    assert len(settings.sources) == 3


def test_project_root_from_inside_a_worktree(repo, tmp_path):
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "x", str(worktree))
    assert config.project_root(worktree) == repo
    assert config.project_root(tmp_path) is None


def test_rules_text_lists_existing_rule_files(repo):
    (repo / "AGENTS.md").write_text("rules")
    text = config.load(repo).rules_text()
    assert "AGENTS.md" in text and "CLAUDE.md" not in text
