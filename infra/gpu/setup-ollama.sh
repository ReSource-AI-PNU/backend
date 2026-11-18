#!/usr/bin/env bash
# GPU VM(infra/gpu-vm.bicep)에 Ollama를 깔고 업체 측 오픈 모델을 받는다. az 로그인과 SSH 키가 있는 곳(맥)에서 돌린다.
#
#   bash infra/gpu/setup-ollama.sh
#   FRONTEND_DIR=... USE_OLLAMA=gpu bash infra/vm/deploy-vm.sh   # 그 뒤 앱이 GPU VM의 Ollama를 쓰게 다시 배포
#
# - GPU 드라이버(GRID)를 먼저 깔고(nvidia-smi가 되면 건너뜀) 그 뒤에 Ollama를 깐다. 드라이버가 없을 때 Ollama 설치
#   스크립트가 다른 드라이버를 깔면 GRID 드라이버와 부딪히기 때문이다. Azure 드라이버 확장은 ubuntu-desktop까지 깔다가
#   대화형 질문에서 멈춰서 쓰지 않는다(infra/gpu-vm.bicep).
# - Ollama는 VM의 사설 IP에만 바인딩한다(인터넷에도 0번 주소에도 열지 않는다). 앱 VNet은 피어링으로 닿는다.
# - 모델은 쓰지 않아도 하루 동안 메모리에 둔다(첫 답이 느려지지 않게). 업체 대화가 여럿이면 동시에 넷까지 받는다.
#
# 환경 변수: RG, GPU_VM(기본 vm-rsai-gpu), SSH_KEY, OLLAMA_MODELS(기본 qwen2.5:7b), SSH_SOURCE(기본 지금 공용 IP)
set -euo pipefail
RG="${RG:-team_AIBIC2026_03_RG_EUS2}"
GPU_VM="${GPU_VM:-vm-rsai-gpu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/rsai_demo_vm}"
MODELS="${OLLAMA_MODELS:-qwen2.5:7b}"
say() { echo "[gpu] $*"; }

net="[0].virtualMachine.network"
PRIVATE="$(az vm list-ip-addresses -g "$RG" -n "$GPU_VM" --query "$net.privateIpAddresses[0]" -o tsv)"
PUBLIC="$(az vm list-ip-addresses -g "$RG" -n "$GPU_VM" --query "$net.publicIpAddresses[0].ipAddress" -o tsv)"
[ -n "$PRIVATE" ] && [ -n "$PUBLIC" ] || { say "GPU VM $GPU_VM의 주소를 찾지 못했다"; exit 1; }
say "GPU VM $GPU_VM 사설 $PRIVATE · 공용 $PUBLIC"

SSH_SOURCE="${SSH_SOURCE:-$(curl -fsS https://api.ipify.org)}"
current="$(az network nsg rule show -g "$RG" --nsg-name nsg-rsai-gpu -n AllowSshFromAdmin --query sourceAddressPrefix -o tsv)"
if [ "$current" != "$SSH_SOURCE/32" ]; then
  say "SSH 허용 주소를 $SSH_SOURCE로 맞춤"
  az network nsg rule update -g "$RG" --nsg-name nsg-rsai-gpu -n AllowSshFromAdmin --source-address-prefixes "$SSH_SOURCE/32" -o none
fi

SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=30 "rsai@$PUBLIC")
wait_ssh() {
  sleep 20
  for _ in $(seq 1 60); do "${SSH[@]}" -o BatchMode=yes true 2>/dev/null && return 0; sleep 10; done
  say "다시 켠 뒤 SSH가 열리지 않는다"; return 1
}

# 1) GRID 드라이버(Azure 문서의 NVadsA10 v5 Linux 수동 절차). 두 번 다시 켠다: nouveau를 끈 뒤, 드라이버를 깐 뒤
if ! "${SSH[@]}" 'nvidia-smi >/dev/null 2>&1'; then
  say "GRID 드라이버 준비(빌드 도구, nouveau 끄기)"
  "${SSH[@]}" 'bash -s' <<'PREP'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -q >/dev/null
sudo apt-get install -y -q build-essential dkms "linux-headers-$(uname -r)" >/dev/null
printf 'blacklist nouveau\nblacklist lbm-nouveau\noptions nouveau modeset=0\n' | sudo tee /etc/modprobe.d/nouveau.conf >/dev/null
sudo update-initramfs -u >/dev/null 2>&1
PREP
  "${SSH[@]}" 'sudo reboot' || true; wait_ssh
  say "GRID 드라이버 설치"
  "${SSH[@]}" 'bash -s' <<'GRID'
set -euo pipefail
cd /tmp
wget -q -O grid.run "https://go.microsoft.com/fwlink/?linkid=874272"
chmod +x grid.run
sudo ./grid.run --silent --dkms >/tmp/grid-install.log 2>&1 || { tail -20 /var/log/nvidia-installer.log; exit 1; }
sudo cp /etc/nvidia/gridd.conf.template /etc/nvidia/gridd.conf
printf 'IgnoreSP=FALSE\nEnableUI=FALSE\n' | sudo tee -a /etc/nvidia/gridd.conf >/dev/null
sudo sed -i '/FeatureType=0/d' /etc/nvidia/gridd.conf
GRID
  "${SSH[@]}" 'sudo reboot' || true; wait_ssh
fi

# 2) Ollama
"${SSH[@]}" "PRIVATE='$PRIVATE' MODELS='$MODELS' bash -s" <<'REMOTE'
set -euo pipefail
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || { echo "[remote] GPU 드라이버가 없다"; exit 1; }
if ! command -v ollama >/dev/null; then
  echo "[remote] Ollama 설치"
  curl -fsSL https://ollama.com/install.sh | sh >/dev/null
fi
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=%s:11434"\nEnvironment="OLLAMA_KEEP_ALIVE=24h"\nEnvironment="OLLAMA_NUM_PARALLEL=4"\n' "$PRIVATE" \
  | sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable ollama >/dev/null 2>&1 && sudo systemctl restart ollama
export OLLAMA_HOST="$PRIVATE:11434"
for i in $(seq 1 30); do curl -fsS "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1 && break; sleep 2; done
for m in $MODELS; do
  ollama show "$m" >/dev/null 2>&1 || { echo "[remote] $m 받는 중"; ollama pull "$m" >/dev/null 2>&1; }
done
echo "[remote] 예열"; ollama run "${MODELS%% *}" "한 문장으로 인사해 주세요." >/dev/null 2>&1
ollama ps
REMOTE
say "끝. 앱에서 쓸 주소: http://$PRIVATE:11434 (USE_OLLAMA=gpu로 배포하면 자동으로 찾는다)"
