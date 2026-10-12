import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.backfill_homepage import extract_homepage_plots
from scripts.backfill_wayback_api import DETAIL_API, RANKING_API
from scripts.backfill_wayback import parse_counts
from scripts.collect import (
    build_dashboard,
    build_regeneration_cohorts,
    collect_comments,
    connect,
    crawl_tag_queue,
    ensure_measurement_panel,
    import_archive,
    import_external_history,
    import_published_history,
    ingest_payload,
    normalize_plot,
    plan_collection,
    ranking_url,
    refresh_known_plots,
    refresh_plot_ids,
    repair_last_values,
    select_followup_targets,
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

    def test_trending_ranking_uses_required_genre_filter(self):
        url = ranking_url("TRENDING", limit=30)
        self.assertIn("type=TRENDING", url)
        self.assertIn("limit=30", url)
        self.assertIn("genres=ALL", url)
        self.assertIn("filterType=GENRE", url)
        self.assertIn("filterValues=all", url)
        self.assertNotIn("gender=ALL", url)
        self.assertIn("gender=ALL", ranking_url("WEEKLY"))

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
        self.assertEqual(latest["intervalHours"], 24.0)
        self.assertEqual(latest["newChatsPer24h"], 30)
        self.assertEqual(latest["validStreakDays"], 14)

    def add_panel(self, plot_ids, selected_date="2026-09-01"):
        for plot_id in plot_ids:
            self.db.execute(
                """
                INSERT INTO measurement_panel (
                  plot_id, segment, selected_at, selected_date
                ) VALUES (?, 'volume', ?, ?)
                """,
                (plot_id, f"{selected_date}T00:00:00Z", selected_date),
            )
        self.db.commit()

    def test_activity_uses_actual_interval_and_skips_off_schedule_day(self):
        # 09-01 값은 오후 수동 재실행으로 덮여 다음 날까지 12시간뿐인 상황
        observations = (
            ("2026-09-01", "2026-09-01T07:00:00Z", 1_000),
            ("2026-09-02", "2026-09-01T19:00:00Z", 1_500),
            ("2026-09-03", "2026-09-02T19:30:00Z", 2_520),
        )
        for day, observed_at, chats in observations:
            self.ingest(
                dict(SAMPLE, id="panel-a", interactionCount=chats),
                day,
                observed_at=observed_at,
            )
        self.add_panel(["panel-a"])

        history = build_dashboard(self.db, self.root / "out-interval", [])["activityHistory"]
        short_day, normal_day = history[1], history[2]
        self.assertEqual(short_day["totalNewChats"], 500)
        self.assertEqual(short_day["intervalHours"], 12.0)
        self.assertEqual(short_day["newChatsPer24h"], 1_000)
        self.assertFalse(short_day["timingValid"])
        self.assertFalse(short_day["validForTrend"])
        self.assertEqual(short_day["validStreakDays"], 0)
        self.assertEqual(short_day["observedClockKst"], "04:00")

        self.assertEqual(normal_day["totalNewChats"], 1_020)
        self.assertEqual(normal_day["intervalHours"], 24.5)
        self.assertEqual(normal_day["newChatsPer24h"], round(1_020 * 24 / 24.5))
        self.assertTrue(normal_day["validForTrend"])
        self.assertEqual(normal_day["validStreakDays"], 1)

    def test_followup_refreshes_every_watch_plot_before_new_and_rotation(self):
        now = datetime(2026, 9, 28, 19, 0, tzinfo=timezone.utc)
        self.ingest(dict(SAMPLE, id="panel", interactionCount=9_000_000), "2026-09-25")
        self.add_panel(["panel"], "2026-09-25")
        snapshot = self.root / "snapshot.json"
        snapshot.write_text(
            json.dumps(
                {
                    "plots": [
                        {
                            "id": "snapshot-core",
                            "name": "스냅샷 핵심",
                            "series": [
                                {"date": "2026-09-23", "chats": 3_000_000, "source": "detail"}
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        import_published_history(self.db, snapshot)
        self.ingest(dict(SAMPLE, id="watch", interactionCount=600_000), "2026-09-26")
        self.db.execute(
            "INSERT INTO plots (plot_id, name, first_seen_at, last_seen_at) VALUES "
            "('orphan-core', '값 비어 있음', '2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z')"
        )
        self.db.execute(
            """
            INSERT INTO plot_observations (
              plot_id, observed_date, observed_at, source, interaction_count, source_url
            ) VALUES ('orphan-core', '2026-09-20', '2026-09-20T00:00:00Z', 'tag:x', 1_200_000, 'u')
            """
        )
        self.db.commit()
        self.assertEqual(repair_last_values(self.db), 1)
        self.ingest(
            dict(SAMPLE, id="new-small", interactionCount=5_000,
                 releasedAt="2026-09-27T10:00:00Z"),
            "2026-09-10",
            observed_at="2026-09-10T00:00:00Z",
        )
        self.ingest(
            dict(SAMPLE, id="new-big", interactionCount=90_000,
                 releasedAt="2026-09-26T10:00:00Z"),
            "2026-09-10",
            observed_at="2026-09-10T00:00:00Z",
        )
        self.ingest(
            dict(SAMPLE, id="old-small", interactionCount=100, releasedAt="2024-01-01T00:00:00Z"),
            "2026-09-10",
            observed_at="2026-09-10T00:00:00Z",
        )

        targets = select_followup_targets(self.db, now, limit=10, hot_limit=1)
        self.assertEqual(targets[:3], ["snapshot-core", "orphan-core", "watch"])
        self.assertEqual(targets[3], "new-big")
        self.assertNotIn("panel", targets)
        self.assertEqual(set(targets[4:]), {"new-small", "old-small"})

    def test_comments_never_copy_an_older_day_count(self):
        self.ingest(dict(SAMPLE, id="quiet"), "2026-09-20", observed_at="2026-09-20T15:10:00Z")
        self.ingest(dict(SAMPLE, id="fresh", interactionCount=300), "2026-09-23",
                    observed_at="2026-09-22T19:00:00Z")
        with patch("scripts.collect.request_json", return_value={"count": 7}):
            collect_comments(
                self.db,
                datetime(2026, 9, 22, 19, 5, tzinfo=timezone.utc),
                limit=10,
                errors=[],
            )
        rows = {
            row["plot_id"]: row
            for row in self.db.execute(
                "SELECT * FROM plot_observations WHERE source='live' AND observed_date='2026-09-23'"
            )
        }
        self.assertIsNone(rows["quiet"]["interaction_count"])
        self.assertEqual(rows["quiet"]["comment_count"], 7)
        self.assertEqual(rows["fresh"]["interaction_count"], 300)

        payload = build_dashboard(self.db, self.root / "out-comments", [])
        quiet = next(plot for plot in payload["plots"] if plot["id"] == "quiet")
        self.assertEqual(quiet["chats"], 100)
        self.assertEqual(quiet["comments"], 7)
        self.assertEqual(payload["coreInventory"]["observedPlots"], 1)

    def test_missing_plot_is_recorded_and_leaves_core_inventory(self):
        self.ingest(dict(SAMPLE, id="gone", interactionCount=1_500_000), "2026-09-24",
                    observed_at="2026-09-23T19:00:00Z")
        self.ingest(dict(SAMPLE, id="alive", interactionCount=2_000_000), "2026-09-24",
                    observed_at="2026-09-23T19:00:00Z")

        def response(url, attempts=2):
            if url.endswith("/gone"):
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return dict(SAMPLE, id="alive", interactionCount=2_000_700)

        errors = []
        with patch("scripts.collect.request_json", side_effect=response):
            result = refresh_plot_ids(
                self.db,
                datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc),
                ["gone", "alive"],
                workers=1,
                errors=errors,
            )
        self.assertEqual(result, {"requested": 2, "completed": 1, "missing": 1})
        self.assertEqual(errors, [])
        missing = self.db.execute(
            "SELECT missing_since FROM plots WHERE plot_id='gone'"
        ).fetchone()[0]
        self.assertIsNotNone(missing)

        payload = build_dashboard(self.db, self.root / "out-missing", [])
        latest = payload["coreHistory"][-1]
        self.assertEqual(latest["date"], "2026-09-25")
        self.assertEqual(latest["corePlotCount"], 1)
        self.assertEqual(latest["exitedCoreCount"], 1)
        self.assertEqual(latest["missingCoreCount"], 1)
        self.assertEqual(latest["exitedCoreChats"], 1_500_000)
        self.assertEqual(latest["existingCoreGrowth"], 700)
        self.assertEqual(payload["coverage"]["missingPlots"], 1)
        gone = next(plot for plot in payload["plots"] if plot["id"] == "gone")
        self.assertIsNotNone(gone["missingSince"])

    def test_core_change_decomposes_exactly(self):
        self.ingest(dict(SAMPLE, id="crosses", interactionCount=900_000), "2026-09-24")
        self.ingest(dict(SAMPLE, id="existing", interactionCount=1_200_000), "2026-09-24")
        self.ingest(dict(SAMPLE, id="crosses", interactionCount=1_100_000), "2026-09-25")
        self.ingest(dict(SAMPLE, id="existing", interactionCount=1_300_000), "2026-09-25")
        self.ingest(dict(SAMPLE, id="late", interactionCount=2_000_000), "2026-09-25")
        point = build_dashboard(self.db, self.root / "out-decompose", [])["coreHistory"][-1]
        self.assertEqual(point["previousCoreTotalChats"], 1_200_000)
        self.assertEqual(point["existingCoreGrowth"], 100_000)
        self.assertEqual(point["crossedCoreChats"], 1_100_000)
        self.assertEqual(point["discoveredCoreChats"], 2_000_000)
        self.assertEqual(
            point["coreTotalChats"] - point["previousCoreTotalChats"],
            point["existingCoreGrowth"]
            + point["crossedCoreChats"]
            + point["discoveredCoreChats"]
            + point["returnedCoreChats"]
            - point["exitedCoreChats"],
        )

    def test_archive_restores_every_wayback_row_and_homepage_summary(self):
        self.ingest(dict(SAMPLE, id="archived"), "2024-05-22",
                    observed_at="2024-05-22T00:00:00Z", source="wayback-home")
        archive = self.root / "archive.json"
        archive.write_text(
            json.dumps(
                {
                    "plots": [{"id": "archived", "name": "복원", "tags": ["bl"]}],
                    "observations": [
                        {"plotId": "archived", "date": "2024-05-22",
                         "observedAt": "2024-05-22T16:51:34Z", "source": "wayback-home",
                         "chats": 100, "sourceUrl": "https://web.archive.org/web/20240522165134id_/x"},
                        {"plotId": "archived", "date": "2024-05-22",
                         "observedAt": "2024-05-22T17:07:19Z", "source": "wayback-api",
                         "chats": 104, "chatsWithRegen": 130, "sourceUrl": "https://example.test/api"},
                    ],
                    "homepage": [
                        {"date": "2024-05-22", "observedAt": "2024-05-22T16:51:34Z",
                         "observedPlots": 1, "totalChats": 100, "averageChatsPerPlot": 100,
                         "medianChatsPerPlot": 100, "top10Chats": 100,
                         "sourceUrl": "https://web.archive.org/web/20240522165134id_/x"}
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        first = import_archive(self.db, archive)
        self.assertEqual(first, {"plots": 0, "observations": 1, "homepage": 1})
        self.assertEqual(import_archive(self.db, archive), {"plots": 0, "observations": 0, "homepage": 0})
        home_row = self.db.execute(
            "SELECT observed_at FROM plot_observations WHERE source='wayback-home'"
        ).fetchone()[0]
        self.assertEqual(home_row, "2024-05-22T16:51:34Z")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM plot_observations").fetchone()[0], 2
        )
        payload = build_dashboard(self.db, self.root / "out-archive", [])
        self.assertEqual(len(payload["homepageHistory"]), 1)

    def test_external_history_is_idempotent_and_never_becomes_platform_inventory(self):
        archive = self.root / "external.json"
        archive.write_text(
            json.dumps(
                {
                    "plots": [
                        {
                            "id": "external-plot",
                            "name": "외부 과거 플롯",
                            "creator": "공개 제작자",
                            "tags": ["hl"],
                            "firstSeenAt": "2026-02-02T03:43:14Z",
                            "lastSeenAt": "2026-02-04T00:27:01Z",
                        }
                    ],
                    "observations": [
                        {
                            "plotId": "external-plot",
                            "date": "2026-02-02",
                            "observedAt": "2026-02-02T03:43:14Z",
                            "source": "external:otoscor-zeta",
                            "chats": 1_500_000,
                            "sourceUrl": "https://github.com/Otoscor/chatbotmonitoring/blob/example/data.json",
                        },
                        {
                            "plotId": "external-plot",
                            "date": "2026-02-04",
                            "observedAt": "2026-02-04T00:27:01Z",
                            "source": "external:otoscor-zeta",
                            "chats": 1_600_000,
                            "sourceUrl": "https://github.com/Otoscor/chatbotmonitoring/blob/example2/data.json",
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            import_external_history(self.db, archive),
            {"plots": 1, "observations": 2},
        )
        self.assertEqual(
            import_external_history(self.db, archive),
            {"plots": 0, "observations": 0},
        )
        payload = build_dashboard(self.db, self.root / "out-external", [])
        self.assertEqual(payload["coverage"]["externalHistoryPlots"], 1)
        self.assertEqual(payload["coverage"]["externalHistoryObservations"], 2)
        self.assertEqual(payload["platformHistory"], [])
        self.assertEqual(payload["coreHistory"], [])
        plot = next(row for row in payload["plots"] if row["id"] == "external-plot")
        self.assertTrue(plot["historicalOnly"])
        self.assertEqual(plot["latestObservedDate"], "2026-02-04")
        self.assertEqual(len(plot["series"]), 2)

    def add_homepage_sample(self, day, plot_ids, chats=100):
        self.db.execute(
            """
            INSERT INTO homepage_observations (
              observed_date, observed_at, observed_plots, total_chats,
              average_chats_per_plot, median_chats_per_plot, top10_chats, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'https://example.test/home')
            """,
            (day, f"{day}T00:00:00Z", len(plot_ids), chats * len(plot_ids), chats, chats, chats * 10),
        )
        for plot_id in plot_ids:
            self.db.execute(
                "INSERT OR IGNORE INTO plots (plot_id, name, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
                (plot_id, plot_id, f"{day}T00:00:00Z", f"{day}T00:00:00Z"),
            )
            self.db.execute(
                """
                INSERT INTO plot_observations (
                  plot_id, observed_date, observed_at, source, interaction_count, source_url
                ) VALUES (?, ?, ?, 'wayback-home', ?, 'https://example.test/home')
                """,
                (plot_id, day, f"{day}T00:00:00Z", chats),
            )
        self.db.commit()

    def test_growth_index_needs_most_of_the_old_sample_to_link_today(self):
        sample = [f"home-{index}" for index in range(20)]
        self.add_homepage_sample("2026-06-18", sample)
        for plot_id in sample[:11]:
            self.ingest(dict(SAMPLE, id=plot_id, interactionCount=300), "2026-09-28")
        history = build_dashboard(self.db, self.root / "out-link-low", [])["matchedGrowthHistory"]
        self.assertFalse(any(point.get("isCurrent") for point in history))

        self.ingest(dict(SAMPLE, id=sample[11], interactionCount=300), "2026-09-28")
        history = build_dashboard(self.db, self.root / "out-link-ok", [])["matchedGrowthHistory"]
        current = history[-1]
        self.assertTrue(current["isCurrent"])
        self.assertEqual(current["matchedPlots"], 12)
        self.assertEqual(current["comparisonPlots"], 20)
        self.assertEqual(current["matchedGrowthIndex"], 300.0)

    def test_followup_rereads_recent_wayback_home_sample(self):
        now = datetime(2026, 9, 28, 19, 0, tzinfo=timezone.utc)
        self.add_homepage_sample("2026-06-18", ["old-home"], chats=5_000)
        self.ingest(dict(SAMPLE, id="bigger-rotation", interactionCount=90_000), "2026-09-01",
                    observed_at="2026-09-01T00:00:00Z")
        repair_last_values(self.db)
        targets = select_followup_targets(self.db, now, limit=1, hot_limit=0)
        self.assertEqual(targets, ["old-home"])

    def test_plan_skips_second_collection_on_the_same_kst_day(self):
        # 2026-09-27T18:58Z = KST 09-28 03:58
        self.db.execute(
            "INSERT INTO runs(started_at, finished_at, success) VALUES (?, ?, 1)",
            ("2026-09-27T18:58:12Z", "2026-09-27T19:11:15Z"),
        )
        self.db.commit()
        later_same_day = datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
        next_day = datetime(2026, 9, 28, 15, 10, tzinfo=timezone.utc)
        self.assertEqual(plan_collection(self.db, later_same_day, "auto")[0], False)
        self.assertEqual(plan_collection(self.db, later_same_day, "collect")[0], True)
        self.assertEqual(plan_collection(self.db, later_same_day, "build-only")[0], False)
        self.assertEqual(plan_collection(self.db, next_day, "auto")[0], True)

    def test_data_branch_export_is_stable_and_restores_everything(self):
        from scripts.export_data_branch import export
        from scripts.restore_from_data_branch import restore

        self.ingest(dict(SAMPLE, id="a", interactionCount=1_000), "2026-09-27",
                    observed_at="2026-09-26T19:00:05Z")
        self.ingest(dict(SAMPLE, id="a", interactionCount=1_500), "2026-09-28",
                    observed_at="2026-09-27T19:00:07Z")
        self.ingest(dict(SAMPLE, id="b", interactionCount=2_000_000, hashtags=["bl"]),
                    "2026-09-28", observed_at="2026-09-27T19:00:09Z", source="tag:bl")
        self.ingest(dict(SAMPLE, id="old"), "2024-05-22",
                    observed_at="2024-05-22T16:51:34Z", source="wayback-home")
        self.add_panel(["a"], "2026-09-27")
        self.db.execute(
            "INSERT INTO runs(started_at, finished_at, success, errors_json) "
            "VALUES ('2026-09-27T19:00:00Z', '2026-09-27T19:10:00Z', 1, '[\"x\"]')"
        )
        self.db.execute(
            "UPDATE plots SET missing_since='2026-09-27T19:00:08Z', missing_date='2026-09-28' "
            "WHERE plot_id='old'"
        )
        self.db.commit()

        out = self.root / "datastore"
        summary = export(self.root / "test.sqlite3", out, export_all=True, day=None)
        self.assertEqual(summary["days"], {"2026-09-27": 1, "2026-09-28": 2})
        self.assertEqual(summary["wayback"], 1)
        first_bytes = {path: path.read_bytes() for path in out.rglob("*.gz")}
        export(self.root / "test.sqlite3", out, export_all=True, day=None)
        self.assertEqual(first_bytes, {path: path.read_bytes() for path in out.rglob("*.gz")})
        # 같은 날 일일 내보내기는 그날 조회한 플롯만 담지만, 첫날 전체 스냅샷을 지우면 안 됨
        daily = export(self.root / "test.sqlite3", out, export_all=False, day=None)
        self.assertEqual(list(daily["days"]), ["2026-09-28"])
        self.assertEqual(daily["plots"], 3)

        restored_path = self.root / "restored.sqlite3"
        counts = restore(out, restored_path)
        self.assertEqual(counts["observations"], 3)
        restored = connect(restored_path)

        def table(db, sql):
            return [tuple(row) for row in db.execute(sql)]

        for sql in (
            "SELECT * FROM plot_observations ORDER BY plot_id, observed_date, source",
            "SELECT plot_id, tag FROM plot_tags ORDER BY plot_id, tag",
            "SELECT plot_id, segment, selected_date, active FROM measurement_panel ORDER BY plot_id",
            "SELECT run_id, started_at, finished_at, success, errors_json FROM runs ORDER BY run_id",
            "SELECT plot_id, name, last_interaction_count, missing_since, missing_date FROM plots ORDER BY plot_id",
        ):
            self.assertEqual(table(self.db, sql), table(restored, sql), sql)
        restored.close()

    def test_requests_carry_the_selected_market_header(self):
        from scripts import collect

        seen = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(request, timeout=0):
            seen.append(request.get_header("X-user-language"))
            return FakeResponse()

        original = collect.MARKET_LANGUAGE
        try:
            with patch("scripts.collect.urllib.request.urlopen", side_effect=fake_urlopen):
                collect.request_json("https://example.test/a")
                collect.set_market("JAPANESE")
                collect.request_json("https://example.test/b")
        finally:
            collect.set_market(original)
        self.assertEqual(seen, ["KOREAN", "JAPANESE"])
        with self.assertRaises(ValueError):
            collect.set_market("FRENCH")

    def test_other_markets_use_their_own_db_and_skip_korean_archives(self):
        from scripts.collect import market_paths

        root = Path("/repo")
        korean = market_paths(root, "KOREAN")
        japanese = market_paths(root, "JAPANESE")
        self.assertTrue(korean["db"].endswith("zeta.sqlite3"))
        self.assertIsNotNone(korean["archive"])
        self.assertTrue(japanese["db"].endswith("zeta-ja.sqlite3"))
        self.assertTrue(Path(japanese["out"]).as_posix().endswith("public/data/ja"))
        self.assertIsNone(japanese["archive"])
        self.assertIsNone(japanese["seed_snapshot"])
        self.assertIsNone(japanese["external_history"])

    def test_tag_crawl_starts_with_tags_holding_more_known_plots(self):
        for index in range(3):
            self.ingest(dict(SAMPLE, id=f"big-{index}", hashtags=["zzz"]), "2026-09-25")
        self.ingest(dict(SAMPLE, id="small", hashtags=["aaa"]), "2026-09-25")
        requested = []

        def response(url, attempts=3):
            requested.append(url)
            if "plot-count" in url:
                return {"count": 3}
            return {"plots": [], "nextCursor": None}

        with patch("scripts.collect.request_json", side_effect=response):
            crawl_tag_queue(
                self.db,
                datetime(2026, 9, 25, tzinfo=timezone.utc),
                max_pages=1,
                errors=[],
            )
        self.assertIn("%23zzz", requested[0])


if __name__ == "__main__":
    unittest.main()
