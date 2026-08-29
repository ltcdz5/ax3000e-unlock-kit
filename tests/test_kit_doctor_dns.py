# -*- coding: utf-8 -*-
"""kit_doctor 纯函数测试（DNS 输出解析）。"""
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
