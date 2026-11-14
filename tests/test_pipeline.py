import json
import os

from agent_harness import config, engines, pipeline
from conftest import git


def fake_roles(monkeypatch, *, executor_writes):
    seen = []

    def run_role(settings, role, prompt, *, cwd=None, schema=None, **kwargs):
        seen.append(role)
        text = {"planner": "# Plan\nadd feature.py", "plan_review": json.dumps({"verdict": "approve", "issues": []}),
                "reviewer": json.dumps({"verdict": "approve", "issues": []})}.get(role, "done")
        if role == "executor":
            executor_writes(cwd)
        return engines.CallResult(ok=True, text=text, role=role, model="fake", account="t", seconds=0, log_path=None)

    monkeypatch.setattr(pipeline, "run_role", run_role)
    return seen


def test_pipeline_produces_reviewed_branch_without_unsafe_files(repo, monkeypatch):
    (repo / ".agent-harness").mkdir()
    (repo / ".agent-harness" / "config.toml").write_text('[tests]\ncommand = "test -f feature.py"\n')
    settings = config.load(repo)

    def write(cwd):
        (cwd / "feature.py").write_text("z = 3\n")
        os.symlink("/tmp", cwd / "data")      # an agent creating a symlink must not reach the branch
        (cwd / ".env").write_text("TOKEN=x\n")

    seen = fake_roles(monkeypatch, executor_writes=write)
    run, status = pipeline.pipeline(settings, "Add feature.py", run_id="t1")
    assert status == "ready"
    assert seen == ["planner", "plan_review", "executor", "reviewer"]
    meta = pipeline.load_run(settings, "t1")
    files = git(repo, "diff", "--name-only", f"{meta['base']}..{meta['branch']}").split()
    assert files == ["feature.py"]
    assert (run.dir / "report.md").exists() and (run.dir / "plan.md").exists()


def test_failing_tests_get_another_round(repo, monkeypatch):
    (repo / ".agent-harness").mkdir()
    (repo / ".agent-harness" / "config.toml").write_text('[tests]\ncommand = "test -f second.py"\n'
                                                         '[pipeline]\nmax_rounds = 1\n')
    settings = config.load(repo)
    rounds = []

    def write(cwd):
        rounds.append(1)
        (cwd / ("second.py" if len(rounds) == 2 else "first.py")).write_text("1\n")

    fake_roles(monkeypatch, executor_writes=write)
    _run, status = pipeline.pipeline(settings, "task", plan_text="plan", run_id="t2")
    assert status == "ready" and len(rounds) == 2


def test_research_brief_reaches_the_planner(repo, monkeypatch):
    settings = config.load(repo)
    prompts = {}

    def run_role(settings, role, prompt, **kwargs):
        prompts[role] = prompt
        text = "BRIEF: use API v2" if role == "researcher" else (
            json.dumps({"verdict": "approve", "issues": []}) if role in ("plan_review", "reviewer") else "plan")
        return engines.CallResult(ok=True, text=text, role=role, model="f", account="t", seconds=0, log_path=None)

    monkeypatch.setattr(pipeline, "run_role", run_role)
    pipeline.pipeline(settings, "task", run_id="t3", with_research=True)
    assert "BRIEF: use API v2" in prompts["planner"]


def test_unreviewed_plan_is_not_executed(repo, monkeypatch):
    settings = config.load(repo)
    calls = []

    def run_role(settings, role, prompt, **kwargs):
        calls.append(role)
        if role == "plan_review":
            raise engines.QuotaExhausted("all accounts cooling down")
        return engines.CallResult(ok=True, text="plan", role=role, model="f", account="t", seconds=0, log_path=None)

    monkeypatch.setattr(pipeline, "run_role", run_role)
    run, status = pipeline.pipeline(settings, "task", run_id="t4")
    assert status == "no_plan" and "executor" not in calls
    assert (run.dir / "plan.md").read_text() == "plan"


def test_quota_during_execution_keeps_the_worktree(repo, monkeypatch):
    settings = config.load(repo)

    def run_role(settings, role, prompt, **kwargs):
        raise engines.QuotaExhausted("no model")

    monkeypatch.setattr(pipeline, "run_role", run_role)
    run, status = pipeline.pipeline(settings, "task", plan_text="plan", run_id="t5")
    meta = pipeline.load_run(settings, "t5")
    assert status == "quota" and meta["status"] == "quota"
    from pathlib import Path
    assert Path(meta["worktree"]).exists()
