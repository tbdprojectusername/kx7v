#!/usr/bin/env python3
"""Stateless source B live poller — current UFC/DWCS moneylines only.

Implements the capture handoff (private repo, CLAUDE_HANDOFF_FIGHTODDS_CAPTURE.md,
2026-08-11): a small, polite, once-per-cycle observer of CURRENT prices for
upcoming events. Deliberately NOT the historical backfill — no cursor history,
no resumable state, no SQLite, no imports from that pipeline.

Guarantees:
  * one worker; >=1s + jitter between request STARTS (local calibration
    2026-08-10: clean at 1.5 req/s, HTTP 429 at 2.0 — this runs ~10x under)
  * discovery via allEvents (date-bounded, paginated), promotion filtered
    client-side to ufc/dwcs
  * one eventByPk call per upcoming event: roster + current Straight offers
    (offerType_Category "1"); the oddsOutcome history nest is never requested
  * fail-closed pagination: >100 fights or >100 offers on a fight fails that
    EVENT — never silent truncation
  * side identity from fighter SLUGS only; a book/fight quote is quarantined
    unless exactly one outcome maps to each fighter slug (no response-order
    fallback — the legacy _orient_outcomes() and export_exec_odds.py
    outcome_no shortcuts are both banned here)
  * HTTP 429 -> honor Retry-After (>=60s), one retry, then abort the cycle;
    403 or an HTML challenge -> abort immediately; 5xx/timeout -> two bounded
    retries with jitter; GraphQL `errors` -> that event fails, cycle partial
  * an aborted cycle publishes NOTHING; a partial cycle publishes only rows
    from succeeded events, stamped cycle_status=partial
  * change-detected append: a (fight_slug, book) row is written on first
    sight, on any price change, or when its last written row is >24h old
    (heartbeat). Cycle liveness lives in data/fightodds_cycle_latest.json,
    NOT in per-row poll_time.

SHADOW-ONLY: nothing in the private scorer consumes this feed yet.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from curl_cffi import requests as creq

import base64 as _b64
GQL = _b64.b64decode("aHR0cHM6Ly9hcGkuZmlnaHRvZGRzLmlvL2dxbA==").decode()
HDRS = {"content-type": "application/json", "origin": _b64.b64decode("aHR0cHM6Ly9maWdodG9kZHMuaW8=").decode(),
        "referer": _b64.b64decode("aHR0cHM6Ly9maWdodG9kZHMuaW8v").decode()}
PROMOTIONS = {"ufc", "dwcs"}
# book policy (handoff §5): clones/junk dropped at capture; exchanges kept but labeled
EXCLUDED_BOOKS = {"ohmbet", "sportsbet", "sportbet", "sportsbetting", "betdsi", "betcris"}
EXCHANGES = {"novig", "prophetx", "prophet x", "prophet exchange", "polymarket",
             "polymarket(us)", "sporttrade", "betopenly", "kalshi", "smarkets",
             "betfair", "sxbet", "sx bet", "4casters", "4cx"}
OVERROUND_LO, OVERROUND_HI = 0.90, 1.35
FLIP_MARGIN = 0.02   # mirror test must beat the direct fit by this much to call a flip
FLIP_MIN_BOOKS = 3
# A transpose is only identifiable when the field median and its mirror are far
# enough apart to tell apart, and a real transpose lands ~exactly on the mirror.
# Measured 2026-08-16 on 9,389 captured quotes: honest cross-book disagreement is
# 0.4pp median / 4.4pp p99 / 7.5pp p99.9, so on a near-even fight ordinary
# disagreement crosses the midpoint and reads as "transposed". Four books at once
# were being dropped from a 51/49 fight (padilla|haqparast, 2026-08-16).
FLIP_MIN_SEPARATION = 0.10   # |2*median - 1|
FLIP_MIRROR_TOL = 0.05       # ||q - median| - separation|
HEARTBEAT_H = 24.0
MIN_INTERVAL = 1.0  # seconds between request starts, global (single worker)

EVENTS_Q = """
query EventsQuery($first: Int, $after: String, $dateGte: Date, $dateLt: Date, $orderBy: String) {
  allEvents(first: $first, after: $after, date_Gte: $dateGte, date_Lt: $dateLt, orderBy: $orderBy) {
    edges { node { pk name slug date promotion { slug } } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

EVENT_Q = """
query CurrentMoneylines($pk: Int) {
  event: eventByPk(pk: $pk) {
    pk
    fights(first: 100) {
      edges { node {
        slug
        isCancelled
        fighter1 { firstName lastName slug }
        fighter2 { firstName lastName slug }
        offers(first: 100, offerType_Category: "1") {
          edges { node {
            id
            timestamp
            sportsbook { shortName slug }
            outcomes { edges { node {
              id name fighter { slug } odds oddsOpen
            } } }
          } }
          pageInfo { hasNextPage endCursor }
        }
      } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


class CycleAbort(RuntimeError):
    """Stop the whole cycle and publish nothing (rate-limited / blocked)."""


class EventFailed(RuntimeError):
    """This event's poll failed; the cycle continues and is marked partial."""


class Pacer:
    def __init__(self, min_interval: float = MIN_INTERVAL):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self):
        d = self._last + self.min_interval + random.uniform(0.05, 0.30) - time.monotonic()
        if d > 0:
            time.sleep(d)
        self._last = time.monotonic()


def nrm(x) -> str:
    """Must match the private scorer's nrm() exactly, or joins silently fail."""
    s = unicodedata.normalize("NFKD", str(x))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def _amer(v):
    """Source American price as int, or None if missing/malformed."""
    try:
        a = int(v)
    except (TypeError, ValueError):
        return None
    return a if abs(a) >= 100 else None


def a2d(a: int) -> float:
    return 1 + a / 100 if a > 0 else 1 + 100 / (-a)


def _fmt_amer(a: int) -> str:
    return f"+{a}" if a > 0 else str(a)


def _parse_ts(v) -> str:
    """source B offer timestamp -> ISO string, '' when unparseable."""
    if v in (None, ""):
        return ""
    try:
        f = float(v)
        unit = "ms" if f > 1e12 else "s"
        return pd.Timestamp(f, unit=unit, tz="UTC").isoformat()
    except (TypeError, ValueError):
        pass
    try:
        t = pd.Timestamp(str(v))
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return t.isoformat()
    except Exception:
        return ""


def _age_h(later_iso: str, earlier_iso: str):
    try:
        d = (pd.Timestamp(later_iso) - pd.Timestamp(earlier_iso)).total_seconds() / 3600
        return round(d, 2)
    except Exception:
        return ""


def gql(sess, pacer: Pacer, query: str, variables: dict) -> dict:
    """One GraphQL call under the handoff's failure policy."""
    retried_429 = False
    soft_fails = 0
    while True:
        pacer.wait()
        try:
            r = sess.post(GQL, headers=HDRS, timeout=90,
                          json={"query": query, "variables": variables})
        except Exception as e:  # transport error / timeout
            soft_fails += 1
            if soft_fails > 2:
                raise EventFailed(f"transport failed after retries: {e!r}")
            time.sleep(2 * soft_fails + random.uniform(0, 1))
            continue
        if r.status_code == 429:
            if retried_429:
                raise CycleAbort("second HTTP 429 — backing off this cycle")
            retried_429 = True
            ra = str(r.headers.get("retry-after", "")).strip()
            pause = max(60, int(ra)) if ra.isdigit() else 60
            print(f"  429 — honoring Retry-After, sleeping {pause}s", flush=True)
            time.sleep(pause)
            continue
        if r.status_code == 403:
            raise CycleAbort("HTTP 403 — blocked/challenged; not retrying")
        if "text/html" in str(r.headers.get("content-type", "")).lower():
            raise CycleAbort(f"HTML challenge page (HTTP {r.status_code})")
        if r.status_code >= 500:
            soft_fails += 1
            if soft_fails > 2:
                raise EventFailed(f"HTTP {r.status_code} after retries")
            time.sleep(2 * soft_fails + random.uniform(0, 1))
            continue
        if r.status_code != 200:
            raise EventFailed(f"HTTP {r.status_code}")
        try:
            d = r.json()
        except Exception:
            raise EventFailed("non-JSON 200 response")
        if d.get("errors"):
            # GraphQL errors arrive with HTTP 200 and must still fail the poll
            raise EventFailed(f"GraphQL errors: {json.dumps(d['errors'])[:200]}")
        return d.get("data") or {}


def discover(sess, pacer: Pacer, days: int) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    v = {"first": 100, "after": None, "dateGte": str(today),
         "dateLt": str(today + timedelta(days=days)), "orderBy": "date"}
    out, pages = [], 0
    while True:
        d = gql(sess, pacer, EVENTS_Q, v)
        conn = (d or {}).get("allEvents") or {}
        for e in conn.get("edges") or []:
            n = e.get("node") or {}
            promo = ((n.get("promotion") or {}).get("slug") or "").lower()
            if promo in PROMOTIONS and n.get("pk") is not None:
                out.append({"pk": int(n["pk"]), "name": n.get("name") or "",
                            "slug": n.get("slug") or "", "date": n.get("date") or "",
                            "promotion": promo})
        pages += 1
        pi = conn.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        if pages >= 10:
            raise CycleAbort("discovery pagination runaway (>10 pages)")
        v["after"] = pi.get("endCursor")
    out.sort(key=lambda e: e["date"])
    return out


def parse_event(data: dict, ev: dict, poll_iso: str):
    """Rows for one event payload. Raises EventFailed on fail-closed conditions.

    Returns (rows, quarantined_count). Quarantine = a specific book/fight quote
    (or fight) that cannot be identified safely; it is counted, never guessed.
    """
    node = (data or {}).get("event")
    if not node:
        raise EventFailed("empty event payload")
    fconn = node.get("fights") or {}
    if (fconn.get("pageInfo") or {}).get("hasNextPage"):
        raise EventFailed(">100 fights — refusing to truncate")
    rows, quar = [], 0
    for fe in fconn.get("edges") or []:
        f = fe.get("node") or {}
        if f.get("isCancelled"):
            continue
        f1, f2 = f.get("fighter1") or {}, f.get("fighter2") or {}
        s1, s2 = f1.get("slug"), f2.get("slug")
        if not s1 or not s2 or s1 == s2:
            quar += 1
            continue
        k1 = nrm(f"{f1.get('firstName', '')} {f1.get('lastName', '')}")
        k2 = nrm(f"{f2.get('firstName', '')} {f2.get('lastName', '')}")
        if not k1 or not k2 or k1 == k2:
            # identical normalized names inside one fight: pair key cannot orient
            quar += 1
            continue
        key_by_slug = {s1: k1, s2: k2}
        side1, side2 = sorted([k1, k2])
        pair = f"{side1}|{side2}"
        oconn = f.get("offers") or {}
        if (oconn.get("pageInfo") or {}).get("hasNextPage"):
            raise EventFailed(f">100 offers on {f.get('slug')} — refusing to truncate")
        fight_rows = []
        for oe in oconn.get("edges") or []:
            o = oe.get("node") or {}
            sb = o.get("sportsbook") or {}
            book = str(sb.get("shortName") or sb.get("slug") or "").strip()
            if not book or book.lower() in EXCLUDED_BOOKS:
                continue
            by_slug, dup = {}, False
            for oc in ((o.get("outcomes") or {}).get("edges") or []):
                n_ = oc.get("node") or {}
                fs = (n_.get("fighter") or {}).get("slug")
                if fs in key_by_slug:
                    if fs in by_slug:
                        dup = True
                    by_slug[fs] = n_
            if dup or set(by_slug) != {s1, s2}:
                quar += 1  # missing/duplicate/unmatched fighter slug — never order-based
                continue
            a1, a2 = _amer(by_slug[s1].get("odds")), _amer(by_slug[s2].get("odds"))
            if a1 is None or a2 is None:
                quar += 1
                continue
            d1, d2 = a2d(a1), a2d(a2)
            if key_by_slug[s1] != side1:  # orient prices to the alphabetical side keys
                a1, a2, d1, d2 = a2, a1, d2, d1
            ov = 1 / d1 + 1 / d2
            if not (OVERROUND_LO <= ov <= OVERROUND_HI):
                quar += 1
                continue
            src_ts = _parse_ts(o.get("timestamp"))
            fight_rows.append({
                "poll_time": poll_iso, "event_pk": ev["pk"], "pair": pair,
                "fight_slug": f.get("slug") or "", "event_date": ev["date"],
                "event_name": ev["name"], "promotion": ev["promotion"],
                "side1_key": side1, "side2_key": side2, "book": book,
                "book_role": "exchange" if book.lower() in EXCHANGES else "sportsbook",
                "dec1": f"{d1:.4f}", "dec2": f"{d2:.4f}",
                "amer1": _fmt_amer(a1), "amer2": _fmt_amer(a2),
                "source_offer_ts": src_ts,
                "source_change_age_h": _age_h(poll_iso, src_ts) if src_ts else "",
                "cycle_status": "",  # stamped at write time
            })
        # Cross-book flip guard. A book whose implied side1 probability sits on the
        # opposite side of the field has its two outcomes transposed AT THE SOURCE
        # (observed 2026-08-13: one book quoting a -450 favourite at +350 while
        # sixteen others agreed). Slug orientation cannot catch it — the source's
        # own labels are wrong — so the field is the only available anchor.
        # Needs >=3 books to have a field at all; below that we cannot check and
        # the row is kept (its price still faces the executable floor downstream).
        if len(fight_rows) >= FLIP_MIN_BOOKS:
            qs = []
            for r_ in fight_rows:
                i1, i2 = 1 / float(r_["dec1"]), 1 / float(r_["dec2"])
                qs.append(i1 / (i1 + i2))
            srt = sorted(qs)
            med = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2
            kept = []
            sep = abs(2 * med - 1)
            for r_, q_ in zip(fight_rows, qs):
                # Three conditions, all required. (1) mirror test: is this row
                # closer to the field's TRANSPOSE than to the field? A fixed gap
                # misses near-pick'ems (flipping a -140 favourite moves the
                # probability only ~16pp) yet those rows still fabricate arbs.
                # (2) identifiable: the median and its mirror are `sep` apart, and
                # that collapses to zero on a coinflip, where honest disagreement
                # crosses the midpoint constantly. (3) signature: a real transpose
                # is an exact swap, so its distance from the field equals `sep`.
                d_ = abs(q_ - med)
                if (abs(q_ - (1 - med)) + FLIP_MARGIN < d_
                        and sep >= FLIP_MIN_SEPARATION
                        and abs(d_ - sep) <= FLIP_MIRROR_TOL):
                    print(f"  FLIP quarantine: {r_['book']} on {r_['fight_slug']} "
                          f"(q={q_:.2f} vs field {med:.2f}, sep={sep:.2f})", flush=True)
                    r_["quarantine_reason"] = (f"transposed: q={q_:.4f} mirrors field "
                                               f"median {med:.4f} (sep {sep:.4f}, "
                                               f"{len(qs)} books)")
                    quar += 1
                kept.append(r_)   # marked, never dropped — write_rows routes it
            fight_rows = kept
        rows.extend(fight_rows)
    return rows, quar


def write_rows(rows: list[dict], out_dir, status: str):
    """Change-detected append to the monthly CSV; returns (path, rows_written).

    Rows the flip guard marked are routed to a QUARANTINE SIDECAR rather than
    deleted. The main file keeps its exact schema, and a dropped quote stays
    recoverable and countable — deleting them made the guard's own misfires
    invisible (it was silently discarding four honest books at once from a 51/49
    fight before the identifiability floor landed on 2026-08-16).
    """
    now = pd.Timestamp.now(tz="UTC")
    path = Path(out_dir) / f"fightodds_{now:%Y-%m}.csv"
    qpath = Path(out_dir) / f"fightodds_quarantine_{now:%Y-%m}.csv"
    held = [r for r in rows if r.get("quarantine_reason")]
    rows = [r for r in rows if not r.get("quarantine_reason")]
    if held:
        for r in held:
            r["cycle_status"] = status
        Path(out_dir).mkdir(exist_ok=True)
        pd.DataFrame(held).to_csv(qpath, mode="a", header=not qpath.exists(), index=False)
    prev_latest = {}
    if path.exists():
        try:
            prev = pd.read_csv(path, dtype=str).fillna("")
            prev = prev.sort_values("poll_time").groupby(
                ["fight_slug", "book"], as_index=False).tail(1)
            for _, r in prev.iterrows():
                prev_latest[(r.fight_slug, r.book)] = r
        except Exception as e:
            print(f"  warning: unreadable {path.name} ({e}); writing full snapshot", flush=True)
    keep = []
    for r in rows:
        p = prev_latest.get((r["fight_slug"], r["book"]))
        if p is None:
            keep.append(r)
            continue
        changed = (str(p.get("dec1")) != r["dec1"]) or (str(p.get("dec2")) != r["dec2"])
        age = _age_h(r["poll_time"], str(p.get("poll_time")))
        if changed or age == "" or float(age) >= HEARTBEAT_H:
            keep.append(r)
    for r in keep:
        r["cycle_status"] = status
    if keep:
        Path(out_dir).mkdir(exist_ok=True)
        pd.DataFrame(keep).to_csv(path, mode="a", header=not path.exists(), index=False)
    return path, len(keep)


def write_manifest(out_dir, poll_iso, requested, succeeded, failed, rows_written, status):
    """Per-cycle health record — the authoritative freshness/liveness signal."""
    Path(out_dir).mkdir(exist_ok=True)
    (Path(out_dir) / "fightodds_cycle_latest.json").write_text(json.dumps({
        "poll_time": poll_iso, "status": status, "requested_pks": requested,
        "succeeded_pks": succeeded, "failed_pks": failed,
        "rows_written": rows_written}, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--days", type=int, default=90, help="event look-ahead window")
    ap.add_argument("--canary", choices=["discovery", "event"], default=None,
                    help="dry probes: discovery = 1 call; event = 1 event, parsed, no writes")
    ap.add_argument("--pk", type=int, default=None, help="event pk for --canary event")
    a = ap.parse_args()
    sess = creq.Session(impersonate="chrome")
    pacer = Pacer()
    poll_iso = datetime.now(timezone.utc).isoformat()

    try:
        t0 = time.monotonic()
        events = discover(sess, pacer, a.days)
        ms = (time.monotonic() - t0) * 1000
    except (CycleAbort, EventFailed) as e:
        print(f"ABORT: discovery failed: {e}", flush=True)
        if a.canary is None:
            write_manifest(a.out_dir, poll_iso, [], [], [], 0, "aborted")
        return 2
    print(f"discovery: {len(events)} upcoming ufc/dwcs events in {a.days}d ({ms:.0f}ms)",
          flush=True)
    for e in events:
        print(f"  pk={e['pk']} {e['date']} [{e['promotion']}] {e['name']}", flush=True)
    if a.canary == "discovery":
        return 0

    if a.canary == "event":
        if not events:
            print("no upcoming events to canary")
            return 2
        events = [next((e for e in events if e["pk"] == a.pk), events[0])]

    requested = [e["pk"] for e in events]
    rows, succeeded, failed, total_quar = [], [], [], 0
    try:
        for ev in events:
            try:
                d = gql(sess, pacer, EVENT_Q, {"pk": ev["pk"]})
                r, q = parse_event(d, ev, poll_iso)
                rows.extend(r)
                total_quar += q
                succeeded.append(ev["pk"])
                print(f"  ok pk={ev['pk']} {ev['name']}: {len(r)} book-quotes, "
                      f"{q} quarantined", flush=True)
            except EventFailed as e:
                failed.append(ev["pk"])
                print(f"  FAILED pk={ev['pk']} {ev['name']}: {e}", flush=True)
    except CycleAbort as e:
        print(f"ABORT mid-cycle: {e} — publishing nothing", flush=True)
        if a.canary is None:
            write_manifest(a.out_dir, poll_iso, requested, succeeded, failed, 0, "aborted")
        return 2

    if a.canary == "event":
        df = pd.DataFrame(rows)
        if len(df):
            cov = (df.groupby(["book", "book_role"]).agg(fights=("fight_slug", "nunique"))
                     .sort_values("fights", ascending=False))
            print("\ntwo-sided coverage by book:")
            print(cov.to_string())
            ex = df[df.book.isin(["BetOnline", "Pinnacle", "Bookmaker", "Stake", "Bet105"])]
            print(f"\nexecution-set rows: {len(ex)} across {ex.fight_slug.nunique()} fights")
            print("\nsample rows:")
            print(df.head(10).to_string(index=False))
        print(f"\nquarantined: {total_quar} (dry canary — nothing written)")
        return 0

    status = "complete" if not failed else "partial"
    path, n = write_rows(rows, a.out_dir, status)
    write_manifest(a.out_dir, poll_iso, requested, succeeded, failed, n, status)
    print(f"cycle {status}: {len(rows)} quotes observed, {n} rows appended -> {path.name}; "
          f"{total_quar} quarantined; failed pks: {failed or 'none'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
