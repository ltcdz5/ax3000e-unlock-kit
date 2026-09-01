# -*- coding: utf-8 -*-
"""dnsmasq 配置契约：合并进同一个 conf-dir 的片段，不得重复「单例关键字」。

单例表来自 2026-09-02 在 RN07（dnsmasq 2.86）上的实测：把每个候选关键字写两行进
conf-dir，再用 `dnsmasq --test` 逐个判定，只有这 4 个会 `illegal repeated keyword`
拒绝启动（其余 15 个候选重复无害）。重复一次即全屋断 DNS，且现场极难归因。
"""

SINGLETON_KEYS = frozenset({
    "logfacility",     # log-facility
    "cachesize",       # cache-size / cachesize
    "port",
    "dhcpleasefile",   # dhcp-leasefile
})


def option_key(line):
    """取一行的选项名并归一化（去连字符，让 cache-size 与 cachesize 视为同一键）。"""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    return text.split("=", 1)[0].strip().replace("-", "").lower()


def singleton_duplicates(config_text):
    """合并后的配置原文 → {归一化键: 出现次数}，只含重复的单例键。"""
    counts = {}
    for line in (config_text or "").splitlines():
        key = option_key(line)
        if key in SINGLETON_KEYS:
            counts[key] = counts.get(key, 0) + 1
    return {k: c for k, c in counts.items() if c > 1}
