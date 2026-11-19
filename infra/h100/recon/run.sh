#!/usr/bin/env bash
# 재구성 서버를 띄운다(systemd 단위 rsai-recon이 부른다). 계산 인스턴스의 사설 주소(eth0)에만 묶는다.
# 껐다 켜서 사설 주소가 바뀌어도 다시 띄울 때 새 주소로 묶이므로 따로 고칠 것이 없다.
set -euo pipefail
IP="$(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1 | head -1)"
[ -n "$IP" ] || { echo "eth0 사설 주소가 없다" >&2; exit 1; }
export HF_HOME=/opt/rsai-recon/hf HF_HUB_OFFLINE=1 RECON_JOBS=/mnt/recon/jobs
mkdir -p "$RECON_JOBS"
exec /opt/rsai-recon/env/bin/uvicorn server:app --app-dir /opt/rsai-recon/app --host "$IP" --port 8090 --workers 1 --log-level warning
