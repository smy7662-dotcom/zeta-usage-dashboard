#!/usr/bin/env python3
"""Wayback에 보관된 제타 API JSON에서 플롯별 과거 대화수를 복원함."""

from __future__ import annotations

import argparse
import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from backfill_wayback import ARCHIVE, CDX, request_bytes
    from collect import build_dashboard, connect, ingest_payload, iso_z
except ModuleNotFoundError:
    from scripts.backfill_wayback import ARCHIVE, CDX, request_bytes
    from scripts.collect import build_dashboard, connect, ingest_payload, iso_z


DETAIL_API = re.compile(
    r"^https?://api\.zeta-ai\.io/v1/(characters|plots)/([0-9a-f-]{36})/?$",
    re.IGNORECASE,
)
RANKING_API = re.compile(
    r"^https?://api\.zeta-ai\.io/v1/(characters|plots)/ranking\?",
    re.IGNORECASE,
)


def cdx_query(url: str, *, match_type: str | None = None) -> list[dict[str, str]]:
    params: dict[str, Any] = {
        "url": url,
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest",
        "filter": ["statuscode:200", "mimetype:application/json"],
        "collapse": "digest",
        "from": "2024",
        "limit": "10000",
    }
    if match_type:
        params["matchType"] = match_type
    query = urllib.parse.urlencode(params, doseq=True)
    payload = json.loads(request_bytes(f"{CDX}?{query}", attempts=3, timeout=90))
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    header = payload[0]
    return [dict(zip(header, row)) for row in payload[1:] if len(row) == len(header)]


def api_captures() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for prefix in (
        "api.zeta-ai.io/v1/characters/",
        "api.zeta-ai.io/v1/plots/",
    ):
        rows.extend(cdx_query(prefix, match_type="prefix"))
    for pattern in (
        "api.zeta-ai.io/v1/characters/ranking*",
        "api.zeta-ai.io/v1/plots/ranking*",
    ):
        rows.extend(cdx_query(pattern))

    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in rows:
        original = row["original"]
        if not (DETAIL_API.match(original) or RANKING_API.match(original)):
            continue
        key = (row["timestamp"], original, row["digest"])
        unique[key] = row
    return sorted(unique.values(), key=lambda row: row["timestamp"])


def fetch_capture(row: dict[str, str]) -> dict[str, Any]:
    url = archive_url(row)
    payload = json.loads(request_bytes(url, attempts=3, timeout=75).decode("utf-8"))
    return {"row": row, "url": url, "payload": payload}


def archive_url(row: dict[str, str]) -> str:
    return f"{ARCHIVE}/{row['timestamp']}id_/{row['original']}"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(root / "data" / "zeta.sqlite3"))
    parser.add_argument("--out", default=str(root / "public" / "data"))
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    all_captures = api_captures()
    db = connect(Path(args.db))
    existing_urls = {
        row[0]
        for row in db.execute(
            "SELECT DISTINCT source_url FROM plot_observations WHERE source='wayback-api'"
        ).fetchall()
    }
    captures = [row for row in all_captures if archive_url(row) not in existing_urls]
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(fetch_capture, row): row for row in captures}
        for future in as_completed(futures):
            row = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append(f"{row['timestamp']} {row['original']}: {exc}")

    ingested = 0
    try:
        for result in sorted(results, key=lambda item: item["row"]["timestamp"]):
            timestamp = result["row"]["timestamp"]
            observed = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )
            found, _ = ingest_payload(
                db,
                result["payload"],
                observed_at=iso_z(observed),
                observed_date=f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}",
                source="wayback-api",
                source_url=result["url"],
            )
            ingested += found
        db.commit()
        build_dashboard(db, Path(args.out), errors)
    finally:
        db.close()

    print(
        json.dumps(
            {
                "indexedApiCaptures": len(all_captures),
                "previouslyFetched": len(all_captures) - len(captures),
                "pendingApiCaptures": len(captures),
                "fetchedApiCaptures": len(results),
                "ingestedPlotRecords": ingested,
                "errors": len(errors),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
