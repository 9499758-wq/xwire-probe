#!/usr/bin/env python3
"""把 X 推文正文翻成简体中文。

为什么单独一层：推文原文是英文，直接推到微信读起来很费劲。
这里按 id 缓存译文，同一条只翻一次；失败就退回原文，不会让整批卡住。
"""
import hashlib, json, os, re, subprocess, sys

BASE = os.path.dirname(os.path.abspath(__file__))
CRED = os.path.expanduser("~/.dsh/.credentials.yaml")
CACHE = os.path.join(BASE, "trans-cache-v3.json")

API = "https://v1.pvaki.com/v1/chat/completions"
MODEL = os.environ.get("XWIRE_TRANSLATE_MODEL", "Kimi-K3")
BATCH = 6

SYS = (
    "你是中文科技编辑，把英文推文译成简体中文。硬性要求："
    "1) 必须像中文母语者自己写的一样通顺，读起来没有翻译腔；"
    "不能出现「投入了哪些东西」「被表达得过头了」这类生硬直译；"
    "2) 意思准确：谁做了什么、出了什么、有什么数据、怎么用、限制是什么，一个都不能漏；"
    "3) 保留 @用户名、产品名、模型名、公司名的原文，不音译不改写；"
    "4) 原文有省略号或半句没说完的，译文也保持不完整，禁止自行补全；"
    "5) 原文多长就译多长，长推文照常完整译出，不要压成一句话概括；"
    "6) 不加译者自己的评价和解释；"
    "7) 严格输出 JSON 数组，每项一个字符串，顺序与输入一一对应，不要任何多余文字。"
)


def _key():
    """取翻译网关的 key。

    本机是 Mac，凭证在 DSH 的 credentials.yaml 里；搬到玩客云后那边没有 DSH，
    改读同目录的 secrets.json。两处都试，谁有就用谁。
    """
    secret = os.path.join(BASE, "secrets.json")
    if os.path.exists(secret):
        try:
            v = json.load(open(secret)).get("BAOYUE_API_KEY")
            if v:
                return v
        except Exception:
            pass
    try:
        txt = open(CRED).read()
    except OSError:
        return None
    m = re.search(r"^\s*BAOYUE_API_KEY:\s*(.+)$", txt, re.M)
    return m.group(1).strip().strip("\"'") if m else None


def _load():
    try:
        return json.load(open(CACHE))
    except Exception:
        return {}


def _save(d):
    json.dump(d, open(CACHE, "w"), ensure_ascii=False)


def _call(key, items, timeout=90):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYS},
            {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    p = subprocess.run(
        ["curl", "-s", "-m", str(timeout), API,
         "-H", f"Authorization: Bearer {key}",
         "-H", "content-type: application/json",
         "-d", json.dumps(payload, ensure_ascii=False)],
        capture_output=True, text=True, timeout=timeout + 10)
    try:
        d = json.loads(p.stdout)
        txt = d["choices"][0]["message"]["content"]
    except Exception:
        return None
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    try:
        out = json.loads(txt)
        if isinstance(out, list) and len(out) == len(items):
            return [str(x).strip() for x in out]
    except Exception:
        pass
    return None


def translate(items, ids=None):
    """items: 英文正文列表；ids: 对应 id 列表（用于缓存）。
    返回中文列表，长度与输入一致。
    """
    if not items:
        return []
    key = _key()
    if not key:
        return list(items)

    cache = _load()
    ids = ids or [str(i) for i in range(len(items))]
    out = [None] * len(items)
    todo = []
    def ck(mid, txt):
        # URL 不唯一标识译文：同一条推文先拿 278 字符、后拿 572 字符，
        # 若只按 URL 缓存，旧译文会永远盖住新的全文译文。
        return mid + "#" + hashlib.sha1(txt.encode("utf-8")).hexdigest()[:12]

    for i, (mid, txt) in enumerate(zip(ids, items)):
        if ck(mid, txt) in cache:
            out[i] = cache[ck(mid, txt)]
        else:
            todo.append(i)

    for start in range(0, len(todo), BATCH):
        idx = todo[start:start + BATCH]
        batch = [items[i] for i in idx]
        got = _call(key, batch)
        if got is None:                       # 整批失败，退原文
            got = batch
        for i, tr in zip(idx, got):
            out[i] = tr
            cache[ck(ids[i], items[i])] = tr
        _save(cache)
    return out


if __name__ == "__main__":
    # 自测：从 stdin 读 JSON 数组
    data = json.load(sys.stdin)
    for src, dst in zip(data, translate(data)):
        print(f"{src}\n  → {dst}\n")
