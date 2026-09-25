import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.backfill_homepage import extract_homepage_plots
from scripts.backfill_wayback_api import DETAIL_API, RANKING_API
from scripts.backfill_wayback import parse_counts
from scripts.collect import (
    build_dashboard,
    build_regeneration_cohorts,
    connect,
    ensure_measurement_panel,
    import_published_history,
    ingest_payload,
    normalize_plot,
    refresh_known_plots,
    select_refresh_targets,
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
        without_regen = normalize_plot(
            {key: value for key, value in SAMPLE.items() if key != "interactionCountWithRegen"}
        )
        self.assertIsNone(without_regen["interaction_with_regen"])

    def test_regeneration_cohorts_use_fixed_top_groups_and_stop_on_equal_fields(self):
        plots = []
        for index in range(10):
            plots.append(
                {
                    "id": f"plot-{index}",
                    "name": f"플롯 {index}",
                    "chats": 10_000 - index,
                    "series": [
                        {
                            "date": "2026-01-01",
                            "chats": 100 + index,
                            "chatsWithRegen": 120 + index,
                        },
                        {
                            "date": "2026-02-01",
                            "chats": 200 + index,
                            "chatsWithRegen": 245 + index,
                        },
                        {
                            "date": "2026-03-01",
                            "chats": 300 + index,
                            "chatsWithRegen": 300 + index,
                        },
                    ],
                }
            )

        result = build_regeneration_cohorts(plots)
        top10 = result["cohorts"][0]
        self.assertEqual(top10["size"], 10)
        self.assertEqual(top10["availablePlots"], 10)
        self.assertEqual(len(top10["history"]), 1)
        point = top10["history"][0]
        self.assertEqual(point["startDate"], "2026-01-01")
        self.assertEqual(point["date"], "2026-02-01")
        self.assertEqual(point["matchedPlots"], 10)
        self.assertEqual(point["regenerationDelta"], 250)
        self.assertEqual(point["withRegenDelta"], 1_250)
        self.assertEqual(point["regenerationRatePct"], 20.0)
        self.assertEqual(result["latestSeparableDate"], "2026-02-01")
        self.assertEqual(result["unseparableFrom"], "2026-03-01")
        self.assertFalse(result["currentSeparable"])

    def test_same_day_same_source_is_idempotent(self):
        self.ingest(SAMPLE, "2026-09-23")
        changed = dict(SAMPLE, interactionCount=140)
        self.ingest(changed, "2026-09-23", observed_at="2026-09-23T15:20:00Z")
        count = self.db.execute("SELECT COUNT(*) FROM plot_observations").fetchone()[0]
        value = self.db.execute("SELECT interaction_count FROM plot_observations").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(value, 140)

    def test_checked_in_history_seed_is_idempotent_and_preserves_regen(self):
        snapshot = self.root / "published.json"
        snapshot.write_text(
            json.dumps(
                {
                    "plots": [
                        {
                            "id": "archived-plot",
                            "name": "복원 플롯",
                            "creator": "archivist",
                            "tags": ["로맨스"],
                            "series": [
                                {
                                    "date": "2024-05-22",
                                    "chats": 100,
                                    "chatsWithRegen": 125,
                                    "source": "wayback-api",
                                    "sourceUrl": "https://example.test/archive",
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.assertEqual(import_published_history(self.db, snapshot), 1)
        self.assertEqual(import_published_history(self.db, snapshot), 0)
        row = self.db.execute(
            """
            SELECT interaction_count, interaction_with_regen
            FROM plot_observations WHERE plot_id='archived-plot'
            """
        ).fetchone()
        self.assertEqual(row["interaction_count"], 100)
        self.assertEqual(row["interaction_with_regen"], 125)

    def test_core_inventory_matches_latest_plot_payloads_on_source_tie(self):
        observed_at = "2026-09-25T04:00:00Z"
        self.ingest(
            dict(SAMPLE, id="core-tie", interactionCount=1_500_000),
            "2026-09-25",
            observed_at=observed_at,
            source="detail",
        )
        self.ingest(
            dict(SAMPLE, id="core-tie", interactionCount=1_500_924),
            "2026-09-25",
            observed_at=observed_at,
            source="ranking",
        )

        payload = build_dashboard(self.db, self.root / "out-tie", [])
        direct_total = sum(
            plot["chats"]
            for plot in payload["plots"]
            if plot["chats"] >= 1_000_000
        )
        self.assertEqual(payload["coreInventory"]["coreTotalChats"], direct_total)
        self.assertEqual(direct_total, 1_500_924)

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

    def test_refresh_prioritizes_core_then_watchlist(self):
        self.ingest(dict(SAMPLE, id="ordinary", interactionCount=100_000), "2026-09-20")
        self.ingest(dict(SAMPLE, id="watch", interactionCount=750_000), "2026-09-20")
        self.ingest(dict(SAMPLE, id="core", interactionCount=1_500_000), "2026-09-20")

        requested = []

        def response(url, attempts=2):
            plot_id = url.rsplit("/", 1)[-1]
            requested.append(plot_id)
            value = {"core": 1_500_100, "watch": 750_100}[plot_id]
            return dict(SAMPLE, id=plot_id, interactionCount=value)

        with patch("scripts.collect.request_json", side_effect=response):
            count = refresh_known_plots(
                self.db,
                datetime(2026, 9, 23, tzinfo=timezone.utc),
                limit=2,
                workers=1,
                errors=[],
            )
        self.assertEqual(count, 2)
        self.assertEqual(requested, ["core", "watch"])

    def test_core_history_tracks_inventory_crossing_and_discovery(self):
        self.ingest(
            dict(SAMPLE, id="crosses", interactionCount=900_000),
            "2026-09-24",
        )
        self.ingest(
            dict(SAMPLE, id="existing-core", interactionCount=1_200_000),
            "2026-09-24",
        )
        self.ingest(
            dict(SAMPLE, id="crosses", interactionCount=1_100_000),
            "2026-09-25",
        )
        self.ingest(
            dict(SAMPLE, id="existing-core", interactionCount=1_300_000),
            "2026-09-25",
        )
        self.ingest(
            dict(SAMPLE, id="new-core", interactionCount=2_000_000),
            "2026-09-25",
        )

        payload = build_dashboard(self.db, self.root / "out-core", [])
        history = payload["coreHistory"]
        self.assertEqual(history[0]["corePlotCount"], 1)
        self.assertEqual(history[0]["coreTotalChats"], 1_200_000)
        self.assertEqual(history[1]["corePlotCount"], 3)
        self.assertEqual(history[1]["coreTotalChats"], 4_400_000)
        self.assertEqual(history[1]["crossedCoreCount"], 1)
        self.assertEqual(history[1]["discoveredCoreCount"], 1)
        self.assertEqual(history[1]["refreshedCorePlots"], 3)
        self.assertEqual(history[1]["coreCoveragePct"], 100.0)
        self.assertEqual(payload["coreInventory"], history[-1])

    def test_measurement_panel_is_fixed_and_refreshes_before_rotation(self):
        for index, chats in enumerate((2_000_000, 1_000_000, 400_000, 40_000, 4_000)):
            self.ingest(
                dict(SAMPLE, id=f"plot-{index}", interactionCount=chats),
                "2026-09-25",
            )
        now = datetime(2026, 9, 25, tzinfo=timezone.utc)
        size = ensure_measurement_panel(
            self.db,
            now,
            target_size=4,
            volume_target=2,
        )
        self.assertEqual(size, 4)
        first_members = {
            row[0]
            for row in self.db.execute(
                "SELECT plot_id FROM measurement_panel WHERE active=1"
            )
        }
        self.ingest(
            dict(SAMPLE, id="later-hit", interactionCount=9_000_000),
            "2026-09-26",
        )
        ensure_measurement_panel(
            self.db,
            now + timedelta(days=1),
            target_size=4,
            volume_target=2,
        )
        second_members = {
            row[0]
            for row in self.db.execute(
                "SELECT plot_id FROM measurement_panel WHERE active=1"
            )
        }
        self.assertEqual(first_members, second_members)
        targets = select_refresh_targets(
            self.db,
            now + timedelta(days=1),
            limit=5,
            hot_limit=1,
        )
        self.assertEqual(set(targets[:4]), first_members)
        self.assertEqual(targets[4], "later-hit")

    def test_activity_history_uses_consecutive_fixed_panel_deltas(self):
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        for index in range(15):
            day = (start + timedelta(days=index)).date().isoformat()
            self.ingest(
                dict(SAMPLE, id="panel-a", interactionCount=1_000_000 + index * 10),
                day,
                source="detail",
            )
            self.ingest(
                dict(SAMPLE, id="panel-b", interactionCount=2_000_000 + index * 20),
                day,
                source="detail",
            )
        for plot_id in ("panel-a", "panel-b"):
            self.db.execute(
                """
                INSERT INTO measurement_panel (
                  plot_id, segment, selected_at, selected_date
                ) VALUES (?, 'volume', '2026-09-01T00:00:00Z', '2026-09-01')
                """,
                (plot_id,),
            )
        self.db.commit()

        payload = build_dashboard(self.db, self.root / "out-activity", [])
        latest = payload["activityLatest"]
        self.assertEqual(latest["totalNewChats"], 30)
        self.assertEqual(latest["averageNewChatsPerPlot"], 15)
        self.assertEqual(latest["medianNewChatsPerPlot"], 15)
        self.assertEqual(latest["activeSharePct"], 100.0)
        self.assertEqual(latest["comparisonCoveragePct"], 100.0)
        self.assertEqual(latest["sevenDayAverageNewChats"], 30)
        self.assertEqual(latest["previousSevenDayAverageNewChats"], 30)
        self.assertEqual(latest["accelerationPct"], 0.0)
        self.assertEqual(latest["direction"], "provisional")


if __name__ == "__main__":
    unittest.main()
