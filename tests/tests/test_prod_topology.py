"""
Live verification of the production-shaped topology.

This is the executable half of the finding audit. `tests/findings/` proves what
the collection's *source* says; this module proves what a deployed cluster
actually does, on the topology the source reviews assume: PostgreSQL 17 on
RHEL 9, one primary and three standbys under quorum synchronous replication, a
repmgr witness, pgBackRest, and an HAProxy client gate.

Two kinds of test live here, and the distinction matters when reading a
failure:

  * `test_*` -- things that must work. A failure is a broken deployment.
  * `test_finding_*` -- the reviewed defects. These assert the behaviour the
    reviews describe, so they PASS while the defect is present. Each one names
    the finding and says what a fix would look like. When a defect is fixed,
    the corresponding test fails loudly and tells you to update it and the
    baseline in tests/findings/.

That inversion is deliberate: it makes the known-bad behaviour visible in CI
and impossible to fix silently, which is what the reviews ask for.
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
    get_primary,
    get_standbys,
    get_witness,
    load_ansible_vars,
)


# `ANY 2 ("standby1","standby2","standby3")`
SYNC_SPEC_RE = re.compile(
    r'^\s*(?P<method>ANY|FIRST)\s+(?P<num_sync>\d+)\s*\((?P<names>.*)\)\s*$',
    re.IGNORECASE,
)

TEST_TABLE = 'prod_topology_check'

# The fake archive command the init template installs. Findings EDB-05/EDB-18.
FAKE_ARCHIVE_COMMAND = '/bin/true'


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def psql(host, query, database='postgres', timeout=None):
    """Run a query as the Postgres owner over the unix socket."""
    command = 'psql -At -h %s -c "%s" %s' % (
        get_pg_unix_socket_dir(), query, database
    )
    if timeout is not None:
        command = 'timeout %d %s' % (timeout, command)

    with host.sudo(get_pg_owner()):
        return host.run(command)


def psql_output(host, query, database='postgres'):
    """Run a query and fail the test if it did not succeed."""
    result = psql(host, query, database=database)

    assert result.rc == 0, \
        'Query %r failed with rc=%d: %s' % (
            query, result.rc, result.stderr.strip())

    return result.stdout.strip()


def show(host, setting):
    """Return the running value of a GUC on one node."""
    return psql_output(host, 'SHOW %s' % setting)


def parse_sync_spec(spec):
    """Split synchronous_standby_names into (method, num_sync, names)."""
    match = SYNC_SPEC_RE.match(spec)

    assert match, \
        'synchronous_standby_names=%r is not a quorum specification' % spec

    names = [n.strip().strip('"') for n in match.group('names').split(',')]

    return (match.group('method').upper(), int(match.group('num_sync')), names)


def expected_sync_spec():
    """
    The quorum the test case asked for, built from vars.json.

    vars.json leaves `synchronous_standby_names` empty on purpose so the role
    has to generate it; the expected value is therefore assembled here from the
    same fields the role reads.
    """
    ansible_vars = load_ansible_vars()

    quorum = ansible_vars.get('standby_quorum_type', 'ANY').upper()
    num_sync = int(ansible_vars.get('synchronous_standby_num_sync', 1))
    names = ansible_vars.get('synchronous_standby_application_names', [])

    assert names, \
        'This test case must declare synchronous_standby_application_names'

    return (quorum, num_sync, names)


def all_database_nodes():
    """Every promotion-capable node: the primary and all standbys."""
    return [('primary1', get_primary())] + get_named_hosts('standby')


def wait_until(predicate, timeout=180, interval=3):
    """Poll until predicate returns something truthy, or time out."""
    deadline = time.monotonic() + timeout
    value = predicate()

    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = predicate()

    return value


# ---------------------------------------------------------------------------
# the deployment must work
# ---------------------------------------------------------------------------

def test_prod_topology_postgres_is_running_everywhere():
    """Every PostgreSQL node, witness included, runs the requested version."""
    nodes = all_database_nodes() + [('witness1', get_witness()[0])]

    for name, host in nodes:
        service = host.service(get_pg_service_name())

        assert service.is_running, '%s: Postgres is not running' % name

        version = psql_output(host, 'SHOW server_version')

        assert version.split('.')[0] == str(get_pg_version()), \
            '%s: expected Postgres %s, found %s' % (
                name, get_pg_version(), version)


def test_prod_topology_node_roles():
    """The primary is writable and every standby is in recovery."""
    assert psql_output(get_primary(), 'SELECT pg_is_in_recovery()') == 'f', \
        'primary1 is in recovery'

    for name, host in get_named_hosts('standby'):
        assert psql_output(host, 'SELECT pg_is_in_recovery()') == 't', \
            '%s is not in recovery' % name


def test_prod_topology_pg_basebackup_built_every_standby():
    """
    pg_basebackup worked on all three standbys.

    This is the check that would have caught EDB-14, where the role asked
    pg_basebackup to create a replication slot the upstream node had already
    created, so `-C` failed and no standby was ever built. It asserts the
    outcome rather than the flag: a real data directory, cloned from the
    primary, sharing the primary's system identifier.
    """
    primary_sysid = psql_output(
        get_primary(), 'SELECT system_identifier FROM pg_control_system()')

    for name, host in get_named_hosts('standby'):
        pgdata = show(host, 'data_directory')

        assert host.file('%s/PG_VERSION' % pgdata).exists, \
            '%s: %s/PG_VERSION is missing, base backup did not complete' % (
                name, pgdata)

        # A standby built by pg_basebackup -R carries a primary_conninfo.
        conninfo = show(host, 'primary_conninfo')

        assert conninfo, \
            '%s: primary_conninfo is empty, this node was not cloned' % name

        sysid = psql_output(
            host, 'SELECT system_identifier FROM pg_control_system()')

        assert sysid == primary_sysid, \
            '%s: system identifier %s does not match the primary (%s); this ' \
            'node is not a clone of it' % (name, sysid, primary_sysid)


def test_prod_topology_replication_slots_are_reused_not_duplicated():
    """
    Each standby has exactly one physical slot on the primary, and it is
    active. Two slots for one standby, or an inactive slot, means the
    pre-create and the base backup disagreed about slot ownership.
    """
    rows = psql_output(
        get_primary(),
        "SELECT slot_name, slot_type, active FROM pg_replication_slots "
        "ORDER BY slot_name")

    slots = {}
    for line in [line for line in rows.split('\n') if line]:
        slot_name, slot_type, active = line.split('|')
        slots[slot_name] = (slot_type, active)

    for name, _host in get_named_hosts('standby'):
        expected = name.replace('-', '_')

        assert expected in slots, \
            '%s: no physical slot named %r on the primary; found %s' % (
                name, expected, sorted(slots))

        slot_type, active = slots[expected]

        assert slot_type == 'physical', \
            '%s: slot %s is %s, expected physical' % (name, expected, slot_type)
        assert active == 't', \
            '%s: slot %s is not active, the standby is not streaming from it' \
            % (name, expected)


def test_prod_topology_every_standby_is_streaming():
    """All three standbys appear in pg_stat_replication under their own name."""
    def streaming():
        rows = psql_output(
            get_primary(),
            "SELECT application_name, state FROM pg_stat_replication "
            "WHERE state = 'streaming' ORDER BY application_name")
        return [line for line in rows.split('\n') if line]

    rows = wait_until(lambda: (
        streaming() if len(streaming()) == len(get_standbys()) else None))

    assert rows, \
        'not all standbys reached state=streaming; pg_stat_replication shows %s' \
        % streaming()

    names = sorted(line.split('|')[0] for line in rows)
    _quorum, _num_sync, expected_names = expected_sync_spec()

    assert names == sorted(expected_names), \
        'streaming standbys %s do not match the declared quorum members %s' % (
            names, sorted(expected_names))


def test_prod_topology_synchronous_policy_is_identical_on_every_node():
    """
    The promotion-ready durability check (finding EDB-03).

    Every node that could be promoted must already carry the approved
    synchronous_standby_names. A standby that is missing it would, on
    promotion, accept commits with no synchronous protection at all.
    """
    quorum, num_sync, names = expected_sync_spec()

    for name, host in all_database_nodes():
        spec = show(host, 'synchronous_standby_names')

        assert spec, \
            '%s: synchronous_standby_names is empty; on promotion this node ' \
            'would acknowledge commits with no synchronous protection' % name

        got_quorum, got_num_sync, got_names = parse_sync_spec(spec)

        assert (got_quorum, got_num_sync, sorted(got_names)) == \
               (quorum, num_sync, sorted(names)), \
            '%s: synchronous policy is %r, expected %s %d (%s)' % (
                name, spec, quorum, num_sync, ', '.join(names))


def test_prod_topology_synchronous_commit_is_durable():
    """
    `synchronous_commit` must wait for a remote flush on every node. Anything
    weaker than `on`/`remote_apply` silently drops the guarantee the quorum is
    supposed to provide.
    """
    for name, host in all_database_nodes():
        value = show(host, 'synchronous_commit')

        assert value in ('on', 'remote_apply'), \
            '%s: synchronous_commit=%s does not wait for a remote flush' % (
                name, value)


def test_prod_topology_writes_reach_every_standby():
    """A committed row is replayed on all three standbys."""
    primary = get_primary()

    psql_output(
        primary,
        'CREATE TABLE IF NOT EXISTS %s (id int primary key, note text)'
        % TEST_TABLE)
    psql_output(
        primary,
        "INSERT INTO %s VALUES (1, 'replicated') "
        'ON CONFLICT (id) DO UPDATE SET note = EXCLUDED.note' % TEST_TABLE)

    for name, host in get_named_hosts('standby'):
        found = wait_until(lambda h=host: psql(
            h, 'SELECT note FROM %s WHERE id = 1' % TEST_TABLE
        ).stdout.strip() == 'replicated')

        assert found, '%s: the committed row never arrived' % name


def test_prod_topology_repmgr_registered_every_node():
    """repmgr knows about the primary, all standbys and the witness."""
    primary = get_primary()

    rows = psql_output(
        primary,
        'SELECT node_name, type FROM repmgr.nodes ORDER BY node_name',
        database='repmgr')

    registered = {}
    for line in [line for line in rows.split('\n') if line]:
        node_name, node_type = line.split('|')
        registered[node_name] = node_type

    expected = {'primary1': 'primary', 'witness1': 'witness'}
    for name, _host in get_named_hosts('standby'):
        expected[name] = 'standby'

    assert registered == expected, \
        'repmgr.nodes holds %s, expected %s' % (registered, expected)


def test_prod_topology_haproxy_is_running():
    """The client gate is up and its configuration lists every backend."""
    proxies = get_hosts('proxy')

    assert proxies, 'no host in the proxy group'

    for host in proxies:
        assert host.service('haproxy').is_running, 'haproxy is not running'

        config = host.file('/etc/haproxy/haproxy.cfg')

        assert config.exists, '/etc/haproxy/haproxy.cfg is missing'

        for name, _node in all_database_nodes():
            assert name in config.content_string, \
                'haproxy.cfg has no backend entry for %s' % name


def test_prod_topology_pgbackrest_stanza_is_healthy():
    """`pgbackrest check` passes and a full backup exists in the repository."""
    ansible_vars = load_ansible_vars()
    stanza = ansible_vars.get('pg_instance_name', 'main')

    primary = get_primary()

    with primary.sudo(get_pg_owner()):
        result = primary.run('pgbackrest --stanza=%s check' % stanza)

    assert result.rc == 0, \
        'pgbackrest check failed on the primary: %s' % result.stderr.strip()

    with primary.sudo(get_pg_owner()):
        info = primary.run('pgbackrest --stanza=%s info' % stanza)

    assert 'full backup' in info.stdout, \
        'no full backup in the repository: %s' % info.stdout.strip()


# ---------------------------------------------------------------------------
# reviewed defects -- these pass while the defect is present
# ---------------------------------------------------------------------------

def test_finding_edb_18_standbys_keep_the_fake_archive_command():
    """
    EDB-18: a promotion-capable standby keeps `archive_command='/bin/true'`.

    The pgBackRest role gives standbys `archive_mode=on` but installs the real
    archive-push command on them only when `backup_standby == 'y'`, and the
    shipped default is `"n"`. `archive_mode=on` keeps the fake command inert
    while the node is in recovery, so nothing looks wrong -- until the node is
    promoted, at which point it reports every WAL segment as archived while
    discarding it, breaking PITR from the new primary.

    A fix installs the real command on every promotion-capable node. When that
    lands, this test fails; delete it and update tests/findings/baseline.json.
    """
    still_broken = []

    for name, host in get_named_hosts('standby'):
        archive_command = show(host, 'archive_command')
        archive_mode = show(host, 'archive_mode')

        if FAKE_ARCHIVE_COMMAND in archive_command:
            still_broken.append((name, archive_mode, archive_command))

    assert still_broken, (
        'EDB-18 appears to be FIXED: no standby carries '
        "archive_command='%s' any more. Remove this test and refresh "
        'tests/findings/baseline.json.' % FAKE_ARCHIVE_COMMAND)

    # Record precisely what the defect looks like on this deployment.
    for name, archive_mode, archive_command in still_broken:
        assert archive_mode == 'on', (
            '%s: archive_mode=%s. With `always` the fake command would run '
            'immediately rather than lying in wait until promotion.'
            % (name, archive_mode))


def test_finding_edb_17_standbys_have_no_archive_fallback():
    """
    EDB-17: no standby has a `restore_command`.

    pgBackRest's standby configuration defines an `[global:archive-get]`
    section, but PostgreSQL is never told to call it. A standby that falls
    behind further than its slot retains therefore cannot fetch the missing WAL
    from the repository, and needs a full re-clone instead of an archive
    catch-up.
    """
    without_fallback = []

    for name, host in get_named_hosts('standby'):
        restore_command = show(host, 'restore_command')
        if not restore_command.strip():
            without_fallback.append(name)

    assert without_fallback, (
        'EDB-17 appears to be FIXED: every standby now has a restore_command. '
        'Remove this test and refresh tests/findings/baseline.json.')

    assert len(without_fallback) == len(get_standbys()), \
        'only some standbys lack a restore_command: %s' % without_fallback


def test_finding_edb_08_slot_retention_is_unbounded():
    """
    EDB-08: `max_slot_wal_keep_size` is left at -1 on every node.

    A standby that goes away holds its slot, and the slot pins WAL on the
    primary with no ceiling, until `pg_wal` fills and the primary stops. The
    fix is a reviewed finite value sized against WAL generation and the standby
    repair SLA -- not simply a smaller number.
    """
    unbounded = []

    for name, host in all_database_nodes():
        value = show(host, 'max_slot_wal_keep_size')
        if value.strip() in ('-1', '-1B'):
            unbounded.append(name)

    assert unbounded, (
        'EDB-08 appears to be FIXED: max_slot_wal_keep_size is bounded. '
        'Remove this test and refresh tests/findings/baseline.json.')

    assert len(unbounded) == len(all_database_nodes()), \
        'slot retention is bounded on only some nodes: %s' % unbounded


def test_finding_edb_01_failover_is_automatic_without_fencing():
    """
    EDB-01: repmgr is configured for automatic failover.

    The deployment has no mechanism that proves a former primary cannot still
    accept writes, so an automatic promotion during a partition can produce two
    writable timelines. The reviews require `failover=manual` until a tested
    fencing mechanism exists.
    """
    conf = get_primary().file('/etc/repmgr/%s/repmgr.conf' % get_pg_version())

    if not conf.exists:
        pytest.skip('repmgr.conf not found at the expected path')

    match = re.search(r"^failover\s*=\s*'?(\w+)'?", conf.content_string,
                      re.MULTILINE)

    assert match, 'no failover setting in repmgr.conf'

    assert match.group(1) == 'automatic', (
        'EDB-01 appears to be FIXED: failover=%s. Remove this test and '
        'refresh tests/findings/baseline.json.' % match.group(1))


def test_finding_tls_02_no_client_certificate_authentication():
    """
    TLS-02: the deployment has no client-certificate authentication.

    The target architecture assumes certbot with a local ACME server issuing
    both client and server certificates. The collection has no ACME
    integration (TLS-01) and never writes `clientcert=verify-full`, so even
    with TLS enabled a client that trusts the server CA still authenticates by
    password alone.
    """
    hba_paths = []

    for name, host in all_database_nodes():
        pgdata = show(host, 'data_directory')
        hba = host.file('%s/pg_hba.conf' % pgdata)

        if not hba.exists:
            continue

        hba_paths.append(name)

        assert 'clientcert' not in hba.content_string, (
            'TLS-02 appears to be FIXED: %s has a clientcert rule in '
            'pg_hba.conf. Remove this test and refresh '
            'tests/findings/baseline.json.' % name)

    assert hba_paths, 'no pg_hba.conf found on any database node'
