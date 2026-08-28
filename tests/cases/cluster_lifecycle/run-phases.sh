#!/bin/bash -eu
# Drives the three lifecycle phases against the external playbook.
#
# Runs inside the Ansible runner. /playbook is the checkout under test,
# mounted read-only; /workspace is this repository, which supplies the
# inventory, the phase variables and the pytest assertions.

cd /workspace
CASE=/workspace/tests/cases/cluster_lifecycle
INV="${CASE}/inventory.yml"
PLAYBOOK="/playbook/${EXTERNAL_PLAYBOOK:-cluster.yml}"

mkdir -p /root/.ssh && chmod 0700 /root/.ssh
cp "${CASE}/.ssh/id_rsa" "${CASE}/.ssh/ssh_config" /root/.ssh/
chmod 0600 /root/.ssh/id_rsa
"${CASE}/.ssh/add_hosts.sh"

export EDB_SSH_USER=root
export EDB_SSH_KEY=/root/.ssh/id_rsa
export EDB_SSH_CONFIG=/root/.ssh/ssh_config
export EDB_INVENTORY="${INV}"
export ANSIBLE_PIPELINING=1
export ANSIBLE_FORKS="${ANSIBLE_FORKS:-10}"

run_playbook() {  # $1 = vars file
    echo "--- ansible-playbook ${PLAYBOOK} with $(basename "$1") ---"
    ansible-playbook -i "${INV}" \
        --private-key /root/.ssh/id_rsa \
        --extra-vars "pg_type=${EDB_PG_TYPE}" \
        --extra-vars "pg_version=${EDB_PG_VERSION}" \
        --extra-vars "enable_edb_repo=${EDB_ENABLE_REPO}" \
        --extra-vars "@$1" \
        "${PLAYBOOK}"
}

run_tests() {  # $1 = -k expression
    export EDB_ANSIBLE_VARS="$2"
    py.test -v -k "$1" /workspace/tests/tests/test_cluster_lifecycle.py
}

has_phase() { [[ ",${LIFECYCLE_PHASES}," == *",$1,"* ]]; }

if has_phase provision; then
    echo "=== PHASE 1: provision ==="
    run_playbook "${CASE}/vars.provision.json"
    run_tests "lifecycle_provision" "${CASE}/vars.provision.json"
fi

if has_phase reconfigure; then
    echo "=== PHASE 2: reconfigure a running cluster ==="
    # Same playbook, same nodes, different input. Deploying with the final
    # value would not test this.
    run_playbook "${CASE}/vars.reconfigure.json"
    run_tests "lifecycle_reconfigure" "${CASE}/vars.reconfigure.json"
fi

if has_phase failover; then
    echo "=== PHASE 3: failover ==="
    run_tests "lifecycle_failover" "${CASE}/vars.reconfigure.json"
fi

echo "=== lifecycle complete ==="
