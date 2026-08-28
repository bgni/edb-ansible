"""
Non regression tests for a four node Postgres cluster (one primary and three
standbys) using quorum based synchronous replication.

The quorum is declared once, in the test case `vars.json`, as
`standby_quorum_type` + `synchronous_standby_num_sync` +
`synchronous_standby_application_names`. Both the deployment and the assertions
below derive from that single declaration, so changing the quorum in
`vars.json` changes what is deployed *and* what is verified. The finished
`synchronous_standby_names` string is left empty on purpose so that the role's
generator, and the validation asserts that go with it, are exercised rather
than bypassed.

With `ANY 2 ("standby1", "standby2", "standby3")` the expected behaviour is:

  * the three standbys are quorum candidates, none of them is a dedicated
    synchronous standby,
  * a commit is acknowledged as soon as any two of the three standbys have
    flushed it, so losing one standby does not stop writes,
  * losing two standbys leaves only one quorum candidate and commits block
    until a second candidate comes back.
"""

import re
import time

import pytest

from conftest import (
    get_named_hosts,
    get_pg_owner,
    get_pg_service_name,
    get_pg_unix_socket_dir,
    get_pg_version,
    get_primary,
    load_ansible_vars,
)


# `ANY 2 ("standby1", "standby2")` / `FIRST 1 (standby1)` and friends.
SYNC_SPEC_RE = re.compile(
    r'^\s*(?P<method>ANY|FIRST)\s+(?P<num_sync>\d+)\s*\((?P<names>.*)\)\s*$',
    re.IGNORECASE,
)

# Table used by the write tests. Created on demand so that each test remains
# runnable on its own.
TEST_TABLE = 'quorum_replication_check'

# How long a commit that is expected to succeed may take before we call it a
# failure, and how long we watch a commit that is expected to block.
COMMIT_TIMEOUT = 60
BLOCKED_COMMIT_TIMEOUT = 25
# `timeout` exits with 124 when it had to kill the command.
TIMEOUT_EXIT_CODE = 124


def parse_sync_spec(spec):
    """
    Splits a synchronous_standby_names value into (method, num_sync, names).
    """
    match = SYNC_SPEC_RE.match(spec)

    assert match, \
        "synchronous_standby_names=%r is not a quorum/priority specification" \
        % spec

    names = [n.strip().strip('"') for n in match.group('names').split(',')]

    return (match.group('method').upper(), int(match.group('num_sync')), names)


def expected_sync_spec():
    """
    The quorum requested by the test case, as declared in vars.json.

    The case leaves `synchronous_standby_names` empty so the role has to build
    the value from `standby_quorum_type`, `synchronous_standby_num_sync` and
    `synchronous_standby_application_names`. Declaring the finished string
    instead would take the literal branch in `primary_synchronous_param.yml`
    and skip the generator and its validation asserts, so the expected value is
    assembled here from the same fields the role reads.

    A case that does pin the finished string is still honoured, so this helper
    keeps working for any variant that wants to test the literal branch.
    """
    ansible_vars = load_ansible_vars()

    pinned = ansible_vars.get('synchronous_standby_names', '')
    if pinned:
        return parse_sync_spec(pinned)

    names = ansible_vars.get('synchronous_standby_application_names', [])

    assert names, \
        "This test case must declare either synchronous_standby_names or " \
        "synchronous_standby_application_names in vars.json"

    return (
        ansible_vars.get('standby_quorum_type', 'ANY').upper(),
        int(ansible_vars.get('synchronous_standby_num_sync', 1)),
        names,
    )


def psql(host, query, database='postgres', timeout=None):
    """
    Runs a query as the Postgres owner over the unix socket and returns the
    testinfra command result. When timeout is set, the client is killed after
    that many seconds and the result carries TIMEOUT_EXIT_CODE.
    """
    command = 'psql -At -h %s -c "%s" %s' % (
        get_pg_unix_socket_dir(), query, database
    )

    if timeout is not None:
        command = 'timeout %d %s' % (timeout, command)

    with host.sudo(get_pg_owner()):
        return host.run(command)


def psql_output(host, query, database='postgres', timeout=None):
    """
    Same as psql() but fails the test when the query did not succeed, and
    returns the trimmed output.
    """
    result = psql(host, query, database=database, timeout=timeout)

    assert result.rc == 0, \
        "Query %r failed with rc=%d: %s" % (query, result.rc, result.stderr.strip())

    return result.stdout.strip()


def psql_rows(host, query, database='postgres'):
    """
    Returns the non empty output lines of a query.
    """
    output = psql_output(host, query, database=database)

    return [line for line in output.split('\n') if line]


def systemctl(host, action, service=None):
    """
    Drives the Postgres unit of a node. Tests connect as root, no sudo needed.
    """
    service = service or get_pg_service_name()

    return host.run('systemctl %s %s' % (action, service))


def wait_until(predicate, timeout=180, interval=3):
    """
    Polls predicate until it returns a truthy value. Returns the last value
    seen, truthy on success and falsy on timeout, so callers can build a
    meaningful assertion message.
    """
    deadline = time.monotonic() + timeout
    value = predicate()

    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = predicate()

    return value


def streaming_standbys(primary):
    """
    Application names of the standbys currently streaming from the primary, or
    None when the primary could not be queried. Callers poll this while nodes
    are being stopped and restarted, so a transient failure is not fatal.
    """
    result = psql(
        primary,
        "SELECT application_name FROM pg_stat_replication "
        "WHERE state = 'streaming'"
    )

    if result.rc != 0:
        return None

    return sorted(line for line in result.stdout.strip().split('\n') if line)


def wait_for_streaming_standbys(primary, count, timeout=180):
    """
    Waits until exactly count standbys are streaming and returns their names.
    """
    seen = []

    def streaming_count_reached():
        nonlocal seen
        seen = streaming_standbys(primary)
        return seen is not None and len(seen) == count

    assert wait_until(streaming_count_reached, timeout=timeout), \
        "Expected %d streaming standby(s) after %ds, found %s" \
        % (count, timeout, seen)

    return seen


def ensure_test_table(primary):
    """
    Creates the table used by the write tests, if it does not exist yet.

    This commits, so it must never be called while the quorum is unavailable.
    """
    psql_output(
        primary,
        "CREATE TABLE IF NOT EXISTS %s ("
        "id serial PRIMARY KEY, "
        "label text NOT NULL, "
        "created_at timestamptz NOT NULL DEFAULT now())" % TEST_TABLE,
        timeout=COMMIT_TIMEOUT,
    )


def insert_row(primary, label, timeout=COMMIT_TIMEOUT):
    """
    Inserts a labelled row with synchronous_commit explicitly enabled, so the
    commit waits for the quorum. Returns the testinfra command result.
    """
    return psql(
        primary,
        "SET synchronous_commit TO on; "
        "INSERT INTO %s (label) VALUES ('%s')" % (TEST_TABLE, label),
        timeout=timeout,
    )


def count_rows(host, label):
    """
    Number of rows carrying the given label as seen by a node, or None when the
    node could not be queried. A node that has just been restarted may still be
    starting up, which callers poll through rather than fail on.
    """
    result = psql(
        host,
        "SELECT count(*) FROM %s WHERE label = '%s'" % (TEST_TABLE, label)
    )

    if result.rc != 0:
        return None

    return int(result.stdout.strip())


def row_exists(host, label):
    """Whether the labelled row is visible right now, without waiting."""
    result = psql(
        host, "SELECT count(*) FROM %s WHERE label = '%s'" % (TEST_TABLE, label),
        timeout=30)
    return result.rc == 0 and result.stdout.strip() not in ('', '0')


def wait_for_row(host, label, timeout=120):
    """
    Waits until a node sees the labelled row. Returns True on success.
    """
    return bool(wait_until(lambda: count_rows(host, label) == 1, timeout=timeout))


@pytest.fixture(scope='module', autouse=True)
def restore_standbys():
    """
    Whatever the tests did, leave every standby running so the cluster stays
    usable for the remaining tests and for post mortem inspection.
    """
    yield

    for _, host in get_named_hosts('standby'):
        systemctl(host, 'start')



def insert_row_in_background(host, label):
    """
    Start a commit that is expected to block, without waiting for it.

    nohup plus a redirect so the connection outlives this command: the point is
    to leave a backend waiting, not to collect its output.
    """
    # Same statement as insert_row(), including the explicit
    # synchronous_commit, so the commit really does wait for the quorum.
    command = (
        "nohup psql -At -h %s -c \"SET synchronous_commit TO on; "
        "INSERT INTO %s (label) VALUES ('%s')\" postgres "
        ">/tmp/blocked_commit.out 2>&1 &"
        % (get_pg_unix_socket_dir(), TEST_TABLE, label)
    )
    with host.sudo(get_pg_owner()):
        return host.run(command)


def waiting_backends(host):
    """Every client backend and what it is waiting on, for failure messages."""
    result = psql(
        host,
        "SELECT coalesce(wait_event_type,'-')||'/'||coalesce(wait_event,'-')"
        "||' '||left(query,40) FROM pg_stat_activity "
        "WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()")
    return result.stdout.strip() or '(no client backends)'


def backend_waiting_on_syncrep(host, label):
    """
    True when a backend is blocked waiting for synchronous confirmation.

    'SyncRep' with wait_event_type 'IPC' is what PostgreSQL 17 reports for a
    commit waiting on synchronous_standby_names.
    """
    result = psql(
        host,
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE wait_event_type = 'IPC' AND wait_event = 'SyncRep' "
        "AND query LIKE '%%%s%%'" % label)
    return result.rc == 0 and result.stdout.strip() not in ('', '0')


def test_setup_quorum_replication_cluster_topology():
    primaries = get_named_hosts('primary')
    standbys = get_named_hosts('standby')
    (method, num_sync, sync_names) = expected_sync_spec()

    assert len(primaries) == 1, \
        "Expected a single primary, found %d" % len(primaries)

    assert len(primaries) + len(standbys) == 4, \
        "Expected a four node cluster, found %d node(s)" \
        % (len(primaries) + len(standbys))

    assert method == 'ANY', \
        "Expected a quorum (ANY) specification, found %s" % method

    assert 0 < num_sync < len(standbys), \
        "A quorum of %d over %d standby(s) does not exercise quorum commit" \
        % (num_sync, len(standbys))

    assert sorted(sync_names) == sorted([name for name, _ in standbys]), \
        "Quorum candidates %s do not match the standbys %s" \
        % (sorted(sync_names), sorted([name for name, _ in standbys]))


def test_setup_quorum_replication_service():
    service = get_pg_service_name()

    for name, host in get_named_hosts('primary') + get_named_hosts('standby'):
        assert host.service(service).is_running, \
            "Postgres service is not running on %s" % name

        assert host.service(service).is_enabled, \
            "Postgres service is not enabled on %s" % name


def test_setup_quorum_replication_postgres_version():
    expected = get_pg_version()

    for name, host in get_named_hosts('primary') + get_named_hosts('standby'):
        version = psql_output(host, 'SHOW server_version')

        assert version.split('.')[0] == str(expected), \
            "Expected Postgres %s on %s, found %s" % (expected, name, version)


def test_setup_quorum_replication_node_roles():
    (primary_name, primary) = get_named_hosts('primary')[0]

    assert psql_output(primary, 'SELECT pg_is_in_recovery()') == 'f', \
        "%s is in recovery, it is not a primary" % primary_name

    for name, host in get_named_hosts('standby'):
        assert psql_output(host, 'SELECT pg_is_in_recovery()') == 't', \
            "%s is not in recovery, it is not a standby" % name


def test_setup_quorum_replication_synchronous_standby_names():
    primary = get_primary()
    expected = expected_sync_spec()

    setting = psql_output(primary, 'SHOW synchronous_standby_names')

    assert setting, \
        "synchronous_standby_names is not set on the primary"

    (method, num_sync, names) = parse_sync_spec(setting)

    assert (method, num_sync, sorted(names)) == \
        (expected[0], expected[1], sorted(expected[2])), \
        "Deployed synchronous_standby_names %r does not match the requested " \
        "quorum %r" % (setting, expected)


def test_setup_quorum_replication_quorum_members():
    primary = get_primary()
    (_, num_sync, sync_names) = expected_sync_spec()

    wait_for_streaming_standbys(primary, len(sync_names))

    rows = psql_rows(
        primary,
        "SELECT application_name || ' ' || sync_state FROM pg_stat_replication "
        "WHERE state = 'streaming' ORDER BY application_name"
    )
    members = dict(row.split(' ', 1) for row in rows)

    assert sorted(members.keys()) == sorted(sync_names), \
        "Streaming standbys %s do not match the quorum candidates %s" \
        % (sorted(members.keys()), sorted(sync_names))

    for name in sync_names:
        assert members[name] == 'quorum', \
            "%s has sync_state=%s, expected quorum" % (name, members[name])

    # num_sync is the size of the quorum, not the number of candidates: every
    # candidate must be reported as a quorum member, including the extra ones.
    assert len(members) > num_sync, \
        "Expected more than %d quorum candidate(s), found %d" \
        % (num_sync, len(members))


def test_setup_quorum_replication_write_is_replicated():
    primary = get_primary()
    standbys = get_named_hosts('standby')
    label = 'all-standbys-up'

    wait_for_streaming_standbys(primary, len(standbys))

    ensure_test_table(primary)

    result = insert_row(primary, label)

    assert result.rc == 0, \
        "Commit did not complete with every standby up (rc=%d): %s" \
        % (result.rc, result.stderr.strip())

    assert count_rows(primary, label) == 1, \
        "The row is missing on the primary"

    for name, host in standbys:
        assert wait_for_row(host, label), \
            "The row was not replicated to %s" % name


def test_setup_quorum_replication_commits_with_quorum_available():
    """
    Losing one standby still leaves num_sync candidates, so commits must go
    through untouched.
    """
    primary = get_primary()
    standbys = get_named_hosts('standby')
    (_, num_sync, _) = expected_sync_spec()
    label = 'quorum-available'

    wait_for_streaming_standbys(primary, len(standbys))
    ensure_test_table(primary)

    stopped = standbys[num_sync:]
    running = standbys[:num_sync]

    for name, host in stopped:
        assert systemctl(host, 'stop').rc == 0, \
            "Could not stop Postgres on %s" % name

    try:
        wait_for_streaming_standbys(primary, len(running))

        # Enough candidates remain, so this must commit normally.
        result = insert_row(primary, label)

        assert result.rc == 0, \
            "Commit did not go through with the quorum still available: %s" \
            % (result.stdout + result.stderr).strip()
    finally:
        for name, host in stopped:
            systemctl(host, 'start')

    wait_for_streaming_standbys(primary, len(standbys))

    for name, host in stopped:
        assert wait_for_row(host, label), \
            "%s did not catch up after being restarted" % name


def test_setup_quorum_replication_blocks_without_quorum():
    """
    Losing one standby too many leaves fewer candidates than the quorum needs,
    so a commit must block until a candidate comes back.

    While it waits the row is not visible to other sessions, and it becomes
    visible on the primary and reaches every standby once the quorum is
    restored.
    """
    primary = get_primary()
    standbys = get_named_hosts('standby')
    (_, num_sync, _) = expected_sync_spec()
    label = 'quorum-lost'

    wait_for_streaming_standbys(primary, len(standbys))
    # Creating the table commits, so it has to happen while the quorum holds.
    ensure_test_table(primary)

    # Leave one candidate short of the quorum.
    stopped = standbys[num_sync - 1:]
    running = standbys[:num_sync - 1]

    for name, host in stopped:
        assert systemctl(host, 'stop').rc == 0, \
            "Could not stop Postgres on %s" % name

    try:
        wait_for_streaming_standbys(primary, len(running))

        # Observe the server blocking rather than killing psql after a timeout
        # and inferring it from exit code 124. A backend waiting for
        # synchronous confirmation reports wait_event 'SyncRep' with
        # wait_event_type 'IPC' -- confirmed against a real PostgreSQL 17.
        # pg_wait_events also lists Client/WaitForStandbyConfirmation, which is
        # not what a committing backend shows.
        #
        # This is exact, needs no timeout, and does not disconnect a client
        # mid-commit.
        insert_row_in_background(primary, label)

        assert wait_until(lambda: backend_waiting_on_syncrep(primary, label)), \
            "The commit did not enter a SyncRep wait one candidate short of " \
            "the quorum; pg_stat_activity shows %s" % waiting_backends(primary)

        # And it must NOT be visible while it waits.
        #
        # An earlier version asserted the opposite, reasoning that the commit
        # is "already committed locally". That phrase in the PostgreSQL
        # documentation means durable -- it will be committed after a crash --
        # not visible. RecordTransactionCommit() marks the xid committed in
        # clog before SyncRepWaitForLSN(), but ProcArrayEndTransaction() runs
        # after it, so other sessions' snapshots still see the xid in progress.
        #
        # Measured on PostgreSQL 17 with a commit blocked on an unreachable
        # standby: wait_event IPC/SyncRep, backend_xid still in
        # pg_stat_activity, and count(*) from the table 0 from another session.
        #
        # This direction is deterministic rather than timing-sensitive: the row
        # cannot become visible until the commit completes, and the commit
        # cannot complete until the quorum returns.
        assert not row_exists(primary, label), \
            "The blocked transaction is visible on the primary before its " \
            "commit completed"
    finally:
        for name, host in stopped:
            systemctl(host, 'start')

    wait_for_streaming_standbys(primary, len(standbys))

    # Once the quorum is back the commit completes, so the row becomes visible
    # on the primary and reaches every standby.
    assert wait_for_row(primary, label), \
        "The transaction never became visible on the primary after the " \
        "quorum was restored"

    for name, host in standbys:
        assert wait_for_row(host, label), \
            "%s did not receive the transaction after the quorum was restored" \
            % name
