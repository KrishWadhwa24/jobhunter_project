#!/bin/bash
# Creates a dedicated "jobhunter" role + database inside your existing Postgres container and writes DATABASE_URL to .env.
# Connects from inside the container (no existing password needed). Does not touch any other database.
cd "$(dirname "$0")" || exit 1
PG=$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /postgres/ {print $1; exit}')
[ -z "$PG" ] && { echo "No running Postgres container found."; exit 1; }
PORT=$(docker port "$PG" 5432/tcp 2>/dev/null | head -1 | sed 's/.*://')
[ -z "$PORT" ] && { echo "Container $PG does not publish port 5432."; exit 1; }
SU=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$PG" | sed -n 's/^POSTGRES_USER=//p'); SU=${SU:-postgres}
PASS=$(python3 -c 'import secrets;print(secrets.token_hex(16))')
psql_in() { docker exec -i "$PG" psql -v ON_ERROR_STOP=1 -U "$SU" -d postgres -tA "$@"; }

if [ "$(psql_in -c "SELECT 1 FROM pg_roles WHERE rolname='jobhunter'")" = "1" ]; then
  psql_in -c "ALTER ROLE jobhunter WITH LOGIN PASSWORD '$PASS'" >/dev/null || exit 1
else
  psql_in -c "CREATE ROLE jobhunter WITH LOGIN PASSWORD '$PASS'" >/dev/null || exit 1
fi
if [ "$(psql_in -c "SELECT 1 FROM pg_database WHERE datname='jobhunter'")" != "1" ]; then
  psql_in -c "CREATE DATABASE jobhunter OWNER jobhunter ENCODING 'UTF8'" >/dev/null || exit 1
fi

[ -f .env ] || cp .env.example .env
python3 - "$PORT" "$PASS" <<'PY'
import sys,re
port,pw=sys.argv[1],sys.argv[2]
s=open('.env').read()
url='DATABASE_URL=postgresql://jobhunter:%s@127.0.0.1:%s/jobhunter'%(pw,port)
s=re.sub(r'^DATABASE_URL=.*$',lambda m:url,s,flags=re.M) if re.search(r'^DATABASE_URL=',s,flags=re.M) else s+'\n'+url+'\n'
open('.env','w').write(s)
PY
chmod 600 .env
echo "Done: role and database 'jobhunter' ready in container $PG (port $PORT). .env updated."
