"""
Finding checks for replication, synchronous durability, slots and archiving.

Covers EDB-03, EDB-04, EDB-05, EDB-06, EDB-08, EDB-14, EDB-17, EDB-18.
"""

import re

from harness import (
    CONFIG_DIRS, PRESENT, FIXED, PARTIAL, Result, quote, missing,
)


def check_edb_03(repo):
    """
    EDB-03: the synchronous durability policy must be installed on every
    promotion-capable node, not only on the inventory primary, and must be
    read back and verified after it is written.
    """
    setup = 'roles/setup_replication/tasks/setup_replication.yml'
    param = 'roles/setup_replication/tasks/primary_synchronous_param.yml'
    pernode = 'roles/setup_replication/tasks/configure_synchronous_node.yml'

    evidence = []

    # The reviewed defect: the sync-policy import was delegated to the primary,
    # so standbys never received a promotion-ready value.
    delegated = [
        (n, line) for n, line in repo.find(setup, r'delegate_to.*primary_inventory_hostname')
    ]
    # Only care whether the *synchronous* import is the delegated one. Find the
    # task block that imports primary_synchronous_param.yml.
    lines = repo.lines(setup)
    sync_import = None
    for n, line in enumerate(lines, start=1):
        if 'primary_synchronous_param.yml' in line:
            sync_import = n
            break

    if sync_import is None:
        return Result('EDB-03', PRESENT,
                      'the synchronous policy import was not found at all',
                      ['%s: no import of primary_synchronous_param.yml' % setup])

    # Look at the task block following the import for a delegate_to.
    block = lines[sync_import - 1:sync_import + 12]
    delegated_sync = any('delegate_to' in b and 'primary_inventory_hostname' in b
                         for b in block)
    evidence.append(quote(setup, sync_import, lines[sync_import - 1]))

    # The fix fans the policy out over every promotion candidate.
    fanout = repo.find(param, r'loop:\s*"\{\{\s*pg_cluster_nodes')
    candidate_when = repo.find(param, r"node_type in \['primary', 'standby'\]")
    verified = repo.find(pernode, r'SHOW synchronous_standby_names')
    asserted = repo.find(pernode, r'ansible\.builtin\.assert')

    for rel, hits in ((param, fanout), (param, candidate_when),
                      (pernode, verified), (pernode, asserted)):
        for n, line in hits:
            evidence.append(quote(rel, n, line))

    if delegated_sync:
        return Result('EDB-03', PRESENT,
                      'synchronous policy is still delegated to the inventory '
                      'primary only', evidence)

    if fanout and candidate_when and verified and asserted:
        return Result('EDB-03', FIXED,
                      'synchronous policy is applied to every promotion '
                      'candidate and read back with SHOW + assert', evidence)

    return Result('EDB-03', PARTIAL,
                  'policy is no longer primary-only but the per-node apply or '
                  'verify step is incomplete', evidence)


def check_edb_04(repo):
    """
    EDB-04: num_sync must be an explicit policy decision, not inferred from
    the number of inventory hosts labelled `replication_type: synchronous`.

    Note the residual risk this check reports even when the generator is
    fixed: the shipped default is num_sync=1, which for the reviewed
    three-standby consistency-first design is weaker than the `ANY 2` the
    review recommends. That is a deployment decision, so it is surfaced as
    PARTIAL rather than PRESENT.
    """
    defaults = 'roles/setup_replication/defaults/main.yml'
    param = 'roles/setup_replication/tasks/primary_synchronous_param.yml'
    evidence = []

    # The reviewed defect: num_sync came from `_synchronous_standbys | length`.
    derived = repo.find(param, r'_synchronous_standbys.*\|\s*length')
    explicit = repo.find(param, r'synchronous_standby_num_sync\s*\|\s*int')
    num_sync_default = repo.find(defaults, r'^synchronous_standby_num_sync:')
    quorum_default = repo.find(defaults, r'^standby_quorum_type:')

    for rel, hits in ((param, derived), (param, explicit),
                      (defaults, num_sync_default), (defaults, quorum_default)):
        for n, line in hits:
            evidence.append(quote(rel, n, line))

    # Is the generated string built from the explicit knob?
    generator = repo.find(
        param, r'standby_quorum_type.*upper.*synchronous_standby_num_sync')
    for n, line in generator:
        evidence.append(quote(param, n, line))

    if derived and not explicit:
        return Result('EDB-04', PRESENT,
                      'num_sync is still derived from the count of standbys '
                      'labelled synchronous', evidence)

    if not (explicit and generator):
        return Result('EDB-04', PRESENT,
                      'no explicit num_sync knob feeds the generated '
                      'synchronous_standby_names', evidence)

    # Generator is explicit. Now judge the shipped default.
    default_value = None
    for n, line in num_sync_default:
        m = re.search(r':\s*(\d+)', line)
        if m:
            default_value = int(m.group(1))

    if default_value is not None and default_value < 2:
        return Result(
            'EDB-04', PARTIAL,
            'num_sync is now explicit (generator fixed), but the shipped '
            'default of %s yields ANY 1 for a three-standby cluster, which is '
            'weaker than the ANY 2 the review recommends for RPO=0'
            % default_value,
            evidence)

    return Result('EDB-04', FIXED,
                  'num_sync is an explicit, bounds-checked policy value',
                  evidence)


def check_edb_05(repo):
    """
    EDB-05: initialization must not enable archiving with a fake
    archive_command. `/bin/true` exits zero, so PostgreSQL believes each
    segment was archived and is free to recycle it.
    """
    evidence = []
    hits = repo.grep(r"archive_command\s*=\s*'/bin/true'",
                     subdirs=('roles',),
                     suffixes=('.template', '.j2', '.yml'))
    for rel, n, line in hits:
        evidence.append(quote(rel, n, line))

    if not hits:
        return Result('EDB-05', FIXED,
                      "no template sets archive_command to '/bin/true'",
                      [missing("archive_command = '/bin/true'")])

    return Result(
        'EDB-05', PRESENT,
        "%d template(s) enable archiving with a fake '/bin/true' "
        'archive_command' % len(hits),
        evidence)


def check_edb_06(repo):
    """
    EDB-06: a completed backup is not recovery proof. Something must restore
    into an isolated directory, replay to a target and verify the data.
    """
    evidence = []

    restore = repo.grep(r'pgbackrest.*\brestore\b',
                        subdirs=('roles', 'tests'),
                        suffixes=('.yml', '.py', '.j2', '.template'))
    pitr = repo.grep(r'recovery_target|--target=|--type=time',
                     subdirs=('roles', 'tests'),
                     suffixes=('.yml', '.py', '.j2', '.template'))

    # What the collection *does* do, for contrast in the report.
    backup = repo.grep(r'--type=full backup', subdirs=('roles',),
                       suffixes=('.yml',))
    check = repo.grep(r'pgbackrest --stanza=.* check', subdirs=('roles',),
                      suffixes=('.yml',))
    for rel, n, line in backup + check:
        evidence.append(quote(rel, n, line))

    if restore or pitr:
        for rel, n, line in (restore + pitr):
            evidence.append(quote(rel, n, line))
        return Result('EDB-06', FIXED,
                      'a restore/PITR exercise exists in the collection',
                      evidence)

    evidence.append(missing('pgbackrest restore / recovery_target / --type=time'))
    return Result(
        'EDB-06', PRESENT,
        'backups are created and catalogued but never restored, replayed or '
        'data-verified anywhere in roles/ or tests/', evidence)


def check_edb_08(repo):
    """
    EDB-08: replication slots are on by default, so an unbounded
    max_slot_wal_keep_size lets a single down standby pin WAL until pg_wal
    fills and the primary stops.
    """
    evidence = []
    slots_default = repo.find('roles/setup_replication/defaults/main.yml',
                              r'^use_replication_slots:')
    for n, line in slots_default:
        evidence.append(quote('roles/setup_replication/defaults/main.yml', n, line))

    cap = repo.grep(r'max_slot_wal_keep_size',
                    subdirs=CONFIG_DIRS,
                    suffixes=('.yml', '.j2', '.template', '.json'))

    slots_on = any('true' in line.lower() for _, line in slots_default)

    if cap:
        for rel, n, line in cap:
            evidence.append(quote(rel, n, line))
        return Result('EDB-08', FIXED,
                      'max_slot_wal_keep_size is configured', evidence)

    evidence.append(missing('max_slot_wal_keep_size'))
    if not slots_on:
        return Result('EDB-08', PARTIAL,
                      'max_slot_wal_keep_size is never set, but replication '
                      'slots are not enabled by default', evidence)

    return Result(
        'EDB-08', PRESENT,
        'replication slots are enabled by default and max_slot_wal_keep_size '
        'is never set or validated, so slot WAL retention is unbounded',
        evidence)


def check_edb_14(repo):
    """
    EDB-14: the physical slot is pre-created on the upstream node, so
    pg_basebackup must not also be told to create it with -C. This is the
    check that proves a fresh standby build can get past base backup.
    """
    basebackup = 'roles/setup_replication/tasks/pg_basebackup.yml'
    upstream = 'roles/setup_replication/tasks/upstream_node_slots.yml'
    evidence = []

    slot_flag = repo.find(basebackup, r'--slot=')
    for n, line in slot_flag:
        evidence.append(quote(basebackup, n, line))

    precreate = repo.find(upstream, r'slot_type: physical|name:.*regex_replace')
    for n, line in precreate:
        evidence.append(quote(upstream, n, line))

    # -C as a standalone pg_basebackup flag (create-slot).
    creates_slot = repo.find(basebackup, r"(^|\s)-C(\s|')")
    # ...and in the base command defined per platform.
    base_cmd = repo.grep(r'pg_basebackup\s+-', subdirs=('roles/setup_replication/vars',),
                         suffixes=('.yml',))
    base_creates = [(rel, n, line) for rel, n, line in base_cmd
                    if re.search(r"(^|\s)-C(\s|$)", line)]

    for n, line in creates_slot:
        evidence.append(quote(basebackup, n, line))
    for rel, n, line in base_creates:
        evidence.append(quote(rel, n, line))

    if creates_slot or base_creates:
        return Result(
            'EDB-14', PRESENT,
            'pg_basebackup still passes -C for a slot the upstream node has '
            'already created; fresh standby bootstrap fails', evidence)

    if not slot_flag:
        return Result('EDB-14', PARTIAL,
                      'pg_basebackup no longer passes -C but also does not '
                      'pass --slot; slot reuse is unverified', evidence)

    return Result(
        'EDB-14', FIXED,
        'pg_basebackup reuses the pre-created slot with --slot and no -C',
        evidence)


def check_edb_17(repo):
    """
    EDB-17: streaming standbys need a real PostgreSQL restore_command so they
    can bridge a WAL gap from the archive instead of requiring a re-clone.

    A pgBackRest `[global:archive-get]` section does nothing on its own;
    PostgreSQL has to be told to call it.
    """
    evidence = []

    # Every restore_command occurrence, classified.
    hits = repo.grep(r'restore_command',
                     subdirs=('roles', 'playbook-examples'),
                     suffixes=('.yml', '.j2', '.template'))

    active = []
    for rel, n, line in hits:
        stripped = line.strip()
        commented = stripped.startswith('#')
        # EFM writes a deliberately-failing restore_command into a fence file.
        efm_fence = 'setup_efm' in rel
        if not commented and not efm_fence:
            active.append((rel, n, line))
        evidence.append(quote(rel, n, line))

    # The archive-get section that is configured but never invoked.
    archive_get = repo.grep(r'\[global:archive-get\]',
                            subdirs=('roles/setup_pgbackrest',),
                            suffixes=('.template', '.j2'))
    for rel, n, line in archive_get:
        evidence.append(quote(rel, n, line))

    if active:
        return Result('EDB-17', FIXED,
                      'an active PostgreSQL restore_command is installed',
                      evidence)

    if not hits:
        evidence.append(missing('restore_command'))

    return Result(
        'EDB-17', PRESENT,
        'no active PostgreSQL restore_command is ever installed; standbys can '
        'stream but cannot fall back to archive-get, so a WAL gap forces a '
        're-clone', evidence)


def check_edb_18(repo):
    """
    EDB-18: a promotion-capable standby must not carry `/bin/true` as its
    archive_command. archive_mode=on keeps the fake command inert while the
    node is in recovery, so the breakage only appears at promotion -- which
    is exactly what makes it silent.
    """
    backup_cfg = 'roles/setup_pgbackrest/tasks/configure_pg_backup.yml'
    post_cfg = 'roles/setup_pgbackrest/tasks/post_configure_pgbackrest.yml'
    pg_settings = 'roles/setup_pgbackrest/tasks/configure_pg_settings.yml'
    defaults = 'roles/setup_pgbackrestserver/defaults/main.yml'
    evidence = []

    # 1. Standbys get archive_mode but no archive_command from this path.
    for n, line in repo.find(backup_cfg, r'archive_mode|archive_command|standby_present'):
        evidence.append(quote(backup_cfg, n, line))

    # 2. The real archive-push command is gated on backup_standby.
    gate = repo.find(post_cfg, r"backup_standby\s*==\s*'y'")
    for n, line in gate:
        evidence.append(quote(post_cfg, n, line))

    # 3. The shipped default of that gate.
    default_hits = repo.find(defaults, r'^backup_standby:')
    for n, line in default_hits:
        evidence.append(quote(defaults, n, line))

    # 4. The real command itself.
    for n, line in repo.find(pg_settings, r'archive-push'):
        evidence.append(quote(pg_settings, n, line))

    default_is_n = any(re.search(r':\s*"?n"?\s*$', line) for _, line in default_hits)

    # Does init leave /bin/true behind for the standby to inherit?
    fake = repo.grep(r"archive_command\s*=\s*'/bin/true'",
                     subdirs=('roles/init_dbserver',),
                     suffixes=('.template',))

    if not fake:
        return Result('EDB-18', FIXED,
                      'standbys have no fake archive_command to inherit',
                      evidence)

    if gate and default_is_n:
        return Result(
            'EDB-18', PRESENT,
            "standbys inherit archive_command='/bin/true' from the init "
            'template; the real archive-push command is applied only when '
            "backup_standby == 'y', and the shipped default is \"n\"",
            evidence)

    if not gate:
        return Result('EDB-18', FIXED,
                      'the real archive-push command is applied to standbys '
                      'unconditionally', evidence)

    return Result('EDB-18', PARTIAL,
                  'the archive-push gate exists but its default could not be '
                  'confirmed', evidence)


CHECKS = (
    check_edb_03,
    check_edb_04,
    check_edb_05,
    check_edb_06,
    check_edb_08,
    check_edb_14,
    check_edb_17,
    check_edb_18,
)
