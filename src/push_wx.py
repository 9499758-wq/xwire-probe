#!/usr/bin/env python3
"""把当天 X 快讯报告推到微信（iLink bot 通道）

为什么单独写一个：cron 里的 crawl.py 只负责落盘，推送是另一步。
这里直接调 iLink 的 ilink/bot/sendmessage，和 dsh-im 里微信通道用的是同一个接口，
只是不经过会话层，所以定时任务也能主动发出去。

用法：
  python3 push_wx.py                # 推当天报告
  python3 push_wx.py out/xxx.md     # 推指定报告
  python3 push_wx.py --dry          # 只打印要发什么，不发
"""
import base64
import json
import os
import re
import secrets
import subprocess
import sys
import time
import hashlib
import uuid

from translate import translate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CRED = os.path.expanduser("~/.dsh/.credentials.yaml")
OUT = os.path.join(BASE_DIR, "out")
RAW = os.path.join(BASE_DIR, "raw")
PENDING = os.path.join(OUT, "pending-wx.json")   # 没发成功的段落，下次补

API = "https://ilinkai.weixin.qq.com/"
CHANNEL_VERSION = "2.4.6"
APP_ID = "bot"
APP_VER = str((2 << 16) | (4 << 8) | 6)
BOT_AGENT = "DeepSeekHarness/1.1.0"

GH_RE = r'- (?:(?:\*\*)?([\w./\-]+)(?:\*\*)?)\s*(★\d+)?\s*—\s*(.+?)(?:\s*·\s*(https://github\.com/[^\s]+))?$'

MAX_CHARS = 1700        # 微信单条上限 1800，留余量
BODY_CHARS = 700        # 正文不截断：内容本身才是要看的东西，只有极长推文才收尾
SEND_GAP = 4.0          # 两条之间的间隔。iLink 主动推送配额很小，1.2s 会撞墙
MAX_PER_RUN = 3         # 每轮最多主动发几条，其余留队列等下一轮
# iLink 一张 context_token 实测只放 10 条（4+4+2 之后第 11 条起 ret:-2 prepare failed），
# 一天三轮 × 3 条 = 9 条，刚好在配额内，还留一条余量

# 报告里这三类人不需要逐条搬：媒体号条数多、单条信息量低
# iLink 主动消息有配额（实测一轮十几条后返回 ret:-2 prepare failed），
# 所以刻意压成三段：大模型、科技、白嫖各一段，每段贴近单条上限。
DIGEST_CAP = {"llm": 10, "tech": 10, "free": 3}
ROW_CAP = 5       # 免费额度表只推前 5 家，完整表在报告里
REPO_CAP = 5      # GitHub 资源同理


def load_token():
    """取微信 bot token。

    玩客云上没有 DSH，凭证靠同目录的 secrets.json 带过去；
    本机则回落到 DSH 的 credentials.yaml。
    """
    secret = os.path.join(BASE_DIR, "secrets.json")
    if os.path.exists(secret):
        try:
            v = json.load(open(secret)).get("WX_TOKEN")
            if v:
                return v
        except Exception:
            pass
    try:
        txt = open(CRED).read()
    except OSError:
        return None
    m = re.search(r"^\s*DSH_WEIXIN_BOT_TOKEN_[0-9A-F]+:\s*(.+)$", txt, re.M)
    if not m:
        return None
    return m.group(1).strip().strip("\"'")


def load_recipient():
    local = os.path.join(BASE_DIR, "recipient.txt")
    if os.path.exists(local):
        v = open(local).read().strip()
        if v:
            return v
    cfg = json.load(open(os.path.expanduser("~/.dsh/integrations/dsh-weixin/config.json")))
    return cfg["accounts"][0]["ownerUserId"]


def send_text(token, to_user, text):
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user,
            "client_id": f"dsh-weixin-{uuid.uuid4()}",
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        },
        "base_info": {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT},
    }
    uin = base64.b64encode(str(secrets.randbits(32)).encode()).decode()
    cmd = ["curl", "-s", "-m", "20", "-X", "POST", API + "ilink/bot/sendmessage",
           "-d", json.dumps(body, ensure_ascii=False),
           "-H", f"iLink-App-Id: {APP_ID}",
           "-H", f"iLink-App-ClientVersion: {APP_VER}",
           "-H", "content-type: application/json",
           "-H", "AuthorizationType: ilink_bot_token",
           "-H", f"X-WECHAT-UIN: {uin}",
           "-H", f"Authorization: Bearer {token}",
           "-w", "\n%{http_code}"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    parts = p.stdout.strip().rsplit("\n", 1)
    code = parts[-1] if parts else "000"
    body_out = parts[0] if len(parts) > 1 else ""
    ok = code == "200" and '"message_id"' in body_out
    return ok, code, body_out[:200]


def split(text, max_chars=MAX_CHARS):
    """按行切，不把一条推文拦腰截断。"""
    if len(text) <= max_chars:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > max_chars and cur:
            chunks.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks


def parse_report(path):
    """把 markdown 报告拆成三段：大模型 / 科技 / 白嫖。"""
    raw = open(path).read()
    title = raw.split("\n", 1)[0].lstrip("# ").strip()

    meta = ""
    for ln in raw.splitlines()[1:8]:
        if ln.startswith("> "):
            meta = ln[2:].strip()
            break

    secs = {}
    cur = None
    for ln in raw.splitlines():
        m = re.match(r"^## (🧠|🛠|🆓)\s*(.*?)（(\d+) 条）", ln)
        if m:
            cur = m.group(1)
            secs[cur] = {"name": m.group(2).strip(), "count": int(m.group(3)), "lines": []}
        elif ln.startswith("## "):
            cur = None
        elif ln.startswith("### "):
            if cur:
                secs[cur]["lines"].append("\n" + ln)
        elif cur:
            secs[cur]["lines"].append(ln)
    return title, meta, secs


def parse_items(sec):
    """把一节的 markdown 解成结构化条目，方便统一翻译。"""
    items, cur = [], None
    for ln in sec["lines"]:
        if ln.startswith("### "):
            items.append({"kind": "sub", "title": re.sub(r"（.*?）", "", ln[4:]).strip()})
            cur = None
        elif re.match(r"^- \*\*@", ln):
            m = re.match(r"^- \*\*@(\S+)\*\*（(.+?)）· (.+?) · (.+)$", ln)
            if m:
                cur = {"kind": "tweet", "handle": m.group(1),
                       "when": m.group(3), "stats": m.group(4), "text": []}
                items.append(cur)
        elif ln.startswith("> → https://x.com"):
            if cur:
                cur["url"] = ln[4:].strip()
            cur = None
        elif ln.startswith("> "):
            t = ln[2:].strip()
            if t and cur:
                cur["text"].append(t)
        elif ln.startswith("| "):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if cells[0].startswith("---") or cells[0] in ("提供方", "模型"):
                continue
            items.append({"kind": "row", "cells": cells})
            cur = None
        elif ln.startswith("- **") or ln.startswith("- ["):
            items.append({"kind": "line", "raw": ln})
            cur = None
    return items


def clip(text, limit):
    """按词边界截断，避免把英文单词切一半。"""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit * 0.6 else cut).rstrip(" ,;:-") + "…"


def clean_en(body):
    """去掉 X 自带的半句尾巴和 HTML 实体。"""
    body = body.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    body = re.sub(r"\s+", " ", body).strip()
    body = re.sub(r"(?:for|and|with|to|the|of|in|on|that|which|is|are|was)$", "", body).strip()
    return body


def is_zh(text):
    """粗略判断这段是否已经是中文。

    报告正文现在在 crawl.py 里就译好了，推送到这里若再送一遍翻译，
    既是白花 API，也可能被模型顺手改写。汉字占比超过一成才当中文看。
    """
    if not text:
        return False
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    letters = len(re.findall(r"[A-Za-z]", text))
    return han >= 6 and han >= letters * 0.3


def localize(items):
    """把条目里的英文正文换成中文，就地填进 item['zh']。已是中文的跳过。"""
    texts, ids = [], []
    for it in items:
        if it["kind"] == "tweet" and it["text"]:
            raw = clean_en(" ".join(it["text"]))
            if raw and not is_zh(raw):
                texts.append(raw)
                ids.append(it.get("url", raw)[-40:])
        elif it["kind"] == "line":
            m = re.match(GH_RE, it["raw"])
            if m and re.search(r"[A-Za-z]{4}", m.group(3)):
                texts.append(m.group(3).strip()[:300])
                ids.append("repo:" + m.group(1))
        elif it["kind"] == "row" and len(it["cells"]) > 1:
            # 表格第二格是英文免费条件说明
            cell = re.sub(r"\s{2,}", " ", re.sub(r"`[^`]*`", "", it["cells"][1])).strip()
            if cell and re.search(r"[A-Za-z]{4}", cell):
                texts.append(cell[:300])
                ids.append("row:" + hashlib.md5(cell.encode()).hexdigest()[:12])

    if not texts:
        return items
    zh = translate(texts, ids)
    j = 0
    for it in items:
        if (it["kind"] == "tweet" and it["text"]
                and clean_en(" ".join(it["text"]))
                and not is_zh(clean_en(" ".join(it["text"])))):
            it["zh"] = zh[j]
            j += 1
        elif it["kind"] == "line":
            m = re.match(GH_RE, it["raw"])
            if m and re.search(r"[A-Za-z]{4}", m.group(3)):
                it["zh"] = zh[j]
                j += 1
        elif it["kind"] == "row" and len(it["cells"]) > 1:
            cell = re.sub(r"\s{2,}", " ", re.sub(r"`[^`]*`", "", it["cells"][1])).strip()
            if cell and re.search(r"[A-Za-z]{4}", cell):
                it["zh_cell"] = zh[j]
                j += 1
    return items



def mark_truncated(items):
    """报告里看不出哪条正文是抓残的，靠 url 里的 status id 回查当天快照判断。

    legacy.full_text 在 ~280 字符处硬截。长度卡在这个窄带、又没有
    note_tweet 全文、结尾又不是标点的，就是半句话。
    """
    snap = {}
    for fn in os.listdir(RAW):
        if fn.startswith("tweets-") and fn.endswith(".json"):
            try:
                for t in json.load(open(os.path.join(RAW, fn), encoding="utf-8")):
                    snap[str(t.get("id"))] = t
            except Exception:
                continue
    for it in items:
        if it["kind"] != "tweet":
            continue
        m = re.search(r"status/(\d{15,})", it.get("url") or "")
        t = snap.get(m.group(1)) if m else None
        if not t:
            continue
        txt = (t.get("text") or "").rstrip()
        if (not t.get("long")
                and 268 <= len(txt) <= 284
                and txt[-1:] not in ".。!！?？)）\"”"):
            it["trunc"] = True
    return items


def render_section(sec, cap=None):
    """渲染成微信里能一眼扫过格式：编号、账号、时间、中文正文、原帖链接。"""
    items = mark_truncated(localize(parse_items(sec)))
    out, n, rc, gc = [], 0, 0, 0
    for it in items:
        if it["kind"] == "sub":
            out.append("\n── " + it["title"] + " ──")
        elif it["kind"] == "tweet":
            # 抓残的半句（legacy 在 280 字符处硬截、又没有 note_tweet 全文）
            # 直接不推：用户要的是别人说的内容，半句话没有内容
            if it.get("trunc"):
                continue
            n += 1
            if cap and n > cap:
                continue
            body = it.get("zh") or clean_en(" ".join(it["text"]))
            if len(body) > BODY_CHARS:
                body = body[:BODY_CHARS].rstrip() + "…"
            if body:
                # 序号和正文第一行同一行，长推文换行时后续行缩进对齐
                lines = [x.strip() for x in body.split("\n") if x.strip()]
                out.append(f"\n{n}. " + (lines[0] if lines else ""))
                for extra in lines[1:]:
                    out.append("   " + extra)
            else:
                out.append(f"\n{n}.")
            if it.get("url"):
                out.append("   " + it["url"])
        elif it["kind"] == "row":
            if rc >= ROW_CAP:
                continue
            rc += 1
            cells = it["cells"]
            name = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cells[0]).replace("**", "")
            cond = it.get("zh_cell") or re.sub(r"`[^`]*`", "", cells[1]).strip()
            cond = clip(re.sub(r"\s{2,}", " ", cond), 110)
            extra = ""
            if len(cells) > 3:
                extra = clip(re.sub(r"<br>", " / ", re.sub(r"`", "", cells[3])), 80)
            out.append(f"· {name}｜{cond}" + (f"｜{extra}" if extra else ""))
        elif it["kind"] == "line":
            m = re.match(GH_RE, it["raw"])
            if m:
                if gc >= REPO_CAP:
                    continue
                gc += 1
                name, star, desc, url = m.group(1), m.group(2) or "", m.group(3), m.group(4)
                zh = it.get("zh") or desc
                if len(zh) > 130:
                    zh = clip(zh, 220)
                out.append(f"· {name} {star} — {zh}")
                if url:
                    out.append(f"   {url}")
            else:
                out.append("· " + clip(re.sub(r"\*\*|`", "", it["raw"][2:]), 170))
    if cap and n > cap:
        out.append(f"\n（本节共 {sec['count']} 条，这里挑互动量前 {cap} 条，完整清单见附件）")
    return "\n".join(out).strip()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    if args:
        path = args[0]
    else:
        path = os.path.join(OUT, f"xwire-{time.strftime('%Y-%m-%d')}.md")
    if not os.path.exists(path):
        print(f"[push] 找不到报告 {path}", file=sys.stderr)
        return 2

    token = load_token()
    to_user = load_recipient()
    if not token:
        print("[push] 凭据里没有微信 bot token", file=sys.stderr)
        return 3

    title, meta, secs = parse_report(path)

    msgs = []
    # 标题日期从报告文件名取，别硬编码——跨天后会一直显示旧日期
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
    day = m.group(1)[5:] if m else time.strftime("%m-%d")
    head = f"【X 快讯 · {day}】"
    if meta:
        # 只留窗口和条数，生成时间这类元信息没人看
        keep = [x for x in meta.split("·") if "生成" not in x]
        head += "\n" + "｜".join(x.strip() for x in keep if x.strip())
    msgs.append(head)

    for key, icon, cat in (("🧠", "①", "llm"), ("🛠", "②", "tech"), ("🆓", "③", "free")):
        sec = secs.get(key)
        if not sec:
            continue
        body = render_section(sec, cap=DIGEST_CAP.get(cat))
        if not body:
            continue
        msgs.append(f"{icon} {sec['name']}\n{body}")

    if dry:
        for i, m in enumerate(msgs, 1):
            print(f"\n===== 第 {i} 段（{len(m)} 字符）=====\n{m}")
        return 0

    # 先补上次没发出去的段落（服务端拒收时不会永久丢内容）
    queue = []
    if os.path.exists(PENDING):
        try:
            queue = json.load(open(PENDING)).get("chunks", [])
        except Exception:
            queue = []
    if queue:
        print(f"[push] 有 {len(queue)} 段待补发", file=sys.stderr)
    queue = list(queue) + msgs

    sent, failed = 0, []
    for i, seg in enumerate(queue, 1):
        if sent >= MAX_PER_RUN:
            # 本轮额度用完，剩下原样留在队列
            failed += [c for k in queue[i - 1:] for c in split(k)]
            break
        parts = split(seg)
        for j, chunk in enumerate(parts, 1):
            ok, code, body = send_text(token, to_user, chunk)
            tag = f"[{i}.{j}]" if len(parts) > 1 else f"[{i}]"
            print(f"{tag} {len(chunk):5d} 字符 → {'OK' if ok else 'FAIL ' + code}", file=sys.stderr)
            if ok:
                sent += 1
            else:
                print(f"       {body}", file=sys.stderr)
                failed.append(chunk)
                # 服务端明确拒收（ret 负数）说明这一轮已经饱和，后面的不必再撞
                if '"ret":-' in body:
                    failed += [c for k in queue[i:] for c in split(k)]
                    break
            time.sleep(SEND_GAP)
        else:
            continue
        break

    # 去重：同一段内容不能在队列里出现两次，否则下一轮会重复推到微信
    uniq = list(dict.fromkeys(failed))
    json.dump({"chunks": uniq}, open(PENDING, "w"), ensure_ascii=False)
    print(f"[push] 发出 {sent} 条，待补 {len(uniq)} 段")
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
