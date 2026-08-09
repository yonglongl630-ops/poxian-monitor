#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手机推送模块
============
支持三种推送渠道，配置写在 config.json 的 "push" 字段：

  "push": {
    "serverchan_key": "Server酱(SCT)的SendKey",   # 推送到微信
    "serverchan_channel": "飞书群",               # 可选：sct.ftqq.com/forward 配置的通道名（转发到飞书群等）
    "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",  # 可选：飞书群机器人Webhook（直接@所有人）
    "bark_key": "Bark 设备的 Key",                # 推送到 iOS
    "email": {
      "smtp_host": "smtp.qq.com",
      "smtp_port": 465,
      "username": "你的邮箱",
      "password": "SMTP授权码",
      "to": ["接收邮箱"]
    }
  }

- Server酱：https://sct.ftqq.com 微信扫码登录后获取 SendKey
- 转发到飞书群：在 sct.ftqq.com/forward 添加"飞书群机器人"通道，把通道名填入 serverchan_channel
- 直接@所有人：飞书群添加自定义机器人，把 Webhook 地址填入 feishu_webhook（消息自动 @所有人）
- Bark：App Store 安装 Bark，打开后得到 https://api.day.app/你的Key/
- 邮箱：任意支持 SMTP 的邮箱（QQ/163 需开启 SMTP 并生成授权码）
"""

import json
import smtplib
import ssl
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

TIMEOUT = 10


def _serverchan(key, title, message, channel=None):
    url = f"https://sctapi.ftqq.com/{key}.send"
    params = {"title": title, "desp": message}
    if channel:
        params["channel"] = channel
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("code") == 0:
        return "Server酱 推送成功"
    return f"Server酱 推送失败：{payload.get('message') or payload}"


def _bark(key, title, message):
    url = f"https://api.day.app/{key}/"
    data = json.dumps({"title": title, "body": message}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("code") == 200:
        return "Bark 推送成功"
    return f"Bark 推送失败：{payload}"


def _feishu(webhook, title, message):
    """飞书群机器人 Webhook 直接推送，自动 @所有人。"""
    payload = {
        "msg_type": "text",
        "content": {"text": f"[{title}]\n{message}"},
        "at": {"at_all": True},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("code") == 0 or result.get("StatusCode") == 0:
        return "飞书群推送成功（已@所有人）"
    return f"飞书群推送失败：{result}"


def _email(cfg, title, message):
    msg = MIMEText(message, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr((str(Header("破线监控", "utf-8")), cfg["username"]))
    to = cfg.get("to") or []
    msg["To"] = ", ".join(to)
    host = cfg["smtp_host"]
    port = int(cfg.get("smtp_port", 465))
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=context) as server:
            server.login(cfg["username"], cfg["password"])
            server.sendmail(cfg["username"], to, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as server:
            server.starttls(context=context)
            server.login(cfg["username"], cfg["password"])
            server.sendmail(cfg["username"], to, msg.as_string())
    return f"邮件推送成功（{len(to)} 个收件人）"


def send_push(config, title, message):
    """按配置推送，返回各渠道结果列表（未配置的渠道自动跳过）。"""
    push = (config or {}).get("push") or {}
    results = []
    if push.get("serverchan_key"):
        try:
            results.append(
                _serverchan(
                    push["serverchan_key"],
                    title,
                    message,
                    channel=push.get("serverchan_channel") or None,
                )
            )
        except Exception as exc:
            results.append(f"Server酱 推送失败：{exc}")
    if push.get("feishu_webhook"):
        try:
            results.append(_feishu(push["feishu_webhook"], title, message))
        except Exception as exc:
            results.append(f"飞书群推送失败：{exc}")
    if push.get("bark_key"):
        try:
            results.append(_bark(push["bark_key"], title, message))
        except Exception as exc:
            results.append(f"Bark 推送失败：{exc}")
    if isinstance(push.get("email"), dict) and push["email"].get("smtp_host"):
        try:
            results.append(_email(push["email"], title, message))
        except Exception as exc:
            results.append(f"邮件推送失败：{exc}")
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="测试手机推送")
    parser.add_argument("--title", default="破线监控测试")
    parser.add_argument("--message", default="推送功能测试：如果收到本条消息，说明配置成功。")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for r in send_push(cfg, args.title, args.message):
        print(r)
