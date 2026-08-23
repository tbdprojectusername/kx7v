import unittest

from poll_fightodds_props import parse_page


class PropParserTests(unittest.TestCase):
    def test_prematch_props_kept_and_moneyline_live_categories_excluded(self):
        offer = {
            "id": "offer-1", "status": "O", "disabled": False,
            "timestamp": "1700000000", "createdAt": "1699990000", "value": "2.5",
            "offerType": {"offerTypeId": "TOTAL", "category": "A_5", "subCategory": "A_13"},
            "sportsbook": {"shortName": "BetOnline"},
            "outcomes": {"edges": [{"node": {
                "id": "outcome-1", "name": "Over", "odds": -120,
                "oddsOpen": -110, "oddsBest": 105, "oddsWorst": -135,
                "fighter": None,
            }}]},
        }
        data = {"event": {"fights": {"pageInfo": {"hasNextPage": False}, "edges": [
            {"node": {"slug": "a-vs-b-1", "isCancelled": False,
                      "offers": {"pageInfo": {"hasNextPage": False}, "edges": [
                          {"node": offer},
                          {"node": {**offer, "id": "ml", "offerType": {"category": "A_1"}}},
                          {"node": {**offer, "id": "live", "offerType": {"category": "A_2"}}},
                      ]}}}
        ]}}}
        event = {"pk": 1, "date": "2026-08-24", "name": "UFC Test", "promotion": "ufc"}
        rows, more, invalid = parse_page(data, event, "2026-08-23T12:00:00+00:00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "A_5")
        self.assertEqual(rows[0]["american"], "-120")
        self.assertFalse(more)
        self.assertEqual(invalid, 0)


if __name__ == "__main__":
    unittest.main()
