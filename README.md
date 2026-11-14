# Agent-Harness

`ah` runs a software change through several AI coding agents, the way a small
team would: one model researches and plans, a different model reviews the plan,
another writes the code in an isolated git worktree, the tests run, a reviewer
checks the diff, and the branch is pushed, opened as a PR, tested by CI and
merged only when your policy allows.

It drives the official CLIs (`codex exec`, `claude -p`), so it uses your
existing ChatGPT/Codex and Claude subscriptions. Several Codex accounts rotate
automatically on usage limits, and every role falls back to the other vendor
when its engine is out of quota or not installed.

```
research (web) → plan → plan review → execute (worktree) → tests → review → fix
                                                                          ↓
                    merge (manual or auto) ← agent PR review ← CI (+ fix rounds) ← PR ← push
```

## Install

```bash
uv tool install git+https://github.com/vutl/Agent-Harness     # or: pipx install git+…
ah skills install          # teach Claude Code and Codex (the head agent) to use it
```

Requirements: Python 3.11+, git, and at least one of the
[Codex CLI](https://github.com/openai/codex) (`codex login`) or
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude` logged in).
For PRs and CI: the GitHub CLI (`brew install gh && gh auth login`).

## Use in a project

```bash
cd your-repo
ah init --ci       # .agent-harness/config.toml, .gitignore entry, AGENTS.md, CI workflow
ah doctor          # check everything is wired

ah research "How do I stream responses with FastAPI and SSE in 2026?"
ah do --task task.md --research    # plan → … → reviewed branch in a worktree
ah runs
ah ship <run>                      # push, PR, CI with auto-fix rounds, agent review
ah review --pr 42 --post           # review any PR and comment on it
```

Inside Claude Code or Codex you can simply ask: *"use the harness to add rate
limiting to the API"*; the installed skill tells the head agent how.

## Configuration

Three layers, later wins (tables merge key by key, lists replace):

| Layer | File | Holds |
|---|---|---|
| package | `agent_harness/defaults.toml` | roles, safety defaults |
| you | `~/.config/agent-harness/config.toml` | Codex accounts, gateways, model choices |
| project | `.agent-harness/config.toml` (committed) | rules, test command, protected paths, git policy |

Example user config:

```toml
[[accounts]]
name = "personal"
codex_home = "~/.codex-personal"     # CODEX_HOME=~/.codex-personal codex login

[[accounts]]
name = "work"
codex_home = "~/.codex-work"

[roles.executor]
model = "gpt-6.1-sol"

# Optional: an Anthropic-compatible gateway for extra Claude capacity.
[gateways.router]
base_url = "http://127.0.0.1:20128"
token_env = "ROUTER_API_KEY"

[roles.coder_gw]
engine = "claude"
gateway = "router"
model = "claude-sonnet-5-5"
permission_mode = "dontAsk"
tool_set = "edit"
fallback = "coder"
```

Roles: `researcher` (web), `planner` (web), `plan_review`, `executor`, `coder`,
`reviewer`, `scout`, each with a fallback. `ah status` lists them.

## Safety

Agents never stage files themselves. After each round the harness stages the
changed files path by path and refuses:

- symlinks, protected paths (`.env`, keys, `node_modules`, your extra list) and
  files over `max_file_mb`;
- branches that commit a symlink, a submodule or a protected path (no push, no merge);
- local merges that would overwrite untracked or **ignored** files in your
  checkout. Git silently deletes an ignored directory when a merged branch adds
  a file at that path; the harness checks for this before merging.

Merging is `manual` by default: the harness opens the PR and stops. Set
`[git] merge = "auto"` per project to merge after CI passes and the agent review
has no blocking issues.

## Development

```bash
uv run --with pytest --with ruff python -m pytest -q
uv run --with ruff ruff check src tests
```

MIT licensed.
