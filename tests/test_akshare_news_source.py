# -*- coding: utf-8 -*-
"""akshare 免费个股新闻 / 公告源的单元测试与网络冒烟测试。

离线用例覆盖纯逻辑（相关性判据 / 公告分类与去重 / 时间解析 / 短路分支）；
真实取数用 @pytest.mark.network 隔离，便于 `pytest -m "not network"` 跳过。
"""

import os
import sys
from datetime import datetime, timedelta

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.services.akshare_news_source import (  # noqa: E402
    _announcement_subject_key,
    _classify_announcement,
    _dedupe_records,
    _extract_symbol,
    _is_relevant,
    _parse_datetime,
    _within_window,
    load_akshare_news_context,
)


# --------------------------------------------------------------------------
# 代码归一化
# --------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("600519", "600519"),
        ("sh600519", "600519"),
        ("600519.SH", "600519"),
        ("sz300750", "300750"),
        ("AAPL", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_symbol(raw, expected):
    assert _extract_symbol(raw) == expected


# --------------------------------------------------------------------------
# 相关性判据（核心回归点）
# --------------------------------------------------------------------------
@pytest.mark.unit
def test_relevance_accepts_company_name_in_title():
    record = {"title": "宁德时代，再推员工持股计划", "summary": "……"}
    assert _is_relevant(record, "300750", "宁德时代") is True


@pytest.mark.unit
def test_relevance_accepts_code_in_title():
    record = {"title": "300750 拟回购公司股份", "summary": "……"}
    assert _is_relevant(record, "300750", "宁德时代") is True


@pytest.mark.unit
def test_relevance_rejects_body_only_hit():
    """回归用例：东财统计稿只在正文表格里列出股票代码，不应被当作该公司新闻。

    实测中「9月30日18只股盘后交易额超500万元」这类稿件正文含大量股票代码，
    如果判据看正文，会给 LLM 灌入噪音并虚增 news_result_count。
    """
    record = {
        "title": "9月30日18只股盘后交易额超500万元",
        "summary": "002487 大金重工 46.29 600519 贵州茅台 1258.62 300750 宁德时代",
    }
    assert _is_relevant(record, "600519", "贵州茅台") is False


# --------------------------------------------------------------------------
# 公告分类
# --------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "title,expected",
    [
        ("关于股东减持股份的公告", "risk"),
        # 解禁类公告的典型标题不含"解禁"字面，靠"限售股/上市流通"命中
        ("关于部分限售股上市流通的公告", "risk"),
        ("关于股东部分股份解除质押的公告", "risk"),
        ("关于回购公司A股股份的进展公告", "catalyst"),
        ("关于2026年A股第二期员工持股计划草案的公告", "catalyst"),
        ("2026年半年度报告", None),
    ],
)
def test_classify_announcement(title, expected):
    assert _classify_announcement(title) == expected


# --------------------------------------------------------------------------
# 公告同事件折叠
# --------------------------------------------------------------------------
@pytest.mark.unit
def test_announcement_subject_key_collapses_procedural_filings():
    """同一公司行为的程序性文件（法律意见书/核查意见/合规性说明）应折叠为一类。"""
    titles = [
        "上海市通力律师事务所关于宁德时代新能源科技股份有限公司2026年A股第二期员工持股计划相关事宜的法律意见书",
        "董事会关于2026年A股第二期员工持股计划草案合规性说明",
        "董事会薪酬与考核委员会关于2026年A股第二期员工持股计划相关事项的核查意见",
        "《2026年A股第二期员工持股计划管理办法》",
    ]
    keys = [_announcement_subject_key(t) for t in titles]
    assert keys[0] == keys[1] == keys[2], keys
    assert keys[3] != keys[0], keys


@pytest.mark.unit
def test_dedupe_records_collapses_identical_summaries():
    """同一事件被多家媒体重复收录时保留一条。"""
    records = [
        {"title": "宁德时代，再推员工持股计划", "summary": "公司发布员工持股计划草案……"},
        {"title": "宁德时代：拟推2026年第二期员工持股计划", "summary": "公司发布员工持股计划草案……"},
        {"title": "关于回购公司股份的进展公告", "summary": "回购进展情况……"},
    ]
    assert len(_dedupe_records(records)) == 2


# --------------------------------------------------------------------------
# 时间解析与窗口
# --------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected_year",
    [
        ("2026-09-30 16:23:00", 2026),
        ("2026-09-30", 2026),
        (datetime(2026, 9, 30), 2026),
    ],
)
def test_parse_datetime(raw, expected_year):
    parsed = _parse_datetime(raw)
    assert parsed is not None and parsed.year == expected_year


@pytest.mark.unit
@pytest.mark.parametrize("raw", [None, "", "nan", "NaT", "-"])
def test_parse_datetime_invalid(raw):
    assert _parse_datetime(raw) is None


@pytest.mark.unit
def test_within_window_requires_parseable_date():
    """无日期的条目必须排除——prompt 强制要求每条都带具体日期。"""
    assert _within_window({"published_at": None}, 3) is False
    assert _within_window({"published_at": datetime.now() - timedelta(days=1)}, 3) is True
    assert _within_window({"published_at": datetime.now() - timedelta(days=10)}, 3) is False


# --------------------------------------------------------------------------
# 短路分支（无需网络）
# --------------------------------------------------------------------------
@pytest.mark.unit
def test_non_cn_market_returns_none():
    assert load_akshare_news_context("AAPL", "Apple", market="us") is None


@pytest.mark.unit
def test_index_returns_none():
    assert load_akshare_news_context("sh000016", "上证50", market="cn", is_index=True) is None


@pytest.mark.unit
def test_disabled_switch_returns_none(monkeypatch):
    monkeypatch.setenv("AKSHARE_NEWS_ENABLED", "false")
    assert load_akshare_news_context("600519", "贵州茅台", market="cn") is None


# --------------------------------------------------------------------------
# 真实网络冒烟测试
# --------------------------------------------------------------------------
@pytest.mark.network
def test_network_real_fetch_returns_context():
    """真实调用：必须是真数据，不能只靠 mock 证明实现通过。"""
    out = load_akshare_news_context("300750", "宁德时代", market="cn")
    if out is None:
        pytest.skip("akshare 上游暂不可达或无近窗口数据（外部依赖，不视为失败）")
    assert "免费资讯补充" in out
    assert "宁德时代" in out
