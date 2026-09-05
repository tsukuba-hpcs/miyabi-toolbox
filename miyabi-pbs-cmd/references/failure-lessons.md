# Operational Lessons From Recorded Failures

This is a provenance index for the operational guidance, not a requirement to
read a separate project before using the skill. The reviewed corpus is the 12
`failures.md` files under
`/work/xg24i002/x10041/fsb_decoupled_diloco/reports/checked/`, inspected on
2026-09-05. Links below refer to local historical records; the maintained rules
and their scope live in this skill's linked references.

| Recorded incident | Reusable lesson and maintained guidance |
| --- | --- |
| [spec01] — “Login host 无法执行项目 Ruff”; [spec02] — 2026-08-26 00:39; [spec03] — 2026-08-26 14:59 | A G environment's ARM binaries failed on C login. Choose role, architecture and workload separately; preserve the environment and route incompatible/runtime checks to target compute. Compatible lightweight tools may run on login when project rules permit. [python-env.md](python-env.md) |
| [spec01] — “首轮九节点运行使用了已解析的 Python 路径” | Resolving `.venv/bin/python` to base Python lost site-packages, causing missing `yaml` on all actors although parent validation passed. Preserve the virtualenv symlink and exercise the child interpreter. [python-env.md](python-env.md), [templates.md](templates.md) |
| [plan05] — U1 candidates 4, 5, 17; [debug-info] — 2026-09-02 17:45 | A missing Node toolchain, npm's env-based Node lookup, and unexported/wrong `PYTHON_BIN` broke nested validation. Bind and propagate the selected interpreters through the actual wrapper chain. Do not impose the recorded Node/Python versions on other projects. [python-env.md](python-env.md) |
| [training-validation] — G3 compiler failure and replacement launch failure, 2026-09-01 10:44/10:53 UTC | Triton invoked site `nvc`; passing `CC` before a child login shell did not preserve the override. A fresh-cache probe confirmed the selected GNU compiler worked when applied after child initialization. Keep the workaround symptom- and version-dependent. [mpi.md](mpi.md) |
| [debug-info] — 17:15/17:57; [new-design-review-repair] — 09:58/11:05 | Generic PBS `qstat -u`, `-Q`, `-xf` and `-x -f` were rejected. Use the site dialect. The earlier inability to retrieve history is superseded by the live-verified `qstat -H -f <id>` interface. [qstat.md](qstat.md) |
| [new-design] — Phase 8; [experiment05-adamw-performance] — 03:03/05:11 | Interactive `regular-g`, explicit `ngpus` on `interact-g`, and omitted `group_list` caused submission failures. Use the supported interactive queue and its resource form, with an explicit eligible group. [qsub.md](qsub.md) |
| [new-design] — Phase 5 allocation boundary; [training-validation] — G0 | Walltime expiration or an `exit` in a check command closed the interactive session. Preserve logs, isolate check-shell failure, recheck host/allocation before continuing, and renew only within authorization. [validation.md](validation.md) |
| [new-design] — Phase 1; [training-validation] — G6 preparation log; [fs-diloco-runtime-reliability] — candidate `2919cc48` and retry `3288620` | Compute `/tmp` was not visible on login, `tee` opened before its parent existed, and ranks lacked publication parents. Create required shared parents before pipelines/MPI; keep large preflight artifacts out of the small report package. [validation.md](validation.md), [filesystem-network.md](filesystem-network.md) |
| [spec-new-design-review-01] — 07:08; [spec03] — 14:59; [training-validation] — G7 | Compute prepare-only mode inherited a login-only gate, while another submitter executed ARM validation on login; an omitted prepare-only flag started a workload. Define separate submission and preparation contracts and verify the actual mode. [validation.md](validation.md) |
| [debug-info] — 17:30/17:32/17:54; [new-design] — Phase 4; [fs-diloco-runtime-reliability] — invocation corrections | Wrong/deleted paths, incorrect cwd/checkout, or test discovery context invalidated checks. Inspect the actual invocation and use the chosen checkout's paths; do not convert harness mistakes into product fixes. [validation.md](validation.md) |
| [spec02] — 02:09; [plan05] — “Formal orchestration”; [experiment05-adamw-performance] — 01:31 | Queue delay was mistaken for startup failure, concurrent supervisors starved child jobs, and a queued job could not be moved with `qalter -q`. Account for project limits and actual readiness; select an eligible queue before submitting. These records do not imply every experiment must run serially. [qsub.md](qsub.md), [validation.md](validation.md) |
| [fs-diloco-runtime-reliability] — retry `3288655`; [spec01] — descriptor failure with PBS exit 0 | Parent allocation status and even PBS exit 0 were insufficient to establish actor/application success. Reconcile rank exits and expected outputs/readback separately. [qstat.md](qstat.md), [mpi.md](mpi.md) |

The remaining incidents were reviewed for scope. Project-specific protocol,
database/retention, model-performance, metric/oracle and generated-documentation
defects remain with their owners. Historical requirements to rerun all scenarios,
use particular candidate checkpoints, hash artifacts, or impose special retry
counts are not imported. The `model-retention-cleanup` records supplied no
additional Miyabi-specific rule. Historical incident fixes never override the
current user instruction, project contract or live site evidence.

[spec01]: /work/xg24i002/x10041/fsb_decoupled_diloco/reports/checked/spec01/failures.md
[spec02]: /work/xg24i002/x10041/fsb_decoupled_diloco/reports/checked/spec02/failures.md
[spec03]: /work/xg24i002/x10041/fsb_decoupled_diloco/reports/checked/spec03/failures.md
[plan05]: /work/xg24i002/x10041/fsb_decoupled_diloco/reports/checked/plan05/failures.md
[debug-info]: /work/xg24i002/x10041/fsb_decoupled_diloco/reports/checked/debug-info/failures.md
[new-design]: /work/xg24i002/x10041/fsb_decoupled_diloco/reports/checked/new-design/failures.md
[new-design-review-repair]: /work/xg24i002/x10041/fsb_decoupled_diloco/reports/checked/new-design-review-repair/failures.md
[spec-new-design-review-01]: /work/xg24i002/x10041/fsb_decoupled_diloco/reports/checked/spec-new-design-review-01/failures.md
[training-validation]: /work/xg24i002/x10041/fsb_decoupled_diloco/reports/checked/training-validation/failures.md
[experiment05-adamw-performance]: /work/xg24i002/x10041/fsb_decoupled_diloco/reports/checked/experiment05-adamw-performance/failures.md
[fs-diloco-runtime-reliability]: /work/xg24i002/x10041/fsb_decoupled_diloco/reports/checked/fs-diloco-runtime-reliability/failures.md
