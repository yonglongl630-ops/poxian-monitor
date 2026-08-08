#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工作日定时调度器
================
默认在每个交易日 10:30 / 14:30 自动执行一次破线监控（跳过节假日）。
需要常驻运行；也可通过 install_launchd.sh 安装为 macOS 开机自启服务。

用法：
    python3 scheduler.py          # 常驻调度
    python3 scheduler.py --once   # 立即执行一次后退出（用于测试）
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import stock_data

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
STATE_FILE = os.path.join(OUTPUT_DIR, "scheduler_state.json")
LOG_FILE = os.path.join(OUTPUT_DIR, "scheduler.log")
MONITOR_PY = os.path.join(BASE_DIR, "monitor.py")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
INTERVAL_SECONDS = 20
TRIGGER_BEFORE = 30   # 触发窗口：提前 30 秒
TRIGGER_AFTER_MIN = 5  # 触发窗口：延后 5 分钟（避免刚开机/延迟错过）
STATE_TTL_DAYS = 7


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def run_monitor():
    log("执行破线监控 ...")
    try:
        proc = subprocess.run(
            [sys.executable, MONITOR_PY],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        log("监控执行超时（>10 分钟）")
        return 1
    for line in (proc.stdout or "").splitlines()[-6:]:
        log("  " + line)
    if proc.returncode != 0:
        for line in (proc.stderr or "").splitlines()[-6:]:
            log("  ERR " + line)
        log("监控执行失败")
    else:
        log("监控执行完成")
    return proc.returncode


def handle_slots(now, config, state):
    times = config.get("check_times") or ["10:30", "14:30"]
    today = now.strftime("%Y-%m-%d")
    changed = False
    for slot in times:
        key = f"{today} {slot}"
        if state.get(key):
            continue
        try:
            hh, mm = slot.split(":")
            slot_dt = datetime(now.year, now.month, now.day, int(hh), int(mm), tzinfo=now.tzinfo)
        except (ValueError, TypeError):
            log(f"配置的监控时间格式无效：{slot}")
            continue
        if not (slot_dt - timedelta(seconds=TRIGGER_BEFORE) <= now <= slot_dt + timedelta(minutes=TRIGGER_AFTER_MIN)):
            continue
        try:
            trading = stock_data.is_trading_day_today()
        except Exception as exc:
            log(f"[{key}] 交易日判断失败（{exc}），按交易日处理")
            trading = True
        if trading:
            rc = run_monitor()
            state[key] = "ok" if rc == 0 else "error"
        else:
            log(f"[{key}] 今日为非交易日（节假日/周末），跳过")
            state[key] = "holiday"
        changed = True
    return changed


def prune_state(state):
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (
        datetime.strptime(today, "%Y-%m-%d") - timedelta(days=STATE_TTL_DAYS)
    ).strftime("%Y-%m-%d")
    return {k: v for k, v in state.items() if k[:10] >= cutoff}


def main():
    parser = argparse.ArgumentParser(description="破线监控 - 工作日定时调度器")
    parser.add_argument("--once", action="store_true", help="立即执行一次监控后退出")
    args = parser.parse_args()

    config = load_json(CONFIG_PATH, {})
    tz_name = config.get("timezone") or "Asia/Shanghai"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = None

    if args.once:
        log("手动触发一次监控")
        return run_monitor()

    state = prune_state(load_json(STATE_FILE, {}))
    log(f"调度器启动：工作日 {', '.join(config.get('check_times') or ['10:30', '14:30'])} 执行（时区 {tz_name}）")
    while True:
        now = datetime.now(tz)
        if now.weekday() < 5 and handle_slots(now, config, state):
            save_json(STATE_FILE, state)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
