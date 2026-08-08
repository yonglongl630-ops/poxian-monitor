#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
监控表网页服务 + 盘中实时刷新
=============================
在本机启动 HTTP 服务，手机/其他设备可通过网址访问监控表：

用法：
    python3 serve.py                     # 端口 8800，交易时段每 60 秒自动刷新数据
    python3 serve.py --port 9000         # 自定义端口
    python3 serve.py --interval 30       # 刷新间隔（秒）
    python3 serve.py --always            # 非交易时段也持续刷新

手机访问（同一 WiFi）：
    http://本机局域网IP:8800/dashboard.html
"""

import argparse
import os
import socket
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import monitor
import stock_data

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=OUTPUT_DIR, **kwargs)

    def log_message(self, fmt, *args):
        print(f"[serve {datetime.now().strftime('%H:%M:%S')}] {fmt % args}", flush=True)


def lan_ips():
    ips = []
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def live_loop(config_path, interval, always):
    lock = threading.Lock()
    while True:
        now = datetime.now()
        in_session = stock_data.market_in_session(now)
        if always or in_session:
            if lock.acquire(blocking=False):
                try:
                    monitor.run(config_path, notify_enabled=True)
                except Exception as exc:
                    print(f"[live] 刷新失败：{exc}", flush=True)
                finally:
                    lock.release()
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="破线监控网页服务")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--interval", type=int, default=60, help="实时刷新间隔（秒）")
    parser.add_argument("--always", action="store_true", help="非交易时段也持续刷新")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    parser.add_argument("--no-live", action="store_true", help="只提供网页，不做自动刷新")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if not args.no_live:
        threading.Thread(
            target=live_loop,
            args=(args.config, args.interval, args.always),
            daemon=True,
        ).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"监控网页已启动：http://127.0.0.1:{args.port}/dashboard.html")
    for ip in lan_ips():
        print(f"手机访问（同一WiFi）：http://{ip}:{args.port}/dashboard.html")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
