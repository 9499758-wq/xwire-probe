# xwire

每天定时抓取 X（Twitter）上关于**大模型**、**科技**、**能白嫖的**内容，生成中文快讯。

每条内容都带 `https://x.com/<handle>/status/<id>` 原帖地址，点进去就能看原文。

```
## 🧠 大模型（4 条）
## 🛠 科技（30 条）
## 🆓 能白嫖的（0 条）
### 🎯 永久免费 LLM API（15 家 · 91 个模型 · 注册即用）
### 🎫 免费额度 / 限免清单（5 条）
### 🔑 OpenRouter 免费模型（16 个 · 实时）
### 🧲 GitHub 上能直接拿的免费资源（10 个）
### 🐙 GitHub 新冒头 AI 开源项目（近 7 天 · 6 个）
```

## 为什么要在 Actions 里跑

抓 X 需要一个能访问 x.com 的出口。家里那台常开的 ARM 小主机（玩客云）对 Twitter 系全部域名都不通——换端口、换 SNI、换 CF 优选节点、换 2600 个公共免费节点都试过，全是 000。这是网络层面的封锁，不是配置问题。

GitHub Actions 的 runner 直连没问题，所以**抓取搬到这里，机器只负责拉结果和推送**。这样本地关机也不影响出报。

## 工作流

```
GitHub Actions（每小时级别的 cron）
  ├─ crawl.py      抓 X 时间线，分类，生成报告
  └─ commit        src/out/*.md + src/raw/*.json 推回本仓库
        │
        ▼
本地小主机
  ├─ fetch_remote.py   用 contents API 拉当天报告（不需要 git）
  ├─ translate.py      正文译成中文（带缓存）
  └─ push_wx.py        分段推到微信，失败的进队列下轮补
```

## 文件

| 文件 | 作用 |
|---|---|
| `src/crawl.py` | 主流程：抓取 → 打分门控 → 三分类 → 拼报告 |
| `src/xclient.py` | X GraphQL 客户端（`UserOriginalsTimeline`） |
| `src/gql-config.json` | GraphQL queryId / features / 请求头 |
| `src/translate.py` | 中文翻译（OpenAI 兼容接口 + 按内容哈希缓存） |
| `src/push_wx.py` | 解析报告、译中文、分段发微信 |
| `src/fetch_remote.py` | 从本仓库拉报告（contents API，base64 解码） |
| `src/out/` | 每天的报告 `xwire-YYYY-MM-DD.md` |
| `src/raw/` | 当天推文快照 `tweets-*.json` + 各账号原始返回 `g_*.json` |
| `src/seen.json` | 已推送过的推文 id（保留 60 天，防重复） |

## 分类怎么判

不是关键词硬匹配——先算信号分，再决定归属：

- **分数**：命中模型名 +5、厂商名 +2、带链接 +2、带数字单位 +2、术语每个 +2（上限 4）、免费渠道名 +3、额度词 +2、泛 AI 词 +1；长度 ≥70 / ≥120 各 +1；命中噪声词 −4
- **门控**：噪声直接丢；内容 <24 字符且无模型名丢；分数 ≥3 收；媒体号/厂商号 ≥55 字符兜底收（新闻标题常不含术语）
- **归属**：白嫖信号 > 具体模型 > 术语 ≥2 > 泛 AI > 账号默认档
- **半句丢弃**：`legacy.full_text` 在 ~280 字符处硬截，长度正好落在 268–284 且结尾不是标点的，判定为抓残的半句，整条不推

研究号（`@karpathy`、`@jxnlco` 一类）噪声大，只走高分门槛；媒体号和模型厂默认档放宽。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `XWIRE_WINDOW_HOURS` | 36 | 时间窗口 |
| `XWIRE_SLEEP_MIN` / `MAX` | 6 / 12 | 每抓一个账号后随机睡多久（x.com 限流很凶） |
| `XWIRE_CACHE_HOURS` | 48 | 限流时回退本地 `g_*.json` 的有效期 |
| `XWIRE_NO_X` | 空 | 设 `1` 则跳过 X 抓取，只出白嫖区 |
| `XWIRE_PROXY` | `http://127.0.0.1:7890` | 出口代理；Actions 里置空 |
| `XWIRE_MIN_SCORE` | 3 | 门控阈值 |

## Secrets

| Name | 用途 |
|---|---|
| `XWIRE_COOKIE` | 登录后的 `auth_token=...; ct0=...` |
| `XWIRE_META` | cookie 元信息（用于判断过期） |
| `BAOYUE_API_KEY` | 翻译接口的 key |

凭证只在运行时落成临时文件，用完在提交前 `rm` 掉，`.gitignore` 也挡着。

## 定时

```yaml
- cron: '0 6 * * *'    # 北京 14:00
- cron: '0 10 * * *'   # 北京 18:00
- cron: '0 16 * * *'   # 北京 00:00
```

GitHub 的 schedule 在仓库长期无提交时可能被降频，所以每轮跑完都会 commit 一次，本身也算保活。

## 已知限制

- **微信主动推送有硬配额**：一张 `context_token` 大约只放 10 条。推送按每轮 ≤3 段、一天三轮 = 9 条控制，发不完的进 `out/pending-wx.json`，下轮自动补。
- **cookie 会过期**：`auth_token` 失效时 `crawl.py` 退出码 5，需重新登录导出后更新 secret。
- **公共免费代理整体不可用**：实测 2411 个节点里 TCP 可达 1127 个，逐个发真实请求 0 命中。

## 备注

仓库名里的 `probe` 是早期做连通性探测留下的，内容和探针无关。
