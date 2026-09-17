#!/usr/bin/env python3
"""An aborted cycle must never overwrite the last good cycle manifest.

The warehouse gate (d6mw current_state) refuses a feed whose manifest says
`aborted`. Transient upstream aborts (11 on 2026-09-16) each clobbered a
healthy manifest and failed one warehouse run, while the snapshot beside it
was minutes old. Aborts now go to `<feed>_cycle_last_abort.json`.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import poll_fightodds
import poll_fightodds_props


class AbortManifest(unittest.TestCase):
    def test_fightodds_abort_goes_to_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            poll_fightodds.write_manifest(td, "2026-09-17T00:00:00+00:00", [1], [1], [], 5, "complete",
                                          snapshot={"path": "fightodds_snapshot_latest.csv"})
            poll_fightodds.write_manifest(td, "2026-09-17T00:10:00+00:00", [], [], [], 0, "aborted")
            good = json.loads((Path(td) / "fightodds_cycle_latest.json").read_text())
            bad = json.loads((Path(td) / "fightodds_cycle_last_abort.json").read_text())
            self.assertEqual(good["status"], "complete")
            self.assertEqual(good["poll_time"], "2026-09-17T00:00:00+00:00")
            self.assertEqual(bad["status"], "aborted")

    def test_props_abort_goes_to_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            poll_fightodds_props.write_manifest(d, {"poll_time": "t0", "status": "complete"})
            poll_fightodds_props.write_manifest(d, {"poll_time": "t1", "status": "aborted", "reason": "x"})
            self.assertEqual(json.loads((d / "fightodds_props_cycle_latest.json").read_text())["poll_time"], "t0")
            self.assertEqual(json.loads((d / "fightodds_props_cycle_last_abort.json").read_text())["reason"], "x")

    def test_partial_and_complete_still_publish(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            poll_fightodds_props.write_manifest(d, {"poll_time": "t2", "status": "partial"})
            self.assertEqual(json.loads((d / "fightodds_props_cycle_latest.json").read_text())["status"], "partial")


if __name__ == "__main__":
    unittest.main()
