#!/usr/bin/env python3
"""x.com 登录态客户端（GraphQL）

用登录 cookie 走 x.com 自己的 web 接口，不走主页 HTML：
  UserByScreenName        → screen_name 换 userId
  UserOriginalsTimeline   → 拿该账号的原帖时间线（含置顶、长文 note_tweet、浏览量）

cookie 会过期。401/403 时抛 AuthExpired，上层提示重新跑 login.cjs。
"""
import json, os, re, subprocess, tempfile
import urllib.parse as up

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "gql-config.json")
COOKIE_PATH = os.path.join(BASE, "cookie.txt")
META_PATH = os.path.join(BASE, "cookie-meta.json")
UID_CACHE = os.path.join(BASE, "userid-cache.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")


class AuthExpired(Exception):
    """cookie 失效，需要重新登录"""


class XClient:
    def __init__(self, proxy=None):
        # GitHub Actions 里没有浏览器 profile，凭证由 secret 写进 cookie.txt；
        # 本地则走 login.cjs 导出的同一份文件，两条路径格式一致。
        self.cfg = json.load(open(CFG_PATH))
        self.cookie = open(COOKIE_PATH).read().strip()
        meta = json.load(open(META_PATH))
        self.ct0 = meta["ct0"]
        self.proxy = proxy
        self.uid_cache = self._load(UID_CACHE, {})

    @staticmethod
    def _load(p, d):
        try:
            return json.load(open(p))
        except Exception:
            return d

    def _save_uids(self):
        json.dump(self.uid_cache, open(UID_CACHE, "w"), ensure_ascii=False)

    def _gql(self, op, variables, timeout=60):
        c = self.cfg[op]
        q = up.urlencode({
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": c["features"],
        })
        if c.get("fieldToggles"):
            q += "&fieldToggles=" + c["fieldToggles"]
        url = f"https://x.com/i/api/graphql/{c['queryId']}/{op}?{q}"

        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        cmd = ["curl", "-s", "--compressed", "-m", str(timeout), "-o", tmp,
               "-w", "%{http_code}",
               "-H", f"authorization: Bearer {self.cfg['bearer']}",
               "-H", f"x-csrf-token: {self.ct0}",
               "-H", "x-twitter-auth-type: OAuth2Session",
               "-H", "x-twitter-active-user: yes",
               "-H", "x-twitter-client-language: en",
               "-H", f"cookie: {self.cookie}",
               "-H", f"user-agent: {UA}"]
        if self.proxy:
            cmd += ["-x", self.proxy]
        cmd.append(url)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout + 10)
            code = (p.stdout or "").strip()[:3]
            if code in ("401", "403"):
                raise AuthExpired(f"{op} http={code}")
            if code == "429":
                raise RateLimited(f"{op} http=429")
            body = json.load(open(tmp))
            return body
        except json.JSONDecodeError:
            return None
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def user_id(self, screen_name):
        key = screen_name.lower()
        if key in self.uid_cache:
            return self.uid_cache[key]
        d = self._gql("UserByScreenName",
                      {"screen_name": screen_name, "withGrokTranslatedBio": False})
        if not d:
            return None
        try:
            uid = d["data"]["user"]["result"]["rest_id"]
        except (KeyError, TypeError):
            return None
        self.uid_cache[key] = uid
        self._save_uids()
        return uid

    def tweets(self, screen_name, count=40):
        """返回推文列表；置顶单独标记。解析失败返回 []。"""
        uid = self.user_id(screen_name)
        if not uid:
            return []
        d = self._gql("UserOriginalsTimeline", {
            "userId": uid, "count": count,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
        })
        if not d:
            return []
        try:
            ins = d["data"]["user"]["result"]["timeline"]["timeline"]["instructions"]
        except (KeyError, TypeError):
            return []
        return self._parse_instructions(ins)

    @staticmethod
    def _parse_instructions(ins):
        from email.utils import parsedate_to_datetime
        out = []
        for block in ins:
            btype = block.get("type", "")
            entries = []
            if btype == "TimelineAddEntries":
                entries = [(e, False) for e in block.get("entries", [])]
            elif btype == "TimelinePinEntry":
                e = block.get("entry")
                if e:
                    entries = [(e, True)]
            for e, pinned in entries:
                res = (e.get("content", {})
                        .get("itemContent", {})
                        .get("tweet_results", {})
                        .get("result", {}))
                if not res or res.get("__typename") != "Tweet":
                    continue
                leg = res.get("legacy", {}) or {}
                tid = res.get("rest_id") or leg.get("id_str")
                if not tid:
                    continue

                # 长推文：legacy.full_text 会被截断，全文在 note_tweet
                text = leg.get("full_text", "") or ""
                note = (res.get("note_tweet", {})
                         .get("note_tweet_results", {})
                         .get("result", {}) or {})
                if note.get("text"):
                    text = note["text"]

                text = re.sub(r"\s*https://t\.co/\S+", "", text).strip()

                ts = 0
                ca = leg.get("created_at")
                if ca:
                    try:
                        ts = int(parsedate_to_datetime(ca).timestamp() * 1000)
                    except Exception:
                        ts = 0

                user = ((res.get("core", {}) or {}).get("user_results", {}) or {})
                screen = ((user.get("result", {}) or {}).get("core", {}) or {}).get("screen_name")

                out.append({
                    "id": str(tid),
                    "ts": ts,
                    "text": text,
                    "fav": int(leg.get("favorite_count") or 0),
                    "rt": int(leg.get("retweet_count") or 0),
                    "reply": int(leg.get("reply_count") or 0),
                    "views": int((res.get("views") or {}).get("count") or 0),
                    "pinned": pinned,
                    "handle": screen,
                    "long": bool(note.get("text")),
                })
        return out


class RateLimited(Exception):
    pass
