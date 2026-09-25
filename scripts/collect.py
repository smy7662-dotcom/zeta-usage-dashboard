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
PANEL_TARGET = 1_500
PANEL_VOLUME_TARGET = 1_000
PANEL_TAIL_TARGET = 500
HOT_REFRESH_TARGET = 500
ROTATION_REFRESH_TARGET = 1_000
REGEN_COHORT_MIN_MATCHED = {10: 3, 30: 5, 50: 8, 100: 10}


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

        CREATE TABLE IF NOT EXISTS measurement_panel (
          plot_id TEXT PRIMARY KEY,
          segment TEXT NOT NULL,
          selected_at TEXT NOT NULL,
          selected_date TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1,
          FOREIGN KEY (plot_id) REFERENCES plots(plot_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_observations_date
          ON plot_observations(observed_date);
        CREATE INDEX IF NOT EXISTS idx_observations_plot_date
          ON plot_observations(plot_id, observed_date);
        CREATE INDEX IF NOT EXISTS idx_plot_tags_tag
          ON plot_tags(tag, plot_id);
        CREATE INDEX IF NOT EXISTS idx_measurement_panel_active
          ON measurement_panel(active, segment, plot_id);
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
        "interaction_with_regen": with_regen,
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
          last_interaction_with_regen=excluded.last_interaction_with_regen
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


def ensure_measurement_panel(
    db: sqlite3.Connection,
    now: datetime,
    *,
    target_size: int = PANEL_TARGET,
    volume_target: int = PANEL_VOLUME_TARGET,
) -> int:
    """최초 한 번 고정 측정 패널을 만들고 이후 실행에서는 그대로 유지함."""
    existing = db.execute(
        "SELECT COUNT(*) FROM measurement_panel WHERE active=1"
    ).fetchone()[0]
    if existing or target_size <= 0:
        return existing

    selected_at, selected_date = iso_z(now), kst_day(now)
    volume_limit = min(target_size, max(0, volume_target))
    volume_rows = db.execute(
        """
        SELECT plot_id
        FROM plots
        WHERE last_interaction_count IS NOT NULL
        ORDER BY last_interaction_count DESC, plot_id
        LIMIT ?
        """,
        (volume_limit,),
    ).fetchall()
    for row in volume_rows:
        db.execute(
            """
            INSERT OR IGNORE INTO measurement_panel (
              plot_id, segment, selected_at, selected_date
            ) VALUES (?, 'volume', ?, ?)
            """,
            (row["plot_id"], selected_at, selected_date),
        )

    remaining = max(0, target_size - len(volume_rows))
    if remaining:
        candidates = db.execute(
            """
            SELECT p.plot_id, COALESCE(p.last_interaction_count, 0) AS chats
            FROM plots p
            WHERE NOT EXISTS (
              SELECT 1 FROM measurement_panel m WHERE m.plot_id=p.plot_id
            )
            ORDER BY p.plot_id
            """
        ).fetchall()
        buckets: dict[str, list[sqlite3.Row]] = {
            "mid": [],
            "long": [],
            "micro": [],
        }
        for row in candidates:
            if row["chats"] >= 100_000:
                buckets["mid"].append(row)
            elif row["chats"] >= 10_000:
                buckets["long"].append(row)
            else:
                buckets["micro"].append(row)

        indexes = {name: 0 for name in buckets}
        chosen: list[tuple[sqlite3.Row, str]] = []
        while len(chosen) < remaining:
            added = False
            for name in ("mid", "long", "micro"):
                index = indexes[name]
                if index >= len(buckets[name]):
                    continue
                chosen.append((buckets[name][index], name))
                indexes[name] += 1
                added = True
                if len(chosen) >= remaining:
                    break
            if not added:
                break
        for row, bucket in chosen:
            db.execute(
                """
                INSERT OR IGNORE INTO measurement_panel (
                  plot_id, segment, selected_at, selected_date
                ) VALUES (?, ?, ?, ?)
                """,
                (row["plot_id"], f"tail:{bucket}", selected_at, selected_date),
            )
    db.commit()
    return db.execute(
        "SELECT COUNT(*) FROM measurement_panel WHERE active=1"
    ).fetchone()[0]


def select_refresh_targets(
    db: sqlite3.Connection,
    now: datetime,
    *,
    limit: int,
    hot_limit: int = HOT_REFRESH_TARGET,
) -> list[str]:
    """고정 패널 → 신규·급상승 → 오래 미관측 순환 표본을 고름."""
    if limit <= 0:
        return []
    fixed_rows = db.execute(
        """
        SELECT p.plot_id
        FROM measurement_panel m
        JOIN plots p ON p.plot_id=m.plot_id
        WHERE m.active=1
        ORDER BY CASE WHEN m.segment='volume' THEN 0 ELSE 1 END,
                 COALESCE(p.last_interaction_count, 0) DESC,
                 p.plot_id
        """
    ).fetchall()
    targets = [row["plot_id"] for row in fixed_rows[:limit]]
    selected = set(targets)
    remaining_budget = limit - len(targets)
    if remaining_budget <= 0:
        return targets

    rows = db.execute(
        """
        SELECT p.plot_id, p.first_seen_at,
               COALESCE(p.last_interaction_count, 0) AS chats,
               COALESCE((
                 SELECT MAX(o.observed_date)
                 FROM plot_observations o
                 WHERE o.plot_id=p.plot_id AND o.source='detail'
               ), '') AS last_detail_date
        FROM plots p
        WHERE NOT EXISTS (
          SELECT 1 FROM measurement_panel m
          WHERE m.plot_id=p.plot_id AND m.active=1
        )
        """
    ).fetchall()
    recent_cutoff = iso_z(now - timedelta(days=2))
    hot_rows = sorted(
        rows,
        key=lambda row: (
            0 if row["chats"] >= WATCH_THRESHOLD else
            1 if row["first_seen_at"] >= recent_cutoff else 2,
            -row["chats"],
            row["plot_id"],
        ),
    )
    for row in hot_rows[: min(hot_limit, remaining_budget)]:
        targets.append(row["plot_id"])
        selected.add(row["plot_id"])
    remaining_budget = limit - len(targets)
    if remaining_budget <= 0:
        return targets

    rotation_rows = sorted(
        (row for row in rows if row["plot_id"] not in selected),
        key=lambda row: (
            row["last_detail_date"],
            -row["chats"],
            row["plot_id"],
        ),
    )
    targets.extend(row["plot_id"] for row in rotation_rows[:remaining_budget])
    return targets


def refresh_known_plots(
    db: sqlite3.Connection,
    now: datetime,
    *,
    limit: int,
    workers: int,
    errors: list[str],
) -> int:
    """고정 측정 패널·신규 급상승·순환 표본 순서로 상세 원값을 갱신함."""
    if limit <= 0:
        return 0
    observed_at, observed_date = iso_z(now), kst_day(now)
    ensure_measurement_panel(db, now)
    plot_ids = select_refresh_targets(db, now, limit=limit)

    def fetch(plot_id: str) -> tuple[str, str, Any]:
        url = f"{API}/v1/plots/{plot_id}"
        return plot_id, url, request_json(url, attempts=2)

    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch, plot_id): plot_id for plot_id in plot_ids}
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
        current["source"],
    )
    candidate_key = (
        candidate["observed_at"],
        candidate["comment_count"] is not None,
        candidate["source"] == "live",
        candidate["source"],
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


def build_activity_history(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """고정 패널의 연속 일일 상세 관측으로 채팅 활동 속도를 계산함."""
    panel_size = db.execute(
        "SELECT COUNT(*) FROM measurement_panel WHERE active=1"
    ).fetchone()[0]
    if not panel_size:
        return []
    rows = db.execute(
        """
        SELECT o.*
        FROM plot_observations o
        JOIN measurement_panel m ON m.plot_id=o.plot_id AND m.active=1
        WHERE o.source='detail'
          AND o.interaction_count IS NOT NULL
          AND o.observed_date >= m.selected_date
        ORDER BY o.observed_date, o.plot_id
        """
    ).fetchall()
    by_day: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
    for row in rows:
        by_day[row["observed_date"]][row["plot_id"]] = row

    history: list[dict[str, Any]] = []
    for day in sorted(by_day):
        previous_day = (
            datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=1)
        ).isoformat()
        current = by_day[day]
        previous = by_day.get(previous_day, {})
        matched_ids = current.keys() & previous.keys()
        raw_deltas = [
            current[plot_id]["interaction_count"]
            - previous[plot_id]["interaction_count"]
            for plot_id in matched_ids
        ]
        valid_deltas = [value for value in raw_deltas if value >= 0]
        positive = [value for value in valid_deltas if value > 0]
        current_values = [row["interaction_count"] for row in current.values()]
        total = sum(valid_deltas)
        top10 = sum(sorted(positive, reverse=True)[:10])
        point: dict[str, Any] = {
            "date": day,
            "panelSize": panel_size,
            "observedPanelPlots": len(current),
            "panelCoveragePct": round(len(current) / panel_size * 100, 1),
            "matchedPlots": len(matched_ids),
            "comparisonCoveragePct": round(len(matched_ids) / panel_size * 100, 1),
            "validDeltaPlots": len(valid_deltas),
            "negativeCorrections": len(raw_deltas) - len(valid_deltas),
            "panelTotalChats": sum(current_values) if current_values else None,
            "averageCumulativeChatsPerPlot": round(statistics.mean(current_values))
            if current_values else None,
            "medianCumulativeChatsPerPlot": round(statistics.median(current_values))
            if current_values else None,
            "totalNewChats": total if valid_deltas else None,
            "averageNewChatsPerPlot": round(statistics.mean(valid_deltas))
            if valid_deltas else None,
            "medianNewChatsPerPlot": round(statistics.median(valid_deltas))
            if valid_deltas else None,
            "activePlots": len(positive),
            "activeSharePct": round(len(positive) / len(valid_deltas) * 100, 1)
            if valid_deltas else None,
            "top10ContributionPct": round(top10 / total * 100, 1)
            if total else None,
            "sevenDayAverageNewChats": None,
            "previousSevenDayAverageNewChats": None,
            "accelerationPct": None,
            "breadthChangePp": None,
            "noiseBandPct": None,
            "direction": "collecting",
        }
        history.append(point)

        recent7 = history[-7:]
        recent7_ready = (
            len(recent7) == 7
            and (
                datetime.strptime(recent7[-1]["date"], "%Y-%m-%d").date()
                - datetime.strptime(recent7[0]["date"], "%Y-%m-%d").date()
            ).days == 6
            and all(
                item["totalNewChats"] is not None
                and item["comparisonCoveragePct"] >= 95
                for item in recent7
            )
        )
        if recent7_ready:
            point["sevenDayAverageNewChats"] = round(
                statistics.mean(item["totalNewChats"] for item in recent7)
            )

        recent14 = history[-14:]
        recent14_ready = (
            len(recent14) == 14
            and (
                datetime.strptime(recent14[-1]["date"], "%Y-%m-%d").date()
                - datetime.strptime(recent14[0]["date"], "%Y-%m-%d").date()
            ).days == 13
            and all(
                item["totalNewChats"] is not None
                and item["comparisonCoveragePct"] >= 95
                for item in recent14
            )
        )
        if recent14_ready:
            previous7 = recent14[:7]
            current7 = recent14[7:]
            current_average = round(
                statistics.mean(item["totalNewChats"] for item in current7)
            )
            previous_average = round(
                statistics.mean(item["totalNewChats"] for item in previous7)
            )
            point["sevenDayAverageNewChats"] = current_average
            point["previousSevenDayAverageNewChats"] = previous_average
            point["accelerationPct"] = round(
                (current_average / previous_average - 1) * 100, 1
            ) if previous_average else None
            current_breadth = statistics.mean(
                item["activeSharePct"] for item in current7
            )
            previous_breadth = statistics.mean(
                item["activeSharePct"] for item in previous7
            )
            point["breadthChangePp"] = round(current_breadth - previous_breadth, 1)
            point["direction"] = "provisional"

        if len(history) >= 28 and point["accelerationPct"] is not None:
            acceleration_values = [
                item["accelerationPct"]
                for item in history[-28:]
                if item["accelerationPct"] is not None
            ]
            if acceleration_values:
                noise = round(
                    statistics.median(abs(value) for value in acceleration_values),
                    1,
                )
                point["noiseBandPct"] = noise
                acceleration = point["accelerationPct"]
                if acceleration > noise:
                    point["direction"] = (
                        "broadAcceleration"
                        if (point["breadthChangePp"] or 0) >= 0
                        else "concentratedAcceleration"
                    )
                elif acceleration < -noise:
                    point["direction"] = "decelerating"
                else:
                    point["direction"] = "mixed"
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


def build_regeneration_cohorts(
    plot_payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build fixed latest-volume cohorts without inventing regeneration values.

    A date is usable only when at least one public observation still separates
    interactionCountWithRegen from interactionCount.  This prevents the API's
    later equal-valued fields from being rendered as a false 0% regeneration
    rate.  Each interval uses only the same plots at both endpoints.
    """
    observed_by_date: dict[str, int] = defaultdict(int)
    distinct_by_date: dict[str, int] = defaultdict(int)
    for plot in plot_payloads:
        for point in plot.get("series", []):
            chats = point.get("chats")
            with_regen = point.get("chatsWithRegen")
            if not isinstance(chats, int) or not isinstance(with_regen, int):
                continue
            day = point["date"]
            observed_by_date[day] += 1
            if with_regen > chats:
                distinct_by_date[day] += 1

    separable_dates = {
        day for day, count in distinct_by_date.items() if count > 0
    }
    latest_observed_date = max(observed_by_date, default=None)
    latest_separable_date = max(separable_dates, default=None)
    unseparable_from = None
    if latest_separable_date:
        unseparable_from = min(
            (
                day
                for day, count in observed_by_date.items()
                if day > latest_separable_date
                and count >= 10
                and distinct_by_date.get(day, 0) == 0
            ),
            default=None,
        )

    ranked = [
        plot for plot in plot_payloads if isinstance(plot.get("chats"), int)
    ]
    cohorts: list[dict[str, Any]] = []
    for requested_size, configured_minimum in REGEN_COHORT_MIN_MATCHED.items():
        members = ranked[:requested_size]
        member_series: list[dict[str, tuple[int, int]]] = []
        candidate_dates: set[str] = set()
        for member in members:
            values: dict[str, tuple[int, int]] = {}
            for point in member.get("series", []):
                day = point["date"]
                chats = point.get("chats")
                with_regen = point.get("chatsWithRegen")
                if (
                    day in separable_dates
                    and isinstance(chats, int)
                    and isinstance(with_regen, int)
                ):
                    values[day] = (chats, with_regen)
                    candidate_dates.add(day)
            member_series.append(values)

        minimum_matched = min(configured_minimum, len(members))
        dates = sorted(candidate_dates)
        history: list[dict[str, Any]] = []
        for end_index, end_date in enumerate(dates[1:], start=1):
            for start_date in reversed(dates[:end_index]):
                base_delta = 0
                with_regen_delta = 0
                regeneration_delta = 0
                matched = 0
                corrections = 0
                for values in member_series:
                    if start_date not in values or end_date not in values:
                        continue
                    start_chats, start_with_regen = values[start_date]
                    end_chats, end_with_regen = values[end_date]
                    plot_base_delta = end_chats - start_chats
                    plot_with_regen_delta = end_with_regen - start_with_regen
                    plot_regeneration_delta = (
                        plot_with_regen_delta - plot_base_delta
                    )
                    if (
                        plot_base_delta < 0
                        or plot_with_regen_delta <= 0
                        or plot_regeneration_delta < 0
                    ):
                        corrections += 1
                        continue
                    base_delta += plot_base_delta
                    with_regen_delta += plot_with_regen_delta
                    regeneration_delta += plot_regeneration_delta
                    matched += 1
                if matched < minimum_matched:
                    continue
                history.append(
                    {
                        "date": end_date,
                        "startDate": start_date,
                        "matchedPlots": matched,
                        "coveragePct": round(
                            matched / requested_size * 100, 1
                        ) if requested_size else None,
                        "baseDelta": base_delta,
                        "withRegenDelta": with_regen_delta,
                        "regenerationDelta": regeneration_delta,
                        "regenerationRatePct": round(
                            regeneration_delta / with_regen_delta * 100, 2
                        ),
                        "excludedCorrections": corrections,
                    }
                )
                break

        cohorts.append(
            {
                "size": requested_size,
                "availablePlots": len(members),
                "minimumMatchedPlots": minimum_matched,
                "cutoffChats": members[-1]["chats"] if members else None,
                "history": history,
            }
        )

    latest_observed_pairs = (
        observed_by_date.get(latest_observed_date, 0)
        if latest_observed_date
        else 0
    )
    latest_distinct_pairs = (
        distinct_by_date.get(latest_observed_date, 0)
        if latest_observed_date
        else 0
    )
    return {
        "cohortDefinition": "latestKnownCumulativeChatsFixed",
        "formula": "deltaRegen/deltaInteractionWithRegen",
        "latestObservedDate": latest_observed_date,
        "latestObservedPairs": latest_observed_pairs,
        "latestDistinctPairs": latest_distinct_pairs,
        "latestSeparableDate": latest_separable_date,
        "unseparableFrom": unseparable_from,
        "currentSeparable": bool(latest_distinct_pairs),
        "cohorts": cohorts,
    }


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
    regeneration_cohorts = build_regeneration_cohorts(plot_payloads)

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
    activity_history = build_activity_history(db)
    activity_panel_rows = db.execute(
        """
        SELECT segment, COUNT(*) AS member_count, MIN(selected_date) AS selected_date
        FROM measurement_panel
        WHERE active=1
        GROUP BY segment
        """
    ).fetchall()
    activity_panel_size = sum(row["member_count"] for row in activity_panel_rows)
    activity_selected_date = min(
        (row["selected_date"] for row in activity_panel_rows),
        default=None,
    )
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
        "activityPolicy": {
            "metric": "interactionCount",
            "label": "observedPublicChatActivityProxy",
            "panelTarget": PANEL_TARGET,
            "volumePanelTarget": PANEL_VOLUME_TARGET,
            "tailPanelTarget": PANEL_TAIL_TARGET,
            "hotRefreshTarget": HOT_REFRESH_TARGET,
            "rotationRefreshTarget": ROTATION_REFRESH_TARGET,
            "negativeDeltaRule": "excludedAndFlagged",
            "directionMinimumDays": 14,
            "noiseBandMinimumDays": 28,
        },
        "activityPanel": {
            "panelSize": activity_panel_size,
            "selectedDate": activity_selected_date,
            "segments": {
                row["segment"]: row["member_count"]
                for row in activity_panel_rows
            },
        },
        "activityLatest": activity_history[-1] if activity_history else None,
        "activityHistory": activity_history,
        "regenerationCohorts": regeneration_cohorts,
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
            "activityPanel": payload["activityPanel"],
            "activityLatest": payload["activityLatest"],
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
    parser.add_argument("--max-plot-refresh", type=int, default=3000)
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
