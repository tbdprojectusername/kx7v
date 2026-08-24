#!/usr/bin/env python3
"""Change-detected live UFC/DWCS prop capture from FightOdds.

This is a current-state observer, not a historical tick reconstruction. It
keeps named outcomes and the source's open/current/best/worst summary fields,
while appending a row whenever the current price or offer state changes.
Straight moneylines (A_1) and the source's live-betting category (A_2) are
excluded so they cannot enter the prematch prop namespace.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from curl_cffi import requests as creq

from poll_fightodds import (
    CycleAbort,
    EventFailed,
    EXCHANGES,
    EXCLUDED_BOOKS,
    GQL,
    HDRS,
    Pacer,
    _age_h,
    _amer,
    _fmt_amer,
    _parse_ts,
    discover,
    gql,
)
from snapshot_io import atomic_csv, atomic_json


EXCLUDED_CATEGORIES = {"A_1", "A_2"}
HEARTBEAT_H = 24.0
PROP_FIELDS = [
    "poll_time", "event_pk", "event_date", "event_name", "promotion", "fight_slug",
    "offer_id", "outcome_id", "book", "book_role", "type_id", "category",
    "subcategory", "description", "not_description", "offer_value", "type_value",
    "outcome_name", "outcome_fighter_slug", "is_not", "american", "american_open",
    "american_best", "american_worst", "source_offer_ts", "source_created_at",
    "source_change_age_h", "offer_status", "disabled", "cycle_status",
]
PROPS_Q = """
query CurrentProps($pk: Int, $off: Int) {
  event: eventByPk(pk: $pk) {
    pk
    fights(first: 100) { edges { node { slug isCancelled
      offers(first: 100, offset: $off) {
        edges { node {
          id sbId status disabled timestamp createdAt value
          offerType { offerTypeId category subCategory description value notDescription }
          sportsbook { shortName slug }
          outcomes { edges { node {
            id name isNot odds oddsOpen oddsBest oddsWorst fighter { slug }
          } } }
        } }
        pageInfo { hasNextPage }
      }
    } } pageInfo { hasNextPage } }
  }
}
"""


def parse_page(data: dict, event: dict, poll_iso: str) -> tuple[list[dict], bool, int]:
    node = (data or {}).get("event")
    if not node:
        raise EventFailed("empty event payload")
    fights = node.get("fights") or {}
    if (fights.get("pageInfo") or {}).get("hasNextPage"):
        raise EventFailed(">100 fights — refusing to truncate")
    rows: list[dict] = []
    any_more = False
    invalid = 0
    for edge in fights.get("edges") or []:
        fight = edge.get("node") or {}
        if fight.get("isCancelled"):
            continue
        offers = fight.get("offers") or {}
        any_more = any_more or bool((offers.get("pageInfo") or {}).get("hasNextPage"))
        for offer_edge in offers.get("edges") or []:
            offer = offer_edge.get("node") or {}
            offer_type = offer.get("offerType") or {}
            category = str(offer_type.get("category") or "")
            if category in EXCLUDED_CATEGORIES:
                continue
            sportsbook = offer.get("sportsbook") or {}
            book = str(sportsbook.get("shortName") or sportsbook.get("slug") or "").strip()
            if not book or book.lower() in EXCLUDED_BOOKS:
                continue
            offer_id = str(offer.get("id") or "")
            if not offer_id:
                invalid += 1
                continue
            source_ts = _parse_ts(offer.get("timestamp"))
            created_at = _parse_ts(offer.get("createdAt"))
            for outcome_edge in ((offer.get("outcomes") or {}).get("edges") or []):
                outcome = outcome_edge.get("node") or {}
                outcome_id = str(outcome.get("id") or "")
                american = _amer(outcome.get("odds"))
                if not outcome_id or american is None:
                    invalid += 1
                    continue
                def optional_price(name: str) -> str:
                    value = _amer(outcome.get(name))
                    return "" if value is None else _fmt_amer(value)
                rows.append({
                    "poll_time": poll_iso,
                    "event_pk": event["pk"],
                    "event_date": event["date"],
                    "event_name": event["name"],
                    "promotion": event["promotion"],
                    "fight_slug": fight.get("slug") or "",
                    "offer_id": offer_id,
                    "outcome_id": outcome_id,
                    "book": book,
                    "book_role": "exchange" if book.lower() in EXCHANGES else "sportsbook",
                    "type_id": offer_type.get("offerTypeId") or "",
                    "category": category,
                    "subcategory": offer_type.get("subCategory") or "",
                    "description": offer_type.get("description") or "",
                    "not_description": offer_type.get("notDescription") or "",
                    "offer_value": "" if offer.get("value") is None else str(offer.get("value")),
                    "type_value": "" if offer_type.get("value") is None else str(offer_type.get("value")),
                    "outcome_name": outcome.get("name") or "",
                    "outcome_fighter_slug": ((outcome.get("fighter") or {}).get("slug") or ""),
                    "is_not": 1 if outcome.get("isNot") else 0,
                    "american": _fmt_amer(american),
                    "american_open": optional_price("oddsOpen"),
                    "american_best": optional_price("oddsBest"),
                    "american_worst": optional_price("oddsWorst"),
                    "source_offer_ts": source_ts,
                    "source_created_at": created_at,
                    "source_change_age_h": _age_h(poll_iso, source_ts) if source_ts else "",
                    "offer_status": offer.get("status") or "",
                    "disabled": 1 if offer.get("disabled") else 0,
                    "cycle_status": "",
                })
    return rows, any_more, invalid


def poll_event(session, pacer: Pacer, event: dict, poll_iso: str) -> tuple[list[dict], int]:
    combined: dict[tuple[str, str], dict] = {}
    invalid = 0
    offset = 0
    for _ in range(30):
        data = gql(session, pacer, PROPS_Q, {"pk": event["pk"], "off": offset})
        rows, more, bad = parse_page(data, event, poll_iso)
        invalid += bad
        for row in rows:
            combined[(row["offer_id"], row["outcome_id"])] = row
        if not more:
            return list(combined.values()), invalid
        offset += 100
    raise EventFailed("prop pagination exceeded 30 pages")


def write_rows(rows: list[dict], out_dir: Path, status: str) -> tuple[Path, int]:
    now = pd.Timestamp.now(tz="UTC")
    path = out_dir / f"fightodds_props_{now:%Y-%m}.csv"
    previous: dict[tuple[str, str, str], pd.Series] = {}
    if path.exists():
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        frame = frame.sort_values("poll_time").groupby(
            ["offer_id", "outcome_id", "book"], as_index=False, dropna=False
        ).tail(1)
        previous = {
            (str(row.offer_id), str(row.outcome_id), str(row.book)): row
            for _, row in frame.iterrows()
        }
    keep = []
    compare = ("american", "offer_status", "disabled")
    for row in rows:
        key = (row["offer_id"], row["outcome_id"], row["book"])
        old = previous.get(key)
        changed = old is None or any(str(old.get(column, "")) != str(row[column]) for column in compare)
        age = "" if old is None else _age_h(row["poll_time"], str(old.get("poll_time", "")))
        if changed or age == "" or float(age) >= HEARTBEAT_H:
            row["cycle_status"] = status
            keep.append(row)
    if keep:
        out_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(keep).to_csv(path, mode="a", header=not path.exists(), index=False)
    return path, len(keep)


def write_manifest(out_dir: Path, payload: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(out_dir / "fightodds_props_cycle_latest.json", payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path("data"))
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--canary", action="store_true")
    args = parser.parse_args()
    session = creq.Session(impersonate="chrome")
    pacer = Pacer()
    poll_iso = datetime.now(timezone.utc).isoformat()
    try:
        events = discover(session, pacer, args.days)
        if args.canary:
            events = events[:1]
        rows, succeeded, failed, invalid = [], [], [], 0
        for event in events:
            try:
                event_rows, bad = poll_event(session, pacer, event, poll_iso)
                rows.extend(event_rows)
                invalid += bad
                succeeded.append(event["pk"])
                print(f"ok pk={event['pk']}: {len(event_rows)} prop outcomes, {bad} invalid")
            except EventFailed as exc:
                failed.append(event["pk"])
                print(f"FAILED pk={event['pk']}: {exc}")
    except CycleAbort as exc:
        if not args.canary:
            write_manifest(args.out_dir, {"poll_time": poll_iso, "status": "aborted", "reason": str(exc)})
        print(f"ABORT: {exc}")
        return 2
    if args.canary:
        print(f"canary: {len(rows)} rows; invalid={invalid}; nothing written")
        return 0 if succeeded else 2
    status = "complete" if not failed else "partial"
    path, written = write_rows(rows, args.out_dir, status)
    snapshot_rows = []
    for row in rows:
        item = dict(row)
        item["cycle_status"] = status
        snapshot_rows.append(item)
    snapshot = atomic_csv(
        args.out_dir / "fightodds_props_snapshot_latest.csv",
        snapshot_rows,
        PROP_FIELDS,
    )
    write_manifest(args.out_dir, {
        "contract": "FIGHTODDS-PROPS-CURRENT-SNAPSHOT-1",
        "poll_time": poll_iso,
        "status": status,
        "requested_pks": [event["pk"] for event in events],
        "succeeded_pks": succeeded,
        "failed_pks": failed,
        "observed_outcomes": len(rows),
        "invalid_outcomes": invalid,
        "rows_written": written,
        "snapshot": snapshot,
    })
    print(f"cycle {status}: {len(rows)} observed, {written} appended -> {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
