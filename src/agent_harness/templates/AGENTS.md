# AGENTS.md — {name}

Rules every coding agent (Codex, Claude Code, harness roles) follows in this repository.

## Project

- What it is, how to run it, where the main code lives.

## Rules

- Run the tests before finishing; never weaken a test to make it pass.
- Never commit secrets, `.env` files, data directories or symlinks.
- Stage explicit paths; never `git add -A` / `git add .`.
- Fix the general mechanism; do not special-case one input or test.
