# Tests

Plain-Python test scripts with no third-party dependencies — they run with the same
interpreter the collectors use, and need no test framework installed.

## Running

```bash
python tests/run_all.py           # all test files, quiet unless something fails
python tests/run_all.py -v        # stream each file's output
python tests/test_work_hours.py   # one file on its own, for iterating
```

`run_all.py` runs each file in a separate interpreter, so leaked matplotlib state
or a hard crash in one file cannot affect another. It exits non-zero when anything
fails, which makes it usable as a pre-commit or CI step.

Assertions are reported rather than raised, so one run shows every failure instead
of stopping at the first.

## What each file covers

| File | Covers |
| ---- | ------ |
| `test_opsgenie_attribution.py` | Who gets credited for acknowledging, closing, and escalating an alert. Includes the case the alert record alone gets wrong: one person acknowledges, another closes. Both escalation directions. |
| `test_opsgenie_permissions.py` | Behaviour with a real read-only API key — denied on `/v2/account` and `/v2/users`, allowed on alerts. Preflight target, degradation to username matching, API URL normalization, unreachable host. |
| `test_work_hours.py` | Out-of-hours bucketing in each member's local timezone, the configurable business-hours window with minute precision, DST, weekend precedence, and rejection of malformed settings. |
| `test_dashboard_layout.py` | The self-sizing chart grid: which panel earns the full-width row, and that the layout has no holes or overlaps for 2–12 panels. |
| `test_report_generation.py` | End-to-end markdown, PNG, and CSV output across source combinations: everything present, OpsGenie absent, OpsGenie errored, ZenDesk empty, CSAT present/absent, nothing at all. |

`_support.py` holds the shared result reporter and the metrics fixture. It is not a
test file and `run_all.py` skips it.

## Live smoke test

`smoke_opsgenie.py` is **not** part of the automated suite — it talks to the real
OpsGenie API and needs credentials:

```bash
export OPSGENIE_API_KEY=...
python tests/smoke_opsgenie.py 7
```

It bypasses `validate_config()`, so no Gitea/Jenkins/ArgoCD credentials are
required. Use it when first configuring OpsGenie or when a report's numbers look
wrong: it separates the preflight, identity resolution, and attribution stages so
you can see which one is failing rather than inferring it from a run's logs.

## Notes for adding tests

- Start a file with `import _support` before importing anything from the project.
  That sets `sys.path` and forces the headless matplotlib backend.
- Build fixtures with `_support.sample_metrics()` rather than hand-rolling a
  metrics dict, so a schema change is fixed in one place.
- Name new files `test_*.py` to have `run_all.py` find them.
- These run their assertions at import time and exit with a status code, so they
  are driven by `run_all.py` rather than by pytest. Adopting pytest later would
  mean wrapping each section in a `test_*` function and dropping the `r.exit()`
  calls — worth doing if the suite grows, but it would add a dev dependency to a
  project that currently has none.
