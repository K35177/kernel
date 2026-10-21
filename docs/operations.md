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

Use SQLite's online backup mechanism while either service may be running. Store
the result outside the release tree with mode `0600`. Verify the copy with
`PRAGMA integrity_check`, record its schema version and checksum privately, and
test restore procedures on a separate path.

Never copy only the main database file from an active WAL database. Never place
database backups in Git or a web root.

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
