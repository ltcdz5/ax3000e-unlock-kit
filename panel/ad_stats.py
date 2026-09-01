# -*- coding: utf-8 -*-
"""去广告统计的纯计算：设备计数原文 → 面板展示值。不连设备、不读文件，便于单测。

没开 log-queries 时拦截率必须回 None：前端把 0 渲染成「<1%」，会把「没记录」伪装成「拦得少」。
"""


def parse_count(text):
    """取第一个数字 token。grep -c 无匹配时退出码非零，`|| echo 0` 会再补一行 0。"""
    for token in (text or "").split():
        if token.isdigit():
            return int(token)
    return None


def block_rate(blocked, queries):
    """拦截率百分比（1 位小数）；没有查询样本时返回 None。"""
    if not queries:
        return None
    return round((blocked or 0) * 100.0 / queries, 1)


def summarize(counts, logging_on, list_total):
    """counts = (blocked, queries, cached) → 面板展示字典。"""
    blocked, queries, cached = counts
    return {
        "total_domains": str(list_total or 0),
        "blocked_today": str(blocked or 0),
        "total_queries": str(queries or 0),
        "cached": str(cached or 0),
        "logging": bool(logging_on),
        "block_rate": block_rate(blocked, queries) if logging_on else None,
    }
