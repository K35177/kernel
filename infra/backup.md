# SQLite backup helper

`infra/sqlite_backup.py` creates consistent snapshots with SQLite's online
Backup API. Committed transactions in WAL mode are included without copying the
live database, WAL, or SHM files directly.

The source database, its parent, and the destination directory must already
exist and be private to the current user. The helper rejects symlinks,
hardlinked source files, permissive modes, and concurrent runs.

```bash
chmod 0700 /srv/example/backups
chmod 0600 /srv/example/state/database.sqlite3
python -m infra.sqlite_backup backup \
  --source /srv/example/state/database.sqlite3 \
  --destination /srv/example/backups
```

Each completed snapshot is a mode `0700` directory containing a mode `0600`
standalone database and manifest. A final snapshot name appears only after
`integrity_check`, `foreign_key_check`, hashing, and filesystem synchronization
succeed.

Verify a snapshot before considering it for restore:

```bash
python -m infra.sqlite_backup verify /srv/example/backups/snapshot-YYYYMMDDTHHMMSSZ-random
```

For recovery, stop every process that can write the live database, verify the
snapshot, preserve the failed database and any sidecars, and copy the verified
database to a temporary file on the same filesystem. Set the required owner and
mode, synchronize it, then atomically replace the live database. Start the API
first and validate it before starting the Telegram bot. Practice this procedure
with synthetic data; a backup is not proven until a restore has been tested.


## Service identity and privilege boundaries

The example `kernel-backup.service` starts Python directly as `User=kernel` and
`Group=kernel`, with empty capability bounding and ambient sets. The backup
helper uses that identity throughout; it does not start as root or perform a
later `setuid` transition. Give this account access only to the designated
source and backup directories. The example does not read the bot environment
file or require access to root-only configuration.

A design with a privileged parent and a child that changes identity has different
requirements. Do not copy its capabilities or user directives into this example.
Before activating a revised service, validate the exact unit with
`systemd-analyze verify`, then exercise it on synthetic SQLite data under the
same sandbox and systemd version as the target host. Verify the actual process
UID/GID, supplementary groups, and effective, permitted, inheritable, and
ambient capabilities. A capability bounding set alone does not prove that a
process has that capability available for a privilege transition.

Keep `NoNewPrivileges` and the other sandbox restrictions during validation.
Check a fresh snapshot independently, including committed WAL transactions,
and leave existing production data and completed backups intact.
