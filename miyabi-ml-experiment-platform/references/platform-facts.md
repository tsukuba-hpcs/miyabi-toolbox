# Miyabi-G platform facts

Checked on the dates given (2026-10-16 unless stated). Quotas, queue settings
and site load change: rerun the check before a design decision depends on a
number. The project group in the examples is `xg24i002`.

## Contents

1. Nodes and storage
2. Queues
3. Project limits
4. Measured queue and run times
5. Pitfalls and fixes

## 1 Nodes and storage

| Item | Value | Check |
| --- | --- | --- |
| Compute node | 1 GH200 per node, aarch64, 72 cores; device memory 102,005,473,280 B (≈ 95 GiB); job memory 100 GiB per node | `exec_vnode` in `qstat -H -f <job>`; `torch.cuda.mem_get_info()` inside a job |
| Accounting | one GPU per node, so node·h = GPU·h. The `TOKEN` column tracks node·h (2026-10-20: 11 nodes × 5,059 s → 15.5) | `qstat -H` |
| `/local` | node-local NVMe, ~900 GB (965 GB free on mg0016); job scratch only. PBS also exports `LOCALDIR=/local/<job>.<n>` (observed 2026-10-20; whether the site empties it at job end is unverified, so remove your own directory) | `df -h /local` inside a job |
| `/work` | Lustre. Group quota 27 TB and 56.62 M files (14.6 TB and 12.9 M files used across the group's projects) | `lfs quota -g xg24i002 /work`; `lfs quota -u $USER /work` |
| Lustre semantics | plain `rename` is atomic; `renameat2(RENAME_NOREPLACE)` is not supported; `os.link` fails when the target exists, so it works as create-if-absent across nodes (claims) | — |
| Login nodes (`miyabi-g*`) | file edits, light metadata, PBS operations, `uv` venv sync from wheels. Not: `import torch`, test suites, compiles, `du`/`find` over large trees | `miyabi-pbs-cmd` `context.py` |
| Model files | jobs do not download: fill a shared `HF_HOME` on /work from the login node, pin revisions, set `HF_HUB_OFFLINE=1` in jobs | — |
| GPU telemetry | `qstat -H -f` keeps per-node average and maximum GPU utilization, memory and power for finished jobs. Utilization and power seem to cover only about the **last hour** of a job (inferred 2026-10-20: in 11-node job 3585013, eight client GPUs that trained for the first 29 of 112 min, at 31 GB, record maximum utilization 0); memory peaks cover the whole job | `scripts/pbs_time_report.py` summarizes them; for longer jobs, measure idle time from task timelines |

## 2 Queues (`qstat --rsc -x`)

| Queue | Nodes | Max walltime | Use |
| --- | --- | --- | --- |
| `debug-g` | 1–16 | 30 min | smokes, release tests, short tasks; usually starts within minutes (G1-1 median wait 0.25 h; 2026-10-16 afternoon 36 min with an 80 min prediction at worst) |
| `short-g` | 1–8 | 8 h | avoid: one-node jobs waited more than 7 h |
| `regular-g` → `small-g` / `medium-g` / `large-g` / `x-large-g` | 1–16 / 17–64 / 65–128 / 129–256 | 48 h (x-large 24 h) | submit to `regular-g`; the site routes by node count |
| `interact-g_n1` / `interact-g_n8` | 1 / 2–8 | 2 h / 10 min | interactive debugging |
| `debug-mig` / `short-mig` / `regular-mig` | one MIG instance (1/2/4 slices) | 30 min / 8 h / 48 h | 25 GiB memory; candidate for tiny tests (unverified) |
| `prepost` | 1 | 6 h | 240 GiB memory; candidate for pre/post-processing (unverified) |

A debug-g job that is not clearly done in ~20 minutes belongs elsewhere: 30-min
debug-g jobs sized without margin all timed out (2026-10-14).

## 3 Project limits (`qstat --limit`)

Miyabi-G, per project: 128 accepted jobs, 16 running jobs, 256 nodes (plus 24 MIG).

- The running limit counts **jobs**, not nodes. One-node jobs alone can use at
  most 16 GPUs: pack single-GPU work (evaluations, central training,
  generation, probes) into multi-node pilots, or run it inside the allocation
  that produced its input. For long tasks the choice between one packed wave
  and several 1-node waves is a trade ([architecture.md](architecture.md) §9).
- Every experiment of the project shares the limits. Read them each cycle and
  give each experiment a share (`max_pilots`, `--max-queued`); never hard-code
  them, and never cap yourself far below them without a reason (G1-1 once ran
  its search with a self-imposed 15-job cap).
- Keep a few accepted slots free (fedcl: 4) for bad-node resubmissions and
  release tests.

## 4 Measured queue and run times

| Period and source | Shape | Run | Queue wait | Used / requested walltime |
| --- | --- | --- | --- | --- |
| G1-1, 855 jobs, 2026-10-13 → 16, guessed walltime | 11 nodes | median 16 min | median 3.6 h, p90 10.4 h, max 24.5 h | median 0.17 (p10 0.07, p90 0.58) |
| same | 1 node, short-g / small-g | — | median 2.8 h / 2.9 h (p90 5.1 / 8.0 h) | — |
| same | debug-g | — | median 0.25 h | — |
| exp-v2 pilots, 2026-10-18 → 20, walltime 1.5× measured (`pbs_time_report.py --hday 3 --name fedcl-n`) | 11 nodes regular-g (31 jobs) | median 1.4 h | median 0.85 h, p90 2.5 h, max 3.4 h | median 0.62 |
| same | 11 nodes debug-g (26) | median 0.25 h | median 0.21 h | median 0.49 |
| same | 1 node regular-g (39) | median 0.88 h | median 0.48 h, p90 1.7 h | median 0.52 |
| same | 1 node debug-g (62) | — | median < 1 min, p90 0.13 h | — |

PBS GPU utilization in the same exp-v2 window, weighted by node time: 11-node
regular-g jobs 24.5%, 1-node regular-g jobs 58.7%. These figures describe
roughly each job's last hour (§1), when 11-node jobs were mostly running
serial single-node steps; task timelines put the idle share of the h0004d
root's formal 11-node node·h at ~35%.

Design premise: on large shapes queue wait is comparable to or far larger than
run time, and it grows with the walltime you request. Measure your own window
before planning a schedule:

```bash
/usr/bin/python3 -I "$SKILL_ROOT/scripts/pbs_time_report.py" --hday 7 --name <prefix> --text
```

## 5 Pitfalls and fixes

| Symptom | Fix |
| --- | --- |
| `qstat <id>` exits 0 after the job has ended ("No unfinished job found."), so wait loops never end | judge liveness from the job table: `qstat -f` for current jobs, `qstat -H -f` for history, or `miyabi-pbs-cmd`'s `qstat_json.py`; grep for the ID |
| A bad node: mg0882 gave CUDA illegal memory access five times and wasted 11-node runs | before claiming work, check the whole `PBS_NODEFILE`; on a hit, resubmit the same script and resources once, write a receipt, exit 79; never count it as a scientific failure |
| PBS writes `-j oe` output to the `qsub` working directory; a job submitted from `/tmp` (an agent scratchpad) lost its output | submit from `<R>/control/pbs/`, or pass `-o` with a /work path |
| PyTorch Inductor on aarch64 needs a compiler that builds SVE2 kernels | GCC toolset 14 (`/opt/rh/gcc-toolset-14/root/usr/bin/{gcc,g++}`) as `CC`/`CXX`, with its `lib64` and `lib` on `LD_LIBRARY_PATH` and `LIBRARY_PATH` |
| transformers silently falls back to a Torch reference when an optional kernel (`causal-conv1d`, `flash-linear-attention`) fails to import — one bad node mixes implementations within a deployment | import the kernel package explicitly at model load; fail the task when it is missing |
| No aarch64 + CUDA 13.2 wheel on PyPI for some CUDA extensions; `pip install` compiles on the login node | build the wheel once in a compute job for the node's architecture only (`sm_90`), keep it in a /work wheel directory with its SHA-256, point `pyproject.toml`/`uv.lock` at it. Missing `libctf.so` at link time means GCC toolset 14's `lib64` is not on `LD_LIBRARY_PATH` |
| `mpirun` does not forward the caller's environment: remote ranks failed with `No module named …` (or would import the wrong code) | put every variable the remote process needs on the command line (`mpirun … /usr/bin/env K=V … python …`), `PYTHONPATH` included |
| Files written by remote ranks get mode 600 instead of 640 | set `umask` in the remote command when the group must read them |
| `qstat` prints a scheduled-stop banner (2026-10-28 09:00 at the time of writing) | jobs must end before it, or accept being killed and rerun from scratch |
| A compile cache per process: ~26,000 files for one 11-node deployment | caches on `/local`; shared archives on /work ([artifacts-and-storage.md](artifacts-and-storage.md) §5) |
