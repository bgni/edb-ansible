#!/bin/bash -eux
cd /workspace
make install-build
CASE_VARS="/workspace/tests/cases/${CASE_NAME}/vars.${EDB_OS}.json"
if [[ ! -f "${CASE_VARS}" ]]; then
	CASE_VARS="/workspace/tests/cases/${CASE_NAME}/vars.json"
fi
mkdir -p /root/.ssh
chmod 0700 /root/.ssh
cp /workspace/tests/cases/${CASE_NAME}/.ssh/id_rsa /root/.ssh/.
cp /workspace/tests/cases/${CASE_NAME}/.ssh/ssh_config /root/.ssh/.
chmod 0600 /root/.ssh/id_rsa
/workspace/tests/cases/${CASE_NAME}/.ssh/add_hosts.sh

# SKIP_PLAYBOOK re-runs the tests against a cluster that is already deployed.
# The deploy is the expensive part -- minutes -- while pytest is seconds, so
# iterating on a test does not need the cluster rebuilt. See
# `tests/run-case.sh --tests-only`.
if [[ "${SKIP_PLAYBOOK:-false}" != "true" ]]; then
	# More forks than the default 5: the larger cases have eight hosts, and
	# with the default the last ones wait for a free slot at every task.
	ANSIBLE_PIPELINING=1 ANSIBLE_FORKS="${ANSIBLE_FORKS:-10}" ansible-playbook \
		-i /workspace/tests/cases/${CASE_NAME}/inventory.yml \
		--extra-vars "repo_username=${EDB_REPO_USERNAME}" \
		--extra-vars "repo_password=${EDB_REPO_PASSWORD}" \
		--extra-vars "repo_token=${EDB_REPO_TOKEN}" \
		--extra-vars "enable_edb_repo=${EDB_ENABLE_REPO}" \
		--extra-vars "pg_type=${EDB_PG_TYPE}" \
		--extra-vars "pg_version=${EDB_PG_VERSION}" \
		--extra-vars "@${CASE_VARS}" \
		--extra-vars "ansible_core_version=${ANSIBLE_CORE_VERSION}" \
		--extra-vars "preinstalled=${PREINSTALLED:-false}" \
		--private-key /root/.ssh/id_rsa \
		/workspace/tests/cases/${CASE_NAME}/playbook.yml
fi

export EDB_SSH_USER=root
export EDB_SSH_KEY=/root/.ssh/id_rsa
export EDB_SSH_CONFIG=/root/.ssh/ssh_config
export EDB_INVENTORY=/workspace/tests/cases/${CASE_NAME}/inventory.yml
export EDB_ANSIBLE_VARS="${CASE_VARS}"
# PYTEST_ARGS narrows a re-run further, e.g. PYTEST_ARGS='-k mtls -x'.
py.test -v -k ${CASE_NAME} ${PYTEST_ARGS:-} /workspace/tests/tests
