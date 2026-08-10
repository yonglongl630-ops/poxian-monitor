#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同花顺 Cookie 自动刷新（自检闭环）
==================================
由 GitHub Actions 在交易日 10:00 / 14:00（北京时间）定时运行，逻辑：
  1. 用现有 Cookie 请求自选股分组接口 —— 有效则无需刷新；
  2. 失效且配置了账号密码 -> 密码登录拿到新 Cookie -> 再次请求接口验证；
  3. 验证通过 -> 写入 config.json，并更新 GitHub Secret THS_COOKIE（供后续运行使用）；
  4. 全部失败 -> 推送飞书/微信告警，提示用户手动更新 Cookie。

退出码：0 = 有效或已刷新；2 = 刷新失败（非致命，工作流继续，网页回退到仓库内分组）。

环境变量：
  THS_COOKIE      现有 Cookie（可能过期）
  THS_USERNAME    同花顺账号（手机号/用户名）
  THS_PASSWORD    同花顺密码
  GH_PAT          GitHub PAT（用于更新 THS_COOKIE Secret）
  GITHUB_REPOSITORY  owner/repo（Actions 自动提供）
  SERVERCHAN_KEY / FEISHU_WEBHOOK / FEISHU_SECRET  失败告警推送（可选）
"""

import json
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
sys.path.insert(0, BASE_DIR)

import ths_client_sync  # noqa: E402
import ths_login  # noqa: E402


def _load_cookie():
    cookie = os.environ.get("THS_COOKIE") or ""
    if cookie:
        return cookie.strip()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return (json.load(f).get("ths_cookie") or "").strip()
    except Exception:
        return ""


def _save_local(cookie):
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg["ths_cookie"] = cookie
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _cookie_valid(cookie):
    """用 Cookie 请求分组接口，返回 (是否有效, 说明/分组数)。"""
    try:
        groups = ths_client_sync.fetch_groups(cookie)
        return True, groups
    except Exception as exc:
        return False, str(exc)


def _update_secret(cookie):
    """用 GH_PAT 更新 GitHub Secret THS_COOKIE，供后续工作流运行使用。"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pat = os.environ.get("GH_PAT", "")
    if not repo or not pat:
        return False
    env = dict(os.environ)
    env["GH_TOKEN"] = pat
    try:
        proc = subprocess.run(
            ["gh", "secret", "set", "THS_COOKIE", "--repo", repo, "--body", cookie],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _send_alert(title, message):
    """刷新失败时推送飞书/微信告警（配置了密钥才发送）。"""
    push = {}
    if os.environ.get("SERVERCHAN_KEY"):
        push["serverchan_key"] = os.environ["SERVERCHAN_KEY"]
    if os.environ.get("FEISHU_WEBHOOK"):
        push["feishu_webhook"] = os.environ["FEISHU_WEBHOOK"]
        push["feishu_secret"] = os.environ.get("FEISHU_SECRET") or ""
    if not push:
        return
    try:
        import notify  # noqa: PLC0415

        for result in notify.send_push({"push": push}, title, message):
            print(f"[ths_refresh] 告警推送：{result}")
    except Exception as exc:
        print(f"[ths_refresh] 告警推送失败：{exc}")


def main():
    cookie = _load_cookie()
    if not cookie:
        print("[ths_refresh] 未配置 THS_COOKIE，跳过自动刷新（网页使用仓库内分组）")
        return 0

    ok, detail = _cookie_valid(cookie)
    if ok:
        print(f"[ths_refresh] Cookie 有效（已获取 {len(detail)} 个分组），无需刷新")
        return 0
    print(f"[ths_refresh] Cookie 已失效：{detail}")

    username = os.environ.get("THS_USERNAME") or ""
    password = os.environ.get("THS_PASSWORD") or ""
    if not (username and password):
        print("[ths_refresh] 未配置 THS_USERNAME / THS_PASSWORD，无法自动刷新")
        _send_alert(
            "同花顺Cookie已过期",
            "云端无法自动刷新：请在仓库 Secrets 配置 THS_USERNAME 和 THS_PASSWORD，"
            "或手动更新 THS_COOKIE。当前网页将使用仓库内分组。",
        )
        return 2

    try:
        new_cookie = ths_login.login(username, password)
    except Exception as exc:
        print(f"[ths_refresh] 密码登录失败：{exc}")
        _send_alert("同花顺Cookie自动刷新失败", f"密码登录失败：{exc}\n请手动更新 THS_COOKIE。")
        return 2

    ok, detail = _cookie_valid(new_cookie)
    if not ok:
        print(f"[ths_refresh] 新 Cookie 验证失败：{detail}")
        _send_alert("同花顺Cookie自动刷新失败", f"新 Cookie 验证失败：{detail}\n请手动更新 THS_COOKIE。")
        return 2

    _save_local(new_cookie)
    updated = _update_secret(new_cookie)
    print(
        f"[ths_refresh] 已刷新并验证新 Cookie（{len(detail)} 个分组），"
        f"GitHub Secret 更新：{'成功' if updated else '失败（仅本次运行生效）'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
