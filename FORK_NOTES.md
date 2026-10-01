# FORK_NOTES.md — 本 fork 的本地开发背景与约定

> **与 `AGENTS.md` 的关系**：`AGENTS.md` 仍然是**上游项目规则的唯一真源**，本文件不复制、不替代它。
> 本文件只补充**这个 fork 特有的**背景、约定、已做改动和踩过的坑，供 AI 助手和未来的自己参考。
> 两者冲突时，**以 `AGENTS.md` 为准**（上游规则优先）；本文件只在其之上追加 fork 本地事实。
>
> 维护约定：本文件是新增文件，上游不存在 → 修改它**永不产生 rebase 冲突**。
> 因此 fork 特有的内容都应写在这里，而不是写进 `AGENTS.md`。

---

## 1. 仓库拓扑

```
origin    https://github.com/tyt2008cn/daily_stock_analysis.git      ← 我的 fork（备份用）
upstream  https://github.com/ZhuLinsen/daily_stock_analysis.git      ← 原项目（只读参照）

main          ← 上游的纯净镜像，永不直接提交，跟踪 upstream/main
local/custom  ← 本 fork 的常驻工作分支，所有本地改动都在这里
```

## 2. 维护路线：路线 B（本地长期维护）

不向上游提 PR，改动只留在本地分支。因此**最小化 rebase 冲突面是首要约束**。

### 日常同步流程

```powershell
# 每次开工前
git fetch upstream --prune
git switch main
git merge --ff-only upstream/main      # main 保持镜像（pull.ff=only 已配置，防止误产生 merge commit）
git switch local/custom
git rebase main                        # 把本地改动重放到最新上游之上
```

### 三条纪律

1. **`main` 永不提交** —— 它只是上游的镜子。
2. **所有改动在 `local/custom`**，且**一个功能压成一个 commit**（便于 `git revert` 精确回滚）。
3. **用 `rebase` 吸收上游，不用 `merge`** —— 保证本地改动永远是"挂在最新上游上的几个干净提交"。

### ⭐ 最重要的一条工程纪律：别碰巨型文件

上游开发活跃，这三个文件又大又常改：

| 文件 | 大小 | 风险 |
|---|---|---|
| `src/analyzer.py` | ~257 KB | 几乎每周都改 |
| `src/core/pipeline.py` | ~204 KB | 同上 |
| `src/search_service.py` | ~199 KB | 同上 |

**在这三个文件里插入大段代码 = 每次 rebase 都可能冲突，且要在几千行里找自己那几行。**

✅ **正确做法：新增独立文件 + 最小挂载点。**
已有的正面例子：akshare 新闻源只改了 `pipeline.py` **18 行**（3 个挂载点），其余全在新文件里。

---

## 3. 已做的本地改动

### 3.1 `feat: add free akshare per-stock news and announcement source`

- **commit**：`1f782be6`
- **目的**：让报告中「🚨 风险警报 / ✨ 利好催化」两节在**没有任何付费搜索渠道**时仍有真实、带日期的证据。
- **新增**：`src/services/akshare_news_source.py`、`tests/test_akshare_news_source.py`
- **修改**：`src/core/pipeline.py`（+18 行）、`.env.example`（+11 行）
- **数据源**（都免 Key、免代理，仅 A 股）：
  - `ak.stock_news_em` —— 东方财富个股新闻
  - `ak.stock_zh_a_disclosure_report_cninfo` —— 巨潮资讯公告
- **开关注入**：`src/core/pipeline.py` 在既有本地资讯池（`_load_persisted_intelligence_context`）之后
  调用 `load_akshare_news_context()`，并登记进 `news_evidence_present()`。
- **配置**：`AKSHARE_NEWS_ENABLED`（默认 true），窗口/条数可覆盖，见 `.env.example`
- **软回滚**：`.env` 设 `AKSHARE_NEWS_ENABLED=false`
- **硬回滚**：`git revert 1f782be6`
- **设计文档**：`.claude/reviews/design-akshare-free-news.md`（含实现偏差记录）

### 3.2 `scripts/webui.ps1` + 登录自启计划任务

- **目的**：把"手动开一个终端跑 WebUI"变成可控的常驻服务。
- **常驻模式**：`main.py --serve` + `SCHEDULE_ENABLED=true`
  → 走 **"Web/API runtime scheduler"** 分支：**WebUI 与每日定时任务同进程**，且**启动时不跑一次性分析**。
  （若 `SCHEDULE_ENABLED=false`，`--serve` 会落到"模式3"并在启动时跑一次完整分析 —— 注意区别。）
- **用法**：
  ```powershell
  .\scripts\webui.ps1 start     # 后台启动（脱离终端）
  .\scripts\webui.ps1 stop      # 停止（进程树 kill）
  .\scripts\webui.ps1 restart
  .\scripts\webui.ps1 status    # PID / 端口 / 健康检查
  .\scripts\webui.ps1 logs      # 跟踪 stdout+stderr
  ```
- **计划任务**：名为 `DSA WebUI`，登录后延迟 30s 触发，无执行时限，`MultipleInstances=IgnoreNew`。
  查看/排查：
  ```powershell
  Get-ScheduledTaskInfo -TaskName 'DSA WebUI'     # LastTaskResult
  Get-Content logs\webui.control.log              # 脚本自身的操作审计
  ```
- **日志**：`logs/webui.control.log`（脚本操作）、`logs/webui.out.log` / `webui.err.log`（应用控制台输出）

---

## 4. 踩过的坑（Windows 特有，重要）

1. **venv 的 `python.exe` 是启动器，会再派生真正的解释器**。
   实测进程链是 **三层**：`python.exe(launcher) → python.exe → python.exe(真正服务)`。
   **只杀 launcher 会留下服务在跑** → 停止必须用**进程树 kill**（`taskkill /PID <pid> /T /F`）。
2. **重定向的控制台输出会中文乱码**。Windows 默认代码页（CP936）导致。
   必须在启动子进程前设 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`
   （`docs/full-guide.md` 对手动运行也给了同样建议）。`scripts/webui.ps1` 已内置。
3. **端口占用检查必须校验命令行为**。脚本在 kill 端口占用者前会确认其命令行含 `main.py`，
   避免误杀无关进程。
4. **`Start-Process -RedirectStandardOutput` 启动的常驻进程会被"等待整棵进程树"的包装器当作未结束**。
   这是调用方（如某些作业调度器）的行为，不是脚本缺陷 —— 脚本本身会正常返回（实测 exit 0，
   且应用继续存活）。在普通终端里提示符会立即返回。
5. **`CLAUDE.md` 在 Windows 上会被 git 检出成普通文件**（`core.symlinks` 默认 `false`），
   于是 `python scripts/check_ai_assets.py` 报
   `ERROR: CLAUDE.md must be a symlink to AGENTS.md`。
   **这是检出问题，不是仓库问题** —— git 里记录的 mode 是 `120000`（symlink），
   磁盘上却是个写着 `AGENTS.md` 的 9 字节普通文件。
   本仓库已修复并设 `core.symlinks=true`；修复方式：
   ```powershell
   git config core.symlinks true
   Remove-Item CLAUDE.md
   git checkout -- CLAUDE.md
   ```
   修复后该检查输出 `[ai-assets] OK`。
   > 提醒：改动 `AGENTS.md` 等 AI 协作治理资产后，按 §2 必须跑一次这个检查。

---

## 5. 当前环境约束与已知状态

| 项 | 状态 | 说明 |
|---|---|---|
| Docker | **未安装** | 所有方案都用非 Docker 方式 |
| 代理 | **10809 无进程监听** | `USE_PROXY=false`；开启前必须先启动代理软件 |
| Python | `.venv`（3.12） | 直接用 `.venv\Scripts\python.exe`，无需激活 |
| `pytest` / `flake8` | 已装进 venv | `AGENTS.md` 要求的验证工具 |

### 搜索渠道现状（重要）

| Provider | 状态 |
|---|---|
| Anspire | **已停用**（余额 0），`.env` 中已注释并注明原因 |
| Bocha | **已停用**（余额不足），同上 |
| Tavily | **已停用**（本机直连被重置 `ConnectionResetError 10054`），同上 |
| SerpAPI | 启用中，但免费额度约 100/月，**仅作补充** |
| **akshare 免费源** | ✅ **已启用**，是本 fork 消息面证据的主力 |

**关键教训：失效的 provider 不会自动退出轮询。**
`BaseSearchProvider.is_available` 只看"有没有配 Key"（`src/search_service.py:307`），
不看 Key 是否还有额度；而 `search_comprehensive_intel` 按**维度轮询** provider 且**失败不换源**。
所以一个余额为 0 的 provider 会持续霸占轮询位、浪费其他可用引擎的机会。
**结论：渠道失效时应当直接注释掉，而不是留在配置里。**

---

## 6. 上游值得注意的行为（排查时省时间）

- **新闻检索三态**：`news_result_count` = `None` 未执行检索 / `0` 执行了但零命中 / `>0` 正常。
  报告会据此渲染不同披露文案（`src/services/empty_news.py`）。
  **披露是准确的**，看到"未纳入新闻面证据"说明确实没拿到，不是 bug。
- **`market="global"` 的资讯源没有消费方**。本地资讯池读取时只按 `market=<自身市场>` 过滤
  （`pipeline.py`、`market_analyzer.py`），因此 MarketWatch / 金十数据抓了也进不了提示词。
- **本地资讯池自建被 SSRF 守卫限制**：`src/services/intelligence_service.py` 拒绝
  `localhost` / 内网 IP（`_is_blocked_ip`）。所以 NewsNow 自建**必须挂在公网域名**上。
  而 SearXNG 没有这个限制（`src/config.py:1710` 只校验 scheme+netloc）→ **允许 `http://127.0.0.1:8080`**。
- **`docs/intelligence-sources.md` 里的 NewsNow 仓库链接是 404**（写成 `qqhann/newsnow`），
  真实仓库是 `ourongxing/newsnow`。

---

## 7. 上游更新后要做什么（检查清单）

```powershell
git fetch upstream --prune
git switch main; git merge --ff-only upstream/main
git switch local/custom; git rebase main
```

rebase 后：

1. `git status` / `git log --oneline -5` 确认本地 commit 仍在
2. 确认三个挂载点没被上游改写（`src/core/pipeline.py`）：
   - `from src.services.akshare_news_source import load_akshare_news_context`
   - `akshare_news_context = load_akshare_news_context(...)`
   - `news_evidence_present(..., akshare_news_context,)`
3. `python -m py_compile src/core/pipeline.py src/services/akshare_news_source.py`
4. `python -m pytest tests/test_akshare_news_source.py -m "not network" -q`
5. **若上游改了 `news_context` 的组装方式或 `news_evidence_present` 的签名**，
   挂载点可能失效 —— 这是最需要复查的地方（也是当初刻意只留 3 个挂载点的原因）
