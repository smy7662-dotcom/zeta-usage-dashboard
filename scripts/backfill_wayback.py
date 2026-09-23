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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from collect import USER_AGENT, build_dashboard, connect, iso_z
except ModuleNotFoundError:  # unittest가 저장소 루트에서 모듈로 불러올 때
    from scripts.collect import USER_AGENT, build_dashboard, connect, iso_z

CDX = "https://web.archive.org/cdx/search/cdx"
ARCHIVE = "https://web.archive.org/web"
NUMBER_PATTERNS = {
    "interaction_count": re.compile(r'(?<!WithRegen)interactionCount(?:\\?"|\\?\')?\s*:\s*(\d+)'),
    "interaction_with_regen": re.compile(r'interactionCountWithRegen(?:\\?"|\\?\')?\s*:\s*(\d+)'),
}


def request_bytes(url: str, attempts: int = 2) -> bytes:
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
            with urllib.request.urlopen(request, timeout=50) as response:
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


def cdx_rows(original_url: str) -> list[dict[str, str]]:
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
    payload: Any = json.loads(request_bytes(f"{CDX}?{query}").decode("utf-8"))
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    header = payload[0]
    return [dict(zip(header, row)) for row in payload[1:] if len(row) == len(header)]


def save_snapshot(db, plot_id: str, row: dict[str, str]) -> bool:
    timestamp = row["timestamp"]
    original = row["original"]
    snapshot_url = f"{ARCHIVE}/{timestamp}id_/{original}"
    html = request_bytes(snapshot_url).decode("utf-8", errors="replace")
    chats, regens = parse_counts(html)
    if chats is None:
        return False
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
    return True


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(root / "data" / "zeta.sqlite3"))
    parser.add_argument("--out", default=str(root / "public" / "data"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--plot-id", action="append", default=[])
    args = parser.parse_args()

    db = connect(Path(args.db))
    errors: list[str] = []
    if args.plot_id:
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
        for plot in rows:
            plot_id = plot["plot_id"]
            plot_saved = 0
            for original in profile_urls(plot_id):
                try:
                    captures = cdx_rows(original)
                    for capture in captures:
                        if save_snapshot(db, plot_id, capture):
                            plot_saved += 1
                            saved += 1
                        time.sleep(args.delay)
                except Exception as exc:
                    errors.append(f"{plot_id} {original}: {exc}")
            db.execute(
                "UPDATE plots SET wayback_checked_at=? WHERE plot_id=?",
                (iso_z(datetime.now(timezone.utc)), plot_id),
            )
            db.commit()
            checked += 1
            print(f"{checked}/{len(rows)} {plot['name']}: {plot_saved} captures")
        build_dashboard(db, Path(args.out), errors)
        print(json.dumps({"checkedPlots": checked, "savedCaptures": saved, "errors": len(errors)}))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
