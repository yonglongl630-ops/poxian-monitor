#!/bin/bash
# 卸载 launchd 常驻服务（仅停止后台常驻，不删除项目文件）。
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.poxian.monitor.plist"
if [ -f "$PLIST" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "已卸载 launchd 服务：$PLIST"
else
  echo "未找到已安装的服务（$PLIST）"
fi
