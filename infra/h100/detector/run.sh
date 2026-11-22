#!/usr/bin/env bash
# 탐지 채점 서버를 띄운다(systemd 단위 rsai-detector가 부른다). 계산 인스턴스의 사설 주소(eth0)에만 묶는다.
# 껐다 켜서 사설 주소가 바뀌어도 다시 띄울 때 새 주소로 묶이므로 따로 고칠 것이 없다.
set -euo pipefail
IP="$(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1 | head -1)"
[ -n "$IP" ] || { echo "eth0 사설 주소가 없다" >&2; exit 1; }
cd /opt/rsai-detector/app
exec /opt/rsai-detector/env/bin/uvicorn app.ml.score_api:app --app-dir /opt/rsai-detector/app \
  --host "$IP" --port 8095 --workers 1 --log-level warning
