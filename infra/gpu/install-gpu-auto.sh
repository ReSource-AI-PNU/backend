#!/usr/bin/env bash
# 맥에 GPU VM 자동 켜기·끄기를 건다: launchd가 1분마다 gpu.sh auto를 부른다.
# 꺼져 있는데 앱이 Ollama를 찾았으면(백엔드 경고) 켜고, 켜져 있는데 Ollama를 IDLE_MINUTES(기본 5분) 쓰지 않았으면 끈다.
#
#   bash infra/gpu/install-gpu-auto.sh            # 설치(다시 실행하면 최신 gpu.sh로 바꾼다)
#   bash infra/gpu/install-gpu-auto.sh remove     # 지우기
#   IDLE_MINUTES=10 bash infra/gpu/install-gpu-auto.sh
#
# - 맥이 켜져 있고 az가 팀 계정으로 로그인돼 있을 때만 돈다(구독 권한으로는 앱이 VM을 켜거나 Azure 쪽 예약 종료를 쓸 수 없다, gpu.sh 설명).
# - gpu.sh를 ~/.resource-ai/에 복사해 쓴다(저장소 위치·브랜치와 상관없이 돈다). 기록은 ~/Library/Logs/resource-ai-gpu.log
set -euo pipefail
LABEL="com.resourceai.gpu-idle"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HOME_DIR="$HOME/.resource-ai"
LOG="$HOME/Library/Logs/resource-ai-gpu.log"
IDLE_MINUTES="${IDLE_MINUTES:-5}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = remove ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"; echo "GPU 자동 켜기·끄기를 지웠다"; exit 0
fi

mkdir -p "$HOME_DIR" "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
cp "$HERE/gpu.sh" "$HOME_DIR/gpu.sh"; chmod +x "$HOME_DIR/gpu.sh"
cat >"$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$HOME_DIR/gpu.sh</string><string>auto</string><string>$IDLE_MINUTES</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLISTEOF
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "GPU 자동 켜기·끄기를 걸었다: 1분마다 확인, 앱이 Ollama를 찾으면 켜고 ${IDLE_MINUTES}분 쓰지 않으면 끈다. 기록: $LOG"
