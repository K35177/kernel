"""Shared SQLite access/auth repository. No Telegram calls inside transactions."""
import hashlib
import secrets
import time
import uuid

from .storage import Store

SCHEMA = '''
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_auth_expiry ON auth_sessions(expires_at);
CREATE TABLE IF NOT EXISTS auth_exchanges (
    fingerprint TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS access_requests (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','joined')),
    created_at REAL NOT NULL,
    decided_at REAL,
    decided_by INTEGER,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_access_active ON access_requests(user_id)
    WHERE status IN ('pending','approved');
CREATE INDEX IF NOT EXISTS ix_access_user ON access_requests(user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS access_memberships (
    user_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
    is_member INTEGER,
    telegram_status TEXT,
    checked_at REAL NOT NULL,
    error_code TEXT
);
CREATE TABLE IF NOT EXISTS access_invites (
    invite_link TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES access_requests(id),
    expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS access_notifications (
    owner_chat_id INTEGER NOT NULL,
    owner_message_id INTEGER NOT NULL,
    request_id TEXT NOT NULL REFERENCES access_requests(id),
    PRIMARY KEY(owner_chat_id,owner_message_id)
);
CREATE TABLE IF NOT EXISTS access_outbox (
    id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES access_requests(id),
    kind TEXT NOT NULL CHECK(kind IN ('owner_request','user_decision','join_approval','revoke_invite')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','done')),
    available_at REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    claim_token TEXT,
    error_code TEXT,
    UNIQUE(request_id,kind)
);
CREATE INDEX IF NOT EXISTS ix_access_outbox ON access_outbox(status,available_at);
'''


class ReplayError(ValueError):
    pass


class AccessConflict(ValueError):
    pass


class AccessStore:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def enqueue(conn, request_id, kind):
        conn.execute('INSERT INTO access_outbox(request_id,kind) VALUES (?,?) ON CONFLICT DO NOTHING',
                     (request_id, kind))

    def exchange(self, user, fingerprint, proof_expires_at, session_ttl):
        now = time.time()
        token = secrets.token_urlsafe(32)
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM auth_sessions WHERE expires_at<=?', (now,))
            conn.execute('DELETE FROM auth_exchanges WHERE expires_at<=?', (now,))
            if conn.execute('SELECT 1 FROM auth_exchanges WHERE fingerprint=?', (fingerprint,)).fetchone():
                raise ReplayError('initData already exchanged')
            conn.execute('''INSERT INTO users(telegram_id,full_name,username,updated_at) VALUES (?,?,?,?)
                ON CONFLICT(telegram_id) DO UPDATE SET full_name=excluded.full_name,
                username=excluded.username,updated_at=excluded.updated_at''',
                (user.id, user.display_name, user.username, now))
            conn.execute('INSERT INTO auth_exchanges VALUES (?,?)', (fingerprint, proof_expires_at))
            conn.execute('INSERT INTO auth_sessions VALUES (?,?,?)',
                         (hashlib.sha256(token.encode()).hexdigest(), user.id, now + session_ttl))
        return token

    def session_user(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            return None
        with self.store.connection() as conn:
            row = conn.execute('''SELECT u.telegram_id,u.full_name,u.username FROM auth_sessions s
                JOIN users u ON u.telegram_id=s.user_id WHERE s.token_hash=? AND s.expires_at>?''',
                (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            return dict(row) if row else None

    def latest(self, user_id):
        with self.store.connection() as conn:
            row = conn.execute('SELECT * FROM access_requests WHERE user_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
                               (user_id,)).fetchone()
            return dict(row) if row else None

    def request(self, user_id, cooldown=300):
        now = time.time()
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''SELECT * FROM access_requests WHERE user_id=?
                ORDER BY created_at DESC,rowid DESC LIMIT 1''', (user_id,)).fetchone()
            if row and row['status'] in ('pending', 'approved'):
                if row['status'] == 'approved':
                    invite = conn.execute('SELECT 1 FROM access_invites WHERE request_id=? AND expires_at>? AND revoked=0',
                                          (row['id'], now)).fetchone()
                    if not invite:
                        conn.execute('''UPDATE access_outbox SET status='pending',available_at=0
                            WHERE request_id=? AND kind='user_decision' AND status='done' ''', (row['id'],))
                return dict(row), False
            if row and row['status'] == 'rejected' and (row['decided_at'] or 0) + cooldown > now:
                raise AccessConflict('reapply_cooldown')
            request_id = uuid.uuid4().hex
            conn.execute('INSERT INTO access_requests(id,user_id,status,created_at) VALUES (?,?,?,?)',
                         (request_id, user_id, 'pending', now))
            self.enqueue(conn, request_id, 'owner_request')
            return dict(conn.execute('SELECT * FROM access_requests WHERE id=?', (request_id,)).fetchone()), True

    def get(self, request_id):
        with self.store.connection() as conn:
            row = conn.execute('''SELECT a.*,u.full_name,u.username FROM access_requests a
                JOIN users u ON u.telegram_id=a.user_id WHERE a.id=?''', (request_id,)).fetchone()
            return dict(row) if row else None

    def decide(self, request_id, decision, actor_id):
        if decision not in ('approved', 'rejected'):
            raise ValueError('Invalid decision')
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM access_requests WHERE id=?', (request_id,)).fetchone()
            if not row:
                raise AccessConflict('not_found')
            if row['status'] != 'pending':
                return dict(row), False
            conn.execute('''UPDATE access_requests SET status=?,decided_at=?,decided_by=?,version=version+1
                WHERE id=? AND status='pending' ''', (decision, time.time(), actor_id, request_id))
            self.enqueue(conn, request_id, 'user_decision')
            return dict(conn.execute('SELECT * FROM access_requests WHERE id=?', (request_id,)).fetchone()), True

    def membership(self, user_id, member, telegram_status=None, error=None):
        with self.store.connection() as conn:
            conn.execute('''INSERT INTO access_memberships VALUES (?,?,?,?,?) ON CONFLICT(user_id)
                DO UPDATE SET is_member=excluded.is_member,telegram_status=excluded.telegram_status,
                checked_at=excluded.checked_at,error_code=excluded.error_code''',
                (user_id, None if member is None else int(member), telegram_status, time.time(), error))
            if member is True:
                rows = conn.execute("SELECT id FROM access_requests WHERE user_id=? AND status IN ('pending','approved')",
                                    (user_id,)).fetchall()
                for row in rows:
                    conn.execute("UPDATE access_requests SET status='joined',version=version+1 WHERE id=?", (row['id'],))
                    self.enqueue(conn, row['id'], 'revoke_invite')

    def cached_membership(self, user_id):
        with self.store.connection() as conn:
            row = conn.execute('SELECT * FROM access_memberships WHERE user_id=?', (user_id,)).fetchone()
            return dict(row) if row else None

    def callback_matches(self, request_id, owner_chat_id, message_id):
        with self.store.connection() as conn:
            return bool(conn.execute('''SELECT 1 FROM access_notifications
                WHERE request_id=? AND owner_chat_id=? AND owner_message_id=?''',
                (request_id, owner_chat_id, message_id)).fetchone())

    def invite(self, request_id, *, include_expired=False):
        with self.store.connection() as conn:
            row = conn.execute('''SELECT * FROM access_invites WHERE request_id=? AND revoked=0
                AND expires_at>? ORDER BY expires_at DESC LIMIT 1''',
                (request_id, 0 if include_expired else time.time())).fetchone()
            return dict(row) if row else None

    def save_invite(self, request_id, link, expires_at):
        with self.store.connection() as conn:
            conn.execute('INSERT INTO access_invites VALUES (?,?,?,0)', (link, request_id, expires_at))

    def managed_invite(self, link):
        with self.store.connection() as conn:
            row = conn.execute('''SELECT a.*,i.expires_at,i.revoked FROM access_invites i
                JOIN access_requests a ON a.id=i.request_id WHERE i.invite_link=?''', (link,)).fetchone()
            return dict(row) if row else None

    def queue_join(self, request_id):
        with self.store.connection() as conn:
            self.enqueue(conn, request_id, 'join_approval')

    def mark_revoked(self, link):
        with self.store.connection() as conn:
            conn.execute('UPDATE access_invites SET revoked=1 WHERE invite_link=?', (link,))

    def claim_job(self):
        now = time.time()
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''SELECT * FROM access_outbox WHERE
                (status='pending' AND available_at<=?) OR (status='sending' AND lease_until<=?)
                ORDER BY id LIMIT 1''', (now, now)).fetchone()
            if not row:
                return None
            token = secrets.token_hex(16)
            conn.execute('''UPDATE access_outbox SET status='sending',lease_until=?,attempts=attempts+1,
                claim_token=? WHERE id=?''', (now + 300, token, row['id']))
            return dict(conn.execute('SELECT * FROM access_outbox WHERE id=?', (row['id'],)).fetchone())

    def finish_job(self, job, *, error=None, delay=0, notification=None):
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute("SELECT 1 FROM access_outbox WHERE id=? AND claim_token=? AND status='sending'",
                                (job['id'], job['claim_token'])).fetchone():
                return False
            if notification:
                conn.execute('''INSERT INTO access_notifications VALUES (?,?,?) ON CONFLICT DO NOTHING''',
                             (*notification, job['request_id']))
            conn.execute('''UPDATE access_outbox SET status=?,available_at=?,lease_until=0,error_code=?
                WHERE id=? AND claim_token=?''',
                ('pending' if error else 'done', time.time() + delay, error, job['id'], job['claim_token']))
            return True

    def retry(self, request_id):
        with self.store.connection() as conn:
            return conn.execute("UPDATE access_outbox SET available_at=0 WHERE request_id=? AND status='pending'", (request_id,)).rowcount

    def list_requests(self):
        with self.store.connection() as conn:
            rows = [dict(row) for row in conn.execute('''SELECT a.*,u.full_name,u.username FROM access_requests a
                JOIN users u ON u.telegram_id=a.user_id ORDER BY a.created_at DESC LIMIT 100''')]
            for row in rows:
                row['deliveries'] = [dict(job) for job in conn.execute('''SELECT kind,status,attempts,available_at,error_code
                    FROM access_outbox WHERE request_id=? ORDER BY id''', (row['id'],))]
            return rows
