# Deployment

This document describes a reference deployment. It does not target an existing
server and does not automate production changes.

## Layout

The included examples assume:

```text
/opt/kernel/current/             application release
/opt/kernel/current/.venv/       release virtual environment
/etc/kernel/kernel.env           root-owned secrets and settings
/etc/kernel/production-approved  explicit API start gate
/etc/kernel/polling-approved     explicit bot polling gate
/var/lib/kernel/                 private SQLite state
/var/www/kernel/current/         built frontend
```

Use a dedicated `kernel` system user without an interactive login. The service
examples rely on systemd to create `/var/lib/kernel` with mode `0700`.

## Configuration

Create `/etc/kernel/kernel.env` manually with mode `0600` and ownership readable
only by the service account. Required private values are:

- `BOT_TOKEN`
- `OWNER_ID`
- `ROOT_GROUP_ID`
- `DATABASE_PATH=/var/lib/kernel/kernel.sqlite3`

For production, also set an HTTPS `MINI_APP_URL`, `API_ENV=production`, the
single expected `API_ALLOWED_ORIGINS`, and explicit `API_ALLOWED_HOSTS`.

Do not place credentials in a unit file, Caddyfile, release archive, shell
history, frontend variable, or GitHub Actions secret unless a future deployment
workflow explicitly needs it. The current workflows never deploy.

## Build the release

Create the virtual environment inside an immutable release directory and use
the reviewed constraints:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -c infra/python-constraints.txt
```

Build the frontend without private variables:

```bash
cd frontend
npm ci
npm run build
```

Publish only `frontend/dist/` to the web root. Never publish the project root.

## Database initialization and migration

Application startup creates a missing database with restrictive permissions.
For an existing database, take an online SQLite backup first and verify it
before starting new code. Initialization accepts only known schema versions and
applies the supported additive migration transactionally.

Do not copy a live database file while writers are active. Use SQLite's backup
API or stop both services and preserve the database together with any WAL/SHM
state before moving it.

## systemd

Review and install the files from `infra/systemd/`. They include an explicit
approval gate, loopback API binding, single-poller lock, restart limits, an
empty capability set, and filesystem/kernel protections.

Create the approval files only after configuration, database backup, ownership,
and paths have been verified. Enable the API first. Enable bot polling only
after proving that no other bot instance is polling the same token.

## Caddy

Copy `infra/caddy/kernel.caddy.example`, replace `kernel.example.com` with the
public hostname, and review it as part of the existing Caddy configuration.
The example proxies `/api` to loopback, serves only built frontend files, and
returns 404 for source, configuration, and database paths.

Telegram embeds the Mini App, so do not add a blanket `X-Frame-Options: DENY`
or a `frame-ancestors 'none'` policy.

## Validation

Before enabling the bot, verify:

1. the API listens only on loopback;
2. HTTPS serves the expected frontend build;
3. private paths return 404;
4. invalid and expired Telegram proof is rejected without being echoed;
5. the SQLite backup can be opened and passes `PRAGMA integrity_check`;
6. only one polling process can hold the bot lock;
7. systemd logs contain no credentials or message bodies.
