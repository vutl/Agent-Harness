"""Ship a finished run: push the branch, open a PR, wait for CI (fixing failures),
review the PR, and merge only when the project's policy allows it."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import gitops
from .config import Settings
from .engines import run_role
from .pipeline import VERDICT_SCHEMA, Run, blocking, parse_verdict, prompt, stage_and_commit, tests_command


class ShipError(RuntimeError):
    pass


def gh(*args: str, cwd: Path, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    if not shutil.which("gh"):
        raise ShipError("GitHub CLI `gh` is not installed (brew install gh; gh auth login)")
    completed = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and completed.returncode != 0:
        raise ShipError(f"gh {' '.join(args)}: {completed.stderr.strip() or completed.stdout.strip()}")
    return completed


def gh_ready(cwd: Path) -> tuple[bool, str]:
    if not shutil.which("gh"):
        return False, "gh not installed (brew install gh; gh auth login)"
    completed = subprocess.run(["gh", "auth", "status"], cwd=cwd, capture_output=True, text=True)
    lines = [line.strip() for line in (completed.stdout + completed.stderr).splitlines() if line.strip()]
    return completed.returncode == 0, " ".join(lines[1:3]) if len(lines) > 1 else "".join(lines)


def guard(settings: Settings, worktree: Path, base: str) -> None:
    problems = gitops.unsafe_changes(settings, worktree, base)
    if problems:
        raise ShipError("refusing to push:\n  " + "\n  ".join(problems))


def push(settings: Settings, worktree: Path, branch: str) -> None:
    remote = settings.section("git").get("remote", "origin")
    gitops.git("push", "-q", "-u", remote, branch, cwd=worktree)


def open_pr(settings: Settings, worktree: Path, branch: str, title: str, body_file: Path) -> str:
    base_branch = settings.section("git").get("base", "main")
    existing = gh("pr", "list", "--head", branch, "--json", "url", "--jq", ".[0].url", cwd=worktree, check=False)
    if existing.returncode == 0 and existing.stdout.strip():
        return existing.stdout.strip()
    created = gh("pr", "create", "--base", base_branch, "--head", branch, "--title", title,
                 "--body-file", str(body_file), cwd=worktree)
    return created.stdout.strip().splitlines()[-1]


def wait_for_checks(worktree: Path, pr: str, timeout: int = 3600) -> tuple[str, str]:
    """('pass' | 'fail' | 'none', output) once the PR's checks finish."""
    has_workflows = any((worktree / ".github" / "workflows").glob("*.y*ml"))
    output = ""
    # Right after a push the checks may not be registered yet.
    for _attempt in range(8 if has_workflows else 1):
        completed = gh("pr", "checks", pr, "--watch", "--interval", "20", cwd=worktree, check=False,
                       timeout=timeout)
        output = (completed.stdout + completed.stderr).strip()
        if "no checks reported" not in output.lower():
            return ("pass" if completed.returncode == 0 else "fail"), output
        time.sleep(15)
    return "none", output


def failed_log(worktree: Path, branch: str, limit_chars: int = 12000) -> str:
    runs = gh("run", "list", "--branch", branch, "--limit", "5", "--json", "databaseId,conclusion",
              cwd=worktree, check=False)
    try:
        failed = [r["databaseId"] for r in json.loads(runs.stdout or "[]") if r.get("conclusion") == "failure"]
    except ValueError:
        failed = []
    if not failed:
        return "(no failed workflow log found)"
    log = gh("run", "view", str(failed[0]), "--log-failed", cwd=worktree, check=False, timeout=300).stdout
    return log[-limit_chars:]


def review_pr(settings: Settings, pr: str, *, cwd: Path, log_dir: Path, post: bool = False) -> dict[str, Any]:
    info = json.loads(gh("pr", "view", pr, "--json", "title,body", cwd=cwd).stdout)
    diff = gh("pr", "diff", pr, cwd=cwd).stdout
    if len(diff) > 150_000:
        diff = diff[:150_000] + "\n… (diff truncated; inspect the rest with git)"
    verdict = parse_verdict(run_role(
        settings, "reviewer", prompt("pr_review", settings, title=info.get("title", ""), body=info.get("body") or "",
                                     diff=diff),
        cwd=cwd, schema=VERDICT_SCHEMA, log_dir=log_dir, wait_for_quota=False).text)
    if post:
        body = format_review(verdict)
        body_file = log_dir / "pr_review.md"
        body_file.write_text(body, encoding="utf-8")
        gh("pr", "review", pr, "--comment", "--body-file", str(body_file), cwd=cwd)
    return verdict


def format_review(verdict: dict[str, Any]) -> str:
    lines = [f"**Agent review: {verdict.get('verdict')}**", ""]
    for issue in verdict.get("issues") or []:
        where = f"`{issue.get('file')}:{issue.get('line')}`" if issue.get("file") else "general"
        lines.append(f"- **{issue.get('severity')}** {where}: {issue.get('problem')}  \n  Fix: {issue.get('fix')}")
    if not verdict.get("issues"):
        lines.append("No issues found.")
    return "\n".join(lines) + "\n"


def ship(settings: Settings, run_meta: dict[str, Any], *, executor: str = "executor",
         merge: str | None = None) -> str:
    """Push, PR, CI (with fix rounds), review, and merge per policy. Returns the PR URL."""
    worktree, base, branch = Path(run_meta["worktree"]), run_meta["base"], run_meta["branch"]
    run = Run(settings, run_meta["id"], settings.state_dir / "runs" / run_meta["id"], [])
    policy = merge or settings.section("git").get("merge", "manual")
    if not gitops.has_remote(settings):
        raise ShipError("no git remote configured; use `ah merge <run>` for a guarded local merge")
    if not gitops.git("log", "--oneline", f"{base}..HEAD", cwd=worktree):
        raise ShipError("the run's branch has no commits")
    guard(settings, worktree, base)
    push(settings, worktree, branch)
    report = run.dir / "report.md"
    title = (run_meta.get("task") or branch).strip().splitlines()[0][:72]
    pr = open_pr(settings, worktree, branch, title, report if report.exists() else run.dir / "task.md")
    run.log(f"PR: {pr}")
    rounds = int(settings.section("git").get("fix_ci_rounds", 2))
    for round_number in range(rounds + 1):
        state, output = wait_for_checks(worktree, pr)
        run.log(f"CI: {state}")
        if state != "fail" or round_number == rounds:
            break
        result = run_role(settings, executor, prompt(
            "fix_ci", settings, branch=branch, checks=output[-3000:], log=failed_log(worktree, branch),
            tests=tests_command(settings) or "no test command configured"),
            cwd=worktree, log_dir=run.dir, add_dirs=[settings.root / ".git"], timeout=5400, wait_for_quota=False)
        run.save(f"fix_ci.r{round_number + 1}.md", result.text)
        stage_and_commit(settings, worktree, f"harness {run.id}: fix CI round {round_number + 1}", run)
        guard(settings, worktree, base)
        push(settings, worktree, branch)
    verdict = review_pr(settings, pr, cwd=worktree, log_dir=run.dir, post=True)
    issues = blocking(verdict)
    run.log(f"PR review: {verdict.get('verdict')}, {len(issues)} blocking")
    if policy == "auto" and state in ("pass", "none") and not issues:
        gh("pr", "merge", pr, "--squash", "--delete-branch", cwd=worktree)
        run.log("merged (policy: auto)")
    else:
        run.log(f"waiting for a human to merge (policy: {policy}, CI: {state}, blocking: {len(issues)})")
    run.meta(pr=pr, ci=state, review=verdict.get("verdict"))
    with open(run.dir / "report.md", "a", encoding="utf-8") as handle:
        handle.write("\n## Ship\n\n" + "\n".join(run.report) + "\n")
    return pr
