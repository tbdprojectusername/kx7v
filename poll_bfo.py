#!/usr/bin/env python3
"""Poll source A' live board and APPEND per-book quotes to a monthly CSV.

Self-contained by design: this repo is public, so it holds ONLY the scraper —
no model, no strategy, no predictions.

Output: data/bfo_<YYYY-MM>.csv, one row per (poll, matchup, side, book).
CSV rather than SQLite because git diffs text well and binary DB files bloat
history and conflict on concurrent writes.

Page structure (decoded 2026-07-27 — brittle; if quote counts drop to 0 the
layout changed and the selectors below need revisiting):
  Each event page has two parallel <table>s with identical row counts.
    table[0] = sticky label column: fighter name or prop name per row
    table[1] = odds grid; its header row names the books in column order
  Each odds cell:
    <td data-li="[bookId, side, matchupId]"><span>+335</span>
        <span class="aru">▲</span></td>
  side 1/2 = the matchup's two selections. A trailing arrow span is SA's
  recent-move indicator. Props live behind a per-matchup link, NOT on this page.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time

from bs4 import BeautifulSoup
from curl_cffi import requests

import base64 as _b64
BASE = _b64.b64decode("aHR0cHM6Ly93d3cuYmVzdGZpZ2h0b2Rkcy5jb20=").decode()
FIELDS = ["poll_time", "event_slug", "event_name", "matchup_id", "side",
          "selection", "row_kind", "book_id", "book", "american", "move_arrow"]
PROP_HINT = re.compile(
    r"(round|decision|draw|by (tko|ko|submission)|goes the distance|"
    r"wins by|point|over|under)", re.I)


def log(msg: str) -> None:
    """stdout may be absent in some runners; never let logging kill a poll."""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def get(url: str, tries: int = 4) -> str:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, impersonate="chrome", timeout=40)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:                       # transient network/TLS
            last = repr(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"failed to fetch {url}: {last}")


def event_links(html: str) -> dict[str, str]:
    s = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    for a in s.select('a[href^="/events/"]'):
        out.setdefault(a["href"], a.get_text(" ", strip=True))
    return out


MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
DATE_RX = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2})(?:st|nd|rd|th)?")


def event_dates(html: str) -> dict[str, str]:
    """Per-event ISO date from the homepage headers ('UFC Shanghai Odds August
    29th'). Fail-safe: any parse problem just omits that event."""
    out: dict[str, str] = {}
    try:
        s = BeautifulSoup(html, "lxml")
        today = dt.date.today()
        for a in s.select('a[href^="/events/"]'):
            try:
                slug = a["href"].rstrip("/").split("/")[-1]
                if slug in out:
                    continue
                parent = a.find_parent(["div", "td", "th", "tr", "h2", "h3"])
                ctx = parent.get_text(" ", strip=True) if parent else ""
                m = DATE_RX.search(ctx)
                if not m:
                    continue
                mo, day = MONTHS[m.group(1)], int(m.group(2))
                year = today.year
                cand = dt.date(year, mo, day)
                if (today - cand).days > 90:      # wrapped into next year
                    cand = dt.date(year + 1, mo, day)
                out[slug] = cand.isoformat()
            except Exception:
                continue
    except Exception:
        pass
    return out


def parse_event(html: str, slug: str) -> list[dict]:
    s = BeautifulSoup(html, "lxml")
    tabs = s.select("table")
    if len(tabs) < 2:
        return []
    t_lab, t_odds = tabs[0], tabs[1]

    # Book names in column order. Use the FIRST anchor per header cell: a second
    # anchor is a promo blurb ("Polymarket" + "$20 Bonus"). Do NOT regex-split
    # the name — that truncates BetRivers/BetWay/BetMGM to a useless "Bet".
    book_names = []
    for cell in t_odds.select("tr")[0].select("th,td")[1:]:
        a = cell.select_one("a")
        if a is not None:
            name = (a.get("title") or a.get_text(strip=True)).strip()
        else:
            name = re.split(r"\$|Up to", cell.get_text(strip=True))[0].strip()
        book_names.append(name or "unknown")

    ev_name = None
    h = s.select_one("h1")
    if h is not None:
        ev_name = h.get_text(strip=True)

    rows_lab, rows_odd = t_lab.select("tr"), t_odds.select("tr")
    out = []
    for i in range(min(len(rows_lab), len(rows_odd))):
        label = re.sub(r"^\d{4,6}\s*", "",
                       rows_lab[i].get_text(" ", strip=True)).strip()
        if not label:
            continue
        for col, td in enumerate(rows_odd[i].select("td")):
            dli = td.get("data-li")
            if not dli:
                continue
            try:
                book_id, side, mu = json.loads(dli)
            except Exception:
                continue
            sp = td.select_one("span")
            if sp is None:
                continue
            val = sp.get_text(strip=True).replace("−", "-")
            if not re.fullmatch(r"[+-]?\d{2,5}", val):
                continue
            arr = td.select_one("span.aru, span.ard")
            arrow = None
            if arr is not None:
                arrow = arr.get_text(strip=True) or " ".join(arr.get("class", []))
            out.append({
                "event_slug": slug, "event_name": ev_name,
                "matchup_id": int(mu), "side": int(side), "selection": label,
                "row_kind": "prop" if PROP_HINT.search(label) else "moneyline",
                "book_id": int(book_id),
                "book": book_names[col] if col < len(book_names) else f"book{book_id}",
                "american": int(val), "move_arrow": arrow,
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--only", default="ufc",
                    help="substring filter on event slug ('' = every event)")
    a = ap.parse_args()

    poll = dt.datetime.now(dt.timezone.utc).isoformat()
    home_html = get(BASE + "/")
    links = event_links(home_html)

    # Event dates ride in a separate tiny CSV; failures here must never block
    # the quote capture (movement scorer uses this as its date fallback).
    try:
        dates = event_dates(home_html)
        if dates:
            os.makedirs(a.out_dir, exist_ok=True)
            epath = os.path.join(a.out_dir, f"bfo_events_{poll[:7]}.csv")
            seen = set()
            if os.path.exists(epath):
                with open(epath, newline="", encoding="utf-8") as fh:
                    seen = {(r["event_slug"], r["event_date"])
                            for r in csv.DictReader(fh)}
            fresh = [(s_, d_) for s_, d_ in sorted(dates.items())
                     if (s_, d_) not in seen]
            if fresh:
                new_e = not os.path.exists(epath)
                with open(epath, "a", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=["poll_time", "event_slug",
                                                       "event_date"])
                    if new_e:
                        w.writeheader()
                    for s_, d_ in fresh:
                        w.writerow({"poll_time": poll, "event_slug": s_,
                                    "event_date": d_})
                log(f"  event dates: {len(fresh)} new -> {epath}")
    except Exception as e:
        log(f"  event-date capture skipped: {e!r}")

    targets = [h for h in links if a.only.lower() in h.lower()] if a.only else list(links)
    if not targets:
        log(f"poll {poll}: no events matched --only={a.only!r}; saw {sorted(links)}")
        return 0                      # not an error: there may be no cards up

    rows: list[dict] = []
    for href in sorted(targets):
        slug = href.rstrip("/").split("/")[-1]
        got = parse_event(get(BASE + href), slug)
        log(f"  {slug}: {len(got)} quotes")
        rows.extend(got)
        time.sleep(1.0)               # politeness between event pages

    if not rows:
        log(f"poll {poll}: parsed 0 quotes — SA layout probably changed")
        return 1                      # fail loudly so the workflow surfaces it

    os.makedirs(a.out_dir, exist_ok=True)
    path = os.path.join(a.out_dir, f"bfo_{poll[:7]}.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({"poll_time": poll, **r})

    ml = sum(1 for r in rows if r["row_kind"] == "moneyline")
    log(f"poll {poll}: {len(rows)} quotes ({ml} moneyline), "
        f"{len({r['matchup_id'] for r in rows})} matchups, "
        f"{len({r['book'] for r in rows})} books -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
