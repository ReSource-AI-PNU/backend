#!/usr/bin/env bash
# H100 계산 인스턴스(Azure ML 작업 영역 team-workspace의 rsai-h100, Standard_NC40ads_H100_v5, H100 NVL 94GB)를
# 켜고 끄고 상태를 본다. 업체 측 오픈 모델(Ollama)이 여기서 돈다. az 로그인과 SSH 키가 있는 곳(맥)에서 돌린다.
#
#   bash infra/h100/h100.sh up        # 켜고 Ollama가 답할 때까지 기다린다. 사설 주소가 바뀌었으면 앱의 Ollama 주소도 고친다
#   bash infra/h100/h100.sh down      # 끈다. 서비스하지 않을 때 끈다(디스크와 받은 모델은 남는다)
#   bash infra/h100/h100.sh status    # 상태, 사설 주소, 받은 모델, 앱이 쓰는 Ollama 주소
#   bash infra/h100/h100.sh ip        # 사설 주소
#   bash infra/h100/h100.sh ssh [명령]
#
# 2026-11-19부터 A10 GPU VM(infra/gpu) 대신 쓴다. 조직위가 H100은 "서비스하지 않을 때 꺼 달라"고 했으므로, 쓸 때만
# 켜는 맥의 감시 작업(infra/gpu/install-gpu-auto.sh)은 붙이지 않고 서비스하는 동안 켜 두었다가 끝나면 down으로 끈다.
# 꺼져 있어도 서비스는 멈추지 않는다: 배포가 OLLAMA_FALLBACK=1을 넣어, Ollama에 닿지 않는 차례는 Foundry가 답한다.
# 네트워크: vnet-rsai-h100(eastus2, 10.72.0.0/24)이 앱 VNet(10.70.0.0/16)과 피어링돼 있고, NSG는 11434와 SSH를 앱 VNet에만
# 연다. SSH 키는 RSA여야 한다(계산 인스턴스가 ssh-rsa만 받는다, ~/.ssh/rsai_h100).
set -euo pipefail
RG="${RG:-team_AIBIC2026_03_RG_EUS2}"
WORKSPACE="${WORKSPACE:-team-workspace}"
COMPUTE="${COMPUTE:-rsai-h100}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/rsai_h100}"
APP_KEY="${APP_KEY:-$HOME/.ssh/rsai_demo_vm}"
APP_HOST="${APP_HOST:-rsai-o7xpbty4q5vgs.centralus.cloudapp.azure.com}"
API="2024-04-01"
say() { echo "[h100 $(date '+%m-%d %H:%M')] $*"; }

SUB="$(az account show --query id -o tsv)"
URL="https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.MachineLearningServices/workspaces/$WORKSPACE/computes/$COMPUTE"
get() { az rest --method get --url "$URL?api-version=$API" --query "$1" -o tsv; }
state() { get "properties.properties.state"; }
private_ip() { get "properties.properties.connectivityEndpoints.privateIpAddress"; }

ssh_cmd() {
  # 계산 인스턴스의 공용 SSH(50000)는 닿지 않아서 앱 VM을 거쳐 사설 주소로 들어간다(NSG AllowSshFromApp)
  app true  # 앱 VM SSH 허용 주소를 먼저 맞춘다
  local port user
  port="$(get properties.properties.sshSettings.sshPort)"
  user="$(get properties.properties.sshSettings.adminUserName)"
  SSH=(ssh -i "$SSH_KEY" -p "$port" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o BatchMode=yes
    -o ServerAliveInterval=30 -o "ProxyCommand=ssh -i $APP_KEY -o StrictHostKeyChecking=accept-new -o BatchMode=yes -W %h:%p rsai@$APP_HOST"
    "$user@$(private_ip)")
}

# 앱 VM SSH는 관리자 공용 IP 하나에만 열려 있다. 이 컴퓨터의 공용 IP가 바뀌었으면 맞춘다(한 번만)
app_ready=""
app() {
  if [ -z "$app_ready" ]; then
    local mine rule
    mine="$(curl -fsS -m 10 https://api.ipify.org)"
    rule="$(az network nsg rule show -g "$RG" --nsg-name nsg-rsai-demo-app -n AllowSshFromAdmin --query sourceAddressPrefix -o tsv)"
    if [ "$rule" != "$mine/32" ]; then
      az network nsg rule update -g "$RG" --nsg-name nsg-rsai-demo-app -n AllowSshFromAdmin --source-address-prefixes "$mine/32" -o none
    fi
    app_ready=1
  fi
  ssh -i "$APP_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes "rsai@$APP_HOST" "$@"
}

# 앱이 쓰는 Ollama 주소가 H100의 사설 주소와 다르면 고치고 백엔드만 다시 띄운다(배포하지 않아도 된다)
point_app() {
  local want now
  [ "${SKIP_APP:-}" = 1 ] && return  # 배포가 앱 설정을 새로 쓸 때
  want="http://$1:11434"
  now="$(app "sudo grep -E '^OLLAMA_BASE_URL=' /opt/rsai/env/backend.env | cut -d= -f2-" || true)"
  if [ "$now" = "$want" ]; then
    say "앱의 Ollama 주소가 이미 $want"
    return
  fi
  say "앱의 Ollama 주소를 ${now:-없음} → $want 로 고치고 백엔드를 다시 띄운다"
  # 보관장 3D 스캔 재구성 서버(infra/h100/recon)도 같은 사설 주소의 8090이다
  app "sudo sed -i '/^OLLAMA_BASE_URL=/d;/^OLLAMA_FALLBACK=/d;/^V3_RECON_URL=/d' /opt/rsai/env/backend.env && printf 'OLLAMA_BASE_URL=%s\nOLLAMA_FALLBACK=1\nV3_RECON_URL=http://%s:8090\n' '$want' '$1' | sudo tee -a /opt/rsai/env/backend.env >/dev/null && cd /opt/rsai && sudo docker compose up -d --force-recreate backend >/dev/null 2>&1"
}

case "${1:-status}" in
  up)
    now="$(state)"
    if [ "$now" != "Running" ]; then
      say "켠다(지금 $now). 몇 분 걸린다"
      az rest --method post --url "$URL/start?api-version=$API" -o none
      for _ in $(seq 1 60); do [ "$(state)" = "Running" ] && break; sleep 15; done
    fi
    [ "$(state)" = "Running" ] || { say "켜지지 않았다(지금 $(state)). 업체 측 오픈 모델 차례는 Foundry가 답한다"; exit 1; }
    ip="$(private_ip)"
    ssh_cmd
    # Ollama는 사설 주소에만 묶는다(설계 4장). 껐다 켜서 주소가 바뀌었으면 다시 묶고 다시 띄운다
    "${SSH[@]}" "IP=$ip bash -s" <<'BIND'
set -euo pipefail
conf=/etc/systemd/system/ollama.service.d/override.conf
if ! grep -q "OLLAMA_HOST=$IP:11434" "$conf" 2>/dev/null; then
  sudo sed -i "s#OLLAMA_HOST=[^\"]*#OLLAMA_HOST=$IP:11434#" "$conf"
  sudo systemctl daemon-reload && sudo systemctl restart ollama
fi
BIND
    for _ in $(seq 1 40); do "${SSH[@]}" "curl -fsS -m 3 http://$ip:11434/api/version" >/dev/null 2>&1 && break; sleep 5; done
    "${SSH[@]}" "curl -fsS -m 3 http://$ip:11434/api/version" >/dev/null || { say "Ollama가 답하지 않는다(infra/h100/setup-ollama.sh)"; exit 1; }
    # 시연 업체가 쓰는 모델을 미리 올려 둔다(첫 차례가 모델을 읽느라 늦지 않게)
    "${SSH[@]}" "curl -fsS -m 180 http://$ip:11434/api/generate -d '{\"model\":\"${WARM_MODEL:-qwen2.5:32b}\",\"prompt\":\"안녕\",\"stream\":false,\"options\":{\"num_predict\":1}}'" >/dev/null || say "모델 예열 실패(첫 차례가 조금 늦을 수 있다)"
    point_app "$ip"
    # 앱 VM에서 닿는지(피어링과 NSG) 확인한다
    app "curl -fsS -m 5 http://$ip:11434/api/version" >/dev/null && say "켜짐. 앱에서 http://$ip:11434 에 닿는다" || say "앱 VM에서 Ollama에 닿지 않는다(피어링·NSG 확인)"
    ;;
  down)
    now="$(state)"
    if [ "$now" = "Stopped" ]; then say "이미 꺼져 있다"; exit 0; fi
    say "끈다(지금 $now)"
    az rest --method post --url "$URL/stop?api-version=$API" -o none
    for _ in $(seq 1 40); do [ "$(state)" = "Stopped" ] && break; sleep 15; done
    say "지금 $(state). 업체 측 오픈 모델 차례는 Foundry가 답한다"
    ;;
  status)
    now="$(state)"
    say "상태 $now, 사설 주소 $(private_ip)"
    if [ "$now" = "Running" ]; then
      ssh_cmd
      "${SSH[@]}" "curl -fsS -m 5 http://$(private_ip):11434/api/tags | python3 -c 'import json,sys; print(\"받은 모델:\", \", \".join(m[\"name\"] for m in json.load(sys.stdin)[\"models\"]))'; nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader" || true
    fi
    say "앱이 쓰는 주소: $(app "sudo grep -E '^OLLAMA_BASE_URL=' /opt/rsai/env/backend.env | cut -d= -f2-" || echo '확인 못 함')"
    ;;
  ip)
    private_ip
    ;;
  ssh)
    shift
    ssh_cmd
    "${SSH[@]}" "$@"
    ;;
  *)
    echo "쓰는 법: $0 up|down|status|ssh [명령]" >&2
    exit 2
    ;;
esac
