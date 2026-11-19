#!/usr/bin/env bash
# H100 계산 인스턴스(infra/h100/h100.sh의 rsai-h100)에 Ollama를 깔고 업체 측 오픈 모델을 받는다. 맥에서 돌린다.
#
#   bash infra/h100/setup-ollama.sh
#   OLLAMA_MODELS="qwen2.5:32b qwen2.5:7b" bash infra/h100/setup-ollama.sh
#
# 2026-11-19 비교(시험셋 20사례, 회사 측 Foundry, 업체 측만 바꿈): qwen2.5:32b 20/20(생성 초당 60토큰),
# qwen2.5:7b 10/20, gpt-oss:20b 10/20, Foundry gpt-5-mini 19/20. 시연 업체(남해열분해)는 qwen2.5:32b를 쓴다.
#
# - NVIDIA 드라이버와 CUDA는 계산 인스턴스 이미지에 있다(A10 VM처럼 GRID 드라이버를 깔지 않는다).
# - Ollama는 계산 인스턴스의 사설 주소에만 바인딩한다(설계 4장, 인터넷에도 0번 주소에도 열지 않는다). 껐다 켜서 사설
#   주소가 바뀌면 h100.sh up이 다시 묶는다. 서브넷 NSG도 11434를 앱 VNet에만 연다.
# - 모델은 루트 디스크(껐다 켜도 남는다, 여유 약 60GB)에 둔다. /mnt는 임시 디스크다.
# - 모델은 쓰지 않아도 하루 동안 메모리에 둔다. 업체 대화가 여럿이면 동시에 넷까지 받는다(A10 VM과 같다).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MODELS="${OLLAMA_MODELS:-qwen2.5:32b}"
PRIVATE="$("$HERE/h100.sh" ip)"
"$HERE/h100.sh" ssh "PRIVATE='$PRIVATE' MODELS='$MODELS' bash -s" <<'REMOTE'
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
for i in $(seq 1 30); do curl -fsS "http://$PRIVATE:11434/api/version" >/dev/null 2>&1 && break; sleep 2; done
for m in $MODELS; do
  OLLAMA_HOST="$PRIVATE:11434" ollama show "$m" >/dev/null 2>&1 || { echo "[remote] $m 받는 중"; OLLAMA_HOST="$PRIVATE:11434" ollama pull "$m" >/dev/null 2>&1; }
done
OLLAMA_HOST="$PRIVATE:11434" ollama list
df -h / | tail -1
REMOTE
