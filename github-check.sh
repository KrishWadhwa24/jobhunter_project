#!/usr/bin/env bash
# Diagnoses GitHub Models access. Reads GITHUB_TOKEN from .env, never prints it.
cd "$(dirname "$0")"
TOKEN=$(grep -E '^GITHUB_TOKEN=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
[ -z "$TOKEN" ] && { echo "GITHUB_TOKEN is empty in .env"; exit 1; }
echo "token prefix: ${TOKEN:0:11}...  length: ${#TOKEN}"
echo; echo "== 1. catalog"
curl -sS -m 25 -D - -o /tmp/gh_cat.txt -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" https://models.github.ai/catalog/models | grep -iE '^(HTTP|content-type|server|x-ratelimit|x-github-request-id|location|via)'
head -c 300 /tmp/gh_cat.txt; echo
echo; echo "== 2. chat"
curl -sS -m 40 -D - -o /tmp/gh_chat.txt -X POST https://models.github.ai/inference/chat/completions -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" -H "Content-Type: application/json" -d '{"model":"openai/gpt-4o-mini","max_tokens":20,"messages":[{"role":"user","content":"say ok"}]}' | grep -iE '^(HTTP|content-type|server|x-ratelimit|x-github-request-id|location|via)'
head -c 400 /tmp/gh_chat.txt; echo
echo; echo "== 3. who am I (token check)"
curl -sS -m 20 -o /dev/null -w "api.github.com/user -> HTTP %{http_code}\n" -H "Authorization: Bearer $TOKEN" https://api.github.com/user
rm -f /tmp/gh_cat.txt /tmp/gh_chat.txt
