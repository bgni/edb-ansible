"""
Day-two operations on the production-shaped topology.

Everything in `test_prod_topology.py` verifies the cluster as deployed. This
module verifies what happens *after* that: applying a PostgreSQL configuration
change to a running cluster, and promoting a standby to primary.

Both are where the reviewed defects actually bite. A configuration change is
what drives the un-orchestrated restart path (EDB-09), and promotion is the
moment the inherited `/bin/true` archive command stops being inert and starts
silently discarding WAL (EDB-18), and the moment a standby needs to have been
carrying the synchronous policy all along (EDB-03).

Ordering matters and is load-bearing. pytest collects files alphabetically, so
`test_prod_topology.py` runs before `test_prod_topology_day2.py`, and within
this file the configuration tests run before the promotion tests. The promotion
tests deliberately stop the primary and promote a standby, so nothing that
assumes the original topology may run after them.
"""

import json
import os
import subprocess

import pytest

from conftest import (
    get_named_hosts,
    get_pg_owner,
    get_pg_service_name,
    get_pg_version,
    get_primary,
    get_standbys,
)
from test_prod_topology import (
    all_database_nodes,
    expected_sync_spec,
    parse_sync_spec,
    psql,
    psql_output,
    repmgr_config_path,
    show,
    wait_until,
    FAKE_ARCHIVE_COMMAND,
)


CASE_DIR = '/workspace/tests/cases/prod_topology'
DAY2_PLAYBOOK = os.path.join(CASE_DIR, 'day2_config.yml')

# Reload-only and restart-only settings used to drive the two configuration
# paths. log_min_duration_statement takes effect on reload;
# shared_buffers needs a restart. shared_buffers is chosen over something like
# max_connections because it carries no primary/standby ordering constraint --
# a standby refuses to start if its max_connections is below the primary's,
# which would make the test fail for a reason unrelated to what it checks.
RELOAD_SETTING = 'log_min_duration_statement'
RELOAD_VALUE = '1234ms'
RESTART_SETTING = 'shared_buffers'
RESTART_VALUE = '160MB'

DAY2_TABLE = 'prod_topology_day2_check'

# Promotion and cluster recovery are not instant.
RECOVERY_TIMEOUT = 240
PROMOTION_TIMEOUT = 180


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run_playbook(playbook, extra_vars=None):
    """
    Run a playbook against the deployed cluster from inside the tester
    container, the same way exec-tests.sh ran the deployment.

    Returns the CompletedProcess so a caller can assert on it.
    """
    command = [
        'ansible-playbook',
        '-i', os.environ['EDB_INVENTORY'],
        '--private-key', os.environ.get('EDB_SSH_KEY', '/root/.ssh/id_rsa'),
        '--extra-vars', '@%s' % os.environ['EDB_ANSIBLE_VARS'],
        '--extra-vars', 'pg_type=%s' % os.environ['EDB_PG_TYPE'],
        '--extra-vars', 'pg_version=%s' % os.environ['EDB_PG_VERSION'],
        '--extra-vars', 'enable_edb_repo=%s' % os.environ.get(
            'EDB_ENABLE_REPO', 'false'),
    ]

    if extra_vars:
        command += ['--extra-vars', json.dumps(extra_vars)]

    command.append(playbook)

    env = os.environ.copy()
    env['ANSIBLE_PIPELINING'] = '1'

    return subprocess.run(
        command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True,
    )


def apply_setting(name, value):
    """
    Apply one PostgreSQL setting to every database node through the collection,
    and fail the test with the playbook output if the run did not succeed.
    """
    result = run_playbook(DAY2_PLAYBOOK, extra_vars={
        'pg_postgres_conf_params': [{'name': name, 'value': value}],
    })

    assert result.returncode == 0, (
        'day-two playbook failed applying %s=%s (rc=%d):\n%s'
        % (name, value, result.returncode, result.stdout[-4000:]))

    return result


def postmaster_start_times():
    """
    Map node name to pg_postmaster_start_time(), used to tell a reload from a
    restart. A reload leaves the start time untouched.
    """
    times = {}
    for name, host in all_database_nodes():
        times[name] = psql_output(host, 'SELECT pg_postmaster_start_time()')
    return times


def wait_for_postgres(host, timeout=RECOVERY_TIMEOUT):
    """Wait until a node answers a trivial query again."""
    return wait_until(
        lambda: psql(host, 'SELECT 1').rc == 0,
        timeout=timeout,
    )


def streaming_standbys(primary):
    """Application names of the standbys currently streaming from primary."""
    result = psql(
        primary,
        "SELECT application_name FROM pg_stat_replication "
        "WHERE state = 'streaming' ORDER BY application_name")

    if result.rc != 0:
        return []

    return [line for line in result.stdout.strip().split('\n') if line]


def promote(name, host):
    """
    Promote a standby with repmgr, falling back to pg_ctl.

    repmgr is the promotion path this deployment actually uses, so it is tried
    first; the fallback keeps the test meaningful if repmgr is unavailable.
    """
    config = repmgr_config_path()

    if host.file(config).exists:
        with host.sudo(get_pg_owner()):
            result = host.run(
                '/usr/pgsql-%s/bin/repmgr -f %s standby promote'
                % (get_pg_version(), config))

        if result.rc == 0:
            return 'repmgr'

        promotion_error = result.stderr.strip()
    else:
        promotion_error = '%s does not exist' % config

    pgdata = show(host, 'data_directory')

    with host.sudo(get_pg_owner()):
        result = host.run(
            '/usr/pgsql-%s/bin/pg_ctl promote -D %s'
            % (get_pg_version(), pgdata))

    assert result.rc == 0, (
        '%s: could not be promoted. repmgr: %s. pg_ctl: %s'
        % (name, promotion_error, result.stderr.strip()))

    return 'pg_ctl'


# ---------------------------------------------------------------------------
# day two: configuration change on a running cluster
# ---------------------------------------------------------------------------

def test_prod_topology_day2_reload_change_applies_without_restart():
    """
    A reload-only setting reaches every node, and no node restarts.

    This is the safe day-two path: the collection should notice the setting
    does not require a restart, reload instead, and leave every postmaster
    running. A restart here would be an availability event nobody asked for.
    """
    before = postmaster_start_times()

    apply_setting(RELOAD_SETTING, RELOAD_VALUE)

    for name, host in all_database_nodes():
        value = show(host, RELOAD_SETTING)

        assert value == RELOAD_VALUE, \
            '%s: %s is %r after the day-two change, expected %r' % (
                name, RELOAD_SETTING, value, RELOAD_VALUE)

    after = postmaster_start_times()

    restarted = [name for name in before if before[name] != after[name]]

    assert not restarted, (
        'a reload-only change restarted %s. The setting does not require a '
        'restart, so this is an unnecessary availability event.' % restarted)


def test_prod_topology_day2_reload_change_leaves_replication_healthy():
    """The reload did not disturb streaming replication."""
    primary = get_primary()

    names = wait_until(
        lambda: (streaming_standbys(primary)
                 if len(streaming_standbys(primary)) == len(get_standbys())
                 else None))

    assert names, \
        'not all standbys are streaming after the reload; currently: %s' \
        % streaming_standbys(primary)


def test_prod_topology_day2_restart_change_applies_and_cluster_recovers():
    """
    A restart-requiring setting reaches every node and the cluster comes back.

    The collection restarts each node as soon as it sees the change, with no
    replica-first ordering, catch-up wait or quorum guard (EDB-09). This test
    does not assert that ordering is safe -- it is not -- it asserts the
    outcome an operator depends on: every node returns, every standby
    re-attaches, and the synchronous policy survives.
    """
    apply_setting(RESTART_SETTING, RESTART_VALUE)

    for name, host in all_database_nodes():
        assert wait_for_postgres(host), \
            '%s: Postgres did not come back after the restart-requiring ' \
            'change' % name

        value = show(host, RESTART_SETTING)

        assert value == RESTART_VALUE, \
            '%s: %s is %r after the day-two change, expected %r' % (
                name, RESTART_SETTING, value, RESTART_VALUE)

    primary = get_primary()

    names = wait_until(
        lambda: (streaming_standbys(primary)
                 if len(streaming_standbys(primary)) == len(get_standbys())
                 else None),
        timeout=RECOVERY_TIMEOUT)

    assert names, \
        'not every standby re-attached after the restart; currently: %s' \
        % streaming_standbys(primary)


def test_prod_topology_day2_restart_change_preserves_synchronous_policy():
    """
    The approved quorum is still in force on every node after the restart.

    A configuration change that silently dropped synchronous_standby_names
    would leave the cluster acknowledging commits with no remote durability,
    which is the failure the durability policy exists to prevent.
    """
    quorum, num_sync, names = expected_sync_spec()

    for name, host in all_database_nodes():
        spec = show(host, 'synchronous_standby_names')

        assert spec, \
            '%s: synchronous_standby_names is empty after the day-two ' \
            'restart' % name

        got = parse_sync_spec(spec)

        assert (got[0], got[1], sorted(got[2])) == \
               (quorum, num_sync, sorted(names)), \
            '%s: synchronous policy drifted to %r during the day-two change' \
            % (name, spec)


def test_prod_topology_day2_writes_still_replicate_after_reconfiguration():
    """A row committed after the reconfiguration reaches every standby."""
    primary = get_primary()

    psql_output(
        primary,
        'CREATE TABLE IF NOT EXISTS %s (id int primary key, note text)'
        % DAY2_TABLE)
    psql_output(
        primary,
        "INSERT INTO %s VALUES (1, 'after-reconfigure') "
        'ON CONFLICT (id) DO UPDATE SET note = EXCLUDED.note' % DAY2_TABLE)

    for name, host in get_named_hosts('standby'):
        found = wait_until(lambda h=host: psql(
            h, 'SELECT note FROM %s WHERE id = 1' % DAY2_TABLE
        ).stdout.strip() == 'after-reconfigure')

        assert found, \
            '%s: the row committed after reconfiguration never arrived' % name


# ---------------------------------------------------------------------------
# day two: promotion
#
# Everything below stops the original primary and promotes a standby. Nothing
# that assumes the deployed topology may be added after this point.
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def promoted():
    """
    Stop the primary and promote the first standby. Returns
    (name, host, method).

    Deliberately idempotent. The fixture is module-scoped, and
    test_prod_topology_mtls.py imports it to test authentication against the
    promoted primary -- which gives pytest a second fixture definition and a
    second invocation. Promoting an already-promoted node fails, so this
    returns the existing promotion instead of attempting another one.
    """
    standbys = get_named_hosts('standby')

    assert standbys, 'the case deployed no standby to promote'

    name, host = standbys[0]

    # Already promoted by an earlier module: nothing to do.
    if psql(host, 'SELECT pg_is_in_recovery()').stdout.strip() == 'f':
        return (name, host, 'already-promoted')

    old_primary = get_primary()

    # Stop the primary first. Promoting while the old primary is still writable
    # is exactly the split-brain the reviews warn about, and is not something a
    # test should manufacture.
    old_primary.run('systemctl stop %s' % get_pg_service_name())

    assert wait_until(
        lambda: psql(old_primary, 'SELECT 1').rc != 0, timeout=60), \
        'the old primary is still answering queries after systemctl stop'

    method = promote(name, host)

    assert wait_until(
        lambda: psql(host, 'SELECT pg_is_in_recovery()').stdout.strip() == 'f',
        timeout=PROMOTION_TIMEOUT), \
        '%s: still in recovery after promotion via %s' % (name, method)

    return (name, host, method)


def test_prod_topology_day2_promoted_standby_accepts_writes(promoted):
    """The promoted node left recovery and is writable."""
    name, host, method = promoted

    assert psql_output(host, 'SELECT pg_is_in_recovery()') == 'f', \
        '%s: still in recovery after promotion via %s' % (name, method)

    psql_output(
        host,
        'CREATE TABLE IF NOT EXISTS %s_promoted (id int primary key)'
        % DAY2_TABLE)
    psql_output(
        host,
        'INSERT INTO %s_promoted VALUES (1) ON CONFLICT DO NOTHING'
        % DAY2_TABLE)

    assert psql_output(
        host, 'SELECT count(*) FROM %s_promoted' % DAY2_TABLE) == '1', \
        '%s: the promoted node did not accept a write' % name


def test_prod_topology_day2_promoted_standby_kept_the_synchronous_policy(
        promoted):
    """
    The promoted node was already carrying the approved synchronous policy.

    This is the whole point of EDB-03. The policy has to be installed on every
    promotion candidate *before* it is promoted: a node that only receives it
    afterwards spends the window between promotion and reconfiguration
    acknowledging commits with no synchronous protection at all.
    """
    name, host, _method = promoted

    quorum, num_sync, names = expected_sync_spec()

    spec = show(host, 'synchronous_standby_names')

    assert spec, (
        '%s: synchronous_standby_names is empty on the newly promoted node. '
        'It is now accepting commits with no synchronous protection.' % name)

    got = parse_sync_spec(spec)

    assert (got[0], got[1], sorted(got[2])) == \
           (quorum, num_sync, sorted(names)), \
        '%s: promoted node carries %r, expected the approved %s %d (%s)' % (
            name, spec, quorum, num_sync, ', '.join(names))


def test_prod_topology_day2_promoted_standby_timeline_advanced(promoted):
    """Promotion moved the node onto a new timeline, as a promotion must."""
    name, host, _method = promoted

    timeline = psql_output(
        host, 'SELECT timeline_id FROM pg_control_checkpoint()')

    assert int(timeline) > 1, \
        '%s: timeline is still %s after promotion' % (name, timeline)


def test_finding_edb_18_promotion_activates_the_fake_archive_command(promoted):
    """
    EDB-18, demonstrated live: promotion turns the inherited `/bin/true`
    archive command from inert into actively destructive.

    While the node was a standby, `archive_mode=on` meant PostgreSQL never ran
    `archive_command`, so nothing looked wrong. Now that it has been promoted,
    recovery is over and the command runs -- exiting 0 for every segment
    without storing it. PostgreSQL believes each segment is safely archived and
    is free to recycle it, so PITR from this new primary is broken from the
    moment it was promoted.

    Passes while the defect is present. When the real archive command is
    installed on every promotion-capable node, this fails; remove it and
    refresh tests/findings/baseline.json.
    """
    name, host, _method = promoted

    archive_mode = show(host, 'archive_mode')
    archive_command = show(host, 'archive_command')

    assert FAKE_ARCHIVE_COMMAND in archive_command, (
        'EDB-18 appears to be FIXED: the promoted node %s has '
        'archive_command=%r. Remove this test and refresh '
        'tests/findings/baseline.json.' % (name, archive_command))

    assert archive_mode == 'on', \
        '%s: archive_mode=%s, expected on' % (name, archive_mode)

    assert psql_output(host, 'SELECT pg_is_in_recovery()') == 'f', \
        '%s: not out of recovery, the archive command would still be inert' \
        % name

    # Force a segment through the archiver and show PostgreSQL counting it as
    # archived. That count is the false assurance: nothing was stored.
    before = psql_output(
        host, 'SELECT archived_count FROM pg_stat_archiver')

    psql_output(host, 'SELECT pg_switch_wal()')

    archived = wait_until(
        lambda: psql_output(
            host, 'SELECT archived_count FROM pg_stat_archiver') != before,
        timeout=60)

    assert archived, (
        '%s: the archiver did not report a segment after pg_switch_wal(); '
        'expected /bin/true to report success for it' % name)

    failed = psql_output(host, 'SELECT failed_count FROM pg_stat_archiver')

    assert failed == '0', (
        '%s: pg_stat_archiver reports %s failures. The point of this finding '
        'is that /bin/true reports success -- a visible failure would at '
        'least be detectable.' % (name, failed))


def test_finding_edb_07_inventory_is_stale_after_promotion(promoted):
    """
    EDB-07, demonstrated live: the inventory still calls the stopped node the
    primary.

    Node roles come from static inventory groups, and nothing re-derives them
    from the running cluster. After a promotion the inventory describes the
    former topology, so a rerun of the collection would configure -- and
    force-register with `repmgr ... register -F` -- the wrong node.

    Passes while the defect is present.
    """
    promoted_name, _host, _method = promoted

    inventory_primaries = [name for name, _h in get_named_hosts('primary')]
    inventory_standbys = [name for name, _h in get_named_hosts('standby')]

    assert promoted_name in inventory_standbys, (
        'EDB-07 appears to be FIXED: the promoted node %s is no longer listed '
        'as a standby in the inventory. Remove this test and refresh '
        'tests/findings/baseline.json.' % promoted_name)

    assert inventory_primaries, 'the inventory lists no primary at all'

    # The node the inventory still calls the primary is the one we stopped.
    for name, host in get_named_hosts('primary'):
        assert psql(host, 'SELECT 1').rc != 0, (
            '%s is still answering queries; this test assumed it was the '
            'stopped former primary' % name)
