#!/usr/bin/env python3
"""A monthly file rolls to a numbered part before GitHub's 100 MiB limit."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from monthly_roll import append_target, month_parts, part_number


class MonthlyRoll(unittest.TestCase):
    def test_no_file_yet_is_the_base_name(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(append_target(td, "bfo", "2026-09").name, "bfo_2026-09.csv")

    def test_small_base_keeps_appending(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "bfo_2026-09.csv").write_text("x" * 10)
            self.assertEqual(append_target(td, "bfo", "2026-09", limit=100).name, "bfo_2026-09.csv")

    def test_full_base_rolls_to_p2_then_p3(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "bfo_2026-09.csv").write_text("x" * 100)
            self.assertEqual(append_target(td, "bfo", "2026-09", limit=100).name, "bfo_2026-09_p2.csv")
            (Path(td) / "bfo_2026-09_p2.csv").write_text("x" * 100)
            self.assertEqual(append_target(td, "bfo", "2026-09", limit=100).name, "bfo_2026-09_p3.csv")

    def test_parts_order_numerically_and_feeds_do_not_bleed(self):
        with tempfile.TemporaryDirectory() as td:
            for n in ["fightodds_2026-09.csv", "fightodds_2026-09_p10.csv", "fightodds_2026-09_p2.csv",
                      "fightodds_props_2026-09.csv", "quarantine_fightodds_2026-09.csv",
                      "fightodds_2026-08.csv"]:
                (Path(td) / n).write_text("x")
            got = [p.name for p in month_parts(td, "fightodds", "2026-09")]
            self.assertEqual(got, ["fightodds_2026-09.csv", "fightodds_2026-09_p2.csv",
                                   "fightodds_2026-09_p10.csv"])
            self.assertEqual([p.name for p in month_parts(td, "fightodds_props", "2026-09")],
                             ["fightodds_props_2026-09.csv"])

    def test_part_number(self):
        self.assertEqual(part_number("pinnacle_2026-09.csv"), 1)
        self.assertEqual(part_number("pinnacle_2026-09_p7.csv"), 7)


if __name__ == "__main__":
    unittest.main()
