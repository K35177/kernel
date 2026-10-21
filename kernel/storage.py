"""Stage 1 SQLite repository; all SQL is parameterized.

Use through asyncio.to_thread in asynchronous handlers. Each operation uses its
own short transaction; no database transaction spans a Telegram API call.
"""
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import time


SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY CHECK (telegram_id > 0),
    full_name TEXT NOT NULL,
    username TEXT,
    contact_active INTEGER NOT NULL DEFAULT 0 CHECK (contact_active IN (0, 1)),
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS contact_messages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    content_text TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    sent_at REAL NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued', 'notifying', 'notified', 'delivering', 'delivered', 'failed', 'uncertain')),
    retry_at REAL NOT NULL DEFAULT 0,
    failure_code TEXT,
    UNIQUE (source_chat_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS ix_contact_history ON contact_messages(user_id, id DESC);
CREATE INDEX IF NOT EXISTS ix_contact_queue ON contact_messages(status, retry_at, id);
CREATE TABLE IF NOT EXISTS message_mappings (
    owner_chat_id INTEGER NOT NULL,
    owner_message_id INTEGER NOT NULL,
    contact_message_id INTEGER NOT NULL REFERENCES contact_messages(id),
    kind TEXT NOT NULL CHECK (kind IN ('notification', 'copy')),
    PRIMARY KEY (owner_chat_id, owner_message_id)
);
'''


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA busy_timeout = 10000')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Set permissions before SQLite creates WAL/SHM sidecars, including
        # when DATABASE_PATH points into an existing shared directory.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.path.chmod(0o600)
        # Do not reset an existing schema or delete any previous data.
        with self.connection() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2):
                raise RuntimeError('Unsupported database version; migration required')
            conn.execute('PRAGMA journal_mode = WAL')
            from .access_store import SCHEMA as ACCESS_SCHEMA
            # Both processes may start together. SQLite serializes this entire
            # additive migration; an interrupted transaction rolls back.
            conn.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + ACCESS_SCHEMA
                               + '\nPRAGMA user_version = 2;\nCOMMIT;')

    def set_contact(self, user_id: int, full_name: str, username: str | None, active: bool):
        with self.connection() as conn:
            conn.execute('''INSERT INTO users
                (telegram_id, full_name, username, contact_active, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET full_name=excluded.full_name,
                username=excluded.username, contact_active=excluded.contact_active,
                updated_at=excluded.updated_at''',
                (user_id, full_name, username, int(active), time.time()))

    def contact_active(self, user_id: int) -> bool:
        with self.connection() as conn:
            row = conn.execute('SELECT contact_active FROM users WHERE telegram_id = ?',
                               (user_id,)).fetchone()
            return bool(row and row['contact_active'])

    def enqueue(self, *, user_id: int, full_name: str, username: str | None,
                direction: str, source_chat_id: int, source_message_id: int,
                content_type: str, content_text: str, payload_json: str,
                sent_at: float) -> tuple[int, bool]:
        with self.connection() as conn:
            conn.execute('''INSERT INTO users (telegram_id, full_name, username, updated_at)
                VALUES (?, ?, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET
                full_name=excluded.full_name, username=excluded.username,
                updated_at=excluded.updated_at''', (user_id, full_name, username, time.time()))
            cursor = conn.execute('''INSERT INTO contact_messages
                (user_id, direction, source_chat_id, source_message_id, content_type,
                 content_text, payload_json, sent_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_chat_id, source_message_id)
                DO NOTHING''', (user_id, direction, source_chat_id, source_message_id,
                content_type, content_text, payload_json, sent_at, time.time()))
            inserted = bool(cursor.rowcount)
            row = conn.execute('SELECT * FROM contact_messages WHERE source_chat_id = ? AND source_message_id = ?',
                               (source_chat_id, source_message_id)).fetchone()
            if row['user_id'] != user_id or row['direction'] != direction:
                raise ValueError('Source message is already assigned to another conversation')
            return row['id'], inserted

    def resolve_reply(self, owner_chat_id: int, owner_message_id: int):
        with self.connection() as conn:
            row = conn.execute('''SELECT c.*, u.full_name, u.username FROM message_mappings m
                JOIN contact_messages c ON c.id=m.contact_message_id
                JOIN users u ON u.telegram_id=c.user_id
                WHERE m.owner_chat_id=? AND m.owner_message_id=? AND c.direction='inbound' ''',
                (owner_chat_id, owner_message_id)).fetchone()
            return dict(row) if row else None

    def get(self, message_id: int):
        with self.connection() as conn:
            row = conn.execute('''SELECT c.*, u.full_name, u.username FROM contact_messages c
                JOIN users u ON u.telegram_id=c.user_id WHERE c.id=?''', (message_id,)).fetchone()
            return dict(row) if row else None

    def history(self, user_id: int, before_id: int, limit: int = 5):
        with self.connection() as conn:
            rows = conn.execute('''SELECT * FROM contact_messages
                WHERE user_id=? AND id<? ORDER BY id DESC LIMIT ?''',
                (user_id, before_id, limit)).fetchall()
            return [dict(row) for row in reversed(rows)]

    def next_pending(self):
        with self.connection() as conn:
            row = conn.execute('''SELECT c.*, u.full_name, u.username FROM contact_messages c
                JOIN users u ON u.telegram_id=c.user_id
                WHERE c.status IN ('queued', 'notified') AND c.retry_at<=?
                ORDER BY CASE c.direction WHEN 'outbound' THEN 0 ELSE 1 END, c.id LIMIT 1''',
                (time.time(),)).fetchone()
            return dict(row) if row else None

    def claim(self, message_id: int, previous: str, current: str) -> bool:
        with self.connection() as conn:
            return bool(conn.execute('UPDATE contact_messages SET status=? WHERE id=? AND status=?',
                                     (current, message_id, previous)).rowcount)

    def finish(self, message_id: int, status: str, *, failure_code: str | None = None,
               retry_at: float = 0, mapping: tuple[int, int, str] | None = None):
        with self.connection() as conn:
            if mapping:
                chat_id, telegram_message_id, kind = mapping
                # No REPLACE/UPSERT: a conflicting routing key must fail closed.
                conn.execute('''INSERT INTO message_mappings
                    (owner_chat_id, owner_message_id, contact_message_id, kind) VALUES (?, ?, ?, ?)''',
                    (chat_id, telegram_message_id, message_id, kind))
            conn.execute('UPDATE contact_messages SET status=?, failure_code=?, retry_at=? WHERE id=?',
                         (status, failure_code, retry_at, message_id))

    def recover_interrupted(self) -> int:
        # Bot API has no idempotency key. An interrupted write may already have
        # reached Telegram: never send an owner answer twice automatically.
        with self.connection() as conn:
            cursor = conn.execute('''UPDATE contact_messages SET status='uncertain',
                failure_code='interrupted' WHERE status IN ('notifying', 'delivering')''')
            return cursor.rowcount
