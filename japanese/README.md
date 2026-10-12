# 제타 관심도 관측소 원자료

`main` 브랜치의 수집기가 매일 수집 뒤 자동으로 커밋하는 원자료 보관 브랜치임.
사이트 코드는 없음.

- `observations/YYYY/YYYY-MM-DD.jsonl.gz`: 그 KST 수집일의 모든 관측행(원천별, 실제 관측 시각 포함)
- `plots/YYYY/YYYY-MM-DD.jsonl.gz`: 그날 관측·상태가 바뀐 플롯의 메타데이터와 태그(뒤 날짜가 앞 날짜를 덮음)
- `wayback/latest.jsonl.gz`: Wayback 계열 관측행 전체
- `state/panel.json`: 고정 측정 패널 구성
- `state/runs.jsonl.gz`: 실행 기록

복원: `main` 체크아웃에서
`python scripts/restore_from_data_branch.py --data-dir <이 브랜치 경로> --db data/zeta.sqlite3`
