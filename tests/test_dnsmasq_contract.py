# -*- coding: utf-8 -*-
"""dnsmasq 配置契约测试：防「合并进同一 conf-dir 后单例关键字重复」导致 dnsmasq 拒绝启动。
2026-09-02 真实事故：查询日志开关写的 log-facility 与自愈脚本 93-stats.conf 撞车，一开就断网。"""
import glob
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))
import dnsmasq_contract as dc

SOURCES = [os.path.join(_ROOT, "router", "auto_ssh.sh"),
           os.path.join(_ROOT, "deploy", "oneclick_deploy.py")] + \
          sorted(glob.glob(os.path.join(_ROOT, "panel", "*.py")))

WRITE_RE = re.compile(r"""(?:echo|printf)\s+['"]([^'"]*?)['"]\s*>\s*(?:/tmp/dnsmasq\.d|/data)/\S+""")


def _sources_text():
    """仓库里所有会落到 dnsmasq.d 的配置原文：入仓 configs 文件 + 源码字面量写入行。"""
    chunks = []
    for path in sorted(glob.glob(os.path.join(_ROOT, "router", "configs", "*.conf"))):
        chunks.append(open(path, encoding="utf-8").read())
    for path in SOURCES:
        text = open(path, encoding="utf-8").read()
        for raw in WRITE_RE.findall(text):
            chunks.append(raw.replace("\\\\n", "\n").replace("\\n", "\n"))
    return "\n".join(chunks)


def test_detects_the_real_incident():
    """事故原样复现：93-stats 与 93-logqueries 各带一个 log-facility。"""
    merged = "log-facility=/tmp/dnsquery.log\nlog-queries\nlog-facility=/tmp/dnsquery.log\n"
    assert dc.singleton_duplicates(merged) == {"logfacility": 2}


def test_aliases_count_as_one_key():
    """cache-size 与 cachesize 是同一选项的两种写法，必须一起算。"""
    assert dc.singleton_duplicates("cache-size=4096\ncachesize=4096\n") == {"cachesize": 2}


def test_list_options_may_repeat():
    """实测允许重复的项：server / address / log-queries 多条不报警。"""
    merged = ("server=223.5.5.5\nserver=/microsoft.com/4.2.2.1\n"
              "address=/pstatp.com/::\naddress=/ad.test/0.0.0.0\nlog-queries\nlog-queries\n")
    assert dc.singleton_duplicates(merged) == {}


def test_comments_and_blank_lines_ignored():
    assert dc.singleton_duplicates("# port=53\n\n  \n") == {}


def test_repo_sources_have_no_singleton_clash():
    """整仓扫描：任何两处往 dnsmasq.d 写同一个单例键，都会让设备拒绝启动。"""
    merged = _sources_text()
    assert "log-facility" in merged                      # 确认扫描确实覆盖到自愈那行
    assert dc.singleton_duplicates(merged) == {}, dc.singleton_duplicates(merged)


def test_query_log_snippet_does_not_carry_log_facility():
    """查询日志片段只允许写 log-queries：facility 归自愈脚本独占。"""
    text = open(os.path.join(_ROOT, "router", "configs", "logqueries.conf"), encoding="utf-8").read()
    assert "log-facility" not in text
    assert "log-queries" in text
