#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同花顺自选股同步
================
使用同花顺网页登录后的 Cookie，从 t.10jqka.com.cn 拉取账号自选股，
写入 config.json 的 watchlist 字段。

也可使用账号密码直接登录（自动获取会话 Cookie）：
    python3 ths_sync.py --username 你的账号 --password 你的密码

获取 Cookie 方法：
  1. 浏览器打开并登录 https://t.10jqka.com.cn/
  2. 按 F12 打开开发者工具 → Network（网络）→ 刷新页面
  3. 任选一个 t.10jqka.com.cn 请求 → Headers（标头）→ 复制 Request Headers 里的 Cookie 整串
  4. 粘贴到 config.json 的 "ths_cookie"，或执行：
     python3 ths_sync.py --cookie "粘贴的Cookie整串"

用法：
    python3 ths_sync.py                    # 使用 config.json 中的 ths_cookie
    python3 ths_sync.py --cookie "..."     # 临时指定 Cookie 并写入 config.json
    python3 ths_sync.py --username 账号 --password 密码   # 账号密码登录
    python3 ths_sync.py --print            # 只打印结果，不写回配置
"""

import argparse
import json
import os
import sys
import urllib.request

import ths_login

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LIST_URL = "https://t.10jqka.com.cn/newcircle/group/getSelfStockWithMarket/"
UA = (
    "Hexin_Gphone/11.28.03 (Royal Flush) hxtheme/0 "
    "innerversion/G037.09.028.1.32 followPhoneSystemTheme/0 "
    "userid/000000000 getHXAPPAccessibilityMode/0 hxNewFont/1 "
    "isVip/0 getHXAPPFontSetting/normal getHXAPPAdaptOldSetting/0 okhttp/3.14.9"
)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")


def fetch_self_stocks(cookie):
    req = urllib.request.Request(
        LIST_URL,
        headers={
            "User-Agent": UA,
            "Cookie": cookie,
            "Referer": "http://t.10jqka.com.cn/",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("errorCode") != 0:
        msg = payload.get("errorMsg") or payload.get("errorMessage") or str(payload)[:200]
        raise RuntimeError(f"同花顺接口返回异常（Cookie 可能已失效）：{msg}")
    result = payload.get("result") or []
    codes = []
    seen = set()
    for item in result:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip().lower()
        for prefix in ("sh", "sz", "bj"):
            if code.startswith(prefix):
                code = code[len(prefix):]
                break
        if code.isdigit():
            code = code.zfill(6)
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def main():
    parser = argparse.ArgumentParser(description="同花顺自选股同步")
    parser.add_argument("--cookie", default="", help="同花顺登录 Cookie（可选，缺省读 config.json）")
    parser.add_argument("--username", default="", help="同花顺账号（与 --password 配合自动登录）")
    parser.add_argument("--password", default="", help="同花顺密码（与 --username 配合自动登录）")
    parser.add_argument("--print", action="store_true", help="只打印不写回配置")
    args = parser.parse_args()

    config = load_config()
    cookie = args.cookie or config.get("ths_cookie") or ""
    login_source = "Cookie"
    if args.username and args.password:
        print("正在使用账号密码登录同花顺 ...")
        try:
            cookie = ths_login.login(args.username, args.password)
        except ths_login.THSLoginError as exc:
            print(f"账号密码登录失败：{exc}")
            return 1
        login_source = "账号密码登录"
    if not cookie:
        print("未提供任何登录凭据。请任选一种方式：")
        print("  1) python3 ths_sync.py --username 账号 --password 密码")
        print("  2) python3 ths_sync.py --cookie \"从浏览器复制的完整Cookie\"")
        print("获取方法见本文件顶部注释或 README.md。")
        return 1

    try:
        codes = fetch_self_stocks(cookie)
    except Exception as exc:
        print(f"同步失败：{exc}")
        if not (args.username and args.password):
            print("提示：Cookie 可能缺少登录会话字段（userid/sessionid，HttpOnly Cookie 常被漏掉）。")
            print("建议改用账号密码登录（--username/--password），或按 README 用『复制为 cURL』重新获取完整 Cookie。")
        return 1

    print(f"从同花顺获取自选股 {len(codes)} 只（{login_source}）：")
    print("、".join(codes))
    if args.print:
        return 0
    if not codes:
        print("未解析到任何股票代码，未修改 config.json")
        return 1
    config["watchlist"] = codes
    if cookie:
        config["ths_cookie"] = cookie
    save_config(config)
    print(f"已写入 config.json 的 watchlist（共 {len(codes)} 只）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
