#!/usr/bin/env bash
#
# Run one test case without make.
#
#   tests/run-case.sh <case> [os]              run a case (os defaults to rhel9)
#   tests/run-case.sh <case> [os] --preinstalled  offline: skip the install roles
#   tests/run-case.sh <case> [os] --tests-only  re-run the tests only
#   tests/run-case.sh <case> [os] --clean       tear the case down and exit
#   tests/run-case.sh --list                    list the available cases
#
# --tests-only reuses a cluster that is already up, skipping the image build,
# the container start and the Ansible deploy: about three minutes instead of
# fourteen. Deploy first with KEEP_CONTAINERS=true.
#
# IMPORTANT: the suite is destructive. The day-two tests stop the primary and
# promote a standby, and they do not put it back. Re-running the *whole* suite
# against a cluster that has already been through it fails in bulk -- the tests
# expect the deployed topology and find a promoted one.
#
# So --tests-only is for iterating on a specific test, narrowed with
# PYTEST_ARGS, not for repeating a full run:
#
#     PYTEST_ARGS='-k mtls_unmapped' ./tests/run-case.sh <case> --tests-only
#
# For a clean full run, redeploy.
#
# Everything the Makefile targets did, in a single script: build the collection
# tarball, bring the node containers up, prepare SSH between them, render the
# inventory, then run the tester container which executes the playbook and
# pytest.
#
# Configuration is by environment variable. See tests/README.md; the ones that
# matter most:
#
#   CONTAINER_ENGINE   podman (default when present) or docker
#   EDB_PG_TYPE        PG or EPAS                       (default PG)
#   EDB_PG_VERSION     Postgres major                   (default 17)
#   EDB_ENABLE_REPO    use the EDB repositories         (default false)
#   ANSIBLE_CORE_VERSION                                (default 2.15)
#
# For an air-gapped run also set RHEL_BASE_IMAGE, TESTER_BASE_IMAGE,
# YUM_BASEURL, CUSTOM_CA_CERT_FILE, PIP_INDEX_URL, APT_MIRROR and
# ANSIBLE_GALAXY_SERVER, and check
# them first with tests/scripts/check-resources.py.
#
# --preinstalled runs with no package repository at all. It uses a node image
# that already carries the whole package set and skips setup_repo and
# install_dbserver, which are the only roles that fetch anything. Everything
# that remains -- init_dbserver, replication, identity maps, HBA, the
# synchronous policy, day two -- is configuration, which is where most changes
# are. Build the image once where a repository is reachable:
#
#   docker build -f tests/docker/Dockerfile.rhel9-preinstalled \
#       --build-arg pg_version=17 -t localhost/edb-ansible/rhel9-pg17:local tests/docker
#
# Add `--secret id=custom_root_ca,src=/path/to/root-ca.pem`
# when the mirror is served behind a private root CA, then point
# PREINSTALLED_IMAGE at it (defaults to that local tag).

set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${TESTS_DIR}/.." && pwd)"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '\n==> %s\n' "$*"; }

list_cases() {
    find "${TESTS_DIR}/cases" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort
}

if [[ "${1:-}" == "--list" ]]; then
    list_cases
    exit 0
fi

CASE_NAME="${1:-}"
[[ -n "${CASE_NAME}" ]] || die "usage: $0 <case> [os] [--clean]   ($0 --list)"

CASE_DIR="${TESTS_DIR}/cases/${CASE_NAME}"
[[ -d "${CASE_DIR}" ]] || die "no such case: ${CASE_NAME}. Available:
$(list_cases)"

# Second argument is the OS unless it is a flag.
EDB_OS="rhel9"
if [[ "${2:-}" != "" && "${2:-}" != --* ]]; then
    EDB_OS="$2"
    shift
fi
shift || true

CLEAN_ONLY=false
TESTS_ONLY=false
PREINSTALLED=false
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN_ONLY=true ;;
        --tests-only) TESTS_ONLY=true ;;
        --preinstalled) PREINSTALLED=true ;;
        *) die "unknown argument: $arg" ;;
    esac
done

# Prefer podman when both are installed, matching tests/scripts/lib/docker.py.
if [[ -z "${CONTAINER_ENGINE:-}" ]]; then
    if command -v podman >/dev/null 2>&1; then
        CONTAINER_ENGINE=podman
    elif command -v docker >/dev/null 2>&1; then
        CONTAINER_ENGINE=docker
    else
        die "no container engine found; install podman or docker, or set CONTAINER_ENGINE"
    fi
fi

COMPOSE=("${CONTAINER_ENGINE}" compose)

export CONTAINER_ENGINE EDB_OS
export EDB_PG_TYPE="${EDB_PG_TYPE:-PG}"
export EDB_PG_VERSION="${EDB_PG_VERSION:-17}"
export EDB_ENABLE_REPO="${EDB_ENABLE_REPO:-false}"
export ANSIBLE_CORE_VERSION="${ANSIBLE_CORE_VERSION:-2.15}"

# Offline mode: use the image that already has the packages, and tell the
# playbook to skip the two roles that would otherwise reach a repository.
export PREINSTALLED
if [[ "${PREINSTALLED}" == true ]]; then
    export PREINSTALLED_IMAGE="${PREINSTALLED_IMAGE:-localhost/edb-ansible/rhel9-pg17:local}"
    export RHEL_BASE_IMAGE="${PREINSTALLED_IMAGE}"
fi

cd "${CASE_DIR}"

teardown() {
    note "Tearing down ${CASE_NAME}"
    "${COMPOSE[@]}" rm -s -f || true
    rm -rf ./.ssh ./inventory.yml ./certs
}

if [[ "${CLEAN_ONLY}" == true ]]; then
    teardown
    exit 0
fi

if [[ "${TESTS_ONLY}" == true ]]; then
    [[ -f ./inventory.yml ]] || die \
        "no inventory.yml in ${CASE_DIR}: --tests-only needs a cluster that is
already deployed. Run without it first, using KEEP_CONTAINERS=true."

    note "Re-running the tests against the existing cluster"
    if [[ -z "${PYTEST_ARGS:-}" ]]; then
        printf '%s\n' \
          "    Note: no PYTEST_ARGS set, so the whole suite will run." \
          "    If this cluster has already been through the day-two tests its" \
          "    primary is stopped and a standby promoted, and much of the suite" \
          "    will fail on that rather than on anything you changed." \
          "    Narrow it, e.g. PYTEST_ARGS='-k mtls', or redeploy." >&2
    fi
    export SKIP_PLAYBOOK=true
    "${COMPOSE[@]}" up ansible-tester \
        --force-recreate --abort-on-container-exit \
        --exit-code-from ansible-tester
    exit $?
fi

# The tester installs the collection from this tarball, so it must be rebuilt
# before every run or a change to a role is silently not tested.
note "Building the collection tarball"
command -v ansible-galaxy >/dev/null 2>&1 \
    || die "ansible-galaxy not found on PATH; install ansible-core on this host"
sed -E "s/version:.*/version: \"$(head -n1 "${REPO_ROOT}/VERSION")\"/g" \
    "${REPO_ROOT}/galaxy.template.yml" > "${REPO_ROOT}/galaxy.yml"
# --output-path: the build writes to the working directory otherwise, and the
# tester installs the tarball from the repository root.
ansible-galaxy collection build --force \
    --output-path "${REPO_ROOT}" "${REPO_ROOT}"

# Which services make up this case: everything named <something>-<os>.
mapfile -t NODES < <("${COMPOSE[@]}" config --services | grep -- "-${EDB_OS}\$" | sort)
[[ ${#NODES[@]} -gt 0 ]] || die "case ${CASE_NAME} defines no services for os '${EDB_OS}'"

note "Starting ${#NODES[@]} ${EDB_OS} node(s): ${NODES[*]}"
# --build, because `up` alone reuses an existing image: a change to
# tests/docker/Dockerfile.<os> would otherwise not reach the nodes and the run
# would silently test the previous image. Layer caching keeps this cheap when
# nothing changed.
for node in "${NODES[@]}"; do
    "${COMPOSE[@]}" up "${node}" -d --build
done

note "Preparing SSH and rendering the inventory"
python3 "${TESTS_DIR}/scripts/ssh-keygen.py" --ssh-dir .ssh
python3 "${TESTS_DIR}/scripts/prep-containers.py" --compose-dir . --ssh-dir .ssh
python3 "${TESTS_DIR}/scripts/build-inventory.py" --compose-dir .
python3 "${TESTS_DIR}/scripts/ssh-build-add-hosts-sh.py" --compose-dir . --ssh-dir .ssh
python3 "${TESTS_DIR}/scripts/ssh-build-ssh-config.py" --ssh-dir .ssh

note "Running the playbook and tests"
set +e
"${COMPOSE[@]}" up ansible-tester \
    --force-recreate --build --abort-on-container-exit \
    --exit-code-from ansible-tester
RESULT=$?
set -e

if [[ "${KEEP_CONTAINERS:-false}" == "true" ]]; then
    note "KEEP_CONTAINERS=true, leaving ${CASE_NAME} running"
    printf 'Tear down with: %s %s %s --clean\n' "$0" "${CASE_NAME}" "${EDB_OS}"
else
    teardown
fi

exit "${RESULT}"
