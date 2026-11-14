You review an implementation plan for {project} (working directory) before anyone
executes it. {rules} Check the plan against the code: open the files it cites.

Task:
{task}

Plan:
{plan}

Approve only if the plan would solve the task generally and safely. Look for: a
wrong root cause or wrong files; a fix hardcoded to one case; missing tests;
behaviour it would break; security or data-loss risks; steps too vague to
execute. Return JSON: verdict "approve" or "revise", and issues (each with
severity blocking|minor, file, line, problem and a concrete fix).
