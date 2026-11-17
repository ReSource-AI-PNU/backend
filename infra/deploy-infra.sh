#!/usr/bin/env bash
# Re:Source AI 인프라 배포. 항상 what-if로 먼저 확인하고 apply로 만든다.
#   bash infra/deploy-infra.sh what-if base
#   bash infra/deploy-infra.sh apply ai
# 묶음: base(기반) | ai(AI·데이터) | ollama(GPU VM) | ml(Azure ML·Fabric) | all
# 공급자 등록이나 할당량이 풀린 뒤 켜는 것: ENABLE_CONTAINER_APPS, ENABLE_MAPS, ENABLE_FABRIC, ENABLE_GPU_CLUSTER (true/false)
set -euo pipefail

RG="${RG:-team_AIBIC2026_03_RG_EUS2}"
MODE="${1:-what-if}"
BUNDLE="${2:-base}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$MODE" in what-if|apply) ;; *) echo "사용법: $0 what-if|apply base|ai|ollama|ml|all" >&2; exit 2 ;; esac
case "$BUNDLE" in base|ai|ollama|ml|all) ;; *) echo "묶음은 base|ai|ollama|ml|all 중 하나" >&2; exit 2 ;; esac

flag() { if [ "$BUNDLE" = all ] || [ "$BUNDLE" = "$1" ]; then echo true; else echo false; fi; }

DEPLOYER_OID="$(az ad signed-in-user show --query id -o tsv)"
PARAMS=(
  "deployBase=$(flag base)" "deployAi=$(flag ai)" "deployOllama=$(flag ollama)" "deployMl=$(flag ml)"
  "deployerObjectId=$DEPLOYER_OID"
  "enableContainerApps=${ENABLE_CONTAINER_APPS:-false}"
  "enableMaps=${ENABLE_MAPS:-false}"
  "enableFabric=${ENABLE_FABRIC:-false}"
  "enableGpuCluster=${ENABLE_GPU_CLUSTER:-false}"
)
[ -n "${CHAT_MODEL_CAPACITY:-}" ] && PARAMS+=("chatModelCapacity=$CHAT_MODEL_CAPACITY")
[ -n "${POSTGRES_SKU:-}" ] && PARAMS+=("postgresSku=$POSTGRES_SKU")
[ -n "${POSTGRES_ZONE:-}" ] && PARAMS+=("postgresZone=$POSTGRES_ZONE")
[ -n "${POSTGRES_LOCATION:-}" ] && PARAMS+=("postgresLocation=$POSTGRES_LOCATION")
[ -n "${SEARCH_LOCATION:-}" ] && PARAMS+=("searchLocation=$SEARCH_LOCATION")
[ -n "${OLLAMA_VM_SIZE:-}" ] && PARAMS+=("ollamaVmSize=$OLLAMA_VM_SIZE")
[ -n "${FABRIC_ADMINS:-}" ] && PARAMS+=("fabricAdmins=$FABRIC_ADMINS")

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

if [ "$(flag base)" = true ]; then
  # PostgreSQL 관리자 비밀번호: 이미 Key Vault에 있으면 그대로 쓰고, 없으면 새로 만든다. 화면과 로그에 찍지 않는다.
  KV="$(az keyvault list -g "$RG" --query "[?tags.project=='resource-ai'] | [0].name" -o tsv)"
  PGPASS=""
  if [ -n "$KV" ]; then
    PGPASS="$(az keyvault secret show --vault-name "$KV" -n postgres-admin-password --query value -o tsv 2>/dev/null || true)"
  fi
  if [ -z "$PGPASS" ]; then
    PGPASS="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-28)Aa1"
  fi
  PARAMS+=("postgresAdminPassword=$PGPASS")
fi

if [ "$(flag ollama)" = true ]; then
  # VM에는 공용 IP가 없다. 관리 작업은 az vm run-command로 하므로 SSH 키는 한 번 쓰고 버린다.
  ssh-keygen -q -t ed25519 -N '' -f "$TMP/ollama" >/dev/null
  PARAMS+=("ollamaSshPublicKey=$(cat "$TMP/ollama.pub")")
fi

if [ "$MODE" = what-if ]; then
  az deployment group what-if -g "$RG" -f "$HERE/main.bicep" -p "${PARAMS[@]}" --exclude-change-types Ignore NoChange
else
  NAME="rsai-${BUNDLE}-$(date +%Y%m%d%H%M%S)"
  az deployment group create -g "$RG" -n "$NAME" -f "$HERE/main.bicep" -p "${PARAMS[@]}" \
    --query "{deployment:name, state:properties.provisioningState, names:properties.outputs.resourceNames.value}" -o json
fi
