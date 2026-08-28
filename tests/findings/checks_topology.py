"""
Finding checks for failover authority, the HAProxy health gate, topology
reconciliation and node-class scope.

Covers EDB-01, EDB-02, EDB-07, EDB-13, plus LOCAL-01, a defect found while
verifying EDB-02 that is not in either source review.
"""

import re

from harness import PRESENT, FIXED, PARTIAL, Result, quote, missing


def check_edb_01(repo):
    """
    EDB-01: automatic repmgr failover is unsafe without fencing. The target
    architecture has no mechanism that proves the former primary cannot still
    accept writes, so the default must be manual and `automatic` must be
    rejected unless a tested fencing mechanism is configured.
    """
    defaults = 'roles/setup_repmgr/defaults/main.yml'
    evidence = []

    default_hits = repo.find(defaults, r'^repmgr_failover:')
    for n, line in default_hits:
        evidence.append(quote(defaults, n, line))

    # Everywhere the mode is chosen.
    everywhere = repo.grep(r'repmgr_failover',
                           subdirs=('roles', 'playbook-examples'),
                           suffixes=('.yml', '.j2'))
    for rel, n, line in everywhere:
        if rel != defaults:
            evidence.append(quote(rel, n, line))

    # Is there any guard that refuses automatic without fencing?
    guard = repo.grep(r"repmgr_failover.*==.*'?automatic",
                      subdirs=('roles/setup_repmgr/tasks',),
                      suffixes=('.yml',))
    for rel, n, line in guard:
        evidence.append(quote(rel, n, line))

    is_automatic = any(re.search(r':\s*["\']?automatic', line)
                       for _, line in default_hits)

    if not is_automatic and guard:
        return Result('EDB-01', FIXED,
                      'failover defaults to manual and automatic is guarded',
                      evidence)

    if not is_automatic:
        return Result('EDB-01', PARTIAL,
                      'failover no longer defaults to automatic, but nothing '
                      'rejects automatic when no fencing is configured',
                      evidence)

    return Result(
        'EDB-01', PRESENT,
        'repmgr_failover defaults to automatic and no assertion rejects it in '
        'the absence of a tested fencing mechanism', evidence)


def check_edb_02(repo):
    """
    EDB-02: the HAProxy health endpoint must fail closed. As written it
    discards SQL errors into `wc -l`, so a failing slot query yields a count
    of zero, which is indistinguishable from the legitimate "no slots"
    case and returns HTTP 200 "primary".
    """
    tmpl = 'roles/postgres-cluster-xinetd/templates/pgsqlchck.j2'
    evidence = []

    if repo.read(tmpl) is None:
        return Result('EDB-02', FIXED,
                      'the health-check template no longer exists',
                      ['%s: file absent' % tmpl])

    # The decisive pattern: stderr discarded, then piped into a line count.
    swallowed = repo.find(tmpl, r'2>\s*/dev/null\s*\|\s*wc -l')
    for n, line in swallowed:
        evidence.append(quote(tmpl, n, line))

    # The branch that turns a zero count into a success.
    branch = repo.find(tmpl, r'NUMSLOTS.*-lt 1')
    for n, line in branch:
        evidence.append(quote(tmpl, n, line))

    # How many psql calls one probe can make.
    psql_calls = repo.find(tmpl, r'\bpsql\b')
    evidence.append('%s: %d psql invocation(s) in the probe script'
                    % (tmpl, len(psql_calls)))

    if swallowed and branch:
        return Result(
            'EDB-02', PRESENT,
            'a failed slot query is counted as zero slots and falls into the '
            'HTTP 200 "primary" branch, so the endpoint fails open', evidence)

    if not swallowed:
        return Result('EDB-02', PARTIAL,
                      'query errors are no longer discarded into wc -l; '
                      'confirm the endpoint now fails closed', evidence)

    return Result('EDB-02', PARTIAL,
                  'the error-swallowing pattern is present but the success '
                  'branch has changed; re-review the probe logic', evidence)


def check_local_01(repo):
    """
    LOCAL-01 (not in either source review): the health-check probe connects as
    `postgresql_cluster_xinetd_group`, a variable defined nowhere in the
    collection. It only works because its inline default happens to match the
    default of `postgresql_cluster_xinetd_user`, which is the variable that
    actually creates the role. Setting `postgresql_cluster_xinetd_user`
    silently breaks the probe.
    """
    role = 'roles/postgres-cluster-xinetd'
    evidence = []

    used = repo.grep(r'postgresql_cluster_xinetd_group',
                     subdirs=(role,), suffixes=('.j2', '.yml'))
    defined = repo.grep(r'^\s*postgresql_cluster_xinetd_group\s*:',
                        subdirs=(role,), suffixes=('.yml',))
    user_var = repo.grep(r'postgresql_cluster_xinetd_user',
                         subdirs=(role,), suffixes=('.j2', '.yml'))

    for rel, n, line in used:
        evidence.append(quote(rel, n, line))
    for rel, n, line in user_var:
        evidence.append(quote(rel, n, line))

    if not used:
        return Result('LOCAL-01', FIXED,
                      'the probe no longer references an undefined group '
                      'variable', [missing('postgresql_cluster_xinetd_group')])

    if defined:
        for rel, n, line in defined:
            evidence.append(quote(rel, n, line))
        return Result('LOCAL-01', FIXED,
                      'postgresql_cluster_xinetd_group is defined', evidence)

    evidence.append(missing('a definition of postgresql_cluster_xinetd_group'))
    return Result(
        'LOCAL-01', PRESENT,
        'the probe connects as postgresql_cluster_xinetd_group, which is never '
        'defined; it survives only because its inline default matches the '
        'default of postgresql_cluster_xinetd_user, the variable that actually '
        'creates the role', evidence)


def check_edb_07(repo):
    """
    EDB-07: node registration uses `repmgr ... register -F` (force) with a role
    derived purely from static inventory groups. After a failover the inventory
    describes the former topology, so a rerun can force-register the wrong node
    and overwrite the live repmgr.nodes row.
    """
    register = 'roles/setup_repmgr/tasks/repmgr_register_node.yml'
    evidence = []

    forced = repo.find(register, r'register -F')
    for n, line in forced:
        evidence.append(quote(register, n, line))

    # Role selection is inventory-derived.
    group_gate = repo.grep(r"'(primary|standby|witness)' in group_names",
                           subdirs=('roles/setup_repmgr/tasks',),
                           suffixes=('.yml',))
    for rel, n, line in group_gate[:4]:
        evidence.append(quote(rel, n, line))

    # Is there any runtime topology probe before the mutation?
    probe = repo.grep(r'pg_is_in_recovery|system_identifier|pg_control_system',
                      subdirs=('roles/setup_repmgr',),
                      suffixes=('.yml', '.j2'))
    for rel, n, line in probe:
        evidence.append(quote(rel, n, line))

    if not forced:
        return Result('EDB-07', FIXED,
                      'registration no longer forces with -F', evidence)

    if probe:
        return Result('EDB-07', PARTIAL,
                      'registration still forces with -F but a runtime '
                      'topology probe now exists', evidence)

    evidence.append(missing(
        'a runtime pg_is_in_recovery()/system-identifier/timeline check under '
        'roles/setup_repmgr'))
    return Result(
        'EDB-07', PRESENT,
        'nodes are force-registered (-F) with a role taken from static '
        'inventory groups, with no runtime topology validation gating the '
        'mutation', evidence)


def check_edb_13(repo):
    """
    EDB-13: node-class applicability.

    The review states there is no applicability model. That part does not hold:
    `supported_roles` maps inventory group to permitted roles, and witness is a
    supported node class. The reachable defect is narrower and concrete --
    example playbooks run some roles under `hosts: all` with no
    `supported_roles` gate, so those roles execute on every inventory host.
    """
    evidence = []

    # Roles invoked in example playbooks without a supported_roles gate.
    playbooks = [rel for rel in repo.walk(('playbook-examples',), ('.yml',))
                 if 'inventory' not in rel]
    playbooks += ['playbook.yml', 'playbook_patroni_cluster.yml']

    ungated = []
    for rel in playbooks:
        lines = repo.lines(rel)
        for n, line in enumerate(lines, start=1):
            m = re.match(r'\s*-\s*role:\s*([\w\-]+)', line)
            if not m:
                continue
            # A gate may appear on the following few lines.
            window = ' '.join(lines[n:n + 3])
            if 'supported_roles' not in window:
                ungated.append((rel, n, line.rstrip(), m.group(1)))

    for rel, n, line, _role in ungated:
        evidence.append(quote(rel, n, line))

    # Confirm the applicability model does exist, so the report is accurate.
    model = repo.grep(r"'witness'", subdirs=('plugins/lookup',),
                      suffixes=('.py',))
    for rel, n, line in model[:2]:
        evidence.append(quote(rel, n, line))

    if not ungated:
        return Result('EDB-13', FIXED,
                      'every role invocation in the shipped playbooks is gated '
                      'by supported_roles', evidence)

    roles = sorted({r for _, _, _, r in ungated})
    return Result(
        'EDB-13', PARTIAL,
        'an applicability model exists (supported_roles maps inventory group '
        'to permitted roles, and witness is a supported class), but %d role '
        'invocation(s) in the shipped playbooks run under broad host patterns '
        'with no gate: %s' % (len(ungated), ', '.join(roles)),
        evidence)


CHECKS = (
    check_edb_01,
    check_edb_02,
    check_local_01,
    check_edb_07,
    check_edb_13,
)
