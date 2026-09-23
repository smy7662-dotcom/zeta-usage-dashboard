import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.backfill_homepage import extract_homepage_plots
from scripts.backfill_wayback_api import DETAIL_API, RANKING_API
from scripts.backfill_wayback import parse_counts
from scripts.collect import (
    build_dashboard,
    connect,
    ingest_payload,
    normalize_plot,
    refresh_known_plots,
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
        self.assertEqual(written["plots"][0]["series"][0]["source"], "detail")

    def test_wayback_parser_requires_real_integer(self):
        html = r'{\"interactionCount\":6606886,\"interactionCountWithRegen\":7973919}'
        self.assertEqual(parse_counts(html), (6606886, 7973919))
        self.assertEqual(parse_counts("interactionCount: null"), (None, None))

    def test_homepage_parser_extracts_flight_plot(self):
        flight = '14:[{"id":"plot-a","name":"플롯 A","interactionCount":123,"interactionCountWithRegen":140}]\n'
        html = f"<script>self.__next_f.push({json.dumps([1, flight])})</script>"
        plots = extract_homepage_plots(html)
        self.assertEqual(plots["plot-a"]["interactionCount"], 123)
        self.assertEqual(plots["plot-a"]["interactionCountWithRegen"], 140)

    def test_homepage_growth_index_uses_matched_plots(self):
        for day, multiplier in (("2024-05-22", 1), ("2024-05-25", 2)):
            self.db.execute(
                """
                INSERT INTO homepage_observations (
                  observed_date, observed_at, observed_plots, total_chats,
                  average_chats_per_plot, median_chats_per_plot, top10_chats,
                  source_url
                ) VALUES (?, ?, 10, ?, ?, ?, ?, ?)
                """,
                (day, f"{day}T00:00:00Z", 550 * multiplier,
                 55 * multiplier, 55 * multiplier, 550 * multiplier,
                 f"https://example.test/{day}"),
            )
            for index in range(10):
                plot_id = f"plot-{index}"
                self.db.execute(
                    "INSERT OR IGNORE INTO plots (plot_id, name, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
                    (plot_id, plot_id, f"{day}T00:00:00Z", f"{day}T00:00:00Z"),
                )
                self.db.execute(
                    """
                    INSERT INTO plot_observations (
                      plot_id, observed_date, observed_at, source,
                      interaction_count, source_url
                    ) VALUES (?, ?, ?, 'wayback-home', ?, ?)
                    """,
                    (plot_id, day, f"{day}T00:00:00Z",
                     (index + 1) * 10 * multiplier, f"https://example.test/{day}"),
                )
        self.db.commit()
        payload = build_dashboard(self.db, self.root / "out", [])
        history = payload["homepageHistory"]
        self.assertEqual(history[0]["matchedGrowthIndex"], 100.0)
        self.assertEqual(history[1]["matchedGrowthIndex"], 200.0)
        self.assertEqual(history[1]["matchedPlots"], 10)

        for index in range(10):
            plot_id = f"plot-{index}"
            self.db.execute(
                """
                INSERT INTO plot_observations (
                  plot_id, observed_date, observed_at, source,
                  interaction_count, source_url
                ) VALUES (?, '2026-09-23', '2026-09-23T00:00:00Z',
                          'detail', ?, 'https://api.example.test/current')
                """,
                (plot_id, (index + 1) * 40),
            )
        self.db.commit()
        payload = build_dashboard(self.db, self.root / "out-current", [])
        current = payload["matchedGrowthHistory"][-1]
        self.assertEqual(current["date"], "2026-09-23")
        self.assertEqual(current["matchedGrowthIndex"], 400.0)
        self.assertEqual(current["matchedPlots"], 10)
        self.assertTrue(current["isCurrent"])

    def test_wayback_api_url_filter_excludes_subresources(self):
        detail = "https://api.zeta-ai.io/v1/plots/004c611d-3ca2-46d3-b443-786552edfe94"
        self.assertIsNotNone(DETAIL_API.match(detail))
        self.assertIsNone(DETAIL_API.match(f"{detail}/comments/count"))
        self.assertIsNotNone(
            RANKING_API.match(
                "https://api.zeta-ai.io/v1/plots/ranking?limit=10&type=DAILY"
            )
        )

    def test_refresh_prioritizes_wayback_cohort_for_current_matching(self):
        self.ingest(
            dict(SAMPLE, id="historical"),
            "2026-06-18",
            observed_at="2026-09-22T00:00:00Z",
            source="wayback-home",
        )
        self.ingest(
            dict(SAMPLE, id="not-historical"),
            "2026-01-01",
            observed_at="2026-01-01T00:00:00Z",
            source="detail",
        )
        refreshed = dict(SAMPLE, id="historical", interactionCount=400)
        errors = []
        with patch("scripts.collect.request_json", return_value=refreshed) as request:
            count = refresh_known_plots(
                self.db,
                datetime(2026, 9, 23, tzinfo=timezone.utc),
                limit=1,
                workers=1,
                errors=errors,
            )
        self.assertEqual(count, 1)
        self.assertEqual(errors, [])
        self.assertTrue(request.call_args.args[0].endswith("/v1/plots/historical"))


if __name__ == "__main__":
    unittest.main()
