#!/usr/bin/env bash
# H100 계산 인스턴스(infra/h100/h100.sh의 rsai-h100)에 사진·영상 탐지 채점 서버(rsai-detector, 포트 8095)를 깐다. 맥에서 돌린다.
#
#   bash infra/h100/detector/setup-detector.sh          # 설치(처음 한 번 약 10분) + 코드·가중치 갱신 + 다시 띄우기
#   bash infra/h100/detector/setup-detector.sh code     # 코드만 갱신하고 다시 띄우기
#
# - 2026-11-22부터 탐지는 H100에서만 돈다(앱 VM의 CPU detector 컨테이너는 없앴다). 앱 VM CPU로는 사진 한 장 약 1.1초,
#   30초 영상(4fps, 120장면)은 약 2분이 걸리고 그동안 백엔드 API와 vCPU 4개를 나눠 썼다. H100을 꺼 두면 사진·영상
#   분석만 쉰다(백엔드가 연결 확인으로 DetectorOffline을 알아채고 화면이 "분석 서버가 꺼져 있습니다"라고 알린다).
# - 서버는 백엔드의 app/ml/score_api.py 그대로다. 이 저장소의 커밋된 app/와 pyproject.toml을 /opt/rsai-detector/app에
#   올리고 ml extra를 CUDA torch 위에 설치한다.
# - 가중치(약 160MB)는 Key Vault의 model-blob-url에서 /opt/rsai-detector/models로 받는다(scripts/fetch_models.py).
#   도메인 분류기가 쓰는 torchvision resnet18 가중치도 설치 때 /opt/rsai-detector/torch에 받아 둔다(켤 때마다 받지 않게).
# - 키: Key Vault detector-api-key를 /opt/rsai-detector/detector.env(600)에 DETECTOR_API_KEY로 둔다. 앱의
#   V3_DETECTOR_API_KEY와 같은 값이다. 비밀 값과 코드는 ssh 표준 입력으로 보낸다(명령 인자에 싣지 않는다).
# - 서버는 사설 주소에만 묶는다(run.sh). 서브넷 NSG(nsg-rsai-h100)의 AllowDetectorFromApp 규칙이 8095를 앱 VNet에만
#   연다(없으면 이 스크립트가 만든다).
# - GPU는 Ollama(업체 측 모델), 재구성 서버와 함께 쓴다. 탐지 모델 둘(RT-DETRv2 r18)은 GPU 메모리 1~2GB를 쓴다.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
H100="$HERE/../h100.sh"
RG="${RG:-team_AIBIC2026_03_RG_EUS2}"
# 원격 스크립트(작은 것)는 base64로 명령 인자에 싣는다(here-doc을 그대로 쓰려고). 파일과 비밀 값은 send로 표준 입력에 보낸다.
remote() { "$H100" ssh "echo $(base64 | tr -d '\n') | base64 -d | $1"; }
send() { "$H100" ssh "$1"; }
say() { echo "[detector $(date '+%m-%d %H:%M')] $*"; }
MODE="${1:-all}"

# NSG: 앱 VNet에서만 8095
if ! az network nsg rule show -g "$RG" --nsg-name nsg-rsai-h100 -n AllowDetectorFromApp -o none 2>/dev/null; then
  say "NSG nsg-rsai-h100에 AllowDetectorFromApp(8095, 앱 VNet만)을 만든다"
  az network nsg rule create -g "$RG" --nsg-name nsg-rsai-h100 -n AllowDetectorFromApp --priority 135 \
    --direction Inbound --access Allow --protocol Tcp --source-address-prefixes 10.70.0.0/16 \
    --destination-port-ranges 8095 -o none
fi

if [ "$MODE" = all ]; then
  say "파이썬 환경과 CUDA torch를 준비한다"
  remote "bash -s" <<'REMOTE'
set -euo pipefail
sudo mkdir -p /opt/rsai-detector && sudo chown "$USER" /opt/rsai-detector
cd /opt/rsai-detector
[ -x env/bin/python ] || /anaconda/bin/conda create -y -q -p /opt/rsai-detector/env --override-channels -c conda-forge python=3.12 pip
export PATH=/opt/rsai-detector/env/bin:$PATH
pip install -q --upgrade pip wheel
pip install -q torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cu126
REMOTE
fi

# 코드 올리기: 백엔드 app/·pyproject.toml·scripts/fetch_models.py(커밋된 것 그대로)
say "코드를 올린다($(git -C "$ROOT" rev-parse --short HEAD))"
git -C "$ROOT" archive --format=tar HEAD app pyproject.toml scripts/fetch_models.py \
  | send "(rm -rf /opt/rsai-detector/app.new && mkdir -p /opt/rsai-detector/app.new && tar -xf - -C /opt/rsai-detector/app.new && rm -rf /opt/rsai-detector/app && mv /opt/rsai-detector/app.new /opt/rsai-detector/app)"
COPYFILE_DISABLE=1 tar --no-xattrs -C "$HERE" -cf - run.sh \
  | send "(tar -xf - -C /opt/rsai-detector && chmod +x /opt/rsai-detector/run.sh)"

if [ "$MODE" = all ]; then
  KV="$(az keyvault list -g "$RG" --query "[?tags.project=='resource-ai'] | [0].name" -o tsv)"
  [ -n "$KV" ] || { say "Key Vault를 찾지 못했다"; exit 1; }
  say "키와 가중치 주소를 올린다(Key Vault $KV)"
  # 값은 작은따옴표로 감싼다(SAS 주소의 &를 셸이 명령 구분으로 읽지 않게. systemd EnvironmentFile도 읽는다)
  {
    printf "DETECTOR_API_KEY='%s'\n" "$(az keyvault secret show --vault-name "$KV" -n detector-api-key --query value -o tsv)"
    printf "MODEL_BLOB_URL='%s'\n" "$(az keyvault secret show --vault-name "$KV" -n model-blob-url --query value -o tsv)"
  } | send "(umask 077 && cat > /opt/rsai-detector/secrets.env)"
  say "ml extra 설치, 가중치 받기, 첫 추론"
  remote "bash -s" <<'REMOTE'
set -euo pipefail
cd /opt/rsai-detector
export PATH=/opt/rsai-detector/env/bin:$PATH TORCH_HOME=/opt/rsai-detector/torch
pip install -q "./app[ml]"
set -a; . ./secrets.env; set +a
[ -n "${MODEL_BLOB_URL:-}" ] || { echo "MODEL_BLOB_URL이 비었다(Key Vault model-blob-url)" >&2; exit 1; }
MODELS_DIR=/opt/rsai-detector/models python app/scripts/fetch_models.py
# 서버 환경: 키와 모델 위치. 가중치 받는 주소는 서버에 두지 않는다
umask 077
{
  grep '^DETECTOR_API_KEY=' secrets.env
  echo CLASSIFIER_BACKEND=trained
  echo MATERIAL_MODEL_DIR=/opt/rsai-detector/models/material
  echo CONDITION_MODEL_DIR=/opt/rsai-detector/models/condition
  echo DOMAIN_CLF_DIR=/opt/rsai-detector/models/domain_clf
  echo TORCH_HOME=/opt/rsai-detector/torch
} > detector.env
rm -f secrets.env
unset MODEL_BLOB_URL
# 첫 추론으로 resnet18 가중치를 받아 두고 GPU로 도는지 본다
set -a; . ./detector.env; set +a
cd /opt/rsai-detector/app
python - <<'PY'
import time

import cv2
import numpy as np
import torch

from app.ml.detector import get_detector

d = get_detector()
cv2.imwrite("/tmp/rsai-detector-warm.jpg", (np.random.rand(720, 1280, 3) * 255).astype("uint8"))
for _ in range(3):
    t = time.time()
    r = d.classify(["/tmp/rsai-detector-warm.jpg"])
print("cuda", torch.cuda.is_available(), "사진 한 장", round(time.time() - t, 3), "초", r.get("backend"))
PY
REMOTE
fi

say "systemd 단위 rsai-detector를 다시 띄운다"
remote "bash -s" <<'UNIT'
set -euo pipefail
sudo tee /etc/systemd/system/rsai-detector.service >/dev/null <<EOF
[Unit]
Description=Re:Source AI 사진·영상 탐지 채점 서버
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=/opt/rsai-detector/app
EnvironmentFile=/opt/rsai-detector/detector.env
ExecStart=/opt/rsai-detector/run.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable rsai-detector >/dev/null 2>&1
sudo systemctl restart rsai-detector
IP="$(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1 | head -1)"
for _ in $(seq 1 60); do
  if curl -fsS -m 3 "http://$IP:8095/healthz" 2>/dev/null | grep -q '"ok"'; then
    echo "[remote] 탐지 서버 준비됨: http://$IP:8095"
    exit 0
  fi
  sleep 3
done
echo "[remote] 탐지 서버가 답하지 않는다(journalctl -u rsai-detector)"
exit 1
UNIT
