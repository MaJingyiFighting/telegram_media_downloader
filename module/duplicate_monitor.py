"""Persistent duplicate index and directory monitor."""

import hashlib
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set

from loguru import logger


def normalize_path(file_path: str) -> str:
    """Normalize a path for stable database lookups."""
    return os.path.normcase(os.path.abspath(file_path))


def is_path_under_root(file_path: str, root_path: str) -> bool:
    """Check whether a file path is inside a monitored root."""
    try:
        return os.path.commonpath([file_path, root_path]) == root_path
    except ValueError:
        return False


@dataclass
class FileRecord:
    """Indexed file metadata."""

    path: str
    size: int
    mtime: float
    header_md5: Optional[str]
    full_md5: Optional[str]
    first_seen: float
    last_seen: float
    file_unique_id: Optional[str]


class DuplicateFileMonitor:
    """Maintain a persistent file index and remove later duplicates."""

    def __init__(self, db_path: str, header_size: int = 1024 * 1024):
        self.db_path = normalize_path(db_path)
        self.header_size = max(int(header_size), 1)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self):
        """Close the sqlite connection."""
        with self._lock:
            self._conn.close()

    def _init_schema(self):
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_index (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    header_md5 TEXT,
                    full_md5 TEXT,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    file_unique_id TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_file_index_size_header
                ON file_index(size, header_md5)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_file_index_unique_id
                ON file_index(file_unique_id)
                """
            )
            self._conn.commit()

    def _row_to_record(self, row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            path=row["path"],
            size=row["size"],
            mtime=row["mtime"],
            header_md5=row["header_md5"],
            full_md5=row["full_md5"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            file_unique_id=row["file_unique_id"],
        )

    def _get_record(self, file_path: str) -> Optional[FileRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM file_index WHERE path = ?",
                (file_path,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def _get_candidates(
        self, file_path: str, file_size: int, header_md5: str
    ) -> List[FileRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM file_index
                WHERE size = ? AND header_md5 = ? AND path != ?
                ORDER BY first_seen ASC, path ASC
                """,
                (file_size, header_md5, file_path),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def _get_all_paths(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute("SELECT path FROM file_index").fetchall()
        return [row["path"] for row in rows]

    def _upsert_record(
        self,
        file_path: str,
        file_size: int,
        mtime: float,
        header_md5: Optional[str],
        full_md5: Optional[str],
        file_unique_id: Optional[str],
        first_seen: float,
        last_seen: float,
    ):
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO file_index (
                    path,
                    size,
                    mtime,
                    header_md5,
                    full_md5,
                    first_seen,
                    last_seen,
                    file_unique_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size = excluded.size,
                    mtime = excluded.mtime,
                    header_md5 = excluded.header_md5,
                    full_md5 = excluded.full_md5,
                    first_seen = excluded.first_seen,
                    last_seen = excluded.last_seen,
                    file_unique_id = COALESCE(excluded.file_unique_id, file_index.file_unique_id)
                """,
                (
                    file_path,
                    file_size,
                    mtime,
                    header_md5,
                    full_md5,
                    first_seen,
                    last_seen,
                    file_unique_id,
                ),
            )
            self._conn.commit()

    def _update_full_md5(self, file_path: str, full_md5: str):
        with self._lock:
            self._conn.execute(
                "UPDATE file_index SET full_md5 = ? WHERE path = ?",
                (full_md5, file_path),
            )
            self._conn.commit()

    def remove_file(self, file_path: str):
        """Remove a stale file record from the database."""
        normalized_path = normalize_path(file_path)
        with self._lock:
            self._conn.execute(
                "DELETE FROM file_index WHERE path = ?",
                (normalized_path,),
            )
            self._conn.commit()

    def _compute_head_md5(self, file_path: str) -> str:
        hasher = hashlib.md5()
        with open(file_path, "rb") as file_obj:
            hasher.update(file_obj.read(self.header_size))
        return hasher.hexdigest()

    def _compute_full_md5(self, file_path: str) -> str:
        hasher = hashlib.md5()
        with open(file_path, "rb") as file_obj:
            for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _delete_duplicate_file(self, file_path: str, kept_path: str) -> bool:
        try:
            os.remove(file_path)
            logger.info(
                f"Removed later duplicate file: {file_path}, kept existing file: {kept_path}"
            )
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            logger.warning(f"Failed to remove duplicate file {file_path}: {exc}")
            return False

    def find_tracked_file_by_unique_id(
        self, file_unique_id: Optional[str]
    ) -> Optional[str]:
        """Find an existing file by Telegram file_unique_id."""
        if not file_unique_id:
            return None

        stale_paths: List[str] = []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT path FROM file_index
                WHERE file_unique_id = ?
                ORDER BY first_seen ASC, path ASC
                """,
                (file_unique_id,),
            ).fetchall()

        for row in rows:
            file_path = row["path"]
            if os.path.isfile(file_path):
                return file_path
            stale_paths.append(file_path)

        for stale_path in stale_paths:
            self.remove_file(stale_path)

        return None

    def register_file(
        self, file_path: str, file_unique_id: Optional[str] = None
    ) -> str:
        """Register a completed file and delete it if it is a later duplicate."""
        normalized_path = normalize_path(file_path)

        if not os.path.isfile(normalized_path):
            self.remove_file(normalized_path)
            return normalized_path

        try:
            current_stat = os.stat(normalized_path)
        except FileNotFoundError:
            self.remove_file(normalized_path)
            return normalized_path
        existing_record = self._get_record(normalized_path)
        if (
            existing_record
            and existing_record.size == current_stat.st_size
            and abs(existing_record.mtime - current_stat.st_mtime) < 1e-6
        ):
            if file_unique_id and existing_record.file_unique_id != file_unique_id:
                self._upsert_record(
                    normalized_path,
                    existing_record.size,
                    existing_record.mtime,
                    existing_record.header_md5,
                    existing_record.full_md5,
                    file_unique_id,
                    existing_record.first_seen,
                    time.time(),
                )
            return normalized_path

        if file_unique_id:
            tracked_path = self.find_tracked_file_by_unique_id(file_unique_id)
            if tracked_path and tracked_path != normalized_path:
                if self._delete_duplicate_file(normalized_path, tracked_path):
                    self.remove_file(normalized_path)
                    return tracked_path

        try:
            header_md5 = self._compute_head_md5(normalized_path)
        except FileNotFoundError:
            self.remove_file(normalized_path)
            return normalized_path
        candidate_records = self._get_candidates(
            normalized_path,
            current_stat.st_size,
            header_md5,
        )

        current_full_md5: Optional[str] = None
        for candidate in candidate_records:
            if not os.path.isfile(candidate.path):
                self.remove_file(candidate.path)
                continue

            if current_full_md5 is None:
                try:
                    current_full_md5 = self._compute_full_md5(normalized_path)
                except FileNotFoundError:
                    self.remove_file(normalized_path)
                    return normalized_path

            candidate_full_md5 = candidate.full_md5
            if not candidate_full_md5:
                try:
                    candidate_full_md5 = self._compute_full_md5(candidate.path)
                except FileNotFoundError:
                    self.remove_file(candidate.path)
                    continue
                self._update_full_md5(candidate.path, candidate_full_md5)

            if candidate_full_md5 == current_full_md5:
                if self._delete_duplicate_file(normalized_path, candidate.path):
                    self.remove_file(normalized_path)
                    return candidate.path

        current_time = time.time()
        first_seen = (
            existing_record.first_seen if existing_record is not None else current_time
        )
        self._upsert_record(
            normalized_path,
            current_stat.st_size,
            current_stat.st_mtime,
            header_md5,
            current_full_md5,
            file_unique_id,
            first_seen,
            current_time,
        )
        return normalized_path

    def cleanup_missing_files(
        self,
        roots: Sequence[str],
        seen_paths: Optional[Iterable[str]] = None,
    ):
        """Remove file records whose files are no longer present."""
        normalized_roots = [normalize_path(root) for root in roots]
        seen = set(seen_paths or [])
        for file_path in self._get_all_paths():
            if normalized_roots and not any(
                is_path_under_root(file_path, root_path)
                for root_path in normalized_roots
            ):
                continue
            if seen and file_path in seen:
                continue
            if not os.path.isfile(file_path):
                self.remove_file(file_path)

    def scan_paths(
        self, roots: Sequence[str], stable_seconds: float = 3.0
    ) -> List[str]:
        """Scan monitored roots for new files and later duplicates."""
        normalized_roots = [normalize_path(root) for root in roots]
        seen_paths: Set[str] = set()
        removed_paths: List[str] = []
        current_time = time.time()

        for root_path in normalized_roots:
            if not os.path.isdir(root_path):
                continue
            for dir_path, dir_names, file_names in os.walk(root_path):
                dir_names.sort()
                file_names.sort()
                for file_name in file_names:
                    file_path = normalize_path(os.path.join(dir_path, file_name))
                    if file_path == self.db_path:
                        continue
                    seen_paths.add(file_path)
                    try:
                        if (
                            stable_seconds > 0
                            and current_time - os.path.getmtime(file_path)
                            < stable_seconds
                        ):
                            continue
                        kept_path = self.register_file(file_path)
                        if kept_path != file_path and not os.path.exists(file_path):
                            removed_paths.append(file_path)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        logger.warning(f"Failed to scan file {file_path}: {exc}")

        self.cleanup_missing_files(normalized_roots, seen_paths)
        return removed_paths
