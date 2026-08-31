#!/usr/bin/env bash
#
# Cluster lifecycle test for the collection in this working tree.
#
#   tests/run-cluster-lifecycle.sh
#   tests/run-cluster-lifecycle.sh --phase provision,reconfigure --keep
#
# Five containers -- four Postgres nodes and a witness that also hosts the
# pgBackRest repository -- deployed by tests/cases/cluster_lifecycle/playbook.yml
# using the roles as they exist in this checkout. The collection is rebuilt from
# the working tree before every run, so what is exercised is your changes, not a
# published release.
#
# Three phases, in order, because each depends on the last:
#
#   1. provision    clean nodes, run the playbook, cluster serves queries
#   2. reconfigure  change the synchronous policy and re-run the same playbook
#                   against the running cluster; observe it converge
#   3. failover     kill the primary and verify no acknowledged transaction was
#                   lost
#
# Phase 2 is the one initial deployment cannot substitute for: deploying
# straight to the final value proves nothing about applying a change to a
# cluster that is already running and serving.
#
# Phase 3 tests the promotion procedure as much as the configuration. ANY N
# guarantees each acknowledged commit reached at least N qualifying standbys --
# not that a particular survivor has it -- so the acknowledged set is checked
# against whichever node is promoted.
#
# Options:
#   --engine ENGINE       podman or docker (default: auto)
#   --postgres-version N  (default 17)
#   --phase LIST          comma-separated subset
#   --preinstalled        use the pre-built package image, no repository needed
#   --keep                leave the cluster running afterwards
#   --clean               tear the cluster down and exit
#
# For an air-gapped run also set RHEL_BASE_IMAGE, TESTER_BASE_IMAGE,
# YUM_BASEURL, CUSTOM_CA_CERT_FILE, PIP_INDEX_URL, APT_MIRROR and
# ANSIBLE_GALAXY_SERVER before starting the case.
#
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CASE_NAME="cluster_lifecycle"
CASE_DIR="${TESTS_DIR}/cases/${CASE_NAME}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '\n==> %s\n' "$*"; }

ENGINE="${CONTAINER_ENGINE:-}"
PGVER="${EDB_PG_VERSION:-17}"
PHASES="provision,reconfigure,failover"
KEEP=false
PREINSTALLED=false
CLEAN_ONLY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --engine) ENGINE="$2"; shift 2 ;;
        --postgres-version) PGVER="$2"; shift 2 ;;
        --phase) PHASES="$2"; shift 2 ;;
        --keep) KEEP=true; shift ;;
        --clean) CLEAN_ONLY=true; shift ;;
        --preinstalled) PREINSTALLED=true; shift ;;
        -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done


if [[ -z "${ENGINE}" ]]; then
    if command -v podman >/dev/null 2>&1; then ENGINE=podman
    elif command -v docker >/dev/null 2>&1; then ENGINE=docker
    else die "no container engine found; pass --engine"; fi
fi
COMPOSE=("${ENGINE}" compose)

# Record exactly what is under test. A green run against an unidentified
# revision is not evidence of anything.
REPO_ROOT="$(cd "${TESTS_DIR}/.." && pwd)"
COLLECTION_REV="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
[[ -n "$(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null | head -c1)" ]] \
    && COLLECTION_REV="${COLLECTION_REV}+dirty"

export CONTAINER_ENGINE="${ENGINE}"
export EDB_OS=rhel9
export EDB_PG_VERSION="${PGVER}"
export EDB_PG_TYPE="${EDB_PG_TYPE:-PG}"
export EDB_ENABLE_REPO="${EDB_ENABLE_REPO:-false}"
export ANSIBLE_CORE_VERSION="${ANSIBLE_CORE_VERSION:-2.15}"
export LIFECYCLE_PHASES="${PHASES}"
export PREINSTALLED
if [[ "${PREINSTALLED}" == true ]]; then
    export RHEL_BASE_IMAGE="${PREINSTALLED_IMAGE:-localhost/edb-ansible/rhel9-pg17:local}"
fi

if [[ "${CLEAN_ONLY}" == true ]]; then
    cd "${CASE_DIR}"
    "${COMPOSE[@]}" rm -s -f >/dev/null 2>&1 || true
    ids="$("${ENGINE}" ps -aq --filter "name=${CASE_NAME}-" 2>/dev/null || true)"
    [[ -n "${ids}" ]] && "${ENGINE}" rm -f ${ids} >/dev/null 2>&1 || true
    rm -rf ./.ssh ./inventory.yml
    printf 'torn down %s\n' "${CASE_NAME}"
    exit 0
fi

RESULTS="${CASE_DIR}/results"
mkdir -p "${RESULTS}"

note "Collection under test"
printf '  revision: %s\n  postgres: %s\n  phases:   %s\n' \
    "${COLLECTION_REV}" "${PGVER}" "${PHASES}"
printf '{"collection_revision":"%s","postgres_version":"%s","phases":"%s"}\n' \
    "${COLLECTION_REV}" "${PGVER}" "${PHASES}" > "${RESULTS}/under-test.json"

# The tester installs the collection from this tarball, so rebuild it from the
# working tree or the run silently exercises the previous build.
note "Building the collection from the working tree"
command -v ansible-galaxy >/dev/null 2>&1 \
    || die "ansible-galaxy not found on PATH; install ansible-core"
sed -E "s/version:.*/version: \"$(head -n1 "${REPO_ROOT}/VERSION")\"/g" \
    "${REPO_ROOT}/galaxy.template.yml" > "${REPO_ROOT}/galaxy.yml"
ansible-galaxy collection build --force --output-path "${REPO_ROOT}" "${REPO_ROOT}"

cd "${CASE_DIR}"

teardown() {
    note "Tearing down ${CASE_NAME}"
    "${COMPOSE[@]}" rm -s -f >/dev/null 2>&1 || true
    # compose rm leaves containers behind on rootless podman when it cannot
    # tear the netns down; clear them directly so the next run starts clean.
    ids="$("${ENGINE}" ps -aq --filter "name=${CASE_NAME}-" 2>/dev/null || true)"
    [[ -n "${ids}" ]] && "${ENGINE}" rm -f ${ids} >/dev/null 2>&1 || true
    rm -rf ./.ssh ./inventory.yml
}

note "Starting ephemeral nodes"
mapfile -t NODES < <("${COMPOSE[@]}" config --services | grep -- "-rhel9\$" | sort)
[[ ${#NODES[@]} -gt 0 ]] || die "no rhel9 services defined in ${CASE_DIR}"
for n in "${NODES[@]}"; do "${COMPOSE[@]}" up "$n" -d --build; done

note "Preparing SSH and rendering the inventory"
python3 "${TESTS_DIR}/scripts/ssh-keygen.py" --ssh-dir .ssh
python3 "${TESTS_DIR}/scripts/prep-containers.py" --compose-dir . --ssh-dir .ssh
python3 "${TESTS_DIR}/scripts/build-inventory.py" --compose-dir .
python3 "${TESTS_DIR}/scripts/ssh-build-add-hosts-sh.py" --compose-dir . --ssh-dir .ssh
python3 "${TESTS_DIR}/scripts/ssh-build-ssh-config.py" --ssh-dir .ssh

note "Running the lifecycle: ${PHASES}"
set +e
"${COMPOSE[@]}" up ansible-tester \
    --force-recreate --build --abort-on-container-exit \
    --exit-code-from ansible-tester
RESULT=$?
set -e

if [[ "${KEEP}" == true ]]; then
    note "--keep: cluster left running. Tear down with:"
    printf '  %s compose -f %s/docker-compose.yml rm -s -f\n' "${ENGINE}" "${CASE_DIR}"
else
    teardown
fi

printf '\ncollection revision under test: %s\n' "${COLLECTION_REV}"
exit "${RESULT}"
