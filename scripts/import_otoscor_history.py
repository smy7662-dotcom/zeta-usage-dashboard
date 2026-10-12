#!/usr/bin/env python3
"""Otoscor/chatbotmonitoring의 Zeta API 스냅샷을 정규화함.

초기 커밋에는 HTML 반올림값과 샘플 데이터가 섞여 있으므로, Zeta 수집기가
공개 API로 전환된 4490d3c 이후의 스냅샷만 사용한다. 같은 KST 날짜에 여러
스냅샷이 있으면 가장 늦은 관측만 남긴다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPOSITORY = "https://github.com/Otoscor/chatbotmonitoring.git"
SOURCE_WEB = "https://github.com/Otoscor/chatbotmonitoring"
DATA_PATH = "frontend/public/data/chat_characters.json"
API_START_COMMIT = "4490d3c"
SOURCE = "external:otoscor-zeta"
KST = timezone(timedelta(hours=9))


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 원 저장소의 crawled_at은 UTC를 timezone 없이 직렬화한 값임. 커밋 시각과
    # 일관되게 9시간 차이가 나므로 UTC로 해석하고 KST 날짜를 따로 계산함.
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def tags(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip().casefold() for item in value if str(item).strip()})


def build_archive(repo: Path) -> dict[str, Any]:
    start = git(repo, "rev-parse", API_START_COMMIT)
    commits = git(
        repo,
        "log",
        "--format=%H",
        "--reverse",
        f"{start}^..HEAD",
        "--",
        DATA_PATH,
    ).splitlines()

    snapshots: dict[str, dict[str, Any]] = {}
    for sha in commits:
        try:
            payload = json.loads(git(repo, "show", f"{sha}:{DATA_PATH}"))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
        rows = [row for row in payload if str(row.get("service", "")).casefold() == "zeta"]
        if not rows:
            continue
        observed_at = max(parse_utc(str(row["crawled_at"])) for row in rows)
        day = observed_at.astimezone(KST).date().isoformat()
        candidate = {"sha": sha, "observed_at": observed_at, "rows": rows}
        if day not in snapshots or observed_at > snapshots[day]["observed_at"]:
            snapshots[day] = candidate

    plots: dict[str, dict[str, Any]] = {}
    observations: dict[tuple[str, str], dict[str, Any]] = {}
    for day, snapshot in sorted(snapshots.items()):
        sha = snapshot["sha"]
        observed_at = snapshot["observed_at"]
        source_url = f"{SOURCE_WEB}/blob/{sha}/{DATA_PATH}"
        for row in snapshot["rows"]:
            plot_id = str(row.get("character_id") or "").strip()
            chats = row.get("views")
            if not plot_id or not isinstance(chats, int) or chats < 0:
                continue
            seen_at = observed_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            plot = plots.setdefault(
                plot_id,
                {
                    "id": plot_id,
                    "name": str(row.get("name") or "이름 없음"),
                    "creator": row.get("author"),
                    "mode": "ZETA",
                    "tags": tags(row.get("tags")),
                    "firstSeenAt": seen_at,
                    "lastSeenAt": seen_at,
                },
            )
            plot["name"] = str(row.get("name") or plot["name"])
            plot["creator"] = row.get("author") or plot.get("creator")
            plot["tags"] = sorted(set(plot.get("tags", [])) | set(tags(row.get("tags"))))
            plot["lastSeenAt"] = seen_at
            observation = {
                    "plotId": plot_id,
                    "date": day,
                    "observedAt": seen_at,
                    "source": SOURCE,
                    "chats": chats,
                    "rank": int(row.get("rank") or 0) or None,
                    "rankingType": "TRENDING",
                    "sourceUrl": source_url,
                    "sourceCommit": sha,
                }
            key = (plot_id, day)
            previous = observations.get(key)
            # 원 응답에 같은 ID가 중복되면 더 높은 순위(작은 rank) 한 행만 보존함.
            if previous is None or (observation["rank"] or 10_000) < (previous["rank"] or 10_000):
                observations[key] = observation

    days = sorted(snapshots)
    return {
        "schemaVersion": 1,
        "source": {
            "repository": SOURCE_WEB,
            "license": "MIT",
            "metric": "interactionCount",
            "rankingType": "TRENDING",
            "cohort": "dynamic top 30",
            "apiStartCommit": start,
            "selection": "latest snapshot per KST date",
        },
        "coverage": {
            "firstDate": days[0] if days else None,
            "lastDate": days[-1] if days else None,
            "dates": len(days),
            "plots": len(plots),
            "observations": len(observations),
        },
        "caveats": [
            "동적 TRENDING 상위 30 표본이며 플랫폼 전체 합계가 아님",
            "4490d3c 이전 HTML 반올림값·샘플 데이터는 제외함",
            "재생성 포함 대화량과 댓글 수는 원 저장소에 없어 비워 둠",
        ],
        "plots": sorted(plots.values(), key=lambda row: row["id"]),
        "observations": sorted(
            observations.values(),
            key=lambda row: (row["date"], row["rank"] or 10_000, row["plotId"]),
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", help="이미 클론된 chatbotmonitoring 저장소")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    if args.repo:
        archive = build_archive(Path(args.repo))
    else:
        with tempfile.TemporaryDirectory(prefix="otoscor-zeta-") as temp:
            repo = Path(temp) / "repo"
            subprocess.run(["git", "clone", "--quiet", REPOSITORY, str(repo)], check=True)
            archive = build_archive(repo)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(archive, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(archive["coverage"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
