#!/usr/bin/env bash
# H100 계산 인스턴스(infra/h100/h100.sh의 rsai-h100)에 보관장 3D 스캔 재구성 서버(rsai-recon, 포트 8090)를 깐다. 맥에서 돌린다.
#
#   bash infra/h100/recon/setup-recon.sh          # 설치(처음 한 번 약 10분) + 코드 갱신 + 다시 띄우기
#   bash infra/h100/recon/setup-recon.sh code     # 코드만 갱신하고 다시 띄우기
#
# - 루트 디스크 /opt/rsai-recon에 둔다(껐다 켜도 남는다). /mnt는 임시 디스크라 작업 결과(/mnt/recon/jobs)만 둔다.
# - 파이썬은 conda-forge 채널만 써서 만든다(Anaconda 기본 채널은 이용 약관 동의가 필요하다).
# - 모델: VGGT-1B(facebookresearch/vggt, 가중치 facebook/VGGT-1B), MoGe-2(microsoft/MoGe, 가중치 Ruicheng/moge-2-vitl-normal).
#   가중치(약 6GB)는 /opt/rsai-recon/hf에 받고, 서버는 HF_HUB_OFFLINE=1로 그것만 읽는다.
# - 서버는 사설 주소에만 묶는다(run.sh). 서브넷 NSG(nsg-rsai-h100)의 AllowReconFromApp 규칙이 8090을 앱 VNet에만 연다.
# - GPU는 Ollama(업체 측 모델)와 함께 쓴다. 재구성 한 번에 GPU 메모리 약 12GB를 잠깐 쓰고 작업은 한 번에 하나씩 돈다.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
H100="$HERE/../h100.sh"
# h100.sh ssh는 앱 VM을 먼저 한 번 부르면서 표준 입력을 먹을 수 있다. 그래서 원격 스크립트와 파일을 base64로 명령 인자에 싣는다.
remote() { "$H100" ssh "echo $(base64 | tr -d '\n') | base64 -d | $1"; }
MODE="${1:-all}"
VGGT_REF=a288dd0f14786c93483e45524328726ab7b1b4ce
MOGE_REF=74fbce054ebed49800de42d0ad0e83495065719a

if [ "$MODE" = all ]; then
  remote "VGGT_REF=$VGGT_REF MOGE_REF=$MOGE_REF bash -s" <<'REMOTE'
set -euo pipefail
sudo mkdir -p /opt/rsai-recon && sudo chown "$USER" /opt/rsai-recon
cd /opt/rsai-recon
[ -x env/bin/python ] || /anaconda/bin/conda create -y -q -p /opt/rsai-recon/env --override-channels -c conda-forge python=3.11 pip
export PATH=/opt/rsai-recon/env/bin:$PATH HF_HOME=/opt/rsai-recon/hf
pip install -q --upgrade pip wheel
pip install -q torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cu126
pip install -q fastapi==0.165.0 "uvicorn[standard]==0.54.0" python-multipart==0.0.34 opencv-python-headless==5.1.0.95 \
  scipy==1.17.1 numpy==2.4.6 pillow==13.0.0 huggingface_hub==2.4.0 safetensors==0.8.0 einops==0.8.2 trimesh==5.2.1 \
  click matplotlib requests tqdm \
  "utils3d_moge @ git+https://github.com/EasternJournalist/utils3d-moge.git@62f09d58509485564e24d5d9f6aac9ee9ebc0c37" \
  "pipeline @ git+https://github.com/EasternJournalist/pipeline.git@1c511390d90226c00c101f34b84df26a0f8789b4"
[ -d vggt ] || git clone -q https://github.com/facebookresearch/vggt.git
git -C vggt fetch -q && git -C vggt checkout -q "$VGGT_REF"
[ -d MoGe ] || git clone -q https://github.com/microsoft/MoGe.git
git -C MoGe fetch -q && git -C MoGe checkout -q "$MOGE_REF"
pip install -q -e vggt --no-deps
pip install -q -e MoGe --no-deps
python - <<'PY'
from vggt.models.vggt import VGGT
from moge.model.v2 import MoGeModel
VGGT.from_pretrained("facebook/VGGT-1B")
MoGeModel.from_pretrained("Ruicheng/moge-2-vitl-normal")
print("모델 가중치 준비됨")
PY
REMOTE
fi

# 코드 올리기와 systemd 단위
COPYFILE_DISABLE=1 tar --no-xattrs -C "$HERE" -cf - recon_pipeline.py server.py run.sh \
  | remote "(mkdir -p /opt/rsai-recon/app && tar -xf - -C /opt/rsai-recon/app && chmod +x /opt/rsai-recon/app/run.sh)"
remote "bash -s" <<'UNIT'
set -euo pipefail
sudo tee /etc/systemd/system/rsai-recon.service >/dev/null <<EOF
[Unit]
Description=Re:Source AI 보관장 3D 스캔 재구성 서버
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
ExecStartPre=+/bin/sh -c 'mkdir -p /mnt/recon/jobs && chown -R $USER /mnt/recon'
ExecStart=/opt/rsai-recon/app/run.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable rsai-recon >/dev/null 2>&1
sudo systemctl restart rsai-recon
IP="$(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1 | head -1)"
for _ in $(seq 1 60); do
  if curl -fsS -m 3 "http://$IP:8090/health" 2>/dev/null | grep -q '"ok":true'; then
    echo "[remote] 재구성 서버 준비됨: http://$IP:8090"
    exit 0
  fi
  sleep 3
done
echo "[remote] 재구성 서버가 답하지 않는다(journalctl -u rsai-recon)"
exit 1
UNIT
