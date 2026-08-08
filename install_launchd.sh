#!/bin/bash
# 安装 macOS launchd 常驻服务：开机自启，工作日 10:30 / 14:30 自动执行破线监控。
# 用法：bash install_launchd.sh
# 卸载：bash uninstall_launchd.sh（或手动 launchctl unload ~/Library/LaunchAgents/com.poxian.monitor.plist）
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.poxian.monitor.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$DIR/output"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.poxian.monitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>${DIR}/scheduler.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${DIR}/output/launchd.log</string>
  <key>StandardErrorPath</key>
  <string>${DIR}/output/launchd.err</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "已安装并启动 launchd 服务：$PLIST"
echo "调度器日志：$DIR/output/scheduler.log"
