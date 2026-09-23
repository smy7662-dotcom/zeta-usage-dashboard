# 제타 관심도 관측소

제타의 공개 플롯·태그·댓글 원값을 날짜별로 수집하고, 플랫폼 전체 관심도에서 태그와 플롯까지 내려가 변화 원인을 확인하는 정적 대시보드임.

## 실행

```powershell
python scripts/collect.py
python scripts/backfill_wayback.py --limit 20
python -m unittest scripts/test_collect.py
.\run.ps1
```

브라우저에서 `http://127.0.0.1:8878`을 열면 됨.

## 현재 수집 원천

- [플롯 랭킹 API](https://api.zeta-ai.io/v1/plots/ranking?type=GLOBAL&limit=100&gender=ALL)
- [디스커버리 API](https://api.zeta-ai.io/v1/discovery-tab)
- [태그 검색 API](https://api.zeta-ai.io/v2/plots/search?keyword=%23zeta&limit=50&order=LATEST)
- [댓글 수 API](https://api.zeta-ai.io/v1/plots/7ca5d04c-e2e0-425a-9dfd-e4cf0cdf7878/comments/count)
- Wayback 플롯 프로필 원문

## 데이터 정책

- 정확한 API 원값만 저장함.
- 결측일을 0으로 채우거나 보간하지 않음.
- 초기 전수조사 중에는 확보 범위를 화면에 표시함.
- 플랫폼 총량은 고유 플롯 ID로 중복 제거함.
- 좋아요는 안정적인 자동 수집이 확인될 때만 추가함.

