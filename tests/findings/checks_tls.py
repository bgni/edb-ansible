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
    TLS-02: client-certificate authentication (mTLS) is never configured.
    `ssl=on` plus a server certificate proves transport encryption, not client
    identity. Without `clientcert=verify-full` in pg_hba, any client that
    trusts the server CA can still authenticate by password alone.
    """
    evidence = []

    # Any active clientcert setting anywhere?
    clientcert = repo.grep(r'clientcert',
                           subdirs=('roles', 'playbook-examples'),
                           suffixes=('.yml', '.j2', '.template'))
    active = [(rel, n, line) for rel, n, line in clientcert
              if not line.strip().startswith('#')]

    for rel, n, line in clientcert[:8]:
        evidence.append(quote(rel, n, line))

    # pg_ident identity mapping, the other half of mTLS.
    ident = repo.grep(r'pg_ident|ident_map',
                      subdirs=('roles/init_dbserver', 'roles/manage_dbserver'),
                      suffixes=('.yml', '.j2', '.template'))
    for rel, n, line in ident[:4]:
        evidence.append(quote(rel, n, line))

    if active:
        return Result('TLS-02', FIXED,
                      'clientcert authentication is configured', evidence)

    if not clientcert:
        evidence.append(missing('clientcert'))

    return Result(
        'TLS-02', PRESENT,
        'clientcert=verify-full is never set (the only occurrences are '
        'commented-out examples), so the deployment gets transport encryption '
        'but not client-certificate identity', evidence)


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
