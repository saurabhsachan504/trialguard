#!/usr/bin/env bash
# TubeNotes backend - DGX par deploy / update.
#
#   bash deploy/deploy.sh
#
# Pehli baar aur har update - dono ke liye yahi script hai. Ye idempotent hai:
# baar-baar chalane se kuch tootta nahi.
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.prod.yml"

# ---- pehle rok-tok, phir kaam -------------------------------------------
if [[ ! -f .env ]]; then
  echo "ERROR: .env nahi mila."
  echo "       cp .env.prod.example .env   &&   nano .env"
  exit 1
fi

for key in SECRET_KEY DEVICE_HASH_SECRET POSTGRES_PASSWORD; do
  val="$(grep -E "^${key}=" .env | cut -d= -f2- || true)"
  if [[ -z "${val}" || "${val}" == *"<FILL"* || "${val}" == *"change-me"* ]]; then
    echo "ERROR: .env mein ${key} abhi bhara nahi hai."
    echo "       python3 -c \"import secrets; print(secrets.token_urlsafe(48))\""
    exit 1
  fi
done

if grep -qE '^ENV=prod' .env && grep -qE '^MOCK_BILLING_SECRET=.+' .env; then
  echo "ERROR: prod mein MOCK_BILLING_SECRET khali hona chahiye."
  echo "       warna koi bhi banda khud ko free subscription de sakta hai."
  exit 1
fi

# ---- build + start -------------------------------------------------------
echo "==> build"
$COMPOSE build

echo "==> up (migrations container start hote hi khud chalti hain)"
$COMPOSE up -d

# ---- wait for healthy ----------------------------------------------------
echo -n "==> health check "
for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    echo " OK"
    curl -s http://127.0.0.1:8000/healthz; echo
    echo
    echo "Local par chal gaya. Ab bahar se check karo:"
    echo "   curl https://tubenotes.trueworks.in/healthz"
    exit 0
  fi
  echo -n "."
  sleep 2
done

echo " FAILED"
echo "Aakhri 50 line log:"
$COMPOSE logs --tail=50 api
exit 1
