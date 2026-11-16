#!/usr/bin/env bash
# 미실행·미검증: 이 스크립트는 배포된 API를 상대로 돌려 보지 못했다(bash -n 문법 확인만 했다).
#
# 시연 시작 상태로 되돌린다. 배포된 API의 POST /api/v3/demo/reset 을 부르고 조직 수를 확인한다.
#   bash infra/seed-demo.sh [API 주소]
#   예) bash infra/seed-demo.sh https://app-rsai-demo-api-xxxxxx.azurewebsites.net
# 주소를 주지 않으면 API_BASE_URL, 그것도 없으면 리소스 그룹에서 API 앱을 찾는다(az 로그인 필요).
# 진행 중인 진단과 대화 기록이 모두 지워진다. 시연 데이터의 회사와 수치는 모두 가상이다.
set -euo pipefail

RG="${RG:-team_AIBIC2026_03_RG_EUS2}"
BASE="${1:-${API_BASE_URL:-}}"
say() { echo "[seed-demo] $*"; }

if [ -z "$BASE" ]; then
  HOST="$(az webapp list -g "$RG" --query "[?tags.project=='resource-ai' && contains(name,'-api-')] | [0].defaultHostName" -o tsv 2>/dev/null || true)"
  if [ -z "$HOST" ]; then
    HOST="$(az containerapp show -g "$RG" -n ca-rsai-demo-api --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || true)"
  fi
  [ -n "$HOST" ] || { echo "[seed-demo] 오류: API 주소를 찾지 못했다. 주소를 인자로 준다" >&2; exit 1; }
  BASE="https://$HOST"
fi
BASE="${BASE%/}"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# 멈춰 있던 앱은 첫 요청에 깨어나느라 오래 걸린다. 상태 확인이 될 때까지 기다린다.
say "$BASE 깨우는 중"
for i in $(seq 1 30); do
  if curl -fsS -o /dev/null --max-time 20 "$BASE/api/v1/health"; then break; fi
  [ "$i" -lt 30 ] || { echo "[seed-demo] 오류: $BASE/api/v1/health 가 응답하지 않는다" >&2; exit 1; }
  sleep 10
done

CODE="$(curl -sS -o "$TMP/reset.json" -w '%{http_code}' --max-time 120 -X POST -H "X-Org-Id: ${ADMIN_ORG_ID:-org_admin}" "$BASE/api/v3/demo/reset")"
if [ "$CODE" != 200 ]; then
  echo "[seed-demo] 오류: /api/v3/demo/reset 이 $CODE 로 답했다" >&2
  head -c 600 "$TMP/reset.json" >&2 || true; echo >&2
  exit 1
fi

CODE="$(curl -sS -o "$TMP/orgs.json" -w '%{http_code}' --max-time 60 "$BASE/api/v3/orgs")"
[ "$CODE" = 200 ] || { echo "[seed-demo] 오류: /api/v3/orgs 가 $CODE 로 답했다" >&2; exit 1; }
COUNT="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$TMP/orgs.json")"
[ "$COUNT" -gt 0 ] || { echo "[seed-demo] 오류: 되돌린 뒤에 조직이 하나도 없다" >&2; exit 1; }
say "시연 시작 상태로 되돌렸다. 조직 $COUNT 곳"
