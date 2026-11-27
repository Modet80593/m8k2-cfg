#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
琉璃TV 源配置生成器（App「源配置（自动跟随）」读的那份 JSON）
=============================================================

做的事：
1. 请求星海主入口，只取第一跳的响应头：
   - 302/301 → 解析出 Location（比如 tv.s.990080.xyz:29639），这就是上游当前真实入口；
   - 200     → 主入口本身直出，没有跳转。
2. 候选备用池 = 跳转目标 + 历史已知入口，逐个轻量探测（只读开头 4KB，8 秒超时），
   要求 HTTP 200 且内容里出现 #EXTM3U / #EXTINF 才算“活”。
3. 输出 sources.json：main 永远是主入口（主入口连不上时退到第一个验活的地址），
   backup 只写验活的地址，验活失败的自动剔除——所以端口变了、某条死了，
   这份文件自己会跟上，App 端 6 小时内拿到新值，不用发新 APK。
4. 全挂时不覆盖旧文件（宁可保留上一次可用的结果），退出码仍为 0，避免把工作流刷红。

本地自测：
    python3 scripts/update_source_config.py
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(BASE_DIR, "sources.json")

# 与 App 端 Prefs.builtinSources() 保持一致：主入口走 Cloudflare，靠 302 跟随
MAIN_ENTRY = "https://tv.990080.xyz/interface.m3u"

# 历史已知直连入口：只用来自动验证去留，不在这里“猜”新端口
KNOWN_BACKUPS = [
    "https://tv.s.990080.xyz:29639/interface.m3u",
    "https://tv.s.990080.xyz:24846/interface.m3u",
]

MAX_BACKUPS = 6          # 与 App 端 SourceConfig.MAX_BACKUPS 对齐
UA = "liuli-tv-source-config/1.0"
PROBE_BYTES = 4096
TIMEOUT = 8

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """只看第一跳，不要自动跟随"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(url, follow):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Range": "bytes=0-%d" % (PROBE_BYTES - 1),
    })
    handlers = [urllib.request.HTTPSHandler(context=_CTX)]
    if not follow:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    return opener.open(req, timeout=TIMEOUT)


def first_hop(url):
    """返回 (状态码, 跳转地址或 None)"""
    try:
        resp = _open(url, follow=False)
        code = getattr(resp, "status", None) or resp.getcode()
        loc = resp.headers.get("Location")
        resp.close()
        return code, loc
    except urllib.error.HTTPError as e:
        code = e.code
        loc = e.headers.get("Location") if e.headers else None
        return code, loc
    except Exception as e:
        print("  ! 首跳失败 %s: %s" % (url, e))
        return 0, None


def probe(url):
    """能否拉到直播清单：返回 (ok, 耗时秒)"""
    started = time.time()
    try:
        resp = _open(url, follow=True)
        code = getattr(resp, "status", None) or resp.getcode()
        body = resp.read(PROBE_BYTES)
        resp.close()
        cost = time.time() - started
        if code != 200:
            return False, cost
        head = body.decode("utf-8", "ignore")
        return ("#EXTM3U" in head or "#EXTINF" in head), cost
    except Exception as e:
        print("  ! 探测失败 %s: %s" % (url, e))
        return False, time.time() - started


def same_file(a, b):
    """除 updated 外完全一致就不算变化，避免每 6 小时一次无意义提交"""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    for k in ("name", "main", "backup"):
        if a.get(k) != b.get(k):
            return False
    return True


def main():
    print("主入口:", MAIN_ENTRY)
    code, loc = first_hop(MAIN_ENTRY)
    print("  首跳:", code, "->", loc)

    candidates = []
    if loc:
        loc = loc.strip()
        if loc.startswith("http://"):
            loc = "https://" + loc[len("http://"):]
        if loc.startswith("https://") and loc != MAIN_ENTRY:
            candidates.append(loc)
    for u in KNOWN_BACKUPS:
        if u != MAIN_ENTRY and u not in candidates:
            candidates.append(u)

    alive = []
    for u in candidates:
        ok, cost = probe(u)
        print("  %s  %-58s %.2fs" % ("活" if ok else "死", u, cost))
        if ok:
            alive.append((cost, u))
    alive.sort(key=lambda x: x[0])

    mains_ok = code in (200, 301, 302, 303, 307, 308)
    main_url = MAIN_ENTRY
    if not mains_ok and alive:
        main_url = alive[0][1]
        print("  ! 主入口不通，main 退到第一个验活地址")

    backups = [u for _, u in alive if u != main_url][:MAX_BACKUPS]
    if not mains_ok and not backups:
        print("! 全部入口不可用，保留旧文件不覆盖")
        return 0

    cfg = {
        "name": "星海直播",
        "main": main_url,
        "backup": backups,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    old = None
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = None

    if same_file(old, cfg):
        print("配置无变化（备用 %d 条），跳过写入" % len(backups))
        return 0

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("已写入 sources.json：main=%s，backup=%d 条" % (main_url, len(backups)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
