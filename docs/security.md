# Security design

## Protected assets

- Telegram bot token
- owner and private-group identifiers
- signed Telegram `initData` and bearer sessions
- private message content and routing metadata
- SQLite database, WAL/SHM files, and backups
- server paths, inventory, and operational evidence

None of these assets belongs in Git, frontend assets, CI logs, public issues, or
release archives.

## Authentication controls

The backend validates Telegram's HMAC using constant-time comparison, enforces
a short proof age, rejects future timestamps, duplicate query keys, malformed
encoding, duplicate JSON keys, bots, and invalid user fields. It derives the
user identity only from the validated payload.

Each proof may be exchanged once. Sessions are random, short-lived, and stored
as hashes. Authentication is accepted only through the bearer header; query
parameters and arbitrary client user IDs are not trusted.

## Authorization controls

Owner API routes compare the authenticated Telegram identity with protected
server configuration. Telegram moderation callbacks additionally require a
saved notification mapping. Root-group operations use the configured negative
group identifier and verify Telegram membership rather than relying on an
approval label.

## Data and delivery safety

- SQL values are parameterized.
- Foreign keys are enabled for every connection.
- Active access requests are unique per user.
- Conditional state changes make owner decisions idempotent.
- External Telegram calls happen outside database transactions.
- Durable outboxes survive restart and use expiring worker leases.
- Ambiguous interrupted sends are quarantined instead of automatically retried.
- Error messages and logs avoid echoing tokens, init data, and request bodies.

## HTTP and browser controls

Production configuration requires explicit HTTPS origins and host names.
Wildcard hosts are rejected. The API limits authentication attempts and request
body size, disables credentials in CORS, and marks responses as no-store.

The frontend keeps its session in memory. It does not persist Telegram proof or
bearer tokens in local storage. External and Telegram links are validated before
navigation.

## Deployment controls

The example services use an unprivileged account, restrictive umask, systemd
sandboxing, no Linux capabilities, and a dedicated writable state directory.
Caddy exposes only the built frontend and API route. It blocks common source,
configuration, and database paths before the SPA fallback.

The backup example runs without network access or Linux capabilities. It
rejects permissive paths, symlinked inputs, hardlinked databases, concurrent
runs, integrity failures, and foreign-key violations. Completed snapshots and
their manifests are private files; their hashes prove file identity, not
correctness or recoverability. Restore exercises remain mandatory.

## Known boundaries

- SQLite is appropriate for the current single-host deployment, not horizontal
  multi-writer scaling.
- A bot token cannot be recovered safely after disclosure; rotate it through
  BotFather before cleaning history.
- Secret scanning reduces accidental exposure but does not prove that a system
  is free of sensitive business data.
- The example backup timer does not implement retention or off-host copies.
- The infrastructure examples still require host-specific review, backups, and
  rollback planning.
