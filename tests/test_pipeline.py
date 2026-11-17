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


def test_fix_rounds_review_only_the_new_diff(repo, monkeypatch):
    (repo / ".agent-harness").mkdir(exist_ok=True)
    (repo / ".agent-harness" / "config.toml").write_text('[tests]\ncommand = "true"\n')
    settings = config.load(repo)
    prompts = []
    rounds = []

    def run_role(settings, role, prompt, *, cwd=None, **kwargs):
        if role == "executor":
            rounds.append(1)
            (cwd / f"f{len(rounds)}.py").write_text("1\n")
            text = "done"
        else:
            prompts.append(prompt)
            first = len(prompts) == 1
            issue = {"severity": "blocking", "file": "f1.py", "line": 1, "problem": "bug", "fix": "fix it"}
            text = json.dumps({"verdict": "changes" if first else "approve", "issues": [issue] if first else []})
        return engines.CallResult(ok=True, text=text, role=role, model="f", account="t", seconds=0, log_path=None)

    monkeypatch.setattr(pipeline, "run_role", run_role)
    _run, status = pipeline.pipeline(settings, "task", plan_text="plan", run_id="t6")
    assert status == "ready" and len(rounds) == 2
    assert "git diff" in prompts[1] and "..HEAD" in prompts[1] and "bug" in prompts[1]
    assert "Plan it implements" not in prompts[1]


def test_failing_tests_parses_common_runners():
    output = ("FAILED tests/a.py::test_x - AssertionError\nERROR tests/b.py::test_y\n"
              "--- FAIL: TestZ (0.00s)\n FAIL src/c.test.ts\nall FAILED here is prose\n")
    assert pipeline.failing_tests(output) == {"tests/a.py::test_x", "tests/b.py::test_y", "TestZ",
                                              "src/c.test.ts"}


def test_judge_tests_blocks_only_new_failures():
    old = "FAILED t.py::old - x\n"
    assert pipeline.judge_tests(False, "1 failed", old, {"t.py::old"})[0] is True
    passed, _summary, new = pipeline.judge_tests(False, "2 failed", old + "FAILED t.py::new - y\n", {"t.py::old"})
    assert not passed and new == {"t.py::new"}
    # Unparseable output or no baseline keeps the old rule: a failing suite blocks.
    assert pipeline.judge_tests(False, "crash", "Traceback", {"t.py::old"})[0] is False
    assert pipeline.judge_tests(False, "1 failed", old, set())[0] is False


def test_failures_already_on_base_do_not_block(repo, monkeypatch):
    (repo / ".agent-harness").mkdir()
    command = ("""sh -c 'echo "FAILED t.py::old - boom"; """
               """test -f new_fail.py && echo "FAILED t.py::new - boom"; exit 1'""")
    (repo / ".agent-harness" / "config.toml").write_text(f"[tests]\ncommand = {json.dumps(command)}\n"
                                                         "[pipeline]\nmax_rounds = 0\n")
    settings = config.load(repo)
    fake_roles(monkeypatch, executor_writes=lambda cwd: (cwd / "feature.py").write_text("1\n"))
    run, status = pipeline.pipeline(settings, "task", plan_text="plan", run_id="t-base")
    assert status == "ready" and (run.dir / "tests.base.log").exists()

    fake_roles(monkeypatch, executor_writes=lambda cwd: (cwd / "new_fail.py").write_text("1\n"))
    _run, status = pipeline.pipeline(settings, "task", plan_text="plan", run_id="t-new")
    assert status == "changes"
