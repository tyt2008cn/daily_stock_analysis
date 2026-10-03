# 数据源全景说明（DATA_SOURCES）

> 本文说明 DSA（股票智能分析系统）**需要哪些数据、每类数据由谁提供、用来做什么**，
> 以及在项目内置数据源之外**还可以接入什么**（免费 / 收费）。
>
> 适用范围：上游项目 `ZhuLinsen/daily_stock_analysis` 的能力现状 + 本 fork 的本地改动。
> 本 fork 的当前启用状态见文末 [§8](#8-本机当前启用状态)。
> 行号引用对应本仓库当前工作区；标注「**未核实**」的地方是静态读码无法证实的，请以官方文档为准。

---

## 0. 一张图看懂数据分层

```
                    ┌─────────────────────────────────────────┐
   行情/技术层 ────► │ data_provider/   指数·K线·实时·筹码·资金流 │ ──► 技术指标计算
                    └─────────────────────────────────────────┘      （纯 pandas）
                    ┌─────────────────────────────────────────┐
   基本面层  ────►  │ fundamental_adapter  估值·成长·业绩·机构   │ ──► 提示词「基本面/财报」段
                    └─────────────────────────────────────────┘
                    ┌─────────────────────────────────────────┐
   新闻情报层 ────► │ ① 搜索 provider（7 家，需 Key）            │ ──► 风险警报 / 利好催化
                    │ ② 本地资讯池（RSS/NewsNow，免费）          │     / 舆情情绪 / 业绩预期
                    │ ③ akshare 免费源（新闻+公告，免费）★本fork │
                    │ ④ 社媒情绪（仅美股）                       │
                    └─────────────────────────────────────────┘
                    ┌─────────────────────────────────────────┐
   决策层    ────►  │ LLM（litellm，多provider）               │ ──► 决策仪表盘 JSON
                    └─────────────────────────────────────────┘
                    ┌─────────────────────────────────────────┐
   出口层    ────►  │ 企业微信/飞书/TG/Discord/Slack/邮件/...    │ ──► 推送
                    └─────────────────────────────────────────┘
```

**核心设计原则**：数字与表格由代码确定性生成，LLM 只负责解释。所以数据源的可靠性直接决定报告质量。

---

## 1. 行情数据源（`data_provider/`）

### 1.1 重要前提：不是"一条固定回退链"

普通 A 股日线走的是**按 priority 排序的遍历 + 逐源 failover**（`base.py:1879`、排序 `base.py:1864`），
只有下面几种是**写死的链路**：

| 场景 | 固定链路 | 位置 |
|---|---|---|
| 已登记 A 股指数日线 | 腾讯 → AkShare → TickFlow → YFinance | `base.py:634-639` |
| 指数实时 | AkShare(tencent) → AkShare(sina) → Efinance → TickFlow | `base.py:640-645` |
| 美股日线 | Finnhub → AlphaVantage → YFinance → Longbridge（有 Longbridge 时提到最前） | `base.py:1956-1968` |
| A 股实时 | `REALTIME_SOURCE_PRIORITY` 默认 `tencent,akshare_sina,efinance,akshare_em` | `config.py:1228` |

### 1.2 全部数据源一览

| 数据源 | 优先级 | 覆盖市场 | 需要凭证？ | 主要能力 |
|---|---|---|---|---|
| **EfinanceFetcher** | 0 | A股 | ❌ 免费 | 日线、实时、指数、市场统计、板块排行、所属板块 |
| **AkshareFetcher** | 1 | A股/港股 | ❌ 免费 | **能力最全**：日线、实时、指数、市场统计、**行业+概念排行**、**人气股**、**涨停池**、**筹码分布** |
| **TencentFetcher** | 5 | A股 | ❌ 免费 | 仅日线（最终兜底）+ 股票名称 |
| **TushareFetcher** | 2 → **−1** | A股/港股 | ✅ `TUSHARE_TOKEN` | 日线、实时、指数、市场统计、板块排行、筹码分布、股票列表 |
| **TickFlowFetcher** | 2 | A股 | ✅ `TICKFLOW_API_KEY` | 日线（支持批量预取）、实时、指数、市场统计、申万一级板块 |
| **PytdxFetcher** | 2 | A股 | ❌ 免费 | 日线、实时、股票名称（通达信 TCP） |
| **FutuFetcher** | 2 | 港股 | ⚠️ 无需 Key，但需本机跑 Futu OpenD | 实时、日线、财报、分红拆股、**资金流**、所属板块 |
| **BaostockFetcher** | 3 | A股 | ❌ 免费 | 日线、股票列表、股票名称 |
| **YfinanceFetcher** | 4 | A股/港/美/日/韩/台 | ❌ 免费 | 日线、实时、**六个市场的指数**（日韩台唯一来源） |
| **LongbridgeFetcher** | 5 | 港股/美股 | ✅ `LONGBRIDGE_*` | 日线、实时，**补齐量比/换手率/PE** |
| **FinnhubFetcher** | 2（写死） | 美股 | ✅ `FINNHUB_API_KEY` | 日线、实时 |
| **AlphaVantageFetcher** | 3（写死） | 美股 | ✅ `ALPHAVANTAGE_API_KEY` | 日线、实时 |
| **TwInstitutionalFetcher** | — | 台湾 | ❌ 免费（政府开放数据） | **三大法人买卖超**（台股独有） |

> ⚠️ **Tushare 会自我提升到 −1**：只要配了 `TUSHARE_TOKEN` 且 HTTP 客户端初始化成功，
> 它就排到所有源最前面（`tushare_fetcher.py:216-235`）。这是"配了 token 就用它"的设计。

### 1.3 零配置（不填任何 Key）你会得到什么

```
efinance(0) → akshare(1) → pytdx(2) → baostock(3) → yfinance(4) → tencent(5)
```

A 股日线/实时/板块/涨跌家数基本够用。**代价**：免费源受上游限流与接口变动影响，稳定性不保证
（README 原话：*"免费源受上游限流、接口变动和网络波动影响，稳定性不保证"*）。

### 1.4 能力边界（只有特定源才有的数据）

| 能力 | 谁提供 | 说明 |
|---|---|---|
| **概念题材排行** | **仅 AkShare** | 其它源都没实现这个方法 |
| **人气股 / 涨停池** | **仅 AkShare** | |
| **筹码分布** | AkShare + Tushare | 项目自注：*"该接口不稳定，云端部署建议关闭"* |
| **资金流（主力净流入）** | 仅 `AkshareFundamentalAdapter`（**仅 A 股**） | 没有任何 fetcher 实现 |
| **龙虎榜** | 仅 `AkshareFundamentalAdapter`，且**只返回标记**不是完整榜单 | |
| **量比 / 换手率 / PE** | 靠"字段兜底"补齐 | 见下 |
| **成交额（真值）** | 仅 AkShare | 其它源用 `成交量 × 收盘价` 估算 |
| **日/韩/台股** | **仅 YFinance** | |

**字段兜底机制**：`_SUPPLEMENT_FIELDS = ['volume_ratio','turnover_rate','pe_ratio','pb_ratio','total_mv','circ_mv','amplitude']`
（`base.py:2738-2742`）。主源缺这些字段时，按顺序问后续源补齐。
**Longbridge 是自己算的**（换手率 = 成交量/流通股，量比 = 当日量/5日均量）。

### 1.5 免费额度（来自代码注释，**未与厂商核实**）

| 源 | 免费额度 |
|---|---|
| Tushare 免费用户 | 80 次/分钟、500 次/天（`tushare_fetcher.py:143-149`） |
| Finnhub | 60 calls/min |
| AlphaVantage | **25 次/天**、5 次/分钟 |
| 台湾证交所 T86 | 非正式限制约 3 请求/5 秒 |

### 1.6 稳定性机制（值得知道）

- **进程级隔离**：只有 AkShare 的重调用跑在 **spawn 子进程**里，30 秒硬超时强杀（`akshare_fetcher.py:338-401`）
- **线程超时**：efinance 用 `ThreadPoolExecutor`，但**线程无法强杀**（代码自己写了这个限制）
- **熔断器**：日线（3 次失败/300 秒）、实时（3/300）、筹码（**2 次/600 秒**，更保守）
- **反封策略**：AkShare 每次请求前随机休眠 2-5 秒 + UA 轮换 + 指数退避重试

---

## 2. 基本面 / 资本市场数据源

基本面**不是 fetcher**，而是由"适配器"聚合，按市场分派：

| 适配器 | 覆盖市场 | 提供内容 | 成本 |
|---|---|---|---|
| `AkshareFundamentalAdapter` | A股 | 估值（来自实时行情）、成长、业绩预告/快报、机构持股、十大股东、**资金流**、**龙虎榜标记**、分红 | 免费 |
| `YfinanceFundamentalAdapter` | 港/美/日/韩/台 | 估值、成长、业绩、分红、所属行业板块 | 免费 |
| `FutuFundamentalAdapter` | 港股 | 公司资料、三大报表、分红拆股、资金流、所属板块 | 需本机 OpenD |

实际组装成 **7 个 block**：
`valuation / growth / earnings / institution / capital_flow / dragon_tiger / boards`

> ⚠️ **市场差异很大**：`institution` 块**只对台股有效**；`capital_flow` 与 `dragon_tiger` **仅 A 股**。
> 美股/港股这些块恒为 `not_supported`（`docs/full-guide.md:450-452`）。

---

## 3. 新闻 / 情报数据源

这是**决定「🚨 风险警报 / ✨ 利好催化」质量**的部分，也是历史上最容易踩坑的部分。

### 3.1 实时搜索 provider（`src/search_service.py`）

**共 7 家**，实际尝试顺序（代码为准）：

```
① Anspire → ② Bocha → ③ Tavily → ④ Brave → ⑤ SerpAPI → ⑥ MiniMax → ⑦ SearXNG
```

| # | Provider | env Key | 成本 | 境内直连 |
|---|---|---|---|---|
| 1 | Anspire 安思派 | `ANSPIRE_API_KEYS` | 有免费额度（**未核实**） | ✅ |
| 2 | Bocha 博查 | `BOCHA_API_KEYS` | 付费（代码内无免费声明） | ✅ |
| 3 | Tavily | `TAVILY_API_KEYS` | **约 1000 次/月**（代码注释） | ❌ 需代理 |
| 4 | Brave | `BRAVE_API_KEYS` | 代码称"免费层可用" | ❌ 需代理 |
| 5 | SerpAPI | `SERPAPI_API_KEYS` | **约 100 次/月**（代码注释） | ⚠️ 实测可直连 |
| 6 | MiniMax | `MINIMAX_API_KEYS` | 需 Coding Plan 订阅 | ✅ |
| 7 | **SearXNG** | `SEARXNG_BASE_URLS` | **自建无配额，完全免费** | ✅ 可 `127.0.0.1` |

**关键机制**

- **多 Key**：全部只支持 `*_API_KEYS` 逗号分隔形式；轮询 + 错误计数 ≥3 跳过
- **⚠️ 失效 provider 不会自动退出轮询**：`is_available` 只看"有没有配 Key"（`search_service.py:307`），
  不看是否还有额度。而 `search_comprehensive_intel` 按维度轮询且**失败不换源**。
  **⇒ 余额耗尽的渠道会持续霸占轮询位、浪费可用引擎的机会。失效就注释掉，别留着。**
- **单只个股的检索维度**：A 股 6 个（最新消息/机构分析/风险排查/公司公告/业绩预期/行业分析），
  港美股 5 个；但传统路径 `max_searches=5`，**第 6 个维度会被截断**
- **零命中也是"成功"**：`format_intel_report()` 零命中时输出「未找到相关信息」占位文本，
  所以**不能用 `news_context` 是否非空判断有没有新闻**（`empty_news.py:79` 明确点名该陷阱）
- **本地确定性打分**：`_score_news_relevance` 纯规则无 LLM —— 代码命中标题 +55、公司名命中标题 +45…

### 3.2 本地资讯池（RSS / Atom / NewsNow）—— 免费

`NEWS_INTEL_AUTO_FETCH_ENABLED=true` 一个开关即可（自动建源 + 拉取）。内置 **8 个源**：

| 源 | 类型 | 市场 |
|---|---|---|
| SEC Latest Filings | RSS | us |
| HKEX Market News | RSS | hk |
| MarketWatch Top Stories | RSS | **global** |
| 财联社热门 | NewsNow | cn |
| 雪球热门股票 | NewsNow | cn |
| 华尔街见闻快讯 | NewsNow | cn |
| 金十数据 | NewsNow | **global** |
| 格隆汇事件 | NewsNow | hk |

**⚠️ 三个限制必须知道**

1. **全部内置源 `scope_type` 都是 `market`** → 只提供**市场级**信息，**不解决个股风险排查**
2. **`market="global"` 的源没有任何消费方** —— 大盘复盘 region 与个股 market 都不可能是 `global`，
   所以 MarketWatch 与金十数据**抓了也不会进提示词**
3. **自建被 SSRF 守卫限制**：`intelligence_service.py` 拒绝 `localhost` / 内网 IP，
   NewsNow 自建**必须挂在公网域名**上

### 3.3 akshare 免费个股新闻 + 公告 —— 免费（★ 本 fork 新增）

这是目前**唯一能免费提供个股级、带日期、可分类消息面证据**的路径。

| 数据 | 接口 | 窗口 |
|---|---|---|
| 东财个股新闻 | `ak.stock_news_em` | 默认 3 天 |
| 巨潮公告 | `ak.stock_zh_a_disclosure_report_cninfo` | 默认 **90 天** |

- **免 Key、免代理**，仅 A 股；实测东财/巨潮在境内稳定可达
- **只认标题命中**（代码或公司名）—— 正文命中会大量误收"某日N只股成交额超X万"这类统计稿
- **公告按事件去重 + 风险/利好分类 + 风险优先排序**
- 开关 `AKSHARE_NEWS_ENABLED`（默认开），**fail-open 永不阻断分析**
- ⚠️ **目前只覆盖传统分析路径**；若开 `AGENT_MODE=true`，Agent 路径不加载它

### 3.4 社媒情绪 —— 免费额度 250 次/月

`SOCIAL_SENTIMENT_API_KEY` → `api.adanos.org`，提供 **Reddit / X(Twitter) / Polymarket** 情绪。
**仅对美股生效**，A 股/港股自动忽略。

### 3.5 新闻数据的完整来源路径

| # | 路径 | 喂个股仪表盘？ | 喂大盘复盘？ |
|---|---|---|---|
| 1 | 实时搜索 provider | ✅ | ✅ |
| 2 | 本地资讯池 | ✅（symbol + 同市场 market） | ✅（market scope） |
| 3 | **akshare 免费源** | ✅（**仅传统路径**） | ❌ |
| 4 | 社媒情绪 | ✅（**仅美股**） | ❌ |
| 5 | 选股候选上下文（akshare+腾讯） | ❌ 只进选股 | ❌ |
| 6 | 选股 DSA provider 搜索 | ❌ 只进选股 | ❌ |
| 7 | `news_intel` 持久化表 | ❌ 只被历史页读回 | ❌ |

---

## 4. 其他外部服务

| 类别 | 服务 | env | 用途 | 成本 |
|---|---|---|---|---|
| LLM | litellm 统一网关 | `LLM_CHANNELS` / `*_API_KEY` | 生成决策仪表盘 | 各家自付 |
| 图片识别 | Vision（Gemini/Claude/OpenAI） | `VISION_MODEL` | 从截图识别股票代码 | 各家自付 |
| 通知 | 企业微信/飞书/TG/Discord/Slack/邮件/钉钉/Pushover/ntfy/Gotify/PushPlus/ServerChan/AstrBot | 各自 webhook/token | 推送报告 | 基本免费 |
| 持仓导入 | Futu OpenD | `FUTU_OPEND_*` | `--portfolio futu` 读真实持仓 | 免费（需券商账户） |
| 股票清单 | GitHub raw | 无 | 远程更新代码/名称索引 | 免费 |

---

## 5. 各类数据分别喂给哪个流程

| 数据类别 | 自选股分析 | 大盘复盘 | 选股 | Agent 问股 |
|---|---|---|---|---|
| 行情/日线/技术指标 | ✅ | ✅ | ✅ | ✅ |
| 实时行情（含量比/换手） | ✅ | ✅ | ✅ | ✅ |
| 筹码分布 | ✅ | ❌ | ❌ | ✅ |
| 资金流 | ✅（仅A股） | ❌ | ✅ | ✅ |
| 基本面（估值/成长/业绩） | ✅ | ❌ | ✅ | ✅ |
| 龙虎榜 | ✅（仅A股，仅标记） | ❌ | ❌ | ✅ |
| 搜索结果（7 家 provider） | ✅ | ✅ | ✅ | ✅ |
| 本地资讯池 | ✅ | ✅ | ❌ | ✅ |
| **akshare 免费新闻/公告** | ✅（仅传统路径） | ❌ | ❌ | ❌ |
| 社媒情绪 | ✅（仅美股） | ❌ | ❌ | ❌ |
| 台股三大法人 | ✅（仅台股） | ❌ | ❌ | ❌ |

---

## 6. 除内置之外，还能接入什么？

### 6.1 接入方式（按难度排序）

| 方式 | 难度 | 适用 |
|---|---|---|
| **填 `.env` 的 Key** | ⭐ | 项目已支持的源 |
| **加 RSS 源** | ⭐⭐ | 任意合规 RSS/Atom：`POST /api/v1/intelligence/sources`（`source_type=rss`，`enabled: true`） |
| **自建 SearXNG** | ⭐⭐⭐ | 要无限量的网页检索（**允许 `127.0.0.1`**） |
| **自建 RSSHub** | ⭐⭐⭐ | 给没有 RSS 的站点生成 RSS |
| **改 `data_provider/` 加新 fetcher** | ⭐⭐⭐⭐ | 新行情/基本面源（需实现 `BaseFetcher` 接口 + 注册优先级） |
| **改 `search_service.py` 加新 provider** | ⭐⭐⭐⭐ | 新搜索源（实现 `BaseSearchProvider`） |

### 6.2 免费可选源（值得接）

| 类别 | 名称 | 能补什么 | 说明 |
|---|---|---|---|
| **行情** | 已有 efinance/akshare/baostock/pytdx/腾讯/新浪 | — | 免费层已经很全 |
| **行情（美股）** | **Tiingo**、**EODHD**、**Financial Modeling Prep**、**Polygon.io** | 美股日线/实时，质量与稳定性优于 yfinance | 均有免费层（**额度未核实**） |
| **宏观** | **FRED**（美联储经济数据） | 利率、CPI、失业率等宏观因子 | 免费 API Key，机构级数据 |
| **宏观** | 国家统计局 / 中国人民银行 | 国内宏观 | 免费，多为网页/无稳定 API |
| **公告** | **巨潮资讯 RSS**、交易所公告页 | 官方公告 | 可用 RSS 接入（注意合规边界） |
| **新闻** | **NewsAPI**、**Marketaux**、**GNews** | 英文财经新闻 | 有免费层 |
| **新闻** | 已有 Tavily/Brave/SerpAPI 免费层 | — | 够用 |
| **舆情** | **StockTwits**（免费 API）、**Reddit（PRAW）** | 美股散户情绪 | 免费，需自行接入 |
| **舆情** | **雪球 / 股吧** | A 股散户情绪 | **无官方 API**，抓取有合规与稳定性风险 |
| **搜索** | **自建 SearXNG** | 无限量网页检索 | **最推荐的免费方案** |
| **基本面** | **akshare 更多接口** | 股东户数、限售解禁、机构调研、行业对比 | 已装 akshare，**零新增依赖** |
| **指数/ETF** | 已有 | — | |

> 💡 **投入产出比最高的免费补充**：
> 1. **自建 SearXNG**（唯一"配置即用 + 免费 + 无配额 + 能按个股搜 + 允许 localhost"的方案）
> 2. **再挖 akshare**（已装、已用，能补解禁/股东户数/机构调研等，且免 Key）
> 3. **FRED**（想加宏观视角时，成本极低、数据权威）

### 6.3 收费可选源

| 类别 | 名称 | 能补什么 | 备注 |
|---|---|---|---|
| **行情（A股）** | **Tushare Pro 积分** | 更稳的历史行情、财务、龙虎榜明细 | 项目已支持，填 token 即自动提到最高优先级 |
| **行情（A股）** | **TickFlow** | 批量日K、实时、申万板块 | 项目已支持，能力按套餐分层 |
| **行情（港美股）** | **Longbridge** | 量比/换手/PE 兜底、实时 | 项目已支持 |
| **行情（港美股）** | Futu OpenD | 持仓、财报、资金流 | 项目已支持，需本机跑 OpenD |
| **专业终端** | **Wind / 同花顺 iFinD / 东方财富 Choice** | 全维度机构级数据 | 年费高，且**无官方开放 API**给个人开发者，通常要企业授权 |
| **量化数据** | **聚宽 JQData / 米筐 RQData / 优矿** | 因子、财务、行情 | 有付费套餐，需自行适配 |
| **行情（美股）** | Polygon.io / IEX Cloud / Bloomberg / Refinitiv | 机构级美股 | 价格梯度大 |
| **搜索** | 博查 / Anspire / MiniMax | 中文搜索、AI 摘要 | 项目已支持 |
| **新闻** | Benzinga / RavenPack / Dow Jones Newswires | 专业财经新闻与事件流 | 企业级定价 |
| **舆情** | 已有 adanos.org（含 X/Reddit/Polymarket） | 美股情绪 | 项目已支持，250 次/月免费 |

### 6.4 接入前的判断清单

新增任何数据源前，建议先问自己：

1. **它补的是哪个空缺？**（个股消息面？宏观？港美股行情？）—— 不要为了"数据多"而接
2. **失败会不会拖垮主流程？** —— 项目约定是 **fail-open**：单一源失败只记日志、不阻断分析
3. **是否与现有源重复？** —— 项目已有 12 个行情源 + 7 个搜索源，重复接入只增加维护面
4. **稳定性与合规**：优先**官方 API / 授权开放数据**；无授权直抓门户违背项目既定边界
   （`docs/intelligence-sources.md:29`：*"不做反爬、模拟登录、Cookie 抓取或非授权门户直抓"*）
5. **是否需要新依赖？** —— 能用 `akshare`（已装）解决的，不要引入新库
6. **有没有 Key 泄露风险？** —— 一律走 `.env`，不要写进代码

---

## 7. 常见坑（排查时省时间）

| 现象 | 原因 |
|---|---|
| 「未纳入新闻面证据」 | `news_result_count` = `None`（未配置搜索渠道）或 `0`（检索了但零命中）。**这个披露是准确的，不是 bug** |
| 明明配了 Key 还是没新闻 | 检查该 Key 是否额度耗尽。**失效 provider 仍占轮询位**，会挤掉可用引擎 |
| 日志里"未配置 Tushare Token" | 正常提示，会自动使用其它免费源 |
| 港股日线跳过某些源 | efinance/pytdx/baostock 不支持港股，属预期 |
| 日/韩/台股只有 YFinance | 唯一来源，YFinance 挂了就没有 |
| 概念排行永远失败 | **只有 AkShare 实现**，它挂了就没有 |
| 筹码分布经常空 | 上游接口不稳定，项目建议云端部署关闭 |
| 本地资讯池抓了没用上 | 内置源全是 `market` 级；且 `global` 源无消费方 |
| NewsNow 自建填 localhost 报错 | SSRF 守卫拒绝内网地址，必须用公网域名 |
| 行情"量比"和交易软件不一样 | 项目口径是「日线量 / 前5日均量」，不是分时量比 |

---

## 8. 本机当前启用状态

> 这一节是**本 fork 的实际配置**，与上游默认不同。

| 项 | 状态 |
|---|---|
| `.env` 中的行情源 | 免费链（efinance→akshare→…）+ `TUSHARE_TOKEN`、`TICKFLOW_API_KEY` 已配 |
| **代理** | `USE_PROXY=true`、`PROXY_PORT=7892`（Clash Party 的 **HTTP** 端口） |
| **搜索渠道** | ✅ Tavily（经代理，**已验证可用**，相关度可达 100）+ ✅ SerpAPI |
| | ❌ Anspire / Bocha 已注释（额度耗尽）；❌ Brave / SearXNG 未配 |
| **akshare 免费新闻源** | ✅ `AKSHARE_NEWS_ENABLED=true`（个股消息面主力） |
| 本地资讯池 | ❌ 未开启（且 NewsNow 公开实例被 Cloudflare 403） |
| 社媒情绪 | ❌ 未配（仅美股适用） |
| 定时任务 | ✅ `SCHEDULE_ENABLED=true`，每日 18:00；非交易日自动跳过 |
| 常驻服务 | ✅ `scripts/webui.ps1` + 计划任务 `DSA WebUI`（登录自启） |

**代理注意事项**

- Clash Party 端口：**HTTP = 7892**、SOCKS = 7891、混合 = 7890。
  项目用的是 `http://host:port` 形式，**必须填 7892 或 7890**（SOCKS 端口不适用）
- 实测分流正确：东财/巨潮/新浪/上交所经代理与直连结果一致（走直连），不受影响
- ⚠️ **`USE_PROXY=true` 后 Clash 必须保持运行**：关掉 Clash 会导致**所有**出网请求失败
  （含境内源，因为代理不可达）。临时停用请把 `USE_PROXY` 改回 `false`

---

## 附：相关文档

- `FORK_NOTES.md` —— 本 fork 的仓库拓扑、同步流程、已做改动与踩坑
- `docs/full-guide.md` —— 上游完整配置与部署指南（数据源优先级、搜索配置、Docker、云部署）
- `docs/intelligence-sources.md` —— 本地资讯池（RSS/NewsNow）专项说明
- `docs/market-support.md` —— 各市场数据能力边界
- `docs/data-source-stability.md` —— 数据源稳定性与降级语义
- `.claude/reviews/design-akshare-free-news.md` —— akshare 免费新闻源的设计与实现偏差记录
