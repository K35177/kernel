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
