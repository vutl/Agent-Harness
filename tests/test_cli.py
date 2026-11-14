import json

from agent_harness import cli, config


def test_init_writes_config_gitignore_agents_and_ci(repo, capsys):
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert cli.main(["init", "--ci"]) == 0
    text = (repo / ".agent-harness" / "config.toml").read_text()
    assert 'command = "python -m pytest -q"' in text and f'name = "{repo.name}"' in text
    assert "/.agent-harness/state/" in (repo / ".gitignore").read_text()
    assert (repo / "AGENTS.md").exists()
    assert "pytest" in (repo / ".github" / "workflows" / "ci.yml").read_text()
    assert config.load(repo).section("tests")["command"] == "python -m pytest -q"


def test_detect_tests(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    assert cli.detect_tests(tmp_path) == ("npm test --silent", "node")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.detect_tests(empty) == ("", "generic")


def test_skills_install_project(repo):
    assert cli.main(["skills", "install", "--project"]) == 0
    skill = repo / ".claude" / "skills" / "agent-harness" / "SKILL.md"
    assert skill.read_text().startswith("---\nname: agent-harness")


def test_help_lists_commands(capsys):
    try:
        cli.main(["--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    for command in ("init", "doctor", "research", "do", "ship", "review", "merge", "clean"):
        assert command in out
