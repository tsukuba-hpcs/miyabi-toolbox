# Miyabi Development

Start with [SKILL.md](SKILL.md) for operations and reference routing. The skill
contains two Python standard-library helpers and three adaptable PBS templates.

## Validation

From this directory, run the small offline regressions with host Python 3.9+:

```bash
/usr/bin/python3 -I -B -m unittest discover -s tests -v
```

Tests cover context refusal, job-table parsing, CLI failures, and simulated PBS
launcher boundaries, including interpreter identity and worker exit propagation.
The anonymized history fixture preserves observed Miyabi table spacing; array,
MIG and malformed-output cases are synthetic.

On 2026-09-05, current/history JSON queries, module JSON and queue discovery were
checked on `miyabi-g1`. Command syntax was checked against the installed
`/usr/local/share/man/man1/qstat.1` and `qsub.1`. These checks do not validate GPU
execution, C allocations or distributed runtime; see
[template validation scope](references/templates.md#validation-scope).
