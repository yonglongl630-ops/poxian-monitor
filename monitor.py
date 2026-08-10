#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
破线监控主程序
==============
支持多个自选股分组（如"景气""收息"）独立监控：
- 每个分组独立统计破 5 日 / 10 日线数量与比例，判断 70% / 80% 阈值
- 生成监控表 output/dashboard.html：总览（4 项破线比例 100% 条 + 趋势）+ 各分组独立板块
- 数据快照 output/latest.json、历史 output/history.jsonl

用法：
    python3 monitor.py                 # 按 config.json 执行一次
    python3 monitor.py --no-notify     # 不发送系统通知
"""

import argparse
import html
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import zoneinfo

import notify
import stock_data

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
LATEST_JSON = os.path.join(OUTPUT_DIR, "latest.json")
HISTORY_JSONL = os.path.join(OUTPUT_DIR, "history.jsonl")
DASHBOARD_HTML = os.path.join(OUTPUT_DIR, "dashboard.html")
KLINE_DAYS = 16
THRESHOLD_NAMES = {
    "ma5_70": "破5日线 ≥ 70%",
    "ma5_80": "破5日线 ≥ 80%",
    "ma10_70": "破10日线 ≥ 70%",
    "ma10_80": "破10日线 ≥ 80%",
}
THRESHOLD_KEYS = list(THRESHOLD_NAMES.keys())

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_codes(watchlist):
    codes = []
    seen = set()
    for c in watchlist or []:
        c = str(c).strip().lower()
        prefix = ""
        for p in ("sh", "sz", "bj", "hk"):
            if c.startswith(p) and len(c) > len(p):
                prefix = p
                c = c[len(p):]
                break
        if c.isdigit():
            c = c.zfill(5 if prefix == "hk" else 6)
        if prefix:
            c = prefix + c
        if c and c not in seen:
            seen.add(c)
            codes.append(c)
    return codes


def load_groups(config):
    """从配置读取分组（名称 -> 代码列表）；无分组时回退为单个"全部"分组。"""
    groups = config.get("groups") or {}
    if not groups:
        codes = normalize_codes(config.get("watchlist") or [])
        return {"全部": codes} if codes else {}
    return {name: normalize_codes(codes) for name, codes in groups.items()}


def compute_ma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def analyze_stock(code, quote, kline, now=None):
    """计算单只股票的破线情况。均线基于前复权收盘价，交易时段内并入实时价。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    price = quote["price"]
    closes = [b["close"] for b in kline if b.get("close") is not None]
    if stock_data.market_in_session(now):
        if closes and kline and kline[-1]["date"] == today:
            closes[-1] = price
        else:
            closes.append(price)
    item = {
        "code": code,
        "name": quote.get("name") or code,
        "price": round(price, 3),
        "pct": round(quote["pct"], 2) if quote.get("pct") is not None else None,
        "closes": [round(c, 3) for c in closes[-10:]],
        "ma5": None,
        "below5": None,
        "dist5": None,
        "ma10": None,
        "below10": None,
        "dist10": None,
        "kline_date": kline[-1]["date"] if kline else None,
    }
    for period in (5, 10):
        ma = compute_ma(closes, period)
        if ma is None:
            continue
        item[f"ma{period}"] = round(ma, 3)
        item[f"below{period}"] = price < ma
        item[f"dist{period}"] = round((price - ma) / ma * 100, 2)
    return item


def summarize(stocks):
    total = len(stocks)
    summary = {}
    for period in (5, 10):
        valid = [s for s in stocks if s[f"ma{period}"] is not None]
        below = [s for s in valid if s[f"below{period}"]]
        pct = round(len(below) / len(valid) * 100, 2) if valid else None
        summary[f"ma{period}"] = {"valid": len(valid), "below": len(below), "pct": pct}
    return total, summary


def check_thresholds(summary, thresholds):
    flags = {}
    for period in (5, 10):
        pct = summary[f"ma{period}"]["pct"]
        for th in thresholds:
            flags[f"ma{period}_{th}"] = pct is not None and pct >= th
    return flags


def collect_hit_keys(group_data, overall_flags):
    """总体 + 各分组共同判断阈值触发，返回 (hit_keys, group_hits)。"""
    hit_set = set(k for k, v in (overall_flags or {}).items() if v)
    group_hits = {}
    for name, gd in group_data.items():
        for k, v in (gd.get("thresholds") or {}).items():
            if v:
                hit_set.add(k)
                group_hits.setdefault(name, []).append(k)
    hit_keys = [k for k in THRESHOLD_KEYS if k in hit_set]
    return hit_keys, group_hits


def format_hit_keys(hit_keys, group_hits, overall_flags):
    """把触发项格式化为可读文本，标注来源（总体/分组名）。"""
    parts = []
    for k in hit_keys:
        src = []
        if (overall_flags or {}).get(k):
            src.append("总体")
        for name, ks in (group_hits or {}).items():
            if k in ks:
                src.append(name)
        parts.append(THRESHOLD_NAMES[k] + (f"（{'/'.join(src)}）" if src else ""))
    return "、".join(parts)


def build_push_text(group_data, overall_summary, overall_flags, group_hits, max_list=8):
    """生成推送文案：触发项 + 总体数字 + 各分组数字 + 各分组破线股票名单。

    数字与监控表共用同一份 group_data / overall_summary，确保文字和页面一致；
    名单可逐只核对，防止只看比例产生误解。返回 (title, detail, group_lines)，
    group_lines 为各分组汇总行的文本（用于飞书里加粗展示；个股名单不加粗）。
    """
    m5, m10 = overall_summary["ma5"], overall_summary["ma10"]
    hit_keys, _ = collect_hit_keys(group_data, overall_flags)
    group_lines = []
    if hit_keys:
        title = "破线监控预警"
        detail = "触发：" + format_hit_keys(hit_keys, group_hits, overall_flags)
    else:
        title = "破线监控"
        detail = "未触发阈值"
    detail += (
        f"\n总体：破5 {m5['below']}/{m5['valid']} 只（{fmt(m5['pct'])}%），"
        f"破10 {m10['below']}/{m10['valid']} 只（{fmt(m10['pct'])}%）"
    )
    for name, gd in group_data.items():
        sm = gd["summary"]
        gm5, gm10 = sm["ma5"], sm["ma10"]
        line = (
            f"{name}：破5 {gm5['below']}/{gm5['valid']}（{fmt(gm5['pct'])}%），"
            f"破10 {gm10['below']}/{gm10['valid']}（{fmt(gm10['pct'])}%）"
        )
        detail += "\n" + line
        group_lines.append(line)
        stocks = gd.get("stocks") or []
        for period, key in ((5, "below5"), (10, "below10")):
            broken = [s for s in stocks if s.get(key)]
            if not broken:
                continue
            names = [f"{s['name']}({s['code']})" for s in broken]
            shown = names[:max_list]
            extra = f" 等 {len(names)} 只" if len(names) > max_list else ""
            line = f"　破{period}：{'、'.join(shown)}{extra}"
            detail += "\n" + line
    return title, detail, group_lines


def sort_stocks(stocks):
    def rank(s):
        b5, b10 = s["below5"], s["below10"]
        return (
            0 if (b5 and b10) else 1 if (b5 or b10) else 2,
            s["dist5"] if s["dist5"] is not None else 999,
        )

    return sorted(stocks, key=rank)


def mac_notify(title, message):
    """macOS 系统通知（失败静默）。"""
    msg = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{msg}" with title "{title}"'
    try:
        subprocess.run(["osascript", "-e", script], timeout=8, capture_output=True)
    except Exception:
        pass


def save_snapshot(snapshot):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    with open(HISTORY_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def load_history(limit=60):
    entries = []
    if not os.path.exists(HISTORY_JSONL):
        return entries
    with open(HISTORY_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            overall = data.get("overall") or {}
            summary = overall.get("summary") or data.get("summary") or {}
            entry = {
                "time": data.get("generated_at", ""),
                "date": data.get("date", ""),
                "slot": data.get("slot", ""),
                "hit_keys": list(data.get("hit_keys") or []),
                "push_sent": bool(data.get("push_sent")),
                "is_trading_day": bool(data.get("is_trading_day")),
                "pct5": (summary.get("ma5") or {}).get("pct"),
                "pct10": (summary.get("ma10") or {}).get("pct"),
                "groups": {},
            }
            for name, g in (data.get("groups") or {}).items():
                gs = g.get("summary") or {}
                entry["groups"][name] = {
                    "pct5": (gs.get("ma5") or {}).get("pct"),
                    "pct10": (gs.get("ma10") or {}).get("pct"),
                    "thresholds": dict(g.get("thresholds") or {}),
                }
            entries.append(entry)
    return entries[-limit:]


def compute_trigger_days(history):
    """按分组统计每个阈值项在交易日内的累计触发天数与连续触发天数。

    只统计 is_trading_day 为 True 的快照；同一交易日有多次快照时以最后一次为准。
    返回 {group: {key: {"streak": int, "cum": int}}}。
    """
    tdates = sorted({e["date"] for e in history if e.get("is_trading_day") and e.get("date")})
    result = {}
    for entry in history:
        if not entry.get("is_trading_day"):
            continue
        date = entry.get("date")
        if not date:
            continue
        for gname, g in (entry.get("groups") or {}).items():
            th = g.get("thresholds") or {}
            per = result.setdefault(gname, {})
            for key in THRESHOLD_KEYS:
                item = per.setdefault(key, {"hit_by_date": {}, "hit_dates": set()})
                item["hit_by_date"][date] = bool(th.get(key))
                if th.get(key):
                    item["hit_dates"].add(date)

    out = {}
    for gname, per in result.items():
        out[gname] = {}
        for key in THRESHOLD_KEYS:
            item = per.get(key)
            if not item:
                out[gname][key] = {"streak": 0, "cum": 0}
                continue
            cum = len(item["hit_dates"])
            streak = 0
            for date in reversed(tdates):
                if not item["hit_by_date"].get(date):
                    break
                streak += 1
            out[gname][key] = {"streak": streak, "cum": cum}
    return out


def fmt(v, nd=2):
    return "-" if v is None else f"{v:.{nd}f}"


def pct_color(pct):
    if pct is None:
        return ""
    if pct >= 70:
        return "red"
    if pct >= 50:
        return "orange"
    return "green"


def render_history_chart(history, group_names):
    """破线比例趋势：每个分组的破5/破10比例各一条线。"""
    if len(history) < 2:
        return (
            '<div class="chart-box"><h2>破线比例走势</h2>'
            '<p style="color:var(--muted);font-size:13px">暂无走势数据，下次监控后显示。</p></div>'
        )
    W, H, pad_l, pad_r, pad_t, pad_b = 820, 280, 44, 14, 18, 26
    n = len(history)
    x_step = (W - pad_l - pad_r) / max(n - 1, 1)

    def xy(i, value, ymax=100.0):
        x = pad_l + i * x_step
        y = pad_t + (H - pad_t - pad_b) * (1 - min(max(value, 0), ymax) / ymax)
        return x, y

    palette = {0: "#3b82f6", 1: "#f59e0b", 2: "#22c55e", 3: "#a855f7"}
    marks = []
    legend = []
    series = []  # (label, color, dasharray)
    gnames = group_names or []
    for idx, gname in enumerate(gnames):
        color = palette[idx % len(palette)]
        for metric, dash in (("pct5", ""), ("pct10", "6,4")):
            label = f"{gname} 破5" if metric == "pct5" else f"{gname} 破10"
            series.append((label, color, dash))
            pts = [
                xy(i, h["groups"].get(gname, {}).get(metric))
                for i, h in enumerate(history)
                if h["groups"].get(gname, {}).get(metric) is not None
            ]
            if len(pts) >= 2:
                d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
                marks.append(
                    f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.2" '
                    f'stroke-dasharray="{dash}"/>'
                )
                x, y = pts[-1]
                marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{color}"/>')

    # 没有分组趋势时回退到总体趋势
    if not marks:
        for metric, color, dash in (
            ("pct5", "#3b82f6", ""),
            ("pct10", "#f59e0b", "6,4"),
        ):
            pts = [xy(i, h.get(metric)) for i, h in enumerate(history) if h.get(metric) is not None]
            if len(pts) >= 2:
                d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
                marks.append(
                    f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.2" '
                    f'stroke-dasharray="{dash}"/>'
                )
                x, y = pts[-1]
                marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{color}"/>')
            series.append(("总体 破5" if metric == "pct5" else "总体 破10", color, dash))

    refs = ""
    for v in (70, 80):
        _, y = xy(0, v)
        refs += (
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" y2="{y:.1f}" '
            f'stroke="#334155" stroke-dasharray="4,4"/>'
            f'<text x="{W - pad_r - 2}" y="{y - 5:.1f}" fill="#64748b" font-size="10" '
            f'text-anchor="end">{v}%</text>'
        )
    yaxis = ""
    for v in (0, 50, 100):
        _, y = xy(0, v)
        yaxis += (
            f'<text x="{pad_l - 6}" y="{y + 3:.1f}" fill="#64748b" font-size="10" '
            f'text-anchor="end">{v}%</text>'
        )
    t0 = history[0].get("time", "")[:16]
    t1 = history[-1].get("time", "")[:16]
    xaxis = (
        f'<text x="{pad_l}" y="{H - 7}" fill="#64748b" font-size="10">{t0}</text>'
        f'<text x="{W - pad_r}" y="{H - 7}" fill="#64748b" font-size="10" text-anchor="end">{t1}</text>'
    )
    legend_items = []
    lx = pad_l
    for label, color, dash in series:
        legend_items.append(
            f'<text x="{lx}" y="{pad_t - 8}" fill="{color}" font-size="11">'
            f'{html.escape(label)}</text>'
        )
        lx += 30 + len(label) * 14
    svg = (
        f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;display:block">'
        + yaxis + refs + "".join(marks) + xaxis + "".join(legend_items) + "</svg>"
    )
    return f'<div class="chart-box"><h2>破线比例走势（近 {len(history)} 次监控）</h2>{svg}</div>'


def render_threshold_intro():
    """监控阈值介绍详情：与【破线比例一览】同款卡片样式，纯说明文字。"""
    return (
        '<div class="bars intro-box">'
        "<h2>监控阈值介绍详情</h2>"
        '<ul class="intro-list">'
        "<li><b>破5日线</b>：最近 5 个交易日收盘均价（前复权，交易时段并入实时价）；"
        "“破线”＝现价低于 5 日线。</li>"
        "<li><b>破10日线</b>：最近 10 个交易日收盘均价（前复权，交易时段并入实时价）；"
        "“破线”＝现价低于 10 日线。</li>"
        "<li><b>阈值 70% / 80%</b>：当总体或任一分组的破线比例达到 70% / 80% 时，"
        "触发红色预警并推送（飞书 @所有人 + 微信）。</li>"
        "<li><b>分组</b>：景气、收息两个板块分别统计、互不合并；分组触发显示在各自页签的"
        "阈值徽标与比例条中。</li>"
        "</ul></div>"
    )


def render_rows(stocks):
    def badge(v):
        return '<span class="badge y">破线</span>' if v else '<span class="badge n">正常</span>'

    rows = []
    for i, s in enumerate(stocks, 1):
        b5, b10 = s["below5"], s["below10"]
        row_cls = "both" if (b5 and b10) else ("one" if (b5 or b10) else "")
        pct = s["pct"]
        pct_cls = "up" if (pct or 0) > 0 else ("down" if (pct or 0) < 0 else "flat")
        name = html.escape(s["name"])
        rows.append(
            f"""<tr class="{row_cls}" data-code="{s['code']}">
<td>{i}</td>
<td>{s['code']}</td>
<td style="text-align:left">{name}</td>
<td class="c-price">{fmt(s['price'], 2)}</td>
<td class="c-pct {pct_cls}">{fmt(pct, 2)}%</td>
<td class="c-ma5">{fmt(s['ma5'])}</td>
<td class="c-dist5">{fmt(s['dist5'], 2)}%</td>
<td class="c-b5">{badge(b5)}</td>
<td class="c-ma10">{fmt(s['ma10'])}</td>
<td class="c-dist10">{fmt(s['dist10'], 2)}%</td>
<td class="c-b10">{badge(b10)}</td>
</tr>"""
        )
    return "\n".join(rows)


def render_bars(groups_meta, trigger_days=None):
    """总览：每个分组的破5/破10比例 100% 条。groups_meta: [(name, summary), ...]"""
    items = []
    for name, summary in groups_meta:
        for period, label in ((5, "破5日线"), (10, "破10日线")):
            sm = summary[f"ma{period}"]
            pct = sm["pct"]
            val = pct if pct is not None else 0
            cls = pct_color(pct)
            info = (trigger_days or {}).get(name, {}).get(f"ma{period}_70") or {}
            streak = info.get("streak") or 0
            days = (
                f'<span class="bar-days">≥70% 连续{streak}个交易日</span>'
                if streak else ""
            )
            items.append(
                f"""<div class="bar-item" data-group="{html.escape(name)}" data-period="{period}">
<div class="bar-label"><span>{html.escape(name)} · {label}</span>
<span class="bar-val {cls}">{fmt(pct)}%</span>
<span class="bar-num">{sm['below']}/{sm['valid']} 只</span>{days}</div>
<div class="bar-track">
<div class="bar-fill {cls}" style="width:{min(val, 100):.2f}%"></div>
<div class="bar-mark" style="left:70%"><i></i><em>70%</em></div>
<div class="bar-mark" style="left:80%"><i></i><em>80%</em></div>
</div></div>"""
            )
    return f'<div class="bars"><h2>破线比例一览（0-100%）</h2>{"".join(items)}</div>'


def render_group_panel(name, group_data, thresholds, trigger_days=None):
    summary = group_data["summary"]
    m5, m10 = summary["ma5"], summary["ma10"]
    stocks = group_data["stocks"]
    total = group_data["total"]
    c5 = pct_color(m5["pct"])
    c10 = pct_color(m10["pct"])
    cards = [
        f'<div class="card"><div class="label">自选股总数</div>'
        f'<div class="num">{total}</div>'
        f'<div class="sub">有效样本：MA5 {m5["valid"]} 只 / MA10 {m10["valid"]} 只</div></div>',
        f'<div class="card"><div class="label">破5日线（现价 &lt; MA5）</div>'
        f'<div class="num {c5}" data-num="ma5"><span class="c-below-n">{m5["below"]}</span><span class="c-valid" style="font-size:15px;color:var(--muted)"> / {m5["valid"]} 只</span></div>'
        f'<div class="sub">比例：<span class="c-pct">{fmt(m5["pct"])}%</span>　阈值 ≥70% / ≥80%</div></div>',
        f'<div class="card"><div class="label">破10日线（现价 &lt; MA10）</div>'
        f'<div class="num {c10}" data-num="ma10"><span class="c-below-n">{m10["below"]}</span><span class="c-valid" style="font-size:15px;color:var(--muted)"> / {m10["valid"]} 只</span></div>'
        f'<div class="sub">比例：<span class="c-pct">{fmt(m10["pct"])}%</span>　阈值 ≥70% / ≥80%</div></div>',
    ]

    def days_info(key):
        return (trigger_days or {}).get(name, {}).get(key) or {}

    def chip_label(key, hit):
        if not hit:
            return "未触发"
        streak = days_info(key).get("streak") or 0
        return f"已触发 · 连续{streak}个交易日" if streak else "已触发"

    chips = "".join(
        f'<span class="chip {"hit" if thresholds[k] else ""}" data-key="{k}" data-streak="{days_info(k).get("streak") or 0}">{name2}：{chip_label(k, thresholds[k])}</span>'
        for k, name2 in THRESHOLD_NAMES.items()
    )

    def days_row(key, label):
        info = days_info(key)
        streak = info.get("streak") or 0
        cum = info.get("cum") or 0
        return (
            f'<div class="trig-row"><span>{label}</span>'
            f'<span class="{"red" if streak else ""}">连续 <b>{streak}</b> 天</span>'
            f'<span class="trig-cum">累计 {cum} 天</span></div>'
        )

    cards.append(
        '<div class="card"><div class="label">破线触发天数（交易日）</div>'
        + days_row("ma5_70", "破5日线 ≥70%")
        + days_row("ma5_80", "破5日线 ≥80%")
        + days_row("ma10_70", "破10日线 ≥70%")
        + days_row("ma10_80", "破10日线 ≥80%")
        + "</div>"
    )
    rows = render_rows(stocks)
    table = (
        '<div class="table-wrap"><table><thead><tr>'
        "<th>#</th><th>代码</th><th>名称</th><th>现价</th><th>涨跌幅</th>"
        "<th>MA5</th><th>距MA5</th><th>破5日线</th>"
        "<th>MA10</th><th>距MA10</th><th>破10日线</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )
    return (
        f'<div id="tab-{html.escape(name)}" class="tab-panel" data-group="{html.escape(name)}" hidden>'
        f'<h2 class="panel-title">{html.escape(name)} 分组</h2>'
        f'<div class="cards">{"".join(cards)}</div>'
        f'<div class="chips">{chips}</div>{table}</div>'
    )


def render_dashboard(snapshot, history):
    groups = snapshot["groups"]
    group_names = snapshot.get("group_names") or list(groups.keys())
    overall = snapshot.get("overall") or {}
    o_summary = overall.get("summary") or {}
    flags = overall.get("thresholds") or {}
    total = overall.get("total") or 0
    trigger_days = compute_trigger_days(history)

    # 红色预警框默认隐藏，仅"总体"阈值触发时显示；分组触发在各自页签的徽标/比例条展示
    hit_keys = [k for k, v in flags.items() if v]
    if hit_keys:
        alert = (
            '<div class="banner warn" id="trigger-alert">⚠ 阈值触发：'
            + format_hit_keys(hit_keys, {}, flags)
            + "，请关注破线风险！</div>"
        )
    else:
        alert = (
            '<div class="banner warn" id="trigger-alert" style="display:none">'
            "⚠ 阈值触发：请关注破线风险！</div>"
        )

    tabs = '<button class="tab active" data-tab="overview">总览</button>'
    for name in group_names:
        tabs += f'<button class="tab" data-tab="{html.escape(name)}">{html.escape(name)}</button>'

    groups_meta = [(name, groups[name]["summary"]) for name in group_names if name in groups]
    overview = (
        '<div id="tab-overview" class="tab-panel">'
        f'{render_bars(groups_meta, trigger_days)}'
        f'{render_history_chart(history, group_names)}'
        f"{render_threshold_intro()}"
        "</div>"
    )
    panels = "".join(
        render_group_panel(name, groups[name], groups[name]["thresholds"], trigger_days)
        for name in group_names
        if name in groups
    )
    note = (
        "说明：MA5/MA10 基于前复权日K收盘价；交易时段内均线并入实时价（与行情软件盘中均线口径一致）。"
        "“破线”= 现价低于对应均线；上市不足 5/10 个交易日的股票不参与对应比例统计。"
        "分组数据来自同花顺客户端云端自选股（景气/收息），可在客户端修改后重新同步。"
        "页面打开后自动实时刷新行情（无需服务器），“立即刷新”随时手动更新。"
        "数据源：腾讯行情（备用：东方财富），仅供研究参考，不构成投资建议。"
    )

    live = {
        "groups": [
            {"name": n, "codes": [s["code"] for s in groups[n]["stocks"]]}
            for n in group_names
            if n in groups
        ],
        "codes": [
            {
                "code": s["code"],
                "symbol": stock_data.code_to_symbol(s["code"]),
                "name": s["name"],
                "closes": s.get("closes") or [],
                "price": s["price"],
                "pct": s.get("pct"),
                "ma5": s.get("ma5"),
                "ma10": s.get("ma10"),
                "below5": bool(s.get("below5")),
                "below10": bool(s.get("below10")),
            }
            for s in (overall.get("stocks") or [])
        ],
    }
    live_json = json.dumps(live, ensure_ascii=False).replace("</", "<\\/")

    return (
        TEMPLATE.replace("__GENERATED_AT__", snapshot["generated_at"])
        .replace("__MARKET_STATUS__", snapshot["market_status"])
        .replace("__TOTAL__", str(total))
        .replace("__BANNER__", alert)
        .replace("__TABS__", tabs)
        .replace("__OVERVIEW_PANEL__", overview)
        .replace("__GROUP_PANELS__", panels)
        .replace("__NOTE__", note)
        .replace("__LIVE_DATA__", live_json)
    )


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>破线监控</title>
<style>
:root{--bg:#0f1420;--card:#171e2d;--border:#243049;--text:#e6ecf5;--muted:#8fa0b8;
--red:#ef4444;--orange:#f59e0b;--green:#22c55e;--blue:#3b82f6}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,
"PingFang SC","Microsoft YaHei",sans-serif;padding:24px}
.top{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px;margin-bottom:16px}
h1{font-size:22px}.meta{color:var(--muted);font-size:13px}
.banner{padding:12px 16px;border-radius:10px;margin-bottom:16px;font-size:15px;font-weight:600}
.banner.warn{background:rgba(239,68,68,.14);border:1px solid var(--red);color:#ff9a9a}
.banner.ok{background:rgba(34,197,94,.12);border:1px solid var(--green);color:#7ee2a5}
.tabs{display:flex;gap:8px;margin-bottom:16px;border-bottom:1px solid var(--border);padding-bottom:10px}
.tab{padding:8px 18px;border-radius:8px;border:1px solid var(--border);background:var(--card);
color:var(--muted);font-size:14px;font-weight:600;cursor:pointer}
.tab.active{background:rgba(59,130,246,.15);border-color:var(--blue);color:var(--text)}
.panel-title{font-size:16px;margin-bottom:12px;color:var(--blue)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px}
.card .label{color:var(--muted);font-size:13px;margin-bottom:8px}
.card .num{font-size:30px;font-weight:700}
.card .sub{color:var(--muted);font-size:12px;margin-top:6px}
.num.red{color:var(--red)}.num.orange{color:var(--orange)}.num.green{color:var(--green)}
.chips{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px}
.chip{padding:8px 14px;border-radius:999px;font-size:13px;font-weight:600;
border:1px solid var(--border);background:var(--card);color:var(--muted)}
.chip.hit{border-color:var(--red);color:#ff9a9a;background:rgba(239,68,68,.12)}
.bars{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:16px}
.bars h2,.chart-box h2{font-size:15px;margin-bottom:14px}
.bar-item{margin-bottom:16px}
.bar-label{display:flex;gap:10px;align-items:baseline;font-size:13px;margin-bottom:6px}
.bar-label .bar-val{font-weight:700;font-size:15px}
.bar-label .bar-num{color:var(--muted);font-size:12px}
.bar-days{color:var(--orange);font-size:12px}
.bar-val.red{color:var(--red)}.bar-val.orange{color:var(--orange)}.bar-val.green{color:var(--green)}
.bar-track{position:relative;height:18px;background:#0b1020;border:1px solid var(--border);border-radius:9px;overflow:visible}
.bar-fill{position:absolute;top:0;left:0;bottom:0;border-radius:9px;background:var(--green);opacity:.85}
.bar-fill.orange{background:var(--orange)}
.bar-fill.red{background:var(--red)}
.bar-mark{position:absolute;top:-3px;bottom:-3px;width:0;border-left:1px dashed var(--muted)}
.bar-mark i{display:block}
.bar-mark em{position:absolute;top:-16px;left:-10px;font-style:normal;color:var(--muted);font-size:10px}
.intro-list{margin:0;padding:0;list-style:none}
.intro-list li{color:var(--muted);font-size:13px;line-height:2;padding-left:14px;position:relative}
.intro-list li::before{content:"·";position:absolute;left:2px;color:var(--blue)}
.intro-list b{color:var(--text)}
.trig-row{display:flex;justify-content:space-between;align-items:baseline;gap:8px;
font-size:13px;color:var(--muted);margin-top:7px}
.trig-row .red{color:var(--red)}
.trig-row b{font-size:17px;font-weight:700;color:var(--text)}
.trig-cum{color:var(--muted);font-size:12px}
.chart-box{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:16px}
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:10px 12px;text-align:right;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--muted);font-weight:600;background:#131a28;position:sticky;top:0}
td:first-child,th:first-child{text-align:center}
td:nth-child(2),th:nth-child(2){text-align:center}
td:nth-child(3){text-align:left}
tr.both{background:rgba(239,68,68,.11)}
tr.one{background:rgba(245,158,11,.07)}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:600}
.badge.y{background:rgba(239,68,68,.18);color:#ff9a9a}
.badge.n{background:rgba(143,160,184,.12);color:var(--muted)}
.up{color:var(--red)}.down{color:var(--green)}.flat{color:var(--muted)}
.btn{margin-left:10px;padding:6px 14px;border-radius:8px;border:1px solid var(--border);
background:var(--card);color:var(--text);font-size:13px;cursor:pointer}
.btn:hover{border-color:var(--blue)}
.foot{margin-top:16px;color:var(--muted);font-size:12px;line-height:1.8}
</style>
</head>
<body>
<div class="top">
  <div>
    <h1>破线监控</h1>
    <div class="meta">更新时间：<span id="gen-time">__GENERATED_AT__</span>　市场：__MARKET_STATUS__　自选股总数：__TOTAL__</div>
  </div>
  <button class="btn" id="refreshBtn">立即刷新</button>
</div>
__BANNER__
<div class="tabs">
__TABS__
</div>
__OVERVIEW_PANEL__
__GROUP_PANELS__
<div class="foot">__NOTE__<br>此页面打开后每 60 秒自动拉取实时行情（交易时段内有效）；定时快照由 GitHub Actions 在交易日 10:30、14:30 生成。</div>
<script>
const LIVE = __LIVE_DATA__;
const THRESH_NAMES = {"ma5_70":"破5日线 ≥ 70%","ma5_80":"破5日线 ≥ 80%","ma10_70":"破10日线 ≥ 70%","ma10_80":"破10日线 ≥ 80%"};
const KEYS = ["ma5_70","ma5_80","ma10_70","ma10_80"];
function badge(v){return v?'<span class="badge y">破线</span>':'<span class="badge n">正常</span>';}
function avg(a){return a.reduce(function(x,y){return x+y;},0)/a.length;}
function summarize(codes){
  var out={};
  [5,10].forEach(function(p){
    var valid=codes.filter(function(c){return c['ma'+p]!=null;});
    var below=valid.filter(function(c){return c['below'+p];});
    out[p]={valid:valid.length,below:below.length,pct:valid.length?below.length/valid.length*100:null};
  });
  return out;
}
function allCodes(){var m={};LIVE.codes.forEach(function(c){m[c.code]=c;});return m;}
function colorCls(p){return p>=70?'red':p>=50?'orange':'green';}
function parseQuotes(){
  var changed=false;
  LIVE.codes.forEach(function(c){
    var raw=window['v_'+c.symbol];
    if(!raw) return;
    var p=String(raw).split('~');
    if(p.length<35) return;
    var price=parseFloat(p[3]);
    if(!(price>0)) return;
    var pct=parseFloat(p[32]);
    var closes=(c.closes||[]).slice();
    if(closes.length) closes[closes.length-1]=price;
    c.price=price;
    c.pct=isFinite(pct)?pct:null;
    c.ma5=closes.length>=5?avg(closes.slice(-5)):null;
    c.ma10=closes.length>=10?avg(closes.slice(-10)):null;
    c.below5=c.ma5!=null&&price<c.ma5;
    c.below10=c.ma10!=null&&price<c.ma10;
    changed=true;
    updateRow(c);
  });
  if(changed){updateSummaries();updateBanner();}
}
function updateRow(c){
  var tr=document.querySelector('tr[data-code="'+c.code+'"]');
  if(!tr) return;
  tr.querySelector('.c-price').textContent=c.price.toFixed(2);
  var pe=tr.querySelector('.c-pct');
  pe.textContent=(c.pct==null?'-':c.pct.toFixed(2)+'%');
  pe.className='c-pct '+(c.pct>0?'up':c.pct<0?'down':'flat');
  tr.querySelector('.c-ma5').textContent=c.ma5==null?'-':c.ma5.toFixed(2);
  tr.querySelector('.c-dist5').textContent=c.ma5==null?'-':((c.price-c.ma5)/c.ma5*100).toFixed(2)+'%';
  tr.querySelector('.c-ma10').textContent=c.ma10==null?'-':c.ma10.toFixed(2);
  tr.querySelector('.c-dist10').textContent=c.ma10==null?'-':((c.price-c.ma10)/c.ma10*100).toFixed(2)+'%';
  tr.querySelector('.c-b5').innerHTML=badge(c.below5);
  tr.querySelector('.c-b10').innerHTML=badge(c.below10);
  tr.className=(c.below5&&c.below10)?'both':(c.below5||c.below10)?'one':'';
}
function updateSummaries(){
  var byCode=allCodes();
  LIVE.groups.forEach(function(g){
    var codes=g.codes.map(function(code){return byCode[code];}).filter(Boolean);
    var sm=summarize(codes);
    [5,10].forEach(function(p){
      var panel=document.querySelector('.tab-panel[data-group="'+g.name+'"]');
      if(!panel) return;
      var num=panel.querySelector('.c-below-n');
      var valid=panel.querySelector('.c-valid');
      var pctEl=panel.querySelector('.c-pct');
      var numDiv=panel.querySelector('.num[data-num="ma'+p+'"]');
      if(num) num.textContent=sm[p].below;
      if(valid) valid.textContent=' / '+sm[p].valid+' 只';
      if(pctEl) pctEl.textContent=sm[p].pct==null?'-':sm[p].pct.toFixed(2)+'%';
      if(numDiv) numDiv.className='num '+colorCls(sm[p].pct==null?0:sm[p].pct);
      KEYS.forEach(function(k){
        if(k.indexOf('ma'+p+'_')!==0) return;
        var th=parseInt(k.split('_')[1],10);
        var hit=sm[p].pct!=null&&sm[p].pct>=th;
        var chip=panel.querySelector('.chip[data-key="'+k+'"]');
        if(chip){
          chip.classList.toggle('hit',hit);
          var streak=parseInt(chip.getAttribute('data-streak')||'0',10);
          chip.textContent=THRESH_NAMES[k]+'：'+(hit?(streak>0?'已触发 · 连续'+streak+'个交易日':'已触发'):'未触发');
        }
      });
      var bar=document.querySelector('.bar-item[data-group="'+g.name+'"][data-period="'+p+'"]');
      if(bar){
        var bv=bar.querySelector('.bar-val'),bn=bar.querySelector('.bar-num'),bf=bar.querySelector('.bar-fill');
        var pct=sm[p].pct==null?0:sm[p].pct;
        var cls=colorCls(pct);
        if(bv){bv.textContent=(sm[p].pct==null?'-':sm[p].pct.toFixed(2))+'%';bv.className='bar-val '+cls;}
        if(bn) bn.textContent=sm[p].below+'/'+sm[p].valid+' 只';
        if(bf){bf.className='bar-fill '+cls;bf.style.width=Math.min(pct,100).toFixed(2)+'%';}
      }
    });
  });
}
function updateBanner(){
  var hit=[];
  var os=summarize(LIVE.codes);
  KEYS.forEach(function(k){
    var p=parseInt(k.slice(2,4),10),th=parseInt(k.slice(5),10);
    if(os[p].pct!=null&&os[p].pct>=th) hit.push(k);
  });
  var banner=document.getElementById('trigger-alert');
  if(banner){
    if(hit.length){
      banner.style.display='block';
      banner.textContent='⚠ 阈值触发：'+hit.map(function(k){return THRESH_NAMES[k];}).join('、')+'，请关注破线风险！';
    }else{
      banner.style.display='none';
    }
  }
}
function refreshQuotes(cb){
  var syms=LIVE.codes.map(function(c){return c.symbol;}).join(',');
  var s=document.createElement('script');
  s.src='https://qt.gtimg.cn/q='+syms+'&_='+Date.now();
  s.onload=function(){try{parseQuotes();}catch(e){console.error(e);}if(cb)cb();};
  s.onerror=function(){if(cb)cb();};
  document.head.appendChild(s);
}
function stamp(){
  var el=document.getElementById('gen-time');
  if(!el) return;
  var n=new Date(),pad=function(x){return x<10?'0'+x:''+x;};
  el.textContent=n.getFullYear()+'-'+pad(n.getMonth()+1)+'-'+pad(n.getDate())+' '+pad(n.getHours())+':'+pad(n.getMinutes())+':'+pad(n.getSeconds())+'（本地实时）';
}
document.getElementById("refreshBtn").addEventListener("click",function(){
  var btn=document.getElementById("refreshBtn"),old=btn.textContent;
  btn.textContent="刷新中…";
  refreshQuotes(function(){btn.textContent=old;stamp();});
});
refreshQuotes(stamp);
setInterval(function(){refreshQuotes(stamp);},60000);
document.querySelectorAll(".tab").forEach(function(btn){
  btn.addEventListener("click",function(){
    document.querySelectorAll(".tab").forEach(function(b){b.classList.remove("active")});
    document.querySelectorAll(".tab-panel").forEach(function(p){p.hidden=true});
    btn.classList.add("active");
    document.getElementById("tab-"+btn.dataset.tab).hidden=false;
  });
});
</script>
</body>
</html>
"""


def run(config_path, notify_enabled):
    config = load_config(config_path)
    tz_name = config.get("timezone") or "Asia/Shanghai"
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
        now = datetime.now(tz)
    except Exception:
        tz = None
        now = datetime.now()
    thresholds = config.get("thresholds") or [70, 80]

    groups = load_groups(config)
    if not groups:
        log("未配置任何自选股分组，请在 config.json 配置 groups（或先运行 ths_client_sync.py 同步）")
        return 1
    all_codes = []
    seen = set()
    for codes in groups.values():
        for c in codes:
            if c not in seen:
                seen.add(c)
                all_codes.append(c)
    log(f"开始监控：{len(groups)} 个分组，共 {len(all_codes)} 只去重股票")
    for name, codes in groups.items():
        log(f"  [{name}] {len(codes)} 只")

    quotes = stock_data.fetch_realtime(all_codes)
    log(f"实时行情获取完成：{len(quotes)}/{len(all_codes)} 只有效")

    klines = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(stock_data.fetch_kline, c, KLINE_DAYS): c for c in all_codes}
        for fut in as_completed(future_map):
            code = future_map[fut]
            try:
                klines[code] = fut.result()
            except Exception as exc:
                log(f"  [{code}] 日K获取失败：{exc}")
                klines[code] = []

    analyzed = {}
    skipped = []
    for code in all_codes:
        quote = quotes.get(code)
        if not quote or quote.get("price") is None:
            skipped.append(code)
            continue
        analyzed[code] = analyze_stock(code, quote, klines.get(code, []), now)

    group_data = {}
    for name, codes in groups.items():
        stocks = [analyzed[c] for c in codes if c in analyzed]
        stocks = sort_stocks(stocks)
        total, summary = summarize(stocks)
        flags = check_thresholds(summary, thresholds)
        group_data[name] = {
            "total": total,
            "stocks": stocks,
            "summary": summary,
            "thresholds": flags,
        }

    overall_stocks = sort_stocks([analyzed[c] for c in all_codes if c in analyzed])
    o_total, o_summary = summarize(overall_stocks)
    o_flags = check_thresholds(o_summary, thresholds)

    try:
        trading = stock_data.is_trading_day_today(tz_name)
    except Exception as exc:
        log(f"交易日判断失败：{exc}，按未知处理")
        trading = None
    in_session = stock_data.market_in_session(now)
    if trading is True and in_session:
        market_status = "交易中"
    elif trading is True:
        market_status = "已收盘"
    elif trading is False:
        market_status = "非交易日"
    else:
        market_status = "未知"

    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    slot = "am" if now.hour < 12 else "pm"
    hit_keys, group_hits = collect_hit_keys(group_data, o_flags)
    snapshot = {
        "generated_at": now_str,
        "date": now_str[:10],
        "time": now_str[11:],
        "slot": slot,
        "hit_keys": hit_keys,
        "is_trading_day": trading,
        "market_status": market_status,
        "group_names": list(groups.keys()),
        "groups": group_data,
        "overall": {
            "total": o_total,
            "stocks": overall_stocks,
            "summary": o_summary,
            "thresholds": o_flags,
        },
        "skipped": skipped,
    }

    push_every = config.get("push_on_every_run", False)
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    push_scheduled = event_name == "schedule"
    # 仅交易日推送；盘中（或 GitHub 定时任务）才推送，避免手动/改代码触发刷屏
    push_enabled = trading is True and (in_session or event_name == "schedule")
    # 定时任务：无论是否触发阈值都推送汇总（飞书 + 微信）；盘中触发阈值也推送
    want_push = push_enabled and (bool(hit_keys) or (push_every and push_scheduled))

    # 兜底去重：同一天同一时段（上午/下午）若已推送过相同内容，则不再重复推送
    already = False
    if want_push:
        want = set(hit_keys)
        for e in load_history():
            if e.get("date") != now_str[:10] or e.get("slot") != slot:
                continue
            if not e.get("push_sent"):
                continue
            if set(e.get("hit_keys") or []) == want:
                already = True
                break
    push_sent = want_push and not already
    snapshot["push_sent"] = push_sent

    save_snapshot(snapshot)
    history = load_history()
    with open(DASHBOARD_HTML, "w", encoding="utf-8") as f:
        f.write(render_dashboard(snapshot, history))

    log("=" * 52)
    for name, gd in group_data.items():
        sm = gd["summary"]
        m5, m10 = sm["ma5"], sm["ma10"]
        log(
            f"[{name}] 破5日线：{m5['below']}/{m5['valid']} 只（{fmt(m5['pct'])}%）　"
            f"破10日线：{m10['below']}/{m10['valid']} 只（{fmt(m10['pct'])}%）"
        )
    m5, m10 = o_summary["ma5"], o_summary["ma10"]
    log(
        f"[总体] 破5日线：{m5['below']}/{m5['valid']} 只（{fmt(m5['pct'])}%）　"
        f"破10日线：{m10['below']}/{m10['valid']} 只（{fmt(m10['pct'])}%）"
    )
    if skipped:
        log(f"以下代码未获取到行情，已跳过：{', '.join(skipped)}")
    log(f"监控表已生成：{DASHBOARD_HTML}")

    if not push_enabled and (hit_keys or push_every):
        log("非交易日或非交易时段，跳过推送（阈值状态基于最近快照）")
    if push_sent:
        title, detail, group_lines = build_push_text(group_data, o_summary, o_flags, group_hits)
        if hit_keys and notify_enabled and config.get("notify_on_hit", True):
            mac_notify("破线监控预警", detail)
            log("已发送系统通知")
        for r in notify.send_push(config, title, detail, bold_lines=group_lines):
            log(f"手机推送：{r}")
    elif want_push and already:
        log("今日该时段已推送过相同内容，跳过重复推送")
    return 0


def main():
    parser = argparse.ArgumentParser(description="破线监控主程序")
    parser.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"), help="配置文件路径")
    parser.add_argument("--no-notify", action="store_true", help="不发送系统通知")
    args = parser.parse_args()
    try:
        return run(args.config, notify_enabled=not args.no_notify)
    except Exception as exc:
        log(f"执行失败：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
