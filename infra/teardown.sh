#!/usr/bin/env bash
# project=resource-ai, managed-by=agent 태그가 붙은 리소스만 지운다. 목록을 보여 주고 확인을 받는다.
# 리소스 그룹과 태그가 없는 리소스(다른 사람이 만든 것)는 건드리지 않는다.
set -uo pipefail
RG="${RG:-team_AIBIC2026_03_RG_EUS2}"
FILTER="[?tags.project=='resource-ai' && tags.\"managed-by\"=='agent']"

echo "지울 리소스 ($RG):"
az resource list -g "$RG" --query "$FILTER.{name:name,type:type,location:location}" -o table
COUNT="$(az resource list -g "$RG" --query "length($FILTER)" -o tsv)"
if [ "$COUNT" = 0 ]; then echo "태그가 붙은 리소스가 없다."; exit 0; fi

read -r -p "위 ${COUNT}개를 지운다. 계속하려면 리소스 그룹 이름을 입력: " answer
if [ "$answer" != "$RG" ]; then echo "취소"; exit 1; fi

KVS="$(az keyvault list -g "$RG" --query "$FILTER.name" -o tsv)"
COGS="$(az cognitiveservices account list -g "$RG" --query "$FILTER.{n:name,l:location}" -o tsv)"

# 의존 관계 때문에 한 번에 안 지워질 수 있어 세 번까지 되풀이한다.
for i in 1 2 3; do
  IDS="$(az resource list -g "$RG" --query "$FILTER.id" -o tsv)"
  [ -z "$IDS" ] && break
  echo "삭제 $i회차"
  # shellcheck disable=SC2086
  az resource delete --ids $IDS -o none 2>/dev/null || true
  sleep 20
done

# 일시 삭제된 것까지 지워야 같은 이름으로 다시 만들 수 있다.
for kv in $KVS; do echo "Key Vault $kv 완전 삭제"; az keyvault purge -n "$kv" -o none 2>/dev/null || true; done
echo "$COGS" | while read -r n l; do
  [ -z "$n" ] && continue
  echo "AI 계정 $n 완전 삭제"; az cognitiveservices account purge -g "$RG" -n "$n" -l "$l" -o none 2>/dev/null || true
done

echo "남은 태그 리소스:"
az resource list -g "$RG" --query "$FILTER.{name:name,type:type}" -o table
