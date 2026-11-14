You review a change to {project}. The working directory has the change
committed on branch {branch}; inspect it with `git diff {base}...HEAD`. {rules}

Task:
{task}

Plan it implements:
{plan}

Test run:
{tests}

Find real defects: behaviour that does not do what the task needs, regressions,
security problems (secrets, injection, unsafe file or shell handling), data
loss, hardcoded special cases, missing or weak tests, broken edge cases. Ignore
style nits. Return JSON: verdict "approve" or "changes", and issues (each with
severity blocking|minor, file, line, problem and the concrete fix).
