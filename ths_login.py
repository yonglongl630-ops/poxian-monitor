#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同花顺账号密码登录（纯标准库实现）
==================================
走官方登录流程获取会话 Cookie，供自选股接口使用：
  1. auth.10jqka.com.cn 获取 RSA 公钥
  2. unified_login 账号密码登录（RSA 加密）
  3. mainverify 换取 passport -> signvalid
  4. upass.10jqka.com.cn/docookie2.php 换取会话 Cookie
"""

import base64
import http.cookiejar
import secrets
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

AUTH_BASE = "https://auth.10jqka.com.cn"
UPASS_BASE = "https://upass.10jqka.com.cn"
VERIFY_PATH = "/verify2"
DOC_COOKIE_PATH = "/docookie2.php"
UA = "THS/7.0.10 CFNetwork/1333.0.4 Darwin/21.5.0"
QSID = "8003"
PRODUCT = "S01"
IMEI = "ZjI6MDY6NGE6NzI6MjQ6NTA="
SECURITIES = "%E5%90%8C%E8%8A%B1%E9%A1%BA%E8%BF%9C%E8%88%AA%E7%89%88"
TA_APP_ID = "2022021114090152"
TIMEOUT = 15


class THSLoginError(Exception):
    pass


def _der_read(data, pos=0):
    """读取一个 DER TLV。返回 (tag, value_bytes, next_pos)。"""
    tag = data[pos]
    pos += 1
    first = data[pos]
    pos += 1
    if first & 0x80:
        num = first & 0x7F
        length = int.from_bytes(data[pos : pos + num], "big")
        pos += num
    else:
        length = first
    return tag, data[pos : pos + length], pos + length


def _parse_rsa_pubkey(pem_text):
    body = pem_text
    for marker in ("-----BEGIN PUBLIC KEY-----", "-----END PUBLIC KEY-----"):
        body = body.replace(marker, "")
    b64 = "".join(body.split())
    der = base64.b64decode(b64)
    tag, spki, _ = _der_read(der)
    if tag != 0x30:
        raise THSLoginError("RSA 公钥格式异常")
    _, _alg_seq, pos = _der_read(spki)
    _tag2, bit_string, _ = _der_read(spki, pos)
    if _tag2 != 0x03 or not bit_string or bit_string[0] != 0:
        raise THSLoginError("RSA 公钥格式异常（BIT STRING）")
    _, rsa_seq, pos2 = _der_read(bit_string, 1)
    _, n_tlv, pos3 = _der_read(rsa_seq)
    _, e_tlv, _ = _der_read(rsa_seq, pos3)
    return int.from_bytes(n_tlv, "big"), int.from_bytes(e_tlv, "big")


def _rsa_encrypt(pem_text, value):
    n, e = _parse_rsa_pubkey(pem_text)
    k = (n.bit_length() + 7) // 8
    msg = value.encode("utf-8")
    ps_len = k - 3 - len(msg)
    if ps_len < 8:
        raise THSLoginError("待加密内容过长")
    while True:
        ps = secrets.token_bytes(ps_len)
        if b"\x00" not in ps:
            break
    em = b"\x00\x02" + ps + b"\x00" + msg
    m = int.from_bytes(em, "big")
    c = pow(m, e, n)
    return base64.b64encode(c.to_bytes(k, "big")).decode("ascii")


def _http_get_xml(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise THSLoginError(f"登录接口响应解析失败：{exc}") from exc
    ret = root.find("ret")
    if ret is None:
        raise THSLoginError("登录接口响应缺少 ret 节点")
    if str(ret.attrib.get("code")) != "0":
        raise THSLoginError(ret.attrib.get("msg") or "未知错误")
    return root


def login(username, password):
    """账号密码登录，返回会话 Cookie 字符串（可直接用于自选股接口）。"""
    if not username or not password:
        raise THSLoginError("需要同时提供账号和密码")

    root = _http_get_xml(
        AUTH_BASE + VERIFY_PATH, {"reqtype": "do_rsa", "type": "get_pubkey"}
    )
    item = root.find("item")
    if item is None:
        raise THSLoginError("未获取到 RSA 公钥")
    pubkey = item.attrib.get("pubkey", "")
    rsa_version = item.attrib.get("rsa_version") or "default_5"
    if not pubkey:
        raise THSLoginError("RSA 公钥为空")

    root = _http_get_xml(
        AUTH_BASE + VERIFY_PATH,
        {
            "account": _rsa_encrypt(pubkey, username),
            "passwd": _rsa_encrypt(pubkey, password),
            "msg": "1",
            "reqtype": "unified_login",
            "rsa_version": rsa_version,
            "ta_appid": TA_APP_ID,
        },
    )
    item = root.find("item")
    if item is None:
        raise THSLoginError("登录响应缺少 item 节点")
    userid = item.attrib.get("userid", "")
    sessionid = item.attrib.get("sessionid", "")
    if not (userid and sessionid):
        raise THSLoginError("登录响应缺少 userid / sessionid")
    rsa_version = item.attrib.get("rsa_version") or rsa_version

    root = _http_get_xml(
        AUTH_BASE + VERIFY_PATH,
        {
            "reqtype": "mainverify",
            "userid": userid,
            "sessionid": sessionid,
            "qsid": QSID,
            "product": PRODUCT,
            "version": "11.4.1.3",
            "imei": IMEI,
            "sdsn": "",
            "rsa_version": rsa_version,
            "nohqlist": "0",
            "securities": SECURITIES,
        },
    )
    item = root.find("item")
    if item is None:
        raise THSLoginError("mainverify 响应缺少 item 节点")
    passport = item.attrib.get("passport", "")
    signvalid = ""
    for chunk in passport.split("|"):
        if "=" in chunk:
            key, val = chunk.split("=", 1)
            if key.strip() == "signvalid":
                signvalid = val.strip()
    if not signvalid:
        raise THSLoginError("未从 passport 解析到 signvalid")

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    url = UPASS_BASE + DOC_COOKIE_PATH + "?" + urllib.parse.urlencode(
        {"userid": userid, "sessionid": sessionid, "signvalid": signvalid}
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener.open(req, timeout=TIMEOUT) as resp:
        resp.read()
    cookies = "; ".join(f"{c.name}={c.value}" for c in jar)
    if not cookies:
        raise THSLoginError("docookie2 未返回任何 Cookie")
    return cookies


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="同花顺账号密码登录，输出会话Cookie")
    parser.add_argument("username", help="同花顺账号（手机号/用户名）")
    parser.add_argument("password", help="同花顺密码")
    args = parser.parse_args()
    try:
        cookie = login(args.username, args.password)
        print(cookie)
    except THSLoginError as exc:
        print(f"登录失败：{exc}", file=sys.stderr)
        sys.exit(1)
