#!/usr/bin/env bash
# dishost 배포: 커밋된 파일(git HEAD)만 별도 폴더로 꺼내 올리고 재시작한다.
# dishost push 는 .venv·state.json 같은 로컬 파일을 거르지 않으므로 저장소 폴더에서 바로 push 하지 않는다.
# 서버의 state.json(타이머 목록)과 .env 는 건드리지 않는다(--clean 을 쓰지 않음).
# 처음 한 번: dishost login → 이 스크립트 실행 시 연결 안 됐으면 dishost link 를 대신 실행한다.
set -euo pipefail

BOT_NAME="${BOT_NAME:-DBot}"
REPO="$(cd "$(dirname "$0")" && pwd)"
OUT="${DEPLOY_DIR:-$HOME/.dbot-deploy}"

case "$OUT" in ""|/|"$HOME"|"$REPO") echo "[deploy] 위험한 DEPLOY_DIR: $OUT"; exit 1;; esac
mkdir -p "$OUT"
find "$OUT" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
git -C "$REPO" archive HEAD -- bot.py requirements.txt | tar -x -C "$OUT"
git -C "$REPO" rev-parse --short HEAD > "$OUT/version.txt"   # 보드 꼬리말 v<해시>
echo "[deploy] $(git -C "$REPO" log --oneline -1) → $OUT"
ls "$OUT"

cd "$OUT"
if ! dishost push; then
  echo "[deploy] 연결된 봇이 없으면 dishost link $BOT_NAME 후 다시 실행"; exit 1
fi
dishost restart "$BOT_NAME"
