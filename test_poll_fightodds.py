#!/usr/bin/env python3
"""Offline parser tests for poll_fightodds.py — no network, synthetic JSON only.

Covers the capture handoff's required cases: reversed outcome order, missing /
duplicate fighter slugs, cancelled fights, one-sided offers, malformed prices,
same normalized names on different fights, fail-closed pagination, book policy,
and change-detected writes.
"""
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from poll_fightodds import EventFailed, parse_event, write_rows

EV = {"pk": 999, "name": "UFC Test", "date": "2026-09-19", "promotion": "ufc"}
POLL = "2026-08-12T21:00:00+00:00"


def outcome(slug, odds):
    return {"node": {"id": "x", "name": slug, "fighter": {"slug": slug},
                     "odds": odds, "oddsOpen": odds}}


def offer(book, outcomes, ts="1754947200000"):
    return {"node": {"id": "o", "timestamp": ts,
                     "sportsbook": {"shortName": book, "slug": book.lower()},
                     "outcomes": {"edges": outcomes}}}


def fight(slug, f1, f2, offers, cancelled=False, offers_more=False):
    return {"node": {
        "slug": slug, "isCancelled": cancelled,
        "fighter1": {"firstName": f1[0], "lastName": f1[1], "slug": f1[2]},
        "fighter2": {"firstName": f2[0], "lastName": f2[1], "slug": f2[2]},
        "offers": {"edges": offers,
                   "pageInfo": {"hasNextPage": offers_more, "endCursor": None}}}}


def payload(fights, fights_more=False):
    return {"event": {"pk": 999, "fights": {
        "edges": fights, "pageInfo": {"hasNextPage": fights_more, "endCursor": None}}}}


ADESANYA = ("Israel", "Adesanya", "israel-adesanya")
BLACHOWICZ = ("Jan", "Blachowicz", "jan-blachowicz")


class ParseTests(unittest.TestCase):
    def one(self, offers, **kw):
        rows, quar = parse_event(
            payload([fight("f-1", ADESANYA, BLACHOWICZ, offers, **kw)]), EV, POLL)
        return rows, quar

    def test_orientation_is_slug_based_not_order_based(self):
        # outcomes arrive REVERSED (fighter2 first); prices must still land on
        # the right alphabetical side keys
        rows, quar = self.one([offer("BetOnline",
                                     [outcome("jan-blachowicz", 150),
                                      outcome("israel-adesanya", -180)])])
        self.assertEqual(quar, 0)
        r = rows[0]
        self.assertEqual(r["side1_key"], "israel adesanya")
        self.assertEqual(r["amer1"], "-180")  # adesanya's price on side1
        self.assertEqual(r["amer2"], "+150")
        self.assertEqual(r["pair"], "israel adesanya|jan blachowicz")

    def test_missing_fighter_slug_quarantines(self):
        bad = {"node": {"id": "x", "name": "?", "fighter": {},
                        "odds": -180, "oddsOpen": -180}}
        rows, quar = self.one([offer("BetOnline",
                                     [bad, outcome("jan-blachowicz", 150)])])
        self.assertEqual((len(rows), quar), (0, 1))

    def test_duplicate_fighter_slug_quarantines(self):
        rows, quar = self.one([offer("BetOnline",
                                     [outcome("israel-adesanya", -180),
                                      outcome("israel-adesanya", -170),
                                      outcome("jan-blachowicz", 150)])])
        self.assertEqual((len(rows), quar), (0, 1))

    def test_cancelled_fight_skipped_silently(self):
        rows, quar = self.one([offer("BetOnline",
                                     [outcome("israel-adesanya", -180),
                                      outcome("jan-blachowicz", 150)])], cancelled=True)
        self.assertEqual((len(rows), quar), (0, 0))

    def test_one_sided_offer_quarantines(self):
        rows, quar = self.one([offer("BetOnline", [outcome("israel-adesanya", -180)])])
        self.assertEqual((len(rows), quar), (0, 1))

    def test_malformed_price_quarantines(self):
        for bad in (-50, 0, None, "EVEN"):
            rows, quar = self.one([offer("BetOnline",
                                         [outcome("israel-adesanya", bad),
                                          outcome("jan-blachowicz", 150)])])
            self.assertEqual((len(rows), quar), (0, 1), msg=f"odds={bad!r}")

    def test_same_normalized_names_on_different_fights_stay_distinct(self):
        a = fight("f-1", ("John", "Smith", "john-smith-1"), ("Bob", "Jones", "bob-jones-1"),
                  [offer("Circa", [outcome("john-smith-1", -120), outcome("bob-jones-1", 100)])])
        b = fight("f-2", ("John", "Smith", "john-smith-2"), ("Bob", "Jones", "bob-jones-2"),
                  [offer("Circa", [outcome("john-smith-2", -300), outcome("bob-jones-2", 250)])])
        rows, quar = parse_event(payload([a, b]), EV, POLL)
        self.assertEqual((len(rows), quar), (2, 0))
        self.assertEqual({r["fight_slug"] for r in rows}, {"f-1", "f-2"})

    def test_same_normalized_names_inside_one_fight_quarantines(self):
        rows, quar = self.one([])  # rebuilt below with colliding names
        f = fight("f-1", ("Jose", "Silva", "jose-silva-a"), ("José", "Silva", "jose-silva-b"),
                  [offer("Circa", [outcome("jose-silva-a", -120), outcome("jose-silva-b", 100)])])
        rows, quar = parse_event(payload([f]), EV, POLL)
        self.assertEqual((len(rows), quar), (0, 1))

    def test_pagination_fail_closed_on_fights(self):
        with self.assertRaises(EventFailed):
            parse_event(payload([], fights_more=True), EV, POLL)

    def test_pagination_fail_closed_on_offers(self):
        f = fight("f-1", ADESANYA, BLACHOWICZ,
                  [offer("BetOnline", [outcome("israel-adesanya", -180),
                                       outcome("jan-blachowicz", 150)])], offers_more=True)
        with self.assertRaises(EventFailed):
            parse_event(payload([f]), EV, POLL)

    def test_empty_event_payload_fails(self):
        with self.assertRaises(EventFailed):
            parse_event({"event": None}, EV, POLL)

    def test_excluded_clone_books_dropped_without_quarantine(self):
        rows, quar = self.one([offer("BetDSI", [outcome("israel-adesanya", -180),
                                                outcome("jan-blachowicz", 150)]),
                               offer("SportsBetting", [outcome("israel-adesanya", -181),
                                                       outcome("jan-blachowicz", 151)])])
        self.assertEqual((len(rows), quar), (0, 0))

    def test_exchange_labeled_not_sportsbook(self):
        rows, _ = self.one([offer("Novig", [outcome("israel-adesanya", -180),
                                            outcome("jan-blachowicz", 150)]),
                            offer("BetOnline", [outcome("israel-adesanya", -175),
                                                outcome("jan-blachowicz", 145)])])
        roles = {r["book"]: r["book_role"] for r in rows}
        self.assertEqual(roles, {"Novig": "exchange", "BetOnline": "sportsbook"})

    def test_implausible_overround_quarantines(self):
        rows, quar = self.one([offer("BetOnline",  # -400/-400 -> overround 1.6
                                     [outcome("israel-adesanya", -400),
                                      outcome("jan-blachowicz", -400)])])
        self.assertEqual((len(rows), quar), (0, 1))

    def test_source_ts_epoch_ms_parsed(self):
        rows, _ = self.one([offer("BetOnline", [outcome("israel-adesanya", -180),
                                                outcome("jan-blachowicz", 150)],
                                  ts="1754947200000")])
        self.assertTrue(rows[0]["source_offer_ts"].startswith("2025-08-11T2"))


class WriteTests(unittest.TestCase):
    def row(self, dec1="1.5556", dec2="2.5000", book="BetOnline", poll=POLL):
        return {"poll_time": poll, "event_pk": 999, "pair": "a|b", "fight_slug": "f-1",
                "event_date": "2026-09-19", "event_name": "UFC Test", "promotion": "ufc",
                "side1_key": "a", "side2_key": "b", "book": book, "book_role": "sportsbook",
                "dec1": dec1, "dec2": dec2, "amer1": "-180", "amer2": "+150",
                "source_offer_ts": "", "source_change_age_h": "", "cycle_status": ""}

    def test_change_detection(self):
        with tempfile.TemporaryDirectory() as td:
            p, n = write_rows([self.row()], td, "complete")
            self.assertEqual(n, 1)  # first sight -> written
            _, n = write_rows([self.row(poll="2026-08-12T22:00:00+00:00")], td, "complete")
            self.assertEqual(n, 0)  # unchanged 1h later -> skipped
            _, n = write_rows([self.row(dec1="1.6000",
                                        poll="2026-08-12T23:00:00+00:00")], td, "complete")
            self.assertEqual(n, 1)  # price change -> written
            _, n = write_rows([self.row(dec1="1.6000",
                                        poll="2026-08-14T00:00:00+00:00")], td, "complete")
            self.assertEqual(n, 1)  # >24h heartbeat -> written
            df = pd.read_csv(p)
            self.assertEqual(len(df), 3)
            self.assertTrue((df.cycle_status == "complete").all())


if __name__ == "__main__":
    unittest.main(verbosity=2)
