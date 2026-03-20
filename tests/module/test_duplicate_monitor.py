"""Tests for the persistent duplicate file monitor."""

import os
import sqlite3
import tempfile
import unittest

from module.duplicate_monitor import DuplicateFileMonitor, normalize_path


class DuplicateMonitorTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root_dir = os.path.join(self.tempdir.name, "downloads")
        os.makedirs(self.root_dir, exist_ok=True)
        self.db_path = os.path.join(self.tempdir.name, "duplicate_index.sqlite3")
        self.monitor = DuplicateFileMonitor(self.db_path, header_size=4)

    def tearDown(self):
        self.monitor.close()
        self.tempdir.cleanup()

    def _write_file(self, relative_path: str, content: bytes) -> str:
        file_path = os.path.join(self.root_dir, relative_path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as file_obj:
            file_obj.write(content)
        return normalize_path(file_path)

    def _db_paths(self):
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT path FROM file_index ORDER BY path ASC"
            ).fetchall()
        return [row[0] for row in rows]

    def test_register_file_removes_later_duplicate(self):
        first_file = self._write_file("a.txt", b"duplicate-content")
        second_file = self._write_file("b.txt", b"duplicate-content")

        self.assertEqual(self.monitor.register_file(first_file), first_file)
        self.assertEqual(self.monitor.register_file(second_file), first_file)

        self.assertTrue(os.path.exists(first_file))
        self.assertFalse(os.path.exists(second_file))
        self.assertEqual(self._db_paths(), [first_file])

    def test_same_head_but_different_full_hash_are_kept(self):
        first_file = self._write_file("a.bin", b"HEAD-1111")
        second_file = self._write_file("b.bin", b"HEAD-2222")

        self.assertEqual(self.monitor.register_file(first_file), first_file)
        self.assertEqual(self.monitor.register_file(second_file), second_file)

        self.assertTrue(os.path.exists(first_file))
        self.assertTrue(os.path.exists(second_file))
        self.assertEqual(self._db_paths(), [first_file, second_file])

    def test_scan_paths_cleans_stale_records_and_external_duplicates(self):
        tracked_file = self._write_file("tracked.txt", b"tracked")
        self.monitor.register_file(tracked_file, file_unique_id="tg-1")
        self.assertEqual(
            self.monitor.find_tracked_file_by_unique_id("tg-1"),
            tracked_file,
        )

        os.remove(tracked_file)
        self.monitor.scan_paths([self.root_dir], stable_seconds=0)
        self.assertIsNone(self.monitor.find_tracked_file_by_unique_id("tg-1"))

        external_first = self._write_file("c.txt", b"same-file")
        external_second = self._write_file("d.txt", b"same-file")
        removed_paths = self.monitor.scan_paths([self.root_dir], stable_seconds=0)

        self.assertTrue(os.path.exists(external_first))
        self.assertFalse(os.path.exists(external_second))
        self.assertIn(external_second, removed_paths)
