#!/usr/bin/env bash
# 앱 VM(infra/vm.bicep)에 백엔드 API, 프런트, Ollama, Caddy를 Docker Compose로 올린다.
# az 로그인과 SSH 키가 있는 곳(맥)에서 돌린다. VM 안에서 이미지를 빌드하므로 ACR 작업이나 레지스트리가 필요 없다.
#
#   bash infra/vm/deploy-vm.sh             # 커밋된 소스를 올리고 다시 빌드한 뒤 띄운다
#   SEED=1 bash infra/vm/deploy-vm.sh      # 끝에 시연 시작 상태로 되돌린다
#                                          # bff: 시연 조직만 되돌리고 시연 진단을 승인 대기까지 돌려 둔다(실제 계정·조직은 그대로)
#                                          # token·header: 진행 중인 진단과 대화 기록이 모두 지워진다
#   GRANT_ADMIN=a@b.c bash infra/vm/deploy-vm.sh  # bff: 끝에 그 이메일로 가입한 사용자를 관리자 조직(org_admin)에 넣는다
#
# 환경 변수
#   RG               리소스 그룹 (기본 team_AIBIC2026_03_RG_EUS2)
#   SSH_KEY          VM SSH 개인 키 (기본 ~/.ssh/rsai_demo_vm)
#   BACKEND_DIR      백엔드 저장소 (기본 이 저장소), BACKEND_REF (기본 HEAD)
#   FRONTEND_DIR     프런트 저장소 (기본 ../frontend), FRONTEND_REF (기본 origin/main)
#                    커밋된 내용만 올린다(git archive). 작업 폴더의 커밋하지 않은 변경은 올라가지 않는다
#   V3_LLM           foundry(기본. Foundry gpt-5-mini, 구조화 출력·추론 강도 low) | rule(규칙 정책) | anthropic
#                    2026-11-18 출력 형식을 고친 뒤 시연 시나리오의 초안 첫 시도 통과율 99~100%, 세 업체 모두 합의
#   V3_AUTH          bff(기본. 실제 계정·조직: 프런트 서버만 V3_BFF_KEY로 부르고 사용자·소속을 확인한다)
#                    | token(조직별 토큰) | header(시연 전용)
#   AUTH_DEV_LOGIN   true(기본. 시연용 로그인) | false
#   USE_OLLAMA       1이면 Ollama를 고른 업체(시연의 남해열분해)가 VM의 Ollama로 답한다. 기본은 비움: GPU 없이 CPU로는
#                    6천 토큰 프롬프트에 qwen2.5:7b 8분 30초·3b 4분 10초가 걸려(2026-11-18 측정) 실시간 대화가 안 되므로,
#                    Ollama 주소를 비우고 V3_LLM_ALL_VENDORS=1로 주 모델(Foundry)이 답하게 한다(운영 화면에는 설정과 실제가 따로 보인다)
#                    gpu면 GPU VM(infra/gpu-vm.bicep, East US A10, infra/gpu/setup-ollama.sh로 준비)의 Ollama가 답한다.
#                    꺼져 있으면 배포가 먼저 켜고(infra/gpu/gpu.sh up), 쓰지 않으면 맥의 자동 끄기가 끈다(install-idle-watch.sh).
#                    꺼진 동안 Ollama 차례는 Foundry가 답한다(OLLAMA_FALLBACK=1).
#                    주소는 GPU VM의 사설 IP로 자동으로 찾고(앱 VNet과 피어링), 앱 VM의 ollama 컨테이너는 쓰지 않는다.
#                    임베딩은 근거 색인과 같은 Foundry 임베딩을 그대로 쓴다
#   OLLAMA_URL       USE_OLLAMA=gpu에서 자동으로 찾은 주소 대신 쓸 Ollama 주소
#   GPU_VM           USE_OLLAMA=gpu일 때 찾을 GPU VM 이름 (기본 vm-rsai-gpu)
#   OLLAMA_MODELS    USE_OLLAMA=1일 때 VM에 받아 둘 Ollama 모델 (기본 "bge-m3 qwen2.5:7b")
#   SSH_SOURCE       SSH를 허용할 공용 IP (기본: 지금 이 컴퓨터의 공용 IP. NSG 규칙과 다르면 맞춘다)
#   SEED             1이면 배포 끝에 시연 데이터를 되돌린다. bff는 백엔드 컨테이너에서 scripts.v3.accounts_admin reset-demo,
#                    token·header는 POST /api/v3/demo/reset 을 관리자 토큰(또는 X-Org-Id)으로 부른다
#   GRANT_ADMIN      bff에서만. 이미 가입한 사용자의 이메일. 그 사용자를 org_admin의 owner로 넣는다
#   INDEX            1이면 배포 끝에 백엔드 컨테이너 안에서 근거 색인을 다시 만든다(scripts/v3/index_evidence.py
#                    --create-index. 운영 DB의 근거를 질의와 같은 임베딩으로 올린다). SEED=1과 함께 주면 되돌린 뒤에 돈다
#   AZURE_SEARCH_INDEX            근거 색인 이름 (기본 rsai-evidence)
#   FOUNDRY_EMBEDDING_DEPLOYMENT  질의·색인 임베딩 배포 (기본 text-embedding-3-small, 1536차원).
#                    Foundry 계정에 이 배포가 없으면 V3_EMBEDDINGS를 비워 벡터 없이 글과 시맨틱 랭커로 찾는다
#
# 비밀: Key Vault에서 읽어 VM의 /opt/rsai/env/*.env(권한 600)에만 쓴다. 화면과 명령줄에 값을 찍지 않는다.
#       v3-bff-key, v3-org-tokens, auth-secret 이 Key Vault에 없으면 새로 만들어 Key Vault에만 둔다.
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
V3_AUTH="${V3_AUTH:-bff}"
AUTH_DEV_LOGIN="${AUTH_DEV_LOGIN:-true}"
USE_OLLAMA="${USE_OLLAMA:-}"
OLLAMA_URL="${OLLAMA_URL:-}"
GPU_VM="${GPU_VM:-vm-rsai-gpu}"
OLLAMA_MODELS="${OLLAMA_MODELS:-bge-m3 qwen2.5:7b}"
[ "$USE_OLLAMA" = 1 ] || OLLAMA_MODELS=""
SEED="${SEED:-}"
GRANT_ADMIN="${GRANT_ADMIN:-}"
INDEX="${INDEX:-}"
SEARCH_INDEX="${AZURE_SEARCH_INDEX:-rsai-evidence}"
EMBED_DEPLOYMENT="${FOUNDRY_EMBEDDING_DEPLOYMENT:-text-embedding-3-small}"
EMBED_DIMENSIONS=1536  # text-embedding-3-small. 다른 모델을 고르면 색인을 그 차원으로 다시 만든다(INDEX=1)
ADMIN_USER="${ADMIN_USER:-rsai}"
TAGQ="tags.project=='resource-ai'"
ORGS=(org_hanbit org_daon org_saegil org_namhae org_miso org_admin)

say() { echo "[deploy-vm] $*"; }
die() { echo "[deploy-vm] 오류: $*" >&2; exit 1; }

case "$V3_AUTH" in bff|token|header) ;; *) die "V3_AUTH는 bff | token | header 가운데 하나다: $V3_AUTH" ;; esac
case "$GRANT_ADMIN" in *"'"*|*'"'*|*' '*) die "GRANT_ADMIN에 따옴표나 공백을 쓸 수 없다" ;; esac
[ -z "$GRANT_ADMIN" ] || [ "$V3_AUTH" = bff ] || die "GRANT_ADMIN은 V3_AUTH=bff에서만 쓴다"

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

if [ "$V3_AUTH" = bff ] && ! kv_has v3-bff-key; then
  say "Key Vault에 v3-bff-key 가 없어 새로 만든다 (프런트 서버가 백엔드를 부를 때 쓰는 키)"
  python3 -I -c 'import secrets; print(secrets.token_urlsafe(32))' >"$TMP/bff"
  kv_set_file v3-bff-key "$TMP/bff"; rm -f "$TMP/bff"
fi
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

# AI 서비스의 주소는 태그(project=resource-ai)와 종류로 찾는다. 키는 아래 put_secret이 Key Vault에서 읽는다
ai_account() { az cognitiveservices account list -g "$RG" --query "[?$TAGQ && kind=='$1'] | [0].$2" -o tsv; }
FOUNDRY_ENDPOINT="$(ai_account AIServices properties.endpoint)"
FOUNDRY_NAME="$(ai_account AIServices name)"
DOCINTEL_ENDPOINT="$(ai_account FormRecognizer properties.endpoint)"
CONTENT_SAFETY_ENDPOINT="$(ai_account ContentSafety properties.endpoint)"
SEARCH_NAME="$(az search service list -g "$RG" --query "[?$TAGQ] | [0].name" -o tsv)"

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
if [ "$USE_OLLAMA" = gpu ]; then
  # GPU VM이 꺼져 있으면 켜고 Ollama가 답할 때까지 기다린다(시연 되돌리기가 실제 모델로 돈다). 실패해도 배포는 계속한다:
  # OLLAMA_FALLBACK=1이라 Ollama에 닿지 않는 차례는 Foundry가 답한다
  bash "$HERE/../gpu/gpu.sh" up || say "GPU VM을 켜지 못했다. 업체 측 오픈 모델 차례는 Foundry가 답한다"
  if [ -z "$OLLAMA_URL" ]; then
    gpu_ip="$(az vm list-ip-addresses -g "$RG" -n "$GPU_VM" --query "[0].virtualMachine.network.privateIpAddresses[0]" -o tsv 2>/dev/null || true)"
    [ -n "$gpu_ip" ] || { say "GPU VM $GPU_VM을 찾지 못했다(infra/gpu-vm.bicep)"; exit 1; }
    OLLAMA_URL="http://$gpu_ip:11434"
  fi
  say "업체 측 오픈 모델: GPU VM의 Ollama ($OLLAMA_URL)"
  put "$B" OLLAMA_BASE_URL "$OLLAMA_URL"
  put "$B" OLLAMA_FALLBACK 1
elif [ "$USE_OLLAMA" = 1 ]; then
  put "$B" OLLAMA_BASE_URL "http://ollama:11434"
  put "$B" OLLAMA_EMBED_MODEL bge-m3
else
  put "$B" V3_LLM_ALL_VENDORS 1
fi
put_secret "$B" AZURE_STORAGE_CONNECTION_STRING storage-connection-string
put_secret "$B" APPLICATIONINSIGHTS_CONNECTION_STRING appinsights-connection-string
# 문서 추출(Document Intelligence)과 업로드 문서의 지시문 주입 검사(Content Safety Prompt Shields)
if [ -n "$DOCINTEL_ENDPOINT" ]; then
  put "$B" AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT "$DOCINTEL_ENDPOINT"
  put_secret "$B" AZURE_DOCUMENT_INTELLIGENCE_API_KEY docintel-key
else say "Document Intelligence 리소스를 찾지 못했다. 문서는 글 파일만 읽는다"; fi
if [ -n "$CONTENT_SAFETY_ENDPOINT" ]; then
  put "$B" AZURE_CONTENT_SAFETY_ENDPOINT "$CONTENT_SAFETY_ENDPOINT"
  put_secret "$B" AZURE_CONTENT_SAFETY_API_KEY contentsafety-key
else say "Content Safety 리소스를 찾지 못했다. 업로드 문서의 주입 검사를 하지 않는다"; fi
# 근거 검색(Azure AI Search). 질의 임베딩은 색인(INDEX=1이나 맥에서 돌린 index_evidence)과 같은 Foundry 배포다
if [ -n "$SEARCH_NAME" ]; then
  put "$B" AZURE_SEARCH_ENDPOINT "https://$SEARCH_NAME.search.windows.net"
  put_secret "$B" AZURE_SEARCH_API_KEY search-admin-key
  put "$B" AZURE_SEARCH_INDEX "$SEARCH_INDEX"
  put "$B" AZURE_SEARCH_VECTOR_DIMENSIONS "$EMBED_DIMENSIONS"
  if [ -n "$FOUNDRY_NAME" ] && az cognitiveservices account deployment show -g "$RG" -n "$FOUNDRY_NAME" \
      --deployment-name "$EMBED_DEPLOYMENT" --query name -o tsv >/dev/null 2>&1; then
    put "$B" V3_EMBEDDINGS foundry
    put "$B" FOUNDRY_EMBEDDING_DEPLOYMENT "$EMBED_DEPLOYMENT"
  else
    say "Foundry 임베딩 배포 $EMBED_DEPLOYMENT 가 없다. 근거 검색은 벡터 없이 글과 시맨틱 랭커로만 한다"
  fi
else say "Azure AI Search 를 찾지 못했다. 근거 검색은 로컬 검색으로 돈다"; fi
put_secret "$B" MODEL_BLOB_URL model-blob-url
# 사진·영상 탐지: 같은 VM의 detector 서비스(app/ml/score_api.py). 두 이름에 같은 키를 넣는다
put "$B" V3_DETECTOR_ENDPOINT "http://detector:8000/score"
put "$B" V3_INTERNAL_HTTP_HOSTS detector
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
put "$F" V3_AUTH "$V3_AUTH"
if [ "$V3_AUTH" = token ]; then
  put_secret "$B" V3_ORG_TOKENS v3-org-tokens
  put_secret "$F" V3_ORG_TOKENS v3-org-tokens
fi
if [ "$V3_AUTH" = bff ]; then
  # 백엔드와 프런트 서버가 같은 키를 갖는다. 키가 없으면 백엔드가 모든 요청을 503으로 거절하므로 멈춘다
  kv_has v3-bff-key || die "Key Vault에 v3-bff-key 가 없다"
  put_secret "$B" V3_BFF_KEY v3-bff-key
  put_secret "$F" V3_BFF_KEY v3-bff-key
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

# 배포하는 곳의 공용 IP가 바뀌면 SSH(22)가 NSG에 막힌다. 허용 주소를 지금 IP로 맞춘다(SSH_SOURCE로 직접 줄 수 있다)
MYIP="${SSH_SOURCE:-$(curl -s --max-time 10 https://api.ipify.org || true)}"
if [ -n "$MYIP" ]; then
  CUR_SRC="$(az network nsg rule show -g "$RG" --nsg-name nsg-rsai-demo-app -n AllowSshFromAdmin --query sourceAddressPrefix -o tsv 2>/dev/null || true)"
  if [ -n "$CUR_SRC" ] && [ "$CUR_SRC" != "$MYIP/32" ]; then
    say "SSH 허용 주소를 $MYIP/32 로 바꾼다 (전: $CUR_SRC)"
    az network nsg rule update -g "$RG" --nsg-name nsg-rsai-demo-app -n AllowSshFromAdmin --source-address-prefixes "$MYIP/32" -o none
    sleep 5
  fi
fi

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

if [ "$SEED" = 1 ] && [ "$V3_AUTH" = bff ]; then
  say "시연 조직 되돌리기 (실제 계정·조직은 그대로)"
  "${SSH[@]}" 'cd /opt/rsai && docker compose exec -T backend python -m scripts.v3.accounts_admin reset-demo' </dev/null
elif [ "$SEED" = 1 ]; then
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
if [ -n "$GRANT_ADMIN" ]; then
  say "관리자 조직에 사용자 넣기"
  "${SSH[@]}" "cd /opt/rsai && docker compose exec -T backend python -m scripts.v3.accounts_admin grant-admin '$GRANT_ADMIN'" </dev/null
fi
if [ "$INDEX" = 1 ]; then
  say "근거 색인 다시 만들기 (백엔드 컨테이너 안에서, 운영 DB의 근거)"
  "${SSH[@]}" 'cd /opt/rsai && docker compose exec -T backend python -m scripts.v3.index_evidence --create-index' </dev/null
fi
say "끝: https://$FQDN"
