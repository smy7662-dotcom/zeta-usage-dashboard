#!/usr/bin/env python3
"""Wayback 원자료를 체크인용 아카이브 JSON으로 내보냄.

공개 대시보드 시계열은 플롯-날짜마다 대표 1행만 담아 같은 날 다른 원천 행과
홈 캡처 요약을 잃는다. 이 스크립트는 수집 DB의 Wayback 계열 행을 원천별로
모두 내보내고, 기존 아카이브와 합쳐 행을 지우지 않는다.

예시:
  python scripts/export_wayback_archive.py --db data/zeta.sqlite3
  python scripts/export_wayback_archive.py --dashboard live-dashboard.json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WAYBACK_SOURCES = ("wayback", "wayback-home", "wayback-api")
WAYBACK_TIMESTAMP = re.compile(r"web\.archive\.org/web/(\d{14})")


def iso_from_wayback_url(url: str | None) -> str | None:
    match = WAYBACK_TIMESTAMP.search(url or "")
    if not match:
        return None
    stamp = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"observations": [], "homepage": [], "plots": []}
    return json.loads(path.read_text(encoding="utf-8"))


def rows_from_db(db_path: Path) -> tuple[list[dict], list[dict], list[dict]]:
    db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in WAYBACK_SOURCES)
    observations = [
        {
            "plotId": row["plot_id"],
            "date": row["observed_date"],
            "observedAt": row["observed_at"],
            "source": row["source"],
            "chats": row["interaction_count"],
            "chatsWithRegen": row["interaction_with_regen"],
            "comments": row["comment_count"],
            "sourceUrl": row["source_url"],
        }
        for row in db.execute(
            f"""
            SELECT * FROM plot_observations
            WHERE source IN ({placeholders}) AND interaction_count IS NOT NULL
            """,
            WAYBACK_SOURCES,
        )
    ]
    homepage = [
        {
            "date": row["observed_date"],
            "observedAt": row["observed_at"],
            "observedPlots": row["observed_plots"],
            "totalChats": row["total_chats"],
            "totalChatsWithRegen": row["total_chats_with_regen"],
            "averageChatsPerPlot": row["average_chats_per_plot"],
            "medianChatsPerPlot": row["median_chats_per_plot"],
            "top10Chats": row["top10_chats"],
            "sourceUrl": row["source_url"],
        }
        for row in db.execute("SELECT * FROM homepage_observations")
    ]
    plot_ids = sorted({row["plotId"] for row in observations})
    plots: list[dict] = []
    for plot_id in plot_ids:
        row = db.execute("SELECT * FROM plots WHERE plot_id=?", (plot_id,)).fetchone()
        if row is None:
            continue
        tags = [
            value[0]
            for value in db.execute(
                "SELECT tag FROM plot_tags WHERE plot_id=? ORDER BY tag", (plot_id,)
            )
        ]
        plots.append(
            {
                "id": plot_id,
                "name": row["name"],
                "creator": row["creator_username"],
                "mode": row["mode"],
                "releasedAt": row["released_at"],
                "firstSeenAt": row["first_seen_at"],
                "tags": tags,
            }
        )
    db.close()
    return observations, homepage, plots


def rows_from_dashboard(path: Path) -> tuple[list[dict], list[dict]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    observations: list[dict] = []
    plots: list[dict] = []
    for plot in payload.get("plots", []):
        kept = False
        for point in plot.get("series", []):
            if point.get("source") not in WAYBACK_SOURCES or point.get("chats") is None:
                continue
            observed_at = point.get("observedAt") or iso_from_wayback_url(point.get("sourceUrl"))
            if observed_at is None:
                continue
            observations.append(
                {
                    "plotId": plot["id"],
                    "date": point["date"],
                    "observedAt": observed_at,
                    "source": point["source"],
                    "chats": point["chats"],
                    "chatsWithRegen": point.get("chatsWithRegen"),
                    "comments": point.get("comments"),
                    "sourceUrl": point.get("sourceUrl"),
                }
            )
            kept = True
        if kept:
            dates = sorted(point["date"] for point in plot.get("series", []) if point.get("date"))
            plots.append(
                {
                    "id": plot["id"],
                    "name": plot.get("name"),
                    "creator": plot.get("creator"),
                    "mode": plot.get("mode"),
                    "releasedAt": plot.get("releasedAt"),
                    "firstSeenAt": f"{dates[0]}T00:00:00Z" if dates else None,
                    "tags": plot.get("tags") or [],
                }
            )
    return observations, plots


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", action="append", default=[])
    parser.add_argument("--dashboard", action="append", default=[])
    parser.add_argument("--out", default=str(root / "archive" / "wayback-observations.json"))
    args = parser.parse_args()

    out = Path(args.out)
    existing = load_existing(out)
    observations = {
        (row["plotId"], row["date"], row["source"]): row
        for row in existing.get("observations", [])
    }
    homepage = {row["date"]: row for row in existing.get("homepage", [])}
    plots = {row["id"]: row for row in existing.get("plots", [])}

    added = {"observations": 0, "homepage": 0, "plots": 0}
    for db_path in args.db:
        db_observations, db_homepage, db_plots = rows_from_db(Path(db_path))
        for row in db_observations:
            key = (row["plotId"], row["date"], row["source"])
            if key not in observations:
                observations[key] = row
                added["observations"] += 1
        for row in db_homepage:
            if row["date"] not in homepage:
                homepage[row["date"]] = row
                added["homepage"] += 1
        for row in db_plots:
            if row["id"] not in plots:
                plots[row["id"]] = row
                added["plots"] += 1
    for dashboard_path in args.dashboard:
        dash_observations, dash_plots = rows_from_dashboard(Path(dashboard_path))
        for row in dash_observations:
            key = (row["plotId"], row["date"], row["source"])
            if key not in observations:
                observations[key] = row
                added["observations"] += 1
        for row in dash_plots:
            if row["id"] not in plots:
                plots[row["id"]] = row
                added["plots"] += 1

    payload = {
        "schemaVersion": 1,
        "description": (
            "Wayback 복원 원자료. 원천(wayback·wayback-home·wayback-api)별 모든 행과 "
            "홈 캡처 요약을 보존함. 수집기가 매 실행 전에 누락 행만 멱등 복원함."
        ),
        "exportedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": {
            "observations": len(observations),
            "bySource": {
                source: sum(1 for key in observations if key[2] == source)
                for source in WAYBACK_SOURCES
            },
            "homepage": len(homepage),
            "plots": len(plots),
        },
        "observations": sorted(
            observations.values(), key=lambda row: (row["date"], row["source"], row["plotId"])
        ),
        "homepage": sorted(homepage.values(), key=lambda row: row["date"]),
        "plots": sorted(plots.values(), key=lambda row: row["id"]),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"added": added, "counts": payload["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
