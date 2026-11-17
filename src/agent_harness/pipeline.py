"""research? → plan → plan review → execute in a worktree → test → review → fix.

The change is left committed on branch ``<prefix><run>`` in
``.agent-harness/state/wt/<run>``; ``ah ship <run>`` pushes it and opens a PR.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import gitops
from .config import PACKAGE_DIR, Settings
from .engines import QuotaExhausted, run_role

PROMPTS = PACKAGE_DIR / "prompts"
VERDICT_SCHEMA = PACKAGE_DIR / "schemas" / "verdict.json"


def prompt(name: str, settings: Settings, **values: Any) -> str:
    base = {"project": settings.project_name, "rules": settings.rules_text()}
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").format(**{**base, **values})


def parse_verdict(text: str) -> dict[str, Any]:
    try:
        verdict = json.loads(text)
        if isinstance(verdict, dict):
            return verdict
    except ValueError:
        pass
    return {"verdict": "revise", "issues": [{"severity": "blocking", "file": None, "line": None,
                                             "problem": "reviewer returned no JSON", "fix": text[:500]}]}


def blocking(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    return [issue for issue in verdict.get("issues") or [] if issue.get("severity") == "blocking"]


def feedback_text(issues: list[dict[str, Any]]) -> str:
    return "Fix these review findings:\n" + "\n".join(
        f"- [{issue.get('file') or '-'}:{issue.get('line') or '-'}] {issue.get('problem')} → {issue.get('fix')}"
        for issue in issues)


def tests_command(settings: Settings, override: str | None = None) -> str:
    template = override or str(settings.section("tests").get("command") or "")
    return template.format(root=shlex.quote(str(settings.root))) if template else ""


def run_tests(settings: Settings, cwd: Path, log: Path, override: str | None = None) -> tuple[bool, str]:
    command = tests_command(settings, override)
    if not command:
        log.write_text("no test command configured\n", encoding="utf-8")
        return True, "no test command configured ([tests] command)"
    timeout = int(settings.section("tests").get("timeout", 1800))
    try:
        completed = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        output, code = (completed.stdout or "") + (completed.stderr or ""), completed.returncode
    except subprocess.TimeoutExpired:
        output, code = f"tests timed out after {timeout}s", -1
    log.write_text(output, encoding="utf-8")
    summary = next((line for line in reversed(output.splitlines())
                    if any(word in line for word in (" passed", " failed", "Tests:", "ok ", "FAIL"))),
                   output[-300:])
    return code == 0, summary.strip() or f"exit code {code}"


@dataclass
class Run:
    settings: Settings
    id: str
    dir: Path
    report: list[str]

    @classmethod
    def new(cls, settings: Settings, run_id: str | None = None) -> Run:
        run = run_id or time.strftime("%Y%m%d-%H%M%S")
        directory = settings.state_dir / "runs" / run
        directory.mkdir(parents=True, exist_ok=True)
        return cls(settings, run, directory, [f"# Harness run {run}", ""])

    def log(self, line: str) -> None:
        print(line, flush=True)
        self.report.append(f"- {line}")

    def save(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def finish(self, extra: list[str] | None = None) -> None:
        self.save("report.md", "\n".join(self.report + (extra or [])) + "\n")

    def meta(self, **values: Any) -> None:
        path = self.dir / "run.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.update(values)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def load_run(settings: Settings, run_id: str) -> dict[str, Any]:
    path = settings.state_dir / "runs" / run_id / "run.json"
    if not path.exists():
        raise FileNotFoundError(f"no run {run_id!r} in {settings.state_dir / 'runs'}")
    return json.loads(path.read_text(encoding="utf-8"))


def research(settings: Settings, question: str, *, run: Run, context: str = "") -> str:
    result = run_role(settings, "researcher", prompt("research", settings, question=question, context=context),
                      log_dir=run.dir, wait_for_quota=False)
    run.save("research.md", result.text)
    run.log(f"research ({result.model} via {result.account}): {'ok' if result.ok else 'FAILED'}")
    return result.text if result.ok else ""


def make_plan(settings: Settings, task: str, *, run: Run, planner: str, max_rounds: int,
              research_brief: str = "") -> str | None:
    feedback = ""
    plan = ""
    research_block = f"Research brief (from the web; verify before relying on it):\n{research_brief}" \
        if research_brief else ""
    plan_rounds = int(settings.section("pipeline").get("plan_rounds", 2))
    for round_number in range(1, plan_rounds + 1):
        try:
            result = run_role(settings, planner, prompt("plan", settings, task=task, feedback=feedback,
                                                        research=research_block),
                              log_dir=run.dir, wait_for_quota=False)
        except QuotaExhausted as exc:
            run.log(f"no planner available: {exc}")
            return None
        if not result.ok:
            run.log(f"planner {result.role} failed; see {result.log_path}")
            return None
        plan = result.text
        run.save(f"plan.r{round_number}.md", plan)
        try:
            review_call = run_role(settings, "plan_review", prompt("plan_review", settings, task=task, plan=plan),
                                   schema=VERDICT_SCHEMA, log_dir=run.dir, wait_for_quota=False)
        except QuotaExhausted as exc:
            # An unreviewed plan is not executed. It is saved so the head agent can
            # review it, then resume with `ah do --plan <run>/plan.md`.
            run.save("plan.md", plan)
            run.log(f"plan review unavailable ({exc}); plan saved unreviewed, stopping")
            return None
        review = parse_verdict(review_call.text)
        run.save(f"plan_review.r{round_number}.json", json.dumps(review, indent=1))
        issues = blocking(review)
        run.log(f"plan round {round_number} ({result.role}): {review.get('verdict')}, {len(issues)} blocking")
        if review.get("verdict") == "approve" or not issues:
            break
        feedback = "Revise your previous plan.\nPrevious plan:\n" + plan + "\n\n" + feedback_text(issues)
    else:
        run.log("plan not approved")
        return None
    run.save("plan.md", plan)
    return plan


def stage_and_commit(settings: Settings, worktree: Path, message: str, run: Run) -> list[str]:
    """Stage what the agent changed (safely) and commit; returns refusal notes."""
    staged = gitops.stage_safely(settings, worktree)
    notes = [f"{path} ({reason})" for path, reason in staged.refused]
    if notes:
        run.log("not staged: " + ", ".join(notes))
    gitops.commit_if_staged(worktree, message)
    return notes


def execute(settings: Settings, *, run: Run, task: str, plan: str, executor: str, max_rounds: int,
            tests_override: str | None = None) -> str:
    worktree, base, branch = gitops.create_worktree(settings, run.id)
    run.meta(worktree=str(worktree), base=base, branch=branch, task=task[:2000])
    protect = ", ".join(settings.protect() + list(settings.section("worktree").get("link") or []))
    tests = tests_command(settings, tests_override) or "no test command configured"
    feedback = ""
    status = "changes"
    open_issues: list[dict[str, Any]] = []
    for round_number in range(1, max_rounds + 2):
        round_start = gitops.git("rev-parse", "HEAD", cwd=worktree)
        result = run_role(settings, executor,
                          prompt("execute", settings, plan=plan, feedback=feedback, tests=tests, protect=protect),
                          cwd=worktree, log_dir=run.dir, add_dirs=[settings.root / ".git"], timeout=5400,
                          wait_for_quota=False)
        run.save(f"execute.r{round_number}.md", result.text)
        stage_and_commit(settings, worktree, f"harness {run.id}: round {round_number}", run)
        passed, summary = run_tests(settings, worktree, run.dir / f"tests.r{round_number}.log", tests_override)
        unsafe = gitops.unsafe_changes(settings, worktree, base)
        run.log(f"execute round {round_number} ({result.model} via {result.account}): "
                f"tests {'pass' if passed else 'FAIL'} — {summary}")
        if round_number == 1 or not open_issues:
            review_prompt = prompt("review", settings, branch=branch, base=base, task=task, plan=plan, tests=summary)
        else:
            # Re-reviewing the whole branch every round costs the most tokens and
            # finds new nits each time; check the fixes and their side effects.
            review_prompt = prompt("review_fix", settings, branch=branch, since=round_start, task=task,
                                   issues=feedback_text(open_issues), tests=summary)
        review = parse_verdict(run_role(
            settings, "reviewer", review_prompt,
            cwd=worktree, schema=VERDICT_SCHEMA, log_dir=run.dir, wait_for_quota=False).text)
        run.save(f"review.r{round_number}.json", json.dumps(review, indent=1))
        issues = blocking(review)
        run.log(f"review round {round_number}: {review.get('verdict')}, {len(issues)} blocking")
        if unsafe:
            run.log("unsafe branch: " + "; ".join(unsafe))
            fix = "remove it from the branch (git rm --cached, then amend the commit)"
            issues += [{"severity": "blocking", "file": None, "line": None, "problem": problem, "fix": fix}
                       for problem in unsafe]
        if not passed:
            issues.append({"severity": "blocking", "file": None, "line": None,
                           "problem": f"tests fail: {summary}", "fix": "make the test suite pass"})
        if not issues:
            status = "ready"
            break
        open_issues = issues
        feedback = feedback_text(issues)
    commits = gitops.git("log", "--oneline", f"{base}..HEAD", cwd=worktree)
    run.meta(status=status)
    run.log(f"status: {status}; branch {branch} in {worktree}")
    run.finish(["", "## Commits", "", commits or "(none)"])
    return status


def pipeline(settings: Settings, task: str, *, plan_text: str | None = None, run_id: str | None = None,
             planner: str = "planner", executor: str = "executor", max_rounds: int | None = None,
             with_research: bool | None = None, tests_override: str | None = None) -> tuple[Run, str]:
    run = Run.new(settings, run_id)
    run.save("task.md", task)
    run.meta(id=run.id, status="planning")
    rounds = max_rounds if max_rounds is not None else int(settings.section("pipeline").get("max_rounds", 2))
    if plan_text:
        plan = plan_text
        run.save("plan.md", plan)
        run.log("plan: provided")
    else:
        brief = ""
        if with_research if with_research is not None else settings.section("pipeline").get("research", False):
            brief = research(settings, f"What does an engineer need to know, from current external sources, "
                                       f"to do this task well?\n\n{task}", run=run)
        plan = make_plan(settings, task, run=run, planner=planner, max_rounds=rounds, research_brief=brief)
        if plan is None:
            run.meta(status="no_plan")
            run.finish()
            return run, "no_plan"
    try:
        status = execute(settings, run=run, task=task, plan=plan, executor=executor, max_rounds=rounds,
                         tests_override=tests_override)
    except QuotaExhausted as exc:
        # Keep the worktree and everything done so far; the head agent resumes later.
        run.meta(status="quota")
        run.log(f"stopped: no model available ({exc}); worktree and logs kept")
        run.finish()
        return run, "quota"
    return run, status
