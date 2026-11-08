#!/usr/bin/env python3
"""从 GitHub 仓库拉取当天报告（Actions 抓的那份）

玩客云自身出不去 x.com，抓取由 GitHub Actions 在 GitHub 机房完成，
跑完把报告和当天快照 commit 回仓库。这里只管下载，不做抓取。

为什么不用 download_url（raw.githubusercontent）：那台机器上实测 000，
但 api.github.com 是通的，contents 接口会把文件内容以 base64 塞在 JSON 里，
一个请求就够，反而更稳。
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "out")
RAW = os.path.join(BASE, "raw")
TZ8 = timezone(timedelta(hours=8))

REPO = os.environ.get("XWIRE_REPO", "9499758-wq/xwire-probe")
REF = os.environ.get("XWIRE_REF", "main")
TOKEN = os.environ.get("XWIRE_GH_TOKEN") or ""

API = "https://api.github.com"
TIMEOUT = 25


def _get(url):
    """GET 一个 JSON 接口。玩客云能直连 api.github.com，不需要代理。"""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    cmd = ["curl", "-s", "--compressed", "-m", str(TIMEOUT),
           "-H", "Accept: application/vnd.github+json",
           "-o", tmp, "-w", "%{http_code}"]
    if TOKEN:
        cmd += ["-H", f"Authorization: Bearer {TOKEN}"]
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT + 8)
        if (p.stdout or "").strip() != "200":
            return None
        return json.load(open(tmp))
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _blob(path):
    """取一个文件的原文。contents 接口对 >1MB 的文件不给 content，
    但报告只有二十几 KB，永远不会碰到那个限制。"""
    d = _get(f"{API}/repos/{REPO}/contents/{path}?ref={REF}")
    if not d or "content" not in d:
        return None
    try:
        return base64.b64decode(d["content"]).decode("utf-8")
    except Exception:
        return None


def fetch(today=None):
    today = today or datetime.now(TZ8).strftime("%Y-%m-%d")
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(RAW, exist_ok=True)

    # 1) 报告
    rel = f"src/out/xwire-{today}.md"
    txt = _blob(rel)
    if not txt:
        # 零点那一轮跑在 UTC 前一天，报告的日期是 CST 当天；如果拿不到，
        # 退一步试昨天，避免因为跨日刚好错开。
        y = (datetime.now(TZ8) - timedelta(days=1)).strftime("%Y-%m-%d")
        txt = _blob(f"src/out/xwire-{y}.md")
        if txt:
            rel = f"src/out/xwire-{y}.md"
            today = y
    if not txt:
        print(f"[fetch] 仓库里没有 {today} 的报告", file=sys.stderr)
        return None

    path = os.path.join(OUT, f"xwire-{today}.md")
    # 本地已有更长的版本就别覆盖：Actions 可能抓得比上次少
    if os.path.exists(path) and os.path.getsize(path) > len(txt.encode()):
        print(f"[fetch] 本地版本更大，保留 {path}", file=sys.stderr)
        return path
    open(path, "w").write(txt)

    # 2) 当天快照：push_wx 靠它判断哪些推文是截断的半句
    snap = _blob(f"src/raw/tweets-{today}.json")
    if snap:
        open(os.path.join(RAW, f"tweets-{today}.json"), "w").write(snap)

    print(f"[fetch] {rel} → {path} ({len(txt)} 字符)", file=sys.stderr)
    return path


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    p = fetch(arg)
    sys.exit(0 if p else 1)
