#!/usr/bin/env python3
"""Mise-o-jeu+ (Loto-Quebec, OpenBet/SG Digital) UFC moneyline poller.

The operator can bet at this book, so its quotes join the executable set.
Public, unauthenticated content-service REST API discovered 2026-08-13:

  https://content.mojp-sgdigital-jel.com/content-service/api/v1/q/
    time-band-event-list?...drilldownTagIds=93...   -> UFC event ids
    events-by-ids?eventIds=...&includeChildMarkets  -> markets/prices

Moneyline = market groupCode FIGHT_WINNER; outcomes carry prices[].decimal and
subType H/A mapping to the event's HOME/AWAY team names. Exact per-fight
startTime is included (a bonus no other captured book provides this cleanly).

Same write contract as poll_fightodds: change-detected append to
data/miseojeu_YYYY-MM.csv (first sight / price change / 24h heartbeat) plus a
cycle manifest. ~3 HTTP requests per cycle. Fail-soft: any error aborts the
cycle without publishing partial rows as fresh.
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

BASE = "https://content.mojp-sgdigital-jel.com/content-service/api/v1/q/"
HDRS = {"accept": "application/json",
        "origin": "https://miseojeuplus.espacejeux.com",
        "referer": "https://miseojeuplus.espacejeux.com/"}
UFC_TAG = "93"
BOOK = "MiseOJeu"
HEARTBEAT_H = 24.0
CHUNK = 20


def nrm(x) -> str:
    s = unicodedata.normalize("NFKD", str(x))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def d2a(d) -> str:
    return f"+{round((d - 1) * 100)}" if d >= 2 else str(round(-100 / (d - 1)))


def get(path):
    r = requests.get(BASE + path, headers=HDRS, timeout=45)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} on {path[:60]}")
    return r.json()


def discover(now):
    # parameter set mirrors the site's own call verbatim (trimming params broke
    # the response: empty bands) — only the caps are raised and the dates roll
    dates = ",".join((now + pd.Timedelta(days=d)).strftime("%Y-%m-%dT04:00:00Z")
                     for d in (1, 2, 3))
    d = get("time-band-event-list?maxMarkets=10&excludeEventsWithNoMarkets=false"
            "&allowedEventSorts=MTCH&includeChildMarkets=true&prioritisePrimaryMarkets=true"
            f"&includeCommentary=true&includeMedia=true&drilldownTagIds={UFC_TAG}"
            "&useMarketGroupCodeCombis=true&maxTotalItems=200&maxEventsPerCompetition=50"
            "&maxCompetitionsPerSportPerBand=5&maxEventsForNextToGo=5"
            f"&startTimeOffsetForNextToGo=600&dates={dates}&lang=en-CA&channel=M")
    ids = set()

    def walk(o):
        if isinstance(o, dict):
            if "startTime" in o and "id" in o and o.get("sortCode") == "MTCH":
                ids.add(str(o["id"]))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(d)
    return sorted(ids)


def parse_events(payload, poll_iso):
    rows, quar = [], 0
    for ev in ((payload.get("data") or {}).get("events")) or []:
        teams = {t.get("side"): nrm(t.get("name", "")) for t in ev.get("teams") or []}
        h, aw = teams.get("HOME"), teams.get("AWAY")
        if not h or not aw or h == aw:
            quar += 1
            continue
        mkts = [m for m in ev.get("markets") or []
                if m.get("groupCode") == "FIGHT_WINNER" and m.get("displayed")]
        if not mkts:
            continue
        by_side = {}
        for o in mkts[0].get("outcomes") or []:
            if not o.get("displayed"):
                continue
            side = {"H": "HOME", "A": "AWAY"}.get(o.get("subType"))
            pr = next((p for p in o.get("prices") or [] if p.get("decimal")), None)
            if side and pr:
                if side in by_side:
                    quar += 1
                    side = None
                if side:
                    by_side[side] = float(pr["decimal"])
        if set(by_side) != {"HOME", "AWAY"}:
            quar += 1
            continue
        d_h, d_a = by_side["HOME"], by_side["AWAY"]
        if not (1.01 <= d_h <= 35 and 1.01 <= d_a <= 35):
            quar += 1
            continue
        side1, side2 = sorted([h, aw])
        d1, d2 = (d_h, d_a) if h == side1 else (d_a, d_h)
        rows.append({
            "poll_time": poll_iso, "event_id": str(ev.get("id")),
            "pair": f"{side1}|{side2}",
            "event_date": str(ev.get("startTime", ""))[:10],
            "event_name": str(ev.get("name", "")).strip(),
            "start_time": ev.get("startTime", ""),
            "book": BOOK, "book_role": "sportsbook",
            "side1_key": side1, "side2_key": side2,
            "dec1": f"{d1:.4f}", "dec2": f"{d2:.4f}",
            "amer1": d2a(d1), "amer2": d2a(d2),
            "cycle_status": "",
        })
    return rows, quar


def write_rows(rows, out_dir, status):
    now = pd.Timestamp.now(tz="UTC")
    path = Path(out_dir) / f"miseojeu_{now:%Y-%m}.csv"
    prev = {}
    if path.exists():
        try:
            df = pd.read_csv(path, dtype=str).fillna("")
            df = df.sort_values("poll_time").groupby("pair", as_index=False).tail(1)
            prev = {r.pair: r for _, r in df.iterrows()}
        except Exception as e:
            print(f"  warning: unreadable {path.name} ({e}); full snapshot", flush=True)
    keep = []
    for r in rows:
        p = prev.get(r["pair"])
        if p is None:
            keep.append(r)
            continue
        changed = (str(p.get("dec1")) != r["dec1"]) or (str(p.get("dec2")) != r["dec2"])
        try:
            age = (pd.Timestamp(r["poll_time"]) - pd.Timestamp(str(p.get("poll_time")))
                   ).total_seconds() / 3600
        except Exception:
            age = HEARTBEAT_H
        if changed or age >= HEARTBEAT_H:
            keep.append(r)
    for r in keep:
        r["cycle_status"] = status
    if keep:
        Path(out_dir).mkdir(exist_ok=True)
        pd.DataFrame(keep).to_csv(path, mode="a", header=not path.exists(), index=False)
    return path, len(keep)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--canary", action="store_true", help="discovery only, no writes")
    a = ap.parse_args()
    now = pd.Timestamp.now(tz="UTC")
    poll_iso = datetime.now(timezone.utc).isoformat()
    try:
        ids = discover(now)
        print(f"miseojeu discovery: {len(ids)} UFC events", flush=True)
        if a.canary:
            return 0
        rows, quar = [], 0
        for i in range(0, len(ids), CHUNK):
            d = get(f"events-by-ids?eventIds={','.join(ids[i:i + CHUNK])}"
                    "&includeChildMarkets=true&includePriceHistory=false&lang=en-CA&channel=M")
            r_, q_ = parse_events(d, poll_iso)
            rows.extend(r_)
            quar += q_
    except Exception as e:
        print(f"miseojeu ABORT: {e}", flush=True)
        (Path(a.out_dir) / "miseojeu_cycle_latest.json").write_text(json.dumps(
            {"poll_time": poll_iso, "status": "aborted", "error": str(e)[:200]}, indent=1))
        return 2
    path, n = write_rows(rows, a.out_dir, "complete")
    (Path(a.out_dir) / "miseojeu_cycle_latest.json").write_text(json.dumps(
        {"poll_time": poll_iso, "status": "complete", "fights": len(rows),
         "rows_written": n, "quarantined": quar}, indent=1))
    print(f"miseojeu cycle complete: {len(rows)} fights priced, {n} rows appended "
          f"-> {path.name}; {quar} quarantined", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
