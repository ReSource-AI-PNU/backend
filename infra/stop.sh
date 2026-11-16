#!/usr/bin/env bash
# 비용이 드는 리소스를 멈춘다. 단계가 끝날 때와 시연이 끝난 뒤에 실행한다.
set -uo pipefail
RG="${RG:-team_AIBIC2026_03_RG_EUS2}"
ML_WS="${ML_WS:-team-workspace}"
TAG="tags.project=='resource-ai'"
say() { echo "[stop] $*"; }

# 1) PostgreSQL 중지 (중지 상태는 7일 뒤 Azure가 자동으로 다시 켠다)
for s in $(az postgres flexible-server list -g "$RG" --query "[?$TAG].name" -o tsv); do
  state="$(az postgres flexible-server show -g "$RG" -n "$s" --query state -o tsv)"
  if [ "$state" = Ready ]; then say "PostgreSQL $s 중지"; az postgres flexible-server stop -g "$RG" -n "$s" -o none
  else say "PostgreSQL $s 상태 $state, 건너뜀"; fi
done

# 2) GPU VM 할당 해제
for v in $(az vm list -g "$RG" --query "[?$TAG].name" -o tsv); do
  say "VM $v 할당 해제"; az vm deallocate -g "$RG" -n "$v" -o none
done

# 3) Fabric 용량 일시 중지
for id in $(az resource list -g "$RG" --resource-type Microsoft.Fabric/capacities --query "[?$TAG].id" -o tsv 2>/dev/null); do
  say "Fabric $(basename "$id") 일시 중지"; az resource invoke-action --ids "$id" --action suspend --api-version 2023-11-01 -o none
done

# 4) Azure ML 온라인 배포 내리기 (엔드포인트는 남기고 배포만 지운다. rsai- 로 시작하는 것만)
for e in $(az ml online-endpoint list -g "$RG" -w "$ML_WS" --query "[?starts_with(name,'rsai-')].name" -o tsv 2>/dev/null); do
  for d in $(az ml online-deployment list -g "$RG" -w "$ML_WS" -e "$e" --query "[].name" -o tsv); do
    say "ML 배포 $e/$d 삭제"; az ml online-endpoint update -g "$RG" -w "$ML_WS" -n "$e" --traffic "$d=0" -o none 2>/dev/null
    az ml online-deployment delete -g "$RG" -w "$ML_WS" -e "$e" -n "$d" --yes --no-wait
  done
done

# 5) Container Apps 최소 복제본 0
if [ "$(az provider show -n Microsoft.App --query registrationState -o tsv 2>/dev/null)" = Registered ]; then
  for a in $(az containerapp list -g "$RG" --query "[?$TAG].name" -o tsv 2>/dev/null); do
    say "Container App $a 최소 복제본 0"; az containerapp update -g "$RG" -n "$a" --min-replicas 0 -o none
  done
fi

say "멈출 수 없어 계속 과금되는 것:"
az resource list -g "$RG" --query "[?$TAG && (type=='Microsoft.Search/searchServices' || type=='Microsoft.ContainerRegistry/registries')].{name:name,type:type,sku:sku.name}" -o table
