# 00:10 KST 정시 수집 — cron-job.org 외부 호출

GitHub 예약 실행은 00:10 KST로 걸어도 2026-09-24~28 실제 시작이 03:31~04:13 KST였음. 같은 저장소의 `workflow_dispatch` 실행은 요청 뒤 8~13초 안에 수집 단계가 시작됐으므로, 외부 서비스가 정시에 실행을 요청하게 함.

## 먼저 확인할 것

- `.github/workflows/collect.yml`에 `mode` 입력(build-only·auto·collect)과 같은 KST 날짜 중복 수집 방지가 들어간 버전이 `main`에 배포돼 있어야 함.
- 그 전에 켜면 00:10 수집 뒤 04시경 GitHub 예약 실행이 다시 수집해 그날 관측 시각을 덮어씀.

## 1. GitHub 토큰 (사용자가 직접 만듦)

- GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token
- Token name: `zeta-cron-dispatch`
- Expiration: 최대 1년, 만료 전 갱신
- Repository access: Only select repositories → `smy7662-dotcom/zeta-usage-dashboard`
- Repository permissions → **Actions: Read and write** (Metadata 읽기는 자동)
  - 근거: 실행 요청 API는 'Actions' 쓰기 권한이 필요함 — [GitHub 권한표](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)
- 이 토큰으로는 코드를 바꿀 수 없지만 실행 요청·취소, 캐시·기록 삭제는 가능하므로 cron-job.org 말고 다른 곳에 두지 않음.

## 2. cron-job.org 작업 (사용자가 계정을 만들고 설정)

| 항목 | 값 |
|---|---|
| URL | `https://api.github.com/repos/smy7662-dotcom/zeta-usage-dashboard/actions/workflows/collect.yml/dispatches` |
| 일정 | 매일 00:10 |
| 시간대 | `Asia/Seoul` |
| Request method | `POST` |
| Header | `Accept: application/vnd.github+json` |
| Header | `Authorization: Bearer <토큰>` |
| Header | `X-GitHub-Api-Version: 2022-11-28` |
| Header | `Content-Type: application/json` |
| Request body | `{"ref":"main","inputs":{"mode":"auto"}}` |

- cron-job.org는 POST·임의 헤더·요청 본문·일정 시간대를 지원하고 무료임 — [FAQ](https://cron-job.org/en/faq/), [REST API 문서](https://docs.cron-job.org/rest-api.html)
- 정시 보장은 없다고 명시돼 있으므로 실제 지연은 cron-job.org 실행 기록의 jitter 값으로 확인함.
- 성공 응답은 현재 문서상 200(실행 정보 포함)임. 예전 방식의 204도 성공임.

## 3. 확인

1. cron-job.org에서 테스트 실행 → 응답 코드 200 또는 204
2. GitHub Actions에 `workflow_dispatch` 실행이 생기고, `Decide whether to collect` 단계가 `collect=true`(그날 첫 수집) 또는 `collect=false`(이미 수집함)를 찍는지 확인
3. 다음 날 대시보드 활동 차트의 관측 시각이 00:1x KST인지 확인
4. 04시경 GitHub 예약 실행은 `already-collected-YYYY-MM-DD`로 수집을 건너뛰고 화면만 다시 배포해야 함

## 전환 비용과 후속 작업

- 첫 정시 수집일은 전날 04시경 관측과의 간격이 약 20시간이라 그날 증분은 계산에서 빠지고 연속 유효일이 1일부터 다시 시작됨.
- 정시 수집이 며칠 이어지면 `scripts/collect.py`의 `TIMING_TOLERANCE_HOURS`를 2.0에서 1.0으로 줄임(사용자 결정 2026-09-28).
- 토큰 만료 시 cron-job.org의 Authorization 헤더만 새 토큰으로 바꿈.
