# Architecture

## Components

### Telegram bot

`bot.py` builds the aiogram dispatcher. It registers menu and join-request
handlers, owner moderation callbacks, the contact relay, and pacing middleware.
The bot is also the worker for durable access and delivery queues.

### FastAPI backend

`backend/app.py` exposes authentication, profile, access status, access request,
and owner-only administration routes. It binds to loopback through
`backend/__main__.py`; HTTPS termination belongs to the reverse proxy.

Request middleware limits body size, rejects unsafe authentication placement,
adds no-store headers, and rate-limits the authentication exchange. Validation
and unexpected-error handlers avoid reflecting submitted credentials.

### React Mini App

The frontend separates transport, Telegram integration, state transitions, and
presentation:

- `core/telegram.ts` is the adapter for Telegram Web Apps.
- `core/api.ts` owns the bearer session and API validation.
- `core/controller.ts` implements the UI state machine.
- `core/access-view.ts` maps backend state to user actions and labels.

Outside Telegram, the application fails closed and asks the user to launch it
from the bot. It does not accept identity from URL parameters or browser state.

### SQLite

The shared database stores users, contact messages, message routing, consumed
authentication exchanges, hashed sessions, access requests, membership cache,
managed invites, owner notifications, and durable outbox jobs.

Connections enable foreign keys and a busy timeout. Initialization sets WAL
mode, checks the schema version, and applies supported additive schema changes
inside an immediate transaction. Unknown future schema versions are rejected.

## Authentication flow

1. Telegram supplies signed `initData` to the Mini App.
2. The frontend sends the unmodified string to `POST /api/auth/telegram`.
3. The backend validates the HMAC, timestamp, field shape, and Telegram user.
4. A fingerprint prevents the same proof from being exchanged twice.
5. The backend returns a random bearer token and stores only its SHA-256 hash.
6. Personal and administration routes resolve identity from that session.

The frontend does not send an authoritative user ID. Fields such as
`initDataUnsafe` are never used as authentication evidence.

## Access flow

1. The backend checks current Telegram membership.
2. A non-member may create one active request; concurrent submissions collapse
   into the same record.
3. The durable outbox asks the bot to notify the owner.
4. Only the configured owner may decide the request.
5. Approval creates an expiring, managed invite rather than granting membership.
6. Actual group membership changes the request to `joined` and revokes the
   managed invite.

All decision and queue transitions use conditional writes so repeated callbacks
remain idempotent.

## Contact flow

Incoming user messages are recorded before delivery. Telegram message IDs are
mapped to an internal contact record, so an owner reply can be routed without
trusting message text. Foreign chats, unrelated replies, conflicting mappings,
and duplicate updates fail closed.

An interrupted Telegram write is marked `uncertain` when delivery cannot be
proved. This prevents automatic duplicate messages after restart.

## Trust boundaries

- The browser and all submitted JSON are untrusted.
- Telegram `initData` becomes trusted only after backend HMAC validation.
- The owner ID and root group ID come only from protected server configuration.
- Caddy exposes built frontend assets and `/api`; source, configuration, and
  database paths are denied.
- SQLite is private runtime state and is never a frontend artifact.
