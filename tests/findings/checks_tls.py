"""
Finding checks for the TLS/PKI layer of the assumed deployment.

The target deployment is PostgreSQL 17 + pgBackRest + repmgr + HAProxy +
certbot with a local ACME server issuing both client and server certificates.
These checks establish what the collection actually provides against that
assumption. They are not from either source review; they carry TLS-* IDs.
"""

from harness import PRESENT, FIXED, PARTIAL, NA, Result, quote, missing


def check_tls_01(repo):
    """
    TLS-01: the collection has no certbot/ACME integration. Certificates must
    be issued outside it and handed in as file paths, so an ACME renewal has
    no path back into PostgreSQL (no reload on renew, no CRL refresh).
    """
    hits = repo.grep(r'certbot|acme|letsencrypt|dehydrated|step-ca',
                     subdirs=('roles', 'plugins', 'playbook-examples'),
                     suffixes=('.yml', '.j2', '.template', '.py'),
                     flags=2)  # re.IGNORECASE

    if hits:
        evidence = [quote(rel, n, line) for rel, n, line in hits[:10]]
        return Result('TLS-01', FIXED,
                      'an ACME/certbot integration exists in the collection',
                      evidence)

    # What it offers instead: copy pre-issued files into PGDATA.
    evidence = [missing('certbot / acme / letsencrypt')]
    send = repo.grep(r'pg_ssl_(cert|key|ca|crl)_file',
                     subdirs=('roles/init_dbserver',),
                     suffixes=('.yml',))
    for rel, n, line in send[:6]:
        evidence.append(quote(rel, n, line))

    return Result(
        'TLS-01', PRESENT,
        'no certbot/ACME integration exists; certificates must be issued '
        'externally and supplied as pg_ssl_*_file paths, and nothing reloads '
        'PostgreSQL or refreshes the CRL when ACME renews them', evidence)


def check_tls_02(repo):
    """
    TLS-02: client-certificate authentication (mTLS).

    Two separate questions, and they moved apart:

    1. Can the collection express mTLS at all? It needs somewhere to declare an
       identity map, and an HBA writer that can emit the `map=` and
       `clientcert=` options after the method.
    2. Is it on by default? `ssl=on` plus a server certificate is transport
       encryption; without a `cert` HBA rule any client that trusts the server
       CA still authenticates by password alone.

    A deployment that wants mTLS must opt in, so the shipped default is still
    password authentication over TLS.
    """
    evidence = []

    # (1) the mechanism: identity maps and HBA auth options.
    ident_maps = repo.grep(r'^\s*pg_ident_maps\s*:',
                           subdirs=('roles/manage_dbserver',),
                           suffixes=('.yml',))
    ident_tasks = repo.grep(r'ident_file|pg_ident_managed_filename',
                            subdirs=('roles/manage_dbserver/tasks',),
                            suffixes=('.yml',))
    hba_options = repo.grep(r'^\s*options:\s*"\{\{\s*line_item\.options',
                            subdirs=('roles/manage_dbserver/tasks',),
                            suffixes=('.yml',))

    for rel, hits in (('', ident_maps), ('', ident_tasks), ('', hba_options)):
        for r, n, line in hits[:3]:
            evidence.append(quote(r, n, line))

    has_mechanism = bool(ident_maps) and bool(ident_tasks) and bool(hba_options)

    # (2) is any shipped default or example actually turning it on?
    clientcert = repo.grep(r'clientcert',
                           subdirs=('roles', 'playbook-examples'),
                           suffixes=('.yml', '.j2', '.template'))
    enabled_by_default = [(rel, n, line) for rel, n, line in clientcert
                          if not line.strip().lstrip('-').strip().startswith('#')]

    for rel, n, line in clientcert[:4]:
        evidence.append(quote(rel, n, line))

    if not has_mechanism:
        if not clientcert:
            evidence.append(missing('clientcert'))
        return Result(
            'TLS-02', PRESENT,
            'the collection cannot express mTLS: no identity-map support and '
            'no way to emit clientcert/map options into pg_hba', evidence)

    if enabled_by_default:
        return Result('TLS-02', FIXED,
                      'mTLS is supported and enabled by a shipped default',
                      evidence)

    return Result(
        'TLS-02', PARTIAL,
        'mTLS is now expressible -- pg_ident_maps renders an Ansible-managed '
        'map file that pg_ident.conf includes, and the HBA writer can emit '
        'map=/clientcert= options -- but no shipped default turns it on, so '
        'the out-of-the-box deployment is still password authentication over '
        'TLS', evidence)


def check_tls_03(repo):
    """
    TLS-03: when no certificate files are supplied, the collection generates a
    self-signed CA and server certificate *inside the database*, via the
    `sslutils` extension. That makes the TLS bootstrap depend on an extra
    package being installable for the target PostgreSQL major version, and it
    produces a private CA that an external ACME setup would not use.

    This records the dependency; whether `sslutils_17` actually installs on
    RHEL 9 from the configured repositories is a live question, covered by the
    deployment test, not by static analysis.
    """
    evidence = []

    gen = repo.grep(r'openssl_rsa_generate_key|openssl_csr_to_crt',
                    subdirs=('roles/init_dbserver',),
                    suffixes=('.yml',))
    for rel, n, line in gen[:4]:
        evidence.append(quote(rel, n, line))

    pkg = repo.grep(r'sslutils_\{\{ pg_version \}\}|sslutils',
                    subdirs=('roles/install_dbserver',),
                    suffixes=('.yml',))
    for rel, n, line in pkg[:4]:
        evidence.append(quote(rel, n, line))

    default = repo.find('roles/install_dbserver/defaults/main.yml', r'^pg_ssl:')
    for n, line in default:
        evidence.append(
            quote('roles/install_dbserver/defaults/main.yml', n, line))

    if not gen:
        return Result('TLS-03', NA,
                      'no in-database certificate generation path exists',
                      evidence or [missing('openssl_rsa_generate_key')])

    ssl_on = any('true' in line.lower() for _, line in default)
    return Result(
        'TLS-03', PRESENT if ssl_on else PARTIAL,
        'certificates are generated inside the database through the sslutils '
        'extension, so TLS bootstrap depends on sslutils being installable for '
        'the target major version and yields a private self-signed CA rather '
        'than the ACME-issued chain the deployment assumes'
        + (' (pg_ssl defaults to true)' if ssl_on else ''),
        evidence)


CHECKS = (
    check_tls_01,
    check_tls_02,
    check_tls_03,
)
