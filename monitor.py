#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
破线监控主程序
==============
读取 config.json 中的自选股，拉取实时行情与前复权日K，
计算 5 日 / 10 日均线破线（现价低于均线）数量与比例，
判断 70% / 80% 阈值是否触发，生成监控表 output/dashboard.html 及数据快照。

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
        if closes and kline[-1]["date"] == today:
            closes[-1] = price
        else:
            closes.append(price)
    item = {
        "code": code,
        "name": quote.get("name") or code,
        "price": round(price, 3),
        "pct": round(quote["pct"], 2) if quote.get("pct") is not None else None,
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


def notify(title, message):
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
            summary = data.get("summary", {})
            entries.append(
                {
                    "time": data.get("generated_at", ""),
                    "pct5": (summary.get("ma5") or {}).get("pct"),
                    "pct10": (summary.get("ma10") or {}).get("pct"),
                }
            )
    return entries[-limit:]


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


def render_history_chart(history):
    if len(history) < 2:
        return (
            '<div class="chart-box"><h2>破线比例走势</h2>'
            '<p style="color:var(--muted);font-size:13px">暂无走势数据，下次监控后显示。</p></div>'
        )
    W, H, pad_l, pad_r, pad_t, pad_b = 820, 250, 44, 14, 18, 26
    n = len(history)
    x_step = (W - pad_l - pad_r) / max(n - 1, 1)

    def xy(i, value, ymax=100.0):
        x = pad_l + i * x_step
        y = pad_t + (H - pad_t - pad_b) * (1 - min(max(value, 0), ymax) / ymax)
        return x, y

    marks = []
    for key, color in (("pct5", "#3b82f6"), ("pct10", "#f59e0b")):
        pts = [xy(i, h[key]) for i, h in enumerate(history) if h.get(key) is not None]
        if len(pts) >= 2:
            d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            marks.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.2"/>')
            x, y = pts[-1]
            marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="{color}"/>')

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
    legend = (
        f'<text x="{pad_l}" y="{pad_t - 8}" fill="#3b82f6" font-size="11">破5日线比例</text>'
        f'<text x="{pad_l + 90}" y="{pad_t - 8}" fill="#f59e0b" font-size="11">破10日线比例</text>'
    )
    svg = (
        f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;display:block">'
        + yaxis + refs + "".join(marks) + xaxis + legend + "</svg>"
    )
    return f'<div class="chart-box"><h2>破线比例走势（近 {len(history)} 次监控）</h2>{svg}</div>'


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
            f"""<tr class="{row_cls}">
<td>{i}</td>
<td>{s['code']}</td>
<td style="text-align:left">{name}</td>
<td>{fmt(s['price'], 2)}</td>
<td class="{pct_cls}">{fmt(pct, 2)}%</td>
<td>{fmt(s['ma5'])}</td>
<td>{fmt(s['dist5'], 2)}%</td>
<td>{badge(b5)}</td>
<td>{fmt(s['ma10'])}</td>
<td>{fmt(s['dist10'], 2)}%</td>
<td>{badge(b10)}</td>
</tr>"""
        )
    return "\n".join(rows)


def render_dashboard(snapshot, history):
    summary = snapshot["summary"]
    flags = snapshot["thresholds"]
    total = snapshot["total"]
    m5, m10 = summary["ma5"], summary["ma10"]

    hit_keys = [k for k, v in flags.items() if v]
    if hit_keys:
        banner = (
            '<div class="banner warn">⚠ 阈值触发：'
            + "、".join(THRESHOLD_NAMES[k] for k in hit_keys)
            + "，请关注破线风险！</div>"
        )
    else:
        banner = '<div class="banner ok">当前未触发 70% / 80% 破线阈值。</div>'

    c5 = pct_color(m5["pct"])
    c10 = pct_color(m10["pct"])
    cards = [
        f'<div class="card"><div class="label">自选股总数</div>'
        f'<div class="num">{total}</div>'
        f'<div class="sub">有效样本：MA5 {m5["valid"]} 只 / MA10 {m10["valid"]} 只</div></div>',
        f'<div class="card"><div class="label">破5日线（现价 &lt; MA5）</div>'
        f'<div class="num {c5}">{m5["below"]}<span style="font-size:15px;color:var(--muted)"> / {m5["valid"]} 只</span></div>'
        f'<div class="sub">比例：{fmt(m5["pct"])}%　阈值 ≥70% / ≥80%</div></div>',
        f'<div class="card"><div class="label">破10日线（现价 &lt; MA10）</div>'
        f'<div class="num {c10}">{m10["below"]}<span style="font-size:15px;color:var(--muted)"> / {m10["valid"]} 只</span></div>'
        f'<div class="sub">比例：{fmt(m10["pct"])}%　阈值 ≥70% / ≥80%</div></div>',
        f'<div class="card"><div class="label">市场状态</div>'
        f'<div class="num" style="font-size:22px">{snapshot["market_status"]}</div>'
        f'<div class="sub">{snapshot["generated_at"]}（{snapshot["is_trading_day"]}）</div></div>',
    ]
    chips = "".join(
        f'<span class="chip {"hit" if flags[k] else ""}">{name}：'
        f'{"已触发" if flags[k] else "未触发"}</span>'
        for k, name in THRESHOLD_NAMES.items()
    )
    rows = render_rows(snapshot["stocks"])
    chart = render_history_chart(history)
    note = (
        "说明：MA5/MA10 基于前复权日K收盘价；交易时段内均线并入实时价（与行情软件盘中均线口径一致）。"
        "“破线”= 现价低于对应均线；上市不足 5/10 个交易日的股票不参与对应比例统计。"
        "数据源：腾讯行情（备用：东方财富），仅供研究参考，不构成投资建议。"
    )

    return (
        TEMPLATE.replace("__GENERATED_AT__", snapshot["generated_at"])
        .replace("__MARKET_STATUS__", snapshot["market_status"])
        .replace("__TOTAL__", str(total))
        .replace("__BANNER__", banner)
        .replace("__CARDS__", "\n".join(cards))
        .replace("__CHIPS__", chips)
        .replace("__ROWS__", rows)
        .replace("__CHART__", chart)
        .replace("__NOTE__", note)
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
.chart-box{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:16px}
.chart-box h2{font-size:15px;margin-bottom:10px}
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
    <div class="meta">更新时间：__GENERATED_AT__　市场：__MARKET_STATUS__　自选股总数：__TOTAL__</div>
  </div>
  <button class="btn" id="refreshBtn">立即刷新</button>
</div>
__BANNER__
<div class="cards">
__CARDS__
</div>
<div class="chips">
__CHIPS__
</div>
__CHART__
<div class="table-wrap">
<table>
<thead><tr>
<th>#</th><th>代码</th><th>名称</th><th>现价</th><th>涨跌幅</th>
<th>MA5</th><th>距MA5</th><th>破5日线</th>
<th>MA10</th><th>距MA10</th><th>破10日线</th>
</tr></thead>
<tbody>
__ROWS__
</tbody>
</table>
</div>
<div class="foot">__NOTE__<br>此页面每 60 秒自动刷新；定时监控由 scheduler.py / launchd 在交易日 10:30、14:30 生成最新快照。</div>
<script>
document.getElementById("refreshBtn").addEventListener("click", function(){location.reload()});
setTimeout(function(){location.reload()}, 60000);
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
    codes = normalize_codes(config.get("watchlist") or [])
    if not codes:
        log("自选股列表为空，请在 config.json 中配置 watchlist（或先运行 ths_sync.py 同步）")
        return 1

    log(f"开始监控：{len(codes)} 只自选股")
    quotes = stock_data.fetch_realtime(codes)
    log(f"实时行情获取完成：{len(quotes)}/{len(codes)} 只有效")

    klines = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(stock_data.fetch_kline, c, KLINE_DAYS): c for c in codes}
        for fut in as_completed(future_map):
            code = future_map[fut]
            try:
                klines[code] = fut.result()
            except Exception as exc:
                log(f"  [{code}] 日K获取失败：{exc}")
                klines[code] = []

    stocks, skipped = [], []
    for code in codes:
        quote = quotes.get(code)
        if not quote or quote.get("price") is None:
            skipped.append(code)
            continue
        stocks.append(analyze_stock(code, quote, klines.get(code, []), now))

    total, summary = summarize(stocks)
    flags = check_thresholds(summary, config.get("thresholds") or [70, 80])

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
    snapshot = {
        "generated_at": now_str,
        "date": now_str[:10],
        "time": now_str[11:],
        "is_trading_day": trading,
        "market_status": market_status,
        "total": total,
        "valid": {"ma5": summary["ma5"]["valid"], "ma10": summary["ma10"]["valid"]},
        "summary": summary,
        "thresholds": flags,
        "stocks": stocks,
        "skipped": skipped,
    }
    save_snapshot(snapshot)
    history = load_history()
    with open(DASHBOARD_HTML, "w", encoding="utf-8") as f:
        f.write(render_dashboard(snapshot, history))

    m5, m10 = summary["ma5"], summary["ma10"]
    log("=" * 52)
    log(f"破5日线：{m5['below']}/{m5['valid']} 只（{fmt(m5['pct'])}%）　阈值触发："
        f"{'是' if flags.get('ma5_70') else '否'} (≥70%) / {'是' if flags.get('ma5_80') else '否'} (≥80%)")
    log(f"破10日线：{m10['below']}/{m10['valid']} 只（{fmt(m10['pct'])}%）　阈值触发："
        f"{'是' if flags.get('ma10_70') else '否'} (≥70%) / {'是' if flags.get('ma10_80') else '否'} (≥80%)")
    if skipped:
        log(f"以下代码未获取到行情，已跳过：{', '.join(skipped)}")
    log(f"监控表已生成：{DASHBOARD_HTML}")

    hit_keys = [k for k, v in flags.items() if v]
    if hit_keys:
        message = "、".join(THRESHOLD_NAMES[k] for k in hit_keys)
        detail = f"{message}（破5日线 {fmt(m5['pct'])}%，破10日线 {fmt(m10['pct'])}%）"
        if notify_enabled and config.get("notify_on_hit", True):
            notify("破线监控预警", detail)
            log("已发送系统通知")
        for r in notify.send_push(config, "破线监控预警", detail):
            log(f"手机推送：{r}")
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
