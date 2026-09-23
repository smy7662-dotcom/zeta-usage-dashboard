#!/usr/bin/env python3
"""Wayback 제타 홈 화면의 공개 표본을 날짜별 횡단면 시계열로 복원함."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from backfill_wayback import ARCHIVE, CDX, request_bytes
    from collect import build_dashboard, connect, iso_z
except ModuleNotFoundError:
    from scripts.backfill_wayback import ARCHIVE, CDX, request_bytes
    from scripts.collect import build_dashboard, connect, iso_z

FLIGHT_PUSH = re.compile(r"<script>self\.__next_f\.push\((.*?)\)</script>", re.S)
FLIGHT_LINE = re.compile(r"^[0-9a-f]+:(.*)$", re.I)


def homepage_captures() -> list[dict[str, str]]:
    query = urllib.parse.urlencode(
        {
            "url": "zeta-ai.io/ko",
            "output": "json",
            "fl": "timestamp,original,statuscode,mimetype,digest",
            "filter": ["statuscode:200", "mimetype:text/html"],
            "collapse": "digest",
            "from": "2024",
            "limit": "10000",
        },
        doseq=True,
    )
    payload = json.loads(request_bytes(f"{CDX}?{query}", timeout=60).decode("utf-8"))
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    header = payload[0]
    return [dict(zip(header, row)) for row in payload[1:] if len(row) == len(header)]


def walk_plots(value: Any, found: dict[str, dict[str, Any]]) -> None:
    if isinstance(value, dict):
        plot_id = value.get("id")
        chats = value.get("interactionCount")
        name = value.get("name")
        if (
            isinstance(plot_id, str)
            and isinstance(name, str)
            and isinstance(chats, int)
            and chats >= 0
        ):
            current = found.get(plot_id)
            if current is None or chats > current["interactionCount"]:
                found[plot_id] = {
                    "id": plot_id,
                    "name": name,
                    "interactionCount": chats,
                    "interactionCountWithRegen": value.get("interactionCountWithRegen"),
                }
        for child in value.values():
            walk_plots(child, found)
    elif isinstance(value, list):
        for child in value:
            walk_plots(child, found)


def extract_homepage_plots(html: str) -> dict[str, dict[str, Any]]:
    parts: list[str] = []
    for raw in FLIGHT_PUSH.findall(html):
        try:
            value = json.loads(raw)
            if len(value) > 1 and isinstance(value[1], str):
                parts.append(value[1])
        except (json.JSONDecodeError, TypeError):
            continue
    stream = "".join(parts)
    found: dict[str, dict[str, Any]] = {}
    for line in stream.splitlines():
        match = FLIGHT_LINE.match(line)
        if not match:
            continue
        try:
            walk_plots(json.loads(match.group(1)), found)
        except json.JSONDecodeError:
            continue
    return found


def fetch_capture(capture: dict[str, str]) -> dict[str, Any] | None:
    timestamp = capture["timestamp"]
    original = capture["original"]
    url = f"{ARCHIVE}/{timestamp}id_/{original}"
    html = request_bytes(url, attempts=2, timeout=60).decode("utf-8", errors="replace")
    plots = extract_homepage_plots(html)
    if not plots:
        return None
    return {"timestamp": timestamp, "url": url, "plots": plots}


def save_result(db, result: dict[str, Any]) -> None:
    timestamp = result["timestamp"]
    observed_date = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
    observed_at = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    plots = result["plots"]
    chats = [plot["interactionCount"] for plot in plots.values()]
    regens = [
        plot["interactionCountWithRegen"]
        for plot in plots.values()
        if isinstance(plot.get("interactionCountWithRegen"), int)
    ]
    db.execute(
        """
        INSERT INTO homepage_observations (
          observed_date, observed_at, observed_plots, total_chats,
          total_chats_with_regen, average_chats_per_plot,
          median_chats_per_plot, top10_chats, source_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(observed_date) DO UPDATE SET
          observed_at=excluded.observed_at,
          observed_plots=excluded.observed_plots,
          total_chats=excluded.total_chats,
          total_chats_with_regen=excluded.total_chats_with_regen,
          average_chats_per_plot=excluded.average_chats_per_plot,
          median_chats_per_plot=excluded.median_chats_per_plot,
          top10_chats=excluded.top10_chats,
          source_url=excluded.source_url
        """,
        (
            observed_date,
            iso_z(observed_at),
            len(chats),
            sum(chats),
            sum(regens) if regens else None,
            round(statistics.mean(chats)),
            round(statistics.median(chats)),
            sum(sorted(chats, reverse=True)[:10]),
            result["url"],
        ),
    )
    for plot in plots.values():
        db.execute(
            """
            INSERT OR IGNORE INTO plots (
              plot_id, name, metric_kind, first_seen_at, last_seen_at
            ) VALUES (?, ?, 'interactionCount', ?, ?)
            """,
            (plot["id"], plot["name"], iso_z(observed_at), iso_z(observed_at)),
        )
        db.execute(
            """
            INSERT INTO plot_observations (
              plot_id, observed_date, observed_at, source,
              interaction_count, interaction_with_regen, source_url
            ) VALUES (?, ?, ?, 'wayback-home', ?, ?, ?)
            ON CONFLICT(plot_id, observed_date, source) DO UPDATE SET
              observed_at=excluded.observed_at,
              interaction_count=excluded.interaction_count,
              interaction_with_regen=excluded.interaction_with_regen,
              source_url=excluded.source_url
            """,
            (
                plot["id"],
                observed_date,
                iso_z(observed_at),
                plot["interactionCount"],
                plot.get("interactionCountWithRegen"),
                result["url"],
            ),
        )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(root / "data" / "zeta.sqlite3"))
    parser.add_argument("--out", default=str(root / "public" / "data"))
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    db = connect(Path(args.db))
    all_captures = homepage_captures()
    existing_days = {
        row[0].replace("-", "")
        for row in db.execute("SELECT observed_date FROM homepage_observations").fetchall()
    }
    captures = [
        capture
        for capture in all_captures
        if capture["timestamp"][:8] not in existing_days
    ]
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(fetch_capture, capture): capture for capture in captures}
            for future in as_completed(futures):
                capture = futures[future]
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                        print(
                            f"{result['timestamp']}: "
                            f"{len(result['plots'])} exact homepage plots"
                        )
                except Exception as exc:
                    errors.append(f"{capture['timestamp']}: {exc}")
        best_by_day: dict[str, dict[str, Any]] = {}
        for result in results:
            day = result["timestamp"][:8]
            current = best_by_day.get(day)
            if current is None or result["timestamp"] > current["timestamp"]:
                best_by_day[day] = result
        for result in sorted(best_by_day.values(), key=lambda item: item["timestamp"]):
            save_result(db, result)
        db.commit()
        build_dashboard(db, Path(args.out), errors)
        print(
            json.dumps(
                {
                    "indexedCaptures": len(all_captures),
                    "previouslyRestoredDays": len(existing_days),
                    "pendingCaptures": len(captures),
                    "savedDays": len(best_by_day),
                    "errors": len(errors),
                }
            )
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
