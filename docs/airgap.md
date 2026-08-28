# Running Tests in Air-Gapped Environments

This guide explains how to run the `edb-ansible` test suite without direct internet access by
pointing each dependency source at internal mirrors (e.g. Artifactory, Nexus, or any
comparable repository manager).

---

## Prerequisites

| Tool | Minimum version |
|---|---|
| Docker (or Podman) | 20.x / 4.x |
| Docker Compose (or podman-compose) | v2 / 1.x |
| Python 3 | 3.8+ |
| Bash | 4+ |

---

## Required Mirrors

Your repository manager must proxy or host **all five** of the following source types.
Run `tests/scripts/check-mirrors.sh` (see [Verifying Mirrors](#verifying-mirrors)) before
the first build to confirm every endpoint is reachable.

### 1. Docker Registry

Proxies upstream registries such as `docker.io`, `registry.access.redhat.com`, etc.

Base images pulled during build:

| Image | Upstream registry |
|---|---|
| `debian:11-slim` | docker.io |
| `rockylinux/rockylinux:8.4` | docker.io |
| `rockylinux/rockylinux:9` | docker.io |
| `almalinux:8.7` | docker.io |
| `jrei/systemd-debian:9/10/11` | docker.io |
| `jrei/systemd-ubuntu:20.04/22.04` | docker.io |
| `jrei/systemd-centos:8` | docker.io |
| `centos/systemd` | docker.io |
| `oraclelinux:8/9` | docker.io |
| `sirkkalap/oraclelinux-systemd` | docker.io |
| `opensuse/leap:latest` | docker.io |
| `registry.access.redhat.com/ubi8/ubi` | registry.access.redhat.com |

### 2. RPM / YUM / DNF Mirror

Used by RHEL-family containers (Rocky, AlmaLinux, CentOS, Oracle Linux, SUSE) during
`docker build`.

### 3. APT Mirror

Used by Debian- and Ubuntu-family containers during `docker build`.

### 4. PyPI Mirror

Used inside `Dockerfile.ansible-tester` to install:
- `pip`, `ansible-core`, `paramiko`, `pytest-testinfra`, `pyyaml`

### 5. Ansible Galaxy Mirror

Used inside `Dockerfile.ansible-tester` to install collections:
- `community.postgresql`, `ansible.posix`, `community.general`, `community.crypto`

### 6. EDB Package Repository (optional)

If your Artifactory proxies the EDB package repository, you can let the playbook configure
it automatically. Otherwise, set `EDB_ENABLE_REPO=false` and pre-install the EDB packages
in your OS mirror.

---

## Configuration

### Environment Variable Reference

Copy the block below into a file (e.g. `airgap.env`), fill in your values, and
`source airgap.env` before running any build or test command.

```bash
# ── Docker registry ───────────────────────────────────────────────────────────
# Pull-through proxy for docker.io and other upstream registries.
# Example: https://artifactory.example.com/artifactory/docker-remote
AIRGAP_DOCKER_MIRROR=

# Optional: token/password for the registry (leave empty if anonymous is allowed)
AIRGAP_DOCKER_MIRROR_TOKEN=

# ── RPM mirror (Rocky, AlmaLinux, CentOS, Oracle Linux) ───────────────────────
# Base URL that your package manager will be pointed to.
# Example: https://artifactory.example.com/artifactory/yum-remote
AIRGAP_YUM_MIRROR=

# Optional auth token for the YUM mirror
AIRGAP_YUM_MIRROR_TOKEN=

# ── APT mirror (Debian, Ubuntu) ────────────────────────────────────────────────
# Mirror host (without scheme), e.g. artifactory.example.com/artifactory/apt-remote
AIRGAP_APT_MIRROR=

# Optional auth token for the APT mirror
AIRGAP_APT_MIRROR_TOKEN=

# ── Zypper mirror (SUSE) ───────────────────────────────────────────────────────
AIRGAP_ZYPPER_MIRROR=
AIRGAP_ZYPPER_MIRROR_TOKEN=

# ── PyPI mirror ───────────────────────────────────────────────────────────────
# Full simple index URL.
# Example: https://artifactory.example.com/artifactory/api/pypi/pypi-remote/simple
AIRGAP_PYPI_MIRROR=

# Optional auth token for PyPI
AIRGAP_PYPI_MIRROR_TOKEN=

# ── Ansible Galaxy mirror ─────────────────────────────────────────────────────
# Example: https://artifactory.example.com/artifactory/ansible-remote
AIRGAP_GALAXY_MIRROR=

# Optional auth token for Galaxy
AIRGAP_GALAXY_MIRROR_TOKEN=

# ── EDB repo (leave empty to skip) ───────────────────────────────────────────
# Set to "false" if your OS mirror already contains EDB packages.
EDB_ENABLE_REPO=true
EDB_REPO_USERNAME=
EDB_REPO_PASSWORD=
EDB_REPO_TOKEN=
```

---

## Docker daemon pull-through mirror

The cleanest way to redirect base-image pulls is to configure Docker's mirror list.
This requires no changes to any Dockerfile.

Edit (or create) `/etc/docker/daemon.json`:

```json
{
  "registry-mirrors": [
    "https://<AIRGAP_DOCKER_MIRROR>"
  ]
}
```

Then restart Docker:

```bash
sudo systemctl restart docker
```

If your registry requires authentication:

```bash
docker login <AIRGAP_DOCKER_MIRROR> -u <user> -p <AIRGAP_DOCKER_MIRROR_TOKEN>
```

---

## Running the Tests

### 1. Verify all mirrors (strongly recommended first)

```bash
source airgap.env
bash tests/scripts/check-mirrors.sh
```

The script exits non-zero and prints a summary if any mirror is unreachable.

### 2. Build and run (example: rocky8)

```bash
source airgap.env

export EDB_OS=rocky8
export EDB_PG_TYPE=epas        # or "pg"
export EDB_PG_VERSION=15

cd tests/cases/setup_replication

# Start OS containers
docker compose up primary1-rocky8 standby1-rocky8 standby2-rocky8 -d

# Generate SSH keys, inventory, etc.
python3 ../../scripts/ssh-keygen.py --ssh-dir .ssh
python3 ../../scripts/prep-containers.py --compose-dir . --ssh-dir .ssh
python3 ../../scripts/build-inventory.py --compose-dir .
python3 ../../scripts/ssh-build-add-hosts-sh.py --compose-dir . --ssh-dir .ssh
python3 ../../scripts/ssh-build-ssh-config.py --ssh-dir .ssh

# Run the ansible-tester container
docker compose up ansible-tester \
  --force-recreate --build \
  --abort-on-container-exit \
  --exit-code-from ansible-tester \
  --build-arg YUM_MIRROR="${AIRGAP_YUM_MIRROR}" \
  --build-arg YUM_MIRROR_TOKEN="${AIRGAP_YUM_MIRROR_TOKEN}" \
  --build-arg PYPI_MIRROR="${AIRGAP_PYPI_MIRROR}" \
  --build-arg PYPI_MIRROR_TOKEN="${AIRGAP_PYPI_MIRROR_TOKEN}" \
  --build-arg GALAXY_MIRROR="${AIRGAP_GALAXY_MIRROR}" \
  --build-arg GALAXY_MIRROR_TOKEN="${AIRGAP_GALAXY_MIRROR_TOKEN}"
```

> **Note:** The `--build-arg` flags shown above rely on the corresponding `ARG` declarations
> being present in the Dockerfiles. If you are using unmodified Dockerfiles, configure the
> mirrors via daemon-level pull-through (Docker) and OS-level repo configuration
> (package manager config files mounted as volumes or baked into override Dockerfiles).

### 3. Clean up

```bash
docker compose rm -s -f
rm -rf .ssh inventory.yml
```

---

## Verifying Mirrors

See [`tests/scripts/check-mirrors.sh`](../tests/scripts/check-mirrors.sh).

The script reads the same environment variables listed above and performs:

- HTTPS connectivity check (with optional token auth) for each configured mirror
- Content sanity check (e.g. returns a valid package list / API response)
- Coloured pass/fail summary

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `docker build` pulls from internet despite mirror config | Docker daemon not restarted after `daemon.json` change | `sudo systemctl restart docker` |
| `dnf install` fails with "No such file" | YUM mirror URL incorrect or unreachable | Run `check-mirrors.sh` and verify `AIRGAP_YUM_MIRROR` |
| `pip install` fails with connection error | PyPI mirror URL or token wrong | Check `AIRGAP_PYPI_MIRROR` and `AIRGAP_PYPI_MIRROR_TOKEN` |
| `ansible-galaxy` fails with 403 | Galaxy mirror requires auth | Set `AIRGAP_GALAXY_MIRROR_TOKEN` |
| Playbook fails installing EDB packages | EDB repo unreachable | Either proxy EDB repo in Artifactory or set `EDB_ENABLE_REPO=false` and pre-populate OS mirror |
| `apt update` fails with "Release file expired" | Clock skew between container and host | Sync host time with NTP |
