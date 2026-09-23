import json
import tempfile
import unittest
from pathlib import Path

from scripts.backfill_wayback import parse_counts
from scripts.collect import (
    build_dashboard,
    connect,
    ingest_payload,
    normalize_plot,
)


SAMPLE = {
    "id": "plot-1",
    "name": "테스트 플롯",
    "interactionCount": 100,
    "interactionCountWithRegen": 125,
    "hashtags": ["로맨스", "현대"],
    "creator": {"id": "creator-1", "username": "tester"},
}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = connect(self.root / "test.sqlite3")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def ingest(self, payload, day, observed_at=None, source="detail"):
        return ingest_payload(
            self.db,
            payload,
            observed_at=observed_at or f"{day}T15:10:00Z",
            observed_date=day,
            source=source,
            source_url="https://example.test/source",
        )

    def test_normalize_keeps_exact_metrics_and_tags(self):
        plot = normalize_plot(SAMPLE)
        self.assertEqual(plot["interaction_count"], 100)
        self.assertEqual(plot["interaction_with_regen"], 125)
        self.assertEqual(plot["tags"], ["로맨스", "현대"])

    def test_same_day_same_source_is_idempotent(self):
        self.ingest(SAMPLE, "2026-09-23")
        changed = dict(SAMPLE, interactionCount=140)
        self.ingest(changed, "2026-09-23", observed_at="2026-09-23T15:20:00Z")
        count = self.db.execute("SELECT COUNT(*) FROM plot_observations").fetchone()[0]
        value = self.db.execute("SELECT interaction_count FROM plot_observations").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(value, 140)

    def test_dashboard_does_not_interpolate_missing_days(self):
        self.ingest(SAMPLE, "2026-09-20")
        self.ingest(dict(SAMPLE, interactionCount=160), "2026-09-23")
        payload = build_dashboard(self.db, self.root / "out", [])
        dates = [point["date"] for point in payload["plots"][0]["series"]]
        self.assertEqual(dates, ["2026-09-20", "2026-09-23"])
        written = json.loads((self.root / "out" / "dashboard.json").read_text("utf-8"))
        self.assertEqual(len(written["platformHistory"]), 2)

    def test_wayback_parser_requires_real_integer(self):
        html = r'{\"interactionCount\":6606886,\"interactionCountWithRegen\":7973919}'
        self.assertEqual(parse_counts(html), (6606886, 7973919))
        self.assertEqual(parse_counts("interactionCount: null"), (None, None))


if __name__ == "__main__":
    unittest.main()
