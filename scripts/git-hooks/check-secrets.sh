#!/bin/sh
# 커밋 전 비밀 파일·키 점검 (2026-10-08 사용자 결정: 커밋·푸시는 상시 허락, 단 이 점검 통과 시만).
# 설치: sh scripts/git-hooks/install.sh  → .git/hooks/pre-commit 이 이 파일을 부른다.
# 수동 실행: sh scripts/git-hooks/check-secrets.sh   (스테이지된 변경만 본다)
# 걸리면 커밋을 막고 파일:줄을 보여준다. 값은 앞 4자리만 표시한다.

fail=0
tab=$(printf '\t')

# 1) 파일 이름 — 비밀이 들어 있는 파일 자체를 막는다
names=$(git diff --cached --name-only --diff-filter=ACMR)
bad_names=$(printf '%s\n' "$names" | grep -E -i \
  -e '(^|/)\.env($|\.)' \
  -e '(^|/)\.[a-z_]*cookie$' \
  -e 'cookie[^/]*\.(txt|json)$' \
  -e 'token[^/]*\.(txt|json)$' \
  -e '(^|/)secrets?(\.json$|/)' \
  -e '\.secrets' \
  -e '\.(session|pem|p12|pfx|key)$' \
  -e '(^|/)id_(rsa|ed25519)' \
  -e '(^|/)\.dev\.vars$' \
  | grep -v -E '(^|/)\.env\.example$')
if [ -n "$bad_names" ]; then
  echo "[비밀 점검] 커밋하면 안 되는 파일:"
  printf '%s\n' "$bad_names" | sed 's/^/  - /'
  fail=1
fi

# 2) 내용 — 추가되는 줄에 키·토큰 모양이 있으면 막는다
pattern='([0-9]{8,10}:AA[A-Za-z0-9_-]{30,})|(ntn_[A-Za-z0-9]{20,})|(secret_[A-Za-z0-9]{30,})|(sk-(ant-)?[A-Za-z0-9_-]{20,})|(gh[pousr]_[A-Za-z0-9]{30,})|(AKIA[0-9A-Z]{16})|(-----BEGIN [A-Z ]*PRIVATE KEY-----)|((KEY|TOKEN|SECRET|APPID|APP_ID|API_KEY|api_key|apiKey|serviceKey|PASSWORD)[A-Za-z_]*[[:space:]]*[:=][[:space:]]*[\x27"][A-Za-z0-9_+/=-]{16,}[\x27"])|(Bearer [A-Za-z0-9._-]{30,})'

hits=$(git diff --cached -U0 --diff-filter=ACMR --no-color | awk -v pat="$pattern" '
  /^\+\+\+ / { f = substr($0, 7); next }
  /^@@/ { split($3, a, ","); ln = substr(a[1], 2) + 0; next }
  /^\+/ { line = substr($0, 2); if (line ~ pat) { print f ":" ln ": " line }; ln++ ; next }
')
if [ -n "$hits" ]; then
  echo "[비밀 점검] 키·토큰으로 보이는 줄:"
  printf '%s\n' "$hits" | sed -E 's/(["\x27:= ])([A-Za-z0-9_]{4})[A-Za-z0-9_+\/=.-]{8,}/\1\2****/g' | cut -c1-200 | sed 's/^/  - /'
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "[비밀 점검] 커밋 중단. 키는 .env 로 옮기고, 비밀 파일은 .gitignore 에 넣은 뒤 다시 커밋."
  echo "  (진짜 키가 아닌 게 확실할 때만: git commit --no-verify)"
  exit 1
fi
exit 0
