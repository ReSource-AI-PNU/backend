#!/usr/bin/env bash
# GPU VM(vm-rsai-gpu, 시간당 1.6달러) 켜기·끄기·상태. az 로그인과 SSH 키가 있는 곳(맥)에서 돌린다.
#
#   bash infra/gpu/gpu.sh up       # 켜고 Ollama가 답할 때까지 기다린다(꺼져 있을 때 약 2~3분)
#   bash infra/gpu/gpu.sh down     # 할당 해제(디스크·공용 IP 비용만 남는다)
#   bash infra/gpu/gpu.sh status   # 전원 상태와 Ollama를 마지막으로 쓴 뒤 지난 분
#   bash infra/gpu/gpu.sh auto [분]   # 쓸 때만 켠다: 꺼져 있는데 앱이 Ollama를 찾았으면 켜고, 켜져 있는데 그만큼(기본 5분)
#                                    # 쓰지 않았으면 끈다. 맥의 launchd가 1분마다 부른다(install-gpu-auto.sh)
#   bash infra/gpu/gpu.sh idle-stop [분]   # 켜져 있고 그만큼 쓰지 않았으면 끄기만 한다
#
# 꺼져 있어도 서비스는 멈추지 않는다: 배포가 OLLAMA_FALLBACK=1을 넣어, Ollama에 닿지 않으면 그 차례는 Foundry가 답한다
# (운영 화면에는 설정한 모델과 실제로 답한 모델이 따로 나온다). 그때 백엔드가 남기는 경고("Ollama에 닿지 않아")가
# 켜 달라는 신호다. 켜지는 데 2~3분이 걸리므로 꺼진 뒤 첫 호출은 Foundry가 답하고, 켜진 뒤의 차례부터 Ollama가 답한다.
# 구독 권한(리소스 그룹 Contributor)으로는 VM이 스스로 끄는 역할 할당도, Azure 예약 종료(DevTestLab)·Automation도
# 쓸 수 없어서 끄기는 맥에서 한다(infra/gpu/install-idle-watch.sh).
set -euo pipefail
RG="${RG:-team_AIBIC2026_03_RG_EUS2}"
GPU_VM="${GPU_VM:-vm-rsai-gpu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/rsai_demo_vm}"
APP_HOST="${APP_HOST:-rsai-o7xpbty4q5vgs.centralus.cloudapp.azure.com}"
DEMAND_TEXT="Ollama에 닿지 않아"
KEEP_ALIVE_MIN=1440  # setup-ollama.sh의 OLLAMA_KEEP_ALIVE=24h
say() { echo "[gpu $(date '+%m-%d %H:%M')] $*"; }

power() { az vm get-instance-view -g "$RG" -n "$GPU_VM" --query "instanceView.statuses[?starts_with(code,'PowerState/')].code | [0]" -o tsv 2>/dev/null | sed 's#PowerState/##'; }
addr() { az vm list-ip-addresses -g "$RG" -n "$GPU_VM" --query "[0].virtualMachine.network.$1" -o tsv; }

ssh_ready() {
  local ip mine rule
  ip="$(addr 'publicIpAddresses[0].ipAddress')"
  mine="$(curl -fsS -m 10 https://api.ipify.org)"
  rule="$(az network nsg rule show -g "$RG" --nsg-name nsg-rsai-gpu -n AllowSshFromAdmin --query sourceAddressPrefix -o tsv)"
  if [ "$rule" != "$mine/32" ]; then
    az network nsg rule update -g "$RG" --nsg-name nsg-rsai-gpu -n AllowSshFromAdmin --source-address-prefixes "$mine/32" -o none
  fi
  SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes "rsai@$ip")
}

# 지난 n분 동안 앱이 꺼진 Ollama를 찾은 횟수(백엔드 경고). 앱 VM SSH 허용 주소도 지금 IP로 맞춘다
app_demand() {
  local mine rule
  mine="$(curl -fsS -m 10 https://api.ipify.org)"
  rule="$(az network nsg rule show -g "$RG" --nsg-name nsg-rsai-demo-app -n AllowSshFromAdmin --query sourceAddressPrefix -o tsv)"
  if [ "$rule" != "$mine/32" ]; then
    az network nsg rule update -g "$RG" --nsg-name nsg-rsai-demo-app -n AllowSshFromAdmin --source-address-prefixes "$mine/32" -o none
  fi
  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes "rsai@$APP_HOST" \
    "sudo docker logs --since ${1}m rsai-backend-1 2>&1 | grep -c '$DEMAND_TEXT' || true"
}

# Ollama를 마지막으로 쓴 뒤 지난 분. 모델이 올라가 있으면 (지금 - (만료 - 24시간)), 없으면 켠 뒤 지난 분
idle_minutes() {
  local private
  private="$(addr 'privateIpAddresses[0]')"
  "${SSH[@]}" "PRIVATE=$private KEEP=$KEEP_ALIVE_MIN python3 -" <<'PY'
import calendar, json, os, re, time, urllib.request
def parse(ts):
    # Ollama: 2026-11-19T14:40:12.123456789Z 또는 ...+09:00 (나노초, 파이썬 3.10의 fromisoformat이 못 읽는다)
    y, mo, d, h, mi, s, tz = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)$", ts).groups()
    base = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s)))
    if tz != "Z":
        base -= (1 if tz[0] == "+" else -1) * (int(tz[1:3]) * 3600 + int(tz[4:6]) * 60)
    return base
now = time.time()
try:
    ps = json.loads(urllib.request.urlopen(f"http://{os.environ['PRIVATE']}:11434/api/ps", timeout=5).read())
except Exception:
    ps = {"models": []}
used = [parse(m["expires_at"]) - int(os.environ["KEEP"]) * 60 for m in ps.get("models") or []]
last = max(used) if used else now - float(open("/proc/uptime").read().split()[0])
print(max(0, int((now - last) // 60)))
PY
}

case "${1:-status}" in
  up)
    state="$(power)"
    if [ "$state" != running ]; then say "GPU VM 켜는 중($state)"; az vm start -g "$RG" -n "$GPU_VM" -o none; fi
    ssh_ready
    private="$(addr 'privateIpAddresses[0]')"
    for _ in $(seq 1 60); do
      "${SSH[@]}" "curl -fsS -m 3 http://$private:11434/api/version" >/dev/null 2>&1 && break
      sleep 5
    done
    "${SSH[@]}" "curl -fsS -m 3 http://$private:11434/api/version" >/dev/null || { say "Ollama가 답하지 않는다"; exit 1; }
    # 첫 답이 느리지 않게 모델을 미리 올린다
    "${SSH[@]}" "curl -fsS -m 120 http://$private:11434/api/generate -d '{\"model\":\"qwen2.5:7b\",\"prompt\":\"안녕\",\"stream\":false,\"options\":{\"num_predict\":1}}'" >/dev/null || true
    say "켜짐: Ollama 준비됨 (http://$private:11434)"
    ;;
  down)
    state="$(power)"
    if [ "$state" = deallocated ]; then say "이미 꺼져 있음"; exit 0; fi
    say "GPU VM 할당 해제"; az vm deallocate -g "$RG" -n "$GPU_VM" -o none; say "꺼짐"
    ;;
  status)
    state="$(power)"
    if [ "$state" = running ]; then ssh_ready; say "켜짐 · Ollama 마지막 사용 뒤 $(idle_minutes)분"; else say "상태: $state"; fi
    ;;
  idle-stop)
    limit="${2:-60}"
    state="$(power)"
    [ "$state" = running ] || exit 0
    ssh_ready
    idle="$(idle_minutes 2>/dev/null || echo "")"
    if [ -z "$idle" ]; then say "쓴 시각을 읽지 못해 끄지 않음"; exit 0; fi
    if [ "$idle" -ge "$limit" ]; then
      say "Ollama를 ${idle}분 쓰지 않아 끈다(기준 ${limit}분)"; az vm deallocate -g "$RG" -n "$GPU_VM" -o none; say "꺼짐"
    else
      say "켜 둠: 마지막 사용 뒤 ${idle}분(기준 ${limit}분)"
    fi
    ;;
  auto)
    limit="${2:-5}"
    state="$(power)"
    demand="$(app_demand "$limit" 2>/dev/null || echo 0)"
    demand="${demand//[^0-9]/}"; demand="${demand:-0}"
    if [ "$state" != running ]; then
      if [ "$demand" -gt 0 ]; then say "앱이 Ollama를 찾았다(${demand}번). 켠다"; exec bash "$0" up; fi
      exit 0
    fi
    ssh_ready
    idle="$(idle_minutes 2>/dev/null || echo "")"
    if [ -z "$idle" ]; then say "쓴 시각을 읽지 못해 끄지 않음"; exit 0; fi
    if [ "$idle" -ge "$limit" ] && [ "$demand" -eq 0 ]; then
      say "Ollama를 ${idle}분 쓰지 않아 끈다(기준 ${limit}분)"; az vm deallocate -g "$RG" -n "$GPU_VM" -o none; say "꺼짐"
    fi
    ;;
  *) echo "쓰는 법: $0 up | down | status | auto [분] | idle-stop [분]"; exit 2 ;;
esac
