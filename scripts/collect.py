#!/usr/bin/env python3
"""제타 관심도 관측소 공개 데이터 수집기.

랭킹·디스커버리·추천을 시드로 삼고, 발견한 해시태그 검색을 재개 가능한
큐로 끝까지 순회한다. 모든 값은 플롯 ID로 중복 제거해 SQLite에 저장하고
공개 화면에는 작은 집계 JSON만 내보낸다.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

API = "https://api.zeta-ai.io"
USER_AGENT = "zeta-usage-observatory/1.0"
KST = timezone(timedelta(hours=9))
RANKING_TYPES = ("GLOBAL", "REALTIME", "DAILY", "WEEKLY", "MONTHLY")
CORE_THRESHOLD = 1_000_000
WATCH_THRESHOLD = 500_000


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kst_day(value: datetime) -> str:
    return value.astimezone(KST).strftime("%Y-%m-%d")


def request_json(url: str, *, attempts: int = 3) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # 네트워크 단기 오류만 재시도함
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    initialize(db)
    return db


def initialize(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS plots (
          plot_id TEXT PRIMARY KEY,
          name TEXT,
          creator_id TEXT,
          creator_username TEXT,
          mode TEXT,
          metric_kind TEXT NOT NULL DEFAULT 'interactionCount',
          released_at TEXT,
          updated_at TEXT,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          last_interaction_count INTEGER,
          last_interaction_with_regen INTEGER,
          last_comment_count INTEGER,
          comment_observed_at TEXT,
          wayback_checked_at TEXT
        );

        CREATE TABLE IF NOT EXISTS plot_tags (
          plot_id TEXT NOT NULL,
          tag TEXT NOT NULL,
          PRIMARY KEY (plot_id, tag),
          FOREIGN KEY (plot_id) REFERENCES plots(plot_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS tag_queue (
          tag TEXT PRIMARY KEY,
          next_cursor TEXT,
          complete INTEGER NOT NULL DEFAULT 0,
          reported_count INTEGER,
          pages_collected INTEGER NOT NULL DEFAULT 0,
          last_crawled_at TEXT,
          last_error TEXT
        );

        CREATE TABLE IF NOT EXISTS plot_observations (
          plot_id TEXT NOT NULL,
          observed_date TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          source TEXT NOT NULL,
          interaction_count INTEGER,
          interaction_with_regen INTEGER,
          comment_count INTEGER,
          source_url TEXT,
          PRIMARY KEY (plot_id, observed_date, source),
          FOREIGN KEY (plot_id) REFERENCES plots(plot_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS runs (
          run_id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          success INTEGER,
          plots_before INTEGER,
          plots_after INTEGER,
          tag_pages INTEGER NOT NULL DEFAULT 0,
          comment_plots INTEGER NOT NULL DEFAULT 0,
          errors_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS homepage_observations (
          observed_date TEXT PRIMARY KEY,
          observed_at TEXT NOT NULL,
          observed_plots INTEGER NOT NULL,
          total_chats INTEGER NOT NULL,
          total_chats_with_regen INTEGER,
          average_chats_per_plot INTEGER NOT NULL,
          median_chats_per_plot INTEGER NOT NULL,
          top10_chats INTEGER NOT NULL,
          source_url TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_observations_date
          ON plot_observations(observed_date);
        CREATE INDEX IF NOT EXISTS idx_observations_plot_date
          ON plot_observations(plot_id, observed_date);
        CREATE INDEX IF NOT EXISTS idx_plot_tags_tag
          ON plot_tags(tag, plot_id);
        """
    )
    db.commit()


def integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_plot(raw: dict[str, Any]) -> dict[str, Any] | None:
    plot_id = raw.get("id") or raw.get("plotId")
    name = raw.get("name") or raw.get("title")
    interaction = integer(raw.get("interactionCount"))
    metric_kind = "interactionCount"
    if interaction is None:
        interaction = integer(raw.get("playTurnCount"))
        metric_kind = "playTurnCount" if interaction is not None else "interactionCount"
    with_regen = integer(raw.get("interactionCountWithRegen"))
    if not plot_id or (name is None and interaction is None):
        return None
    creator = raw.get("creator") if isinstance(raw.get("creator"), dict) else {}
    tags = raw.get("hashtags") if isinstance(raw.get("hashtags"), list) else []
    return {
        "plot_id": str(plot_id),
        "name": str(name or "이름 없음"),
        "creator_id": creator.get("id") or raw.get("creatorId"),
        "creator_username": creator.get("username") or raw.get("creatorUsername"),
        "mode": raw.get("mode") or raw.get("type") or "ZETA",
        "metric_kind": metric_kind,
        "released_at": raw.get("releasedAt") or raw.get("createdAt"),
        "updated_at": raw.get("updatedAt"),
        "interaction_count": interaction,
        "interaction_with_regen": with_regen if with_regen is not None else interaction,
        "tags": [str(tag).strip() for tag in tags if str(tag).strip()],
    }


def iter_plot_candidates(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        normalized = normalize_plot(value)
        if normalized is not None:
            yield normalized
        for child in value.values():
            yield from iter_plot_candidates(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_plot_candidates(child)


def upsert_plot(
    db: sqlite3.Connection,
    plot: dict[str, Any],
    *,
    observed_at: str,
    observed_date: str,
    source: str,
    source_url: str,
) -> bool:
    existing = db.execute("SELECT 1 FROM plots WHERE plot_id=?", (plot["plot_id"],)).fetchone()
    db.execute(
        """
        INSERT INTO plots (
          plot_id, name, creator_id, creator_username, mode, metric_kind,
          released_at, updated_at, first_seen_at, last_seen_at,
          last_interaction_count, last_interaction_with_regen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(plot_id) DO UPDATE SET
          name=excluded.name,
          creator_id=COALESCE(excluded.creator_id, plots.creator_id),
          creator_username=COALESCE(excluded.creator_username, plots.creator_username),
          mode=COALESCE(excluded.mode, plots.mode),
          metric_kind=excluded.metric_kind,
          released_at=COALESCE(excluded.released_at, plots.released_at),
          updated_at=COALESCE(excluded.updated_at, plots.updated_at),
          last_seen_at=excluded.last_seen_at,
          last_interaction_count=COALESCE(excluded.last_interaction_count, plots.last_interaction_count),
          last_interaction_with_regen=COALESCE(excluded.last_interaction_with_regen, plots.last_interaction_with_regen)
        """,
        (
            plot["plot_id"],
            plot["name"],
            plot.get("creator_id"),
            plot.get("creator_username"),
            plot.get("mode"),
            plot.get("metric_kind", "interactionCount"),
            plot.get("released_at"),
            plot.get("updated_at"),
            observed_at,
            observed_at,
            plot.get("interaction_count"),
            plot.get("interaction_with_regen"),
        ),
    )
    if plot.get("interaction_count") is not None:
        db.execute(
            """
            INSERT INTO plot_observations (
              plot_id, observed_date, observed_at, source,
              interaction_count, interaction_with_regen, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plot_id, observed_date, source) DO UPDATE SET
              observed_at=excluded.observed_at,
              interaction_count=excluded.interaction_count,
              interaction_with_regen=excluded.interaction_with_regen,
              source_url=excluded.source_url
            """,
            (
                plot["plot_id"],
                observed_date,
                observed_at,
                source,
                plot.get("interaction_count"),
                plot.get("interaction_with_regen"),
                source_url,
            ),
        )
    for tag in plot.get("tags", []):
        normalized_tag = tag.casefold()
        db.execute(
            "INSERT OR IGNORE INTO plot_tags(plot_id, tag) VALUES (?, ?)",
            (plot["plot_id"], normalized_tag),
        )
        db.execute("INSERT OR IGNORE INTO tag_queue(tag) VALUES (?)", (normalized_tag,))
    return existing is None


def ingest_payload(
    db: sqlite3.Connection,
    payload: Any,
    *,
    observed_at: str,
    observed_date: str,
    source: str,
    source_url: str,
) -> tuple[int, int]:
    seen: set[str] = set()
    found = new = 0
    for plot in iter_plot_candidates(payload):
        if plot["plot_id"] in seen:
            continue
        seen.add(plot["plot_id"])
        found += 1
        new += int(
            upsert_plot(
                db,
                plot,
                observed_at=observed_at,
                observed_date=observed_date,
                source=source,
                source_url=source_url,
            )
        )
    return found, new


def collect_seeds(db: sqlite3.Connection, now: datetime, errors: list[str], infinite_pages: int) -> None:
    observed_at, observed_date = iso_z(now), kst_day(now)
    for ranking_type in RANKING_TYPES:
        url = f"{API}/v1/plots/ranking?type={ranking_type}&limit=100&gender=ALL"
        try:
            payload = request_json(url)
            ingest_payload(
                db,
                payload,
                observed_at=observed_at,
                observed_date=observed_date,
                source=f"ranking:{ranking_type.lower()}",
                source_url=url,
            )
        except Exception as exc:
            errors.append(f"ranking {ranking_type}: {exc}")

    discovery_url = f"{API}/v1/discovery-tab"
    try:
        payload = request_json(discovery_url)
        ingest_payload(
            db,
            payload,
            observed_at=observed_at,
            observed_date=observed_date,
            source="discovery",
            source_url=discovery_url,
        )
    except Exception as exc:
        errors.append(f"discovery: {exc}")

    cursor: str | None = None
    for page in range(infinite_pages):
        params = {"limit": "16"}
        if cursor:
            params["cursor"] = cursor
        url = f"{API}/v1/infinite-plots?{urllib.parse.urlencode(params)}"
        try:
            payload = request_json(url)
            found, _ = ingest_payload(
                db,
                payload,
                observed_at=observed_at,
                observed_date=observed_date,
                source="recommend",
                source_url=url,
            )
            cursor = payload.get("nextCursor")
            if not cursor or found == 0:
                break
        except Exception as exc:
            errors.append(f"recommend page {page + 1}: {exc}")
            break
    db.commit()


def crawl_tag_queue(
    db: sqlite3.Connection,
    now: datetime,
    *,
    max_pages: int,
    errors: list[str],
) -> int:
    observed_at, observed_date = iso_z(now), kst_day(now)
    pages = 0
    while pages < max_pages:
        row = db.execute(
            """
            SELECT tag, next_cursor, reported_count, pages_collected
            FROM tag_queue
            WHERE complete=0
            ORDER BY COALESCE(last_crawled_at, ''), pages_collected, tag
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            break
        tag = row["tag"]
        params = {"keyword": f"#{tag}", "limit": "50", "order": "LATEST"}
        if row["next_cursor"]:
            params["cursor"] = row["next_cursor"]
        url = f"{API}/v2/plots/search?{urllib.parse.urlencode(params)}"
        try:
            payload = request_json(url)
            found, _ = ingest_payload(
                db,
                payload,
                observed_at=observed_at,
                observed_date=observed_date,
                source=f"tag:{tag}",
                source_url=url,
            )
            reported_count = row["reported_count"]
            if reported_count is None:
                count_url = (
                    f"{API}/v2/plots/search/plot-count?"
                    + urllib.parse.urlencode({"keyword": f"#{tag}"})
                )
                try:
                    reported_count = integer(request_json(count_url).get("count"))
                except Exception as exc:
                    errors.append(f"tag count #{tag}: {exc}")
            next_cursor = payload.get("nextCursor")
            complete = int(not next_cursor or found == 0)
            db.execute(
                """
                UPDATE tag_queue SET
                  next_cursor=?, complete=?, reported_count=?,
                  pages_collected=pages_collected+1,
                  last_crawled_at=?, last_error=NULL
                WHERE tag=?
                """,
                (next_cursor, complete, reported_count, observed_at, tag),
            )
            db.commit()
            pages += 1
        except Exception as exc:
            message = str(exc)
            db.execute(
                "UPDATE tag_queue SET last_crawled_at=?, last_error=? WHERE tag=?",
                (observed_at, message[:500], tag),
            )
            db.commit()
            errors.append(f"tag #{tag}: {message}")
            pages += 1
    return pages


def collect_comments(
    db: sqlite3.Connection,
    now: datetime,
    *,
    limit: int,
    errors: list[str],
) -> int:
    observed_at, observed_date = iso_z(now), kst_day(now)
    rows = db.execute(
        """
        SELECT plot_id, last_interaction_count, last_interaction_with_regen
        FROM plots
        ORDER BY COALESCE(comment_observed_at, ''),
                 COALESCE(last_interaction_count, 0) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    completed = 0
    for row in rows:
        plot_id = row["plot_id"]
        url = f"{API}/v1/plots/{plot_id}/comments/count"
        try:
            count = integer(request_json(url).get("count"))
            db.execute(
                """
                INSERT INTO plot_observations (
                  plot_id, observed_date, observed_at, source,
                  interaction_count, interaction_with_regen,
                  comment_count, source_url
                ) VALUES (?, ?, ?, 'live', ?, ?, ?, ?)
                ON CONFLICT(plot_id, observed_date, source) DO UPDATE SET
                  observed_at=excluded.observed_at,
                  interaction_count=COALESCE(excluded.interaction_count, plot_observations.interaction_count),
                  interaction_with_regen=COALESCE(excluded.interaction_with_regen, plot_observations.interaction_with_regen),
                  comment_count=excluded.comment_count,
                  source_url=excluded.source_url
                """,
                (
                    plot_id,
                    observed_date,
                    observed_at,
                    row["last_interaction_count"],
                    row["last_interaction_with_regen"],
                    count,
                    url,
                ),
            )
            db.execute(
                "UPDATE plots SET last_comment_count=?, comment_observed_at=? WHERE plot_id=?",
                (count, observed_at, plot_id),
            )
            db.commit()
            completed += 1
        except Exception as exc:
            errors.append(f"comments {plot_id}: {exc}")
    return completed


def refresh_known_plots(
    db: sqlite3.Connection,
    now: datetime,
    *,
    limit: int,
    workers: int,
    errors: list[str],
) -> int:
    """핵심·후보 집단을 먼저, 나머지는 오래 미관측한 순서로 갱신함."""
    if limit <= 0:
        return 0
    observed_at, observed_date = iso_z(now), kst_day(now)
    rows = db.execute(
        """
        SELECT p.plot_id
        FROM plots p
        ORDER BY CASE
                   WHEN COALESCE(p.last_interaction_count, 0) >= ? THEN 0
                   WHEN COALESCE(p.last_interaction_count, 0) >= ? THEN 1
                   ELSE 2
                 END,
                 EXISTS (
                   SELECT 1 FROM plot_observations h
                   WHERE h.plot_id=p.plot_id AND h.source LIKE 'wayback%'
                 ) DESC,
                 p.last_seen_at,
                 COALESCE(p.last_interaction_count, 0) DESC
        LIMIT ?
        """,
        (CORE_THRESHOLD, WATCH_THRESHOLD, limit),
    ).fetchall()

    def fetch(plot_id: str) -> tuple[str, str, Any]:
        url = f"{API}/v1/plots/{plot_id}"
        return plot_id, url, request_json(url, attempts=2)

    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch, row["plot_id"]): row["plot_id"] for row in rows}
        for future in as_completed(futures):
            plot_id = futures[future]
            try:
                _, url, payload = future.result()
                plot = normalize_plot(payload)
                if plot is None:
                    errors.append(f"plot {plot_id}: detail response had no plot")
                    continue
                upsert_plot(
                    db,
                    plot,
                    observed_at=observed_at,
                    observed_date=observed_date,
                    source="detail",
                    source_url=url,
                )
                completed += 1
                if completed % 50 == 0:
                    db.commit()
            except Exception as exc:
                errors.append(f"plot {plot_id}: {exc}")
    db.commit()
    return completed


def latest_plot_rows(db: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = db.execute(
        """
        WITH ranked AS (
          SELECT o.*,
            ROW_NUMBER() OVER (
              PARTITION BY o.plot_id
              ORDER BY o.observed_at DESC,
                       (o.comment_count IS NOT NULL) DESC,
                       (o.source = 'live') DESC,
                       o.source DESC
            ) AS position
          FROM plot_observations o
        )
        SELECT ranked.*, p.name, p.creator_username, p.mode, p.released_at,
               p.last_comment_count AS latest_comment_count,
               p.comment_observed_at AS latest_comment_observed_at
        FROM ranked
        JOIN plots p ON p.plot_id=ranked.plot_id
        WHERE ranked.position=1
        """
    ).fetchall()
    return {row["plot_id"]: row for row in rows}


def prefer_observation(current: sqlite3.Row | None, candidate: sqlite3.Row) -> sqlite3.Row:
    """같은 날 여러 출처가 있으면 최신·댓글 포함 관측을 대표값으로 고름."""
    if current is None:
        return candidate
    current_key = (
        current["observed_at"],
        current["comment_count"] is not None,
        current["source"] == "live",
    )
    candidate_key = (
        candidate["observed_at"],
        candidate["comment_count"] is not None,
        candidate["source"] == "live",
    )
    return candidate if candidate_key > current_key else current


def previous_values(
    observations: list[sqlite3.Row], current_date: str, days: int, tolerance_days: int = 2
) -> sqlite3.Row | None:
    target = datetime.strptime(current_date, "%Y-%m-%d").date() - timedelta(days=days)
    eligible = [row for row in observations if row["observed_date"] <= target.isoformat()]
    if not eligible:
        return None
    candidate = max(eligible, key=lambda row: row["observed_date"])
    candidate_day = datetime.strptime(candidate["observed_date"], "%Y-%m-%d").date()
    return candidate if (target - candidate_day).days <= tolerance_days else None


def change(current: int | None, previous: int | None) -> int | None:
    if current is None or previous is None:
        return None
    return current - previous


def build_homepage_history(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """홈 표본과 동일 플롯 매칭 성장지수를 함께 반환함.

    각 날짜는 가장 가까운 이전 캡처 중 동일 플롯이 10개 이상인 날짜와
    비교함. 개별 플롯 대화량 배수의 중앙값을 직전 지수에 연결하므로,
    홈 노출 플롯 수가 바뀌어 생기는 합계 왜곡을 줄임.
    """
    rows = db.execute(
        "SELECT * FROM homepage_observations ORDER BY observed_date"
    ).fetchall()
    plots_by_day: dict[str, dict[str, int]] = defaultdict(dict)
    for row in db.execute(
        """
        SELECT observed_date, plot_id, interaction_count
        FROM plot_observations
        WHERE source='wayback-home' AND interaction_count IS NOT NULL
        ORDER BY observed_date
        """
    ).fetchall():
        plots_by_day[row["observed_date"]][row["plot_id"]] = row["interaction_count"]

    history: list[dict[str, Any]] = []
    index_by_day: dict[str, float] = {}
    for row in rows:
        day = row["observed_date"]
        matched_index = 100.0 if not index_by_day else None
        matched_plots = None
        comparison_date = None
        current = plots_by_day.get(day, {})
        if index_by_day:
            for previous in reversed(history):
                previous_day = previous["date"]
                if previous_day not in index_by_day:
                    continue
                prior = plots_by_day.get(previous_day, {})
                overlap = [
                    plot_id
                    for plot_id in current.keys() & prior.keys()
                    if prior[plot_id] > 0
                ]
                if len(overlap) < 10:
                    continue
                ratio = statistics.median(
                    current[plot_id] / prior[plot_id] for plot_id in overlap
                )
                matched_index = index_by_day[previous_day] * ratio
                matched_plots = len(overlap)
                comparison_date = previous_day
                break
        if matched_index is not None:
            index_by_day[day] = matched_index
        history.append(
            {
                "date": day,
                "observedPlots": row["observed_plots"],
                "totalChats": row["total_chats"],
                "totalChatsWithRegen": row["total_chats_with_regen"],
                "averageChatsPerPlot": row["average_chats_per_plot"],
                "medianChatsPerPlot": row["median_chats_per_plot"],
                "top10Chats": row["top10_chats"],
                "matchedGrowthIndex": round(matched_index, 1) if matched_index is not None else None,
                "matchedPlots": matched_plots,
                "comparisonDate": comparison_date,
                "sourceUrl": row["source_url"],
            }
        )
    return history


def build_matched_growth_history(
    db: sqlite3.Connection, homepage_history: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Wayback 홈 동일 플롯 지수에 최신 공개 API 관측점을 연결함."""
    history = [
        {
            "date": point["date"],
            "observedPlots": point["observedPlots"],
            "matchedGrowthIndex": point["matchedGrowthIndex"],
            "matchedPlots": point["matchedPlots"],
            "comparisonDate": point["comparisonDate"],
            "sourceUrl": point["sourceUrl"],
            "isCurrent": False,
        }
        for point in homepage_history
        if point["matchedGrowthIndex"] is not None
    ]
    if not history:
        return history

    latest_row = db.execute(
        """
        SELECT MAX(observed_date)
        FROM plot_observations
        WHERE source NOT LIKE 'wayback%' AND interaction_count IS NOT NULL
        """
    ).fetchone()
    latest_date = latest_row[0] if latest_row else None
    if not latest_date or latest_date <= history[-1]["date"]:
        return history

    current_rows: dict[str, sqlite3.Row] = {}
    for row in db.execute(
        """
        SELECT * FROM plot_observations
        WHERE observed_date=? AND source NOT LIKE 'wayback%'
          AND interaction_count IS NOT NULL
        ORDER BY observed_at
        """,
        (latest_date,),
    ).fetchall():
        current_rows[row["plot_id"]] = prefer_observation(
            current_rows.get(row["plot_id"]), row
        )
    current = {
        plot_id: row["interaction_count"] for plot_id, row in current_rows.items()
    }

    for previous in reversed(history):
        prior_rows = db.execute(
            """
            SELECT plot_id, interaction_count
            FROM plot_observations
            WHERE observed_date=? AND source='wayback-home'
              AND interaction_count IS NOT NULL
            """,
            (previous["date"],),
        ).fetchall()
        prior = {row["plot_id"]: row["interaction_count"] for row in prior_rows}
        overlap = [
            plot_id
            for plot_id in current.keys() & prior.keys()
            if prior[plot_id] > 0
        ]
        if len(overlap) < 10:
            continue
        ratio = statistics.median(
            current[plot_id] / prior[plot_id] for plot_id in overlap
        )
        history.append(
            {
                "date": latest_date,
                "observedPlots": len(current),
                "matchedGrowthIndex": round(
                    previous["matchedGrowthIndex"] * ratio, 1
                ),
                "matchedPlots": len(overlap),
                "comparisonDate": previous["date"],
                "sourceUrl": (
                    "https://api.zeta-ai.io/v1/plots/ranking"
                    "?type=GLOBAL&limit=100&gender=ALL"
                ),
                "isCurrent": True,
            }
        )
        break
    return history


def build_core_history(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """현재 API 관측만으로 100만+ 핵심 플롯 재고의 날짜별 스냅샷을 만듦.

    각 날짜 값은 그날까지 확보한 플롯별 최신 원값을 한 번씩만 사용함. 당일에
    실제로 다시 읽은 핵심 플롯 비율을 함께 내보내 오래된 값이 섞인 정도를 숨기지
    않으며, 기존 관측값이 100만 미만이었다가 넘은 플롯과 처음 발견할 때부터
    100만 이상이었던 플롯을 분리함.
    """
    rows = db.execute(
        """
        SELECT *
        FROM plot_observations
        WHERE source NOT LIKE 'wayback%'
          AND interaction_count IS NOT NULL
        ORDER BY observed_date, observed_at
        """
    ).fetchall()
    by_day: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
    for row in rows:
        current = by_day[row["observed_date"]].get(row["plot_id"])
        by_day[row["observed_date"]][row["plot_id"]] = prefer_observation(
            current, row
        )

    snapshot: dict[str, sqlite3.Row] = {}
    previous_core: set[str] = set()
    history: list[dict[str, Any]] = []
    for day in sorted(by_day):
        previous_values_by_plot = {
            plot_id: row["interaction_count"] for plot_id, row in snapshot.items()
        }
        snapshot.update(by_day[day])
        core_ids = {
            plot_id
            for plot_id, row in snapshot.items()
            if row["interaction_count"] >= CORE_THRESHOLD
        }
        watch_ids = {
            plot_id
            for plot_id, row in snapshot.items()
            if WATCH_THRESHOLD <= row["interaction_count"] < CORE_THRESHOLD
        }
        entrants = core_ids - previous_core
        crossed = sum(
            plot_id in previous_values_by_plot
            and previous_values_by_plot[plot_id] < CORE_THRESHOLD
            for plot_id in entrants
        )
        discovered = sum(plot_id not in previous_values_by_plot for plot_id in entrants)
        refreshed_core = sum(
            snapshot[plot_id]["observed_date"] == day for plot_id in core_ids
        )
        core_count = len(core_ids)
        history.append(
            {
                "date": day,
                "corePlotCount": core_count,
                "coreTotalChats": sum(
                    snapshot[plot_id]["interaction_count"] for plot_id in core_ids
                ),
                "watchPlotCount": len(watch_ids),
                "watchTotalChats": sum(
                    snapshot[plot_id]["interaction_count"] for plot_id in watch_ids
                ),
                "observedPlots": len(by_day[day]),
                "refreshedCorePlots": refreshed_core,
                "coreCoveragePct": round(
                    refreshed_core / core_count * 100, 1
                ) if core_count else None,
                "crossedCoreCount": crossed,
                "discoveredCoreCount": discovered,
            }
        )
        previous_core = core_ids
    return history


def build_dashboard(db: sqlite3.Connection, out_dir: Path, errors: list[str]) -> dict[str, Any]:
    known_plots = db.execute("SELECT COUNT(*) FROM plots").fetchone()[0]
    known_tags = db.execute("SELECT COUNT(*) FROM tag_queue").fetchone()[0]
    completed_tags = db.execute("SELECT COUNT(*) FROM tag_queue WHERE complete=1").fetchone()[0]
    pending_tags = known_tags - completed_tags
    latest = latest_plot_rows(db)

    all_observations = db.execute(
        "SELECT * FROM plot_observations ORDER BY observed_date, observed_at"
    ).fetchall()
    by_plot_days: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
    by_date: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
    for row in all_observations:
        plot_day = by_plot_days[row["plot_id"]].get(row["observed_date"])
        by_plot_days[row["plot_id"]][row["observed_date"]] = prefer_observation(
            plot_day, row
        )
        current = by_date[row["observed_date"]].get(row["plot_id"])
        by_date[row["observed_date"]][row["plot_id"]] = prefer_observation(current, row)
    by_plot: dict[str, list[sqlite3.Row]] = {
        plot_id: [days[day] for day in sorted(days)]
        for plot_id, days in by_plot_days.items()
    }

    platform_history: list[dict[str, Any]] = []
    for day in sorted(by_date):
        rows = list(by_date[day].values())
        chats = [row["interaction_count"] for row in rows if row["interaction_count"] is not None]
        regens = [
            row["interaction_with_regen"]
            for row in rows
            if row["interaction_with_regen"] is not None
        ]
        comments = [row["comment_count"] for row in rows if row["comment_count"] is not None]
        platform_history.append(
            {
                "date": day,
                "observedPlots": len(chats),
                "totalChats": sum(chats) if chats else None,
                "totalChatsWithRegen": sum(regens) if regens else None,
                "averageChatsPerPlot": round(statistics.mean(chats)) if chats else None,
                "medianChatsPerPlot": round(statistics.median(chats)) if chats else None,
                "totalComments": sum(comments) if comments else None,
                "commentCoverage": len(comments),
            }
        )

    latest_date = max((row["observed_date"] for row in latest.values()), default=None)
    current_latest = {
        plot_id: row
        for plot_id, row in latest.items()
        if row["observed_date"] == latest_date
    }
    plot_payloads: list[dict[str, Any]] = []
    deltas: dict[str, dict[int, int | None]] = {}
    for plot_id, row in latest.items():
        series = by_plot[plot_id]
        periods: dict[str, int | None] = {}
        deltas[plot_id] = {}
        for days in (7, 30, 60):
            previous = previous_values(series, row["observed_date"], days)
            delta = change(
                row["interaction_count"],
                previous["interaction_count"] if previous else None,
            )
            periods[str(days)] = delta
            deltas[plot_id][days] = delta
        tags = [
            value[0]
            for value in db.execute(
                "SELECT tag FROM plot_tags WHERE plot_id=? ORDER BY tag", (plot_id,)
            ).fetchall()
        ]
        plot_payloads.append(
            {
                "id": plot_id,
                "name": row["name"],
                "creator": row["creator_username"],
                "mode": row["mode"],
                "releasedAt": row["released_at"],
                "tags": tags,
                "chats": row["interaction_count"],
                "chatsWithRegen": row["interaction_with_regen"],
                "comments": row["latest_comment_count"],
                "commentsObservedAt": row["latest_comment_observed_at"],
                "changes": periods,
                "series": [
                    {
                        "date": item["observed_date"],
                        "chats": item["interaction_count"],
                        "chatsWithRegen": item["interaction_with_regen"],
                        "comments": item["comment_count"],
                        "source": item["source"],
                        "sourceUrl": item["source_url"],
                    }
                    for item in series
                ],
            }
        )
    plot_payloads.sort(key=lambda row: row["chats"] or -1, reverse=True)

    tag_stats: list[dict[str, Any]] = []
    tag_members: dict[str, list[str]] = defaultdict(list)
    for row in db.execute("SELECT tag, plot_id FROM plot_tags"):
        tag_members[row["tag"]].append(row["plot_id"])
    for tag, members in tag_members.items():
        current_rows = [
            current_latest[plot_id]
            for plot_id in members
            if plot_id in current_latest
        ]
        current_chats = [row["interaction_count"] for row in current_rows if row["interaction_count"] is not None]
        current_regens = [
            row["interaction_with_regen"]
            for row in current_rows
            if row["interaction_with_regen"] is not None
        ]
        current_comments = [
            row["latest_comment_count"]
            for row in current_rows
            if row["latest_comment_count"] is not None
        ]
        changes: dict[str, int | None] = {}
        for days in (7, 30, 60):
            values = [deltas[plot_id][days] for plot_id in members if plot_id in deltas]
            usable = [value for value in values if value is not None]
            changes[str(days)] = sum(usable) if usable else None
        queue_row = db.execute(
            "SELECT reported_count, complete FROM tag_queue WHERE tag=?", (tag,)
        ).fetchone()
        tag_stats.append(
            {
                "tag": tag,
                "observedPlots": len(current_rows),
                "reportedPlots": queue_row["reported_count"] if queue_row else None,
                "complete": bool(queue_row["complete"]) if queue_row else False,
                "totalChats": sum(current_chats) if current_chats else None,
                "totalChatsWithRegen": sum(current_regens) if current_regens else None,
                "totalComments": sum(current_comments) if current_comments else None,
                "changes": changes,
                "memberIds": members,
            }
        )
    tag_stats.sort(key=lambda row: row["totalChats"] or -1, reverse=True)

    latest_comments = [
        row["latest_comment_count"]
        for row in current_latest.values()
        if row["latest_comment_count"] is not None
    ]
    homepage_history = build_homepage_history(db)
    core_history = build_core_history(db)
    core_inventory = core_history[-1] if core_history else None
    payload = {
        "schemaVersion": 1,
        "updatedAt": iso_z(utc_now()),
        "latestDate": latest_date,
        "coverage": {
            "knownPlots": known_plots,
            "knownTags": known_tags,
            "completedTags": completed_tags,
            "pendingTags": pending_tags,
            "plotsWithCurrentValues": len(current_latest),
            "plotsWithComments": len(latest_comments),
            "plotsWithHistory": sum(
                1
                for series in by_plot.values()
                if sum(row["interaction_count"] is not None for row in series) >= 2
            ),
            "waybackApiObservations": db.execute(
                "SELECT COUNT(*) FROM plot_observations WHERE source='wayback-api'"
            ).fetchone()[0],
        },
        "platformHistory": platform_history,
        "corePolicy": {
            "metric": "interactionCount",
            "coreThreshold": CORE_THRESHOLD,
            "watchThreshold": WATCH_THRESHOLD,
            "definition": "latestKnownInventory",
        },
        "coreInventory": core_inventory,
        "coreHistory": core_history,
        "homepageHistory": homepage_history,
        "matchedGrowthHistory": build_matched_growth_history(db, homepage_history),
        "plots": plot_payloads,
        "tags": tag_stats,
        "errors": errors[-50:],
        "policy": {
            "primaryMetric": "interactionCount",
            "missingDays": "notInterpolated",
            "markerRule": "markersOnlyOnObservedDates",
            "likes": "omittedUntilStableAnonymousCollection",
        },
    }
    write_json(out_dir / "dashboard.json", payload)
    write_json(
        out_dir / "status.json",
        {
            "updatedAt": payload["updatedAt"],
            "coverage": payload["coverage"],
            "errors": payload["errors"],
        },
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--db", default=str(root / "data" / "zeta.sqlite3"))
    parser.add_argument("--out", default=str(root / "public" / "data"))
    parser.add_argument("--max-tag-pages", type=int, default=120)
    parser.add_argument("--max-comment-plots", type=int, default=250)
    parser.add_argument("--max-plot-refresh", type=int, default=1500)
    parser.add_argument("--refresh-workers", type=int, default=10)
    parser.add_argument("--infinite-pages", type=int, default=8)
    args = parser.parse_args()

    now = utc_now()
    db = connect(Path(args.db))
    before = db.execute("SELECT COUNT(*) FROM plots").fetchone()[0]
    run_id = db.execute(
        "INSERT INTO runs(started_at, plots_before) VALUES (?, ?)",
        (iso_z(now), before),
    ).lastrowid
    db.commit()
    errors: list[str] = []
    try:
        collect_seeds(db, now, errors, args.infinite_pages)
        tag_pages = crawl_tag_queue(db, now, max_pages=args.max_tag_pages, errors=errors)
        refresh_known_plots(
            db,
            now,
            limit=args.max_plot_refresh,
            workers=args.refresh_workers,
            errors=errors,
        )
        comment_plots = collect_comments(
            db, now, limit=args.max_comment_plots, errors=errors
        )
        payload = build_dashboard(db, Path(args.out), errors)
        after = db.execute("SELECT COUNT(*) FROM plots").fetchone()[0]
        db.execute(
            """
            UPDATE runs SET finished_at=?, success=1, plots_after=?,
              tag_pages=?, comment_plots=?, errors_json=?
            WHERE run_id=?
            """,
            (
                iso_z(utc_now()),
                after,
                tag_pages,
                comment_plots,
                json.dumps(errors, ensure_ascii=False),
                run_id,
            ),
        )
        db.commit()
        print(json.dumps(payload["coverage"], ensure_ascii=False, indent=2))
        if errors:
            print(json.dumps({"errors": errors[-10:]}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        errors.append(f"fatal: {exc}")
        db.execute(
            "UPDATE runs SET finished_at=?, success=0, errors_json=? WHERE run_id=?",
            (iso_z(utc_now()), json.dumps(errors, ensure_ascii=False), run_id),
        )
        db.commit()
        build_dashboard(db, Path(args.out), errors)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
