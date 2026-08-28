# -*- coding: utf-8 -*-
"""本地单元测试（不连路由器）：注入拒绝 / 恢复白名单 / 内联转义 / 认证门禁。"""
import importlib.util
import os
import sys

import pytest

PANEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "panel")


def _load(name, filename):
    if PANEL_DIR not in sys.path:
        sys.path.insert(0, PANEL_DIR)          # 主面板 import monitor_web 需要同目录可寻址
    spec = importlib.util.spec_from_file_location(name, os.path.join(PANEL_DIR, filename))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


mw = _load("monitor_web_ut", "monitor_web.py")
main_panel = _load("main_panel_ut", "router_monitor_ax3000e.py")
ap_panel = _load("ap_panel_ut", "router_monitor_ap.py")

INJ = "'" + "$" + "(reboot)" + "'"


# ---------- monitor_web 纯函数 ----------

def test_escape_inline_json_neutralizes_script_breakout():
    evil = json_host = "aa</scr" + "ipt><scr" + "ipt>alert(1)</scr" + "ipt>"
    out = mw.escape_inline_json('{"h": "' + evil + '"}')
    assert "<" not in out and ">" not in out
    assert "\\u003c/script\\u003e" in out


def test_escape_inline_json_line_separators():
    out = mw.escape_inline_json("a" + chr(0x2028) + "b" + chr(0x2029) + "c")
    assert chr(0x2028) not in out and chr(0x2029) not in out


def test_host_ok_blocks_non_loopback_in_local_mode():
    assert mw.host_ok(False, "127.0.0.1:8787")
    assert mw.host_ok(False, "localhost")
    assert not mw.host_ok(False, "192.168.2.102:8787")      # DNS rebinding 面面
    assert mw.host_ok(True, "anything.example")             # --lan 不限制


def test_origin_ok_blocks_cross_site_posts():
    assert not mw.origin_ok(False, "http://evil.example", "127.0.0.1:8787")
    assert mw.origin_ok(False, "http://127.0.0.1:8787", "127.0.0.1:8787")
    assert mw.origin_ok(False, "", "127.0.0.1:8787")        # CLI 直连放行
    assert not mw.origin_ok(True, "", "x")                  # --lan 模式无 Origin 的 POST 拒绝


def test_auth_ok_basic_scheme():
    import base64
    good = "Basic " + base64.b64encode(b"anyuser:test123").decode()
    bad = "Basic " + base64.b64encode(b"anyuser:wrong").decode()
    assert mw.auth_ok("test123", good)
    assert not mw.auth_ok("test123", bad)
    assert not mw.auth_ok("test123", None)
    assert mw.auth_ok("", None)                             # 未配令牌=免认证
    assert not mw.auth_ok("test123", "Bearer xyz")          # 非 Basic 一律拒绝


def test_parse_act_body_json_and_form():
    import json
    a, p = mw.parse_act_body('{"action":"led_schedule","params":{"on":"08:00"}}')
    assert (a, p) == ("led_schedule", {"on": "08:00"})
    from urllib.parse import quote
    form = "json=" + quote(json.dumps({"action": "reboot", "params": {"confirm": "yes"}}))
    a2, p2 = mw.parse_act_body(form)
    assert a2 == "reboot" and p2 == {"confirm": "yes"}
    form2 = form + "&extra=1"
    a3, p3 = mw.parse_act_body(form2)
    assert p3.get("extra") == "1"


# ---------- 主面板：写动作参数白名单 ----------

def test_main_panel_rejects_injected_ip():
    assert "无效" in main_panel.do_action("port_add", {
        "name": "x", "ext": "80", "ip": "1.1.1.1" + INJ, "proto": "tcp"})


def test_main_panel_rejects_bad_mac():
    assert "MAC" in main_panel.do_action("device_bind", {"mac": "aa" + INJ + "bb",
                                                         "ip": "192.168.31.50"})


def test_main_panel_rejects_bad_lease():
    assert "格式" in main_panel.do_action("dhcp_lease", {"lease": "12h" + INJ})


def test_main_panel_rejects_non_numeric_qos():
    assert "数字" in main_panel.do_action("qos_band", {"download": "1" + INJ, "upload": "512"})


def test_main_panel_channel_range_split_by_band():
    assert "2.4G" in main_panel.do_action("wifi_channel", {"band": "2g", "channel": "40"})
    assert "5G" in main_panel.do_action("wifi_channel", {"band": "5g", "channel": "13"})
    assert "无效" in main_panel.do_action("wifi_channel", {"band": "6g", "channel": "36"})


def test_main_panel_rejects_injected_fw_name():
    assert "非法" in main_panel.do_action("fw_rule_add", {"name": "r" + INJ, "target": "DROP"})


# ---------- AP 面板：恢复白名单 ----------

def test_ap_restore_whitelist_accepts_config_paths():
    for p in ("etc/crontabs/root", "data/microsoft.conf", "data/auto_ssh/auto_ssh.sh"):
        assert ap_panel._whitelisted(p), p


def test_ap_restore_whitelist_blocks_traversal_and_lookalikes():
    assert not ap_panel._whitelisted("../etc/shadow")
    assert not ap_panel._whitelisted("etc/crontabs/rootxxx")   # 精确匹配，不许前缀误中
    assert not ap_panel._whitelisted("/etc/passwd")            # 绝对路径
    assert not ap_panel._whitelisted("data/../../etc/shadow")
