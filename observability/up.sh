#!/bin/bash
# 로컬 관측성 스택(Tempo·Prometheus·Alertmanager·Grafana)을 띄운다.
# Alertmanager 설정은 .env의 LOG_BOT_TOKEN·LOG_CHAT_WARNING으로 저장소 밖에 만든다(미러에 안 나감).
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="${ENV_FILE:-$HOME/.config/telegram-bot/.env}"
export OBS_SECRETS_DIR="${OBS_SECRETS_DIR:-$HOME/.config/telegram-bot/observability}"

token=$(grep -E '^LOG_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"' ')
chat=$(grep -E '^LOG_CHAT_WARNING=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"' ')
if [ -z "$token" ] || ! [[ "$chat" =~ ^-?[0-9]+$ ]]; then
  echo "LOG_BOT_TOKEN이 없거나 LOG_CHAT_WARNING이 숫자 ID가 아님 → 중단" >&2
  exit 1
fi

mkdir -p "$OBS_SECRETS_DIR"
chmod 700 "$OBS_SECRETS_DIR"
umask 077
printf '%s' "$token" > "$OBS_SECRETS_DIR/bot_token"
sed "s/__LOG_CHAT_WARNING__/$chat/" alertmanager.yml.tmpl > "$OBS_SECRETS_DIR/alertmanager.yml"
# 컨테이너 안 Alertmanager(nobody)가 읽을 수 있게 파일만 읽기 허용(폴더는 700이라 맥의 다른 사용자는 못 들어옴)
chmod 644 "$OBS_SECRETS_DIR/bot_token" "$OBS_SECRETS_DIR/alertmanager.yml"

docker compose up -d "$@"
docker compose ps
