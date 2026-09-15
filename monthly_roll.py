#!/usr/bin/env python3
"""Roll a feed's monthly CSV into numbered parts before GitHub's hard limit.

GitHub refuses any blob over 100 MiB (pre-receive hook, no override short of
LFS). On 2026-09-12 `fightodds_props_2026-09.csv` crossed it mid-month and
every sync-b push was rejected for three days: the FightOdds feed froze, the
d6mw snapshot-age gate failed on every run, and the operator got the error
mail. `bfo_2026-09.csv` was days behind it on the same path.

Rule: a poller appends to the LAST part of the current month
(`<feed>_<YYYY-MM>.csv`, then `<feed>_<YYYY-MM>_p2.csv`, `_p3`, ...). When
that part is already at or past ROLL_BYTES the next append opens the next
part. Parts are plain monthly files with a suffix; consumers that select by
month keep every part of a month together and read them in part order.
"""
from __future__ import annotations

import re
from pathlib import Path

#: One cycle appends well under 1 MiB, so 90 MiB leaves an order of magnitude
#: of headroom below the 100 MiB (104,857,600-byte) hard limit.
ROLL_BYTES = 90 * 1024 * 1024

PART = re.compile(r"^(?P<feed>.+?)_(?P<ym>\d{4}-\d{2})(?:_p(?P<p>\d+))?\.csv$")


def part_number(path) -> int:
    m = PART.match(Path(path).name)
    return int(m.group("p")) if m and m.group("p") else 1


def month_parts(out_dir, feed: str, ym: str) -> list[Path]:
    """Every part of `feed`'s month `ym`, base first, then _p2, _p3, ... in
    numeric order. `fightodds` never matches `fightodds_props_...`."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return []
    pat = re.compile("^" + re.escape(f"{feed}_{ym}") + r"(?:_p\d+)?\.csv$")
    return sorted((p for p in out_dir.iterdir() if pat.match(p.name)), key=part_number)


def append_target(out_dir, feed: str, ym: str, limit: int = ROLL_BYTES) -> Path:
    """The file this cycle's rows go to: the last part of the month, or a new
    part when the last one has reached `limit` bytes."""
    parts = month_parts(out_dir, feed, ym)
    if not parts:
        return Path(out_dir) / f"{feed}_{ym}.csv"
    last = parts[-1]
    if last.stat().st_size < limit:
        return last
    return Path(out_dir) / f"{feed}_{ym}_p{part_number(last) + 1}.csv"
