# -*- coding: utf-8 -*-
"""search_comprehensive_intel 的 provider 回退回归测试。

背景：该函数此前每个维度只调用一次搜索引擎，失败即作废。于是一个失效渠道
（欠费、网络不通）就会永久丢掉一个维度，并让可用引擎只拿到 1/N 的机会。
本测试锁定修复后的行为：失败时按轮转起点顺序回退，直到拿到可用结果。

全部使用桩 provider，不产生任何网络请求。
"""

import os
import sys
from datetime import datetime

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.search_service import (  # noqa: E402
    SearchResponse,
    SearchResult,
    SearchService,
)


class _StubProvider:
    """最小 provider 桩：只需 name / is_available / search。"""

    def __init__(self, name, responses):
        self.name = name
        self._responses = list(responses)
        self.calls = 0

    @property
    def is_available(self):
        return True

    def search(self, query, max_results=5, days=7, **kwargs):
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return SearchResponse(
            query=query, results=[], provider=self.name, success=False,
            error_message="stub exhausted",
        )


def _ok_response(provider_name, code="300750", name="宁德时代"):
    """构造一条能通过过滤/排序/准入的直连个股新闻。"""
    today = datetime.now().strftime("%Y-%m-%d")
    return SearchResponse(
        query="q",
        results=[
            SearchResult(
                title=f"{name}({code}) 测试消息",
                snippet=f"{name} {code} 公司发布重要公告",
                url=f"https://example.com/{code}/news",
                source="测试来源",
                published_date=today,
            )
        ],
        provider=provider_name,
        success=True,
    )


def _failed_response(provider_name, message="可用余额不足"):
    return SearchResponse(
        query="q", results=[], provider=provider_name,
        success=False, error_message=message,
    )


def _service_with(providers):
    svc = SearchService()
    svc._providers = list(providers)  # 只保留桩，避免任何真实网络调用
    return svc


@pytest.mark.unit
def test_falls_back_to_next_provider_when_first_fails():
    """核心回归：首个引擎失败时，必须回退到下一个并拿到结果。"""
    broken = _StubProvider("BrokenSearch", [_failed_response("BrokenSearch")])
    working = _StubProvider("WorkingSearch", [_ok_response("WorkingSearch")])

    svc = _service_with([broken, working])
    results = svc.search_comprehensive_intel(
        stock_code="300750", stock_name="宁德时代", max_searches=1
    )

    assert broken.calls == 1, "失效引擎应被尝试一次"
    assert working.calls == 1, "应回退到下一个引擎"
    assert "latest_news" in results

    served = results["latest_news"]
    assert served.success is True
    assert served.provider == "WorkingSearch", "结果应来自回退后的引擎"
    assert served.results, "回退后必须拿到非空结果"


@pytest.mark.unit
def test_falls_back_when_provider_returns_zero_results():
    """成功但零结果的引擎也应触发回退（例如公告维度经常 0 条）。"""
    empty = _StubProvider(
        "EmptySearch",
        [SearchResponse(query="q", results=[], provider="EmptySearch", success=True)],
    )
    working = _StubProvider("WorkingSearch", [_ok_response("WorkingSearch")])

    svc = _service_with([empty, working])
    results = svc.search_comprehensive_intel(
        stock_code="300750", stock_name="宁德时代", max_searches=1
    )

    assert empty.calls == 1
    assert working.calls == 1
    assert results["latest_news"].provider == "WorkingSearch"
    assert results["latest_news"].results


@pytest.mark.unit
def test_does_not_call_extra_providers_after_success():
    """首个引擎成功时应立即停止，不浪费其它渠道的额度。"""
    working = _StubProvider("WorkingSearch", [_ok_response("WorkingSearch")])
    spare = _StubProvider("SpareSearch", [_ok_response("SpareSearch")])

    svc = _service_with([working, spare])
    results = svc.search_comprehensive_intel(
        stock_code="300750", stock_name="宁德时代", max_searches=1
    )

    assert working.calls == 1
    assert spare.calls == 0, "已拿到结果就不应继续消耗其它引擎额度"
    assert results["latest_news"].provider == "WorkingSearch"


@pytest.mark.unit
def test_all_providers_failing_is_fail_open():
    """全部引擎失败时不得抛异常，并保留最后一次失败响应用于说明原因。"""
    first = _StubProvider("FirstSearch", [_failed_response("FirstSearch", "余额不足")])
    second = _StubProvider("SecondSearch", [_failed_response("SecondSearch", "连接被重置")])

    svc = _service_with([first, second])
    results = svc.search_comprehensive_intel(
        stock_code="300750", stock_name="宁德时代", max_searches=1
    )

    assert first.calls == 1 and second.calls == 1, "应尝试所有可用引擎"
    served = results["latest_news"]
    assert served.success is False
    assert served.error_message == "连接被重置", "应保留最后一次失败原因"


@pytest.mark.unit
def test_no_available_provider_returns_empty_without_error():
    """没有可用引擎时应安全返回空结果。"""
    svc = _service_with([])
    results = svc.search_comprehensive_intel(
        stock_code="300750", stock_name="宁德时代", max_searches=1
    )
    assert results == {}


@pytest.mark.unit
def test_rotation_starts_from_different_provider_per_dimension():
    """轮转起点仍应逐维度推进（保持原有分摊负载意图）。"""
    a = _StubProvider("A", [
        _ok_response("A"), _ok_response("A"),
    ])
    b = _StubProvider("B", [
        _ok_response("B"), _ok_response("B"),
    ])

    svc = _service_with([a, b])
    results = svc.search_comprehensive_intel(
        stock_code="300750", stock_name="宁德时代", max_searches=2
    )

    providers_used = {r.provider for r in results.values()}
    assert providers_used == {"A", "B"}, f"两个维度应分别由不同引擎承担: {providers_used}"
