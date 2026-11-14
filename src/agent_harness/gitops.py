"""Git operations with guard rails.

Agents never run a bare ``git add``. The harness stages only regular files the
agent changed, and refuses symlinks, protected paths and large files. Before a
push or merge it checks the branch for symlinks and protected paths, and a local
merge is refused when the branch adds a path that exists, untracked or ignored,
in the main checkout: git would replace that local directory and its contents
would be lost.
A worktree with uncommitted changes or commits not on the base branch or a remote
branch is not removed without force.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings

SYMLINK_MODE = "120000"
GITLINK_MODE = "160000"


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path, check: bool = True) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and completed.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {completed.stderr.strip() or completed.stdout.strip()}")
    return completed.stdout.strip()


def is_protected(path: str, patterns: list[str]) -> bool:
    """True when ``path`` or any of its parent directories matches a pattern."""
    path = path.strip("/")
    parts = path.split("/")
    candidates = ["/".join(parts[: index + 1]) for index in range(len(parts))]
    for pattern in patterns:
        pattern = pattern.strip("/")
        for candidate in candidates:
            if fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(candidate.rsplit("/", 1)[-1], pattern):
                return True
    return False


# --- worktrees ---------------------------------------------------------------

def create_worktree(settings: Settings, run: str, base_ref: str = "HEAD") -> tuple[Path, str, str]:
    """New branch ``<prefix><run>`` in ``state/wt/<run>``; links the configured runtime inputs."""
    root = _root(settings)
    base = git("rev-parse", base_ref, cwd=root)
    branch = f"{settings.section('git').get('branch_prefix', 'harness/')}{run}"
    worktree = settings.state_dir / "wt" / run
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-q", "-b", branch, str(worktree), base, cwd=root)
    for relative in settings.section("worktree").get("link") or []:
        source, target = root / relative, worktree / relative
        if source.exists() and not target.exists() and not target.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source)
    return worktree, base, branch


def unsaved_work(settings: Settings, worktree: Path, branch: str | None = None) -> list[str]:
    """Reasons removing this worktree would lose uncommitted or unmerged/unpushed work."""
    try:
        changes = changed_paths(worktree)
    except subprocess.CalledProcessError as exc:
        raise GitError(f"git status: {(exc.stderr or '').strip()}") from exc
    links = {path.strip("/") for path in settings.section("worktree").get("link") or []}
    paths = [path for _status, path in changes
             if not (path.strip("/") in links and (worktree / path).is_symlink())]
    reasons = []
    if paths:
        preview = ", ".join(paths[:3]) + (", …" if len(paths) > 3 else "")
        reasons.append(f"{len(paths)} uncommitted change{'s' if len(paths) != 1 else ''} ({preview})")

    base = settings.section("git").get("base", "main")
    base_ref = f"refs/heads/{base}"
    branch_refs = [f"refs/heads/{branch}"] if branch else []
    refs = git("for-each-ref", "--format=%(refname)", base_ref, *branch_refs, cwd=worktree).splitlines()
    tips = ["HEAD", *(ref for ref in branch_refs if ref in refs)]
    commits = git("log", "--format=%h %s", *tips, "--not",
                  *([base_ref] if base_ref in refs else []), "--remotes", cwd=worktree).splitlines()
    if commits:
        reason = f"{len(commits)} commit{'s' if len(commits) != 1 else ''} not merged or pushed ({commits[0]})"
        if base_ref not in refs:
            reason += f" — base branch '{base}' not found"
        reasons.append(reason)
    return reasons


def remove_worktree(settings: Settings, worktree: Path, branch: str | None = None, *,
                    delete_branch: bool = True, force: bool = False) -> None:
    root = _root(settings)
    if not force:
        reasons = unsaved_work(settings, worktree, branch)
        if reasons:
            raise GitError("; ".join(reasons))
    # Unlink runtime links first so nothing can follow them while removing.
    unlinked = []
    for relative in settings.section("worktree").get("link") or []:
        target = worktree / relative
        if target.is_symlink():
            saved = os.readlink(target)
            target.unlink()
            unlinked.append((target, saved))
    if force:
        git("worktree", "remove", "--force", str(worktree), cwd=root, check=False)
    else:
        try:
            git("worktree", "remove", str(worktree), cwd=root)
        except GitError:
            for target, saved in unlinked:
                if not target.exists() and not target.is_symlink():
                    target.symlink_to(saved)
            raise
    if branch and delete_branch:
        git("branch", "-D", branch, cwd=root, check=not force)


# --- staging -----------------------------------------------------------------

@dataclass
class StageResult:
    staged: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)


def changed_paths(cwd: Path) -> list[tuple[str, str]]:
    """(status, path) for every change in the working tree, untracked files listed one by one."""
    raw = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                         cwd=cwd, capture_output=True, text=True, check=True).stdout
    entries = raw.split("\0")
    result = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":
            index += 1  # the next field is the rename source
        result.append((status, path))
    return result


def stage_safely(settings: Settings, cwd: Path) -> StageResult:
    """Stage the agent's changes path by path; refuse anything unsafe."""
    protect = settings.protect() + list(settings.section("worktree").get("link") or [])
    max_bytes = int(float(settings.section("safety").get("max_file_mb", 5)) * 1024 * 1024)
    result = StageResult()
    for status, path in changed_paths(cwd):
        full = cwd / path
        reason = None
        if is_protected(path, protect):
            reason = "protected path"
        elif full.is_symlink():
            reason = "symlink"
        elif full.is_file() and full.stat().st_size > max_bytes:
            reason = f"larger than {max_bytes // (1024 * 1024)} MB"
        if reason:
            result.refused.append((path, reason))
            continue
        if status.strip() == "D" or not full.exists():
            git("rm", "-q", "--cached", "--ignore-unmatch", "--", path, cwd=cwd)
        else:
            git("add", "--", path, cwd=cwd)
        result.staged.append(path)
    return result


def commit_if_staged(cwd: Path, message: str) -> bool:
    if not git("diff", "--cached", "--name-only", cwd=cwd):
        return False
    git("commit", "-q", "-m", message, cwd=cwd)
    return True


# --- branch checks -----------------------------------------------------------

@dataclass
class DiffEntry:
    status: str
    path: str
    old_mode: str
    new_mode: str


def branch_diff(cwd: Path, base: str, head: str = "HEAD") -> list[DiffEntry]:
    raw = subprocess.run(["git", "diff", "--raw", "-z", "--no-renames", f"{base}..{head}"],
                         cwd=cwd, capture_output=True, text=True, check=True).stdout
    fields = raw.split("\0")
    entries = []
    for index in range(0, len(fields) - 1, 2):
        meta, path = fields[index], fields[index + 1]
        if not meta.startswith(":"):
            continue
        old_mode, new_mode, _old, _new, status = meta[1:].split()
        entries.append(DiffEntry(status=status, path=path, old_mode=old_mode, new_mode=new_mode))
    return entries


def unsafe_changes(settings: Settings, cwd: Path, base: str, head: str = "HEAD") -> list[str]:
    """Reasons this branch must not be pushed or merged."""
    protect = settings.protect() + list(settings.section("worktree").get("link") or [])
    problems = []
    for entry in branch_diff(cwd, base, head):
        if entry.status == "D":
            continue
        if entry.new_mode == SYMLINK_MODE:
            problems.append(f"{entry.path}: commits a symlink")
        elif entry.new_mode == GITLINK_MODE and entry.old_mode != GITLINK_MODE:
            problems.append(f"{entry.path}: adds a submodule/gitlink")
        if is_protected(entry.path, protect):
            problems.append(f"{entry.path}: protected path")
    return problems


def local_paths_at_risk(settings: Settings, branch: str, base: str) -> list[str]:
    """Paths the branch adds that already exist untracked or ignored in the main
    checkout. Merging would make git overwrite them (an ignored directory is
    deleted without warning)."""
    root = _root(settings)
    tracked = set(git("ls-files", "-z", cwd=root).split("\0"))
    risky = []
    for entry in branch_diff(root, base, branch):
        if entry.status != "A":
            continue
        parts = entry.path.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            local = root / prefix
            if (local.exists() or local.is_symlink()) and prefix not in tracked:
                # A tracked directory is fine; an untracked/ignored file or directory is not.
                if local.is_dir() and not local.is_symlink() and any(p.startswith(prefix + "/") for p in tracked):
                    continue
                risky.append(prefix)
                break
    return sorted(set(risky))


def merge_locally(settings: Settings, branch: str, base_branch: str | None = None) -> str:
    """Guarded ``git merge --no-ff`` of ``branch`` into the base branch of the main checkout."""
    root = _root(settings)
    base_branch = base_branch or settings.section("git").get("base", "main")
    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    if current != base_branch:
        raise GitError(f"main checkout is on {current!r}, not {base_branch!r}")
    if git("status", "--porcelain", "--untracked-files=no", cwd=root):
        raise GitError("main checkout has uncommitted changes to tracked files")
    merge_base = git("merge-base", base_branch, branch, cwd=root)
    problems = unsafe_changes(settings, root, merge_base, branch)
    risky = local_paths_at_risk(settings, branch, merge_base)
    if risky:
        problems += [f"{path}: exists locally (untracked/ignored); the merge would overwrite it" for path in risky]
    if problems:
        raise GitError("refusing to merge:\n  " + "\n  ".join(problems))
    git("merge", "--no-ff", "-q", "-m", f"Merge {branch}", branch, cwd=root)
    return git("rev-parse", "--short", "HEAD", cwd=root)


def _root(settings: Settings) -> Path:
    if settings.root is None:
        raise GitError("not inside a git repository")
    return settings.root


def has_remote(settings: Settings) -> bool:
    remote = settings.section("git").get("remote", "origin")
    return remote in git("remote", cwd=_root(settings), check=False).split()


def ensure_state_ignored(root: Path) -> bool:
    """Add ``.agent-harness/state/`` to .gitignore; True when the file changed."""
    gitignore = root / ".gitignore"
    line = "/.agent-harness/state/"
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if line in text.splitlines():
        return False
    gitignore.write_text(text + ("" if text.endswith("\n") or not text else "\n") + line + "\n", encoding="utf-8")
    return True
