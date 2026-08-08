#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行情数据获取模块
================
主数据源：腾讯行情（实时行情 + 前复权日K）
备用数据源：东方财富（实时行情）
纯标准库实现，无第三方依赖。
"""

import datetime
import json
import sys
import urllib.parse
import urllib.request
import zoneinfo

TIMEOUT = 10
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
REFERER = "https://gu.qq.com/"


def _http_get(url, headers=None, timeout=TIMEOUT, decode="utf-8"):
    hdrs = {"User-Agent": UA, "Referer": REFERER}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode(decode, errors="replace")


def code_to_symbol(code):
    """股票代码 -> 腾讯行情代码（sh/sz/bj/hk 前缀）。"""
    code = str(code).strip().zfill(6)
    if code.lower().startswith("hk"):
        return "hk" + code[2:].zfill(5)
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def code_to_secid(code):
    """A股代码 -> 东方财富 secid。"""
    code = str(code).strip().zfill(6)
    if code.lower().startswith("hk"):
        return None
    if code.startswith(("6", "9", "5")):
        return "1." + code
    return "0." + code


def fetch_realtime_tencent(codes):
    """腾讯实时行情（批量）。返回 {code: quote_dict}。"""
    if not codes:
        return {}
    symbols = ",".join(code_to_symbol(c) for c in codes)
    url = "https://qt.gtimg.cn/q=" + symbols
    text = _http_get(url, decode="gbk")
    result = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        symbol = line[2 : line.index("=")].strip()
        body = line.split("=", 1)[1].strip().strip('"')
        parts = body.split("~")
        if len(parts) < 35:
            continue
        raw_code = parts[2]
        if symbol.lower().startswith("hk"):
            code = "hk" + raw_code.zfill(5)
        else:
            code = raw_code.zfill(6)
        try:
            price = float(parts[3])
        except (TypeError, ValueError):
            price = None
        if price is None or price <= 0:
            continue
        result[code] = {
            "code": code,
            "name": parts[1].replace(" ", ""),
            "price": price,
            "prev_close": _f(parts[4]),
            "open": _f(parts[5]),
            "high": _f(parts[33]),
            "low": _f(parts[34]),
            "pct": _f(parts[32]),
            "change": _f(parts[31]),
            "turnover": _f(parts[38]),
            "timestamp": parts[30] if len(parts) > 30 else "",
        }
    return result


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_realtime_eastmoney(codes):
    """东方财富实时行情（备用）。返回 {code: quote_dict}。"""
    codes = [c for c in codes if code_to_secid(c) is not None]
    if not codes:
        return {}
    secids = ",".join(code_to_secid(c) for c in codes)
    params = {
        "secids": secids,
        "fltt": "2",
        "invt": "2",
        "fields": "f2,f3,f12,f13,f14,f15,f16,f17,f18",
    }
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get?" + urllib.parse.urlencode(params)
    data = json.loads(_http_get(url))
    diff = (data.get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    result = {}
    for item in diff:
        code = str(item.get("f12") or "")
        price = item.get("f2")
        if not code or price in (None, "-", ""):
            continue
        result[code] = {
            "code": code,
            "name": (item.get("f14") or "").replace(" ", ""),
            "price": float(price),
            "prev_close": _f(item.get("f18")),
            "open": _f(item.get("f17")),
            "high": _f(item.get("f15")),
            "low": _f(item.get("f16")),
            "pct": _f(item.get("f3")),
            "change": None,
            "turnover": None,
            "timestamp": "",
        }
    return result


def fetch_realtime(codes):
    """批量获取实时行情：腾讯优先，缺失代码用东方财富补齐。"""
    result = {}
    try:
        result.update(fetch_realtime_tencent(codes))
    except Exception as exc:
        print(f"[stock_data] 腾讯行情获取失败，改用东方财富: {exc}", file=sys.stderr)
    missing = [c for c in codes if c not in result and code_to_secid(c) is not None]
    if missing:
        try:
            result.update(fetch_realtime_eastmoney(missing))
        except Exception as exc:
            print(f"[stock_data] 东方财富行情获取失败: {exc}", file=sys.stderr)
    return result


def _fetch_kline_symbol(symbol, days=16):
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
        + urllib.parse.quote(f"{symbol},day,,,{days},qfq")
    )
    data = json.loads(_http_get(url))
    node = (data.get("data") or {}).get(symbol, {})
    bars = node.get("qfqday") or node.get("day") or []
    result = []
    for b in bars:
        result.append(
            {
                "date": str(b[0]),
                "open": _f(b[1]),
                "close": _f(b[2]),
                "high": _f(b[3]),
                "low": _f(b[4]),
                "volume": _f(b[5]) if len(b) > 5 else None,
            }
        )
    return result


def fetch_kline(code, days=16):
    """个股前复权日K（腾讯）。返回 [{date, open, close, high, low, volume}, ...]（升序）。"""
    return _fetch_kline_symbol(code_to_symbol(code), days)


def fetch_index_kline(days=3):
    """上证指数日K，用于交易日判断。"""
    return _fetch_kline_symbol("sh000001", days)


def _now(tz_name="Asia/Shanghai"):
    try:
        return datetime.datetime.now(zoneinfo.ZoneInfo(tz_name))
    except Exception:
        return datetime.datetime.now()


def is_trading_day_today(tz_name="Asia/Shanghai"):
    """今天（北京时间）是否有当日K线 = 是否交易日（盘中/盘后均适用）。"""
    bars = fetch_index_kline(days=3)
    if not bars:
        return False
    today = _now(tz_name).strftime("%Y-%m-%d")
    return bars[-1]["date"] == today


def market_in_session(now=None):
    """当前是否处于 A 股连续竞价时段（周一至周五 09:30-11:30 / 13:00-15:00）。"""
    now = now or _now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)
