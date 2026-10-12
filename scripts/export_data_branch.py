#!/usr/bin/env python3
"""수집 DB의 원자료를 data 브랜치용 날짜별 파일로 내보냄.

Actions 캐시는 7일 쓰지 않으면 지워지고 공개 JSON은 플롯-날짜당 대표 1행만
담으므로, 원천별 모든 관측행과 실제 관측 시각을 날짜별 gzip JSONL로 영구
보존한다. 같은 내용이면 같은 바이트가 나오게 써서 git 변경을 만들지 않는다.

구조:
  observations/YYYY/YYYY-MM-DD.jsonl.gz  그 KST 수집일의 모든 관측행(Wayback 제외)
  plots/YYYY/YYYY-MM-DD.jsonl.gz         그날 관측·상태가 바뀐 플롯의 메타데이터와 태그
  wayback/latest.jsonl.gz                Wayback 계열 관측행 전체(과거 날짜로 계속 추가됨)
  state/panel.json                       고정 측정 패널 구성
  state/runs.jsonl.gz                    실행 기록

예시:
  python scripts/export_data_branch.py --db data/zeta.sqlite3 --out datastore --all
  python scripts/export_data_branch.py --db data/zeta.sqlite3 --out datastore
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

KST = timezone(timedelta(hours=9))
WAYBACK_SOURCES = ("wayback", "wayback-home", "wayback-api")
OBSERVATION_FIELDS = (
    "plot_id",
    "observed_date",
    "observed_at",
    "source",
    "interaction_count",
    "interaction_with_regen",
    "comment_count",
    "source_url",
)
PLOT_FIELDS = (
    "plot_id",
    "name",
    "creator_id",
    "creator_username",
    "mode",
    "metric_kind",
    "released_at",
    "updated_at",
    "first_seen_at",
    "last_seen_at",
    "last_interaction_count",
    "last_interaction_with_regen",
    "last_comment_count",
    "comment_observed_at",
    "wayback_checked_at",
    "missing_since",
    "missing_date",
    "missing_checked_at",
)

README = """# 제타 관심도 관측소 원자료

`main` 브랜치의 수집기가 매일 수집 뒤 자동으로 커밋하는 원자료 보관 브랜치임.
사이트 코드는 없음.

- `observations/YYYY/YYYY-MM-DD.jsonl.gz`: 그 KST 수집일의 모든 관측행(원천별, 실제 관측 시각 포함)
- `plots/YYYY/YYYY-MM-DD.jsonl.gz`: 그날 관측·상태가 바뀐 플롯의 메타데이터와 태그(뒤 날짜가 앞 날짜를 덮음)
- `wayback/latest.jsonl.gz`: Wayback 계열 관측행 전체
- `state/panel.json`: 고정 측정 패널 구성
- `state/runs.jsonl.gz`: 실행 기록

복원: `main` 체크아웃에서
`python scripts/restore_from_data_branch.py --data-dir <이 브랜치 경로> --db data/zeta.sqlite3`
"""


def kst_day_of(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed.astimezone(KST).strftime("%Y-%m-%d")


def write_gzip_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """같은 행이면 같은 바이트가 나오도록 gzip 시각 정보를 0으로 고정함."""
    buffer = io.BytesIO()
    count = 0
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as handle:
        for row in rows:
            handle.write((json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            count += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    data = buffer.getvalue()
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)
    return count


def read_gzip_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def merge_gzip_jsonl(path: Path, rows: list[dict[str, Any]], key_fields: tuple[str, ...]) -> int:
    """같은 날짜 파일을 다시 쓸 때 기존 행을 지우지 않고 합침(새 값이 우선)."""
    merged = {
        tuple(row.get(field) for field in key_fields): row
        for row in read_gzip_jsonl(path)
    }
    for row in rows:
        merged[tuple(row.get(field) for field in key_fields)] = row
    ordered = [merged[key] for key in sorted(merged, key=lambda item: tuple(str(value) for value in item))]
    return write_gzip_jsonl(path, ordered)


def write_text_if_changed(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def observation_rows(db: sqlite3.Connection, day: str) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in WAYBACK_SOURCES)
    return [
        {field: row[field] for field in OBSERVATION_FIELDS}
        for row in db.execute(
            f"""
            SELECT * FROM plot_observations
            WHERE observed_date=? AND source NOT IN ({placeholders})
            ORDER BY plot_id, source
            """,
            (day, *WAYBACK_SOURCES),
        )
    ]


def plot_rows(db: sqlite3.Connection, plot_ids: Iterable[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for plot_id in sorted(set(plot_ids)):
        row = db.execute("SELECT * FROM plots WHERE plot_id=?", (plot_id,)).fetchone()
        if row is None:
            continue
        record = {field: row[field] for field in PLOT_FIELDS if field in row.keys()}
        record["tags"] = [
            value[0]
            for value in db.execute(
                "SELECT tag FROM plot_tags WHERE plot_id=? ORDER BY tag", (plot_id,)
            )
        ]
        rows.append(record)
    return rows


def export(db_path: Path, out: Path, *, export_all: bool, day: str | None) -> dict[str, Any]:
    try:
        db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        db.execute("SELECT 1 FROM plots LIMIT 1")
    except sqlite3.OperationalError:
        # WAL 공유 메모리 파일을 읽기 전용으로 만들 수 없는 환경이면 일반 연결로 읽기만 함
        db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in WAYBACK_SOURCES)
    current_days = [
        row[0]
        for row in db.execute(
            f"""
            SELECT DISTINCT observed_date FROM plot_observations
            WHERE source NOT IN ({placeholders})
            ORDER BY observed_date
            """,
            WAYBACK_SOURCES,
        )
    ]
    first_export = export_all or not (out / "observations").exists()
    if day:
        days = [day]
    elif first_export:
        days = current_days
    else:
        latest_run = db.execute(
            "SELECT started_at FROM runs WHERE success=1 ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        run_day = kst_day_of(latest_run["started_at"]) if latest_run else None
        days = [run_day] if run_day else current_days[-1:]

    summary: dict[str, Any] = {"days": {}, "plots": 0}
    touched: set[str] = set()
    for export_day in days:
        rows = observation_rows(db, export_day)
        if not rows:
            continue
        year = export_day[:4]
        summary["days"][export_day] = merge_gzip_jsonl(
            out / "observations" / year / f"{export_day}.jsonl.gz",
            rows,
            ("plot_id", "observed_date", "source"),
        )
        touched.update(row["plot_id"] for row in rows)
        touched.update(
            row[0]
            for row in db.execute(
                "SELECT plot_id FROM plots WHERE missing_date=?", (export_day,)
            )
        )

    if days:
        # 첫 내보내기는 모든 플롯의 현재 메타데이터를 마지막 날짜 파일 하나에 담음
        plot_ids = (
            [row[0] for row in db.execute("SELECT plot_id FROM plots")]
            if first_export
            else touched
        )
        plots_day = days[-1]
        summary["plots"] = merge_gzip_jsonl(
            out / "plots" / plots_day[:4] / f"{plots_day}.jsonl.gz",
            plot_rows(db, plot_ids),
            ("plot_id",),
        )

    wayback = [
        {field: row[field] for field in OBSERVATION_FIELDS}
        for row in db.execute(
            f"""
            SELECT * FROM plot_observations
            WHERE source IN ({placeholders})
            ORDER BY observed_date, source, plot_id
            """,
            WAYBACK_SOURCES,
        )
    ]
    summary["wayback"] = merge_gzip_jsonl(
        out / "wayback" / "latest.jsonl.gz", wayback, ("plot_id", "observed_date", "source")
    )

    panel = [
        dict(row)
        for row in db.execute(
            "SELECT plot_id, segment, selected_at, selected_date, active FROM measurement_panel ORDER BY plot_id"
        )
    ]
    write_text_if_changed(
        out / "state" / "panel.json",
        json.dumps(panel, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
    )
    runs = [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY run_id")]
    summary["runs"] = merge_gzip_jsonl(out / "state" / "runs.jsonl.gz", runs, ("run_id",))
    write_text_if_changed(out / "README.md", README)
    summary["panel"] = len(panel)
    db.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--all", action="store_true", help="모든 수집일을 다시 내보냄")
    parser.add_argument("--date", help="특정 KST 수집일만 내보냄(YYYY-MM-DD)")
    args = parser.parse_args()
    summary = export(Path(args.db), Path(args.out), export_all=args.all, day=args.date)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
