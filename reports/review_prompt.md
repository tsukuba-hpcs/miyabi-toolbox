Deeply review all code under <TARGET_PATH> as a complete system/subsystem.

Use CodeGraph, Ruff and any other available tools as needed to investigate and validate findings.

Understand its architecture, execution paths, state ownership, invariants, dependencies, tests, and failure behavior before reporting issues. Inspect code outside the target path when needed to understand interactions.

Focus on real problems:

* correctness and regressions;
* broken invariants;
* concurrency/distributed/recovery issues;
* architectural flaws and unclear ownership;
* duplicated sources of truth;
* unnecessary complexity, legacy code, dead code, and obsolete compatibility paths;
* meaningful test gaps;
* performance/resource/scalability problems.

Do not do a superficial file-by-file style review.

For every candidate finding, trace the actual execution path, inspect relevant callers/callees/tests, and actively try to disprove it before reporting it.

Prefer a few high-confidence findings over speculative observations.

For each finding give severity, exact location, problem, evidence, impact, and recommended fix.

Finally summarize architecture quality, simplification/deletion opportunities, missing tests, and an overall verdict.

Report the results to `reports/DOING/code_review/{current_model_name}_{HEAD commit-id}_{YYMMDD-HHMM}.md`