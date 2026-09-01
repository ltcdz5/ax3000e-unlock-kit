# -*- coding: utf-8 -*-
"""wifi_scan 纯函数单测：freq→信道换算、接口识别、邻居解析、信道打分。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "panel"))
import wifi_scan as w


def test_freq_to_channel():
    assert w.freq_to_channel(2412) == 1
    assert w.freq_to_channel(2437) == 6
    assert w.freq_to_channel(2462) == 11
    assert w.freq_to_channel(5180) == 36
    assert w.freq_to_channel(5745) == 149
    assert w.freq_to_channel(5785) == 157
    assert w.freq_to_channel(5805) == 161
    assert w.freq_to_channel(2484) == 14


def test_parse_iw_dev_only_active_aps():
    # 有 ssid 的 5G AP 接口保留；无 ssid 的 2.4G（未启用）接口应被过滤
    dev = (
        "phy#1\n"
        "\tInterface wl0\n\t\tssid MyAP\n\t\ttype AP\n\t\tchannel 149 (5745 MHz), width: 80 MHz\n"
        "phy#0\n"
        "\tInterface wifi0\n\t\ttype AP\n\t\tchannel 1 (2412 MHz), width: 20 MHz\n"
    )
    got = w.parse_iw_dev(dev)
    assert [x["iface"] for x in got] == ["wl0"]
    assert got[0]["band"] == "5g"


def test_parse_scan_neighbors():
    scan = (
        "BSS aa:bb:cc:dd:ee:ff(on wl0)\n\tfreq: 5180\n\tsignal: -60.00 dBm\n\tSSID: x\n"
        "BSS 11:22:33:44:55:66(on wl0)\n\tfreq: 5745\n\tsignal: -80.00 dBm\n"
    )
    nb = w.parse_scan_neighbors(scan)
    assert (36, -60.0) in nb
    assert (149, -80.0) in nb


def test_rank_prefers_cleanest():
    # ch36 挤（两个较强 AP），当前在 36 → 应推荐某个零干扰信道且不是 36
    r = w.rank("5g", [(36, -40.0), (36, -45.0), (149, -92.0)], current_channel=36)
    assert r["recommended"] != 36
    rec = next(c for c in r["candidates"] if c["channel"] == r["recommended"])
    assert rec["score"] == 0.0
    assert r["better"] is True


def test_rank_no_change_when_already_best():
    # 当前 ch36 零干扰，邻居都在 40/44 → 保持 36，不切换
    r = w.rank("5g", [(40, -40.0), (44, -45.0)], current_channel=36)
    assert r["recommended"] == 36
    assert r["better"] is False


def test_2g_overlap_weighting():
    # ch1 上的强 AP 只压 ch1，ch6/ch11 干净 → 推荐应避开 ch1
    r = w.rank("2g", [(1, -30.0)], current_channel=1)
    by = {c["channel"]: c["score"] for c in r["candidates"]}
    assert by[1] > by[6]
    assert by[1] > by[11]
    assert r["recommended"] in (6, 11)
    assert r["better"] is True
