from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mover_core import MoveOptions, SafeFileMover


class SafeFileMoverTests(unittest.TestCase):
    def test_merge_randomize_verify_and_preserve_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            dir1 = base / "Dir1"
            dir2 = base / "Dir2"
            source = dir1 / "123456"

            (source / "123456_2D_DWG").mkdir(parents=True)
            (source / "123456_DOC" / "OTHER" / "nested").mkdir(parents=True)
            (source / "123456_TRASH").mkdir(parents=True)
            (source / "123456_2D_DWG" / "drawing.dwg").write_bytes(b"new drawing")
            (source / "123456_DOC" / "OTHER" / "secret.txt").write_text("secret")
            (source / "123456_DOC" / "OTHER" / "nested" / "data.bin").write_bytes(b"abc")
            (source / "123456_TRASH" / "trash.tmp").write_bytes(b"trash")
            (source / "active.lock").write_text("do not copy or delete")

            destination = dir2 / "123456"
            (destination / "123456_2D_DWG").mkdir(parents=True)
            (destination / "123456_2D_DWG" / "drawing.dwg").write_bytes(b"old drawing")
            (destination / "destination_only.txt").write_text("keep me")

            result = SafeFileMover(
                MoveOptions(
                    source_project=source,
                    destination_root=dir2,
                    postgres_dsn=None,
                    keep_destination_backup=True,
                )
            ).move()

            self.assertEqual(result.status, "completed_with_leftovers")
            self.assertTrue(destination.exists())
            self.assertEqual(
                (destination / "123456_2D_DWG" / "drawing.dwg").read_bytes(),
                b"new drawing",
            )
            self.assertEqual(
                (destination / "destination_only.txt").read_text(), "keep me"
            )
            self.assertTrue((source / "active.lock").exists())
            self.assertFalse((destination / "active.lock").exists())
            self.assertTrue(result.backup_path and Path(result.backup_path).exists())
            self.assertTrue(result.fallback_incident_log)
            self.assertTrue(Path(result.fallback_incident_log).exists())

            other = destination / "123456_DOC" / "OTHER"
            trash = destination / "123456_TRASH"
            self.assertFalse((other / "secret.txt").exists())
            self.assertFalse((trash / "trash.tmp").exists())
            other_logs = list(other.glob("_rename_log_*.jsonl"))
            trash_logs = list(trash.glob("_rename_log_*.jsonl"))
            self.assertEqual(len(other_logs), 1)
            self.assertEqual(len(trash_logs), 1)
            rows = [json.loads(line) for line in other_logs[0].read_text().splitlines()]
            self.assertTrue(any(row["original_relative_path"].endswith("secret.txt") for row in rows))

    def test_source_is_removed_when_no_lock_files_remain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "Dir1" / "654321"
            destination_root = base / "Dir2"
            source.mkdir(parents=True)
            (source / "file.txt").write_text("hello")

            result = SafeFileMover(
                MoveOptions(
                    source_project=source,
                    destination_root=destination_root,
                    postgres_dsn=None,
                )
            ).move()

            self.assertEqual(result.status, "completed")
            self.assertFalse(source.exists())
            self.assertEqual(
                (destination_root / "654321" / "file.txt").read_text(), "hello"
            )
            self.assertEqual(result.leftover_count, 0)


if __name__ == "__main__":
    unittest.main()
