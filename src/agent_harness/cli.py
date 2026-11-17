"""ah — agent harness command line.

  ah init [--ci]                 set up this repository (.agent-harness/config.toml)
  ah doctor                      check CLIs, logins, gh, project config
  ah status                      accounts, cooldowns, roles, recent calls
  ah run <role> [prompt]         one role call (prompt from argument, --prompt-file or stdin)
  ah research "<question>"       web research brief with sources
  ah do --task task.md           plan → review → execute → test → review (worktree branch)
  ah ship <run>                  push, open PR, wait for CI (fix failures), review, merge per policy
  ah review [--pr N] [--post]    review a PR (or the current branch against base)
  ah merge <run>                 guarded local merge of a run's branch
  ah runs | ah clean [<run>|--all] [--force]
  ah skills install [--project]  install the head-agent skill for Claude Code and Codex
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, gitops
from .config import PACKAGE_DIR, PROJECT_DIR, Settings, load, user_dir
from .engines import accounts, binary, call_log_path, read_state, run_role
from .pipeline import Run, load_run, pipeline, research
from .ship import ShipError, gh_ready, review_pr, ship

TEMPLATES = PACKAGE_DIR / "templates"


def _need_root(settings: Settings) -> Path:
    if settings.root is None:
        sys.exit("ah: not inside a git repository")
    return settings.root


# --- init --------------------------------------------------------------------

def detect_tests(root: Path) -> tuple[str, str]:
    """(test command, CI template name) for common project layouts."""
    if (root / "package.json").is_file():
        try:
            scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
        except ValueError:
            scripts = {}
        if "test" in scripts:
            return "npm test --silent", "node"
    if any((root / name).is_file() for name in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini")):
        return ("uv run pytest -q" if (root / "uv.lock").is_file() else "python -m pytest -q"), "python"
    if (root / "go.mod").is_file():
        return "go test ./...", "go"
    if (root / "Cargo.toml").is_file():
        return "cargo test", "rust"
    return "", "generic"


def cmd_init(args: argparse.Namespace, settings: Settings) -> int:
    root = _need_root(settings)
    config = root / PROJECT_DIR / "config.toml"
    command, flavour = detect_tests(root)
    if config.exists() and not args.force:
        print(f"exists: {config.relative_to(root)} (use --force to rewrite)")
    else:
        config.parent.mkdir(exist_ok=True)
        text = (TEMPLATES / "project_config.toml").read_text(encoding="utf-8")
        config.write_text(text.replace("{name}", root.name).replace("{tests}", command.replace('"', '\\"')),
                          encoding="utf-8")
        print(f"wrote {config.relative_to(root)} (tests: {command or 'not detected; set [tests] command'})")
    if gitops.ensure_state_ignored(root):
        print("added /.agent-harness/state/ to .gitignore")
    if args.ci:
        workflows = root / ".github" / "workflows"
        target = workflows / "ci.yml"
        if target.exists():
            print(f"exists: {target.relative_to(root)}")
        else:
            workflows.mkdir(parents=True, exist_ok=True)
            shutil.copy(TEMPLATES / f"ci-{flavour}.yml", target)
            print(f"wrote {target.relative_to(root)} ({flavour}); review it before pushing")
    if not any((root / name).is_file() for name in ("AGENTS.md", "CLAUDE.md")):
        (root / "AGENTS.md").write_text((TEMPLATES / "AGENTS.md").read_text(encoding="utf-8")
                                        .replace("{name}", root.name), encoding="utf-8")
        print("wrote AGENTS.md (project rules every agent reads; fill it in)")
    print("next: `ah doctor`, then `ah skills install` once per machine")
    return 0


# --- doctor / status -----------------------------------------------------------

def _version(path: str) -> str:
    try:
        completed = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        return (completed.stdout or completed.stderr).strip().splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "?"


def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    problems = 0

    def line(ok: bool | None, text: str) -> None:
        nonlocal problems
        mark = {True: "✓", False: "✗", None: "–"}[ok]
        problems += ok is False
        print(f"  {mark} {text}")

    print(f"agent-harness {__version__}; config layers: " + ", ".join(str(p) for p in settings.sources))
    line(sys.version_info >= (3, 11), f"python {sys.version.split()[0]}")
    line(bool(shutil.which("git")), "git")
    for name in ("codex", "claude"):
        try:
            path = binary(settings, name)
            line(True, f"{name}: {_version(path)} ({path})")
        except FileNotFoundError as exc:
            line(False, str(exc))
    logged = accounts(settings)
    line(bool(logged), f"codex accounts logged in: {', '.join(a['name'] for a in logged) or 'none (codex login)'}")
    ok, detail = gh_ready(settings.root or Path.cwd())
    line(ok, f"gh: {detail}")
    if settings.root is None:
        line(None, "not inside a git repository")
    else:
        config = settings.root / PROJECT_DIR / "config.toml"
        line(config.exists(), f"project config: {config if config.exists() else 'missing (ah init)'}")
        tests = settings.section("tests").get("command")
        line(bool(tests), f"tests: {tests or 'not set'}")
        rules = [n for n in settings.section("project").get("rules") or [] if (settings.root / n).is_file()]
        line(bool(rules), f"rule files: {', '.join(rules) or 'none (write AGENTS.md)'}")
        line(None, f"remote: {'yes' if gitops.has_remote(settings) else 'none (local merges only)'}; "
                   f"merge policy: {settings.section('git').get('merge')}")
    for name, gateway in (settings.data.get("gateways") or {}).items():
        line(None, f"gateway {name}: {gateway.get('base_url')}")
    skill = Path.home() / ".claude" / "skills" / "agent-harness" / "SKILL.md"
    line(skill.exists(), "head-agent skill installed" if skill.exists() else "skill not installed (ah skills install)")
    return 1 if problems else 0


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    state = read_state(settings)
    now = time.time()
    logged = {a["name"] for a in accounts(settings)}
    print("codex accounts:")
    configured = settings.data.get("accounts") or [{"name": "default", "codex_home": "~/.codex"}]
    for account in configured:
        cooling = state.get(account["name"], {}).get("cooldown_until", 0) - now
        print(f"  {account['name']:12s} {account['codex_home']:24s} "
              f"{'logged in' if account['name'] in logged else 'NOT LOGGED IN'}"
              + (f", cooling {int(cooling // 60)} min" if cooling > 0 else ""))
    print("roles:")
    for role, config in (settings.data.get("roles") or {}).items():
        extra = " ".join(x for x in (f"gateway={config['gateway']}" if config.get("gateway") else "",
                                     "web" if config.get("web") else "",
                                     f"→ {config['fallback']}" if config.get("fallback") else "") if x)
        print(f"  {role:18s} {config.get('engine', 'codex'):6s} {config.get('model', ''):28s} {extra}")
    log = call_log_path(settings)
    if log.exists():
        today = time.strftime("%Y-%m-%d")
        totals: dict[tuple[str, str], list[float]] = {}
        for raw in log.read_text(encoding="utf-8").splitlines():
            e = json.loads(raw)
            if not e["ts"].startswith(today):
                continue
            tokens = e.get("tokens") or sum(e.get(k) or 0 for k in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
            row = totals.setdefault((e["engine"], e["role"]), [0, 0, 0.0])
            row[0] += 1
            row[1] += tokens or 0
            row[2] += e.get("cost_usd") or 0
        if totals:
            print("today (calls, tokens):")
            for (engine, role), (calls, tokens, cost) in sorted(totals.items(), key=lambda x: -x[1][1]):
                print(f"  {engine:6s} {role:18s} {calls:4d} calls {tokens:>12,} tokens"
                      + (f"  ${cost:.2f}" if cost else ""))
        print("recent calls:")
        for raw in log.read_text(encoding="utf-8").splitlines()[-8:]:
            e = json.loads(raw)
            print(f"  {e['ts']} {str(e.get('project')):14s} {e['role']:16s} {e['model']:20s} {e['account']:14s} "
                  f"{e['seconds']:>6}s ok={e['ok']} limited={e['rate_limited']}")
    return 0


# --- run / research / do ---------------------------------------------------------

def _read_prompt(args: argparse.Namespace) -> str:
    if getattr(args, "prompt_file", None):
        return args.prompt_file.read_text(encoding="utf-8")
    if getattr(args, "prompt", None):
        return args.prompt
    return sys.stdin.read()


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    result = run_role(settings, args.role, _read_prompt(args), cwd=args.cwd, schema=args.schema, timeout=args.timeout)
    if args.out:
        args.out.write_text(result.text, encoding="utf-8")
    print(result.text)
    print(f"[{result.role} {result.model} via {result.account}, {result.seconds}s, ok={result.ok}, "
          f"log={result.log_path}]", file=sys.stderr)
    return 0 if result.ok else 1


def cmd_research(args: argparse.Namespace, settings: Settings) -> int:
    run = Run.new(settings, args.run_id)
    run.meta(id=run.id, kind="research", question=args.question)
    brief = research(settings, args.question, run=run, context=args.context or "")
    if args.out:
        args.out.write_text(brief, encoding="utf-8")
    print(brief or "(research failed; see the run logs)")
    print(f"[saved {run.dir / 'research.md'}]", file=sys.stderr)
    return 0 if brief else 1


def cmd_do(args: argparse.Namespace, settings: Settings) -> int:
    _need_root(settings)
    task = args.task.read_text(encoding="utf-8") if args.task else args.text
    if not task:
        sys.exit("ah do: give --task FILE or a task text")
    plan = args.plan.read_text(encoding="utf-8") if args.plan else None
    run, status = pipeline(settings, task, plan_text=plan, run_id=args.run_id, planner=args.planner,
                           executor=args.executor, max_rounds=args.max_rounds, with_research=args.research,
                           tests_override=args.tests)
    print(f"run {run.id}: {status}; report {run.dir / 'report.md'}")
    if status == "ready" and args.ship:
        return cmd_ship(argparse.Namespace(run=run.id, executor=args.executor, merge=None), settings)
    return 0 if status == "ready" else 1


# --- ship / review / merge ---------------------------------------------------------

def cmd_ship(args: argparse.Namespace, settings: Settings) -> int:
    try:
        pr = ship(settings, load_run(settings, args.run), executor=args.executor, merge=args.merge)
    except (ShipError, gitops.GitError, FileNotFoundError) as exc:
        print(f"ah ship: {exc}", file=sys.stderr)
        return 1
    print(pr)
    return 0


def cmd_review(args: argparse.Namespace, settings: Settings) -> int:
    root = _need_root(settings)
    run = Run.new(settings, f"review-{time.strftime('%Y%m%d-%H%M%S')}")
    if args.pr:
        try:
            verdict = review_pr(settings, args.pr, cwd=root, log_dir=run.dir, post=args.post)
        except ShipError as exc:
            print(f"ah review: {exc}", file=sys.stderr)
            return 1
    else:
        from .pipeline import VERDICT_SCHEMA, parse_verdict, prompt

        base = args.base or settings.section("git").get("base", "main")
        diff = gitops.git("diff", f"{base}...HEAD", cwd=root) + "\n" + gitops.git("diff", "HEAD", cwd=root)
        verdict = parse_verdict(run_role(settings, "reviewer", prompt(
            "pr_review", settings, title=f"working tree vs {base}", body="", diff=diff[:150_000]),
            cwd=root, schema=VERDICT_SCHEMA, log_dir=run.dir, wait_for_quota=False).text)
    from .ship import format_review

    print(format_review(verdict))
    return 0 if verdict.get("verdict") == "approve" else 1


def cmd_merge(args: argparse.Namespace, settings: Settings) -> int:
    meta = load_run(settings, args.run)
    try:
        head = gitops.merge_locally(settings, meta["branch"])
    except gitops.GitError as exc:
        print(f"ah merge: {exc}", file=sys.stderr)
        return 1
    print(f"merged {meta['branch']} → {head}")
    return 0


def cmd_runs(args: argparse.Namespace, settings: Settings) -> int:
    runs_dir = settings.state_dir / "runs"
    for path in sorted(runs_dir.glob("*/run.json"))[-args.limit:]:
        meta = json.loads(path.read_text(encoding="utf-8"))
        alive = Path(meta["worktree"]).exists() if meta.get("worktree") else False
        print(f"{meta.get('id', path.parent.name):24s} {meta.get('status', meta.get('kind', '?')):10s} "
              f"{meta.get('pr', '') or ('worktree' if alive else '')}")
    return 0


def cmd_clean(args: argparse.Namespace, settings: Settings) -> int:
    _need_root(settings)
    runs = sorted((settings.state_dir / "runs").glob("*/run.json"))
    targets = [p for p in runs if args.all or p.parent.name in args.runs]
    skipped = False
    for path in targets:
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("worktree") and Path(meta["worktree"]).exists():
            keep_branch = bool(meta.get("pr")) and not args.delete_branches
            try:
                gitops.remove_worktree(settings, Path(meta["worktree"]), meta.get("branch"),
                                      delete_branch=not keep_branch, force=args.force)
            except gitops.GitError as exc:
                print(f"skipped {meta.get('id', path.parent.name)}: {exc} (use --force to remove anyway)")
                skipped = True
                continue
            print(f"removed worktree {meta['worktree']}")
    gitops.git("worktree", "prune", cwd=settings.root, check=False)
    return 1 if skipped else 0


def cmd_skills(args: argparse.Namespace, settings: Settings) -> int:
    source = TEMPLATES / "SKILL.md"
    if args.project:
        root = _need_root(settings)
        targets = [root / ".claude" / "skills", root / ".agents" / "skills"]
    else:
        targets = [Path.home() / ".claude" / "skills", Path.home() / ".codex" / "skills"]
    for base in targets:
        target = base / "agent-harness" / "SKILL.md"
        if target.parent.is_symlink():
            print(f"skip {target.parent} (a symlink; managed elsewhere)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, target)
        print(f"installed {target}")
    return 0


# --- parser ------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ah", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"agent-harness {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="set up this repository")
    p.add_argument("--ci", action="store_true", help="also write .github/workflows/ci.yml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    sub.add_parser("doctor", help="check the setup").set_defaults(func=cmd_doctor)
    sub.add_parser("status", help="accounts, roles, recent calls").set_defaults(func=cmd_status)

    p = sub.add_parser("run", help="one role call")
    p.add_argument("role")
    p.add_argument("prompt", nargs="?")
    p.add_argument("--prompt-file", type=Path)
    p.add_argument("--cwd", type=Path)
    p.add_argument("--schema", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--timeout", type=int, default=3600)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("research", help="web research brief")
    p.add_argument("question")
    p.add_argument("--context")
    p.add_argument("--out", type=Path)
    p.add_argument("--run-id")
    p.set_defaults(func=cmd_research)

    p = sub.add_parser("do", help="plan → execute → test → review in a worktree")
    p.add_argument("text", nargs="?", help="task text (or --task FILE)")
    p.add_argument("--task", type=Path)
    p.add_argument("--plan", type=Path, help="skip planning and use this approved plan")
    p.add_argument("--run-id")
    p.add_argument("--planner", default="planner")
    p.add_argument("--executor", default="executor")
    p.add_argument("--max-rounds", type=int)
    p.add_argument("--research", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--tests", help="test command for this run")
    p.add_argument("--ship", action="store_true", help="ship when ready")
    p.set_defaults(func=cmd_do)

    p = sub.add_parser("ship", help="push, PR, CI, review, merge per policy")
    p.add_argument("run")
    p.add_argument("--executor", default="executor")
    p.add_argument("--merge", choices=["manual", "auto"])
    p.set_defaults(func=cmd_ship)

    p = sub.add_parser("review", help="review a PR or the current branch")
    p.add_argument("--pr")
    p.add_argument("--base")
    p.add_argument("--post", action="store_true", help="post the review on the PR")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("merge", help="guarded local merge of a run's branch")
    p.add_argument("run")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("runs", help="list runs")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("clean", help="remove run worktrees (logs are kept)")
    p.add_argument("runs", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--delete-branches", action="store_true", help="also delete branches that have a PR")
    p.add_argument("--force", action="store_true",
                   help="remove even with uncommitted changes or commits that are not merged or pushed")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("skills", help="install the head-agent skill")
    p.add_argument("action", choices=["install"])
    p.add_argument("--project", action="store_true", help="into this repository instead of your user folders")
    p.set_defaults(func=cmd_skills)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load()
    user_dir().mkdir(parents=True, exist_ok=True)
    return int(args.func(args, settings) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
