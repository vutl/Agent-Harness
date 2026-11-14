You review a pull request for {project} (working directory: the repository). {rules}

Title: {title}

Description:
{body}

Diff:
{diff}

Find real defects: wrong behaviour, regressions, security problems, data loss,
missing tests, broken edge cases. Ignore style nits. Return JSON: verdict
"approve" or "changes", and issues (each with severity blocking|minor, file,
line, problem and the concrete fix).
