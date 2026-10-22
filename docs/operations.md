# Operations

## Service health

The API and bot are independent systemd services. The API may run without bot
polling, but access notifications and Telegram delivery remain queued until the
bot worker is available.

Use systemd status and narrowly scoped journal queries. Do not export the full
journal to a public report. The application intentionally logs event classes
and error types rather than request bodies or Telegram payloads.

## Startup order

1. Verify the environment file, ownership, release path, and database backup.
2. Start the API and perform local HTTP checks through loopback.
3. Verify HTTPS and private-path denial through Caddy.
4. Confirm that no other process polls the bot token.
5. Create the polling approval gate and start the bot.
6. Perform a test-user authentication and access-state check.

## SQLite backups

Use SQLite's online backup mechanism while either service may be running. The
reference helper in [`infra/sqlite_backup.py`](../infra/sqlite_backup.py)
captures committed WAL transactions, verifies integrity and foreign keys, and
publishes a private snapshot directory only after its database and manifest
have been synchronized.

Never copy only the main database file from an active WAL database. Never place
database backups in Git or a web root.

The example `kernel-backup.service` runs as the same unprivileged account as the
application, has no network access or Linux capabilities, and may write only to
the state and backup directories. Create `/var/backups/kernel` in advance with
owner `kernel:kernel` and mode `0700`. Adapt all paths before installing the
example.

The timer is persistent: a missed run may execute after the host starts. It
does not prune snapshots. Monitor backup age and available space, and define a
retention policy appropriate for the deployment before enabling the timer.

### Restore exercise

A successful backup job is evidence that a snapshot was created, not that the
system can be recovered. Periodically test restoration with synthetic or
appropriately protected data on a separate path:

1. Select a snapshot and run `python -m infra.sqlite_backup verify SNAPSHOT`.
2. Copy its database to an isolated location; never test against the live path.
3. Start a disposable application instance against that copy.
4. Exercise authentication, access status, message routing, and migrations
   without contacting the production Telegram bot.
5. Record the test time, snapshot age, schema version, and result without user
   identifiers or message content.

For an actual restore, stop every database writer and preserve the failed main
database plus any WAL/SHM files. Verify the selected snapshot again, copy it to
a temporary file on the same filesystem as the live database, set the expected
owner and mode, call `fsync`, and use an atomic rename. Start and validate the
API before starting the single Telegram polling process. Keep the preserved
files until application and data checks pass.

## systemd reliability and hardening

The API and bot examples combine `Restart=on-failure` with a bounded start rate.
Configuration failures exit with status 2 and do not loop. Bot lock contention
uses status 75 and also suppresses restart, which prevents two local pollers
from repeatedly competing for the same token.

Resource controls bound memory, tasks, file descriptors, and core dumps.
Filesystem protections leave only the application state writable. Empty
capability and ambient-capability sets, restricted address families, private
devices, and `NoNewPrivileges` reduce the impact of a compromised process.
These settings are examples: validate them against the target systemd version
and application workload before installation.

The services use `LogNamespace=kernel`. Install `journald@kernel.conf` as
`/etc/systemd/journald@kernel.conf` before starting those units, then query the
isolated journal narrowly:

```bash
journalctl --namespace=kernel -u kernel-api.service --since today --no-pager
journalctl --namespace=kernel -u kernel-backup.service -n 50 --no-pager
```

The example namespace has explicit size, free-space, retention, and rate
limits. Journal isolation does not make log content safe; tokens, bearer
headers, Telegram payloads, message bodies, and raw database rows must still
never be logged.

## Updates

Prepare each release in a new directory. Install dependencies with the reviewed
constraints, run backend and frontend checks, build the frontend, and switch
the `current` paths only after verification. Keep the previous release and a
verified pre-migration database backup until the new version has passed its
smoke tests.

Schema initialization supports only known versions and refuses an unexpected
future schema. Treat that refusal as a stop condition rather than bypassing it.

## Failure handling

- Repeated API failure: stop the API, preserve the database and journal excerpt,
  and return the release pointer to the previous tested release.
- Polling conflict: stop the new bot immediately and identify the other poller
  before retrying.
- Ambiguous Telegram send: inspect the durable `uncertain` state; do not resend
  automatically.
- Database integrity failure: stop both writers, preserve all SQLite files, and
  restore only from a verified backup.
- Secret exposure: revoke the credential first, then clean any distribution
  channel and Git history.

## Observability without disclosure

Useful operational signals are service state, restart count, HTTP status,
database integrity, queue counts, oldest pending age, and schema version. User
IDs, names, message bodies, tokens, invite links, bearer headers, and raw
Telegram responses should remain out of dashboards and incident documents.
