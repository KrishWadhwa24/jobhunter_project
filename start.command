#!/bin/bash
# Double-click this file in Finder. It sets everything up on this Mac and starts Jobhunter.
# Reads your existing Docker Postgres/Redis settings locally; nothing is printed or sent anywhere.
cd "$(dirname "$0")" || exit 1
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/6  Checking Docker"
if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop, wait until it says running, then double-click this file again."; read -r -p "Press Enter to close"; exit 1
fi

if [ ! -f .env ]; then cp .env.example .env; fi
chmod 600 .env

setenv() {  # setenv KEY VALUE  (edits .env in place)
  if grep -q "^$1=" .env; then python3 - "$1" "$2" <<'PY'
import sys,re
k,v=sys.argv[1],sys.argv[2]
s=open('.env').read()
s=re.sub(r'^%s=.*$'%re.escape(k), lambda m:'%s=%s'%(k,v), s, flags=re.M)
open('.env','w').write(s)
PY
  else echo "$1=$2" >> .env; fi
}

say "2/6  Finding your Postgres and Redis containers"
cur_db=$(grep '^DATABASE_URL=' .env | cut -d= -f2-)
cur_redis=$(grep '^REDIS_URL=' .env | cut -d= -f2-)
if [ -z "$cur_db" ] || make -s doctor 2>&1 | grep -q "NOT reachable"; then
  ./fix-db.sh || { read -r -p "Database setup failed. Press Enter to close"; exit 1; }
fi
if [ -z "$cur_redis" ]; then
  RD=$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /redis|valkey/ {print $1; exit}')
  if [ -n "$RD" ]; then
    RPORT=$(docker port "$RD" 6379/tcp 2>/dev/null | head -1 | sed 's/.*://')
    [ -n "$RPORT" ] && setenv REDIS_URL "redis://127.0.0.1:$RPORT/1" && echo "Redis: container $RD on port $RPORT"
  else echo "No Redis container found; continuing without Redis (optional)."; fi
fi

say "3/6  Telegram bot token"
if ! grep -q '^TELEGRAM_BOT_TOKEN=.\+' .env; then
  echo "Create a NEW token with @BotFather (/revoke the old one), then paste it here (hidden):"
  read -r -s TOK; echo
  [ -n "$TOK" ] && setenv TELEGRAM_BOT_TOKEN "$TOK"
fi

say "4/6  Installing (first time takes a minute)"
make db-create || { read -r -p "Database step failed. Press Enter to close"; exit 1; }
make setup || { read -r -p "Setup failed. Press Enter to close"; exit 1; }
make doctor

say "5/6  Telegram chat id"
if ! grep -q '^TELEGRAM_CHAT_ID=.\+' .env && grep -q '^TELEGRAM_BOT_TOKEN=.\+' .env; then
  echo "Open your bot in Telegram and send /start, then press Enter here."; read -r
  make chat-id
fi

say "6/6  Starting"
make service-start
sleep 2
open "http://127.0.0.1:8787" 2>/dev/null
echo; echo "Dashboard: http://127.0.0.1:8787     Stop with: make service-stop"
read -r -p "Press Enter to close this window (the service keeps running)"
