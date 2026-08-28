"""
Minimal check harness for the source-review finding checks.

The checks in `check_findings.py` have to run in the same places the rest of
this repository's tooling runs, including bare RHEL/Ubuntu images that have
python3 but no pip. So this deliberately depends on nothing outside the
standard library (PyYAML is used only by the checks that must parse Ansible
files, and is imported there, not here).
"""

import os
import re


# Repo-relative path of this checker, excluded from every tree search.
SELF_DIR = os.path.join('tests', 'findings')

# Files that name settings without configuring them. The sanity-check playbook
# queries pg_settings and reports on what it finds, so every setting it looks
# for appears in its source -- which made two findings read as FIXED when
# nothing had changed. Same failure as the checker matching its own source.
INSPECTION_ONLY = (
    os.path.join('playbook-examples', 'sanity-check.yml'),
)

# Where a PostgreSQL setting can actually be *configured*: role defaults, vars,
# templates and task files, the shipped playbooks, and a test case's vars.json.
#
# tests/tests is deliberately absent. Those modules assert behaviour and name
# settings in docstrings and assertion messages; treating a mention there as
# configuration makes a check report a defect as fixed because a test that
# describes the defect exists.
CONFIG_DIRS = ('roles', 'plugins', 'playbook-examples', 'tests/cases')

# A finding is in one of these states in the working tree.
PRESENT = 'PRESENT'    # the reviewed defect is still reachable in this tree
FIXED = 'FIXED'        # the reviewed defect is gone
PARTIAL = 'PARTIAL'    # materially improved but not closed
NA = 'NA'              # the finding does not apply to this tree

STATUSES = (PRESENT, FIXED, PARTIAL, NA)


class Result:
    """The outcome of one finding check."""

    def __init__(self, finding_id, status, summary, evidence=None):
        if status not in STATUSES:
            raise ValueError('bad status %r for %s' % (status, finding_id))
        self.finding_id = finding_id
        self.status = status
        self.summary = summary
        # Evidence is a list of 'path:line: quoted source' strings. Every
        # check must quote the code it judged, so a reader can confirm the
        # verdict without rerunning the checker.
        self.evidence = evidence or []


class Repo:
    """Read-only accessors for the collection under test."""

    def __init__(self, root):
        self.root = root
        self._cache = {}

    def path(self, rel):
        return os.path.join(self.root, rel)

    def exists(self, rel):
        return os.path.exists(self.path(rel))

    def read(self, rel):
        """Return file contents, or None when the file is absent."""
        if rel not in self._cache:
            try:
                with open(self.path(rel), 'r', encoding='utf-8') as f:
                    self._cache[rel] = f.read()
            except (IOError, OSError):
                self._cache[rel] = None
        return self._cache[rel]

    def lines(self, rel):
        text = self.read(rel)
        return text.splitlines() if text is not None else []

    def find(self, rel, pattern, flags=0):
        """
        Search one file for a regex.

        Returns a list of (line_number, line_text) for every matching line.
        """
        out = []
        for n, line in enumerate(self.lines(rel), start=1):
            if re.search(pattern, line, flags):
                out.append((n, line.rstrip()))
        return out

    def walk(self, subdirs, suffixes):
        """
        Yield repo-relative paths under `subdirs` with a matching suffix.

        The checker's own directory is skipped. Several checks search for a
        setting by name across the tree, and this file quotes those names in
        its patterns and docstrings -- without this exclusion a check would
        find its own source and report the defect as fixed.
        """
        for subdir in subdirs:
            base = self.path(subdir)
            if not os.path.isdir(base):
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames
                               if d not in ('.git', '__pycache__', 'findings')]
                for name in filenames:
                    if not name.endswith(tuple(suffixes)):
                        continue
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, self.root)
                    if rel.startswith(SELF_DIR) or rel in INSPECTION_ONLY:
                        continue
                    yield rel

    def grep(self, pattern, subdirs=('roles', 'plugins', 'playbook-examples'),
             suffixes=('.yml', '.yaml', '.j2', '.template', '.py'), flags=0):
        """
        Search the tree for a regex.

        Returns a list of (rel_path, line_number, line_text). This is the
        workhorse for 'does this setting appear anywhere at all' checks.
        """
        hits = []
        for rel in self.walk(subdirs, suffixes):
            for n, line in self.find(rel, pattern, flags):
                hits.append((rel, n, line))
        return sorted(hits)


def quote(rel, lineno, text, limit=140):
    """Format one evidence line."""
    text = text.strip()
    if len(text) > limit:
        text = text[:limit - 3] + '...'
    return '%s:%d: %s' % (rel, lineno, text)


def missing(what):
    """Format evidence for something that is absent tree-wide."""
    return 'no occurrence of %s anywhere in the collection' % what
