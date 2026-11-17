You check a fix round on branch {branch} of {project}. {rules}
The previous review found the issues below; the executor has since committed fixes.
Inspect `git diff {since}..HEAD` (this round's changes only); open surrounding code as needed,
but do not re-review the rest of the branch.

Task:
{task}

Issues to verify:
{issues}

Test run:
{tests}

For each issue: is it fixed, and is there a test that proves it? Report any new defect the
fix introduces. Do not raise new findings about unchanged code unless they are severe
(security, data loss). Return JSON: verdict "approve" or "changes", and issues (each with
severity blocking|minor, file, line, problem and the concrete fix).
