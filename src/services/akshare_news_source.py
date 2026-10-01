# -*- coding: utf-8 -*-
"""akshare 免费个股新闻 / 公告源（免 API Key、免代理，仅 A 股）。

用途：为自选股分析补充确定性的消息面证据，使「🚨 风险警报 / ✨ 利好催化」
两节在没有任何付费搜索渠道时依然有真实内容。

设计约束（详见 .claude/reviews/design-akshare-free-news.md）：
- **fail-open**：任何异常一律返回 None，绝不阻断主分析链路。
- **最小冲突面**：不改 search_service / analyzer / config，自带精简相关性判据。
  仅新增本文件 + pipeline 少量挂载点，降低与上游 rebase 的冲突概率。
- **仅 A 股**：两个数据源都只覆盖沪深京，其他市场直接返回 None。

数据来源：
- 东方财富个股新闻  ``ak.stock_news_em``
- 巨潮资讯公告      ``ak.stock_zh_a_disclosure_report_cninfo``
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 配置：用 os.getenv 直读，避免修改 src/config.py（压缩 rebase 冲突面）
# --------------------------------------------------------------------------
DEFAULT_NEWS_DAYS = 3
# 实测：45 天对大盘股偏窄——贵州茅台 45 天内 0 条公告，放宽到 90 天才覆盖到
# 半年报 / 会计政策变更 / 董事会决议 / 重大事项公告。公告数量由 limit + 去重 + 风险优先兜住。
DEFAULT_ANNOUNCEMENT_DAYS = 90
DEFAULT_NEWS_LIMIT = 8
DEFAULT_ANNOUNCEMENT_LIMIT = 10
_CACHE_TTL_NEWS_SEC = 1800  # 30 分钟
_CACHE_TTL_ANNOUNCEMENT_SEC = 6 * 3600  # 6 小时（公告变动慢）

# 公告标题关键词分类（确定性，只影响分组与提示，不改变事实）
_RISK_KEYWORDS = (
    "减持", "解禁", "限售股", "上市流通", "质押", "诉讼", "仲裁", "处罚", "问询",
    "违规", "退市", "风险警示", "预亏", "减值", "担保", "冻结", "立案", "监管",
    "谴责", "警示函", "终止上市", "被冻结",
)
_CATALYST_KEYWORDS = (
    "回购", "增持", "股权激励", "员工持股", "中标", "合同", "预增", "分红",
    "并购", "重组", "专利", "获批", "投产", "涨价", "订单",
)

_SIX_DIGIT_RE = re.compile(r"\d{6}")


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _is_enabled() -> bool:
    return _env_bool("AKSHARE_NEWS_ENABLED", True)


# --------------------------------------------------------------------------
# 内存 TTL 缓存
# 项目既有的 WebUI / 任务队列是长驻进程，内存缓存即可覆盖"同一天多次运行"；
# 独立 CLI 进程之间不共享，属可接受取舍（换取零磁盘失败面）。
# --------------------------------------------------------------------------
_CACHE: Dict[str, Tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: str) -> Optional[Any]:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if not hit:
            return None
        expires_at, value = hit
        if expires_at < now:
            _CACHE.pop(key, None)
            return None
        return value


def _cache_put(key: str, value: Any, ttl: int) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + ttl, value)
        if len(_CACHE) > 256:  # 简单容量保护
            for stale in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:64]:
                _CACHE.pop(stale, None)


# --------------------------------------------------------------------------
# akshare 进度条抑制
# ak.stock_zh_a_disclosure_report_cninfo 内部调用 get_tqdm()（默认 enable=True），
# 会向控制台打 tqdm 进度条污染日志。该函数未暴露关闭参数，故一次性替换其模块全局。
# 纯美化：失败无任何功能影响。
# --------------------------------------------------------------------------
_TQDM_PATCHED = False


def _silence_akshare_progress_bars() -> None:
    global _TQDM_PATCHED
    if _TQDM_PATCHED:
        return
    _TQDM_PATCHED = True
    try:
        import akshare as ak

        target = getattr(ak, "stock_zh_a_disclosure_report_cninfo", None)
        module_globals = getattr(target, "__globals__", None)
        if isinstance(module_globals, dict) and "get_tqdm" in module_globals:
            module_globals["get_tqdm"] = lambda enable=True: (
                lambda iterable, *args, **kwargs: iterable
            )
    except Exception:  # 美化失败不影响功能
        pass


# --------------------------------------------------------------------------
# 解析工具
# --------------------------------------------------------------------------
def _extract_symbol(code: str) -> Optional[str]:
    """从任意股票代码形态中取出 6 位 A 股代码。"""
    if not code:
        return None
    match = _SIX_DIGIT_RE.search(str(code))
    return match.group(0) if match else None


def _parse_datetime(value: Any) -> Optional[datetime]:
    """尽量把 akshare 返回的时间字段解析成 datetime；失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    # pandas.Timestamp
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            return to_pydatetime()
        except Exception:
            pass
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "-"}:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    try:  # ISO 兜底
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _clean(value: Any, limit: int = 400) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat", "-"}:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


# --------------------------------------------------------------------------
# 取数
# --------------------------------------------------------------------------
def _fetch_news_records(symbol: str) -> List[Dict[str, Any]]:
    cache_key = f"news:{symbol}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    import akshare as ak

    frame = ak.stock_news_em(symbol=symbol)
    records: List[Dict[str, Any]] = []
    if frame is None or getattr(frame, "empty", True):
        _cache_put(cache_key, records, _CACHE_TTL_NEWS_SEC)
        return records

    for row in frame.to_dict("records"):
        title = _clean(row.get("新闻标题"), 200)
        if not title:
            continue
        records.append(
            {
                "title": title,
                "summary": _clean(row.get("新闻内容"), 300),
                "published_at": _parse_datetime(row.get("发布时间")),
                "source": _clean(row.get("文章来源"), 40),
                "url": _clean(row.get("新闻链接"), 300),
            }
        )
    _cache_put(cache_key, records, _CACHE_TTL_NEWS_SEC)
    return records


def _fetch_announcement_records(symbol: str, days: int) -> List[Dict[str, Any]]:
    cache_key = f"ann:{symbol}:{days}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    import akshare as ak

    end = datetime.now()
    start = end - timedelta(days=days)
    frame = ak.stock_zh_a_disclosure_report_cninfo(
        symbol=symbol,
        market="沪深京",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
    )
    records: List[Dict[str, Any]] = []
    if frame is None or getattr(frame, "empty", True):
        _cache_put(cache_key, records, _CACHE_TTL_ANNOUNCEMENT_SEC)
        return records

    for row in frame.to_dict("records"):
        title = _clean(row.get("公告标题"), 200)
        if not title:
            continue
        records.append(
            {
                "title": title,
                "published_at": _parse_datetime(row.get("公告时间")),
                "url": _clean(row.get("公告链接"), 300),
            }
        )
    _cache_put(cache_key, records, _CACHE_TTL_ANNOUNCEMENT_SEC)
    return records


# --------------------------------------------------------------------------
# 过滤与分类
# --------------------------------------------------------------------------
def _is_relevant(record: Dict[str, Any], symbol: str, stock_name: str) -> bool:
    """精简相关性判据：代码或公司名必须出现在**标题**中。

    实测教训：只看标题+正文会大量误收。东财的「9月30日18只股盘后交易额超500万元」
    「食品饮料行业今日涨1.68%」这类统计稿，正文里成串列出股票代码，
    正文命中完全不等于"这条新闻讲的是这家公司"。

    消息面证据宁缺勿滥——噪音会被 LLM 当作事实依据写进风险/催化结论。
    因此这里刻意只认标题，牺牲召回率换准确率。
    """
    title = str(record.get("title") or "")
    if symbol and symbol in title:
        return True
    name = (stock_name or "").strip()
    if name and name in title:
        return True
    return False


def _dedupe_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按摘要/标题归一化去重。

    同一事件常被多家媒体重复收录（如 300750 的员工持股计划同时出现在
    科创日报与财联社），摘要往往逐字相同，保留一条即可。
    """
    seen = set()
    result: List[Dict[str, Any]] = []
    for record in records:
        basis = str(record.get("summary") or "")[:60] or str(record.get("title") or "")
        key = re.sub(r"[\s\W_]+", "", basis)[:50]
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def _classify_announcement(title: str) -> Optional[str]:
    for keyword in _RISK_KEYWORDS:
        if keyword in title:
            return "risk"
    for keyword in _CATALYST_KEYWORDS:
        if keyword in title:
            return "catalyst"
    return None


# 公告标题里的程序性噪音词：一次公司行为会衍生多份程序文件
# （法律意见书 / 核查意见 / 合规性说明 …），对风控判断没有增量信息。
_ANNOUNCEMENT_NOISE_TOKENS = (
    "法律意见书", "核查意见", "合规性说明", "独立意见", "审核意见", "监事会意见",
    "薪酬与考核委员会", "审计委员会", "战略委员会", "提名委员会",
    "律师事务所", "会计师事务所", "提示性公告", "公告", "说明",
    "关于", "相关事宜", "相关事项", "事项", "草案", "摘要", "全文", "的",
)


def _announcement_subject_key(title: str) -> str:
    """提取公告"事件"归一化键，用于折叠同一事件的程序性文件。

    中文公告的习惯是**把事件名放在标题末尾**（"……关于X的核查意见"），
    程序性文件的前缀千差万别但尾巴一致，因此取噪音剔除后的**尾部**做键，
    比取前缀更能折叠同一事件（实测可把员工持股计划的 3 份文件折成 1 份）。
    """
    text = title
    for token in _ANNOUNCEMENT_NOISE_TOKENS:
        text = text.replace(token, "")
    text = re.sub(r"[\s\W_]+", "", text)
    if not text:
        text = re.sub(r"[\s\W_]+", "", title)
    return text[-16:]


def _dedupe_announcements(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result: List[Dict[str, Any]] = []
    for record in records:
        key = _announcement_subject_key(str(record.get("title") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def _within_window(record: Dict[str, Any], days: int) -> bool:
    published = record.get("published_at")
    if published is None:
        return False  # 无日期无法满足"必须带具体日期"的 prompt 约束
    return published >= datetime.now() - timedelta(days=days)


def _format_date(record: Dict[str, Any]) -> str:
    published = record.get("published_at")
    return published.strftime("%Y-%m-%d") if published else "日期未知"


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _render(
    stock_name: str,
    symbol: str,
    news: List[Dict[str, Any]],
    announcements: List[Dict[str, Any]],
    *,
    news_days: int,
    announcement_days: int,
) -> Optional[str]:
    if not news and not announcements:
        return None

    lines: List[str] = [
        f"## 免费资讯补充（akshare：东财个股新闻 / 巨潮公告）—— {stock_name}({symbol})",
        "",
        "> 说明：以下为代码直取的公开数据，未经过搜索引擎。"
        f"新闻窗口为近 {news_days} 日，公告窗口为近 {announcement_days} 日。",
        "",
    ]

    if news:
        lines.append(f"### 个股新闻（近{news_days}日，共 {len(news)} 条）")
        for index, record in enumerate(news, start=1):
            source = record.get("source") or "来源未标明"
            lines.append(f"{index}. {record['title']}（{source} / {_format_date(record)}）")
            if record.get("summary"):
                lines.append(f"   摘要：{record['summary']}")
            if record.get("url"):
                lines.append(f"   链接：{record['url']}")
        lines.append("")

    if announcements:
        risk = [r for r in announcements if r.get("_class") == "risk"]
        catalyst = [r for r in announcements if r.get("_class") == "catalyst"]
        other = [r for r in announcements if not r.get("_class")]

        lines.append(f"### 公司公告（近{announcement_days}日，共 {len(announcements)} 条）")

        def _emit(label: str, items: List[Dict[str, Any]]) -> None:
            if not items:
                return
            lines.append(f"{label}：")
            for record in items:
                lines.append(f"- {record['title']}（{_format_date(record)}）")
                if record.get("url"):
                    lines.append(f"  链接：{record['url']}")

        _emit("【风险类公告】", risk)
        _emit("【利好类公告】", catalyst)
        _emit("【其他公告】", other)
        lines.append("")

    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# 公开 API
# --------------------------------------------------------------------------
def load_akshare_news_context(
    code: str,
    stock_name: str,
    *,
    market: str = "cn",
    is_index: bool = False,
    news_days: Optional[int] = None,
    announcement_days: Optional[int] = None,
    news_limit: Optional[int] = None,
    announcement_limit: Optional[int] = None,
) -> Optional[str]:
    """返回可拼进 ``news_context`` 的 Markdown 块；无数据/不适用时返回 None。

    本函数**永不抛异常**：任何失败都记日志并返回 None，保证不影响主分析链路。
    """
    try:
        if not _is_enabled():
            return None
        if (market or "cn").lower() != "cn":
            return None  # 两个数据源都只覆盖 A 股
        if is_index:
            return None  # 指数没有个股公告语义

        symbol = _extract_symbol(code)
        if not symbol:
            return None

        news_days = news_days or _env_int("AKSHARE_NEWS_NEWS_DAYS", DEFAULT_NEWS_DAYS)
        announcement_days = announcement_days or _env_int(
            "AKSHARE_NEWS_ANNOUNCEMENT_DAYS", DEFAULT_ANNOUNCEMENT_DAYS
        )
        news_limit = news_limit or _env_int("AKSHARE_NEWS_NEWS_LIMIT", DEFAULT_NEWS_LIMIT)
        announcement_limit = announcement_limit or _env_int(
            "AKSHARE_NEWS_ANNOUNCEMENT_LIMIT", DEFAULT_ANNOUNCEMENT_LIMIT
        )

        try:
            _silence_akshare_progress_bars()
            raw_news = _fetch_news_records(symbol)
        except Exception as exc:
            logger.warning("akshare 个股新闻获取失败（fail-open）: %s", exc)
            raw_news = []

        try:
            _silence_akshare_progress_bars()
            raw_announcements = _fetch_announcement_records(symbol, announcement_days)
        except Exception as exc:
            logger.warning("akshare 公告获取失败（fail-open）: %s", exc)
            raw_announcements = []

        news = _dedupe_records(
            [
                record
                for record in raw_news
                if _within_window(record, news_days)
                and _is_relevant(record, symbol, stock_name)
            ]
        )[:news_limit]

        announcements: List[Dict[str, Any]] = []
        for record in raw_announcements:
            if not _within_window(record, announcement_days):
                continue
            record = dict(record)
            record["_class"] = _classify_announcement(record.get("title", ""))
            announcements.append(record)

        # 折叠同一事件的程序性文件，并在截断前把风险类排在前面，
        # 避免有条数上限时把真正该看的减持/解禁公告挤掉。
        announcements = _dedupe_announcements(announcements)
        _class_priority = {"risk": 0, "catalyst": 1}
        announcements.sort(key=lambda item: _class_priority.get(item.get("_class"), 2))
        announcements = announcements[:announcement_limit]

        rendered = _render(
            stock_name,
            symbol,
            news,
            announcements,
            news_days=news_days,
            announcement_days=announcement_days,
        )
        if rendered:
            logger.info(
                "akshare 免费资讯已加载: %s(%s) 新闻=%d 公告=%d",
                stock_name,
                symbol,
                len(news),
                len(announcements),
            )
        return rendered

    except Exception as exc:
        # 只有真正意外的错误才会走到这里（例如 pandas 结构变化）
        logger.warning("akshare 免费资讯加载失败（fail-open）: %s", exc)
        return None
