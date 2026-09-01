# -*- coding: utf-8 -*-
"""去广告统计纯函数测试：设备计数解析、拦截率口径、未开日志时不得给出百分比。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "panel"))
import ad_stats as ads


def test_parse_count_reads_first_number():
    assert ads.parse_count("42\n") == 42
    assert ads.parse_count("0\n0") == 0          # grep -c 无匹配时 `|| echo 0` 会多打一行
    assert ads.parse_count("") is None
    assert ads.parse_count("wc: no such file") is None


def test_block_rate_is_plain_percentage():
    assert ads.block_rate(30, 1000) == 3.0
    assert ads.block_rate(1, 3) == 33.3
    assert ads.block_rate(1000, 1000) == 100.0


def test_block_rate_unknown_is_not_zero_percent():
    """分母为 0 表示「没记到查询」，必须回 None，不能被渲染成 0% 或 <1%。"""
    assert ads.block_rate(0, 0) is None
    assert ads.block_rate(5, None) is None


def test_summarize_withholds_rate_until_logging_enabled():
    out = ads.summarize((0, 0, 0), logging_on=False, list_total=2134)
    assert out["block_rate"] is None
    assert out["logging"] is False
    assert out["total_domains"] == "2134"


def test_summarize_reports_real_numbers():
    out = ads.summarize((37, 1000, 120), logging_on=True, list_total=2134)
    assert out["block_rate"] == 3.7
    assert out["blocked_today"] == "37"
    assert out["total_queries"] == "1000"
    assert out["logging"] is True
