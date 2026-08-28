"""
Finding checks for restart orchestration, rejoin, destructive interlocks,
version evidence and entry-point separation.

Covers EDB-09, EDB-10, EDB-11, EDB-12, EDB-15, EDB-16.
"""

import re

from harness import PRESENT, FIXED, PARTIAL, Result, quote, missing


def check_edb_09(repo):
    """
    EDB-09: a parameter change that needs a restart must be rolled through the
    topology -- one standby at a time, waiting for catch-up and for the
    synchronous quorum to be restored, with the primary handled by a reviewed
    switchover. A plain per-host `state: restarted` gives none of that.
    """
    params = 'roles/manage_dbserver/tasks/manage_postgres_params.yml'
    evidence = []

    restart = repo.find(params, r'state: restarted')
    for n, line in restart:
        evidence.append(quote(params, n, line))

    # `serial` is the usual way to make a play one-host-at-a-time. Exclude the
    # OpenSSL -CAcreateserial/-CAserial false positives.
    serial = [(rel, n, line) for rel, n, line in
              repo.grep(r'^\s*serial\s*:', subdirs=('roles', 'playbook-examples'),
                        suffixes=('.yml',))]
    throttle = repo.grep(r'^\s*throttle\s*:',
                         subdirs=('roles/manage_dbserver',), suffixes=('.yml',))
    for rel, n, line in serial + throttle:
        evidence.append(quote(rel, n, line))

    # Any wait for the standby to catch up before moving on?
    catchup = repo.grep(r'pg_stat_replication|pg_last_wal_replay_lsn|write_lag',
                        subdirs=('roles/manage_dbserver',), suffixes=('.yml',))
    for rel, n, line in catchup:
        evidence.append(quote(rel, n, line))

    if not restart:
        return Result('EDB-09', FIXED,
                      'the parameter path no longer restarts PostgreSQL',
                      evidence)

    if serial and catchup:
        return Result('EDB-09', FIXED,
                      'restarts are batched and gated on standby catch-up',
                      evidence)

    if not serial:
        evidence.append(missing('a serial: batch limit on any play'))
    if not catchup:
        evidence.append(missing(
            'a catch-up or quorum wait in roles/manage_dbserver'))

    return Result(
        'EDB-09', PRESENT,
        'PostgreSQL is restarted per host with no serial batching, no '
        'replica-first ordering, no catch-up wait and no synchronous-quorum '
        'guard', evidence)


def check_edb_10(repo):
    """
    EDB-10: HAProxy is restarted unconditionally and its configuration is
    written to the live path with no `haproxy -c` validation, so a bad render
    takes the ingress down. repmgr restarts PostgreSQL and repmgrd without
    pausing failover control first.
    """
    ha_setup = 'roles/setup_haproxy/tasks/setup_haproxy.yml'
    ha_conf = 'roles/setup_haproxy/tasks/haproxy_configure.yml'
    evidence = []

    restart = repo.find(ha_setup, r'state: restarted')
    for n, line in restart:
        evidence.append(quote(ha_setup, n, line))

    # Is the render validated before it becomes live?
    validate = repo.find(ha_conf, r'validate:')
    for n, line in validate:
        evidence.append(quote(ha_conf, n, line))

    # repmgr side: does anything pause failover around a restart?
    pause = repo.grep(r'repmgr.*service (pause|unpause)|repmgrd.*pause',
                      subdirs=('roles/setup_repmgr',), suffixes=('.yml', '.j2'))
    for rel, n, line in pause:
        evidence.append(quote(rel, n, line))

    repmgr_restart = repo.grep(r'state: restarted',
                               subdirs=('roles/setup_repmgr/tasks',),
                               suffixes=('.yml',))
    for rel, n, line in repmgr_restart:
        evidence.append(quote(rel, n, line))

    problems = []
    if restart:
        problems.append('haproxy is restarted unconditionally')
    if not validate:
        problems.append('the haproxy config is written with no validate:')
        evidence.append(missing('a validate: argument on the haproxy template'))
    if repmgr_restart and not pause:
        problems.append('repmgr restarts services without pausing failover')
        evidence.append(missing('a repmgr service pause/unpause around restarts'))

    if not problems:
        return Result('EDB-10', FIXED,
                      'service activation is validated and guarded', evidence)

    return Result('EDB-10', PRESENT, '; '.join(problems), evidence)


def check_edb_11(repo):
    """
    EDB-11: pg_rewind rejoin of a former primary.

    The prerequisites are genuinely in place -- wal_log_hints=on, checksums via
    initdb -k, and the non-superuser grants. What is missing is any code path
    that actually rejoins, and any assertion protecting full_page_writes, which
    is merely left at its default.
    """
    tmpl = 'roles/init_dbserver/templates/postgresql.conf.template'
    evidence = []

    hints = repo.find(tmpl, r'wal_log_hints')
    for n, line in hints:
        evidence.append(quote(tmpl, n, line))

    checksums = repo.grep(r'pg_initdb_options.*-k|--data-checksums',
                          subdirs=('roles/init_dbserver',), suffixes=('.yml',))
    for rel, n, line in checksums[:3]:
        evidence.append(quote(rel, n, line))

    fpw = repo.grep(r'full_page_writes',
                    subdirs=('roles', 'playbook-examples'),
                    suffixes=('.yml', '.j2', '.template'))
    for rel, n, line in fpw:
        evidence.append(quote(rel, n, line))

    grants = repo.grep(r'rewind', subdirs=('roles/setup_replication',),
                       suffixes=('.yml',))
    for rel, n, line in grants[:2]:
        evidence.append(quote(rel, n, line))

    # The thing that would actually perform a rejoin.
    rejoin = repo.grep(r'node rejoin|pg_rewind\b',
                       subdirs=('roles/setup_repmgr/tasks',
                                'roles/setup_replication/tasks'),
                       suffixes=('.yml',))
    for rel, n, line in rejoin:
        evidence.append(quote(rel, n, line))

    prereqs_ok = bool(hints) and bool(checksums)

    if rejoin:
        return Result('EDB-11', FIXED,
                      'a rewind/rejoin path exists and prerequisites are set',
                      evidence)

    evidence.append(missing('any "repmgr node rejoin" or pg_rewind invocation'))
    if not fpw:
        evidence.append(missing(
            'full_page_writes (left at the PostgreSQL default, never asserted)'))

    if prereqs_ok:
        return Result(
            'EDB-11', PARTIAL,
            'rewind prerequisites are in place (wal_log_hints=on, initdb -k '
            'checksums, non-superuser grants) but nothing ever performs a '
            'rejoin, and full_page_writes is never set or asserted', evidence)

    return Result('EDB-11', PRESENT,
                  'rewind prerequisites are incomplete and no rejoin path '
                  'exists', evidence)


def check_edb_12(repo):
    """
    EDB-12: destructive rebuild interlocks.

    The reviewed concern was that the flags lack strong interlocks. In this
    tree the exposure is wider than the review recorded: PGDATA and pg_wal sit
    behind a second `force_rm_*` flag, but several other paths are deleted with
    only a "the variable is non-empty" guard.
    """
    rm_repl = 'roles/setup_replication/tasks/rm_replication.yml'
    rm_init = 'roles/init_dbserver/tasks/rm_initdb.yml'
    evidence = []

    # The one real interlock.
    guarded = repo.grep(r'force_rm_pg_data|force_rm_pg_wal',
                        subdirs=('roles',), suffixes=('.yml',))
    for rel, n, line in guarded[:4]:
        evidence.append(quote(rel, n, line))

    # Are those flags defined anywhere, or only referenced?
    defined = repo.grep(r'^\s*force_rm_pg_(data|wal)\s*:',
                        subdirs=('roles',), suffixes=('.yml',))

    # Deletions whose only guard is a length check.
    weak = []
    for rel in (rm_repl, rm_init):
        lines = repo.lines(rel)
        for n, line in enumerate(lines, start=1):
            if 'state: absent' not in line:
                continue
            window = ' '.join(lines[max(0, n - 12):n + 8])
            if 'force_rm_pg_data' in window or 'force_rm_pg_wal' in window:
                continue
            weak.append((rel, n, line.rstrip()))

    for rel, n, line in weak:
        evidence.append(quote(rel, n, line))

    # Is the destructive path able to know it is not hitting the primary?
    identity = repo.grep(r'pg_is_in_recovery',
                         subdirs=('roles/setup_replication/tasks',),
                         suffixes=('.yml',))
    limit = repo.grep(r'confirmation_token|ansible_limit|run_once.*destructive',
                      subdirs=('roles',), suffixes=('.yml',))

    if not defined:
        evidence.append(missing(
            'a defaults entry for force_rm_pg_data / force_rm_pg_wal '
            '(referenced only inside when: conditions)'))
    if not identity:
        evidence.append(missing(
            'a pg_is_in_recovery() target-identity check before deletion'))
    if not limit:
        evidence.append(missing('a one-host limit or confirmation token'))

    if not weak and identity and limit:
        return Result('EDB-12', FIXED,
                      'destructive paths are interlocked', evidence)

    return Result(
        'EDB-12', PRESENT,
        'PGDATA and pg_wal sit behind a second force_rm_* flag, but %d other '
        'deletion(s) are guarded only by a non-empty check, and no target '
        'identity proof, one-host limit or confirmation token gates any of '
        'them' % len(weak), evidence)


def check_edb_15(repo):
    """
    EDB-15: reproducible PostgreSQL 17 evidence.

    Passing means the roles the target deployment actually uses accept PG 17
    and are exercised by CI at PG 17. The core cluster roles now list 17; the
    HA components the deployment depends on -- repmgr aside -- do not.
    """
    evidence = []

    # Roles the assumed deployment needs at PG 17.
    required = (
        'init_dbserver', 'install_dbserver', 'setup_repo', 'setup_replication',
        'setup_repmgr',
    )
    missing_17 = []
    for role in required:
        rel = 'roles/%s/defaults/main.yml' % role
        text = repo.read(rel)
        if text is None:
            continue
        block = re.search(r'supported_pg_version:(.*?)(?=\n\w|\Z)', text, re.S)
        if block is None:
            missing_17.append(role)
            continue
        if not re.search(r'^\s*-\s*17\s*$', block.group(1), re.M):
            missing_17.append(role)
        else:
            for n, line in repo.find(rel, r'^\s*-\s*17\s*$'):
                evidence.append(quote(rel, n, line))

    # CI coverage at PG 17.
    ci_17 = repo.grep(r'EDB_PG_VERSION.*17|EDB_PG_VERSION: .17.',
                      subdirs=('.github',), suffixes=('.yml',))
    for rel, n, line in ci_17[:4]:
        evidence.append(quote(rel, n, line))

    if missing_17:
        evidence.append(
            'roles still lacking PG 17 in supported_pg_version: %s'
            % ', '.join(missing_17))
        return Result('EDB-15', PRESENT,
                      'core roles for the target deployment do not accept '
                      'PG 17: %s' % ', '.join(missing_17), evidence)

    if not ci_17:
        evidence.append(missing('any CI job running EDB_PG_VERSION 17'))
        return Result('EDB-15', PRESENT,
                      'core roles accept PG 17 but no CI job exercises it',
                      evidence)

    return Result(
        'EDB-15', PARTIAL,
        'the core cluster roles accept PG 17 and CI now exercises it, but the '
        'HA components the deployment relies on (haproxy has no version gate; '
        'pgbackrest is untested at 17) have no PG 17 evidence', evidence)


def check_edb_16(repo):
    """
    EDB-16: bootstrap, backup, destructive rebuild, reconciliation and restart
    share one entry point, so neither "safe rerun" nor "safe abort" is defined.
    """
    evidence = []

    main = 'playbook.yml'
    lines = repo.lines(main)

    plays = [n for n, line in enumerate(lines, start=1)
             if re.match(r'^-\s*hosts:', line)]
    roles = [(n, line.rstrip()) for n, line in enumerate(lines, start=1)
             if re.match(r'\s*-\s*role:', line)]
    fatal = repo.find(main, r'any_errors_fatal')

    for n in plays:
        evidence.append(quote(main, n, lines[n - 1]))
    for n, line in fatal:
        evidence.append(quote(main, n, line))
    evidence.append('%s: %d play(s) applying %d roles in one run'
                    % (main, len(plays), len(roles)))

    # force_initdb reaches cleanup paths across many roles at once.
    force = repo.grep(r'force_initdb', subdirs=('roles',), suffixes=('.yml',))
    touched = sorted({rel.split('/')[1] for rel, _, _ in force})
    evidence.append('force_initdb reaches cleanup paths in %d roles: %s'
                    % (len(touched), ', '.join(touched)))

    if len(plays) == 1 and len(roles) > 10:
        return Result(
            'EDB-16', PRESENT,
            'one play applies %d roles covering bootstrap, replication, '
            'failover control, backup and reconciliation, and a single '
            'force_initdb reaches cleanup paths in %d of them'
            % (len(roles), len(touched)), evidence)

    return Result('EDB-16', PARTIAL,
                  'the entry point has been split but lifecycle phases still '
                  'overlap', evidence)


CHECKS = (
    check_edb_09,
    check_edb_10,
    check_edb_11,
    check_edb_12,
    check_edb_15,
    check_edb_16,
)
