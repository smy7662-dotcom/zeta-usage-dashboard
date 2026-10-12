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
import urllib.error
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
RANKING_TYPES = ("GLOBAL", "REALTIME", "DAILY", "WEEKLY", "MONTHLY", "TRENDING")
CORE_THRESHOLD = 1_000_000
WATCH_THRESHOLD = 500_000
PANEL_TARGET = 1_500
PANEL_VOLUME_TARGET = 1_000
PANEL_TAIL_TARGET = 500
HOT_REFRESH_TARGET = 500
ROTATION_REFRESH_TARGET = 1_000
NEW_PLOT_DAYS = 7
REGEN_COHORT_MIN_MATCHED = {10: 3, 30: 5, 50: 8, 100: 10}
# 24시간 ± 이 값을 벗어난 관측 간격은 일일 속도·가속 계산에서 제외함
TIMING_TOLERANCE_HOURS = 2.0
# 성장지수의 현재 연결점에 쓰는 최근 Wayback 홈 표본 날짜 수와 최소 재조회 비율
WAYBACK_COHORT_DATES = 3
CURRENT_MATCH_MIN_SHARE = 0.6
WAYBACK_SOURCES = ("wayback", "wayback-home", "wayback-api")
EXTERNAL_HISTORY_SOURCES = ("external:otoscor-zeta",)
# 제타 공개 API는 X-User-Language 헤더로 시장(한국·일본·영어)을 가름. 헤더가 없으면
# 한국 시장을 주고, 다른 시장 플롯의 상세 조회는 404가 남(2026-10-01 확인).
MARKETS = ("KOREAN", "JAPANESE", "ENGLISH")
MARKET_LANGUAGE = "KOREAN"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_z(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def kst_day(value: datetime) -> str:
    return value.astimezone(KST).strftime("%Y-%m-%d")


def set_market(market: str) -> None:
    global MARKET_LANGUAGE
    if market not in MARKETS:
        raise ValueError(f"unknown market: {market}")
    MARKET_LANGUAGE = market


def request_json(url: str, *, attempts: int = 3) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "X-User-Language": MARKET_LANGUAGE,
        },
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 404 같은 요청 오류는 다시 물어도 같으므로 바로 넘김. 429·5xx만 재시도함
            if 400 <= exc.code < 500 and exc.code != 429:
                raise
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
        except Exception as exc:  # 네트워크 단기 오류만 재시도함
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def ranking_url(ranking_type: str, *, limit: int = 100) -> str:
    """랭킹 종류별 실제 공개 API URL을 반환함.

    TRENDING은 일반 랭킹과 파라미터 계약이 달라 gender=ALL을 넣으면 빈 배열을
    반환한다. 외부 과거 수집기와 같은 장르 필터를 명시해 직접 이어 받는다.
    """
    ranking_type = ranking_type.upper()
    if ranking_type == "TRENDING":
        params = {
            "type": ranking_type,
            "limit": str(limit),
            "genres": "ALL",
            "filterType": "GENRE",
            "filterValues": "all",
        }
    else:
        params = {"type": ranking_type, "limit": str(limit), "gender": "ALL"}
    return f"{API}/v1/plots/ranking?{urllib.parse.urlencode(params)}"


def write_json(path: Path, payload: Any, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    text = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if compact
        else json.dumps(payload, ensure_ascii=False, indent=2)
    )
    temp.write_text(text + "\n", encoding="utf-8")
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
    columns = {row[1] for row in db.execute("PRAGMA table_info(plots)")}
    for column in (
        "missing_since",
        "missing_date",
        "missing_checked_at",
        "wayback_attempted_at",
    ):
        if column not in columns:
            db.execute(f"ALTER TABLE plots ADD COLUMN {column} TEXT")
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


def import_published_history(db: sqlite3.Connection, dashboard_path: Path) -> int:
    """Seed archived observations from the checked-in public snapshot.

    GitHub Actions restores the live SQLite cache independently from Git.  The
    checked-in dashboard contains manually recovered Wayback observations that
    must survive cache replacement, so import only missing observation rows and
    never overwrite fresher database values.
    """
    if not dashboard_path.exists():
        return 0
    try:
        payload = json.loads(dashboard_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0

    imported = 0
    for plot in payload.get("plots", []):
        plot_id = plot.get("id")
        series = plot.get("series") if isinstance(plot.get("series"), list) else []
        dates = sorted(
            point.get("date")
            for point in series
            if isinstance(point, dict) and point.get("date")
        )
        if not plot_id or not dates:
            continue
        first_seen_at = f"{dates[0]}T00:00:00Z"
        last_seen_at = f"{dates[-1]}T00:00:00Z"
        db.execute(
            """
            INSERT OR IGNORE INTO plots (
              plot_id, name, creator_username, mode, metric_kind,
              released_at, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, 'interactionCount', ?, ?, ?)
            """,
            (
                str(plot_id),
                str(plot.get("name") or "이름 없음"),
                plot.get("creator"),
                plot.get("mode") or "ZETA",
                plot.get("releasedAt"),
                first_seen_at,
                last_seen_at,
            ),
        )
        for tag in plot.get("tags") or []:
            normalized_tag = str(tag).strip().casefold()
            if not normalized_tag:
                continue
            db.execute(
                "INSERT OR IGNORE INTO plot_tags(plot_id, tag) VALUES (?, ?)",
                (str(plot_id), normalized_tag),
            )
            db.execute(
                "INSERT OR IGNORE INTO tag_queue(tag) VALUES (?)",
                (normalized_tag,),
            )
        latest_point: dict[str, Any] | None = None
        for point in series:
            if not isinstance(point, dict):
                continue
            day = point.get("date")
            chats = integer(point.get("chats"))
            if not day or chats is None:
                continue
            if latest_point is None or day >= latest_point["date"]:
                latest_point = point
            source = str(point.get("source") or "published-history")
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO plot_observations (
                  plot_id, observed_date, observed_at, source,
                  interaction_count, interaction_with_regen,
                  comment_count, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(plot_id),
                    day,
                    point.get("observedAt") or f"{day}T00:00:00Z",
                    source,
                    chats,
                    integer(point.get("chatsWithRegen")),
                    integer(point.get("comments")),
                    point.get("sourceUrl") or "checked-in-dashboard",
                ),
            )
            imported += max(cursor.rowcount, 0)
        if latest_point is not None:
            # 스냅샷으로만 들어온 플롯도 갱신 우선순위에서 0회로 취급되지 않게 함
            db.execute(
                """
                UPDATE plots SET
                  last_interaction_count=?,
                  last_interaction_with_regen=COALESCE(last_interaction_with_regen, ?)
                WHERE plot_id=? AND last_interaction_count IS NULL
                """,
                (
                    integer(latest_point.get("chats")),
                    integer(latest_point.get("chatsWithRegen")),
                    str(plot_id),
                ),
            )
    db.commit()
    return imported


def import_archive(db: sqlite3.Connection, archive_path: Path) -> dict[str, int]:
    """체크인된 Wayback 원자료 아카이브를 누락 행만 멱등 복원함.

    공개 대시보드 시계열은 플롯-날짜마다 대표 1행만 담으므로 같은 날의 다른
    원천 행과 홈 캡처 요약을 잃는다. 이 아카이브는 원천별 모든 행을 그대로
    보존해 Actions 캐시가 교체돼도 과거 복원값이 사라지지 않게 한다.
    """
    counts = {"plots": 0, "observations": 0, "homepage": 0}
    if not archive_path.exists():
        return counts
    try:
        payload = json.loads(archive_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return counts

    for plot in payload.get("plots", []):
        plot_id = plot.get("id")
        if not plot_id:
            continue
        seen_at = plot.get("firstSeenAt") or "1970-01-01T00:00:00Z"
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO plots (
              plot_id, name, creator_username, mode, metric_kind,
              released_at, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, 'interactionCount', ?, ?, ?)
            """,
            (
                str(plot_id),
                str(plot.get("name") or "이름 없음"),
                plot.get("creator"),
                plot.get("mode") or "ZETA",
                plot.get("releasedAt"),
                seen_at,
                seen_at,
            ),
        )
        counts["plots"] += max(cursor.rowcount, 0)
        for tag in plot.get("tags") or []:
            normalized_tag = str(tag).strip().casefold()
            if not normalized_tag:
                continue
            db.execute(
                "INSERT OR IGNORE INTO plot_tags(plot_id, tag) VALUES (?, ?)",
                (str(plot_id), normalized_tag),
            )
            db.execute("INSERT OR IGNORE INTO tag_queue(tag) VALUES (?)", (normalized_tag,))

    for row in payload.get("observations", []):
        plot_id = row.get("plotId")
        day = row.get("date")
        source = row.get("source")
        chats = integer(row.get("chats"))
        if not plot_id or not day or source not in WAYBACK_SOURCES or chats is None:
            continue
        exists = db.execute("SELECT 1 FROM plots WHERE plot_id=?", (plot_id,)).fetchone()
        if exists is None:
            continue
        observed_at = row.get("observedAt") or f"{day}T00:00:00Z"
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO plot_observations (
              plot_id, observed_date, observed_at, source,
              interaction_count, interaction_with_regen,
              comment_count, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(plot_id),
                day,
                observed_at,
                source,
                chats,
                integer(row.get("chatsWithRegen")),
                integer(row.get("comments")),
                row.get("sourceUrl") or "checked-in-archive",
            ),
        )
        counts["observations"] += max(cursor.rowcount, 0)
        if cursor.rowcount == 0:
            # 공개 스냅샷에서 들어온 행은 캡처 시각 대신 자정으로 저장돼 있었음
            db.execute(
                """
                UPDATE plot_observations SET observed_at=?
                WHERE plot_id=? AND observed_date=? AND source=?
                  AND observed_at=? AND interaction_count=?
                """,
                (observed_at, str(plot_id), day, source, f"{day}T00:00:00Z", chats),
            )

    for row in payload.get("homepage", []):
        required = (
            row.get("date"),
            row.get("observedAt"),
            integer(row.get("observedPlots")),
            integer(row.get("totalChats")),
            integer(row.get("averageChatsPerPlot")),
            integer(row.get("medianChatsPerPlot")),
            integer(row.get("top10Chats")),
            row.get("sourceUrl"),
        )
        if any(value is None for value in required):
            continue
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO homepage_observations (
              observed_date, observed_at, observed_plots, total_chats,
              total_chats_with_regen, average_chats_per_plot,
              median_chats_per_plot, top10_chats, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                required[0],
                required[1],
                required[2],
                required[3],
                integer(row.get("totalChatsWithRegen")),
                required[4],
                required[5],
                required[6],
                required[7],
            ),
        )
        counts["homepage"] += max(cursor.rowcount, 0)
    db.commit()
    return counts


def import_external_history(db: sqlite3.Connection, archive_path: Path) -> dict[str, int]:
    """출처가 고정된 제3자 공개 Git 이력을 개별 플롯 시계열로 복원함.

    이 데이터는 동적 랭킹 상위 표본이므로 플랫폼 총량·핵심 재고 계산에는
    쓰지 않는다. 원 저장소의 커밋 URL과 원값만 보존하며 알 수 없는 값은
    0으로 만들지 않는다.
    """
    counts = {"plots": 0, "observations": 0}
    if not archive_path.exists():
        return counts
    try:
        payload = json.loads(archive_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return counts

    for plot in payload.get("plots", []):
        plot_id = str(plot.get("id") or "").strip()
        if not plot_id:
            continue
        first_seen = plot.get("firstSeenAt") or "1970-01-01T00:00:00Z"
        last_seen = plot.get("lastSeenAt") or first_seen
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO plots (
              plot_id, name, creator_username, mode, metric_kind,
              first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, 'interactionCount', ?, ?)
            """,
            (
                plot_id,
                str(plot.get("name") or "이름 없음"),
                plot.get("creator"),
                plot.get("mode") or "ZETA",
                first_seen,
                last_seen,
            ),
        )
        counts["plots"] += max(cursor.rowcount, 0)
        for tag in plot.get("tags") or []:
            normalized_tag = str(tag).strip().casefold()
            if not normalized_tag:
                continue
            db.execute(
                "INSERT OR IGNORE INTO plot_tags(plot_id, tag) VALUES (?, ?)",
                (plot_id, normalized_tag),
            )
            db.execute("INSERT OR IGNORE INTO tag_queue(tag) VALUES (?)", (normalized_tag,))

    for row in payload.get("observations", []):
        plot_id = str(row.get("plotId") or "").strip()
        day = row.get("date")
        source = row.get("source")
        chats = integer(row.get("chats"))
        if (
            not plot_id
            or not day
            or source not in EXTERNAL_HISTORY_SOURCES
            or chats is None
            or chats < 0
            or db.execute("SELECT 1 FROM plots WHERE plot_id=?", (plot_id,)).fetchone() is None
        ):
            continue
        observed_at = row.get("observedAt") or f"{day}T00:00:00Z"
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO plot_observations (
              plot_id, observed_date, observed_at, source,
              interaction_count, interaction_with_regen,
              comment_count, source_url
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                plot_id,
                day,
                observed_at,
                source,
                chats,
                row.get("sourceUrl") or "checked-in-external-history",
            ),
        )
        counts["observations"] += max(cursor.rowcount, 0)
    db.commit()
    return counts


def repair_last_values(db: sqlite3.Connection) -> int:
    """비어 있는 플롯별 최신 대화량을 저장된 관측값으로 채움."""
    cursor = db.execute(
        """
        UPDATE plots SET
          last_interaction_count=(
            SELECT o.interaction_count
            FROM plot_observations o
            WHERE o.plot_id=plots.plot_id AND o.interaction_count IS NOT NULL
            ORDER BY o.observed_at DESC
            LIMIT 1
          ),
          last_interaction_with_regen=COALESCE(last_interaction_with_regen, (
            SELECT o.interaction_with_regen
            FROM plot_observations o
            WHERE o.plot_id=plots.plot_id AND o.interaction_count IS NOT NULL
            ORDER BY o.observed_at DESC
            LIMIT 1
          ))
        WHERE last_interaction_count IS NULL
          AND EXISTS (
            SELECT 1 FROM plot_observations o
            WHERE o.plot_id=plots.plot_id AND o.interaction_count IS NOT NULL
          )
        """
    )
    db.commit()
    return max(cursor.rowcount, 0)


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
          last_interaction_with_regen=excluded.last_interaction_with_regen,
          missing_since=NULL,
          missing_date=NULL
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


def collect_ranking(db: sqlite3.Connection, now: datetime, ranking_type: str) -> int:
    url = ranking_url(ranking_type)
    payload = request_json(url)
    found, _ = ingest_payload(
        db, payload, observed_at=iso_z(utc_now()), observed_date=kst_day(now),
        source=f"ranking:{ranking_type.lower()}", source_url=url,
    )
    if ranking_type == "TRENDING" and found == 0:
        raise RuntimeError("TRENDING returned no valid plots; collection is incomplete")
    db.commit()
    return found


def collect_seeds(db: sqlite3.Connection, now: datetime, errors: list[str], infinite_pages: int) -> None:
    # 날짜는 실행 기준일로 묶고, 시각은 응답을 받은 실제 시각을 저장함
    observed_date = kst_day(now)
    for ranking_type in RANKING_TYPES:
        try:
            collect_ranking(db, now, ranking_type)
        except Exception as exc:
            if ranking_type == "TRENDING":
                raise
            errors.append(f"ranking {ranking_type}: {exc}")

    discovery_url = f"{API}/v1/discovery-tab"
    try:
        payload = request_json(discovery_url)
        ingest_payload(
            db,
            payload,
            observed_at=iso_z(utc_now()),
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
                observed_at=iso_z(utc_now()),
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
    observed_date = kst_day(now)
    pages = 0
    while pages < max_pages:
        # 이름순이 아니라 확보 플롯이 많은 태그부터 조회함. 아직 한 번도 조회하지
        # 않은 태그를 먼저 돌고, 모두 한 번씩 조회한 뒤에는 오래된 커서부터 이어감.
        row = db.execute(
            """
            SELECT q.tag, q.next_cursor, q.reported_count, q.pages_collected
            FROM tag_queue q
            LEFT JOIN (
              SELECT tag, COUNT(*) AS members FROM plot_tags GROUP BY tag
            ) t ON t.tag=q.tag
            WHERE q.complete=0
            ORDER BY (q.last_crawled_at IS NOT NULL),
                     COALESCE(t.members, 0) DESC,
                     COALESCE(q.last_crawled_at, ''),
                     q.tag
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
        observed_at = iso_z(utc_now())
        try:
            payload = request_json(url)
            observed_at = iso_z(utc_now())
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
    observed_date = kst_day(now)
    rows = db.execute(
        """
        SELECT plot_id
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
            observed_at = iso_z(utc_now())
            # 대화량은 같은 날 실제로 읽은 값만 옮김. 예전 값을 오늘 날짜로 복사하면
            # 멈춘 플롯처럼 보이거나 당일 재조회율이 부풀려짐.
            same_day = db.execute(
                """
                SELECT interaction_count, interaction_with_regen
                FROM plot_observations
                WHERE plot_id=? AND observed_date=? AND source<>'live'
                  AND interaction_count IS NOT NULL
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                (plot_id, observed_date),
            ).fetchone()
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
                    same_day["interaction_count"] if same_day else None,
                    same_day["interaction_with_regen"] if same_day else None,
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


def select_panel_targets(db: sqlite3.Connection, limit: int) -> list[str]:
    """고정 측정 패널 전체를 대화량 구간 순서로 고름."""
    if limit <= 0:
        return []
    rows = db.execute(
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
    return [row["plot_id"] for row in rows[:limit]]


def select_followup_targets(
    db: sqlite3.Connection,
    now: datetime,
    *,
    limit: int,
    hot_limit: int = HOT_REFRESH_TARGET,
) -> list[str]:
    """패널 밖 50만+ 재고 전체 → 신규·급상승 → 오래 미관측 순환 표본을 고름.

    50만+ 플롯은 패널 상위 구간에 모두 들어간다는 가정이 발견 범위가 넓어지며
    깨졌으므로, 패널 밖 핵심·후보 플롯을 한도 안에서 매일 모두 다시 읽는다.
    """
    if limit <= 0:
        return []
    rows = db.execute(
        """
        SELECT p.plot_id, p.first_seen_at, p.released_at, p.missing_since,
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
    targets: list[str] = []
    selected: set[str] = set()

    def take(candidates: Iterable[sqlite3.Row], budget: int) -> None:
        for row in candidates:
            if budget <= 0:
                return
            if row["plot_id"] in selected:
                continue
            targets.append(row["plot_id"])
            selected.add(row["plot_id"])
            budget -= 1

    watch_rows = sorted(
        (row for row in rows if row["chats"] >= WATCH_THRESHOLD),
        # 사라진 것으로 확인된 플롯은 같은 대화량 구간에서 뒤로 보냄
        key=lambda row: (row["missing_since"] is not None, -row["chats"], row["plot_id"]),
    )
    take(watch_rows, limit)

    # 과거 성장지수를 현재까지 잇는 최근 Wayback 홈 표본은 크기와 무관하게 매일 다시 읽음
    wayback_cohort = {
        row["plot_id"]
        for row in db.execute(
            """
            SELECT DISTINCT plot_id
            FROM plot_observations
            WHERE source='wayback-home'
              AND observed_date IN (
                SELECT observed_date FROM homepage_observations
                ORDER BY observed_date DESC
                LIMIT ?
              )
            """,
            (WAYBACK_COHORT_DATES,),
        )
    }
    cohort_rows = sorted(
        (row for row in rows if row["plot_id"] in wayback_cohort),
        key=lambda row: (row["missing_since"] is not None, -row["chats"], row["plot_id"]),
    )
    take(cohort_rows, limit - len(targets))

    released_cutoff = iso_z(now - timedelta(days=NEW_PLOT_DAYS))
    seen_cutoff = iso_z(now - timedelta(days=2))

    def fresh_rank(row: sqlite3.Row) -> int | None:
        if (row["released_at"] or "") >= released_cutoff:
            return 0
        if (row["first_seen_at"] or "") >= seen_cutoff:
            return 1
        return None

    hot_rows = sorted(
        (row for row in rows if fresh_rank(row) is not None),
        key=lambda row: (fresh_rank(row), -row["chats"], row["plot_id"]),
    )
    take(hot_rows, min(hot_limit, limit - len(targets)))

    rotation_rows = sorted(
        rows,
        key=lambda row: (
            row["last_detail_date"],
            -row["chats"],
            row["plot_id"],
        ),
    )
    take(rotation_rows, limit - len(targets))
    return targets


def select_refresh_targets(
    db: sqlite3.Connection,
    now: datetime,
    *,
    limit: int,
    hot_limit: int = HOT_REFRESH_TARGET,
) -> list[str]:
    """고정 패널 → 패널 밖 50만+ → 신규·급상승 → 순환 표본 순서 전체."""
    targets = select_panel_targets(db, limit)
    return targets + select_followup_targets(
        db, now, limit=limit - len(targets), hot_limit=hot_limit
    )


def refresh_plot_ids(
    db: sqlite3.Connection,
    now: datetime,
    plot_ids: list[str],
    *,
    workers: int,
    errors: list[str],
) -> dict[str, int]:
    """상세 API 원값을 읽어 응답을 받은 실제 시각과 함께 저장함."""
    observed_date = kst_day(now)
    result = {"requested": len(plot_ids), "completed": 0, "missing": 0}
    if not plot_ids:
        return result

    def fetch(plot_id: str) -> tuple[str, str, Any, str]:
        url = f"{API}/v1/plots/{plot_id}"
        try:
            payload = request_json(url, attempts=2)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                return plot_id, url, None, iso_z(utc_now())
            raise
        return plot_id, url, payload, iso_z(utc_now())

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch, plot_id): plot_id for plot_id in plot_ids}
        for future in as_completed(futures):
            plot_id = futures[future]
            try:
                _, url, payload, fetched_at = future.result()
                if payload is None:
                    # 삭제·비공개로 보이는 404는 0이 아니라 사라진 플롯으로 기록함
                    db.execute(
                        """
                        UPDATE plots SET
                          missing_since=COALESCE(missing_since, ?),
                          missing_date=COALESCE(missing_date, ?),
                          missing_checked_at=?
                        WHERE plot_id=?
                        """,
                        (fetched_at, observed_date, fetched_at, plot_id),
                    )
                    result["missing"] += 1
                    continue
                plot = normalize_plot(payload)
                if plot is None:
                    errors.append(f"plot {plot_id}: detail response had no plot")
                    continue
                upsert_plot(
                    db,
                    plot,
                    observed_at=fetched_at,
                    observed_date=observed_date,
                    source="detail",
                    source_url=url,
                )
                result["completed"] += 1
                if result["completed"] % 50 == 0:
                    db.commit()
            except Exception as exc:
                errors.append(f"plot {plot_id}: {exc}")
    db.commit()
    return result


def refresh_known_plots(
    db: sqlite3.Connection,
    now: datetime,
    *,
    limit: int,
    workers: int,
    errors: list[str],
    phase: str = "all",
) -> int:
    """고정 측정 패널·패널 밖 50만+·신규 급상승·순환 표본의 상세 원값을 갱신함."""
    if limit <= 0:
        return 0
    ensure_measurement_panel(db, now)
    if phase == "panel":
        plot_ids = select_panel_targets(db, limit)
    elif phase == "followup":
        plot_ids = select_followup_targets(db, now, limit=limit)
    else:
        plot_ids = select_refresh_targets(db, now, limit=limit)
    return refresh_plot_ids(
        db, now, plot_ids, workers=workers, errors=errors
    )["completed"]


def latest_plot_rows(db: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = db.execute(
        """
        WITH ranked AS (
          SELECT o.*,
            ROW_NUMBER() OVER (
              PARTITION BY o.plot_id
              ORDER BY (o.interaction_count IS NOT NULL) DESC,
                       o.observed_at DESC,
                       (o.comment_count IS NOT NULL) DESC,
                       (o.source = 'live') DESC,
                       o.source DESC
            ) AS position
          FROM plot_observations o
        )
        SELECT ranked.*, p.name, p.creator_username, p.mode, p.released_at,
               p.missing_since,
               p.last_comment_count AS latest_comment_count,
               p.comment_observed_at AS latest_comment_observed_at
        FROM ranked
        JOIN plots p ON p.plot_id=ranked.plot_id
        WHERE ranked.position=1
        """
    ).fetchall()
    return {row["plot_id"]: row for row in rows}


def prefer_observation(current: sqlite3.Row | None, candidate: sqlite3.Row) -> sqlite3.Row:
    """같은 날 여러 출처가 있으면 대화량이 있는 최신·댓글 포함 관측을 고름."""
    if current is None:
        return candidate
    current_key = (
        current["interaction_count"] is not None,
        current["observed_at"],
        current["comment_count"] is not None,
        current["source"] == "live",
        current["source"],
    )
    candidate_key = (
        candidate["interaction_count"] is not None,
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
    db: sqlite3.Connection,
    homepage_history: list[dict[str, Any]],
    link_status: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Wayback 홈 동일 플롯 지수에 최신 공개 API 관측점을 연결함.

    link_status를 넘기면 현재 연결 여부와 과거 표본 재조회 비율을 채워 화면이
    연결하지 못한 이유를 숨기지 않게 함.
    """
    status = link_status if link_status is not None else {}
    status.update(
        {
            "linked": False,
            "latestDate": None,
            "bestSharePct": None,
            "requiredSharePct": round(CURRENT_MATCH_MIN_SHARE * 100),
        }
    )
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
        WHERE source NOT LIKE 'wayback%' AND source NOT LIKE 'external:%'
          AND interaction_count IS NOT NULL
        """
    ).fetchone()
    latest_date = latest_row[0] if latest_row else None
    status["latestDate"] = latest_date
    if not latest_date or latest_date <= history[-1]["date"]:
        return history

    current_rows: dict[str, sqlite3.Row] = {}
    for row in db.execute(
        """
        SELECT * FROM plot_observations
        WHERE observed_date=? AND source NOT LIKE 'wayback%'
          AND source NOT LIKE 'external:%'
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

    missing = {
        row["plot_id"]
        for row in db.execute("SELECT plot_id FROM plots WHERE missing_date IS NOT NULL")
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
        # 과거 표본 중 일부 대형 플롯만 다시 읽힌 날은 표본 편향이 커서 연결하지 않음
        alive = [plot_id for plot_id in prior if plot_id not in missing and prior[plot_id] > 0]
        share = len(overlap) / len(alive) if alive else 0
        if status["bestSharePct"] is None or share * 100 > status["bestSharePct"]:
            status["bestSharePct"] = round(share * 100, 1)
        if len(overlap) < 10 or share < CURRENT_MATCH_MIN_SHARE:
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
                "comparisonPlots": len(alive),
                "comparisonDate": previous["date"],
                "sourceUrl": (
                    "https://api.zeta-ai.io/v1/plots/ranking"
                    "?type=GLOBAL&limit=100&gender=ALL"
                ),
                "isCurrent": True,
            }
        )
        status["linked"] = True
        status["bestSharePct"] = round(share * 100, 1)
        break
    return history


def interval_hours(start_at: str | None, end_at: str | None) -> float | None:
    start = parse_iso_z(start_at)
    end = parse_iso_z(end_at)
    if start is None or end is None or end <= start:
        return None
    return (end - start).total_seconds() / 3600


def kst_clock(value: str | None) -> str | None:
    parsed = parse_iso_z(value)
    return parsed.astimezone(KST).strftime("%H:%M") if parsed else None


def build_activity_history(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """고정 패널의 연속 일일 상세 관측으로 채팅 활동 속도를 계산함.

    두 관측의 실제 간격이 24시간이 아니면 원값 증분은 하루치가 아니다. 플롯별
    실제 간격으로 24시간 환산값을 함께 내고, 간격이 24시간 ±
    TIMING_TOLERANCE_HOURS를 벗어난 날은 7일 평균과 가속·감속 계산에서 뺀다.
    """
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

    def consecutive(items: list[dict[str, Any]], span_days: int) -> bool:
        return (
            datetime.strptime(items[-1]["date"], "%Y-%m-%d").date()
            - datetime.strptime(items[0]["date"], "%Y-%m-%d").date()
        ).days == span_days

    history: list[dict[str, Any]] = []
    for day in sorted(by_day):
        previous_day = (
            datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=1)
        ).isoformat()
        current = by_day[day]
        previous = by_day.get(previous_day, {})
        matched_ids = sorted(current.keys() & previous.keys())
        raw_deltas: list[int] = []
        valid_deltas: list[int] = []
        normalized: list[float] = []
        intervals: list[float] = []
        for plot_id in matched_ids:
            delta = (
                current[plot_id]["interaction_count"]
                - previous[plot_id]["interaction_count"]
            )
            raw_deltas.append(delta)
            if delta < 0:
                continue
            valid_deltas.append(delta)
            hours = interval_hours(
                previous[plot_id]["observed_at"], current[plot_id]["observed_at"]
            )
            if hours is not None:
                intervals.append(hours)
                normalized.append(delta * 24 / hours)
        positive = [value for value in valid_deltas if value > 0]
        current_values = [row["interaction_count"] for row in current.values()]
        total = sum(valid_deltas)
        top10 = sum(sorted(positive, reverse=True)[:10])
        interval = statistics.median(intervals) if intervals else None
        timing_valid = (
            interval is not None
            and abs(interval - 24) <= TIMING_TOLERANCE_HOURS
        )
        observed_times = sorted(row["observed_at"] for row in current.values())
        observed_median = (
            observed_times[len(observed_times) // 2] if observed_times else None
        )
        comparison_pct = round(len(matched_ids) / panel_size * 100, 1)
        per24h = (
            round(sum(normalized))
            if valid_deltas and len(normalized) == len(valid_deltas)
            else None
        )
        point: dict[str, Any] = {
            "date": day,
            "panelSize": panel_size,
            "observedPanelPlots": len(current),
            "panelCoveragePct": round(len(current) / panel_size * 100, 1),
            "matchedPlots": len(matched_ids),
            "comparisonCoveragePct": comparison_pct,
            "validDeltaPlots": len(valid_deltas),
            "negativeCorrections": len(raw_deltas) - len(valid_deltas),
            "panelTotalChats": sum(current_values) if current_values else None,
            "averageCumulativeChatsPerPlot": round(statistics.mean(current_values))
            if current_values else None,
            "medianCumulativeChatsPerPlot": round(statistics.median(current_values))
            if current_values else None,
            "totalNewChats": total if valid_deltas else None,
            "newChatsPer24h": per24h,
            "intervalHours": round(interval, 2) if interval is not None else None,
            "intervalMinHours": round(min(intervals), 2) if intervals else None,
            "intervalMaxHours": round(max(intervals), 2) if intervals else None,
            "observedAt": observed_median,
            "observedClockKst": kst_clock(observed_median),
            "timingValid": timing_valid,
            "validForTrend": bool(
                valid_deltas and comparison_pct >= 95 and timing_valid
            ),
            "validStreakDays": 0,
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
        if point["validForTrend"]:
            prior = history[-1] if history else None
            point["validStreakDays"] = (
                prior["validStreakDays"] + 1
                if prior and prior["validForTrend"] and prior["date"] == previous_day
                else 1
            )
        history.append(point)

        recent7 = history[-7:]
        if (
            len(recent7) == 7
            and consecutive(recent7, 6)
            and all(item["validForTrend"] for item in recent7)
        ):
            point["sevenDayAverageNewChats"] = round(
                statistics.mean(item["newChatsPer24h"] for item in recent7)
            )

        recent14 = history[-14:]
        if (
            len(recent14) == 14
            and consecutive(recent14, 13)
            and all(item["validForTrend"] for item in recent14)
        ):
            previous7 = recent14[:7]
            current7 = recent14[7:]
            current_average = round(
                statistics.mean(item["newChatsPer24h"] for item in current7)
            )
            previous_average = round(
                statistics.mean(item["newChatsPer24h"] for item in previous7)
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
    100만 이상이었던 플롯을 분리함. 전날 대비 합산 변화는
    기존 핵심 증가 + 실제 돌파 + 늦게 발견 + 복귀 − 이탈로 정확히 나눔.
    삭제·비공개로 확인된 플롯은 확인한 날부터 공개 재고에서 뺌.
    """
    rows = db.execute(
        """
        SELECT *
        FROM plot_observations
        WHERE source NOT LIKE 'wayback%'
          AND source NOT LIKE 'external:%'
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
    # 사라진 것으로 확인된 수집 기준일(KST)부터 공개 재고에서 뺌
    missing_day: dict[str, str] = {
        row["plot_id"]: row["missing_date"]
        for row in db.execute(
            "SELECT plot_id, missing_date FROM plots WHERE missing_date IS NOT NULL"
        )
    }

    snapshot: dict[str, sqlite3.Row] = {}
    previous_core: set[str] = set()
    previous_active: dict[str, int] = {}
    history: list[dict[str, Any]] = []
    for day in sorted(by_day):
        stale_cutoff = (
            datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=2)
        ).isoformat()
        previous_values_by_plot = {
            plot_id: row["interaction_count"] for plot_id, row in snapshot.items()
        }
        snapshot.update(by_day[day])
        active = {
            plot_id: row
            for plot_id, row in snapshot.items()
            if not (plot_id in missing_day and missing_day[plot_id] <= day)
        }
        core_ids = {
            plot_id
            for plot_id, row in active.items()
            if row["interaction_count"] >= CORE_THRESHOLD
        }
        watch_ids = {
            plot_id
            for plot_id, row in active.items()
            if WATCH_THRESHOLD <= row["interaction_count"] < CORE_THRESHOLD
        }
        entrants = core_ids - previous_core
        crossed_ids = [
            plot_id
            for plot_id in entrants
            if plot_id in previous_values_by_plot
            and previous_values_by_plot[plot_id] < CORE_THRESHOLD
        ]
        discovered_ids = [
            plot_id for plot_id in entrants if plot_id not in previous_values_by_plot
        ]
        returned_ids = [
            plot_id
            for plot_id in entrants
            if plot_id not in crossed_ids and plot_id not in discovered_ids
        ]
        stayed_ids = core_ids & previous_core
        exited_ids = previous_core - core_ids
        refreshed_core = sum(
            active[plot_id]["observed_date"] == day for plot_id in core_ids
        )
        core_count = len(core_ids)
        core_total = sum(active[plot_id]["interaction_count"] for plot_id in core_ids)
        history.append(
            {
                "date": day,
                "corePlotCount": core_count,
                "coreTotalChats": core_total,
                "watchPlotCount": len(watch_ids),
                "watchTotalChats": sum(
                    active[plot_id]["interaction_count"] for plot_id in watch_ids
                ),
                "observedPlots": len(by_day[day]),
                "refreshedCorePlots": refreshed_core,
                "coreCoveragePct": round(
                    refreshed_core / core_count * 100, 1
                ) if core_count else None,
                "staleCorePlots": sum(
                    active[plot_id]["observed_date"] < stale_cutoff
                    for plot_id in core_ids
                ),
                "crossedCoreCount": len(crossed_ids),
                "discoveredCoreCount": len(discovered_ids),
                "returnedCoreCount": len(returned_ids),
                "exitedCoreCount": len(exited_ids),
                "missingCoreCount": sum(
                    plot_id in missing_day and missing_day[plot_id] <= day
                    for plot_id in exited_ids
                ),
                "previousCoreTotalChats": sum(
                    previous_active[plot_id] for plot_id in previous_core
                ),
                "existingCoreGrowth": sum(
                    active[plot_id]["interaction_count"]
                    - previous_values_by_plot[plot_id]
                    for plot_id in stayed_ids
                ),
                "crossedCoreChats": sum(
                    active[plot_id]["interaction_count"] for plot_id in crossed_ids
                ),
                "discoveredCoreChats": sum(
                    active[plot_id]["interaction_count"] for plot_id in discovered_ids
                ),
                "returnedCoreChats": sum(
                    active[plot_id]["interaction_count"] for plot_id in returned_ids
                ),
                "exitedCoreChats": sum(
                    previous_active[plot_id] for plot_id in exited_ids
                ),
            }
        )
        previous_core = core_ids
        previous_active = {
            plot_id: row["interaction_count"] for plot_id, row in active.items()
        }
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
    crawled_tags = db.execute(
        "SELECT COUNT(*) FROM tag_queue WHERE pages_collected>0"
    ).fetchone()[0]
    pending_tags = known_tags - completed_tags
    missing_plots = db.execute(
        "SELECT COUNT(*) FROM plots WHERE missing_since IS NOT NULL"
    ).fetchone()[0]
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
        if str(row["source"]).startswith("external:"):
            continue
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

    latest_date = max(
        (row["observed_date"] for row in latest.values()
         if not str(row["source"]).startswith("external:")), default=None,
    )
    current_latest = {
        plot_id: row
        for plot_id, row in latest.items()
        if row["observed_date"] == latest_date and not str(row["source"]).startswith("external:")
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
                "missingSince": row["missing_since"],
                "tags": tags,
                "chats": row["interaction_count"],
                "chatsWithRegen": row["interaction_with_regen"],
                "comments": row["latest_comment_count"],
                "commentsObservedAt": row["latest_comment_observed_at"],
                "latestObservedDate": row["observed_date"],
                "historicalOnly": str(row["source"]).startswith("external:"),
                "changes": periods,
                "series": [
                    {
                        "date": item["observed_date"],
                        "observedAt": item["observed_at"],
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
    regeneration_cohorts = build_regeneration_cohorts(
        [plot for plot in plot_payloads if not plot["historicalOnly"]]
    )

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
            values = [
                deltas[plot_id][days] for plot_id in members
                if plot_id in deltas and not str(latest[plot_id]["source"]).startswith("external:")
            ]
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
    matched_link: dict[str, Any] = {}
    payload = {
        "schemaVersion": 1,
        "market": MARKET_LANGUAGE,
        "updatedAt": iso_z(utc_now()),
        "latestDate": latest_date,
        "coverage": {
            "knownPlots": known_plots,
            "knownTags": known_tags,
            "completedTags": completed_tags,
            "crawledTags": crawled_tags,
            "pendingTags": pending_tags,
            "missingPlots": missing_plots,
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
            "externalHistoryPlots": db.execute(
                "SELECT COUNT(DISTINCT plot_id) FROM plot_observations WHERE source LIKE 'external:%'"
            ).fetchone()[0],
            "externalHistoryObservations": db.execute(
                "SELECT COUNT(*) FROM plot_observations WHERE source LIKE 'external:%'"
            ).fetchone()[0],
            "trendingLatestDate": db.execute(
                "SELECT MAX(observed_date) FROM plot_observations WHERE source='ranking:trending'"
            ).fetchone()[0],
            "trendingPlotsLatest": db.execute(
                """SELECT COUNT(*) FROM plot_observations
                   WHERE source='ranking:trending' AND observed_date=(
                     SELECT MAX(observed_date) FROM plot_observations WHERE source='ranking:trending'
                   )"""
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
            "rateBasis": "per24hByActualInterval",
            "timingToleranceHours": TIMING_TOLERANCE_HOURS,
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
            "missingPanelPlots": db.execute(
                """
                SELECT COUNT(*)
                FROM measurement_panel m
                JOIN plots p ON p.plot_id=m.plot_id
                WHERE m.active=1 AND p.missing_since IS NOT NULL
                """
            ).fetchone()[0],
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
        "matchedGrowthHistory": build_matched_growth_history(
            db, homepage_history, matched_link
        ),
        "matchedGrowthLink": matched_link,
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
    write_json(out_dir / "dashboard.json", payload, compact=True)
    write_json(
        out_dir / "status.json",
        {
            "updatedAt": payload["updatedAt"],
            "market": payload["market"],
            "latestDate": payload["latestDate"],
            "coverage": payload["coverage"],
            "activityPanel": payload["activityPanel"],
            "activityLatest": payload["activityLatest"],
            "coreInventory": payload["coreInventory"],
            "errors": payload["errors"],
        },
    )
    return payload


def plan_collection(db: sqlite3.Connection, now: datetime, mode: str) -> tuple[bool, str]:
    """이번 실행이 네트워크 수집을 할지 정함.

    auto는 같은 KST 날짜에 성공한 수집이 없을 때만 수집한다. 예약 실행과 외부
    정시 호출이 겹치거나 수동 재실행이 그날 관측 시각을 늦추는 일을 막는다.
    """
    if mode == "build-only":
        return False, "build-only"
    if mode == "collect":
        return True, "forced"
    if mode == "trending-only":
        return True, "trending-only"
    today = kst_day(now)
    for row in db.execute(
        "SELECT started_at FROM runs WHERE success=1 ORDER BY run_id DESC LIMIT 20"
    ):
        started = parse_iso_z(row["started_at"])
        if started is not None and kst_day(started) == today:
            return False, f"already-collected-{today}"
    return True, f"first-collection-{today}"


def market_paths(root: Path, market: str) -> dict[str, str | None]:
    """시장별 기본 경로. 한국은 기존 경로를 그대로 쓰고, 다른 시장은 별도 DB·출력에
    쌓아 한국 시계열과 섞이지 않게 함. 과거 복원 자료(Wayback·외부 스냅샷·체크인
    공개 스냅샷)는 한국 시장 자료라 다른 시장에는 넣지 않음."""
    if market == "KOREAN":
        return {
            "db": str(root / "data" / "zeta.sqlite3"),
            "out": str(root / "public" / "data"),
            "seed_snapshot": str(root / "public" / "data" / "dashboard.json"),
            "archive": str(root / "archive" / "wayback-observations.json"),
            "external_history": str(root / "archive" / "otoscor-zeta-observations.json"),
        }
    suffix = {"JAPANESE": "ja", "ENGLISH": "en"}[market]
    return {
        "db": str(root / "data" / f"zeta-{suffix}.sqlite3"),
        "out": str(root / "public" / "data" / suffix),
        "seed_snapshot": None,
        "archive": None,
        "external_history": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--market", choices=MARKETS, default="KOREAN")
    parser.add_argument("--db")
    parser.add_argument("--out")
    parser.add_argument("--seed-snapshot")
    parser.add_argument("--archive")
    parser.add_argument("--external-history")
    parser.add_argument(
        "--trending-only", action="store_true",
        help="기존 당일 관측을 유지하고 TRENDING만 추가함(그날 정기 수집이 끝난 뒤에만 쓸 것)",
    )
    parser.add_argument("--max-tag-pages", type=int, default=120)
    parser.add_argument("--max-comment-plots", type=int, default=250)
    parser.add_argument("--max-plot-refresh", type=int, default=3000)
    parser.add_argument("--refresh-workers", type=int, default=10)
    parser.add_argument("--infinite-pages", type=int, default=8)
    parser.add_argument(
        "--plan",
        choices=("auto", "collect", "build-only", "trending-only"),
        help="수집 여부만 판단해 GitHub Actions 출력 형식으로 찍고 끝냄",
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="네트워크 수집 없이 DB에서 대시보드만 다시 만듦",
    )
    args = parser.parse_args()
    set_market(args.market)
    defaults = market_paths(root, args.market)
    for key, value in defaults.items():
        if getattr(args, key) is None:
            setattr(args, key, value)

    now = utc_now()
    db = connect(Path(args.db))
    if args.plan:
        # 같은 KST 날짜에 이미 수집했으면 다시 수집하지 않아 그날 관측 시각이 밀리지 않게 함
        should, reason = plan_collection(db, now, args.plan)
        print(f"collect={'true' if should else 'false'}")
        print(f"reason={reason}")
        db.close()
        return 0
    archive_counts = (
        import_archive(db, Path(args.archive)) if args.archive else {}
    )
    external_counts = (
        import_external_history(db, Path(args.external_history))
        if args.external_history
        else {}
    )
    if args.seed_snapshot:
        import_published_history(db, Path(args.seed_snapshot))
    repaired = repair_last_values(db)
    if args.build_only:
        if db.execute("SELECT COUNT(*) FROM runs WHERE success=1").fetchone()[0] == 0:
            # 캐시가 비어 있으면 스냅샷만으로 만든 빈약한 화면을 배포하지 않음
            print("build-only: no successful collection in DB; refusing to publish")
            db.close()
            return 1
        last_run = db.execute(
            "SELECT errors_json FROM runs WHERE finished_at IS NOT NULL ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        try:
            carried = list(json.loads(last_run["errors_json"] or "[]")) if last_run else []
        except (TypeError, ValueError):
            carried = []
        payload = build_dashboard(db, Path(args.out), carried)
        print(json.dumps({"buildOnly": True, "coverage": payload["coverage"]}, ensure_ascii=False))
        db.close()
        return 0
    before = db.execute("SELECT COUNT(*) FROM plots").fetchone()[0]
    run_id = db.execute(
        "INSERT INTO runs(started_at, plots_before) VALUES (?, ?)",
        (iso_z(now), before),
    ).lastrowid
    db.commit()
    errors: list[str] = []
    try:
        tag_pages = comment_plots = 0
        panel_result = followup_result = None
        if args.trending_only:
            collect_ranking(db, now, "TRENDING")
        else:
            # 고정 패널을 실행 직후 가장 먼저 읽어 날마다 같은 시각에 가깝게 관측함
            ensure_measurement_panel(db, now)
            panel_ids = select_panel_targets(db, args.max_plot_refresh)
            panel_result = refresh_plot_ids(
                db, now, panel_ids, workers=args.refresh_workers, errors=errors
            )
            collect_seeds(db, now, errors, args.infinite_pages)
            tag_pages = crawl_tag_queue(db, now, max_pages=args.max_tag_pages, errors=errors)
            followup_ids = select_followup_targets(
                db, now, limit=args.max_plot_refresh - len(panel_ids)
            )
            followup_result = refresh_plot_ids(
                db, now, followup_ids, workers=args.refresh_workers, errors=errors
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
        print(
            json.dumps(
                {
                    "coverage": payload["coverage"],
                    "archiveImported": archive_counts,
                    "externalHistoryImported": external_counts,
                    "repairedLastValues": repaired,
                    "panel": panel_result,
                    "followup": followup_result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
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
