# Source-review finding checks

Two AI-assisted source reviews were written against commit `75724ad`:

- `edb-ansible-production-readiness-review.md` v5.0 — findings `EDB-01`…`EDB-18`
- `postgres17-cis-cluster-readiness-review.md` v5.0 — findings `CIS-01`…`CIS-17`

Both are governed by `postgresql-automation-review-shared-governance.md` v2.0.

This directory answers one question mechanically, on demand: **for each
finding, is the defect still reachable in the tree as it stands now?**

```shell
python3 tests/findings/check_findings.py            # human-readable report
python3 tests/findings/check_findings.py --check    # CI mode: fail on drift
python3 tests/findings/check_findings.py --json     # full evidence, machine readable
python3 tests/findings/check_findings.py --update   # re-record the baseline
```

It needs only `python3` — no pip, no containers, no cluster — and runs in a few
seconds, which is why it is in the per-push CI job.

## How it is meant to be used

`baseline.json` records the known state of every finding. `--check` compares
the tree against it and exits non-zero if anything moved. That makes two
different events loud:

- a **regression** — a finding recorded as `FIXED` is reachable again,
- an **unrecorded fix** — a finding improved but nobody updated the baseline.

When you deliberately fix a finding, refresh the baseline in the same commit:

```shell
python3 tests/findings/check_findings.py --update
```

The diff on `baseline.json` then shows, in the review, exactly which reviewed
defect the change closed.

## Statuses

| Status | Meaning |
|---|---|
| `PRESENT` | The reviewed defect is still reachable in this tree. |
| `PARTIAL` | Materially improved, but not closed. The summary says what remains. |
| `FIXED` | The defect is gone. |
| `NA` | The finding does not apply to this tree. |

Every check quotes the source it judged, as `path:line: code`, so a verdict can
be confirmed without rerunning anything.

## What this cannot tell you

This is static analysis of the collection's source. It proves what the code
says, not what a running cluster does. Three limits are worth stating plainly:

1. **Behaviour needs a cluster.** That a template contains
   `archive_command = '/bin/true'` is provable here; that a promoted standby
   then silently discards WAL is not. The live half lives in
   `tests/cases/prod_topology/` and `tests/tests/test_prod_topology.py`.
2. **The CIS findings are out of scope.** That role lives in a separate
   repository (`linefeedse/POSTGRES-17-CIS`) and is not vendored here, so
   `CIS-01`…`CIS-17` cannot be checked from this tree at all. The reviews'
   ownership matrix still applies to how the two interact.
3. **A check can be wrong.** A check that raises is reported as `PRESENT`
   rather than passing silently, so a broken check fails closed — but a check
   whose pattern is merely too narrow will quietly under-report. The quoted
   evidence is there so that can be spotted.

## Findings not from the reviews

Two IDs are local to this repository:

- `LOCAL-01` — the HAProxy health probe connects as
  `postgresql_cluster_xinetd_group`, a variable defined nowhere. It works only
  because its inline default matches the default of
  `postgresql_cluster_xinetd_user`, the variable that actually creates the
  role, so setting that variable silently breaks the probe.
- `TLS-01`…`TLS-03` — the gap between the assumed deployment (certbot with a
  local ACME server issuing client *and* server certificates) and what the
  collection provides.

## Layout

| File | Purpose |
|---|---|
| `check_findings.py` | Runner, reporting and baseline comparison. |
| `harness.py` | Repo accessors and the `Result` type. No third-party imports. |
| `checks_replication.py` | EDB-03, 04, 05, 06, 08, 14, 17, 18. |
| `checks_topology.py` | EDB-01, 02, 07, 13 and LOCAL-01. |
| `checks_lifecycle.py` | EDB-09, 10, 11, 12, 15, 16. |
| `checks_tls.py` | TLS-01, 02, 03. |
| `baseline.json` | Recorded state of every finding. |

To add a check: write a function taking a `Repo` and returning a `Result`, add
it to that module's `CHECKS` tuple, then run `--update`.
