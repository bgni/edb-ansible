#!/usr/bin/env bash
#
# Run a cluster lifecycle test against a playbook that lives outside this
# repository.
#
#   tests/run-cluster-lifecycle.sh --playbook-root /path/to/private/edb-ansible \
#                                  --playbook cluster.yml
#
# The point is to test *your* playbook, not the one in this tree. The checkout
# you name is mounted read-only into the Ansible runner and nowhere else; the
# database nodes never see it. Its commit is recorded with the results so a
# pass can be attributed to an exact revision.
#
# Three phases, in order, because each depends on the last:
#
#   1. provision    ephemeral nodes, run the playbook, cluster serves queries
#   2. reconfigure  change the inventory, re-run the same playbook against the
#                   running cluster, observe the new configuration converge
#   3. failover     fence the primary, promote a survivor, verify that every
#                   acknowledged transaction survived
#
# Phase 2 is the one that initial deployment cannot substitute for: deploying
# with the final value proves nothing about applying a change to a cluster that
# is already running and serving.
#
# Phase 3 tests the promotion *procedure* as much as the configuration.
# ANY 1 guarantees each acknowledged commit reached at least one qualifying
# standby -- not that any particular survivor has it. So the test verifies the
# acknowledged set against whichever node is promoted, and a procedure that
# cannot demonstrate a safe candidate is expected to refuse rather than promote.
#
# Options:
#   --playbook-root DIR   checkout containing the playbook (required)
#   --playbook FILE       playbook path within that root (default cluster.yml)
#   --inventory FILE      inventory template within this case (default the
#                         generated four-node-plus-witness one)
#   --engine ENGINE       podman or docker (default: auto)
#   --postgres-version N  (default 17)
#   --phase LIST          comma-separated subset, e.g. provision,reconfigure
#   --keep                leave the cluster running afterwards
#
# Results, including the playbook revision under test, land in
# tests/cases/cluster_lifecycle/results/.

set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CASE_NAME="cluster_lifecycle"
CASE_DIR="${TESTS_DIR}/cases/${CASE_NAME}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '\n==> %s\n' "$*"; }

PLAYBOOK_ROOT=""
PLAYBOOK="cluster.yml"
ENGINE="${CONTAINER_ENGINE:-}"
PGVER="${EDB_PG_VERSION:-17}"
PHASES="provision,reconfigure,failover"
KEEP=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --playbook-root) PLAYBOOK_ROOT="$2"; shift 2 ;;
        --playbook) PLAYBOOK="$2"; shift 2 ;;
        --engine) ENGINE="$2"; shift 2 ;;
        --postgres-version) PGVER="$2"; shift 2 ;;
        --phase) PHASES="$2"; shift 2 ;;
        --keep) KEEP=true; shift ;;
        -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ -n "${PLAYBOOK_ROOT}" ]] || die "--playbook-root is required

This harness exists to test a playbook that is not in this repository. Point it
at your checkout:

  $0 --playbook-root /path/to/private/edb-ansible --playbook cluster.yml"

PLAYBOOK_ROOT="$(cd "${PLAYBOOK_ROOT}" && pwd)" || die "cannot resolve --playbook-root"
[[ -f "${PLAYBOOK_ROOT}/${PLAYBOOK}" ]] \
    || die "no ${PLAYBOOK} under ${PLAYBOOK_ROOT}"

if [[ -z "${ENGINE}" ]]; then
    if command -v podman >/dev/null 2>&1; then ENGINE=podman
    elif command -v docker >/dev/null 2>&1; then ENGINE=docker
    else die "no container engine found; pass --engine"; fi
fi
COMPOSE=("${ENGINE}" compose)

# Record exactly what is under test. A green run against an unknown revision is
# not evidence of anything.
PLAYBOOK_REV="$(git -C "${PLAYBOOK_ROOT}" rev-parse HEAD 2>/dev/null || echo 'not-a-git-checkout')"
PLAYBOOK_DIRTY="$(git -C "${PLAYBOOK_ROOT}" status --porcelain 2>/dev/null | head -c1)"
[[ -n "${PLAYBOOK_DIRTY}" ]] && PLAYBOOK_REV="${PLAYBOOK_REV}+dirty"

export CONTAINER_ENGINE="${ENGINE}"
export EDB_OS=rhel9
export EDB_PG_VERSION="${PGVER}"
export EDB_PG_TYPE="${EDB_PG_TYPE:-PG}"
export EDB_ENABLE_REPO="${EDB_ENABLE_REPO:-false}"
export ANSIBLE_CORE_VERSION="${ANSIBLE_CORE_VERSION:-2.15}"
export EXTERNAL_PLAYBOOK_ROOT="${PLAYBOOK_ROOT}"
export EXTERNAL_PLAYBOOK="${PLAYBOOK}"
export LIFECYCLE_PHASES="${PHASES}"

RESULTS="${CASE_DIR}/results"
mkdir -p "${RESULTS}"

note "Playbook under test"
printf '  root:     %s\n  playbook: %s\n  revision: %s\n' \
    "${PLAYBOOK_ROOT}" "${PLAYBOOK}" "${PLAYBOOK_REV}"
printf '{"playbook_root":"%s","playbook":"%s","revision":"%s","postgres_version":"%s","phases":"%s"}\n' \
    "${PLAYBOOK_ROOT}" "${PLAYBOOK}" "${PLAYBOOK_REV}" "${PGVER}" "${PHASES}" \
    > "${RESULTS}/under-test.json"

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

printf '\nplaybook revision under test: %s\n' "${PLAYBOOK_REV}"
exit "${RESULT}"
