#!/usr/bin/env python3
"""
Check that everything a test run needs is reachable before starting one.

Written for air-gapped and mirrored environments, where a run that fails
twenty minutes in because one repository is missing is expensive. Every
external resource the harness touches is listed here with the environment
variable that redirects it, so an unset variable is reported as "will use the
public default" rather than silently passing.

    python3 tests/scripts/check-resources.py
    python3 tests/scripts/check-resources.py --case prod_topology
    python3 tests/scripts/check-resources.py --json

Exit status is 0 when every required resource is reachable, 1 otherwise.

Only the standard library is used, so this runs on a bare host before any of
the tooling is installed.
"""

import argparse
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request


TIMEOUT = 10

OK = 'OK'
FAIL = 'FAIL'
WARN = 'WARN'
SKIP = 'SKIP'


class Check:
    def __init__(self, name, status, detail, hint=None, required=True):
        self.name = name
        self.status = status
        self.detail = detail
        self.hint = hint
        self.required = required


def http_reachable(url, insecure=False):
    """Return (ok, detail) for a URL, treating any HTTP response as reachable."""
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    request = urllib.request.Request(url, method='GET')
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=ctx) as r:
            return True, 'HTTP %s' % r.status
    except urllib.error.HTTPError as exc:
        # 401/403/404 still prove the host answered, which is what matters for
        # a mirror root that does not serve a directory listing.
        return True, 'HTTP %s' % exc.code
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except (socket.timeout, TimeoutError):
        return False, 'timed out after %ds' % TIMEOUT
    except Exception as exc:  # noqa: BLE001 - report anything else verbatim
        return False, '%s: %s' % (type(exc).__name__, exc)


def check_command(name, binary, hint, required=True):
    path = shutil.which(binary)
    if path:
        return Check(name, OK, path, required=required)
    return Check(name, FAIL if required else WARN, 'not on PATH', hint,
                 required=required)


def check_container_engine():
    engine = os.environ.get('CONTAINER_ENGINE')
    if not engine:
        engine = 'podman' if shutil.which('podman') else (
            'docker' if shutil.which('docker') else None)

    if not engine:
        return [Check('container engine', FAIL, 'neither podman nor docker found',
                      'install podman or docker, or set CONTAINER_ENGINE')]

    checks = [Check('container engine', OK, engine)]

    try:
        proc = subprocess.run([engine, 'info', '--format', '{{.Host.CgroupsVersion}}'],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            # docker without a running daemon, or podman without its socket
            checks.append(Check(
                '%s daemon/socket' % engine, FAIL,
                (proc.stderr or proc.stdout).strip().splitlines()[-1:] or 'not responding',
                'for rootless podman: systemctl --user enable --now podman.socket'))
        else:
            checks.append(Check('%s responding' % engine, OK,
                                'cgroups %s' % proc.stdout.strip()))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check('%s responding' % engine, FAIL, str(exc)))

    # compose is a separate binary/plugin and is what actually drives the run
    try:
        proc = subprocess.run([engine, 'compose', 'version'],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            checks.append(Check('%s compose' % engine, OK,
                                proc.stdout.strip().splitlines()[0]))
        else:
            checks.append(Check(
                '%s compose' % engine, FAIL,
                (proc.stderr or proc.stdout).strip().splitlines()[-1] if
                (proc.stderr or proc.stdout).strip() else 'not available',
                'install the compose plugin, or podman-compose'))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check('%s compose' % engine, FAIL, str(exc)))

    return checks


def check_image(engine, image, label):
    """An image counts as available if it is already pulled or can be pulled."""
    try:
        proc = subprocess.run([engine, 'image', 'exists', image],
                             capture_output=True, timeout=30)
        if proc.returncode == 0:
            return Check(label, OK, '%s (present locally)' % image)
    except Exception:  # noqa: BLE001 - docker has no 'image exists'
        pass

    try:
        proc = subprocess.run([engine, 'image', 'inspect', image],
                              capture_output=True, timeout=30)
        if proc.returncode == 0:
            return Check(label, OK, '%s (present locally)' % image)
    except Exception:  # noqa: BLE001
        pass

    return Check(label, WARN, '%s not present locally' % image,
                 'pull it, or point RHEL_BASE_IMAGE/TESTER_BASE_IMAGE at your registry')


def env_backed_url(var, default, label, path=''):
    """
    Check the URL an environment variable points at, or the public default.

    Returns a Check plus whether the value came from the environment, so the
    report can say when a public endpoint would be used in an air-gapped run.
    """
    value = os.environ.get(var, '').strip()
    using_default = not value
    url = (value or default).rstrip('/') + path

    if not url or url == path:
        return Check(label, SKIP, '%s unset and no default' % var, required=False)

    insecure = os.environ.get('CHECK_INSECURE', '').lower() in ('1', 'true', 'yes')
    ok, detail = http_reachable(url, insecure=insecure)

    if ok:
        suffix = ' (public default -- set %s for air-gapped use)' % var if using_default else ''
        return Check(label, OK if not using_default else WARN,
                     '%s -> %s%s' % (url, detail, suffix),
                     required=not using_default)

    return Check(label, FAIL, '%s -> %s' % (url, detail),
                 'set %s to a reachable mirror' % var)


def collection_repo_urls(repo_root):
    """
    The URLs the collection itself fetches at deploy time, read from
    setup_repo's defaults so this stays in step with the role.

    These are Ansible variables, so they are overridden in a case's vars.json
    rather than by an environment variable.
    """
    defaults = os.path.join(repo_root, 'roles', 'setup_repo', 'defaults', 'main.yml')
    wanted = {
        'pg_rpm_repo_9_x86_64': 'PGDG repository RPM (EL9)',
        'pg_gpg_key_9_x86_64': 'PGDG GPG key (EL9)',
        'epel_repo_9': 'EPEL repository RPM (EL9)',
        'epel_gpg_key_9': 'EPEL GPG key (EL9)',
    }
    found = {}
    try:
        with open(defaults, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                for key, label in wanted.items():
                    if line.startswith('%s:' % key):
                        url = line.split(':', 1)[1].strip().strip('"\'')
                        if url.startswith('http'):
                            found[label] = (key, url)
    except OSError:
        pass
    return found


def run_checks(repo_root, case=None):
    checks = []

    checks.append(check_command('python3', 'python3',
                                'required by the harness scripts'))
    checks.append(check_command(
        'ansible-galaxy', 'ansible-galaxy',
        'install ansible-core on this host; run-case.sh builds the collection with it'))
    checks.append(check_command('git', 'git', 'optional', required=False))

    engine_checks = check_container_engine()
    checks.extend(engine_checks)
    engine = engine_checks[0].detail if engine_checks[0].status == OK else None

    if engine:
        rhel_tag = os.environ.get('RHEL9_IMAGE_TAG', '9.7')
        rhel_image = os.environ.get(
            'RHEL_BASE_IMAGE',
            'registry.access.redhat.com/ubi9/ubi-init:%s' % rhel_tag)
        tester_image = os.environ.get('TESTER_BASE_IMAGE', 'debian:11-slim')
        checks.append(check_image(engine, rhel_image, 'database node base image'))
        checks.append(check_image(engine, tester_image, 'tester base image'))

    # Package and module sources used while building the images.
    checks.append(env_backed_url(
        'PIP_INDEX_URL', 'https://pypi.org/simple', 'Python package index'))
    checks.append(env_backed_url(
        'ANSIBLE_GALAXY_SERVER', 'https://galaxy.ansible.com',
        'Ansible Galaxy'))

    yum_baseurl = os.environ.get('YUM_BASEURL', '').strip()
    if yum_baseurl:
        ok, detail = http_reachable(yum_baseurl)
        checks.append(Check('yum mirror', OK if ok else FAIL,
                            '%s -> %s' % (yum_baseurl, detail),
                            None if ok else 'set YUM_BASEURL to a reachable mirror'))
    else:
        checks.append(Check(
            'yum mirror', WARN,
            'YUM_BASEURL unset -- the image will use the repositories its base '
            'image ships, which need internet access',
            'set YUM_BASEURL for air-gapped use', required=False))

    apt_mirror = os.environ.get('APT_MIRROR', '').strip()
    if apt_mirror:
        url = apt_mirror if apt_mirror.startswith('http') else 'http://%s' % apt_mirror
        ok, detail = http_reachable(url)
        checks.append(Check('apt mirror', OK if ok else FAIL,
                            '%s -> %s' % (url, detail),
                            None if ok else 'set APT_MIRROR to a reachable mirror'))
    else:
        checks.append(Check(
            'apt mirror', WARN,
            'APT_MIRROR unset -- the tester image will use deb.debian.org',
            'set APT_MIRROR for air-gapped use', required=False))

    # What the collection fetches during the deploy itself.
    for label, (var, url) in sorted(collection_repo_urls(repo_root).items()):
        ok, detail = http_reachable(url)
        checks.append(Check(
            'collection: %s' % label,
            OK if ok else FAIL,
            '%s -> %s' % (url, detail),
            None if ok else
            'override %s in the case vars.json to point at your mirror' % var))

    if case:
        case_dir = os.path.join(repo_root, 'tests', 'cases', case)
        if os.path.isdir(case_dir):
            checks.append(Check('case %s' % case, OK, case_dir))
        else:
            checks.append(Check('case %s' % case, FAIL, 'no such case directory',
                                'see tests/run-case.sh --list'))

    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--case', help='also check that this test case exists')
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    args = parser.parse_args()

    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

    checks = run_checks(repo_root, case=args.case)

    if args.json:
        print(json.dumps([{
            'name': c.name, 'status': c.status, 'detail': c.detail,
            'hint': c.hint, 'required': c.required,
        } for c in checks], indent=2))
    else:
        width = max(len(c.name) for c in checks)
        print()
        print('Resource availability')
        print('=' * (width + 60))
        for c in checks:
            print('  %-6s %-*s  %s' % (c.status, width, c.name, c.detail))
            if c.hint and c.status in (FAIL, WARN):
                print('  %-6s %-*s  -> %s' % ('', width, '', c.hint))
        print()

    failures = [c for c in checks if c.status == FAIL]
    warnings = [c for c in checks if c.status == WARN]

    if not args.json:
        print('%d ok, %d warning(s), %d failure(s)' % (
            len([c for c in checks if c.status == OK]), len(warnings), len(failures)))
        if warnings and not failures:
            print('Warnings are fine on a connected host. In an air-gapped '
                  'environment every warning above is a resource that will not '
                  'resolve.')
        print()

    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
