from __future__ import annotations

from contextlib import closing, redirect_stderr
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from infra import sqlite_backup


class SQLiteBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.source = self.root / "source.sqlite3"
        self.destination = self.root / "backups"
        self.destination.mkdir(mode=0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def database(self, *, invalid_foreign_key: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(self.source)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        connection.execute("CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parent(id))")
        connection.execute("INSERT INTO parent VALUES (1, 'synthetic row')")
        if invalid_foreign_key:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("INSERT INTO child VALUES (1, 999)")
        else:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("INSERT INTO child VALUES (1, 1)")
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
        self.source.chmod(0o600)
        return connection

    def snapshots(self) -> list[Path]:
        return sorted(path for path in self.destination.iterdir()
                      if path.name != sqlite_backup.LOCK_NAME)

    def test_online_backup_includes_committed_wal_and_is_atomic(self):
        connection = self.database()
        self.assertTrue(Path(str(self.source) + "-wal").exists())
        result = sqlite_backup.create_backup(self.source, self.destination)
        connection.close()

        verified = sqlite_backup.verify_backup(result.path)
        self.assertEqual(verified, result)
        self.assertEqual(stat_mode(result.path), 0o700)
        self.assertEqual(stat_mode(result.path / sqlite_backup.DATABASE_NAME), 0o600)
        self.assertEqual(stat_mode(result.path / sqlite_backup.MANIFEST_NAME), 0o600)
        self.assertFalse(any(path.name.startswith(".partial-") for path in self.destination.iterdir()))
        with closing(sqlite3.connect(result.path / sqlite_backup.DATABASE_NAME)) as backup:
            self.assertEqual(backup.execute("SELECT count(*) FROM child").fetchone(), (1,))

    def test_manifest_has_no_source_path_or_database_content(self):
        connection = self.database()
        result = sqlite_backup.create_backup(self.source, self.destination)
        connection.close()
        raw = (result.path / sqlite_backup.MANIFEST_NAME).read_text()
        manifest = json.loads(raw)
        self.assertEqual(set(manifest), {"created_utc", "database", "format", "sha256", "size", "user_version"})
        self.assertNotIn(str(self.source), raw)
        self.assertNotIn("synthetic row", raw)

    def test_online_backup_stays_consistent_while_writer_commits(self):
        connection = self.database()
        connection.close()
        started = threading.Event()
        stop = threading.Event()
        errors: list[BaseException] = []

        def write_rows():
            try:
                with closing(sqlite3.connect(self.source, timeout=5)) as writer:
                    writer.execute("PRAGMA foreign_keys = ON")
                    identifier = 2
                    while not stop.is_set():
                        writer.execute("INSERT INTO parent VALUES (?, ?)",
                                       (identifier, "concurrent synthetic row"))
                        writer.execute("INSERT INTO child VALUES (?, ?)",
                                       (identifier, identifier))
                        writer.commit()
                        started.set()
                        identifier += 1
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=write_rows)
        worker.start()
        self.assertTrue(started.wait(timeout=2))
        time.sleep(0.01)
        try:
            result = sqlite_backup.create_backup(self.source, self.destination)
        finally:
            stop.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        sqlite_backup.verify_backup(result.path)
        with closing(sqlite3.connect(result.path / sqlite_backup.DATABASE_NAME)) as backup:
            parents = backup.execute("SELECT count(*) FROM parent").fetchone()[0]
            children = backup.execute("SELECT count(*) FROM child").fetchone()[0]
        self.assertEqual(parents, children)
        self.assertGreaterEqual(parents, 2)

    def test_foreign_key_failure_never_publishes_snapshot(self):
        connection = self.database(invalid_foreign_key=True)
        with self.assertRaisesRegex(sqlite_backup.BackupError, "FOREIGN_KEY_CHECK_FAILED"):
            sqlite_backup.create_backup(self.source, self.destination)
        connection.close()
        self.assertEqual(self.snapshots(), [])

    def test_symlink_and_hardlink_sources_are_rejected(self):
        connection = self.database()
        connection.close()
        link = self.root / "source-link.sqlite3"
        link.symlink_to(self.source)
        with self.assertRaisesRegex(sqlite_backup.BackupError, "SOURCE_NOT_REGULAR"):
            sqlite_backup.create_backup(link, self.destination)
        hardlink = self.root / "source-hardlink.sqlite3"
        os.link(self.source, hardlink)
        with self.assertRaisesRegex(sqlite_backup.BackupError, "SOURCE_HARDLINKED"):
            sqlite_backup.create_backup(self.source, self.destination)

    def test_symlink_destination_is_rejected(self):
        connection = self.database()
        connection.close()
        link = self.root / "backup-link"
        link.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaisesRegex(sqlite_backup.BackupError, "BACKUP_DIRECTORY_NOT_REGULAR"):
            sqlite_backup.create_backup(self.source, link)

    def test_concurrent_execution_is_rejected(self):
        connection = self.database()
        with sqlite_backup.backup_lock(self.destination):
            with self.assertRaisesRegex(sqlite_backup.BackupError, "BACKUP_ALREADY_RUNNING"):
                sqlite_backup.create_backup(self.source, self.destination)
        connection.close()

    def test_copy_failure_cleans_partial_without_exposing_error(self):
        connection = self.database()
        with mock.patch.object(sqlite_backup, "_copy_online",
                               side_effect=sqlite3.DatabaseError("private row sentinel")):
            errors = io.StringIO()
            with redirect_stderr(errors):
                status = sqlite_backup.main(["backup", "--source", str(self.source),
                                               "--destination", str(self.destination)])
        connection.close()
        self.assertEqual(status, 1)
        self.assertNotIn("private row sentinel", errors.getvalue())
        self.assertEqual(self.snapshots(), [])

    def test_tampered_backup_is_rejected(self):
        connection = self.database()
        result = sqlite_backup.create_backup(self.source, self.destination)
        connection.close()
        with (result.path / sqlite_backup.DATABASE_NAME).open("ab") as stream:
            stream.write(b"tampered")
        with self.assertRaisesRegex(sqlite_backup.BackupError, "BACKUP_SIZE_MISMATCH"):
            sqlite_backup.verify_backup(result.path)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
