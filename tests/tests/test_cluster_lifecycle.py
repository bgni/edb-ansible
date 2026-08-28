"""
Cluster lifecycle acceptance for an externally supplied playbook.

Driven by tests/run-cluster-lifecycle.sh. The playbook under test lives outside
this repository and is mounted read-only into the runner; these are the
assertions, not the deployment.

Three phases, each depending on the one before, selected by `-k`:

    lifecycle_provision     the playbook built a cluster that serves queries
    lifecycle_reconfigure   re-running it with changed input converged the
                            running cluster onto the new configuration
    lifecycle_failover      an acknowledged transaction survives losing the
                            primary

Phase 2 is the reason this exists. Deploying once with the final value proves
nothing about applying a change to a cluster that is already running; that is
the operation people actually perform, and the one that breaks.

Phase 3 tests the promotion *procedure* as much as the configuration.
`ANY 1` guarantees that each acknowledged commit reached at least one
qualifying standby -- not that a particular survivor has it. So the ledger of
acknowledged transactions is checked against whichever node ends up promoted,
and a promotion that cannot be shown safe should be refused rather than forced.
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


def psql_output(host, query, database='postgres'):
    r = psql(host, query, database=database)
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
    """Every node is still serving after the reconfiguration."""
    for n, host in all_db_nodes():
        assert alive(host), '%s is not answering after reconfiguration' % n
    assert current_primary() is not None, \
        'no writable primary after reconfiguration'


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
def acknowledged_ledger():
    """
    Commit a set of rows, recording each token only after the commit returned.

    That ordering is the whole contract: a token in this list was acknowledged
    to the client, so it must survive the failover. Tokens whose commit did not
    return are deliberately not recorded -- their fate is undefined and
    asserting on them would be wrong.
    """
    name, primary = current_primary()
    ensure_ledger(primary)

    acknowledged = []
    for i in range(10):
        token = 'ack-%02d' % i
        r = psql(primary, "INSERT INTO %s (token) VALUES ('%s')"
                 % (LEDGER_TABLE, token), timeout=60)
        if r.rc == 0:
            acknowledged.append(token)

    assert acknowledged, 'no transaction was acknowledged on %s' % name
    return {'primary': name, 'tokens': acknowledged}


def test_lifecycle_failover_acknowledged_writes_survive(acknowledged_ledger):
    """
    Fence the primary, promote a survivor, and require every acknowledged
    transaction to be present on it.

    ANY 1 says each acknowledged commit reached at least one qualifying
    standby; it does not say which. So the check is against whichever node is
    promoted, and if no survivor holds the acknowledged set then promoting it
    would lose data -- which is a failure of the procedure, and is reported as
    one rather than being tolerated.
    """
    old_name = acknowledged_ledger['primary']
    tokens = acknowledged_ledger['tokens']
    old_host = dict(all_db_nodes())[old_name]

    # Fence: stop it and confirm it is unreachable before anything is promoted.
    old_host.run('systemctl stop %s' % get_pg_service_name())
    assert wait_until(lambda: not alive(old_host), timeout=120), \
        '%s still answers; promoting now would risk two writable nodes' % old_name

    survivors = [(n, h) for n, h in get_named_hosts('standby')
                 if n != old_name and alive(h)]
    assert survivors, 'no surviving standby to promote'

    # Choose the most advanced survivor, which is the only defensible choice.
    def replay_lsn(host):
        r = psql(host, 'SELECT pg_last_wal_replay_lsn()')
        return r.stdout.strip() if r.rc == 0 else ''

    survivors.sort(key=lambda nh: replay_lsn(nh[1]), reverse=True)
    cand_name, cand = survivors[0]

    with cand.sudo(get_pg_owner()):
        promoted = cand.run('/usr/pgsql-%s/bin/pg_ctl promote -D %s'
                            % (get_pg_version(), show(cand, 'data_directory')))
    assert promoted.rc == 0, \
        '%s could not be promoted: %s' % (cand_name, promoted.stderr.strip())

    assert wait_until(lambda: is_primary(cand), timeout=PROMOTE_TIMEOUT), \
        '%s is still in recovery after promotion' % cand_name

    present = psql_output(
        cand, "SELECT token FROM %s WHERE token LIKE 'ack-%%' ORDER BY token"
        % LEDGER_TABLE).split('\n')
    present = [t for t in present if t]

    missing = sorted(set(tokens) - set(present))
    assert not missing, (
        'DATA LOSS: %d acknowledged transaction(s) absent from the promoted '
        'node %s: %s' % (len(missing), cand_name, missing))


def test_lifecycle_failover_exactly_one_writable_node(acknowledged_ledger):
    """After the failover exactly one node accepts writes."""
    writable = [n for n, h in all_db_nodes() if alive(h) and is_primary(h)]
    assert len(writable) == 1, \
        'expected one writable node after failover, found %s' % writable


def test_lifecycle_failover_new_primary_accepts_writes(acknowledged_ledger):
    """The promoted node is a usable primary, not merely out of recovery."""
    found = current_primary()
    assert found, 'no writable primary after failover'
    name, host = found

    psql_output(host, "INSERT INTO %s (token) VALUES ('post-failover')"
                % LEDGER_TABLE)
    assert psql_output(
        host, "SELECT count(*) FROM %s WHERE token = 'post-failover'"
        % LEDGER_TABLE) == '1', '%s did not accept a write' % name
