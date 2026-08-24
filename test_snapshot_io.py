import json
import tempfile
import unittest
from pathlib import Path

from snapshot_io import atomic_csv, atomic_json, sha256_file


class SnapshotIOTests(unittest.TestCase):
    def test_atomic_snapshot_replaces_complete_file_and_hashes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.csv"
            first = atomic_csv(path, [{"id": 1, "price": -120}], ["id", "price"])
            self.assertEqual(first["rows"], 1)
            self.assertEqual(first["sha256"], sha256_file(path))
            atomic_csv(path, [], ["id", "price"])
            self.assertEqual(path.read_text(encoding="utf-8"), "id,price\n")
            self.assertFalse(path.with_suffix(".csv.tmp").exists())

    def test_atomic_manifest_replaces_complete_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cycle.json"
            atomic_json(path, {"status": "complete", "rows": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["rows"], 2)
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
