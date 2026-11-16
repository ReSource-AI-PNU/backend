#!/usr/bin/env bash
# stop.sh로 멈춘 리소스를 다시 켠다. 시연 당일에는 이 스크립트 → 상태 확인 → seed-demo.sh 순서로 쓴다.
set -uo pipefail
RG="${RG:-team_AIBIC2026_03_RG_EUS2}"
TAG="tags.project=='resource-ai'"
say() { echo "[start] $*"; }

for s in $(az postgres flexible-server list -g "$RG" --query "[?$TAG].name" -o tsv); do
  state="$(az postgres flexible-server show -g "$RG" -n "$s" --query state -o tsv)"
  if [ "$state" = Stopped ]; then say "PostgreSQL $s 시작"; az postgres flexible-server start -g "$RG" -n "$s" -o none
  else say "PostgreSQL $s 상태 $state, 건너뜀"; fi
done

for v in $(az vm list -g "$RG" --query "[?$TAG].name" -o tsv); do
  say "VM $v 시작"; az vm start -g "$RG" -n "$v" -o none
done

for id in $(az resource list -g "$RG" --resource-type Microsoft.Fabric/capacities --query "[?$TAG].id" -o tsv 2>/dev/null); do
  say "Fabric $(basename "$id") 재개"; az resource invoke-action --ids "$id" --action resume --api-version 2023-11-01 -o none
done

say "Azure ML 온라인 배포는 모델 배포 스크립트로 다시 올린다 (3단계에서 추가)."
say "Container Apps는 첫 요청이 오면 스스로 깨어난다."
