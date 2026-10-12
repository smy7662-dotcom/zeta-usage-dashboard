#!/bin/sh
# 이 저장소에 커밋 전 비밀 점검을 건다. 새로 clone 한 컴퓨터에서 한 번 실행.
# 작업 트리(worktree)가 여러 개여도 훅 폴더는 하나라 --git-path 로 찾음.
hooks=$(git rev-parse --git-path hooks) || exit 1
mkdir -p "$hooks"
cat > "$hooks/pre-commit" <<'HOOK'
#!/bin/sh
# 점검 스크립트가 아직 없는 옛 브랜치 작업 트리에서는 건너뜀
script="$(git rev-parse --show-toplevel)/scripts/git-hooks/check-secrets.sh"
if [ -f "$script" ]; then
  exec sh "$script"
fi
echo "[비밀 점검] 이 브랜치엔 점검 스크립트가 없어 건너뜀"
HOOK
chmod +x "$hooks/pre-commit" 2>/dev/null
echo "설치됨: $hooks/pre-commit"
