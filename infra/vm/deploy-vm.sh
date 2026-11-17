#!/usr/bin/env bash
# 앱 VM(infra/vm.bicep)에 백엔드 API, 프런트, Ollama, Caddy를 Docker Compose로 올린다.
# az 로그인과 SSH 키가 있는 곳(맥)에서 돌린다. VM 안에서 이미지를 빌드하므로 ACR 작업이나 레지스트리가 필요 없다.
#
#   bash infra/vm/deploy-vm.sh             # 커밋된 소스를 올리고 다시 빌드한 뒤 띄운다
#   SEED=1 bash infra/vm/deploy-vm.sh      # 끝에 시연 시작 상태로 되돌린다(진행 중인 진단과 대화 기록이 지워진다)
#
# 환경 변수
#   RG               리소스 그룹 (기본 team_AIBIC2026_03_RG_EUS2)
#   SSH_KEY          VM SSH 개인 키 (기본 ~/.ssh/rsai_demo_vm)
#   BACKEND_DIR      백엔드 저장소 (기본 이 저장소), BACKEND_REF (기본 HEAD)
#   FRONTEND_DIR     프런트 저장소 (기본 ../frontend), FRONTEND_REF (기본 origin/main)
#                    커밋된 내용만 올린다(git archive). 작업 폴더의 커밋하지 않은 변경은 올라가지 않는다
#   V3_LLM           foundry(기본) | rule | anthropic
#   V3_AUTH          token(기본. 조직별 토큰, 공개 주소용) | header(시연 전용)
#   AUTH_DEV_LOGIN   true(기본. 시연용 로그인) | false
#   OLLAMA_MODELS    VM에 받아 둘 Ollama 모델 (기본 "bge-m3 qwen2.5:7b". 시연 업체 한 곳이 qwen2.5:7b를 고른다)
#   SEED             1이면 배포 끝에 POST /api/v3/demo/reset 을 관리자 토큰으로 부른다
#
# 비밀: Key Vault에서 읽어 VM의 /opt/rsai/env/*.env(권한 600)에만 쓴다. 화면과 명령줄에 값을 찍지 않는다.
#       v3-org-tokens, auth-secret 이 Key Vault에 없으면 새로 만들어 Key Vault에만 둔다.
set -euo pipefail

RG="${RG:-team_AIBIC2026_03_RG_EUS2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/rsai_demo_vm}"
BACKEND_DIR="${BACKEND_DIR:-$ROOT}"
BACKEND_REF="${BACKEND_REF:-HEAD}"
FRONTEND_DIR="${FRONTEND_DIR:-$ROOT/../frontend}"
FRONTEND_REF="${FRONTEND_REF:-origin/main}"
V3_LLM="${V3_LLM:-foundry}"
V3_AUTH="${V3_AUTH:-token}"
AUTH_DEV_LOGIN="${AUTH_DEV_LOGIN:-true}"
OLLAMA_MODELS="${OLLAMA_MODELS:-bge-m3 qwen2.5:7b}"
SEED="${SEED:-}"
ADMIN_USER="${ADMIN_USER:-rsai}"
TAGQ="tags.project=='resource-ai'"
ORGS=(org_hanbit org_daon org_saegil org_namhae org_miso org_admin)

say() { echo "[deploy-vm] $*"; }
die() { echo "[deploy-vm] 오류: $*" >&2; exit 1; }

[ -f "$SSH_KEY" ] || die "SSH 키가 없다: $SSH_KEY"
FQDN="$(az network public-ip show -g "$RG" -n pip-rsai-demo-app --query dnsSettings.fqdn -o tsv)"
[ -n "$FQDN" ] || die "pip-rsai-demo-app 을 찾지 못했다. infra/vm.bicep 을 먼저 배포한다"
KV="$(az keyvault list -g "$RG" --query "[?$TAGQ] | [0].name" -o tsv)"
[ -n "$KV" ] || die "Key Vault를 찾지 못했다"
SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 "$ADMIN_USER@$FQDN")
SCP=(scp -q -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)
say "대상 $FQDN"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
umask 077

kv_get() { az keyvault secret show --vault-name "$KV" -n "$1" --query value -o tsv; }
kv_has() { az keyvault secret show --vault-name "$KV" -n "$1" --query id -o tsv >/dev/null 2>&1; }
kv_set_file() { az keyvault secret set --vault-name "$KV" -n "$1" --file "$2" --query id -o tsv >/dev/null; }

if ! kv_has v3-org-tokens; then
  say "Key Vault에 v3-org-tokens 가 없어 조직별 토큰을 새로 만든다"
  python3 -I -c 'import json,secrets,sys; print(json.dumps({o: secrets.token_urlsafe(32) for o in sys.argv[1:]}))' "${ORGS[@]}" >"$TMP/tokens"
  kv_set_file v3-org-tokens "$TMP/tokens"; rm -f "$TMP/tokens"
fi
if ! kv_has auth-secret; then
  say "Key Vault에 auth-secret 이 없어 새로 만든다"
  python3 -I -c 'import secrets; print(secrets.token_urlsafe(48))' >"$TMP/auth"
  kv_set_file auth-secret "$TMP/auth"; rm -f "$TMP/auth"
fi
if ! kv_has detector-api-key; then
  say "Key Vault에 detector-api-key 가 없어 새로 만든다 (VM 안 탐지 채점 서버 키)"
  python3 -I -c 'import secrets; print(secrets.token_urlsafe(32))' >"$TMP/det"
  kv_set_file detector-api-key "$TMP/det"; rm -f "$TMP/det"
fi

FOUNDRY_ENDPOINT="$(az cognitiveservices account list -g "$RG" --query "[?$TAGQ && kind=='AIServices'] | [0].properties.endpoint" -o tsv)"

# 값을 작은따옴표로 감싸 Compose가 $ 등을 해석하지 않게 한다. 값에 작은따옴표가 있으면 멈춘다
put() {
  local file="$1" key="$2" value="$3"
  case "$value" in *"'"*) die "$key 값에 작은따옴표가 있어 env 파일에 쓸 수 없다" ;; esac
  printf "%s='%s'\n" "$key" "$value" >>"$file"
}
put_secret() { local v; v="$(kv_get "$3" 2>/dev/null || true)"; [ -n "$v" ] && put "$1" "$2" "$v" || say "Key Vault $3 없음, $2 건너뜀"; }

mkdir -p "$TMP/env"
B="$TMP/env/backend.env"; F="$TMP/env/frontend.env"; : >"$B"; : >"$F"
put "$B" APP_ENV demo
put "$B" CORS_ORIGINS "[\"https://$FQDN\"]"
put_secret "$B" DATABASE_URL database-url
put "$B" V3_LLM "$V3_LLM"
put "$B" FOUNDRY_ENDPOINT "$FOUNDRY_ENDPOINT"
put "$B" FOUNDRY_DEPLOYMENT "${FOUNDRY_DEPLOYMENT:-gpt-5-mini}"
put_secret "$B" FOUNDRY_API_KEY foundry-api-key
put "$B" OLLAMA_BASE_URL "http://ollama:11434"
put "$B" OLLAMA_EMBED_MODEL bge-m3
put_secret "$B" AZURE_STORAGE_CONNECTION_STRING storage-connection-string
put_secret "$B" APPLICATIONINSIGHTS_CONNECTION_STRING appinsights-connection-string
put_secret "$B" MODEL_BLOB_URL model-blob-url
# 사진·영상 탐지: 같은 VM의 detector 서비스(app/ml/score_api.py). 두 이름에 같은 키를 넣는다
put "$B" V3_DETECTOR_ENDPOINT "http://detector:8000/score"
put_secret "$B" V3_DETECTOR_API_KEY detector-api-key
put_secret "$B" DETECTOR_API_KEY detector-api-key
put "$B" V3_AUTH "$V3_AUTH"
put "$B" V3_DEMO_OPEN false
if [ "$V3_LLM" = anthropic ]; then put_secret "$B" ANTHROPIC_API_KEY anthropic-api-key; fi
if kv_has kakao-rest-api-key; then put "$B" V3_DISTANCE kakao; put_secret "$B" KAKAO_REST_API_KEY kakao-rest-api-key; fi

put_secret "$F" AUTH_SECRET auth-secret
put "$F" AUTH_URL "https://$FQDN"
put "$F" AUTH_TRUST_HOST true
put "$F" AUTH_DEV_LOGIN "$AUTH_DEV_LOGIN"
put "$F" V3_BACKEND_URL "http://backend:8000/api/v3"
if [ "$V3_AUTH" = token ]; then
  put_secret "$B" V3_ORG_TOKENS v3-org-tokens
  put_secret "$F" V3_ORG_TOKENS v3-org-tokens
fi

say "소스 묶기: backend $(git -C "$BACKEND_DIR" rev-parse --short "$BACKEND_REF"), frontend $(git -C "$FRONTEND_DIR" rev-parse --short "$FRONTEND_REF")"
git -C "$BACKEND_DIR" archive --format=tar.gz -o "$TMP/backend.tar.gz" "$BACKEND_REF"
git -C "$FRONTEND_DIR" archive --format=tar.gz -o "$TMP/frontend.tar.gz" "$FRONTEND_REF"
printf 'SITE_ADDRESS=%s\n' "$FQDN" >"$TMP/compose.env"

# VM에서 돌릴 명령. 파일로 올려 실행한다(표준 입력으로 보내면 docker compose run이 남은 명령을 읽어 버린다)
cat >"$TMP/remote.sh" <<'REMOTE'
set -euo pipefail
cd /opt/rsai
chmod 600 env/*.env
mv upload/compose.yml upload/Caddyfile .
mv upload/compose.env .env
for repo in backend frontend; do
  rm -rf "src/$repo.new" && mkdir -p "src/$repo.new"
  tar -xzf "upload/$repo.tar.gz" -C "src/$repo.new"
  rm -rf "src/$repo" && mv "src/$repo.new" "src/$repo"
done
rm -f upload/*.tar.gz
docker compose build --pull backend frontend
docker compose up -d ollama
docker compose run --rm -T migrate
docker compose up -d --remove-orphans
for m in $OLLAMA_MODELS; do
  if ! docker compose exec -T ollama ollama show "$m" >/dev/null 2>&1; then
    echo "[remote] Ollama $m 받는 중"; docker compose exec -T ollama ollama pull "$m" >/dev/null
  fi
done
docker image prune -f >/dev/null
docker compose ps --format '{{.Service}}\t{{.State}}\t{{.Status}}'
REMOTE

say "VM 준비 확인 (cloud-init의 Docker 설치를 기다린다)"
for i in $(seq 1 30); do
  if "${SSH[@]}" 'command -v docker >/dev/null && docker compose version >/dev/null 2>&1'; then break; fi
  [ "$i" -lt 30 ] || die "VM에 Docker가 준비되지 않았다"
  sleep 10
done

say "파일 올리기"
"${SSH[@]}" 'mkdir -p /opt/rsai/env /opt/rsai/upload && chmod 700 /opt/rsai/env /opt/rsai/upload'
"${SCP[@]}" "$TMP/backend.tar.gz" "$TMP/frontend.tar.gz" "$TMP/compose.env" "$TMP/remote.sh" \
  "$HERE/compose.yml" "$HERE/Caddyfile" "$ADMIN_USER@$FQDN:/opt/rsai/upload/"
"${SCP[@]}" "$TMP/env/backend.env" "$TMP/env/frontend.env" "$ADMIN_USER@$FQDN:/opt/rsai/env/"

say "빌드와 시작 (처음에는 이미지 빌드에 수 분이 걸린다)"
"${SSH[@]}" "OLLAMA_MODELS='$OLLAMA_MODELS' bash /opt/rsai/upload/remote.sh && rm -f /opt/rsai/upload/remote.sh" </dev/null

say "상태 확인: https://$FQDN (인증서를 처음 받을 때는 1~2분 걸린다)"
for i in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$FQDN/" || true)"
  case "$code" in 200|30[0-9]) say "응답 $code"; break ;; esac
  [ "$i" -lt 30 ] || die "https://$FQDN 이 응답하지 않는다 (마지막 코드 $code). ssh 로 들어가 docker compose logs 를 본다"
  sleep 10
done

if [ "$SEED" = 1 ]; then
  say "시연 시작 상태로 되돌리기"
  "${SSH[@]}" 'cd /opt/rsai && docker compose exec -T backend python -c "
import json, os, httpx
mode = os.environ.get(\"V3_AUTH\", \"header\")
headers = {\"X-Org-Id\": \"org_admin\"}
if mode == \"token\":
    tok = json.loads(os.environ[\"V3_ORG_TOKENS\"])[\"org_admin\"]
    headers = {\"Authorization\": \"Bearer \" + (tok[0] if isinstance(tok, list) else tok)}
r = httpx.post(\"http://localhost:8000/api/v3/demo/reset\", headers=headers, timeout=120)
print(\"demo/reset\", r.status_code)
r.raise_for_status()
"' </dev/null
fi
say "끝: https://$FQDN"
