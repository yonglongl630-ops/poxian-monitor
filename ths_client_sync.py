#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同花顺客户端自选股分组同步
==========================
Mac 同花顺客户端的自选股分组保存在云端（与账号同步），本地无独立分组文件。
本脚本通过同一账号的分组接口，读取指定的自选股分组（默认"景气""收息"），
写入 config.json 的 groups 字段。客户端里新增/删改分组后，重新运行本脚本即可。

用法：
    python3 ths_client_sync.py                # 默认同步 景气、收息 两个分组
    python3 ths_client_sync.py --groups 景气,收息,科技
    python3 ths_client_sync.py --print        # 只打印不写回配置
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
GROUP_QUERY_URL = "https://ugc.10jqka.com.cn/optdata/selfgroup/open/api/group/v1/query"
UA = "THS/7.0.10 CFNetwork/1333.0.4 Darwin/21.5.0"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")


def normalize_code(code):
    """统一代码格式：A股6位补零，港股 hk + 5位补零。"""
    code = str(code).strip().lower()
    prefix = ""
    for p in ("sh", "sz", "bj", "hk"):
        if code.startswith(p) and len(code) > len(p):
            prefix = p
            code = code[len(p):]
            break
    if code.isdigit():
        code = code.zfill(5 if prefix == "hk" else 6)
    if prefix:
        code = prefix + code
    return code


def fetch_groups(cookie):
    url = GROUP_QUERY_URL + "?" + urllib.parse.urlencode(
        {"from": "sjcg_gphone", "types": "0,1"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cookie": cookie})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("status_code") != 0:
        raise RuntimeError(data.get("status_msg") or f"接口异常：{str(data)[:200]}")
    result = data.get("data") or {}
    groups = {}
    for g in result.get("group_list") or []:
        name = g.get("name")
        if not name:
            continue
        content = g.get("content") or ""
        codes_part = content.split(",")[0] if "," in content else content
        codes = []
        seen = set()
        for c in codes_part.split("|"):
            c = c.strip()
            if not c:
                continue
            norm = normalize_code(c)
            if norm and norm not in seen:
                seen.add(norm)
                codes.append(norm)
        groups[name] = codes
    return groups


def main():
    parser = argparse.ArgumentParser(description="同花顺客户端自选股分组同步")
    parser.add_argument("--groups", default="景气,收息", help="要同步的分组名，逗号分隔")
    parser.add_argument("--print", action="store_true", help="只打印不写回配置")
    args = parser.parse_args()

    config = load_config()
    cookie = config.get("ths_cookie") or ""
    if not cookie:
        print("config.json 中未配置 ths_cookie（同花顺登录信息），无法同步分组。")
        return 1

    wanted = [n.strip() for n in args.groups.split(",") if n.strip()]
    try:
        all_groups = fetch_groups(cookie)
    except Exception as exc:
        print(f"同步失败：{exc}")
        return 1

    print(f"账号下共有 {len(all_groups)} 个分组：{ '、'.join(all_groups.keys()) }")
    selected = {}
    for name in wanted:
        codes = all_groups.get(name)
        if codes is None:
            print(f"⚠ 未找到分组：{name}")
            continue
        selected[name] = codes
        print(f"  {name}：{len(codes)} 只 -> {'、'.join(codes)}")

    if not selected:
        print("没有匹配到任何目标分组，未修改配置。")
        return 1
    if args.print:
        return 0

    config["groups"] = selected
    # 兼容旧字段：watchlist 保留全部分组并集
    merged = []
    seen = set()
    for codes in selected.values():
        for c in codes:
            if c not in seen:
                seen.add(c)
                merged.append(c)
    config["watchlist"] = merged
    save_config(config)
    print(f"已写入 config.json 的 groups（{len(selected)} 个分组，共 {len(merged)} 只去重股票）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
