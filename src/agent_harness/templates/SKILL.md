---
name: agent-harness
description: Delegate coding work through the `ah` agent harness — web research, planning by one model, plan review by another, execution in an isolated git worktree, tests, code review, then push/PR/CI and merge per policy. Use when a change spans several files or needs a plan, when the user asks to split work between Codex and Claude, to review a PR, to research a library or API on the web before coding, or to ship a branch.
---

# Agent harness (`ah`)

You are the head agent. The harness runs other models through the `codex` and
`claude` CLIs; you decide what to delegate, read every result, and own the merge.

## Commands

```bash
ah doctor                             # setup check (CLIs, logins, gh, project config)
ah research "question" [--out f.md]   # web research brief with sources (primary docs first)
ah do --task task.md [--research]     # plan → plan review → execute in worktree → tests → review → fix
ah do --task task.md --plan plan.md   # skip planning with an approved plan
ah do "small task text" --executor coder
ah runs                               # runs and their status
ah ship <run>                         # push branch, open PR, wait for CI (auto-fix rounds), agent PR review
ah review --pr 12 [--post]            # review a PR; --post comments on it
ah review                             # review the current branch/working tree against base
ah merge <run>                        # guarded local merge (no remote, or the user asked)
ah clean <run> | --all [--force]      # remove finished worktrees (logs kept); skips uncommitted or unmerged/unpushed work unless --force
ah run <role> "prompt"                # one call: scout, coder, reviewer, researcher, ...
ah status                             # account cooldowns, roles, recent calls
```

Run artifacts: `.agent-harness/state/runs/<run>/` (task, research, plan, plan
reviews, execute notes, test logs, reviews, report.md). The branch lives in
`.agent-harness/state/wt/<run>`.

## Choosing

| Work | Do |
|---|---|
| One-line fix, a lookup | do it yourself |
| Unknown library/API, version questions | `ah research` first, then plan |
| Multi-file change with tests | `ah do --task` |
| Bounded edit, script, tests for a behaviour | `ah do "…" --executor coder` |
| Second opinion on a diff or PR | `ah review` |

Write task files like a ticket: goal, observable behaviour, constraints, files
you already know matter, how to test.

## Rules

- Read the run's report, plan and diff (`git -C <worktree> diff <base>...HEAD`)
  before shipping or merging. Nothing merges itself unless the project's
  `[git] merge = "auto"` says so.
- Ask the user before `ah ship` or `ah merge` unless they already told you to
  ship this work.
- Never `git add -A` / `git add .` in a harness worktree; the harness stages
  path by path and refuses symlinks, protected paths and large files. If a run
  reports "unsafe branch", fix it, do not override it.
- `ah merge` refuses a branch that would overwrite untracked or ignored local
  files (e.g. a data directory). Do not work around that refusal.
- Report results faithfully: failing tests, skipped steps, open review issues.
