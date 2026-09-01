# -*- coding: utf-8 -*-
"""kit_doctor 纯函数测试（DNS 输出解析 + 去广告链路实测）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.argv = ["kit_doctor.py"]  # kit_doctor 在模块级解析 argv，先清掉 pytest 参数
import kit_doctor as kd


def test_parse_table_format():
    out = ("Interface Alias AddressFamily ServerAddresses\n"
           "--------- ----- ------------- ---------------\n"
           "Ethernet  6     IPv4          192.168.2.106\n"
           "Ethernet  6     IPv4          223.5.5.5\n")
    assert kd.parse_dns_servers(out) == ["192.168.2.106", "223.5.5.5"]


def test_parse_single_column():
    assert kd.parse_dns_servers("192.168.2.106\n8.8.8.8\n") == ["192.168.2.106", "8.8.8.8"]


def test_parse_dedup_and_skip_noise():
    out = "192.168.2.106\n192.168.2.106\n::1\nWarning: something\n"
    assert kd.parse_dns_servers(out) == ["192.168.2.106"]


def test_parse_empty():
    assert kd.parse_dns_servers("") == []


def test_parse_ad_domain():
    assert kd.parse_ad_domain("address=/adgeo.163.com/#") == "adgeo.163.com"
    assert kd.parse_ad_domain("  address=/0hqq3dnjf.com/1.2.3.4  ") == "0hqq3dnjf.com"
    assert kd.parse_ad_domain("server=223.5.5.5") is None
    assert kd.parse_ad_domain("") is None
    assert kd.parse_ad_domain(None) is None


def test_parse_probe_answers():
    assert kd.parse_probe_answers("0.0.0.0") == ["0.0.0.0"]
    assert kd.parse_probe_answers("180.101.49.44,180.101.51.73") == ["180.101.49.44", "180.101.51.73"]
    assert kd.parse_probe_answers("") == []


def test_adblock_hit_requires_blackhole_answer():
    assert kd.adblock_hit(["0.0.0.0"]) is True            # 实测：/# 在本机 dnsmasq 上回 0.0.0.0
    assert kd.adblock_hit(["0.0.0.0", "::"]) is True
    assert kd.adblock_hit(["60.188.66.35"]) is False      # 放行 = 未拦
    assert kd.adblock_hit([]) is False                    # 没解析出来不算命中


def test_adblock_tally_reports_leak():
    assert kd.adblock_tally([["0.0.0.0"], ["0.0.0.0"], ["0.0.0.0"]]) == (3, 3)      # 全拦
    assert kd.adblock_tally([["1.2.3.4"], ["5.6.7.8"]]) == (0, 2)                    # 全漏
    assert kd.adblock_tally([["0.0.0.0"], ["115.236.118.49"], ["0.0.0.0"]]) == (2, 3)  # 偶发泄漏
    assert kd.adblock_tally([None, []]) is None                                      # 一次有效应答都没有


def test_probe_adblock_rejects_unsafe_input():
    """域名/服务器要拼进 PowerShell 命令，非法输入必须直接判"测不了"。"""
    assert kd.probe_adblock("adgeo.163.com; Remove-Item x", "192.168.2.1") is None
    assert kd.probe_adblock("adgeo.163.com", "192.168.2.1 && calc") is None
    assert kd.probe_adblock("", "192.168.2.1") is None
    assert kd.probe_adblock("adgeo.163.com", "") is None
