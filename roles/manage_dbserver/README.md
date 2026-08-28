# manage_dbserver

`manage_dbserver` role is for managing the database cluster. It makes the
managing of the database cluster by giving key tasks. In all the roles, we have
used the tasks given in this role.

## Requirements

Following are the dependencies and requirement of this role.

  1. Ansible
  2. `community.general` Ansible Module - Utilized when creating aditional
     users during a Postgres Install


## Role variables

This role allows users to pass following variables which helps managing day to
day tasks:

### `pg_postgres_conf_params`

Using this parameters user can set the database parameters.
*Note*: To ensure the playbook runs successfully, input parameter names and values as strings.

Example:
```yaml
pg_postgres_conf_params:
  - name: "listen_addresses"
    value: "*"
```

### `pg_hba_ip_addresses`

With this parameter, user can manage HBA (Host Based Authentication) entries.

```yaml
pg_hba_ip_addresses:
  - contype: "host"
    users: "all"
    databases: "all"
    method: "scram-sha-256"
    source: "127.0.0.1/32"
    state: present
```

`options` carries whatever PostgreSQL puts after the authentication method,
which is how a rule refers to an identity map or demands a client certificate:

```yaml
pg_hba_ip_addresses:
  - contype: "hostssl"
    users: "app_user"
    databases: "all"
    method: "cert"
    source: "10.0.0.0/24"
    options: "map=mtls clientcert=verify-full"
```

### `pg_ident_maps`

Identity maps, used by `cert`, `peer` and `gssapi` authentication to translate
an external identity into a PostgreSQL role. For client-certificate (mTLS)
authentication the external identity is the certificate's Common Name.

```yaml
pg_ident_maps:
  - mapname: "mtls"
    system_username: "app_client"
    pg_username: "app_user"
  - mapname: "mtls"
    comment: "any host certificate in the internal domain maps to its CN"
    system_username: "/^(.*)\\.internal\\.example\\.com$"
    pg_username: "\\1"
    state: present
```

A `system_username` beginning with a slash is a regular expression, and `\1` in
`pg_username` refers to its first capture group. Entries with
`state: absent` are left out of the generated file.

The maps are rendered **in full** into a separate, Ansible-managed file
(`pg_ident_ansible.conf` by default, set by `pg_ident_managed_filename`), which
the main `pg_ident.conf` includes:

```
include_if_exists pg_ident_ansible.conf
```

The file name is deliberately unquoted. `postgresql.conf` requires
`include 'file'`, but `pg_ident.conf` and `pg_hba.conf` take the name
literally and keep any quotes as part of it -- so a quoted name makes
PostgreSQL look for a file whose name contains apostrophes, fail to find
it, and (because `include_if_exists` tolerates a missing file) load no
mappings at all while everything still looks correctly configured.

That keeps one owner per file. This role owns every line of the managed file
and regenerates it on each run, so removing an entry from `pg_ident_maps`
removes it from PostgreSQL; `pg_ident.conf` keeps whatever the distribution or
an operator put there and only ever gains the single include line. Requires
PostgreSQL 15 or later, which is when `include` directives were added to
`pg_ident.conf`.

Maps are applied before HBA entries, because an HBA rule naming a map that does
not exist yet is rejected on reload. Changes reload PostgreSQL; they do not
restart it.

### `pg_slots`

Replication slots management.

```yaml
pg_slots:
  - name: "physical_slot"
    slot_type: "physical"
    state: present
  - name: "logical_slot"
    slot_type: "logical"
    output_plugin: "test_decoding"
    state: present
    database: "edb"
```

### `pg_extensions`

Postgres extensions management.

```yaml
pg_extensions:
    - name: "postgis"
      database: "edb"
      state: present
```

### `pg_grant_privileges`

Grant privileges management.

```yaml
pg_grant_privileges:
    - roles: "efm_user"
      database: "edb"
      privileges: execute
      schema: pg_catalog
      objects: pg_current_wal_lsn(),pg_last_wal_replay_lsn(),pg_wal_replay_resume(),pg_wal_replay_pause()
      type: function
```

### `pg_grant_roles`

Grant roles management.

```yaml
pg_grant_roles:
    - role: pg_monitor
      user: enterprisedb
```

### `pg_sql_scripts`

SQL script execution.

```yaml
pg_sql_scripts:
    - file_path: "/usr/edb/as12/share/edb-sample.sql"
      db: edb
```

### `pg_copy_files`

Copy file on remote host.

```yaml
pg_copy_files:
    - file: "./test.sh"
      remote_file: "/var/lib/edb/test.sh"
      owner: efm
      group: efm
      mode: 0700
```

### `pg_query`

Execute a query on a database.

```yaml
pg_query:
    - query: "Update test set a=b"
      db: edb
```

### `pg_pgpass_values`

`.pgpass` file content management.

```yaml
pg_pgpass_values:
    - host: "127.0.0.1"
      database: edb
      user: enterprisedb
      password: <password>
      state: present
```

### `pg_databases`

Databases management.

```yaml
pg_databases:
    - name: edb_gis
      owner: edb
      encoding: UTF-8
```

Tablesapces management.

```yaml
pg_tablespaces:
    - name: index_tablespace
      owner: edb
      location: "/data/index_tablespace"
      state: present
```

## Dependencies

The `manage_dbserver` role does depend on the following collections:

  * `community.general`

## Example Playbook

### Inventory file content

Content of the `inventory.yml` file:

```yaml
---
all:
  children:
    primary:
      hosts:
        primary1:
          ansible_host: xxx.xxx.xxx.xxx
          private_ip: xxx.xxx.xxx.xxx
    standby:
      hosts:
        standby1:
          ansible_host: xxx.xxx.xxx.xxx
          private_ip: xxx.xxx.xxx.xxx
          upstream_node_private_ip: xxx.xxx.xxx.xxx
          replication_type: synchronous
        standby2:
          ansible_host: xxx.xxx.xxx.xxx
          private_ip: xxx.xxx.xxx.xxx
          upstream_node_private_ip: xxx.xxx.xxx.xxx
          replication_type: asynchronous
```

### How to include the `manage_dbserver` role in your Playbook

Below is an example of how to include the `manage_dbserver` role:

```yaml
---
- hosts: primary,standby
  name: Manage Postgres server
  become: yes
  gather_facts: yes
  any_errors_fatal: true

  collections:
    - edb_devops.edb_postgres

  pre_tasks:
    - name: Initialize the user defined variables
      set_fact:
        pg_version: 14
        pg_type: "PG"

        pg_postgres_conf_params:
          - name: listen_addresses
            value: "*"

        pg_hba_ip_addresses:
          - contype: "host"
            users: "all"
            databases: "all"
            method: "scram-sha-256"
            source: "127.0.0.1/32"
            state: present

        pg_slots:
          - name: "physcial_slot"
            slot_type: "physical"
            state: present
          - name: "logical_slot"
            slot_type: "logical"
            output_plugin: "test_decoding"
            state: present
            database: "edb"

  roles:
    - role: manage_dbserver
      when: "'manage_dbserver' in lookup('edb_devops.edb_postgres.supported_roles', wantlist=True)"
```

Defining and adding variables is done in the `set_fact` of the `pre_tasks`.

All the variables are available at:

  * [roles/manage_dbserver/defaults/main.yml](./defaults/main.yml)
  * [roles/manage_dbserver/vars/EPAS_RedHat.yml](./vars/EPAS_RedHat.yml)
  * [roles/manage_dbserver/vars/PG_RedHat.yml](./vars/PG_RedHat.yml)
  * [roles/manage_dbserver/vars/EPAS_Debian.yml](./vars/EPAS_Debian.yml)
  * [roles/manage_dbserver/vars/PG_Debian.yml](./vars/PG_Debian.yml)

## License

BSD

## Author information

Author:

  * Doug Ortiz
  * Julien Tachoires
  * Vibhor Kumar
  * EDB Postgres
  * DevOps
  * edb-devops@enterprisedb.com www.enterprisedb.com
