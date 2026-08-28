"""
Cluster lifecycle acceptance for the collection in this working tree.

Driven by tests/run-cluster-lifecycle.sh, which deploys one primary, three
standbys and a repmgr witness with the roles as they exist in this checkout.

Automatic failover is disabled in this deployment, so phase 3 promotes the way
an operator would: `repmgr standby promote`, then `repmgr standby follow` on
the survivors.

Three phases, each depending on the one before, selected by `-k`:

    lifecycle_provision     the playbook built a cluster that serves queries
    lifecycle_reconfigure   re-running it with changed input converged the
                            running cluster onto the new configuration
    lifecycle_failover      an acknowledged transaction survives losing the
                            primary

Phase 2 is the reason this exists. Deploying once with the final value proves
nothing about applying a change to a cluster that is already running; that is
the operation people actually perform, and the one that breaks.

Phase 3 tests the promotion *procedure* as much as the configuration. The
writer runs concurrently and the primary is SIGKILLed with transactions in
flight -- a test that finishes its writes and shuts down cleanly proves nothing
about data loss, because a graceful shutdown flushes everything by definition.

`ANY 1` guarantees each acknowledged commit reached at least one qualifying
standby, not that a particular survivor has it. The ledger is therefore checked
against whichever node is promoted, and a survivor missing any acknowledged
token is a failure of the procedure rather than something to tolerate.
"""

import re
import time

import pytest

from conftest import (
    get_hosts,
    get_named_hosts,
    get_pg_owner,
    get_pg_service_name,
    get_pg_unix_socket_dir,
    get_pg_version,
    load_ansible_vars,
)


SYNC_SPEC_RE = re.compile(
    r'^\s*(?P<method>ANY|FIRST)\s+(?P<num_sync>\d+)\s*\((?P<names>.*)\)\s*$',
    re.IGNORECASE)

LEDGER_TABLE = 'lifecycle_ledger'

# Generous: a reconfiguration may reload or restart, and a promotion is not
# instant. These bound a hang, they are not expected durations.
CONVERGE_TIMEOUT = 240
PROMOTE_TIMEOUT = 240


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def psql(host, query, database='postgres', timeout=None):
    command = 'psql -At -h %s -c "%s" %s' % (
        get_pg_unix_socket_dir(), query, database)
    if timeout is not None:
        command = 'timeout %d %s' % (timeout, command)
    with host.sudo(get_pg_owner()):
        return host.run(command)


def psql_output(host, query, database='postgres', timeout=60):
    """
    Always bounded. Under ANY N with no eligible standby connected a commit
    waits indefinitely, and an unbounded query would hang the suite instead of
    reporting the topology problem.
    """
    r = psql(host, query, database=database, timeout=timeout)
    assert r.rc == 0, 'query %r failed rc=%d: %s' % (
        query, r.rc, r.stderr.strip())
    return r.stdout.strip()


def show(host, setting):
    return psql_output(host, 'SHOW %s' % setting)


def alive(host):
    return psql(host, 'SELECT 1').rc == 0


def is_primary(host):
    r = psql(host, 'SELECT pg_is_in_recovery()')
    return r.rc == 0 and r.stdout.strip() == 'f'


def wait_until(predicate, timeout=CONVERGE_TIMEOUT, interval=3):
    deadline = time.monotonic() + timeout
    value = predicate()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = predicate()
    return value


def all_db_nodes():
    """Every promotion-capable node, by inventory name."""
    return get_named_hosts('primary') + get_named_hosts('standby')


def repmgr_conf(host):
    """Path to repmgr.conf, matching the role's own variable."""
    v = load_ansible_vars()
    return '/etc/repmgr/%s/repmgr-%s.conf' % (
        get_pg_version(), v.get('pg_instance_name', 'main'))


def repmgr(host, args, timeout=120):
    """Run a repmgr subcommand as the Postgres owner."""
    with host.sudo(get_pg_owner()):
        return host.run('timeout %d /usr/pgsql-%s/bin/repmgr -f %s %s'
                        % (timeout, get_pg_version(), repmgr_conf(host), args))


def lsn_value(host, expr):
    """
    An LSN as an integer, compared by PostgreSQL rather than as text.

    Sorting LSN strings lexicographically is wrong across a hex digit
    boundary: '0/9000000' sorts above '0/10000000' but is the smaller
    position, so a text sort picks the *least* advanced standby -- precisely
    the wrong node to promote.
    """
    r = psql(host, "SELECT (%s - '0/0'::pg_lsn)::numeric::bigint" % expr,
             timeout=30)
    if r.rc != 0 or not r.stdout.strip():
        return -1
    try:
        return int(r.stdout.strip())
    except ValueError:
        return -1


def start_writer(host, prefix, ledger_path='/tmp/acknowledged.txt'):
    """
    Start a writer that appends a token to a file only after its commit
    returned.

    The file is the record of what was *acknowledged*: a token present there
    was confirmed to a client, so it must survive the failover. A commit that
    was still in flight when the primary died leaves no token, which is
    correct -- its outcome is undefined and asserting on it would be wrong.
    """
    script = (
        "i=0; while true; do "
        "i=$((i+1)); "
        "if psql -At -h %s -c \"INSERT INTO %s (token) VALUES ('%s-$i')\" "
        "postgres >/dev/null 2>&1; then echo '%s-'$i >> %s; fi; "
        "done"
        % (get_pg_unix_socket_dir(), LEDGER_TABLE, prefix, prefix, ledger_path)
    )
    with host.sudo(get_pg_owner()):
        host.run('rm -f %s' % ledger_path)
        host.run("nohup sh -c \"%s\" >/dev/null 2>&1 &" % script)


def stop_writer(host):
    with host.sudo():
        host.run("pkill -f 'INSERT INTO %s' || true" % LEDGER_TABLE)


def acknowledged_tokens(host, ledger_path='/tmp/acknowledged.txt'):
    with host.sudo(get_pg_owner()):
        r = host.run('cat %s 2>/dev/null || true' % ledger_path)
    return [t for t in r.stdout.strip().split('\n') if t]


def expected_spec():
    """The policy the current phase asked for, from its vars file."""
    v = load_ansible_vars()
    pinned = v.get('synchronous_standby_names', '')
    if pinned:
        m = SYNC_SPEC_RE.match(pinned)
        assert m, 'unparseable synchronous_standby_names: %r' % pinned
        return (m.group('method').upper(), int(m.group('num_sync')),
                [n.strip().strip('"') for n in m.group('names').split(',')])
    names = v.get('synchronous_standby_application_names', [])
    assert names, 'the vars file declares no synchronous standby names'
    return (v.get('standby_quorum_type', 'ANY').upper(),
            int(v.get('synchronous_standby_num_sync', 1)), names)


def current_primary():
    """The node currently accepting writes, or None."""
    for name, host in all_db_nodes():
        if alive(host) and is_primary(host):
            return (name, host)
    return None


_START_TIMES = '/tmp/lifecycle_start_times.txt'


def _record_start_times():
    """Persist each node's postmaster start time for a later phase to compare."""
    lines = []
    for n, host in all_db_nodes():
        lines.append('%s=%s' % (n, psql_output(
            host, 'SELECT pg_postmaster_start_time()')))
    with open(_START_TIMES, 'w') as f:
        f.write('\n'.join(lines))


def _read_start_times():
    try:
        with open(_START_TIMES) as f:
            return dict(l.split('=', 1) for l in f.read().split('\n') if '=' in l)
    except OSError:
        return {}


def ensure_ledger(host):
    psql_output(host, 'CREATE TABLE IF NOT EXISTS %s '
                      '(id serial PRIMARY KEY, token text NOT NULL)'
                      % LEDGER_TABLE)


# ---------------------------------------------------------------------------
# phase 1: the playbook produced a working cluster
# ---------------------------------------------------------------------------

def test_lifecycle_provision_one_primary_three_standbys():
    """Exactly one writable primary and three standbys in recovery."""
    primaries = [n for n, h in all_db_nodes() if alive(h) and is_primary(h)]
    standbys = [n for n, h in all_db_nodes() if alive(h) and not is_primary(h)]

    assert len(primaries) == 1, \
        'expected exactly one writable primary, found %s' % primaries
    assert len(standbys) == 3, \
        'expected three standbys in recovery, found %s' % standbys


def test_lifecycle_provision_cluster_accepts_queries():
    """The cluster serves reads and writes -- the point of provisioning."""
    name, primary = current_primary()
    ensure_ledger(primary)

    psql_output(primary, "INSERT INTO %s (token) VALUES ('provisioned')"
                % LEDGER_TABLE)
    assert psql_output(
        primary, "SELECT count(*) FROM %s WHERE token = 'provisioned'"
        % LEDGER_TABLE) == '1', '%s did not accept a write' % name

    for n, host in all_db_nodes():
        assert psql_output(host, 'SELECT 1') == '1', \
            '%s does not answer reads' % n

    # Baseline for the reconfigure phase's restart check.
    _record_start_times()


def test_lifecycle_provision_standbys_stream_under_their_own_names():
    """
    All three standbys stream, under the application names the inventory gave
    them, with no duplicates.

    PostgreSQL matches synchronous_standby_names against application_name and
    does not enforce uniqueness, so a duplicate would silently distort the
    quorum. Worth asserting rather than counting connections.
    """
    _name, primary = current_primary()

    def streaming():
        r = psql(primary, "SELECT application_name FROM pg_stat_replication "
                          "WHERE state = 'streaming' ORDER BY 1")
        return [l for l in r.stdout.strip().split('\n') if l] if r.rc == 0 else []

    got = wait_until(lambda: streaming() if len(streaming()) == 3 else None)
    assert got, 'not all three standbys are streaming: %s' % streaming()
    assert len(set(got)) == len(got), 'duplicate application_name: %s' % got

    expected = sorted(n for n, _ in get_named_hosts('standby'))
    assert sorted(got) == expected, \
        'streaming names %s do not match the inventory standbys %s' % (
            sorted(got), expected)


def test_lifecycle_provision_write_reaches_every_standby():
    _name, primary = current_primary()
    ensure_ledger(primary)
    psql_output(primary, "INSERT INTO %s (token) VALUES ('replicated') "
                         "ON CONFLICT DO NOTHING" % LEDGER_TABLE)

    for n, host in get_named_hosts('standby'):
        assert wait_until(lambda h=host: psql(
            h, "SELECT count(*) FROM %s WHERE token = 'replicated'"
            % LEDGER_TABLE).stdout.strip() == '1'), \
            '%s never received the committed row' % n


def test_lifecycle_provision_repmgr_topology():
    """
    repmgr knows the whole cluster: one primary, three standbys, one witness.

    Replication working does not imply repmgr registered it, and the failover
    phase promotes through repmgr -- so its view has to be right before that
    is attempted.
    """
    _n, primary = current_primary()
    rows = psql_output(primary, 'SELECT node_name, type FROM repmgr.nodes',
                       database='repmgr')

    got = {}
    for line in [l for l in rows.split('\n') if l]:
        name, node_type = line.split('|')
        got[name] = node_type

    expected = {'postgres01': 'primary', 'witness1': 'witness'}
    for n, _h in get_named_hosts('standby'):
        expected[n] = 'standby'

    assert got == expected, 'repmgr.nodes holds %s, expected %s' % (got, expected)


def test_lifecycle_provision_pgbackrest_configured():
    """
    The pgBackRest stanza checks out from a database node.

    Only that the archiving path is configured and reachable -- a restore test
    belongs in its own case, not in a provisioning check.
    """
    v = load_ansible_vars()
    stanza = v.get('pg_instance_name', 'main')
    _n, primary = current_primary()

    with primary.sudo(get_pg_owner()):
        r = primary.run('timeout 120 pgbackrest --stanza=%s check' % stanza)

    assert r.rc == 0, 'pgbackrest check failed on the primary: %s' % (
        (r.stdout + r.stderr).strip())


# ---------------------------------------------------------------------------
# phase 2: re-running the playbook converged the running cluster
# ---------------------------------------------------------------------------

def test_lifecycle_reconfigure_policy_applied_on_every_candidate():
    """
    The new synchronous policy is in force on every promotion-capable node.

    This is the assertion the whole phase exists for: the change was applied to
    a cluster that was already running and serving.
    """
    method, num_sync, names = expected_spec()

    for n, host in all_db_nodes():
        spec = show(host, 'synchronous_standby_names')
        assert spec, '%s: synchronous_standby_names is empty' % n
        m = SYNC_SPEC_RE.match(spec)
        assert m, '%s: unparseable policy %r' % (n, spec)
        got = (m.group('method').upper(), int(m.group('num_sync')),
               sorted(x.strip().strip('"') for x in m.group('names').split(',')))
        assert got == (method, num_sync, sorted(names)), \
            '%s has %r, expected %s %d (%s)' % (
                n, spec, method, num_sync, ', '.join(names))


def test_lifecycle_reconfigure_running_value_matches_the_file():
    """
    The value in force matches what is on disk, and nothing is left pending.

    A setting written to a file but awaiting a restart is not applied, however
    convincing the file looks.
    """
    for n, host in all_db_nodes():
        pending = psql_output(
            host, "SELECT count(*) FROM pg_settings WHERE pending_restart")
        assert pending == '0', \
            '%s has %s setting(s) pending a restart after reconfiguration' % (
                n, pending)

        file_value = psql_output(
            host,
            "SELECT coalesce(max(setting), '') FROM pg_file_settings "
            "WHERE name = 'synchronous_standby_names' AND applied")
        if file_value:
            assert file_value.strip() == show(
                host, 'synchronous_standby_names').strip(), \
                '%s: running value and applied file value differ' % n


def test_lifecycle_reconfigure_cluster_stayed_available():
    """
    No node restarted during the reconfiguration, and all are serving.

    Querying nodes afterwards only shows they recovered. Comparing
    pg_postmaster_start_time() against the value recorded at provisioning
    shows whether they went down at all -- changing synchronous_standby_names
    is a reload, so a restart here is an availability event nobody asked for.

    The baseline is written by the provision phase; when it is absent (a
    reconfigure-only run) the restart check is skipped rather than guessed at.
    """
    for n, host in all_db_nodes():
        assert alive(host), '%s is not answering after reconfiguration' % n
    assert current_primary() is not None, \
        'no writable primary after reconfiguration'

    baseline = _read_start_times()
    if not baseline:
        pytest.skip('no provision-phase baseline; run the provision phase first')

    restarted = []
    for n, host in all_db_nodes():
        now = psql_output(host, 'SELECT pg_postmaster_start_time()')
        if n in baseline and baseline[n] != now:
            restarted.append(n)

    assert not restarted, (
        'reconfiguration restarted %s. Changing synchronous_standby_names is '
        'a reload; a restart is an availability event nobody asked for.'
        % restarted)


def test_lifecycle_reconfigure_commit_succeeds_with_one_standby_down():
    """With ANY 1 and three standbys, losing one must not stop writes."""
    _name, primary = current_primary()
    ensure_ledger(primary)

    victim_name, victim = get_named_hosts('standby')[0]
    victim.run('systemctl stop %s' % get_pg_service_name())
    try:
        assert wait_until(lambda: not alive(victim), timeout=60), \
            '%s did not stop' % victim_name
        r = psql(primary, "INSERT INTO %s (token) VALUES ('one-down')"
                 % LEDGER_TABLE, timeout=60)
        assert r.rc == 0, \
            'commit did not succeed with one standby down: %s' % r.stderr.strip()
    finally:
        victim.run('systemctl start %s' % get_pg_service_name())
        wait_until(lambda: alive(victim), timeout=120)


# ---------------------------------------------------------------------------
# phase 3: failover must not lose an acknowledged transaction
#
# Everything below stops the primary. Nothing that assumes the provisioned
# topology may run after it.
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def failover():
    """
    Kill the primary while writes are in flight, then promote through repmgr.

    Three things matter here and each was wrong in an earlier version:

    * The writer runs *concurrently* and is killed mid-stream. A test that
      finishes its writes and then shuts down cleanly proves nothing about
      data loss -- a graceful shutdown flushes everything by definition.

    * The primary is killed with SIGKILL, not `systemctl stop`. An abrupt loss
      is the failure being tested.

    * Promotion goes through `repmgr standby promote`, the manual procedure
      this deployment actually uses, and the remaining standbys are then made
      to follow with `repmgr standby follow`. Raw pg_ctl would bypass repmgr,
      leave its metadata stale and leave the survivors chasing a dead primary,
      after which the first synchronous write blocks forever.

    Returns the old primary, the promoted node and the acknowledged tokens.
    """
    old_name, old_host = current_primary()
    ensure_ledger(old_host)

    start_writer(old_host, 'ack')
    time.sleep(10)                     # let a meaningful number commit

    # Abrupt: SIGKILL the postmaster, no clean shutdown, writes in flight.
    with old_host.sudo():
        old_host.run('pkill -9 -f "postgres: .*writer" || true')
        old_host.run('systemctl kill -s SIGKILL %s || true'
                     % get_pg_service_name())
        old_host.run('pkill -9 postgres || true')

    assert wait_until(lambda: not alive(old_host), timeout=120), \
        '%s still answers after SIGKILL; promoting now would risk two ' \
        'writable nodes' % old_name

    tokens = acknowledged_tokens(old_host)
    stop_writer(old_host)
    assert tokens, 'the writer acknowledged nothing before the primary died'

    # Choose the most advanced survivor, compared numerically.
    survivors = [(n, h) for n, h in all_db_nodes()
                 if n != old_name and alive(h)]
    assert survivors, 'no surviving node to promote'

    for _n, h in survivors:            # let replay catch up to what was received
        wait_until(lambda hh=h: lsn_value(hh, 'pg_last_wal_replay_lsn()')
                   >= lsn_value(hh, 'pg_last_wal_receive_lsn()'), timeout=60)

    survivors.sort(key=lambda nh: lsn_value(nh[1], 'pg_last_wal_replay_lsn()'),
                   reverse=True)
    cand_name, cand = survivors[0]

    result = repmgr(cand, 'standby promote')
    assert result.rc == 0, '%s: repmgr standby promote failed: %s' % (
        cand_name, (result.stdout + result.stderr).strip())

    assert wait_until(lambda: is_primary(cand), timeout=PROMOTE_TIMEOUT), \
        '%s is still in recovery after repmgr standby promote' % cand_name

    # The others must follow the new primary, or ANY 1 has no eligible
    # standby and the next synchronous commit never returns.
    followed = []
    for n, h in survivors[1:]:
        r = repmgr(h, 'standby follow')
        if r.rc == 0:
            followed.append(n)

    return {'old': old_name, 'promoted': (cand_name, cand),
            'tokens': tokens, 'followed': followed}


def test_lifecycle_failover_no_acknowledged_transaction_is_lost(failover):
    """
    Every transaction acknowledged before the primary died is present on the
    promoted node.

    ANY 1 says each acknowledged commit reached at least one qualifying
    standby; it does not say which. If the promoted survivor is missing any of
    them, promoting it lost data -- a failure of the promotion procedure, and
    reported as one rather than tolerated.
    """
    cand_name, cand = failover['promoted']
    tokens = failover['tokens']

    present = set(psql_output(
        cand, "SELECT token FROM %s WHERE token LIKE 'ack-%%'" % LEDGER_TABLE,
        timeout=60).split('\n'))

    missing = sorted(set(tokens) - present)
    assert not missing, (
        'DATA LOSS: %d of %d acknowledged transaction(s) absent from the '
        'promoted node %s. First missing: %s'
        % (len(missing), len(tokens), cand_name, missing[:5]))


def test_lifecycle_failover_exactly_one_writable_node(failover):
    """Exactly one node accepts writes after the failover."""
    writable = [n for n, h in all_db_nodes() if alive(h) and is_primary(h)]
    assert len(writable) == 1, \
        'expected one writable node after failover, found %s' % writable


def test_lifecycle_failover_synchronous_writes_resume(failover):
    """
    A synchronous commit succeeds on the new primary once the survivors follow.

    Bounded deliberately: under ANY 1 with no eligible standby attached this
    would otherwise wait forever, and a hang is a worse result than a failure.
    """
    cand_name, cand = failover['promoted']

    assert failover['followed'], (
        '%s: no standby followed the new primary, so ANY 1 has no eligible '
        'candidate and synchronous commits cannot complete' % cand_name)

    streaming = wait_until(
        lambda: psql(cand, "SELECT count(*) FROM pg_stat_replication "
                           "WHERE state = 'streaming'",
                     timeout=30).stdout.strip() not in ('', '0'),
        timeout=PROMOTE_TIMEOUT)
    assert streaming, '%s has no streaming standby after the follows' % cand_name

    r = psql(cand, "INSERT INTO %s (token) VALUES ('post-failover')"
             % LEDGER_TABLE, timeout=60)
    assert r.rc == 0, (
        '%s: a synchronous commit did not complete after failover: %s'
        % (cand_name, (r.stdout + r.stderr).strip()))
