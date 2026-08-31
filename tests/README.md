# edb-ansible tests

## Introduction

This folder contains the necessary software infrastructure required to execute
the non-regression test cases of the `edb-ansible` Ansible Collection.

The tests are grouped by a common subject into multiple *test cases*. This
common subject could be related to a specific Role we want to test, or a
combination of several Roles with particular parameters for example.

This testing framework relies mainly on containers, Compose, and `pytest`.
The RHEL 9 cases run under both Docker and rootless Podman; older cases may
still require Docker.

## What is automated, and what is not

Automation is scoped to the deployment this fork actually runs: **PostgreSQL 17
on RHEL 9**, with pgBackRest, repmgr and HAProxy. Three cases are automated,
and between them they exercise every role in that deployment:

| Case | Covers | Runs |
|---|---|---|
| `setup_replication` | Core streaming replication, four nodes | Every push |
| `setup_quorum_replication` | `ANY 2` quorum, including degraded-quorum writes | Every push |
| `prod_topology` | Full stack: repmgr witness, pgBackRest, HAProxy | Manual dispatch |

Plus `tests/findings/`, which checks the source-review findings statically on
every push and needs no containers at all.

**Not automated:** PostgreSQL majors below 17, any OS other than RHEL 9, and
the components we do not deploy — pgBouncer, pgPool-II, EFM, Patroni, PEM,
Barman, PGD, CloudNativePG, TDE, binary upgrade, and the DBT-2/3/7, HammerDB
and touchstone benchmark harnesses.

Those roles and their test cases are still in the tree and still work; they are
simply not run automatically. To run one by hand:

```shell
$ make -C tests/cases/setup_pgbouncer rocky8
```

`tests/config.yml` and the `--pg-version` / `--os` validators in
`test-runner.py` are scoped to match, so a stray `--pg-version 14` fails fast
rather than running a matrix nobody reads. The previous full matrix is in git
history.

## Running a case without `make`

`tests/run-case.sh` does everything the Makefile targets did — build the
collection tarball, start the node containers, set up SSH between them, render
the inventory, then run the playbook and pytest:

```shell
$ ./tests/run-case.sh --list                       # available cases
$ ./tests/run-case.sh prod_topology                # os defaults to rhel9
$ ./tests/run-case.sh setup_replication rhel9
$ ./tests/run-case.sh prod_topology rhel9 --clean  # tear down
```

`make` is not required. `ansible-galaxy` is, on the host, because the tester
container installs the collection from a tarball built there.

Set `KEEP_CONTAINERS=true` to leave the cluster running after the tests, which
is useful when a test fails and you want to inspect the nodes.

### Iterating on a test without redeploying

A full run is about fourteen minutes, of which the tests are roughly seventy
seconds. `--tests-only` reuses a cluster that is already up:

```shell
$ KEEP_CONTAINERS=true ./tests/run-case.sh prod_topology
$ PYTEST_ARGS='-k mtls_unmapped' ./tests/run-case.sh prod_topology --tests-only
```

Narrowed like that it takes seconds rather than minutes.

**The suite is destructive, so narrow it.** The day-two tests stop the primary
and promote a standby, and they do not put the cluster back. Re-running the
*whole* suite against a cluster that has already been through it fails in bulk
-- most tests expect the deployed topology and find a promoted one, which looks
alarming and has nothing to do with whatever you were changing. Measured on the
`prod_topology` case:

| | Time | Result on an already-tested cluster |
|---|---|---|
| Full deploy and run | ~14 min | 34 passed |
| `--tests-only`, whole suite | ~3.3 min | 22 failed, 12 passed |
| `--tests-only` with `PYTEST_ARGS` | ~6 s | the test you asked for |

So `--tests-only` is for iterating on a specific test. For a clean full run,
redeploy. The script warns when `PYTEST_ARGS` is unset.

The Makefile targets still work and are unchanged; the script is an
alternative entry point, not a replacement.

## Cluster lifecycle test

The end-to-end test of the collection in your working tree: provision a
cluster, change its configuration while it is running, then kill the primary
and check that nothing acknowledged was lost.

```shell
# everything, on a machine with enough memory
./tests/run-cluster-lifecycle.sh

# on a small machine: no dnf, serialised
ANSIBLE_FORKS=2 ./tests/run-cluster-lifecycle.sh --preinstalled --keep

# one phase at a time while iterating
./tests/run-cluster-lifecycle.sh --phase provision,reconfigure --keep
./tests/run-cluster-lifecycle.sh --clean
```

Six containers: `postgres01`-`04`, a repmgr witness, and a dedicated pgBackRest
repository host. The repository needs its own node -- co-locating it on a
database node makes `setup_pgbackrestserver` give the postgres user
`pgbackrest` as its *primary* group, after which `manage_dbserver` cannot
restore `~/.pgpass` and every re-run fails.

The collection is rebuilt from the working tree on every run, so what is
exercised is your changes rather than a published release. The revision, marked
`+dirty` when the tree is not clean, is recorded in `results/under-test.json`.

### The three phases

| Phase | What it proves |
|---|---|
| `provision` | one writable primary and three streaming standbys under their own application names, the cluster serving reads and writes, repmgr's topology correct, the pgBackRest stanza checking out, and the failover mode matching what was configured |
| `reconfigure` | re-running the playbook with `synchronous_standby_names` changed from `*` to a named `ANY 1` converges the **running** cluster: the policy in force on every promotion candidate, the running value matching the applied file, nothing pending a restart, no node restarted, and a commit still succeeding with one standby down |
| `failover` | with a writer running and the primary SIGKILLed mid-transaction, every *acknowledged* transaction is present on the promoted node |

Phase 2 is the one initial deployment cannot substitute for. Deploying straight
to the final value proves nothing about applying a change to a cluster that is
already running and serving, which is the operation people actually perform.

### What the failover phase is careful about

**Abrupt, then fenced.** `systemctl stop` alone is graceful and flushes
everything, which tests nothing about data loss. SIGKILL alone is not a fence:
the unit ships `Restart=on-failure`, so systemd brings PostgreSQL straight back
and repmgr then correctly refuses to promote a second primary. The test kills,
then stops, then asserts the node is still down.

**Acknowledged, not attempted.** A writer appends a token to a file only after
its commit returned. A transaction still in flight when the primary died leaves
no token, deliberately: its outcome is undefined and asserting on it would be
wrong.

**Promotion through repmgr.** `repmgr standby promote`, then `repmgr standby
follow` on the survivors -- the manual procedure this deployment uses. Raw
`pg_ctl promote` leaves repmgr's metadata stale and the survivors following a
dead primary, after which the first synchronous commit blocks forever.

**`ANY 1` is not a promise about any particular node.** It guarantees each
acknowledged commit reached at least one qualifying standby, not that the node
you promote has it. The ledger is checked against whichever node is promoted,
and a survivor missing acknowledged transactions is a failure of the promotion
procedure rather than something to tolerate.

### Memory

Memory, not CPU, is the constraint. Six systemd containers with five Postgres
instances plus `dnf` will not fit in 3.3 GB: `dnf` metadata parsing is the
spike, and concurrent transactions OOM. On a small machine use
`--preinstalled` (skips the two roles that install packages) and
`ANSIBLE_FORKS=2`. On a 16 GB runner neither is needed --
`.github/workflows/cluster-lifecycle.yml` runs it on dispatch with full
parallelism.

## Air-gapped and mirrored environments

Everything the harness fetches is redirectable. Start from the example file:

```shell
$ cp tests/airgap.env.example tests/airgap.env
$ $EDITOR tests/airgap.env
$ set -a; . tests/airgap.env; set +a
$ python3 tests/scripts/check-resources.py --case prod_topology
$ ./tests/run-case.sh prod_topology rhel9
```

There are two separate layers, and both need configuring:

**Building the images** — environment variables, consumed as Docker build args:

| Variable | Redirects |
|---|---|
| `RHEL_BASE_IMAGE` | the database node base image |
| `TESTER_BASE_IMAGE` | the controller container base image |
| `YUM_BASEURL` | RPMs inside the database nodes |
| `YUM_GPGCHECK` | set `0` if the mirror serves unsigned metadata |
| `APT_MIRROR`, `APT_SECURITY_MIRROR` | Debian packages in the controller |
| `CUSTOM_CA_CERT_BASE64` | a base64-encoded root CA PEM installed into every test image trust store |
| `PIP_INDEX_URL`, `PIP_TRUSTED_HOST` | Python packages |
| `ANSIBLE_GALAXY_SERVER` | the pinned Ansible collections |

When `YUM_BASEURL` is set, every repository the base image ships is disabled
and that one is used instead — so the mirror must carry the base OS content as
well as anything the roles install.

If your registry or mirror uses a private root CA, set `CUSTOM_CA_CERT_BASE64` to the base64-encoded PEM before the run, for example `export CUSTOM_CA_CERT_BASE64="$(base64 -w0 /path/to/root-ca.pem)"`. The harness injects it into every built image and refreshes the system trust store so `dnf`, `curl`, Python/pip and other TLS clients trust it.

**Deploying the cluster** — Ansible variables, because the `setup_repo` role
reads them at deploy time rather than at build time. They go in the case's
`vars.json`; see `tests/cases/prod_topology/vars.airgap.json.example` for
`pg_rpm_repo_9_x86_64`, `pg_gpg_key_9_x86_64`, `epel_repo_9` and
`epel_gpg_key_9`.

Missing this second layer is the usual way an "air-gapped" run still fails: the
images build fine from your mirrors, and then the deploy reaches for
`download.postgresql.org`.

### Checking resources before a run

```shell
$ python3 tests/scripts/check-resources.py --case prod_topology
```

Standard library only, so it runs on a bare host before any tooling is
installed. It checks the host commands, the container engine and its
socket/compose plugin, whether the base images are present, and every package
source — then reads the four PGDG/EPEL URLs straight out of
`roles/setup_repo/defaults/main.yml` and checks whatever they currently point
at, so it stays in step with the role instead of duplicating the list.

Exit status is 0 when everything required is reachable. Anything left at a
public default is reported as `WARN` rather than `OK`, because on a connected
host it will work and the failure would only surface once you are actually
isolated:

```text
  OK     database node base image     registry.access.redhat.com/ubi9/ubi-init:9.7 (present locally)
  WARN   Python package index         https://pypi.org/simple -> HTTP 200 (public default -- set PIP_INDEX_URL for air-gapped use)
  FAIL   yum mirror                   https://artifactory.invalid/... -> Name or service not known
                                      -> set YUM_BASEURL to a reachable mirror
```

Add `--json` for machine-readable output, and set `CHECK_INSECURE=1` if your
mirror presents a certificate this host does not trust — that affects only the
checker, never the deployment.

## Testing framework

Executing a test case consists basically in:

1. Spinning up one `docker` container in charge of running the Ansible playbook
   attached to the test case, and the tests themselves.
2. Spinning up one or more `docker` containers in charge of hosting the
   components deployed by the previous execution of the Ansible playbook.
3. Destroying the containers at the end of tests execution.

The tests are written in Python and rely on `pytest` and its `testinfra`
module. Tests related to the same test case must be located in the same file
named: `tests/test_<test_case_name>.py`.


## Directory structure

- `cases`: this folder contains one sub-folder per test case.
- `docker`: docker files and scripts.
- `scripts`: python scripts used to apply additional configuration on the
  containers.
- `tests`: `py.test` files.

### Test case directory

The test case directories are used to store all the required files necessary to
create the docker infrastructure (`docker-compose.yml`) and the Ansible files
(`inventory.yml` template, `playbook.yml`, and `vars.json`) needed to deploy
the components related to the test case.

## Test case: `setup_quorum_replication`

This test case covers PostgreSQL 17 on UBI/RHEL 9, deployed as
a four node cluster -- one primary and three standbys -- using quorum based
synchronous replication.

The quorum is declared once, in the test case `vars.json`:

```json
{
  "synchronous_standby_names": "",
  "synchronous_standby_application_names": ["standby1", "standby2", "standby3"],
  "synchronous_standby_num_sync": 2,
  "standby_quorum_type": "ANY"
}
```

`synchronous_standby_names` is left **empty on purpose**. The
`setup_replication` role builds the value from the three fields below it only
when that variable is empty; pinning the finished
`ANY 2 ("standby1", ...)` string instead takes the literal branch in
`primary_synchronous_param.yml` and skips the generator and its validation
asserts entirely — so the case would no longer cover the code it exists to
cover.

Both the deployment and the assertions read that single declaration, so
changing the quorum in `vars.json` changes what is deployed *and* what is
verified.

The tests in `tests/test_setup_quorum_replication.py` check that:

- the cluster has four nodes, all running the requested Postgres major version,
  with one primary and three standbys in recovery;
- the primary's `synchronous_standby_names` matches the requested quorum, and
  the three standbys are reported as `quorum` members in `pg_stat_replication`;
- a committed row reaches all three standbys;
- stopping one standby still leaves two quorum candidates, so commits keep
  going through, and the stopped standby catches up once restarted;
- stopping two standbys leaves the primary one candidate short of the quorum,
  so a commit blocks until a candidate comes back. The transaction is already
  committed locally while it waits, which is what Postgres documents for
  synchronous replication, and it reaches every standby once the quorum is
  restored.

Running it:

```shell
$ export EDB_PG_TYPE=PG
$ export EDB_PG_VERSION=17
$ export EDB_ENABLE_REPO=false
$ export ANSIBLE_CORE_VERSION=2.15
$ make -C cases/setup_quorum_replication rhel9
```

PostgreSQL comes from the PGDG repositories, so no EDB repository credentials
are required.

## Test case: `prod_topology`

This case deploys the full topology the source reviews assume, and runs the
live half of the finding audit against it:

- one primary and three standbys under `ANY 2` quorum synchronous replication,
- a repmgr witness,
- a pgBackRest repository host,
- an HAProxy client gate fed by the `postgres-cluster-xinetd` health endpoint.

```shell
$ export EDB_PG_TYPE=PG
$ export EDB_PG_VERSION=17
$ export EDB_ENABLE_REPO=false
$ export ANSIBLE_CORE_VERSION=2.15
$ make -C cases/prod_topology rhel9
```

It brings up eight containers and builds a real cluster, so it is much heavier
than the per-push cases. CI runs it from the `prod-topology.yml` workflow on
manual dispatch only, not on every push.

`tests/tests/test_prod_topology.py` holds two kinds of test, and the difference
matters when reading a failure:

- `test_*` — things that must work: every node running the requested major
  version, correct primary/standby roles, `pg_basebackup` having produced a
  real clone of the primary on each standby, one active physical slot per
  standby, all three standbys streaming, the synchronous policy **identical on
  every promotion-capable node**, writes reaching all standbys, repmgr holding
  all five nodes, HAProxy up with every backend, and a healthy pgBackRest
  stanza.
- `test_finding_*` — the reviewed defects. These **pass while the defect is
  present**, so the known-bad behaviour is visible in CI and cannot be fixed
  silently. Each names its finding and says what a fix looks like; when one is
  fixed the test fails and tells you to remove it and refresh
  `tests/findings/baseline.json`.

### Day-two operations

`tests/tests/test_prod_topology_day2.py` covers what happens *after* the
deploy. It runs in the same case, so `make -C cases/prod_topology rhel9` picks
it up automatically.

**Configuration change on a running cluster.** `day2_config.yml` re-runs
`manage_dbserver` against the deployed cluster with a changed
`pg_postgres_conf_params` — the way an operator applies a configuration update
after the initial deploy. Two paths are exercised:

- a **reload-only** setting (`log_min_duration_statement`) must reach every
  node and restart nothing. The test compares
  `pg_postmaster_start_time()` before and after and fails if any node
  restarted, because a restart for a reloadable setting is an availability
  event nobody asked for.
- a **restart-requiring** setting (`shared_buffers`) must reach every node and
  the cluster must come back: every postmaster returns, every standby
  re-attaches, the approved quorum is still in force, and a row committed
  afterwards still reaches all three standbys.

`shared_buffers` is used rather than something like `max_connections` because
it carries no primary/standby ordering constraint — a standby refuses to start
if its `max_connections` is below the primary's, which would fail the test for
a reason unrelated to what it checks.

**Promotion.** A module-scoped fixture stops the primary and promotes the first
standby with `repmgr standby promote` (falling back to `pg_ctl promote`), then
the tests assert the promoted node left recovery, accepts writes, advanced its
timeline, and — the point of EDB-03 — **was already carrying the approved
synchronous policy** rather than receiving it after the fact.

Promotion is also where two findings stop being theoretical:

- **EDB-18** — while the node was a standby, `archive_mode=on` meant the
  inherited `/bin/true` archive command never ran, so nothing looked wrong.
  After promotion it runs, exits 0 for every segment, and stores nothing. The
  test forces a WAL switch and shows `pg_stat_archiver` counting the segment as
  archived with zero failures: PITR from this new primary is broken from the
  moment it was promoted, silently.
- **EDB-07** — the inventory still calls the stopped node the primary, so a
  rerun would configure and force-register the wrong node.

Ordering is load-bearing. pytest collects files alphabetically, so
`test_prod_topology.py` runs before `test_prod_topology_day2.py`, and within
that file the configuration tests run before the promotion tests. The promotion
fixture stops the primary, so nothing that assumes the deployed topology may be
added after it.

### Client-certificate (mTLS) authentication

`tests/tests/test_prod_topology_mtls.py` covers certificate authentication,
including across a promotion.

`issue_certs.yml` runs on the control node and issues the PKI: one CA, a server
certificate per database node, a CRL, and two client certificates. That CA
stands in for the deployment's local ACME server — the collection has no ACME
integration, so certificates are issued outside it and handed in through the
`pg_ssl_*_file` variables, which is exactly how a certbot-issued chain reaches
PostgreSQL. What is under test is the authentication configuration the
collection applies on top, not the issuer.

The deployment then declares, on **every promotion-capable node**:

```yaml
pg_ident_maps:
  - mapname: mtls
    system_username: prod-topology-client   # the certificate Common Name
    pg_username: mtls_app                   # the PostgreSQL role
pg_hba_ip_addresses:
  - contype: hostssl
    users: mtls_app
    method: cert
    options: "map=mtls clientcert=verify-full"
```

Two deliberate choices make the tests mean something:

- **The client certificate's Common Name is not the role name.** The identity
  map is what translates one into the other, so a test where the two were equal
  would still pass if the map were ignored entirely.
- **A second certificate from the same CA carries an unmapped CN** and must be
  *rejected*. Without that, the positive test would pass even if any CA-signed
  certificate were accepted — which is the difference between mTLS and having
  turned SSL on.

The tests check that the managed map file exists and is included from
`pg_ident.conf`, that PostgreSQL actually parsed it
(`pg_ident_file_mappings` with no error), that the HBA rule carries
`cert` + `map=` + `clientcert=verify-full` (`pg_hba_file_rules`), that the added
user authenticates with its certificate and lands in the mapped role, that the
unmapped certificate and a certificate-less client are both refused — and then,
**after the promotion**, that the same certificate still authenticates against
the new primary and that roles can still be added there.

That last check is the auth-side equivalent of the promotion-ready durability
policy in EDB-03: a deployment that configured only the old primary locks out
every certificate client at the moment of failover, which is the worst possible
time to discover it.

### Why the certificates are issued outside the collection

`pg_ssl` is `true` in this case, but the certificates come from
`issue_certs.yml` rather than from the collection.

Left to itself, `init_dbserver` generates a private self-signed CA *inside the
database*, through the EDB `sslutils` extension. That works — `sslutils_17` is
available from PGDG on RHEL 9 — but it produces a CA that exists only on that
one node, which is no use for client certificates: every node would trust a
different CA, and nothing outside the database could be issued a certificate at
all. It is also not the ACME-issued chain a certbot deployment assumes.

So the case issues one CA on the control node and hands the results in through
`pg_ssl_cert_file`, `pg_ssl_key_file`, `pg_ssl_ca_file` and `pg_ssl_crl_file`.
Each of `init_dbserver`'s generation steps is guarded by a "does this file
already exist" check, so supplying the files makes it skip its own generation
for them.

`pg_ssl_crl_file` is supplied for a specific reason: `init_dbserver` points
`ssl_crl_file` at `root.crl`, and if that file is not provided it is generated
from the unrelated sslutils CA. A CRL from a different CA than the one that
signed the client certificates breaks verification, so the case issues an empty
CRL from its own CA.

## Checking the source-review findings

`tests/findings/` checks the collection's source against the published review
findings without deploying anything. It needs only `python3` and runs in
seconds:

```shell
$ python3 tests/findings/check_findings.py           # report
$ python3 tests/findings/check_findings.py --check   # fail on drift from baseline
```

See `tests/findings/README.md`.

### About the RHEL 9 containers

The `rhel9` containers are built from the RedHat Universal Base Image
(`registry.access.redhat.com/ubi9/ubi-init`), which is redistributable and
needs no subscription. The image tag is pinned to the RHEL minor version under
test, and can be overridden:

```shell
$ RHEL9_IMAGE_TAG=9.6 make -C cases/setup_quorum_replication rhel9
```

Because the UBI has no access to the full RHEL repository set, the
CodeReady Builder repository is not enabled on `RedHat9` -- the
`setup_repo` role skips it there, expecting `subscription-manager` on a
subscribed host.

Unlike the older test cases, which bind mount `/sys/fs/cgroup` read-only and
therefore only boot systemd on cgroup v1 hosts, this test case runs its
containers in the host cgroup namespace with a writable cgroup mount. That
works on both cgroup v1 and cgroup v2 hosts.

## Running the tests

### Prerequisites

This testing framework requires the following commands/tools:
- `python3`
- `pip3`
- `docker` with `docker compose`, or `podman` with `podman compose`
- `make`

To install the dependencies:
```shell
$ pip3 install -r requirements.txt
```

### Docker CE and compose plugin installation on Debian11

#### cgroup configuration

  1. In order to use systemd based docker images, make sure the following grub
     configuration is being used in `/etc/default/grub`:
```shell
GRUB_CMDLINE_LINUX_DEFAULT="quiet cgroup_enable=memory swapaccount=1"
GRUB_CMDLINE_LINUX="systemd.unified_cgroup_hierarchy=false"
```

  2. Apply grub configuration changes:
```shell
$ sudo update-grub
```

  3. Reboot the host.

#### Docker CE installation

Packages installation:
```shell
$ sudo apt -y install \
  apt-transport-https ca-certificates curl gnupg2 software-properties-common
$ curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/docker-archive-keyring.gpg
$ sudo add-apt-repository \
   "deb [arch=amd64] https://download.docker.com/linux/debian \
   $(lsb_release -cs) \
   stable"
$ sudo apt -y install \
  docker-ce docker-ce-cli containerd.io docker-compose-plugin
```
Starting docker:
```shell
$ sudo systemctl enable --now docker
```
Adding the current user to the `docker` system group:
```shell
$ sudo usermod -aG docker $USER
$ newgrp docker
```

### Test execution

The `test-runner.py` script is intended to ease test execution through only one
command line.

Usage:

```shell
usage: test-runner.py [-h] [-j JOBS] [--configuration CONFIGURATION] --edb-repo-username EDB_REPO_USERNAME --edb-repo-password
                      EDB_REPO_PASSWORD [--edb-repo-token EDB_REPO_TOKEN] [--pg-version PG_VERSION [PG_VERSION ...]] [--pg-type PG_TYPE [PG_TYPE ...]]
                      [--ansible-core-version ANSIBLE_CORE_VERSION [ANSIBLE_CORE_VERSION ...]]
                      [--os OS [OS ...]] [-k KEYWORD [KEYWORD ...]]

optional arguments:
  -h, --help            show this help message and exit
  -j JOBS, --jobs JOBS  Number of parallel jobs. Default: 4
  --configuration CONFIGURATION
                        Configuration file
  --edb-repo-username EDB_REPO_USERNAME
                        EDB package repository 1.0 username
  --edb-repo-password EDB_REPO_PASSWORD
                        EDB package repository 1.0 password
  --edb-repo-token EDB_REPO_TOKEN
                        EDB package repository 2.0 token
  --pg-version PG_VERSION [PG_VERSION ...]
                        Postgres versions list. Default: ['14']
  --pg-type PG_TYPE [PG_TYPE ...]
                        Postgres DB engines list. Default: all
  --ansible-core-version ANSIBLE_CORE_VERSION [ANSIBLE_CORE_VERSION ...]
                        Version of ansible-core to be used in the testing container. Default: ['2.13']
  --os OS [OS ...]      Operating systems list. Default: all
  -k KEYWORD [KEYWORD ...], --keywords KEYWORD [KEYWORD ...]
                        Execute test cases with a name matching the given keywords.
```

### Usage examples

The example below shows how to run the tests for:
- the `install_dbserver` test case
- on `centos7` and `rocky8` operating systems
- using `ansible-core` version 2.13
- PostgreSQL engine only
- for versions `13` and `14`

```shell
$ test-runner.py \
  --edb-repo-username <edb-repo-username> \
  --edb-repo-password <edb-repo-password> \
  --pg-version 14 13 \
  --os rocky8 centos7 \
  --ansible-core-version 2.13 \
  --pg-type PG \
  -k install_dbserver

Test install_dbserver with ansible-core v2.13 PG/13 on centos7 ... OK
Test install_dbserver with ansible-core v2.13 PG/14 on centos7 ... OK
Test install_dbserver with ansible-core v2.13 PG/13 on rocky8 ... OK
Test install_dbserver with ansible-core v2.13 PG/14 on rocky8 ... OK

Tests passed: 4/4 100.00%
```

Running the tests for all the test cases, for every OS, for PostgreSQL and
EPAS, in version `14`:
```shell
$ test-runner.py \
  --edb-repo-username <edb-repo-username> \
  --edb-repo-password <edb-repo-password> \
  --pg-version 14
```

### Manual test execution

When implementing new test cases, it can be more efficient to execute the tests
without using the `test-runner.py` script. This can be done with the following
command lines:

```shell
$ export EDB_PG_TYPE=<pg-type>
$ export EDB_PG_VERSION=<pg-version>
$ export EDB_REPO_USERNAME=<edb-repo-username>
$ export EDB_REPO_PASSWORD=<edb-repo-password>
$ export EDB_REPO_TOKEN=<edb-repo-token>
$ export ANSIBLE_CORE_VERSION=<ansible-core-version>
$ make -C cases/<test-case> <os>
```

Below is an example of running the tests for test case `init_dbserver`, in
version 14 of PostgreSQL, with `ansible-core` version 2.13, on RockyLinux8:

```shell
$ export EDB_PG_TYPE=PG
$ export EDB_PG_VERSION=14
$ export EDB_REPO_USERNAME=<edb-repo-username>
$ export EDB_REPO_PASSWORD=<edb-repo-password>
$ export EDB_REPO_TOKEN=<edb-repo-token>
$ export ANSIBLE_CORE_VERSION=2.13
$ make -C cases/init_dbserver rocky8
```

Select the container engine with `CONTAINER_ENGINE`:

```shell
# Docker (the default used by Make)
$ CONTAINER_ENGINE=docker make -C cases/setup_replication rocky9

# Podman
$ CONTAINER_ENGINE=podman make -C cases/setup_replication rocky9
```

The Python helper scripts auto-detect Podman first and Docker second when the
variable is not set. Set it explicitly when both are installed so that the
Make and Python layers use the same engine.

### PostgreSQL 17 four-node replication case

Run the UBI/RHEL 9 PostgreSQL 17 replication case with either engine:

```shell
CONTAINER_ENGINE=podman \
ANSIBLE_CORE_VERSION=2.15 \
EDB_PG_TYPE=PG \
EDB_PG_VERSION=17 \
EDB_ENABLE_REPO=false \
make -C tests/cases/setup_replication rhel9
```

Replace `podman` with `docker` for Docker Compose. The default target image is
Red Hat UBI Init 9.7; set `RHEL_BASE_IMAGE` to the exact internal RHEL 9.7
image used by production when it is available.

#### Rootless Podman

**Rootless works** -- rootful is not required, despite what the systemd
containers suggest. Verified on Podman 5.4.2 with cgroup v2 and the systemd
cgroup manager: `systemd` reaches `running` inside the UBI Init container,
`sshd` starts, and PostgreSQL 17 installs from PGDG and runs under `systemctl`.

Two things are needed for the Compose path:

```shell
# 1. docker compose (and podman-docker) talk to the Podman API socket, which
#    is not started by default.
$ systemctl --user enable --now podman.socket

# 2. ansible-galaxy must be on the *host* -- the top-level `make` builds the
#    collection tarball outside the containers.
$ sudo apt install ansible-core      # or: dnf install ansible-core
```

`pytest`, `testinfra` and Ansible itself are only needed inside the
`ansible-tester` container, which installs them from `tests/requirements.txt`,
so they do not have to be present on the host.

Two rootless quirks worth knowing:

- `systemctl list-units --state=failed` shows `sys-kernel-config.mount`,
  `sys-kernel-debug.mount` and `sys-kernel-tracing.mount` as failed, which
  makes `systemctl is-system-running` report `degraded`. This is expected in
  an unprivileged container and does not affect the tests.
- Teardown can fail with `rootless netns: kill network process: permission
  denied`. If `make clean` leaves containers behind, remove them directly:

  ```shell
  $ podman rm -f $(podman ps -aq)
  $ podman network prune -f
  ```

This case creates `postgres01` through `postgres04`, checks all three physical
replication connections and slots, verifies the following setting on every
database node, and proves that a commit succeeds while one standby is down:

```text
ANY 1 ("postgres01","postgres02","postgres03","postgres04")
```

Container coverage does not replace VM tests for systemd, storage, SELinux,
firewalld, networking, or failure/restart behavior.

Containers hosting Postgres and the components we had tested with the help of
the previous command are not automatically destroyed. For cleaning up those,
the following command should be executed:

```shell
$ make -C cases/<test-case> clean
```

Because the container are not automatically destroyed, this method is useful
for tests development and debugging: it is possible to open a shell session
on the running container.

```shell
# Fetch the container id from the output of the following command
$ docker ps
# Start a new bash session on the container
$ docker exec -it <container-id> /bin/bash
```

### Logs

In case of test failure, standard output and error are stored in dedicated
files located into the `logs` directory. Filenames are `<test_case>.stdout` and
`<test_case>.sterr`.
