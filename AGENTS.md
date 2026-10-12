# Zeta 관심도 관측소

제타의 공개 플롯·태그·댓글 데이터를 일별로 수집하고 공개 대시보드로 보여주는 프로젝트임.

## 원칙

- 화면 반올림값이 아니라 API 원값을 저장함.
- `interactionCount`를 대표 대화량으로 사용하고 `interactionCountWithRegen`을 별도 보존함.
- 누락일을 0으로 채우거나 보간하지 않음. 차트는 실제 관측점만 표시하고 점 사이만 연결함.
- 플랫폼 합계는 고유 `plot_id`로 중복 제거함. 태그 합계를 다시 더해 플랫폼 합계로 쓰지 않음.
- 초기 전수조사 중에는 확보 플롯·태그·지표 커버리지를 항상 표시함.
- Wayback 캡처 존재와 숫자 원문 확인을 구분함. HTML 안의 정확한 숫자가 확인된 캡처만 저장함.
- 댓글 수는 원댓글과 답글을 합산한 공개 API 표시값을 대표값으로 사용함.
- 좋아요는 안정적인 무인 수집이 확인될 때만 추가하고, 실패값을 0으로 저장하지 않음.
- 관측 시각이 다른 두 날의 증분을 같은 24시간 증가량처럼 비교하지 않음. 증분은 실제 간격으로 24시간 환산하고 24±2시간 밖은 추세 계산에서 뺌.
- 늦게 발견한 100만+ 플롯은 이용 성장이 아니므로 실제 돌파와 합치지 않음.
- 404·410은 0이 아니라 사라진 플롯으로 기록함.

## 주요 명령

```powershell
python scripts/collect.py
python scripts/backfill_wayback.py --limit 20
python -m unittest scripts/test_collect.py
python scripts/export_wayback_archive.py --db data/zeta.sqlite3
python scripts/import_otoscor_history.py --out archive/otoscor-zeta-observations.json
python scripts/restore_from_data_branch.py --data-dir <data 브랜치 체크아웃> --db <새 DB 경로>
.\run.ps1
```

## 파일

- `data/zeta.sqlite3`: 재개 가능한 로컬 수집 DB. Git에는 포함하지 않음.
- `public/data/dashboard.json`: 공개 대시보드용 압축 집계값.
- `public/data/status.json`: 수집 상태·오류·커버리지.
- `archive/wayback-observations.json`: Wayback 원천별 전체 행과 홈 요약. 매 실행 전 멱등 복원하므로 수동 백필 뒤에는 이 파일을 갱신해 체크인함.
- `archive/otoscor-zeta-observations.json`: 외부 공개 Git 이력에서 검증한 API 기반 TRENDING 상위 30 과거 원값. 플랫폼 합계가 아니라 개별 플롯 시계열에만 사용함.
- `data` 브랜치: 매일 수집 뒤 워크플로가 커밋하는 원자료(날짜별 관측행·플롯 메타데이터·패널·실행 기록). 캐시가 사라지면 여기서 복원함.
- `docs/ops/external-trigger.md`: 00:10 KST 정시 호출(cron-job.org) 설정 절차.
- `docs/specs/2026-09-23-zeta-usage-observatory.md`: 승인 설계 정본.

