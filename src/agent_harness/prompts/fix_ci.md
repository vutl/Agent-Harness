CI failed on branch {branch} of {project}. {rules}
Fix the cause in this worktree; do not weaken or skip tests to make CI pass
unless the test itself is wrong (then say why).

Failed checks:
{checks}

Failure log (tail):
{log}

Run the tests ({tests}) before finishing. Do not run `git add -A` or commit: the
harness stages and commits your changes. Final message: the cause and the fix.
