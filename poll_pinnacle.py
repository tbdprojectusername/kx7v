#!/usr/bin/env python3
"""Poll Pinnacle's public guest feed for UFC and APPEND to a monthly CSV.

Secondary source: Pinnacle posts UFC late (a card can appear ~6 days out) and
caps MMA limits hard, so BFO is primary. Pinnacle is still worth capturing —
it is a single named book with a published max-stake field, which BFO does not
expose.

Self-contained by design (public repo): scraper only, no model logic.
Output: data/pinnacle_<YYYY-MM>.csv

The guest feed is the same one pinnacle.com's own front end calls; the API key
below is the public client key embedded in that front end, not a secret.
Override with PINNACLE_API_KEY if it rotates.
UFC = sportId 22 ("Mixed Martial Arts") / leagueId 1624. Only moneyline
(period 0) is exposed for UFC — no method/round props.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import time

import requests

BASE_URL = "https://guest.api.arcadia.pinnacle.com/0.1"
GUEST_API_KEY = os.environ.get("PINNACLE_API_KEY",
                               "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R")
LEAGUE_UFC = 1624
FIELDS = ["poll_time", "league_id", "matchup_id", "home", "away", "start_time",
          "period", "bet_type", "side", "line", "american", "is_alt",
          "max_risk", "currency_hint", "cutoff_at", "version"]
_BET_TYPE = {"moneyline": "ml", "spread": "sp", "total": "ou", "team_total": "tt"}


def log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except Exception:
        pass


class Client:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({
            "x-api-key": GUEST_API_KEY,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) odds-capture/1.0",
            "Referer": "https://www.pinnacle.com/",
            "Origin": "https://www.pinnacle.com",
            "Accept": "application/json",
        })

    def get(self, path: str, tries: int = 4):
        last = None
        for i in range(tries):
            try:
                r = self.s.get(f"{BASE_URL}{path}", timeout=25)
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}"
            except Exception as e:
                last = repr(e)
            time.sleep(2 * (i + 1))
        raise RuntimeError(f"failed {path}: {last}")


def games_index(matchups):
    """matchupId -> {home, away, start_time} for real bouts only.

    units == 'Regular' drops aggregate/special matchups whose participants are
    neutral, so their participantId-keyed prices never join.
    """
    idx = {}
    for m in matchups:
        if m.get("units") != "Regular":
            continue
        home = away = None
        for p in m.get("participants", []):
            if p.get("alignment") == "home":
                home = p.get("name")
            elif p.get("alignment") == "away":
                away = p.get("name")
        if home and away:
            idx[m["id"]] = {"home": home, "away": away,
                            "start_time": m.get("startTime")}
    return idx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--league", type=int, default=LEAGUE_UFC)
    a = ap.parse_args()

    poll = dt.datetime.now(dt.timezone.utc).isoformat()
    c = Client()
    matchups = c.get(f"/leagues/{a.league}/matchups")
    markets = c.get(f"/leagues/{a.league}/markets/straight")
    games = games_index(matchups)

    rows = []
    for mkt in markets:
        bt = _BET_TYPE.get(mkt.get("type", ""))
        g = games.get(mkt.get("matchupId"))
        if bt is None or g is None:
            continue
        prices = mkt.get("prices", [])
        if not all("designation" in p for p in prices):
            continue                      # participantId-keyed special market
        max_risk = None
        for lim in mkt.get("limits", []):
            if lim.get("type") == "maxRiskStake":
                max_risk = lim.get("amount")
        for p in prices:
            pts = p.get("points")
            rows.append({
                "poll_time": poll, "league_id": a.league,
                "matchup_id": mkt["matchupId"], "home": g["home"],
                "away": g["away"], "start_time": g["start_time"],
                "period": mkt.get("period", 0), "bet_type": bt,
                "side": p["designation"],
                "line": float(pts) if pts is not None else None,
                "american": int(p["price"]),
                "is_alt": int(bool(mkt.get("isAlternate", False))),
                # NOTE: maxRisk is denominated in the ACCOUNT's currency (observed
                # CAD for this user) — normalize before comparing across books.
                "max_risk": max_risk, "currency_hint": "account_ccy",
                "cutoff_at": mkt.get("cutoffAt"), "version": mkt.get("version"),
            })

    if not rows:
        log(f"poll {poll}: no UFC game lines on the board (no open cards)")
        return 0

    os.makedirs(a.out_dir, exist_ok=True)
    path = os.path.join(a.out_dir, f"pinnacle_{poll[:7]}.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
    log(f"poll {poll}: {len(rows)} quotes across "
        f"{len({r['matchup_id'] for r in rows})} bouts -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
