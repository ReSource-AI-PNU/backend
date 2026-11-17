#!/usr/bin/env bash
# 미실행·미검증: 이 스크립트는 한 번도 실행해 보지 못했다(작성 환경에 az가 없다. bash -n 문법 확인만 했다).
# 처음 쓸 때는 한 대상씩 돌리며 출력을 확인한다.
#
# Re:Source AI 앱 배포. 이미지 태그는 커밋 SHA다.
#   bash infra/deploy.sh backend | frontend | vendor-agent | mcp | all
#
# 순서
#   1) az acr build 로 이미지를 만든다 (같은 태그가 이미 있으면 다시 만들지 않는다)
#   2) 새 이미지로 alembic upgrade head 를 돌린다 (backend, vendor-agent, mcp)
#   3) 앱을 새 이미지로 바꾼다
#   4) 상태 확인 경로를 부른다
# 2)가 실패하면 3)으로 가지 않는다. 4)가 실패하면 오류로 끝나고 되돌리는 명령을 알려 준다.
#
# 어디에 배포하나 (PLATFORM)
#   containerapps : Container Apps. 단일 리비전 모드라 새 리비전이 뜨지 못하면 이전 리비전이 계속 받는다.
#   appservice    : App Service Linux 컨테이너 (infra/appservice.bicep). Microsoft.App 공급자가 등록되지
#                   않은 동안의 대안이다. 리비전이 없어 3) 뒤에는 곧바로 바뀐다.
#   auto(기본)    : Microsoft.App 이 등록돼 있고 Container Apps 환경이 있으면 containerapps, 아니면 appservice
#
# 환경 변수
#   RG                리소스 그룹 (기본 team_AIBIC2026_03_RG_EUS2)
#   PLATFORM          auto | containerapps | appservice
#   FRONTEND_DIR      프런트 저장소 경로 (기본 ../frontend). Dockerfile이 있어야 한다
#   IMAGE_TAG         이미 올린 태그로 배포한다(되돌릴 때). 주면 빌드와 작업 폴더 검사를 건너뛴다
#   MIGRATE_MODE      acr(기본. ACR 작업으로 Azure 안에서 돌린다) | local(이 PC에서 uv run alembic)
#   BACKEND_EXTRAS    이미지에 넣을 extra (기본 ml. 비우면 추론 extra를 뺀다)
#   V3_LLM            foundry(기본) | rule | anthropic. anthropic이면 Key Vault 비밀 anthropic-api-key가 먼저 있어야 한다
#   ANTHROPIC_MODEL   V3_LLM=anthropic일 때의 모델 이름 (비우면 앱의 기본값)
#   V3_LLM_ALL_VENDORS true면 ollama를 고른 업체도(주소가 없을 때) anthropic 모델이 답한다 (기본 false)
#   FOUNDRY_ENDPOINT  비우면 리소스 그룹의 Foundry(AI Services) 주소를 찾는다
#   FOUNDRY_DEPLOYMENT 기본 gpt-5-mini
#   VENDOR_ORG_ID     업체 측 에이전트 서비스가 대변하는 업체 (기본 org_saegil)
#   OLLAMA_BASE_URL   Ollama 주소 (VNet 안에서만 닿는다. 기본은 비움)
#   V3_AUTH           header(기본, 시연 전용: X-Org-Id를 믿는다) | token(조직별 서비스 토큰). 공개 주소에는 token을
#                     쓴다. token이면 Key Vault 비밀 v3-org-tokens(JSON {"조직 ID": "토큰"})가 먼저 있어야 한다
#   V3_DEMO_OPEN      true면 시연 되돌리기를 누구나 부른다 (기본 false: 관리자 조직만)
#   V3_CORS_ORIGINS   API가 프런트 주소 말고 더 허용할 브라우저 출처 (쉼표로 나눔. 기본은 비움)
#   V3_DISTANCE       도로 거리 제공자: 비움(기본, 없음) | none | kakao | azure
#                     kakao면 Key Vault 비밀 kakao-rest-api-key, azure면 maps-key가 먼저 있어야 한다
#   V3_VENDOR_AGENTS  따로 띄운 업체 측 에이전트 주소 JSON {"업체 조직 ID": "https://..."} (기본 {})
#   AZURE_SEARCH_ENDPOINT  AI Search 주소. 주면 Key Vault 비밀 search-admin-key를 함께 넣는다 (기본은 비움)
#   AZURE_SEARCH_INDEX     기본 rsai-evidence
#   APP_INSIGHTS      true면 Key Vault 비밀 appinsights-connection-string을 API에 넣는다 (기본 false)
#   V3_DETECTOR_ENDPOINT, V3_DETECTOR_DEPLOYMENT  탐지 모델 엔드포인트. 주면 Key Vault 비밀 detector-api-key가 있어야 한다
#   프런트 (frontend 대상일 때만 읽는다)
#   WEB_API           direct | proxy. direct는 브라우저가 API 주소를 바로 부른다(NEXT_PUBLIC_API_BASE=https://<API>/api/v3).
#                     proxy는 브라우저가 프런트의 /api/v3 를 부르고 프런트 서버가 V3_BACKEND_URL로 넘긴다
#                     (NEXT_PUBLIC_API_BASE=/api/v3). 기본은 V3_AUTH=token이면 proxy, 아니면 direct.
#                     token 모드에서 direct는 쓸 수 없다(브라우저는 조직 토큰을 갖지 않는다)
#   AUTH_DEV_LOGIN    true면 시연용 로그인을 켠다 (기본 false)
#   AUTH_URL          프런트의 공개 주소 (비우면 프런트 앱 주소)
#   AUTH_ENTRA_ID, AUTH_ENTRA_ISSUER  Entra External ID 로그인. ID를 주면 Key Vault 비밀 auth-entra-secret이 있어야 한다
#   AUTH_SECRET은 Key Vault 비밀 auth-secret이다. 없으면 새로 만들어 Key Vault에만 둔다
#   ALLOW_DIRTY=1     커밋하지 않은 변경이 있어도 진행한다 (태그와 내용이 달라질 수 있다)
#
# 비밀: Key Vault에서 읽어 앱 비밀로만 넣고 화면에 찍지 않는다. vendor-service-key 와 mcp-service-key 가
#       Key Vault에 없으면 새로 만들어 Key Vault에만 둔다. 사람이 정해 넣는 비밀(anthropic-api-key,
#       kakao-rest-api-key, v3-org-tokens 등)은 여기서 만들지 않고, 없으면 배포 전에 멈춘다.
set -euo pipefail

RG="${RG:-team_AIBIC2026_03_RG_EUS2}"
WHAT="${1:-}"
PLATFORM="${PLATFORM:-auto}"
MIGRATE_MODE="${MIGRATE_MODE:-acr}"
BACKEND_EXTRAS="${BACKEND_EXTRAS-ml}"
V3_LLM="${V3_LLM:-foundry}"
FOUNDRY_DEPLOYMENT="${FOUNDRY_DEPLOYMENT:-gpt-5-mini}"
VENDOR_ORG_ID="${VENDOR_ORG_ID:-org_saegil}"
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-}"
V3_AUTH="${V3_AUTH:-header}"
V3_DEMO_OPEN="${V3_DEMO_OPEN:-false}"
ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-}"
V3_LLM_ALL_VENDORS="${V3_LLM_ALL_VENDORS:-false}"
V3_CORS_ORIGINS="${V3_CORS_ORIGINS:-}"
V3_DISTANCE="${V3_DISTANCE:-}"
V3_VENDOR_AGENTS="${V3_VENDOR_AGENTS:-}"; [ -n "$V3_VENDOR_AGENTS" ] || V3_VENDOR_AGENTS='{}'
AZURE_SEARCH_ENDPOINT="${AZURE_SEARCH_ENDPOINT:-}"
AZURE_SEARCH_INDEX="${AZURE_SEARCH_INDEX:-rsai-evidence}"
APP_INSIGHTS="${APP_INSIGHTS:-false}"
V3_DETECTOR_ENDPOINT="${V3_DETECTOR_ENDPOINT:-}"
V3_DETECTOR_DEPLOYMENT="${V3_DETECTOR_DEPLOYMENT:-}"
AUTH_DEV_LOGIN="${AUTH_DEV_LOGIN:-false}"
AUTH_URL="${AUTH_URL:-}"
AUTH_ENTRA_ID="${AUTH_ENTRA_ID:-}"
AUTH_ENTRA_ISSUER="${AUTH_ENTRA_ISSUER:-}"
WEB_API="${WEB_API:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
FRONTEND_DIR="${FRONTEND_DIR:-$ROOT/../frontend}"
TAGQ="tags.project=='resource-ai'"
CAE="${CAE:-cae-rsai-demo}"

say() { echo "[deploy] $*"; }
die() { echo "[deploy] 오류: $*" >&2; exit 1; }

case "$WHAT" in backend|frontend|vendor-agent|mcp|all) ;; *) echo "사용법: $0 backend|frontend|vendor-agent|mcp|all" >&2; exit 2 ;; esac
case "$PLATFORM" in auto|containerapps|appservice) ;; *) die "PLATFORM은 auto|containerapps|appservice 중 하나" ;; esac
case "$MIGRATE_MODE" in acr|local) ;; *) die "MIGRATE_MODE는 acr|local 중 하나" ;; esac
case "$V3_AUTH" in header|token) ;; *) die "V3_AUTH는 header|token 중 하나" ;; esac
case "$V3_DEMO_OPEN" in true|false) ;; *) die "V3_DEMO_OPEN은 true|false 중 하나" ;; esac
case "$V3_LLM" in rule|foundry|anthropic) ;; *) die "V3_LLM은 rule|foundry|anthropic 중 하나" ;; esac
case "$V3_LLM_ALL_VENDORS" in true|false) ;; *) die "V3_LLM_ALL_VENDORS는 true|false 중 하나" ;; esac
case "$V3_DISTANCE" in ""|none|kakao|azure) ;; *) die "V3_DISTANCE는 비움|none|kakao|azure 중 하나" ;; esac
case "$APP_INSIGHTS" in true|false) ;; *) die "APP_INSIGHTS는 true|false 중 하나" ;; esac
case "$AUTH_DEV_LOGIN" in true|false) ;; *) die "AUTH_DEV_LOGIN은 true|false 중 하나" ;; esac
if [ -z "$WEB_API" ]; then if [ "$V3_AUTH" = token ]; then WEB_API=proxy; else WEB_API=direct; fi; fi
case "$WEB_API" in direct|proxy) ;; *) die "WEB_API는 direct|proxy 중 하나" ;; esac
if [ "$V3_AUTH" = token ] && [ "$WEB_API" = direct ]; then
  die "V3_AUTH=token에서는 WEB_API=proxy만 된다 (브라우저는 조직 토큰을 갖지 않는다)"
fi

selected() { [ "$WHAT" = all ] || [ "$WHAT" = "$1" ]; }
uses_backend_image() { selected backend || selected vendor-agent || selected mcp; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# ── 이미 있는 리소스 찾기 ────────────────────────────────────
ACR="$(az acr list -g "$RG" --query "[?$TAGQ] | [0].name" -o tsv)"
KV="$(az keyvault list -g "$RG" --query "[?$TAGQ] | [0].name" -o tsv)"
[ -n "$ACR" ] || die "태그가 붙은 Container Registry를 찾지 못했다 (infra/deploy-infra.sh apply base 먼저)"
[ -n "$KV" ] || die "태그가 붙은 Key Vault를 찾지 못했다"
SERVER="$ACR.azurecr.io"
SUFFIX="${KV##*-}"   # kv-rsai-demo-<suffix>. Bicep의 uniqueString 접미어와 같다

if [ "$PLATFORM" = auto ]; then
  if [ "$(az provider show -n Microsoft.App --query registrationState -o tsv 2>/dev/null || true)" = Registered ] \
     && az containerapp env show -g "$RG" -n "$CAE" -o none 2>/dev/null; then
    PLATFORM=containerapps
  else
    PLATFORM=appservice
  fi
fi
say "대상 $WHAT, 배포 위치 $PLATFORM, 레지스트리 $ACR"

kv_secret() { az keyvault secret show --vault-name "$KV" -n "$1" --query value -o tsv; }
ensure_key() {
  if ! az keyvault secret show --vault-name "$KV" -n "$1" --query id -o tsv >/dev/null 2>&1; then
    az keyvault secret set --vault-name "$KV" -n "$1" --value "$(openssl rand -hex 32)" --query id -o tsv >/dev/null
    say "Key Vault 비밀 $1 을 새로 만들었다"
  fi
}

# 사람이 정해 Key Vault에 넣는 비밀(여기서 만들지 않는다). 없으면 Key Vault 참조가 풀리지 않아 앱 설정에
# 참조 글이 그대로 남으므로(앱이 그것을 키로 읽는다) 배포 전에 멈춘다. 값은 읽지 않고 있는지만 본다.
need_secret() {
  az keyvault secret show --vault-name "$KV" -n "$1" --query id -o tsv >/dev/null 2>&1 \
    || die "$2인데 Key Vault 비밀 $1 이 없다. 먼저 넣는다: az keyvault secret set --vault-name $KV -n $1 --file <값을 담은 파일>"
}
# token 모드의 조직별 토큰: 없으면 API가 모든 요청에 500으로 답한다. 프런트 서버도 같은 비밀을 읽는다.
if [ "$V3_AUTH" = token ] && { selected backend || selected frontend; }; then
  need_secret v3-org-tokens "V3_AUTH=token"
fi
if [ "$V3_LLM" = anthropic ] && { selected backend || selected vendor-agent; }; then
  need_secret anthropic-api-key "V3_LLM=anthropic"
fi
if selected backend; then
  if [ "$V3_DISTANCE" = kakao ]; then need_secret kakao-rest-api-key "V3_DISTANCE=kakao"; fi
  if [ "$V3_DISTANCE" = azure ]; then need_secret maps-key "V3_DISTANCE=azure"; fi
  if [ "$V3_VENDOR_AGENTS" != '{}' ]; then need_secret vendor-service-key "V3_VENDOR_AGENTS를 준 배포"; fi
  if [ -n "$AZURE_SEARCH_ENDPOINT" ]; then need_secret search-admin-key "AZURE_SEARCH_ENDPOINT를 준 배포"; fi
  if [ "$APP_INSIGHTS" = true ]; then need_secret appinsights-connection-string "APP_INSIGHTS=true"; fi
  if [ -n "$V3_DETECTOR_ENDPOINT" ]; then need_secret detector-api-key "V3_DETECTOR_ENDPOINT를 준 배포"; fi
fi
if selected frontend && [ -n "$AUTH_ENTRA_ID" ]; then
  [ -n "$AUTH_ENTRA_ISSUER" ] || die "AUTH_ENTRA_ID를 주면 AUTH_ENTRA_ISSUER도 줘야 한다"
  need_secret auth-entra-secret "AUTH_ENTRA_ID를 준 배포"
fi

# ── 태그 정하기 ──────────────────────────────────────────────
commit_of() {
  if [ -z "${IMAGE_TAG:-}" ] && [ "${ALLOW_DIRTY:-}" != 1 ] && [ -n "$(git -C "$1" status --porcelain)" ]; then
    die "$1 에 커밋하지 않은 변경이 있다. 커밋하거나 ALLOW_DIRTY=1 을 준다"
  fi
  git -C "$1" rev-parse --short=12 HEAD
}
BACKEND_TAG=""; FRONTEND_TAG=""
if uses_backend_image; then BACKEND_TAG="${IMAGE_TAG:-$(commit_of "$ROOT")}"; fi
if selected frontend; then
  [ -f "$FRONTEND_DIR/Dockerfile" ] || die "$FRONTEND_DIR/Dockerfile 이 없다 (FRONTEND_DIR로 프런트 저장소를 가리킨다)"
  FRONTEND_TAG="${IMAGE_TAG:-$(commit_of "$FRONTEND_DIR")}"
  # 부르는 방식이 번들에 박히므로 같은 커밋이라도 태그를 따로 둔다
  if [ -z "${IMAGE_TAG:-}" ] && [ "$WEB_API" = proxy ]; then FRONTEND_TAG="$FRONTEND_TAG-proxy"; fi
fi

# ── 주소 (프런트 빌드 인자와 CORS에 쓴다) ────────────────────
if [ "$PLATFORM" = appservice ]; then
  APP_API="app-rsai-demo-api-$SUFFIX"; APP_WEB="app-rsai-demo-web-$SUFFIX"
  APP_VENDOR="app-rsai-demo-vendor-$SUFFIX"; APP_MCP="app-rsai-demo-mcp-$SUFFIX"
  HOST_API="$APP_API.azurewebsites.net"; HOST_WEB="$APP_WEB.azurewebsites.net"
  HOST_VENDOR="$APP_VENDOR.azurewebsites.net"; HOST_MCP="$APP_MCP.azurewebsites.net"
else
  DOMAIN="$(az containerapp env show -g "$RG" -n "$CAE" --query properties.defaultDomain -o tsv)"
  APP_API="ca-rsai-demo-api"; APP_WEB="ca-rsai-demo-web"; APP_VENDOR="ca-rsai-demo-vendor"; APP_MCP="ca-rsai-demo-mcp"
  HOST_API="$APP_API.$DOMAIN"; HOST_WEB="$APP_WEB.$DOMAIN"
  HOST_VENDOR="$APP_VENDOR.internal.$DOMAIN"; HOST_MCP="$APP_MCP.internal.$DOMAIN"   # 내부 수신
fi

if [ -z "${FOUNDRY_ENDPOINT:-}" ]; then
  FOUNDRY_ENDPOINT="$(az cognitiveservices account list -g "$RG" --query "[?$TAGQ && kind=='AIServices'] | [0].properties.endpoint" -o tsv)"
fi
if [ "$V3_LLM" = foundry ] && [ -z "$FOUNDRY_ENDPOINT" ] && uses_backend_image; then
  die "Foundry 주소를 찾지 못했다. FOUNDRY_ENDPOINT를 주거나 V3_LLM=rule 로 배포한다"
fi
# 프런트 번들에 박히는 v3 API 주소와, 프런트 서버가 요청을 넘길 백엔드 주소
if [ "$WEB_API" = proxy ]; then WEB_API_BASE="/api/v3"; else WEB_API_BASE="https://$HOST_API/api/v3"; fi
V3_BACKEND_URL="https://$HOST_API/api/v3"

# ── 1) 이미지 ────────────────────────────────────────────────
image_exists() { az acr repository show -n "$ACR" --image "$1" -o none 2>/dev/null; }

if uses_backend_image; then
  if image_exists "rsai-backend:$BACKEND_TAG"; then say "이미지 rsai-backend:$BACKEND_TAG 가 이미 있다. 다시 만들지 않는다"
  elif [ -n "${IMAGE_TAG:-}" ]; then die "IMAGE_TAG=$IMAGE_TAG 이미지가 레지스트리에 없다"
  else
    say "이미지 rsai-backend:$BACKEND_TAG 빌드 (EXTRAS='$BACKEND_EXTRAS')"
    az acr build -r "$ACR" -t "rsai-backend:$BACKEND_TAG" --build-arg "EXTRAS=$BACKEND_EXTRAS" "$ROOT"
  fi
fi
if selected frontend; then
  if image_exists "rsai-frontend:$FRONTEND_TAG"; then say "이미지 rsai-frontend:$FRONTEND_TAG 가 이미 있다. 다시 만들지 않는다 (백엔드 주소가 바뀌었으면 새 커밋으로 다시 만든다)"
  elif [ -n "${IMAGE_TAG:-}" ]; then die "IMAGE_TAG=$IMAGE_TAG 이미지가 레지스트리에 없다"
  else
    say "이미지 rsai-frontend:$FRONTEND_TAG 빌드 (백엔드 주소 https://$HOST_API, v3 API는 $WEB_API_BASE)"
    az acr build -r "$ACR" -t "rsai-frontend:$FRONTEND_TAG" \
      --build-arg "NEXT_PUBLIC_API_BASE_URL=https://$HOST_API/api/v1" \
      --build-arg "NEXT_PUBLIC_API_BASE=$WEB_API_BASE" "$FRONTEND_DIR"
  fi
fi

# ── 2) 마이그레이션 (실패하면 여기서 끝난다) ─────────────────
migrate() {
  say "마이그레이션: alembic upgrade head ($MIGRATE_MODE, 이미지 rsai-backend:$BACKEND_TAG)"
  if [ "$MIGRATE_MODE" = local ]; then
    (cd "$ROOT" && DATABASE_URL="$(kv_secret database-url)" uv run alembic upgrade head)
    return
  fi
  # ACR 작업의 단계는 이미지의 WORKDIR가 아니라 /workspace에서 돈다(ACR Tasks YAML 문서의 workingDirectory
  # 기본값). alembic.ini와 app 패키지는 /app(Dockerfile의 WORKDIR)에 있으므로 작업 폴더를 맞춘다.
  cat > "$TMP/migrate.yaml" <<'YAML'
version: v1.1.0
steps:
  - cmd: "{{.Run.Registry}}/{{.Values.image}} alembic upgrade head"
    workingDirectory: /app
    env:
      - "DATABASE_URL={{.Values.database_url}}"
    timeout: 900
YAML
  az acr run -r "$ACR" -f "$TMP/migrate.yaml" --set "image=rsai-backend:$BACKEND_TAG" \
    --set-secret "database_url=$(kv_secret database-url)" /dev/null
  local status
  status="$(az acr task list-runs -r "$ACR" --top 1 --query "[0].status" -o tsv)"
  [ "$status" = Succeeded ] || die "마이그레이션 실행이 $status 로 끝났다. 앱은 바꾸지 않았다"
}
if uses_backend_image; then migrate; fi

# ── 3) 앱 바꾸기 ─────────────────────────────────────────────
deploy_appservice() {
  local params=("acrName=$ACR" "keyVaultName=$KV" "v3Llm=$V3_LLM" "foundryEndpoint=$FOUNDRY_ENDPOINT"
    "foundryDeployment=$FOUNDRY_DEPLOYMENT" "vendorOrgId=$VENDOR_ORG_ID" "ollamaBaseUrl=$OLLAMA_BASE_URL"
    "v3Auth=$V3_AUTH" "v3DemoOpen=$V3_DEMO_OPEN"
    "anthropicModel=$ANTHROPIC_MODEL" "v3LlmAllVendors=$V3_LLM_ALL_VENDORS" "v3CorsOrigins=$V3_CORS_ORIGINS"
    "v3Distance=$V3_DISTANCE" "v3VendorAgents=$V3_VENDOR_AGENTS" "searchEndpoint=$AZURE_SEARCH_ENDPOINT"
    "searchIndex=$AZURE_SEARCH_INDEX" "enableAppInsights=$APP_INSIGHTS" "detectorEndpoint=$V3_DETECTOR_ENDPOINT"
    "detectorDeployment=$V3_DETECTOR_DEPLOYMENT" "authUrl=$AUTH_URL" "authDevLogin=$AUTH_DEV_LOGIN"
    "authEntraId=$AUTH_ENTRA_ID" "authEntraIssuer=$AUTH_ENTRA_ISSUER" "v3BackendUrl=$V3_BACKEND_URL")
  local apps=()
  if uses_backend_image; then params+=("backendImage=rsai-backend:$BACKEND_TAG"); fi
  if selected backend; then params+=("deployBackend=true"); apps+=("$APP_API"); fi
  if selected vendor-agent; then ensure_key vendor-service-key; params+=("deployVendorAgent=true"); apps+=("$APP_VENDOR"); fi
  if selected mcp; then ensure_key mcp-service-key; params+=("deployMcp=true"); apps+=("$APP_MCP"); fi
  if selected frontend; then
    ensure_key auth-secret
    params+=("deployFrontend=true" "frontendImage=rsai-frontend:$FRONTEND_TAG"); apps+=("$APP_WEB")
  fi
  az deployment group create -g "$RG" -n "rsai-apps-$(date +%Y%m%d%H%M%S)" -f "$HERE/appservice.bicep" \
    -p "${params[@]}" --query "{state:properties.provisioningState}" -o json
  # Key Vault 참조는 접근 정책이 생긴 뒤에 풀린다. 한 번 다시 시작해 새 이미지와 비밀을 함께 읽게 한다.
  local app
  for app in "${apps[@]}"; do say "$app 다시 시작"; az webapp restart -g "$RG" -n "$app" -o none; done
  # 풀리지 않은 Key Vault 참조가 있으면 앱은 참조 글을 값으로 읽는다(AUTH_SECRET이면 누구나 아는 글이 된다).
  # 상태만 보고 값은 찍지 않는다. 이 확인 자체가 실패하면 배포를 막지 않고 손으로 확인하라고 알린다.
  local sub bad
  sub="$(az account show --query id -o tsv)"
  for app in "${apps[@]}"; do
    sleep 20
    if bad="$(az rest --method get --url "https://management.azure.com/subscriptions/$sub/resourceGroups/$RG/providers/Microsoft.Web/sites/$app/config/configreferences/appsettings?api-version=2023-12-01" \
        --query "value[?properties.status!='Resolved' && properties.status!='Initialized'].name" -o tsv 2>/dev/null)"; then
      if [ -n "$bad" ]; then
        die "$app 의 Key Vault 참조가 풀리지 않았다(또는 상태를 읽지 못했다): $(echo "$bad" | tr '\n' ' '). 포털의 환경 변수 화면에서 확인하고, 비밀 이름과 접근 정책을 고친 뒤 az webapp restart 한다. 그 전에는 주소를 알리지 않는다"
      fi
      say "$app 의 Key Vault 참조에 실패로 나온 것이 없다"
    else
      say "경고: $app 의 Key Vault 참조 상태를 읽지 못했다. 포털의 환경 변수 화면에서 참조가 초록색인지 확인한다"
    fi
  done
}

# Container App 하나를 만들거나 새 이미지로 바꾼다.
#   ca_app <이름> <이미지> <포트> <external|internal> -- <환경 변수 K=V ...> -- <비밀 k=v ...>
ca_app() {
  local name="$1" image="$2" port="$3" ingress="$4"; shift 5
  local envs=() secrets=() secret_args=()
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done
  if [ "$#" -gt 0 ]; then shift; fi
  while [ "$#" -gt 0 ]; do secrets+=("$1"); shift; done
  # macOS 기본 bash(3.2)는 set -u 에서 빈 배열을 펼치면 오류를 낸다. 비밀이 있을 때만 인자를 만든다.
  if [ "${#secrets[@]}" -gt 0 ]; then secret_args=(--secrets "${secrets[@]}"); fi
  if az containerapp show -g "$RG" -n "$name" -o none 2>/dev/null; then
    say "$name 을 새 이미지로 바꾼다"
    if [ "${#secrets[@]}" -gt 0 ]; then az containerapp secret set -g "$RG" -n "$name" "${secret_args[@]}" -o none; fi
    az containerapp update -g "$RG" -n "$name" --image "$SERVER/$image" --set-env-vars "${envs[@]}" -o none
  else
    say "$name 을 새로 만든다"
    az containerapp create -g "$RG" -n "$name" --environment "$CAE" --image "$SERVER/$image" \
      --target-port "$port" --ingress "$ingress" --revisions-mode single --min-replicas 0 --max-replicas 1 \
      --registry-server "$SERVER" --registry-username "$(kv_secret acr-username)" \
      --registry-password "$(kv_secret acr-password)" \
      ${secret_args[@]+"${secret_args[@]}"} --env-vars "${envs[@]}" \
      --tags project=resource-ai env=demo managed-by=agent -o none
  fi
}

deploy_containerapps() {
  local model_env=("V3_LLM=$V3_LLM" "FOUNDRY_ENDPOINT=$FOUNDRY_ENDPOINT" "FOUNDRY_DEPLOYMENT=$FOUNDRY_DEPLOYMENT"
    "OLLAMA_BASE_URL=$OLLAMA_BASE_URL" "DATABASE_URL=secretref:database-url" "FOUNDRY_API_KEY=secretref:foundry-api-key"
    "ANTHROPIC_MODEL=$ANTHROPIC_MODEL" "V3_LLM_ALL_VENDORS=$V3_LLM_ALL_VENDORS")
  local model_secret=()
  local db foundry
  if uses_backend_image; then db="$(kv_secret database-url)"; foundry="$(kv_secret foundry-api-key)"; fi
  # anthropic을 골랐을 때만 키를 비밀로 넣는다. API와 업체 측 에이전트가 같이 쓴다.
  if [ "$V3_LLM" = anthropic ] && uses_backend_image; then
    model_env+=("ANTHROPIC_API_KEY=secretref:anthropic-api-key")
    model_secret=("anthropic-api-key=$(kv_secret anthropic-api-key)")
  fi
  if selected backend; then
    # token 모드일 때만 조직별 토큰을 비밀로 넣는다. header 모드에서는 앱이 V3_ORG_TOKENS를 읽지 않는다.
    local auth_env=("V3_AUTH=$V3_AUTH" "V3_DEMO_OPEN=$V3_DEMO_OPEN") auth_secret=()
    if [ "$V3_AUTH" = token ]; then
      auth_env+=("V3_ORG_TOKENS=secretref:v3-org-tokens")
      auth_secret=("v3-org-tokens=$(kv_secret v3-org-tokens)")
    fi
    # 고른 연결만 넣는다. 비밀은 그 연결을 켰을 때만 읽는다.
    local opt_env=("V3_CORS_ORIGINS=$V3_CORS_ORIGINS" "V3_DISTANCE=$V3_DISTANCE" "V3_VENDOR_AGENTS=$V3_VENDOR_AGENTS"
      "AZURE_SEARCH_ENDPOINT=$AZURE_SEARCH_ENDPOINT" "AZURE_SEARCH_INDEX=$AZURE_SEARCH_INDEX"
      "V3_DETECTOR_ENDPOINT=$V3_DETECTOR_ENDPOINT" "V3_DETECTOR_DEPLOYMENT=$V3_DETECTOR_DEPLOYMENT") opt_secret=()
    if [ "$V3_DISTANCE" = kakao ]; then
      opt_env+=("KAKAO_REST_API_KEY=secretref:kakao-rest-api-key"); opt_secret+=("kakao-rest-api-key=$(kv_secret kakao-rest-api-key)")
    fi
    if [ "$V3_DISTANCE" = azure ]; then
      opt_env+=("AZURE_MAPS_KEY=secretref:maps-key"); opt_secret+=("maps-key=$(kv_secret maps-key)")
    fi
    if [ "$V3_VENDOR_AGENTS" != '{}' ]; then
      opt_env+=("VENDOR_SERVICE_KEY=secretref:vendor-service-key"); opt_secret+=("vendor-service-key=$(kv_secret vendor-service-key)")
    fi
    if [ -n "$AZURE_SEARCH_ENDPOINT" ]; then
      opt_env+=("AZURE_SEARCH_API_KEY=secretref:search-admin-key"); opt_secret+=("search-admin-key=$(kv_secret search-admin-key)")
    fi
    if [ "$APP_INSIGHTS" = true ]; then
      opt_env+=("APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:appinsights-connection-string")
      opt_secret+=("appinsights-connection-string=$(kv_secret appinsights-connection-string)")
    fi
    if [ -n "$V3_DETECTOR_ENDPOINT" ]; then
      opt_env+=("V3_DETECTOR_API_KEY=secretref:detector-api-key"); opt_secret+=("detector-api-key=$(kv_secret detector-api-key)")
    fi
    ca_app "$APP_API" "rsai-backend:$BACKEND_TAG" 8000 external -- \
      APP_ROLE=api APP_ENV=demo "CORS_ORIGINS=[\"https://$HOST_WEB\"]" "${model_env[@]}" "${auth_env[@]}" "${opt_env[@]}" \
      AZURE_STORAGE_CONNECTION_STRING=secretref:storage-connection-string -- \
      "database-url=$db" "foundry-api-key=$foundry" "storage-connection-string=$(kv_secret storage-connection-string)" \
      ${auth_secret[@]+"${auth_secret[@]}"} ${model_secret[@]+"${model_secret[@]}"} ${opt_secret[@]+"${opt_secret[@]}"}
  fi
  if selected vendor-agent; then
    ensure_key vendor-service-key
    ca_app "$APP_VENDOR" "rsai-backend:$BACKEND_TAG" 8000 internal -- \
      APP_ROLE=vendor-agent APP_ENV=demo "VENDOR_ORG_ID=$VENDOR_ORG_ID" "${model_env[@]}" \
      VENDOR_SERVICE_KEY=secretref:vendor-service-key -- \
      "database-url=$db" "foundry-api-key=$foundry" "vendor-service-key=$(kv_secret vendor-service-key)" \
      ${model_secret[@]+"${model_secret[@]}"}
  fi
  if selected mcp; then
    ensure_key mcp-service-key
    ca_app "$APP_MCP" "rsai-backend:$BACKEND_TAG" 8000 internal -- \
      APP_ROLE=mcp APP_ENV=demo DATABASE_URL=secretref:database-url MCP_SERVICE_KEY=secretref:mcp-service-key -- \
      "database-url=$db" "mcp-service-key=$(kv_secret mcp-service-key)"
  fi
  if selected frontend; then
    ensure_key auth-secret
    local web_env=(PORT=3000 HOSTNAME=0.0.0.0 "AUTH_URL=${AUTH_URL:-https://$HOST_WEB}" AUTH_TRUST_HOST=true
      "AUTH_DEV_LOGIN=$AUTH_DEV_LOGIN" "V3_BACKEND_URL=$V3_BACKEND_URL" AUTH_SECRET=secretref:auth-secret)
    local web_secret=("auth-secret=$(kv_secret auth-secret)")
    if [ "$V3_AUTH" = token ]; then
      web_env+=("V3_ORG_TOKENS=secretref:v3-org-tokens"); web_secret+=("v3-org-tokens=$(kv_secret v3-org-tokens)")
    fi
    if [ -n "$AUTH_ENTRA_ID" ]; then
      web_env+=("AUTH_MICROSOFT_ENTRA_ID_ID=$AUTH_ENTRA_ID" "AUTH_MICROSOFT_ENTRA_ID_ISSUER=$AUTH_ENTRA_ISSUER"
        AUTH_MICROSOFT_ENTRA_ID_SECRET=secretref:auth-entra-secret)
      web_secret+=("auth-entra-secret=$(kv_secret auth-entra-secret)")
    fi
    ca_app "$APP_WEB" "rsai-frontend:$FRONTEND_TAG" 3000 external -- "${web_env[@]}" -- "${web_secret[@]}"
  fi
}

if [ "$PLATFORM" = appservice ]; then deploy_appservice; else deploy_containerapps; fi

# ── 4) 상태 확인 ─────────────────────────────────────────────
ROLLBACK_TAG="<이전 SHA>"
wait_healthy() {
  local url="$1" i
  for i in $(seq 1 36); do
    if curl -fsS -o /dev/null --max-time 10 "$url"; then say "정상: $url"; return 0; fi
    sleep 10
  done
  die "상태 확인 실패: $url. 되돌리기: IMAGE_TAG=$ROLLBACK_TAG bash infra/deploy.sh $WHAT"
}
if selected backend; then wait_healthy "https://$HOST_API/api/v1/health"; fi
if selected frontend; then wait_healthy "https://$HOST_WEB/"; fi
if [ "$PLATFORM" = appservice ]; then
  if selected vendor-agent; then wait_healthy "https://$HOST_VENDOR/healthz"; fi
  if selected mcp; then wait_healthy "https://$HOST_MCP/healthz"; fi
else
  # 내부 수신이라 밖에서 부를 수 없다. 리비전 상태만 본다.
  for app in "$APP_VENDOR" "$APP_MCP"; do
    if { [ "$app" = "$APP_VENDOR" ] && selected vendor-agent; } || { [ "$app" = "$APP_MCP" ] && selected mcp; }; then
      say "$app 리비전: $(az containerapp revision list -g "$RG" -n "$app" --query "[?properties.active] | [0].{name:name,health:properties.healthState,running:properties.runningState}" -o tsv)"
    fi
  done
fi

say "끝. API https://$HOST_API  프런트 https://$HOST_WEB"
if uses_backend_image; then say "백엔드 이미지 태그 $BACKEND_TAG (되돌릴 때 IMAGE_TAG로 쓴다)"; fi
