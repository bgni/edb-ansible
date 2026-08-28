import pytest

from conftest import (
    get_pg_cluster_nodes,
    get_pg_type,
    get_pg_version,
    get_primary,
    get_standbys,
    get_pg_unix_socket_dir,
    load_ansible_vars,
    load_inventory,
)

def test_setup_replication_user():
    pg_user = 'postgres'
    pg_group = 'postgres'

    if get_pg_type() == 'EPAS':
        pg_user = 'enterprisedb'
        pg_group = 'enterprisedb'

    host = get_primary()
    socket_dir = get_pg_unix_socket_dir()
    
    with host.sudo(pg_user):
        query = "Select * from pg_user where usename = 'repuser' and userepl = 't'"
        cmd = host.run('psql -At -h %s -c "%s" postgres' % (socket_dir, query))
        result = cmd.stdout.strip()

    assert len(result) > 0, \
        "repuser was not succesfully created"

def test_setup_replication_slots():
    pg_user = 'postgres'

    if get_pg_type() == 'EPAS':
        pg_user = 'enterprisedb'

    host = get_primary()
    expected = set(load_inventory()['all']['children']['standby']['hosts'])
    socket_dir = get_pg_unix_socket_dir()
    
    with host.sudo(pg_user):
        query = "Select slot_name from pg_replication_slots"
        cmd = host.run('psql -At -h %s -c "%s" postgres' % (socket_dir, query))
        result = set(filter(None, cmd.stdout.strip().split('\n')))

    assert result == expected, \
        "Expected physical slots %s, got %s" % (expected, result)

def test_setup_replication_stat_replication():
    pg_user = 'postgres'

    if get_pg_type() == 'EPAS':
        pg_user = 'enterprisedb'

    host = get_primary()
    inventory = load_inventory()
    expected_names = set(inventory['all']['children']['standby']['hosts'])
    socket_dir = get_pg_unix_socket_dir()
    
    with host.sudo(pg_user):
        query = "Select application_name from pg_stat_replication"
        cmd = host.run('psql -At -h %s -c "%s" postgres' % (socket_dir, query))
        result = set(cmd.stdout.strip().split('\n'))

    assert result == expected_names, \
        "Expected replication applications %s, got %s" % (expected_names, result)


def test_setup_replication_synchronous_policy_on_every_node():
    ansible_vars = load_ansible_vars()
    expected_names = ansible_vars.get('synchronous_standby_application_names')
    if not expected_names:
        pytest.skip('Structured synchronous standby policy is not configured')

    expected = '%s %s (%s)' % (
        ansible_vars.get('standby_quorum_type', 'ANY').upper(),
        ansible_vars.get('synchronous_standby_num_sync', 1),
        ','.join('"%s"' % name for name in expected_names),
    )
    pg_user = 'enterprisedb' if get_pg_type() == 'EPAS' else 'postgres'
    socket_dir = get_pg_unix_socket_dir()

    for _, host in get_pg_cluster_nodes():
        with host.sudo(pg_user):
            cmd = host.run(
                'psql -At -h %s -c "SHOW synchronous_standby_names" postgres'
                % socket_dir
            )
        assert cmd.rc == 0
        assert cmd.stdout.strip() == expected


def test_setup_replication_uses_quorum_standbys():
    ansible_vars = load_ansible_vars()
    if ansible_vars.get('standby_quorum_type', '').upper() != 'ANY':
        pytest.skip('Quorum synchronous replication is not configured')

    pg_user = 'enterprisedb' if get_pg_type() == 'EPAS' else 'postgres'
    host = get_primary()
    socket_dir = get_pg_unix_socket_dir()
    with host.sudo(pg_user):
        query = "Select application_name from pg_stat_replication where sync_state = 'quorum'"
        cmd = host.run('psql -At -h %s -c "%s" postgres' % (socket_dir, query))
    result = set(cmd.stdout.strip().split('\n'))
    expected = set(load_inventory()['all']['children']['standby']['hosts'])

    assert cmd.rc == 0
    assert result == expected


def test_setup_replication_commit_survives_one_standby_down():
    ansible_vars = load_ansible_vars()
    if (
        get_pg_type() != 'PG'
        or ansible_vars.get('standby_quorum_type', '').upper() != 'ANY'
        or ansible_vars.get('synchronous_standby_num_sync') != 1
        or len(get_standbys()) < 2
    ):
        pytest.skip('The test requires PG with ANY 1 and at least two standbys')

    standby = get_standbys()[0]
    primary = get_primary()
    service = 'postgresql-%s' % get_pg_version()
    socket_dir = get_pg_unix_socket_dir()

    stop = standby.run('systemctl stop %s' % service)
    assert stop.rc == 0
    try:
        with primary.sudo('postgres'):
            commit = primary.run(
                "PGOPTIONS='-c statement_timeout=5000' "
                "psql -v ON_ERROR_STOP=1 -At -h %s -d postgres "
                "-c \"CREATE TABLE IF NOT EXISTS public.edb_ansible_quorum_probe "
                "(id bigint generated always as identity, created_at timestamptz default now()); "
                "INSERT INTO public.edb_ansible_quorum_probe DEFAULT VALUES;\""
                % socket_dir
            )
        assert commit.rc == 0, commit.stderr
    finally:
        start = standby.run('systemctl start %s' % service)
        assert start.rc == 0
        ready = standby.run(
            "for i in $(seq 1 30); do "
            "sudo -u postgres psql -At -h %s -d postgres "
            "-c 'SELECT status FROM pg_stat_wal_receiver' 2>/dev/null "
            "| grep -qx streaming && exit 0; "
            "sleep 1; done; exit 1" % socket_dir
        )
        assert ready.rc == 0

def test_setup_replication_stat_wal_receiver():
    pg_user = 'postgres'

    if get_pg_type() == 'EPAS':
        pg_user = 'enterprisedb'

    hosts = get_standbys()
    expected_slots = list(
        load_inventory()['all']['children']['standby']['hosts']
    )
    socket_dir = get_pg_unix_socket_dir()

    for expected_slot, host in zip(expected_slots, hosts):
        with host.sudo(pg_user):
            query = "Select slot_name from pg_stat_wal_receiver"
            cmd = host.run('psql -At -h %s -c "%s" postgres' % (socket_dir, query))
            result = cmd.stdout.strip()

        assert cmd.rc == 0
        assert result == expected_slot, \
            "Expected WAL receiver slot %s, got %s" % (expected_slot, result)
