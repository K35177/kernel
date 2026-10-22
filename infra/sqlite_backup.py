"""Create and verify private, consistent SQLite backup snapshots.

The helper uses SQLite's online Backup API, so committed data in WAL mode is
included without copying WAL/SHM files. A snapshot becomes visible under its
final name only after database verification, manifest creation, and fsync.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
from typing import Iterator


DATABASE_NAME = "database.sqlite3"
MANIFEST_NAME = "manifest.json"
LOCK_NAME = ".sqlite-backup.lock"
PREFIX_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
HASH_PATTERN = re.compile(r"[a-f0-9]{64}")


class BackupError(RuntimeError):
    """A deliberately non-sensitive backup failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BackupResult:
    path: Path
    sha256: str
    size: int
    user_version: int


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise BackupError(code)


def _private_directory(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise BackupError("BACKUP_DIRECTORY_UNAVAILABLE") from error
    _need(stat.S_ISDIR(info.st_mode) and not path.is_symlink(), "BACKUP_DIRECTORY_NOT_REGULAR")
    _need(info.st_uid == os.geteuid(), "BACKUP_DIRECTORY_OWNER")
    _need(stat.S_IMODE(info.st_mode) & 0o077 == 0, "BACKUP_DIRECTORY_PERMISSIONS")
    return info


def _source_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        info = path.lstat()
    except OSError as error:
        raise BackupError("SOURCE_UNAVAILABLE") from error
    _need(stat.S_ISREG(info.st_mode) and not path.is_symlink(), "SOURCE_NOT_REGULAR")
    _need(info.st_nlink == 1, "SOURCE_HARDLINKED")
    _need(info.st_uid == os.geteuid(), "SOURCE_OWNER")
    _need(stat.S_IMODE(info.st_mode) & 0o077 == 0, "SOURCE_PERMISSIONS")
    _need(os.access(path, os.R_OK), "SOURCE_NOT_READABLE")
    return info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IFMT(info.st_mode)


def _regular_private_file(path: Path, owner: int) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise BackupError("BACKUP_FILE_UNAVAILABLE") from error
    _need(stat.S_ISREG(info.st_mode) and not path.is_symlink(), "BACKUP_FILE_NOT_REGULAR")
    _need(info.st_uid == owner and info.st_nlink == 1, "BACKUP_FILE_METADATA")
    _need(stat.S_IMODE(info.st_mode) == 0o600, "BACKUP_FILE_PERMISSIONS")
    return info


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise BackupError("BACKUP_HASH_FAILED") from error
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise BackupError("BACKUP_FSYNC_FAILED") from error


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise BackupError("BACKUP_DIRECTORY_FSYNC_FAILED") from error


@contextmanager
def backup_lock(root: Path) -> Iterator[None]:
    """Hold the destination lock without following or accepting hardlinks."""

    path = root / LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise BackupError("BACKUP_LOCK_UNAVAILABLE") from error
    try:
        info = os.fstat(descriptor)
        _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
              and info.st_nlink == 1, "BACKUP_LOCK_METADATA")
        _need(stat.S_IMODE(info.st_mode) == 0o600, "BACKUP_LOCK_PERMISSIONS")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BackupError("BACKUP_ALREADY_RUNNING") from error
        yield
    finally:
        os.close(descriptor)


def _copy_online(source: Path, destination: Path) -> None:
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        source_uri = source.absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as input_database:
            input_database.execute("PRAGMA query_only = ON")
            with closing(sqlite3.connect(destination, timeout=30)) as output_database:
                input_database.backup(output_database, pages=256, sleep=0.01)
                output_database.commit()
                journal_mode = output_database.execute("PRAGMA journal_mode = DELETE").fetchone()
                _need(journal_mode == ("delete",), "BACKUP_JOURNAL_MODE")
        destination.chmod(0o600)
    except (OSError, sqlite3.Error) as error:
        raise BackupError("SQLITE_BACKUP_FAILED") from error


def _database_checks(path: Path) -> int:
    try:
        uri = path.absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as database:
            integrity = database.execute("PRAGMA integrity_check").fetchall()
            _need(integrity == [("ok",)], "INTEGRITY_CHECK_FAILED")
            _need(database.execute("PRAGMA foreign_key_check").fetchone() is None,
                  "FOREIGN_KEY_CHECK_FAILED")
            return int(database.execute("PRAGMA user_version").fetchone()[0])
    except BackupError:
        raise
    except sqlite3.Error as error:
        raise BackupError("BACKUP_VERIFICATION_FAILED") from error


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise BackupError("MANIFEST_WRITE_FAILED") from error


def _cleanup_partial(path: Path) -> None:
    """Remove only regular files created inside our private temporary directory."""

    if not path.exists() or path.is_symlink():
        return
    try:
        for child in path.iterdir():
            info = child.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                child.unlink()
            else:
                return
        path.rmdir()
    except OSError:
        return


def create_backup(source: Path, destination: Path, *, prefix: str = "snapshot") -> BackupResult:
    """Create one verified snapshot and atomically publish its directory."""

    source = Path(source)
    destination = Path(destination)
    _need(PREFIX_PATTERN.fullmatch(prefix) is not None, "BACKUP_PREFIX_INVALID")
    _private_directory(destination)
    _private_directory(source.parent)
    source_before = _source_identity(source)
    partial: Path | None = None
    previous_umask = os.umask(0o077)
    try:
        with backup_lock(destination):
            identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(6)
            partial = destination / (".partial-" + identifier)
            final = destination / (prefix + "-" + identifier)
            _need(not os.path.lexists(partial) and not os.path.lexists(final), "BACKUP_TARGET_EXISTS")
            partial.mkdir(mode=0o700)
            database_path = partial / DATABASE_NAME
            _copy_online(source, database_path)
            owner = os.geteuid()
            database_info = _regular_private_file(database_path, owner)
            _need(_source_identity(source) == source_before, "SOURCE_REPLACED_DURING_BACKUP")
            user_version = _database_checks(database_path)
            for suffix in ("-wal", "-shm", "-journal"):
                _need(not os.path.lexists(str(database_path) + suffix), "BACKUP_SIDECAR_PRESENT")
            digest = _sha256(database_path)
            manifest = {
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "database": DATABASE_NAME,
                "format": 1,
                "sha256": digest,
                "size": database_info.st_size,
                "user_version": user_version,
            }
            _write_manifest(partial / MANIFEST_NAME, manifest)
            _regular_private_file(partial / MANIFEST_NAME, owner)
            _fsync_file(database_path)
            _fsync_directory(partial)
            os.rename(partial, final)
            partial = None
            _fsync_directory(destination)
            return BackupResult(final, digest, database_info.st_size, user_version)
    finally:
        os.umask(previous_umask)
        if partial is not None:
            _cleanup_partial(partial)


def verify_backup(snapshot: Path) -> BackupResult:
    """Verify metadata, manifest, integrity, and foreign keys without row output."""

    snapshot = Path(snapshot)
    info = _private_directory(snapshot)
    try:
        names = {entry.name for entry in snapshot.iterdir()}
    except OSError as error:
        raise BackupError("BACKUP_DIRECTORY_UNAVAILABLE") from error
    _need(names == {DATABASE_NAME, MANIFEST_NAME}, "BACKUP_FILE_SET")
    database_path = snapshot / DATABASE_NAME
    manifest_path = snapshot / MANIFEST_NAME
    database_info = _regular_private_file(database_path, info.st_uid)
    manifest_info = _regular_private_file(manifest_path, info.st_uid)
    _need(manifest_info.st_size <= 16 * 1024, "MANIFEST_TOO_LARGE")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BackupError("MANIFEST_INVALID") from error
    expected_keys = {"created_utc", "database", "format", "sha256", "size", "user_version"}
    _need(isinstance(manifest, dict) and set(manifest) == expected_keys, "MANIFEST_FORMAT")
    _need(manifest["database"] == DATABASE_NAME and manifest["format"] == 1,
          "MANIFEST_FORMAT")
    _need(isinstance(manifest["sha256"], str)
          and HASH_PATTERN.fullmatch(manifest["sha256"]) is not None, "MANIFEST_HASH")
    _need(type(manifest["size"]) is int and manifest["size"] >= 0,
          "MANIFEST_SIZE")
    _need(type(manifest["user_version"]) is int and manifest["user_version"] >= 0,
          "MANIFEST_USER_VERSION")
    _need(isinstance(manifest["created_utc"], str), "MANIFEST_CREATED_AT")
    try:
        created_at = datetime.fromisoformat(manifest["created_utc"])
    except ValueError as error:
        raise BackupError("MANIFEST_CREATED_AT") from error
    _need(created_at.tzinfo is not None, "MANIFEST_CREATED_AT")
    _need(manifest["size"] == database_info.st_size, "BACKUP_SIZE_MISMATCH")
    _need(_sha256(database_path) == manifest["sha256"], "BACKUP_HASH_MISMATCH")
    user_version = _database_checks(database_path)
    _need(manifest["user_version"] == user_version, "USER_VERSION_MISMATCH")
    return BackupResult(snapshot, manifest["sha256"], database_info.st_size, user_version)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or verify a private SQLite backup")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--source", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    backup.add_argument("--prefix", default="snapshot")
    verify = commands.add_parser("verify")
    verify.add_argument("snapshot", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = (create_backup(args.source, args.destination, prefix=args.prefix)
                  if args.command == "backup" else verify_backup(args.snapshot))
        print(json.dumps({"ok": True, "snapshot": str(result.path), "sha256": result.sha256,
                          "size": result.size, "user_version": result.user_version}, sort_keys=True))
        return 0
    except BackupError as error:
        print(json.dumps({"ok": False, "error": error.code}, sort_keys=True), file=sys.stderr)
        return 1
    except Exception as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
