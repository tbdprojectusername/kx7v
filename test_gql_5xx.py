#!/usr/bin/env python3
"""A 5xx with an HTML error body is a soft failure, not a bot challenge.

2026-09-16: nine of eleven cycle aborts were `HTML challenge page (HTTP 502)`
— an upstream 502 whose HTML body tripped the challenge check ahead of the
5xx branch, aborting the whole cycle (and clobbering the health manifest)
on a momentary gateway error.
"""
from __future__ import annotations

import unittest
from unittest import mock

import poll_fightodds
from poll_fightodds import CycleAbort, EventFailed, gql


class _Resp:
    def __init__(self, code, ctype="application/json", payload=None):
        self.status_code = code
        self.headers = {"content-type": ctype}
        self._payload = payload

    def json(self):
        return self._payload


class _Sess:
    def __init__(self, responses):
        self._r = list(responses)

    def post(self, *a, **k):
        return self._r.pop(0)


class _Pacer:
    def wait(self):
        pass


class Gql5xx(unittest.TestCase):
    def test_html_502_then_200_succeeds(self):
        sess = _Sess([_Resp(502, "text/html"), _Resp(200, payload={"data": {"ok": 1}})])
        with mock.patch.object(poll_fightodds.time, "sleep"):
            self.assertEqual(gql(sess, _Pacer(), "q", {}), {"ok": 1})

    def test_persistent_html_502_fails_the_call_not_the_cycle(self):
        sess = _Sess([_Resp(502, "text/html")] * 3)
        with mock.patch.object(poll_fightodds.time, "sleep"):
            with self.assertRaises(EventFailed):
                gql(sess, _Pacer(), "q", {})

    def test_html_200_is_still_a_challenge(self):
        with self.assertRaises(CycleAbort):
            gql(_Sess([_Resp(200, "text/html")]), _Pacer(), "q", {})

    def test_403_still_aborts(self):
        with self.assertRaises(CycleAbort):
            gql(_Sess([_Resp(403)]), _Pacer(), "q", {})


if __name__ == "__main__":
    unittest.main()
