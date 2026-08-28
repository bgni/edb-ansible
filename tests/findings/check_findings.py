#!/usr/bin/env python3
"""
Check the working tree against the published source-review findings.

Two source reviews (`edb-ansible-production-readiness-review.md` v5.0 and
`postgres17-cis-cluster-readiness-review.md` v5.0) were written against commit
75724ad. This script answers one question mechanically: for each finding, is
the defect still reachable in the tree as it stands now?

It is static analysis. It proves what the source says, not what a running
cluster does -- the live behaviour is covered by the deployment test case under
tests/cases/. Findings that can only be judged against a running cluster are
listed in UNCHECKABLE below rather than being silently omitted.

Usage:
    python3 tests/findings/check_findings.py              # report
    python3 tests/findings/check_findings.py --check      # fail on drift
    python3 tests/findings/check_findings.py --update     # rewrite baseline
    python3 tests/findings/check_findings.py --json       # machine readable

`--check` is the CI mode: it compares against baseline.json and exits non-zero
if any finding changed state. That makes a regression of a fixed finding, and
an unrecorded fix, both loud.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Repo, PRESENT, FIXED, PARTIAL, NA  # noqa: E402
import checks_replication  # noqa: E402
import checks_topology  # noqa: E402
import checks_lifecycle  # noqa: E402
import checks_tls  # noqa: E402


REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'baseline.json')

ALL_CHECKS = (
    checks_replication.CHECKS
    + checks_topology.CHECKS
    + checks_lifecycle.CHECKS
    + checks_tls.CHECKS
)

# Findings that static analysis cannot settle. Listed explicitly so the
# coverage arithmetic below stays honest: a finding is either checked here,
# or named here as needing the live cluster.
UNCHECKABLE = {
    'EDB-06-live': 'a restore/PITR must actually be performed and timed',
    'CIS-*': 'the CIS role lives in a separate repository '
             '(linefeedse/POSTGRES-17-CIS) and is not vendored here',
}

STATUS_ORDER = {PRESENT: 0, PARTIAL: 1, FIXED: 2, NA: 3}

# Evidence lines printed per finding in the human-readable report. The full
# list is always available through --json.
EVIDENCE_LIMIT = 8


def run_all():
    repo = Repo(REPO_ROOT)
    results = []
    for check in ALL_CHECKS:
        try:
            results.append(check(repo))
        except Exception as exc:  # a broken check must not look like a pass
            from harness import Result
            results.append(Result(
                getattr(check, '__name__', 'unknown').replace(
                    'check_', '').replace('_', '-').upper(),
                PRESENT,
                'the check itself failed: %s: %s' % (type(exc).__name__, exc)))
    results.sort(key=lambda r: (STATUS_ORDER[r.status], r.finding_id))
    return results


def load_baseline():
    try:
        with open(BASELINE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return None


def save_baseline(results):
    data = {
        '_comment': (
            'Known state of each source-review finding in this tree. '
            'Regenerate with: python3 tests/findings/check_findings.py '
            '--update. A diff here is a real change in the collection: '
            'either a finding was fixed, or a fix regressed.'),
        'findings': {r.finding_id: r.status for r in results},
    }
    with open(BASELINE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write('\n')


def print_report(results):
    width = max(len(r.finding_id) for r in results)
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    print()
    print('Source-review findings against the current working tree')
    print('=' * 72)
    print()

    for status in (PRESENT, PARTIAL, FIXED, NA):
        group = [r for r in results if r.status == status]
        if not group:
            continue
        print('%s (%d)' % (status, len(group)))
        print('-' * 72)
        for r in group:
            print('  %-*s  %s' % (width, r.finding_id, r.summary))
            shown = r.evidence[:EVIDENCE_LIMIT]
            for line in shown:
                print('  %-*s    %s' % (width, '', line))
            hidden = len(r.evidence) - len(shown)
            if hidden > 0:
                print('  %-*s    ... and %d more (see --json)'
                      % (width, '', hidden))
            print()
        print()

    print('=' * 72)
    summary = '  '.join('%s=%d' % (s, counts.get(s, 0))
                        for s in (PRESENT, PARTIAL, FIXED, NA))
    print('Totals: %s   (%d findings checked)' % (summary, len(results)))
    if UNCHECKABLE:
        print()
        print('Not decidable by static analysis:')
        for key, why in sorted(UNCHECKABLE.items()):
            print('  %-12s %s' % (key, why))
    print()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true',
                        help='compare against baseline.json and exit non-zero '
                             'on any drift')
    parser.add_argument('--update', action='store_true',
                        help='rewrite baseline.json from the current tree')
    parser.add_argument('--json', action='store_true',
                        help='emit machine-readable results')
    args = parser.parse_args()

    results = run_all()

    if args.json:
        print(json.dumps([{
            'id': r.finding_id,
            'status': r.status,
            'summary': r.summary,
            'evidence': r.evidence,
        } for r in results], indent=2))
        return 0

    if args.update:
        save_baseline(results)
        print('Wrote %s (%d findings)' % (BASELINE, len(results)))
        return 0

    print_report(results)

    if not args.check:
        return 0

    baseline = load_baseline()
    if baseline is None:
        print('No baseline found. Create one with --update.', file=sys.stderr)
        return 2

    expected = baseline.get('findings', {})
    actual = {r.finding_id: r.status for r in results}

    drift = []
    for fid in sorted(set(expected) | set(actual)):
        was = expected.get(fid, '(not in baseline)')
        now = actual.get(fid, '(check removed)')
        if was != now:
            drift.append((fid, was, now))

    if not drift:
        print('No drift from baseline: all %d findings are in their recorded '
              'state.' % len(actual))
        return 0

    print('FINDING DRIFT', file=sys.stderr)
    print('-' * 72, file=sys.stderr)
    for fid, was, now in drift:
        direction = ''
        if was in STATUS_ORDER and now in STATUS_ORDER:
            direction = (' (regression)'
                         if STATUS_ORDER[now] < STATUS_ORDER[was]
                         else ' (improvement)')
        print('  %-12s %s -> %s%s' % (fid, was, now, direction),
              file=sys.stderr)
    print('-' * 72, file=sys.stderr)
    print('If this change is intended, refresh the baseline with:',
          file=sys.stderr)
    print('  python3 tests/findings/check_findings.py --update',
          file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
