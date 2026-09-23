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

## 주요 명령

```powershell
python scripts/collect.py
python scripts/backfill_wayback.py --limit 20
python -m unittest scripts/test_collect.py
.\run.ps1
```

## 파일

- `data/zeta.sqlite3`: 재개 가능한 로컬 수집 DB. Git에는 포함하지 않음.
- `public/data/dashboard.json`: 공개 대시보드용 압축 집계값.
- `public/data/status.json`: 수집 상태·오류·커버리지.
- `docs/specs/2026-09-23-zeta-usage-observatory.md`: 승인 설계 정본.

