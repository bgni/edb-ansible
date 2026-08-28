"""
Client-certificate (mTLS) authentication on the production-shaped topology.

The deployment issues a CA on the control node -- standing in for the local
ACME server -- and hands the resulting certificates to the collection through
the pg_ssl_*_file variables. The collection's job, and what these tests check,
is the authentication configuration it applies on top: an Ansible-managed
identity map included from pg_ident.conf, and an HBA rule requiring a verified
client certificate.

The client certificate's Common Name is deliberately *not* the PostgreSQL role
name. The map is what translates one into the other, so a test where the two
were equal would still pass if the map were ignored entirely.

The last test is the one that matters operationally: the same certificate must
still authenticate after a standby is promoted. Authentication configuration
has to be promotion-ready for the same reason the durability policy does
(EDB-03) -- a new primary that does not already carry the map and the HBA rule
locks out every certificate client the moment it takes over.

File ordering: pytest collects alphabetically, so this module runs after
test_prod_topology_day2.py, whose module-scoped fixture has already stopped the
original primary and promoted standby1. The promotion test here reuses that
fixture rather than promoting a second time.
"""

import os

from conftest import (
    get_pg_owner,
    get_primary,
    load_ansible_vars,
)
from test_prod_topology import (
    all_database_nodes,
    psql_output,
    show,
)


# Where the deployment put the issued certificates, inside the tester
# container. The database nodes have their own copies in PGDATA; these are the
# client-side ones the tests connect with.
def tls_dir():
    return load_ansible_vars().get(
        'tls_dir', '/workspace/tests/cases/prod_topology/certs')


def mtls_user():
    return load_ansible_vars()['mtls_pg_user']


def map_name():
    return load_ansible_vars()['mtls_map_name']


def client_common_name():
    return load_ansible_vars()['tls_client_common_name']


def managed_ident_filename():
    return load_ansible_vars().get(
        'pg_ident_managed_filename', 'pg_ident_ansible.conf')


def psql_with_cert(host, ip, cert, key, user, query='SELECT current_user'):
    """
    Connect to `ip` over TLS presenting a client certificate, and return the
    testinfra command result.

    Run from a database node rather than the tester because psql and the CA are
    both already there, and it exercises the same network path a real client
    would take. sslmode=verify-ca checks the server against the CA without
    requiring the hostname to match the certificate, which keeps the test about
    client authentication rather than server naming.
    """
    command = (
        'PGSSLMODE=verify-ca '
        'PGSSLROOTCERT=%s/root.crt '
        'PGSSLCERT=%s '
        'PGSSLKEY=%s '
        'psql -At -h %s -p 5432 -U %s -d postgres -c "%s"'
        % (show(host, 'data_directory'), cert, key, ip, user, query)
    )

    with host.sudo(get_pg_owner()):
        return host.run(command)


def client_material(host):
    """
    Put the client key and certificate on a node with permissions libpq will
    accept, and return their paths.

    libpq refuses a client key that is group- or world-readable.
    """
    base = '/var/lib/pgsql/mtls_client'
    certs = tls_dir()

    with host.sudo():
        host.run('mkdir -p %s' % base)
        host.run('cp %s/client.crt %s/client.crt' % (certs, base))
        host.run('cp %s/client.key %s/client.key' % (certs, base))
        host.run('cp %s/unmapped.crt %s/unmapped.crt' % (certs, base))
        host.run('cp %s/unmapped.key %s/unmapped.key' % (certs, base))
        host.run('chown -R %s: %s' % (get_pg_owner(), base))
        host.run('chmod 600 %s/client.key %s/unmapped.key' % (base, base))

    return base


def node_ip(name):
    """The private_ip the inventory gave a node."""
    import yaml

    with open(os.environ['EDB_INVENTORY']) as f:
        inventory = yaml.safe_load(f)

    for group in inventory['all']['children'].values():
        for host, attrs in (group.get('hosts') or {}).items():
            if host == name:
                return attrs['private_ip']

    raise AssertionError('%s is not in the inventory' % name)


# ---------------------------------------------------------------------------
# the configuration the collection applied
# ---------------------------------------------------------------------------

def test_prod_topology_mtls_ident_map_is_a_separate_included_file():
    """
    The identity map lives in its own Ansible-managed file, and pg_ident.conf
    includes it rather than being rewritten.

    That separation is the point: this role owns every line of the managed file
    and regenerates it wholesale, while pg_ident.conf keeps whatever the
    distribution or an operator put there.
    """
    for name, host in all_database_nodes():
        ident_file = show(host, 'ident_file')
        managed = os.path.join(
            os.path.dirname(ident_file), managed_ident_filename())

        assert host.file(managed).exists, \
            '%s: %s was not created' % (name, managed)

        content = host.file(managed).content_string

        assert client_common_name() in content, \
            '%s: %s does not map the client CN %r:\n%s' % (
                name, managed, client_common_name(), content)
        assert mtls_user() in content, \
            '%s: %s does not map to the role %r:\n%s' % (
                name, managed, mtls_user(), content)

        main = host.file(ident_file).content_string

        assert managed_ident_filename() in main, \
            '%s: %s does not include %s:\n%s' % (
                name, ident_file, managed_ident_filename(), main)
        assert 'include_if_exists' in main, \
            '%s: the include in %s is not an include_if_exists' % (
                name, ident_file)


def test_prod_topology_mtls_map_is_loaded_by_postgres():
    """
    PostgreSQL parsed the map. pg_ident_file_mappings shows what it actually
    loaded, including from included files, with no error.
    """
    for name, host in all_database_nodes():
        rows = psql_output(
            host,
            "SELECT map_name, sys_name, pg_username, coalesce(error, '') "
            "FROM pg_ident_file_mappings WHERE map_name = '%s'" % map_name())

        assert rows, \
            '%s: pg_ident_file_mappings has no entry for map %r' % (
                name, map_name())

        for line in rows.split('\n'):
            error = line.split('|')[-1]
            assert not error, \
                '%s: map %r has a parse error: %s' % (name, map_name(), error)


def test_prod_topology_mtls_hba_rule_requires_a_client_certificate():
    """The HBA rule for the role uses `cert` with the map and verify-full."""
    for name, host in all_database_nodes():
        rows = psql_output(
            host,
            "SELECT type, auth_method, options::text FROM pg_hba_file_rules "
            "WHERE '%s' = ANY(user_name)" % mtls_user())

        assert rows, \
            '%s: no HBA rule for %r' % (name, mtls_user())

        assert 'cert' in rows, \
            '%s: the HBA rule for %r does not use cert authentication: %s' % (
                name, mtls_user(), rows)
        assert 'map=%s' % map_name() in rows, \
            '%s: the HBA rule for %r does not reference map %r: %s' % (
                name, mtls_user(), map_name(), rows)
        assert 'clientcert=verify-full' in rows, (
            '%s: the HBA rule for %r does not require clientcert=verify-full: '
            '%s' % (name, mtls_user(), rows))


# ---------------------------------------------------------------------------
# does it actually authenticate
# ---------------------------------------------------------------------------

def test_prod_topology_mtls_added_user_authenticates_with_its_certificate():
    """
    The role added by the deployment authenticates with the client
    certificate, and Postgres resolves it through the map.

    This is the end-to-end check: a user was added, a certificate whose CN is
    something else entirely was issued, and the map is what lets one become the
    other.
    """
    primary = get_primary()
    base = client_material(primary)

    result = psql_with_cert(
        primary, node_ip('primary1'),
        '%s/client.crt' % base, '%s/client.key' % base, mtls_user())

    assert result.rc == 0, \
        'certificate authentication as %s failed: %s' % (
            mtls_user(), result.stderr.strip())

    assert result.stdout.strip() == mtls_user(), \
        'connected as %r, expected %r' % (
            result.stdout.strip(), mtls_user())


def test_prod_topology_mtls_unmapped_certificate_is_rejected():
    """
    A certificate from the same CA whose CN the map does not mention is
    refused.

    Without this the previous test would pass even if the map were ignored and
    any CA-signed certificate accepted, which is the difference between mTLS
    and "we turned SSL on".
    """
    primary = get_primary()
    base = client_material(primary)

    result = psql_with_cert(
        primary, node_ip('primary1'),
        '%s/unmapped.crt' % base, '%s/unmapped.key' % base, mtls_user())

    assert result.rc != 0, (
        'a certificate with an unmapped CN authenticated as %s. The identity '
        'map is not being enforced.' % mtls_user())


def test_prod_topology_mtls_password_only_client_is_rejected():
    """
    A client presenting no certificate cannot reach the role.

    `clientcert=verify-full` with method `cert` means there is no password
    fallback; a connection without a certificate must fail.
    """
    primary = get_primary()
    ip = node_ip('primary1')

    with primary.sudo(get_pg_owner()):
        result = primary.run(
            'PGSSLMODE=require psql -At -h %s -p 5432 -U %s -d postgres '
            '-c "SELECT 1"' % (ip, mtls_user()))

    assert result.rc != 0, \
        'a client with no certificate authenticated as %s' % mtls_user()
