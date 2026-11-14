# AGENTS.md — Agent-Harness

`ah`: a CLI that routes planning, coding, review and shipping to Codex and
Claude Code agents. Python 3.11+, standard library only (no runtime deps).

## Layout

- `src/agent_harness/config.py`: layered config (package < user < project).
- `engines.py`: `codex exec` / `claude -p` calls, account rotation, cooldowns, fallbacks.
- `gitops.py`: worktrees, safe staging, branch safety checks, guarded local merge.
- `pipeline.py`: research → plan → plan review → execute → tests → review rounds.
- `ship.py`: push, PR, CI watch and fix rounds, PR review, merge policy (via `gh`).
- `cli.py`: the `ah` command. `prompts/`, `schemas/`, `templates/`: package data.

## Rules

- Tests: `uv run --with pytest python -m pytest -q`; lint: `uv run --with ruff ruff check src tests`.
- Tests never call real models or the network: fake `run_role` / `gh` with monkeypatch.
- Keep the package dependency-free. Keep personal data (accounts, emails, hosts,
  tokens) out of the repository: it is public. Machine settings go in the user config.
- Never weaken the safety checks in `gitops.py`; every new git path needs a test.
