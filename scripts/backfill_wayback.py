#!/usr/bin/env python3
"""Wayback의 제타 플롯 페이지에서 확인 가능한 정확한 누적값을 복원함."""

from __future__ import annotations

import argparse
import gzip
import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from collect import (
        API,
        USER_AGENT,
        build_dashboard,
        connect,
        ingest_payload,
        iso_z,
        kst_day,
        request_json,
        utc_now,
    )
except ModuleNotFoundError:  # unittest가 저장소 루트에서 모듈로 불러올 때
    from scripts.collect import (
        API,
        USER_AGENT,
        build_dashboard,
        connect,
        ingest_payload,
        iso_z,
        kst_day,
        request_json,
        utc_now,
    )

CDX = "https://web.archive.org/cdx/search/cdx"
TIMEMAP = "https://web.archive.org/web/timemap/json"
ARCHIVE = "https://web.archive.org/web"
NUMBER_PATTERNS = {
    "interaction_count": re.compile(r'(?<!WithRegen)interactionCount(?:\\?"|\\?\')?\s*:\s*(\d+)'),
    "interaction_with_regen": re.compile(r'interactionCountWithRegen(?:\\?"|\\?\')?\s*:\s*(\d+)'),
}
PROFILE_ID = re.compile(r"/characters/([0-9a-f-]{36})/profile", re.IGNORECASE)


def request_bytes(url: str, attempts: int = 2, timeout: int = 50) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html,*/*",
            "Accept-Encoding": "gzip",
        },
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
                if response.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                return data
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
    assert last_error is not None
    raise last_error


def parse_counts(html: str) -> tuple[int | None, int | None]:
    values: dict[str, int | None] = {}
    for key, pattern in NUMBER_PATTERNS.items():
        match = pattern.search(html)
        values[key] = int(match.group(1)) if match else None
    return values["interaction_count"], values["interaction_with_regen"]


def profile_urls(plot_id: str) -> list[str]:
    return [
        f"https://zeta-ai.io/ko/characters/{plot_id}/profile",
        f"https://zeta-ai.io/ko/plots/{plot_id}/profile",
    ]


def discover_domain_profiles(limit: int) -> list[tuple[str, str]]:
    """Wayback 도메인 색인에서 보관된 구형 프로필 ID와 대표 캡처를 찾음."""
    query = urllib.parse.urlencode(
        {
            "url": "zeta-ai.io/ko/characters/",
            "matchType": "prefix",
            "output": "json",
            "fl": "timestamp,original",
            "from": "2024",
            "filter": [
                "statuscode:200",
                "mimetype:text/html",
                "original:.*profile.*",
            ],
            "collapse": "urlkey",
            "limit": str(limit * 3),
            "fastLatest": "true",
        },
        doseq=True,
    )
    payload: Any = json.loads(
        request_bytes(f"{CDX}?{query}", attempts=2, timeout=60).decode("utf-8")
    )
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    unique: dict[str, str] = {}
    for timestamp, original in payload[1:]:
        match = PROFILE_ID.search(str(original))
        if match and match.group(1) not in unique:
            unique[match.group(1)] = str(timestamp)
            if len(unique) >= limit:
                break
    return list(unique.items())


def ensure_discovered_plots(db, discovered: list[tuple[str, str]], workers: int) -> None:
    now = utc_now()
    existing = {
        row[0]
        for row in db.execute(
            f"SELECT plot_id FROM plots WHERE plot_id IN ({','.join('?' for _ in discovered)})",
            [plot_id for plot_id, _ in discovered],
        ).fetchall()
    } if discovered else set()
    missing = [(plot_id, timestamp) for plot_id, timestamp in discovered if plot_id not in existing]

    def fetch_current(plot_id: str):
        return plot_id, request_json(f"{API}/v1/plots/{plot_id}", attempts=1)

    fetched: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch_current, plot_id): plot_id for plot_id, _ in missing}
        for future in as_completed(futures):
            try:
                plot_id, payload = future.result()
                fetched[plot_id] = payload
            except Exception:
                pass

    for plot_id, timestamp in missing:
        if plot_id in fetched:
            ingest_payload(
                db,
                fetched[plot_id],
                observed_at=iso_z(now),
                observed_date=kst_day(now),
                source="detail",
                source_url=f"{API}/v1/plots/{plot_id}",
            )
        else:
            seen = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            db.execute(
                """
                INSERT OR IGNORE INTO plots (
                  plot_id, name, metric_kind, first_seen_at, last_seen_at
                ) VALUES (?, ?, 'interactionCount', ?, ?)
                """,
                (plot_id, f"보관 플롯 {plot_id[:8]}", iso_z(seen), iso_z(seen)),
            )
    db.commit()


def cdx_rows(original_url: str) -> list[dict[str, str]]:
    timemap_url = f"{TIMEMAP}?{urllib.parse.urlencode({'url': original_url})}"
    try:
        payload: Any = json.loads(
            request_bytes(timemap_url, attempts=1, timeout=15).decode("utf-8")
        )
        if isinstance(payload, list) and len(payload) >= 2:
            header = payload[0]
            return [
                dict(zip(header, row))
                for row in payload[1:]
                if len(row) == len(header)
                and str(row[4]) == "200"
                and str(row[3]) == "text/html"
            ]
    except Exception:
        pass
    query = urllib.parse.urlencode(
        {
            "url": original_url,
            "output": "json",
            "fl": "timestamp,original,statuscode,mimetype,digest",
            "filter": ["statuscode:200", "mimetype:text/html"],
            "collapse": "digest",
        },
        doseq=True,
    )
    payload = json.loads(
        request_bytes(f"{CDX}?{query}", attempts=1, timeout=20).decode("utf-8")
    )
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    header = payload[0]
    return [dict(zip(header, row)) for row in payload[1:] if len(row) == len(header)]


def fetch_snapshot(plot_id: str, row: dict[str, str]) -> dict[str, Any] | None:
    timestamp = row["timestamp"]
    original = row["original"]
    snapshot_url = f"{ARCHIVE}/{timestamp}id_/{original}"
    html = request_bytes(snapshot_url).decode("utf-8", errors="replace")
    chats, regens = parse_counts(html)
    if chats is None:
        return None
    return {
        "plot_id": plot_id,
        "timestamp": timestamp,
        "chats": chats,
        "regens": regens,
        "source_url": snapshot_url,
    }


def save_snapshot(db, result: dict[str, Any]) -> None:
    plot_id = result["plot_id"]
    timestamp = result["timestamp"]
    chats = result["chats"]
    regens = result["regens"]
    snapshot_url = result["source_url"]
    observed_date = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
    observed_at = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )
    db.execute(
        """
        INSERT INTO plot_observations (
          plot_id, observed_date, observed_at, source,
          interaction_count, interaction_with_regen, source_url
        ) VALUES (?, ?, ?, 'wayback', ?, ?, ?)
        ON CONFLICT(plot_id, observed_date, source) DO UPDATE SET
          observed_at=excluded.observed_at,
          interaction_count=excluded.interaction_count,
          interaction_with_regen=excluded.interaction_with_regen,
          source_url=excluded.source_url
        """,
        (plot_id, observed_date, iso_z(observed_at), chats, regens, snapshot_url),
    )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(root / "data" / "zeta.sqlite3"))
    parser.add_argument("--out", default=str(root / "public" / "data"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--max-captures-per-plot", type=int, default=0)
    parser.add_argument("--plot-id", action="append", default=[])
    parser.add_argument("--discover-domain", type=int, default=0)
    args = parser.parse_args()

    db = connect(Path(args.db))
    errors: list[str] = []
    if args.discover_domain > 0:
        discovered = discover_domain_profiles(args.discover_domain)
        ensure_discovered_plots(db, discovered, args.workers)
        ids = [plot_id for plot_id, _ in discovered]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = db.execute(
                f"SELECT plot_id, name FROM plots WHERE plot_id IN ({placeholders})",
                ids,
            ).fetchall()
        else:
            rows = []
    elif args.plot_id:
        placeholders = ",".join("?" for _ in args.plot_id)
        rows = db.execute(
            f"SELECT plot_id, name FROM plots WHERE plot_id IN ({placeholders})",
            args.plot_id,
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT plot_id, name
            FROM plots
            ORDER BY (wayback_checked_at IS NULL) DESC,
                     COALESCE(last_interaction_count, 0) DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
    checked = saved = 0
    try:
        plot_names = {row["plot_id"]: row["name"] for row in rows}
        captures_by_plot: dict[str, dict[tuple[str, str], dict[str, str]]] = {
            plot_id: {} for plot_id in plot_names
        }
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(cdx_rows, original): (plot_id, original)
                for plot_id in plot_names
                for original in profile_urls(plot_id)
            }
            for future in as_completed(futures):
                plot_id, original = futures[future]
                try:
                    for capture in future.result():
                        key = (capture["timestamp"], capture["original"])
                        captures_by_plot[plot_id][key] = capture
                except Exception as exc:
                    errors.append(f"{plot_id} {original}: {exc}")

        fetch_jobs: list[tuple[str, dict[str, str]]] = []
        for plot_id, captures in captures_by_plot.items():
            ordered = sorted(captures.values(), key=lambda row: row["timestamp"])
            if args.max_captures_per_plot > 0 and len(ordered) > args.max_captures_per_plot:
                if args.max_captures_per_plot == 1:
                    ordered = [ordered[-1]]
                else:
                    step = (len(ordered) - 1) / (args.max_captures_per_plot - 1)
                    ordered = [ordered[round(index * step)] for index in range(args.max_captures_per_plot)]
            fetch_jobs.extend((plot_id, capture) for capture in ordered)
            print(f"index {plot_id}: {len(ordered)} captures")

        best_by_plot_day: dict[tuple[str, str], dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(fetch_snapshot, plot_id, capture): (plot_id, capture)
                for plot_id, capture in fetch_jobs
            }
            for future in as_completed(futures):
                plot_id, capture = futures[future]
                try:
                    result = future.result()
                    if result is None:
                        continue
                    day = result["timestamp"][:8]
                    key = (plot_id, day)
                    current = best_by_plot_day.get(key)
                    if current is None or result["timestamp"] > current["timestamp"]:
                        best_by_plot_day[key] = result
                    time.sleep(args.delay)
                except Exception as exc:
                    errors.append(f"{plot_id} {capture['timestamp']}: {exc}")

        saved_by_plot = {plot_id: 0 for plot_id in plot_names}
        for result in sorted(best_by_plot_day.values(), key=lambda item: item["timestamp"]):
            save_snapshot(db, result)
            saved += 1
            saved_by_plot[result["plot_id"]] += 1
        checked_at = iso_z(datetime.now(timezone.utc))
        for plot_id in plot_names:
            db.execute(
                "UPDATE plots SET wayback_checked_at=? WHERE plot_id=?",
                (checked_at, plot_id),
            )
            checked += 1
            print(
                f"{checked}/{len(rows)} {plot_id}: "
                f"{saved_by_plot[plot_id]} exact captures"
            )
        db.commit()
        build_dashboard(db, Path(args.out), errors)
        print(json.dumps({"checkedPlots": checked, "savedCaptures": saved, "errors": len(errors)}))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
