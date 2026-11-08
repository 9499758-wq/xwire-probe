#!/usr/bin/env python3
"""X 快讯 · 每日 02:00 定时跑

三个分区严格分开：
  🧠 大模型      —— 模型厂官方号 + AI 研究/资讯号，讲模型、训练、推理、评测
  🛠 科技        —— 科技媒体 + 从业者观点，非模型本身
  🆓 能白嫖的    —— 免费 API、试用额度、开源自部署、限免

每条 X 内容都带 https://x.com/<handle>/status/<id> 原帖地址。
白嫖区另附：免费额度清单（来源可查）、OpenRouter 免费模型（实时）、GitHub 新开源 AI 项目。
"""
import os, re, json, time, random, sys, subprocess
from datetime import datetime, timezone, timedelta

from xclient import XClient, AuthExpired, RateLimited

try:
    from translate import translate as _translate
except Exception:            # 翻译不可用时退原文，别让整个抓取失败
    _translate = None

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "raw")
OUT = os.path.join(BASE, "out")
SEEN = os.path.join(BASE, "seen.json")
os.makedirs(RAW, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

PROXY = os.environ.get("XWIRE_PROXY", "http://127.0.0.1:7890")
# 出口不可用时（例如玩客云连不上 x.com），只出白嫖区与公开数据源。
# 这样定时任务在无代理的机器上照样能产出可读报告，而不是整轮失败。
NO_X = os.environ.get("XWIRE_NO_X", "") == "1"
WINDOW_HOURS = int(os.environ.get("XWIRE_WINDOW_HOURS", "36"))
# 报告正文是否译成中文。默认开：仓库里的报告是给人直接读的，不该是英文。
ZH = os.environ.get("XWIRE_ZH", "1") != "0"
# 每抓一个账号后睡多久（秒）；x.com 限流很凶，太小会被封
SLEEP_MIN = float(os.environ.get("XWIRE_SLEEP_MIN", "6"))
SLEEP_MAX = float(os.environ.get("XWIRE_SLEEP_MAX", "12"))
CACHE_MAX_HOURS = float(os.environ.get("XWIRE_CACHE_HOURS", "48"))
TZ8 = timezone(timedelta(hours=8))

UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
]

# ---------------------------------------------------------------- 账号池
# 模型厂官方号 —— 默认「大模型」
LLM_VENDORS = [
    ("OpenAI", "OpenAI"), ("OpenAIDevs", "OpenAI Devs"),
    ("AnthropicAI", "Anthropic"), ("GoogleDeepMind", "DeepMind"),
    ("GoogleAI", "Google AI"), ("AIatMeta", "Meta AI"), ("MistralAI", "Mistral"),
    ("xai", "xAI"), ("huggingface", "HuggingFace"), ("deepseek_ai", "DeepSeek"),
    ("Alibaba_Qwen", "Qwen"), ("Kimi_Moonshot", "Kimi"), ("MiniMax_AI", "MiniMax"),
    ("nvidia", "NVIDIA"),
]
# AI 资讯 / 研究号 —— 默认「大模型」
LLM_RESEARCH = [
    ("TheTuringPost", "The Turing Post"), ("DeepLearningAI", "DeepLearning.AI"),
    ("_akhaliq", "AK"), ("_philschmid", "Philipp Schmid"),
    ("osanseviero", "Omar Sanseviero"), ("jxnlco", "jxnl"),
    ("HamelHusain", "Hamel Husain"), ("lateinteraction", "Late Interaction"),
    ("karpathy", "Karpathy"),
]
# 科技媒体 / 泛科技 —— 默认「科技」
TECH_ACCOUNTS = [
    ("TechCrunch", "TechCrunch"), ("verge", "The Verge"), ("Techmeme", "Techmeme"),
    ("9to5mac", "9to5Mac"), ("sama", "Sam Altman"), ("swyx", "swyx"),
    ("DrJimFan", "Jim Fan"), ("emollick", "Ethan Mollick"), ("steipete", "steipete"),
]
# 白嫖信息源 —— 默认「白嫖」
FREE_ACCOUNTS = [
    ("OpenRouterAI", "OpenRouter"), ("opencode_ai", "OpenCode"),
]

# ---------------------------------------------------------------- 分类词表
MODEL_NAMES = re.compile(
    r"\b(GPT-?[0-9.]|GPT|Claude|Gemini|Qwen|DeepSeek|Kimi|Llama|Grok|Mistral|"
    r"Nemotron|Gemma|Phi-?[0-9]|Command\s?R|Yi-[0-9]|GLM-[0-9]|MiniMax|Hunyuan|"
    r"ERNIE|Sonnet|Opus|Haiku|Flash[-\s]Lite|o[1-4]|"
    r"ChatGPT|Claude\s?Code|Copilot|Cursor|Codex)\b", re.I)
LLM_TERMS = re.compile(
    r"\b(inference|fine-?tun\w*|benchmark\w*|open[- ]weight\w*|tokenizer\w*|MoE|RLHF|"
    r"pre-?train\w*|post-?train\w*|context window|embedding\w*|quantization|"
    r"distillation|checkpoints?|evals?|multimodal|reasoning|alignment|"
    r"agentic|RAG|prompt\w*|hallucination\w*)\b", re.I)
AI_GENERIC = re.compile(
    r"\b(LLM|LLMs|AI models?|AI labs?|AI agents?|machine learning|neural|"
    r"artificial intelligence|大模型|模型)\b", re.I)

FREE_STRONG = re.compile(
    r"(免费|白嫖|限免|试用|赠送|送额度|注册就送|额度|不用信用卡|"
    r"no credit card|free (?:tier|api|credits?|plan|trial|access|usage)|"
    r"open[- ]?source[d]?|开源|open[- ]?weight\w*|self[- ]?host\w*)", re.I)
FREE_PROVIDERS = re.compile(
    r"(OpenRouter|Groq|Cerebras|Together\s?AI|Fireworks|Cloudflare\s?Workers\s?AI|"
    r"AI\s?Studio|Vertex|Colab|Kaggle|Hugging\s?Face|huggingface|"
    r"OpenCode|Ollama|LM\s?Studio|vLLM|llama\.cpp|Replicate|Modal|RunPod)", re.I)

ORG = re.compile(
    r"(OpenAI|Open\s?AI|Anthropic|Google|DeepMind|Meta|Microsoft|Nvidia|"
    r"Mistral|xAI|Hugging\s?Face|DeepSeek|Alibaba|Qwen|Moonshot|Kimi|MiniMax|"
    r"ByteDance|Baidu|Tencent|Apple|Amazon|AWS|Tesla|SpaceX|Intel|AMD|Qualcomm|"
    r"Samsung|TSMC|ASML|Fireworks|Groq|Cerebras|Together|Cloudflare)", re.I)
NUM_UNIT = re.compile(
    r"(?:\$|€|¥)\s?\d|\d{1,3}\s?(?:%|B|M|K|billion|million|"
    r"tokens?|params?|GB|TB|ms|倍)|\bv?\d+\.\d+|\b(?:20\d\d)", re.I)
LINKY = re.compile(r"(https?://|github\.com|arxiv\.org|huggingface\.co|/status/)", re.I)
NOISE = re.compile(
    r"(lol|lmao|haha|😂|🤣|playing|good night|\bgm\b|\bbrb\b|"
    r"come hang|level \d|\bpve\b|\bgg\b)", re.I)
CLAIMABLE = re.compile(
    r"(can claim|claim your|claimable|redeem|voucher|billing settings|"
    r"领取|注册就送|免费领|no credit card)", re.I)
CREDIT_WORD = re.compile(
    r"(api credits?|claim\b.*credits?|billing settings|quota|额度|"
    r"subscribers? can|eligible\b|redeem|voucher|领取)", re.I)

JUNK = re.compile(r"^(@\w+\s*)+$|^https?://\S+$|^[\W_]{0,10}$")


def curl_get(url, timeout=25, tries=3, proxy=True, retry_on_rate_limit=False):
    """proxy=False 时直连（api.github.com / huggingface.co 在代理下会 000）
    retry_on_rate_limit=True 时，429 或空壳页面会触发更长退避后重试。"""
    for attempt in range(tries):
        try:
            cmd = ["curl", "-sL", "--compressed", "-m", str(timeout),
                   "-A", random.choice(UAS), "-w", "\n%{http_code}"]
            if proxy:
                cmd += ["-x", PROXY]
            cmd.append(url)
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout + 10)
            if p.returncode == 0 and p.stdout.strip():
                body, _, code = p.stdout.rpartition("\n")
                if code.strip() == "429":
                    rate_limited = True
                else:
                    return body
        except Exception:
            pass
        # 429 / 失败：指数退避（限流时等得更久）
        back = (12 + attempt * 15) if retry_on_rate_limit else (1.5 + attempt * 2)
        time.sleep(back)
    return None


def signal_score(text):
    """给一条推文的可核验信号打分。分数低的一律不收，
    避免「playing pve horde」这种闲聊被当新闻。"""
    sc = 0
    if MODEL_NAMES.search(text):
        sc += 5          # 点名了具体模型，最强信号
    if ORG.search(text):
        sc += 2          # 点名了机构
    if LINKY.search(text):
        sc += 2          # 带链接，可溯源
    if NUM_UNIT.search(text):
        sc += 2          # 有数字/金额/版本，具体
    terms = len(LLM_TERMS.findall(text))
    sc += min(terms * 2, 4)
    if FREE_PROVIDERS.search(text):
        sc += 3
    if CREDIT_WORD.search(text):
        sc += 2
    if AI_GENERIC.search(text):
        sc += 1
    n = len(text)
    if n >= 70:
        sc += 1
    if n >= 120:
        sc += 1
    if NOISE.search(text):
        sc -= 4          # 口语/游戏/表情，扣分
    return sc


KEEP_THRESHOLD = int(os.environ.get("XWIRE_MIN_SCORE", "3"))
# 媒体号/厂商号发的东西本身就有价值，阈值可以更宽；
# 研究号（个人号）噪声大，必须命中硬信号。
LOOSE_CATS = ("llm", "tech", "free")


def should_keep(text, default_cat="tech"):
    t = text.strip()
    if NOISE.search(t):
        return False                       # 口语/游戏/表情，直接砍
    if len(t) < 24 and not MODEL_NAMES.search(t):
        return False                       # 太短，无信息量
    sc = signal_score(t)
    if sc >= KEEP_THRESHOLD:
        return True
    # 兜底：非研究号的长文，只要不是噪声就收（媒体标题往往没术语）
    if default_cat in LOOSE_CATS and len(t) >= 55:
        return True
    return False


def classify(text, default_cat):
    """返回分区名；返回 None 表示丢弃。
    先过信号门控，再按信号强度判区：
      白嫖 > 具体模型/额度 > 技术术语 > 机构/泛 AI > 账号默认
    """
    if not should_keep(text, default_cat):
        return None

    n_free = len(FREE_STRONG.findall(text))
    if n_free and (FREE_PROVIDERS.search(text) or n_free >= 2
                   or CREDIT_WORD.search(text)):
        return "free"
    # 「有资格领 / 去账单里兑换」这类可领取动作本身就算白嫖，
    # 哪怕正文点名了模型（ChatGPT Pro 领 API credits 就是典型）
    if CLAIMABLE.search(text):
        return "free"
    if CREDIT_WORD.search(text) and default_cat == "free":
        return "free"
    if MODEL_NAMES.search(text) or len(LLM_TERMS.findall(text)) >= 2:
        return "llm"
    if AI_GENERIC.search(text):
        return "llm" if default_cat in ("llm", "research") else default_cat
    if ORG.search(text) and default_cat in ("llm", "research"):
        return "llm"
    # 无强信号的：研究号不发（噪声），厂商号保留（自家动态），其余按默认
    if default_cat == "research":
        return None
    return default_cat


def engage(t):
    return t["fav"] + t["rt"] * 3 + t["reply"]


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def grab_one(client, handle, name, default_cat, cutoff):
    """用 GraphQL 拿一个账号的原帖时间线。

    登录态下这条路比抓主页 HTML 稳得多：不触发主页限流、
    能拿到长推文全文（note_tweet）、还有浏览量。
    """
    try:
        ts = client.tweets(handle, count=40)
    except AuthExpired:
        raise
    except RateLimited:
        return [], "throttled"
    except Exception:
        return [], "net-fail"

    if not ts:
        return [], "no-tweets"

    # 落盘一份原始数据，供次日限流时回退
    path = os.path.join(RAW, f"g_{handle}.json")
    json.dump(ts, open(path, "w"), ensure_ascii=False)

    keep = []
    for t in ts:
        if t["ts"] / 1000 < cutoff:
            continue
        cat = classify(t["text"], default_cat)
        if not cat:
            continue
        t["handle"] = handle
        t["name"] = name
        t["cat"] = cat
        # legacy.full_text 在 ~280 字符处硬截。长度正好卡在这个区间、
        # 又没有 note_tweet 全文、结尾不是标点的，是抓残了的半句——
        # 这种宁可整条不推，也不要把「…探索了 4 种不同的方式」这种半句给人看。
        t["trunc"] = (not t.get("long")
                      and 268 <= len(t["text"]) <= 284
                      and t["text"].rstrip()[-1:] not in ".。!！?？)）\"”")
        keep.append(t)
    return keep, "ok"


def load_raw_cache(handle, cutoff, default_cat, name):
    """限流时回退到本地 g_<handle>.json（只在 CACHE_MAX_HOURS 内有效）"""
    path = os.path.join(RAW, f"g_{handle}.json")
    if not os.path.exists(path):
        return [], None
    age_h = (time.time() - os.path.getmtime(path)) / 3600
    if age_h > CACHE_MAX_HOURS:
        return [], None
    try:
        ts = json.load(open(path))
    except Exception:
        return [], None
    keep = []
    for t in ts:
        if t["ts"] / 1000 < cutoff:
            continue
        cat = classify(t["text"], default_cat)
        if not cat:
            continue
        t["handle"] = handle
        t["name"] = name
        t["cat"] = cat
        # legacy.full_text 在 ~280 字符处硬截。长度正好卡在这个区间、
        # 又没有 note_tweet 全文、结尾不是标点的，是抓残了的半句——
        # 这种宁可整条不推，也不要把「…探索了 4 种不同的方式」这种半句给人看。
        t["trunc"] = (not t.get("long")
                      and 268 <= len(t["text"]) <= 284
                      and t["text"].rstrip()[-1:] not in ".。!！?？)）\"”")
        keep.append(t)
    return keep, f"{age_h:.1f}h前"


def name_lookup(handle):
    for h, n in LLM_VENDORS + LLM_RESEARCH + TECH_ACCOUNTS + FREE_ACCOUNTS:
        if h == handle:
            return n
    return handle


def openrouter_free():
    txt = curl_get("https://openrouter.ai/api/v1/models")
    if not txt:
        return []
    try:
        d = json.loads(txt)
    except Exception:
        return []
    out = []
    for m in d.get("data", []):
        p = m.get("pricing", {})
        try:
            if float(p.get("prompt", 1)) == 0 and float(p.get("completion", 1)) == 0:
                out.append((m["id"], int(m.get("context_length") or 0),
                            (m.get("name") or "")[:44]))
        except Exception:
            pass
    return out


def deals():
    """免费额度 / 试用 / 限免；过滤过期与 stale"""
    txt = curl_get("https://cdn.jsdelivr.net/gh/RomeroYang/free-llm-api-deals@main/deals.json",
                   timeout=20)
    if not txt:
        return []
    try:
        d = json.loads(txt)
    except Exception:
        return []
    today = datetime.now(TZ8).strftime("%Y-%m-%d")
    live = []
    for e in d.get("entries", []):
        if e.get("stale"):
            continue
        exp = e.get("expires_at") or ""
        if exp and exp < today:
            continue
        if (e.get("kind") or "") not in ("free-tier", "always-free", "trial-credit", "time-limited"):
            continue
        live.append(e)
    live.sort(key=lambda x: (x.get("verified_at") or ""), reverse=True)
    return live


def github_free_stuff():
    """GitHub 近 30 天新出的「免费 API / 免费额度」类开源项目（关键词命中）"""
    d30 = (datetime.now(TZ8) - timedelta(days=30)).strftime("%Y-%m-%d")
    found, seen_names = [], set()
    # 注意：GitHub 搜索里 `+` 是限定词分隔符，必须字面保留；
    # 编码成 %2B 会让整个串变字面量 → total_count:0
    queries = [
        f"created:%3E{d30}+stars:%3E20+free+api",
        f"created:%3E{d30}+stars:%3E20+free+llm",
        f"pushed:%3E{d30}+stars:%3E100+topic:free-api",
        f"stars:%3E1500+topic:awesome-list+llm",
        f"pushed:%3E{d30}+stars:%3E300+free+credits",
    ]
    for q in queries:
        txt = curl_get(f"https://api.github.com/search/repositories"
                       f"?q={q}&sort=stars&order=desc&per_page=12",
                       timeout=20, proxy=False)
        if not txt:
            continue
        try:
            d = json.loads(txt)
        except Exception:
            continue
        for r in d.get("items", []):
            nm = r["full_name"]
            if nm in seen_names:
                continue
            desc = (r.get("description") or "")
            # 去掉刷榜 fork：同名同描述只留星最多的
            key = desc.lower()[:60]
            if key and key in seen_names:
                continue
            seen_names.add(nm)
            seen_names.add(key)
            found.append({"name": nm, "star": r["stargazers_count"],
                          "desc": desc.strip(), "url": r["html_url"]})
    return dedupe_repos(found, limit=10)


FREE_API_REPO = "https://raw.githubusercontent.com/mnfst/awesome-free-llm-apis/main/README.md"


def free_llm_apis():
    """解析 awesome-free-llm-apis：每个提供方的永久免费额度 + 可用模型 + 限速。
    这是「白嫖」区最硬的资产——注册就能拿 key 直接用。

    做法：先按 `### [名称](取key地址) 国旗` 切段，段内再宽松扫表格行，
    不依赖段内顺序（表头/说明/Base URL 次序会变）。
    """
    txt = curl_get(FREE_API_REPO, timeout=25, proxy=False)
    if not txt or "###" not in txt:
        txt = curl_get(FREE_API_REPO, timeout=25)   # 直连失败再走代理
    if not txt:
        return []

    heads = [(m.start(), m.group(1).strip(), m.group(2).strip(),
              (m.group(3) or "").strip())
             for m in re.finditer(r'^###\s+\[([^\]]+)\]\(([^)]+)\)\s*(\S*)',
                                  txt, re.M)]
    out = []
    for i, (pos, name, key_url, flag) in enumerate(heads):
        seg = txt[pos:(heads[i + 1][0] if i + 1 < len(heads) else len(txt))]
        models = []
        for ln in seg.splitlines():
            if not ln.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            first = cells[0].strip("`")
            if not first or first.lower().startswith(("model name", "---", ":")):
                continue
            if set(first) <= {"-", ":", " "}:
                continue
            models.append({
                "id": first,
                "ctx": cells[1].strip("`") if len(cells) > 1 else "",
                "maxout": cells[2].strip("`") if len(cells) > 2 else "",
                "modality": cells[3] if len(cells) > 3 else "",
                "limit": cells[4] if len(cells) > 4 else "",
            })
        if not models:
            continue
        base = ""
        mb = re.search(r'Base URL:?\s*`([^`]+)`', seg)
        if mb:
            base = mb.group(1).strip()
        # 段内的说明文字（去掉表格/标题/脚注）
        blurb = " ".join(
            t.strip() for t in seg.splitlines()[1:]
            if t.strip() and not t.lstrip().startswith(("|", "Base", "[^", "#"))
        )
        blurb = re.sub(r'\[\^\d+\]', '', blurb)
        blurb = blurb
        out.append({"provider": name, "key_url": key_url, "flag": flag,
                    "base": base, "blurb": blurb, "models": models})
    return out



def clip(text, limit):
    """按词边界截断：宁可短一点，也不要把一个单词切成两半。"""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit * 0.6 else cut).rstrip(" ,;:-") + "…"

def github_new_ai():
    """GitHub 近 7 天冒头的 AI 开源项目"""
    d7 = (datetime.now(TZ8) - timedelta(days=7)).strftime("%Y-%m-%d")
    # 注意：+ 是 GitHub 搜索里的限定词分隔符，必须字面保留，不能编码成 %2B
    q = (f"https://api.github.com/search/repositories?q=created:%3E{d7}"
         f"+stars:%3E30+topic:llm&sort=stars&order=desc&per_page=15")
    txt = curl_get(q, timeout=20, proxy=False)
    if not txt:
        return []
    try:
        d = json.loads(txt)
    except Exception:
        return []
    items = d.get("items", [])
    return dedupe_repos([
        {"name": r["full_name"], "star": r["stargazers_count"],
         "desc": (r.get("description") or "")[:70], "url": r["html_url"]}
        for r in items])


REPO_NOISE = re.compile(r"(ai[-_]?manus|ai[-_]manus)", re.I)


def repo_key(rec):
    """同一项目被开成十几个 fork 刷榜时，把它们折叠成一个 key：
    仓库名去掉连字符/下划线/点、只保留主干名，再看描述首句。"""
    short = rec["name"].split("/")[-1].lower()
    short = re.sub(r"[-_.]", "", short)
    short = re.sub(r"(ai)+manus", "aimanuss", short)
    desc = re.sub(r"[^a-z0-9]", "", rec["desc"].lower())[:40]
    return (short, desc)


def dedupe_repos(recs, limit=None):
    """同名变体 / 同描述只留星最高的那一条"""
    best = {}
    for r in recs:
        if REPO_NOISE.search(r["name"]) or REPO_NOISE.search(r["desc"]):
            # 已知刷榜家族：只保留星数明显更高、名字干净的一条
            pass
        k = repo_key(r)
        if k not in best or r["star"] > best[k]["star"]:
            best[k] = r
    out = sorted(best.values(), key=lambda x: -x["star"])
    return out[:limit] if limit else out


def fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TZ8).strftime("%m-%d %H:%M")


def fmt_k(n):
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def main():
    now8 = datetime.now(TZ8)
    today = now8.strftime("%Y-%m-%d")
    cutoff = datetime.now(timezone.utc).timestamp() - WINDOW_HOURS * 3600
    seen = load_json(SEEN, {})

    handles = ([(h, n, "llm") for h, n in LLM_VENDORS] +
               [(h, n, "research") for h, n in LLM_RESEARCH] +
               [(h, n, "tech") for h, n in TECH_ACCOUNTS] +
               [(h, n, "free") for h, n in FREE_ACCOUNTS])
    print(f"[*] {len(handles)} 账号 / {WINDOW_HOURS}h 窗口", file=sys.stderr)

    auth_dead = False
    raw_tweets, empty, throttled, from_cache = [], [], [], []

    if NO_X:
        print("[!] XWIRE_NO_X=1，跳过 X 抓取，只出白嫖区", file=sys.stderr)
        handles = []

    # 登录态走 GraphQL，不吃主页那种限流；仍保留小间隔 + 缓存回退，
    # 避免短时间内几十个请求把账号打成风控。
    client = None
    if not NO_X:
        try:
            client = XClient(proxy=PROXY)
        except FileNotFoundError:
            print("[!] 未找到 cookie.txt，请先跑 login.cjs", file=sys.stderr)
            sys.exit(4)

    for idx, (h, n, c) in enumerate(handles, 1):
        try:
            keep, status = grab_one(client, h, n, c, cutoff)
        except AuthExpired:
            print("[!] 登录态失效（401/403），请重新跑 login.cjs", file=sys.stderr)
            auth_dead = True
            break
        if status in ("throttled", "net-fail"):
            keep2, cached_at = load_raw_cache(h, cutoff, c, n)
            if keep2:
                raw_tweets += keep2
                from_cache.append(h)
                status = f"cache({cached_at})"
            else:
                throttled.append(h)
        elif not keep:
            empty.append(h)
        else:
            raw_tweets += keep
        print(f"  [{idx:2d}/{len(handles)}] @{h:16s} {len(keep):2d} {status}", file=sys.stderr)
        time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

    # 当天累积：同一天重跑时把上午抓到的条目并回来，避免报告被冲空
    today_cache = os.path.join(RAW, f"tweets-{today}.json")
    merged = {t["id"]: t for t in load_json(today_cache, [])}
    for t in raw_tweets:
        # 同一条推文可能有长短两版：早期按 HTML 解析只拿到 legacy.full_text 的 278 字符，
        # 后来走 GraphQL 才有 note_tweet 全文。取长的，别让旧短版盖掉全文。
        old = merged.get(t["id"])
        if old and len(old.get("text", "")) > len(t.get("text", "")):
            t = {**t, "text": old["text"], "long": old.get("long")}
        merged[t["id"]] = t          # 新抓的覆盖旧快照（互动数会变）
    raw_tweets = list(merged.values())

    # 快照里的正文如果还能从 g_<handle>.json 找到更完整的版本，就补上。
    # 这样即使某天某个账号没重抓，报告也不会留着截断的半句。
    full = {}
    for fn in os.listdir(RAW):
        if not fn.startswith("g_") or not fn.endswith(".json"):
            continue
        try:
            for t in load_json(os.path.join(RAW, fn), []):
                tid = str(t.get("id"))
                if len(t.get("text", "")) > len(full.get(tid, {}).get("text", "")):
                    full[tid] = t
        except Exception:
            continue
    for t in raw_tweets:
        src = full.get(t["id"])
        if src and len(src.get("text", "")) > len(t.get("text", "")):
            t["text"] = src["text"]
            t["long"] = src.get("long")

    json.dump(raw_tweets, open(today_cache, "w"), ensure_ascii=False)

    # seen 记的是「哪天的日报收录过它」：
    #   seen 里没有         → 全新，收
    #   seen[id] == 今天     → 今天已收过（重跑），仍收，报告不该变短
    #   seen[id] == 更早日期 → 别的日子报过，跳过，避免重复
    # cookie 过期时不中止：白嫖区那些公开数据源不依赖登录，
    # 照常出报告，退出码留给外层判断要不要提醒重新登录。

    fresh = [t for t in raw_tweets
             if t["id"] not in seen or seen[t["id"]] == today]
    buckets = {c: sorted([t for t in fresh if t["cat"] == c],
                         key=engage, reverse=True)[:30] for c in ("llm", "tech", "free")}

    or_free = openrouter_free()
    deal_list = deals()
    gh_new = github_new_ai()
    gh_free = github_free_stuff()
    free_apis = free_llm_apis()

    L = [f"# X 快讯 · {today}", "",
         f"> 窗口 {WINDOW_HOURS}h · 生成 {now8.strftime('%Y-%m-%d %H:%M')} (UTC+8) · "
         f"新条目 {len(fresh)} 条", ""]

    for cat, title in [("llm", "🧠 大模型"), ("tech", "🛠 科技"), ("free", "🆓 能白嫖的")]:
        items = sorted(buckets[cat], key=lambda x: x["ts"], reverse=True)
        L += [f"## {title}（{len(items)} 条）", ""]
        if not items:
            L += ["_本窗口内这些号没发新东西_", ""]
        # 正文先中文化。原来只在推送那一步译，导致仓库里的报告全是英文，
        # 直接读 GitHub 的人看到的就是一屏原文。
        if ZH and _translate and items:
            try:
                texts = [re.sub(r'\s+', ' ', t["text"]).strip() for t in items]
                got = _translate(texts, [t["id"] for t in items])
                for t, zh in zip(items, got):
                    if zh and zh.strip():
                        t["zh"] = zh.strip()
            except Exception as e:
                print(f"[!] 翻译失败，正文用原文：{e}", file=sys.stderr)

        for t in items:
            body = re.sub(r'\n{3,}', '\n\n', (t.get("zh") or t["text"]).strip()).replace("\n", "\n> ")
            L += [f"- **@{t['handle']}**（{t['name']}）· {fmt_ts(t['ts'])}"
                  f" · ♥{fmt_k(t['fav'])} 🔁{fmt_k(t['rt'])}",
                  f"> {body}",
                  f"> → https://x.com/{t['handle']}/status/{t['id']}",
                  ""]

    # ---------------- 白嫖补充三块 ----------------
    if free_apis:
        total_models = sum(len(x["models"]) for x in free_apis)
        L += [f"### 🎯 永久免费 LLM API（{len(free_apis)} 家 · {total_models} 个模型 · 注册即用）", "",
              "| 提供方 | 免费条件 | 基线地址 | 代表模型 / 限速 |", "|---|---|---|---|"]
        for a in free_apis:
            pick = a["models"][:2]
            far = "<br>".join(
                f"`{x['id']}` {x['ctx'] or ''} {('· ' + x['limit']) if x['limit'] else ''}".strip()
                for x in pick)
            more = f"<br>+{len(a['models']) - len(pick)} 个" if len(a["models"]) > len(pick) else ""
            L.append(f"| **[{a['provider']}]({a['key_url']})** {a['flag']} | "
                     f"{a['blurb']} | `{a['base'] or '—'}` | {far}{more} |")
        L += ["", "> 源 `mnfst/awesome-free-llm-apis`：逐家手测的永久免费额度，"
                  "模型名 / 上下文 / 限速都是仓库原始记录", ""]

    if deal_list:
        L += [f"### 🎫 免费额度 / 限免清单（{len(deal_list)} 条）", "",
              "| 提供方 | 内容 | 限额 | 到期 | 源 |", "|---|---|---|---|---|"]
        for e in deal_list[:30]:
            ttl = (e.get("title") or "").replace("|", "/")[:44]
            sry = clip((e.get("summary") or "").replace("|", "/"), 90)
            parts = []
            if e.get("rpm") not in (None, "None", ""):
                parts.append(f"{e['rpm']} RPM")
            if e.get("rpd") not in (None, "None", ""):
                parts.append(f"{e['rpd']} RPD")
            if e.get("credit_usd") not in (None, "None", ""):
                parts.append(f"${e['credit_usd']}")
            lim = " / ".join(parts) or "—"
            exp = e.get("expires_at") or "长期"
            src = e.get("source_url") or ""
            L.append(f"| {e.get('provider') or '—'} | **{ttl}**<br>{sry} | {lim} | {exp} | "
                     f"{('[源](' + src + ')') if src else '—'} |")
        L += ["", "> 数据源 `free-llm-api-deals`（每条带 verified_at 与原始出处）", ""]

    if or_free:
        L += [f"### 🔑 OpenRouter 免费模型（{len(or_free)} 个 · 实时）", "",
              "| 模型 | 上下文 | 名称 |", "|---|---|---|"]
        for mid, ctx, nm in sorted(or_free, key=lambda x: -x[1])[:22]:
            L.append(f"| `{mid}` | {ctx//1024}k | {nm} |")
        L += ["", "> 端点 `https://openrouter.ai/api/v1` · 无 key 也能查清单", ""]

    if gh_free:
        L += [f"### 🧲 GitHub 上能直接拿的免费资源（{len(gh_free)} 个）", ""]
        for g in gh_free:
            L.append(f"- **{g['name']}** ★{g['star']} — {g['desc']} · {g['url']}")
        L += [""]

    if gh_new:
        L += [f"### 🐙 GitHub 新冒头 AI 开源项目（近 7 天 · {len(gh_new)} 个）", ""]
        for g in gh_new:
            L.append(f"- **{g['name']}** ★{g['star']} — {g['desc']} · {g['url']}")
        L += [""]

    if auth_dead:
        L += ["<sub>⚠️ 登录态已失效，本轮只抓了公开数据源</sub>", ""]

    if empty or throttled:
        bits = []
        if empty:
            bits.append(f"无新内容：{', '.join('@'+x for x in sorted(empty))}")
        if from_cache:
            bits.append(f"限流回退缓存：{', '.join('@'+x for x in sorted(from_cache))}")
        if throttled:
            bits.append(f"被限流且无缓存：{', '.join('@'+x for x in sorted(throttled))}")
        L += [f"<sub>{' · '.join(bits)}</sub>", ""]

    path = os.path.join(OUT, f"xwire-{today}.md")
    open(path, "w").write("\n".join(L))

    for t in fresh:
        seen[t["id"]] = today
    for fn in os.listdir(RAW):
        if fn.startswith("tweets-") and fn[7:17] < (now8 - timedelta(days=7)).strftime("%Y-%m-%d"):
            try:
                os.remove(os.path.join(RAW, fn))
            except Exception:
                pass
    keep_after = (now8 - timedelta(days=60)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= keep_after}
    json.dump(seen, open(SEEN, "w"), ensure_ascii=False)

    # 报告也要清：Actions 每轮都 commit，不清的话仓库一年后会堆 365 份
    out_cut = (now8 - timedelta(days=30)).strftime("%Y-%m-%d")
    for fn in os.listdir(OUT):
        if fn.startswith("xwire-") and fn.endswith(".md"):
            day = fn[6:16]
            if day and day < out_cut:
                try:
                    os.remove(os.path.join(OUT, fn))
                except Exception:
                    pass

    print(f"[+] 新 {len(fresh)} 条 | 额度 {len(deal_list)} | 免费模型 {len(or_free)} | "
          f"新项目 {len(gh_new)} | 免费资源 {len(gh_free)} | 免费API {len(free_apis)} → {path}", file=sys.stderr)
    print(path)

    if auth_dead:
        print("[x] 登录态已失效，本轮只有公开数据源；请重新导出 cookie.txt", file=sys.stderr)
        sys.exit(5)


if __name__ == "__main__":
    main()
