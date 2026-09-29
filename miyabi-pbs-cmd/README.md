# Miyabi PBS Commands

Start with [SKILL.md](SKILL.md) for operations and reference routing. The skill
contains two Python standard-library helpers. Shell examples and their tests live
in [miyabi-pbs-shell-template](../miyabi-pbs-shell-template/SKILL.md).

## Validation

From this directory, run the small offline regressions with host Python 3.9+:

```bash
/usr/bin/python3 -I -B -m unittest discover -s tests -v
```

Tests cover context refusal, job-table parsing and CLI failures.
The anonymized history fixture preserves observed Miyabi table spacing. Queued
rows with a parenthesized token and finished rows that never started use
anonymized observed shapes; array, MIG and malformed-output cases are synthetic.

On 2026-09-05, current/history JSON queries, module JSON and queue discovery were
checked on `miyabi-g1`. On 2026-09-29, active queued jobs and a 31-day,
1185-row history parsed without errors there. Command syntax was checked against
the installed `/usr/local/share/man/man1/qstat.1` and `qsub.1`. These checks do
not validate GPU execution, C allocations or distributed runtime; see
[template validation scope](../miyabi-pbs-shell-template/references/templates.md#validation-scope).

`miyabi-pbs-goal-wakeup/scripts/qstat_json.py` is a bundled copy of this
skill's helper. Keep the two files byte-identical after any change.
