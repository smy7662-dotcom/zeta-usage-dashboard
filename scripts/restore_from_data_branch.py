#!/usr/bin/env python3
"""data 브랜치 원자료로 수집 DB를 다시 만듦.

Actions 캐시가 사라졌을 때 쓰는 복구 경로다. 날짜순으로 플롯 메타데이터를
덮어쓰고, 모든 관측행·Wayback 행·고정 패널·실행 기록을 되살린다. 홈 캡처
요약은 main 브랜치의 archive/wayback-observations.json이 수집기 실행 때마다
복원한다.

예시:
  python scripts/restore_from_data_branch.py --data-dir datastore --db data/zeta.sqlite3
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

try:
    from collect import connect
    from export_data_branch import OBSERVATION_FIELDS, PLOT_FIELDS
except ModuleNotFoundError:  # unittest가 저장소 루트에서 모듈로 불러올 때
    from scripts.collect import connect
    from scripts.export_data_branch import OBSERVATION_FIELDS, PLOT_FIELDS


def read_gzip_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def ensure_plot(db: sqlite3.Connection, plot_id: str, seen_at: str) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO plots (plot_id, name, first_seen_at, last_seen_at)
        VALUES (?, '이름 없음', ?, ?)
        """,
        (plot_id, seen_at, seen_at),
    )


def upsert_observation(db: sqlite3.Connection, row: dict[str, Any]) -> None:
    ensure_plot(db, row["plot_id"], row["observed_at"])
    db.execute(
        f"""
        INSERT INTO plot_observations ({", ".join(OBSERVATION_FIELDS)})
        VALUES ({", ".join("?" for _ in OBSERVATION_FIELDS)})
        ON CONFLICT(plot_id, observed_date, source) DO UPDATE SET
          observed_at=excluded.observed_at,
          interaction_count=excluded.interaction_count,
          interaction_with_regen=excluded.interaction_with_regen,
          comment_count=excluded.comment_count,
          source_url=excluded.source_url
        """,
        tuple(row.get(field) for field in OBSERVATION_FIELDS),
    )


def restore(data_dir: Path, db_path: Path) -> dict[str, int]:
    if db_path.exists():
        raise SystemExit(f"{db_path} already exists; restore into a new path")
    db = connect(db_path)
    counts = {"plots": 0, "observations": 0, "wayback": 0, "panel": 0, "runs": 0}

    # 플롯 메타데이터는 날짜순으로 덮어써 가장 최근 상태를 남김.
    # INSERT OR REPLACE는 연결된 관측행까지 지우므로 쓰지 않음.
    for path in sorted((data_dir / "plots").rglob("*.jsonl.gz")):
        for record in read_gzip_jsonl(path):
            fields = [field for field in PLOT_FIELDS if field in record]
            updates = ", ".join(f"{field}=excluded.{field}" for field in fields if field != "plot_id")
            db.execute(
                f"""
                INSERT INTO plots ({", ".join(fields)})
                VALUES ({", ".join("?" for _ in fields)})
                ON CONFLICT(plot_id) DO UPDATE SET {updates}
                """,
                tuple(record.get(field) for field in fields),
            )
            for tag in record.get("tags") or []:
                db.execute(
                    "INSERT OR IGNORE INTO plot_tags(plot_id, tag) VALUES (?, ?)",
                    (record["plot_id"], tag),
                )
                db.execute("INSERT OR IGNORE INTO tag_queue(tag) VALUES (?)", (tag,))
            counts["plots"] += 1

    for path in sorted((data_dir / "observations").rglob("*.jsonl.gz")):
        for row in read_gzip_jsonl(path):
            upsert_observation(db, row)
            counts["observations"] += 1

    wayback = data_dir / "wayback" / "latest.jsonl.gz"
    if wayback.exists():
        for row in read_gzip_jsonl(wayback):
            upsert_observation(db, row)
            counts["wayback"] += 1

    panel = data_dir / "state" / "panel.json"
    if panel.exists():
        for row in json.loads(panel.read_text(encoding="utf-8")):
            ensure_plot(db, row["plot_id"], row["selected_at"])
            db.execute(
                """
                INSERT OR REPLACE INTO measurement_panel (
                  plot_id, segment, selected_at, selected_date, active
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (row["plot_id"], row["segment"], row["selected_at"], row["selected_date"], row["active"]),
            )
            counts["panel"] += 1

    runs = data_dir / "state" / "runs.jsonl.gz"
    if runs.exists():
        for row in read_gzip_jsonl(runs):
            columns = list(row.keys())
            db.execute(
                f"INSERT OR REPLACE INTO runs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                tuple(row[column] for column in columns),
            )
            counts["runs"] += 1
    db.commit()
    db.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    print(json.dumps(restore(Path(args.data_dir), Path(args.db)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
