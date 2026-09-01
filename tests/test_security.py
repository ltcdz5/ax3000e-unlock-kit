# -*- coding: utf-8 -*-
"""本地单元测试（不连路由器）：注入拒绝 / 恢复白名单 / 内联转义 / 认证门禁。"""
import glob
import importlib.util
import json
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_DIR = os.path.join(_ROOT, "panel")
TOOLS_DIR = os.path.join(_ROOT, "tools")


def _load(name, filename, directory=PANEL_DIR):
    if directory not in sys.path:
        sys.path.insert(0, directory)          # 主面板 import monitor_web 需要同目录可寻址
    spec = importlib.util.spec_from_file_location(name, os.path.join(directory, filename))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _load_tool(name, filename):
    return _load(name, filename, TOOLS_DIR)


mw = _load("monitor_web_ut", "monitor_web.py")
sys.modules["monitor_web"] = mw     # 面板 import monitor_web 时复用同一份模块对象，避免双实例
main_panel = _load("main_panel_ut", "router_monitor_ax3000e.py")
ap_panel = _load("ap_panel_ut", "router_monitor_ap.py")

import device_profile as dp  # noqa: E402  设备识别解耦模块（PANEL_DIR 已在 _load 时入 sys.path）

INJ = "'" + "$" + "(reboot)" + "'"


# ---------- device_profile 设备识别（纯函数，不连路由器） ----------

def test_device_profile_known_hardware_maps_to_verified():
    r = dp.parse_device_profile("DEVICE_PRODUCT='Generic'\nDEVICE_MANUFACTURER='OpenWrt'", "RN07")
    assert r["profile"]["verified"] is True
    assert r["profile"]["name"] == "小米 AX3000E"
    assert r["hardware"] == "RN07"
    assert r["device_info"] == {"DEVICE_PRODUCT": "Generic", "DEVICE_MANUFACTURER": "OpenWrt"}


def test_device_profile_unknown_hardware_is_unverified():
    r = dp.parse_device_profile("", "XX99")
    assert r["profile"]["verified"] is False
    assert r["profile"]["name"] == "未知型号"
    assert r["profile"]["ssh_rsa_only"] is False   # 未验证机型不得假设设备特化行为


def test_device_profile_empty_hardware_treated_unknown():
    r = dp.parse_device_profile(None, "")
    assert r["profile"]["verified"] is False
    assert r["hardware"] == ""


def test_device_profile_collect_uses_injected_sh():
    calls = []

    def fake_sh(cmd, timeout=10):
        calls.append(cmd)
        return {"cat /etc/device_info 2>/dev/null": "DEVICE_PRODUCT='Generic'",
                dp.HW_CMD: "RN07",
                "uname -a 2>/dev/null": "Linux XiaoQiang 4.4.60 armv7l"}[cmd]

    r = dp.collect(fake_sh)
    assert len(calls) == 3
    assert r["profile"]["name"] == "小米 AX3000E"
    assert "XiaoQiang" in r["uname"]


# ---------- monitor_web 纯函数 ----------

def test_escape_inline_json_neutralizes_script_breakout():
    evil = json_host = "aa</scr" + "ipt><scr" + "ipt>alert(1)</scr" + "ipt>"
    out = mw.escape_inline_json('{"h": "' + evil + '"}')
    assert "<" not in out and ">" not in out
    assert "\\u003c/script\\u003e" in out


def test_escape_inline_json_line_separators():
    out = mw.escape_inline_json("a" + chr(0x2028) + "b" + chr(0x2029) + "c")
    assert chr(0x2028) not in out and chr(0x2029) not in out


def test_kit_version_is_semver():
    import re as _re
    assert _re.fullmatch(r"\d+\.\d+\.\d+", mw.KIT_VERSION), "发版时必须同步更新 KIT_VERSION"


def test_kit_version_has_single_source():
    """版本号只允许 panel/monitor_web.py 一处定义；tools 必须导入，不得各自硬编码。"""
    for filename in ("kit_doctor.py", "migration_pack.py"):
        tool = _load_tool(filename[:-3] + "_ver_ut", filename)
        assert tool.KIT_VERSION == mw.KIT_VERSION, filename + " 自带了 KIT_VERSION 常量"


# ---------- 实测基准固件版本：按机型查表，不得写死 ----------

_FW_COMPARE_RE = re.compile(r"""[=!]=\s*["']\d+\.\d+\.\d+["']|["']\d+\.\d+\.\d+["']\s*[=!]=""")


def test_firmware_pin_is_per_model_and_none_when_unlisted():
    assert dp.firmware_pin("RN07") == "1.0.24"
    assert dp.firmware_pin("BE3600") == "1.0.81"
    assert dp.firmware_pin("XX99") is None          # 未收录=无基准可比，不得借用别机型的版本
    assert dp.firmware_pin("") is None
    assert dp.firmware_pin(None) is None
    assert dp.firmware_pin("rn07") == "1.0.24"      # 管理页指纹给的是小写
    assert dp.firmware_pin(" be3600 ") == "1.0.81"  # 采集串常带首尾空白


def test_lowercase_fingerprint_is_still_a_verified_model():
    """机型标识大小写因采集来源而异（nvram=RN07，管理页指纹=rn07）；
    按大小写敏感查表会把已知机型降级成"未验证"，功能键与基准判定一起失真。"""
    r = dp.parse_device_profile("", "rn07")
    assert r["profile"]["verified"] is True
    assert r["profile"]["name"] == "小米 AX3000E"
    assert r["profile"]["hardware"] == "rn07"


def _health_sh_returning(hardware, rom):
    """伪造 get_health 那一次单往返的 7 段输出（假 sh 注入，不碰设备）。"""
    raw = "@@".join([json.dumps({"hardware": hardware, "romversion": rom}),
                     json.dumps({"code": 0, "status": 0}),
                     "0", "2", "y", "/data 1700 584 1016 37% /data", "0"])
    return lambda cmd, **kw: raw


def _firmware_row(panel, hardware, rom):
    original = panel.sh
    try:
        panel.sh = _health_sh_returning(hardware, rom)
        rows = [i for i in panel.get_health() if i["title"] == "固件版本"]
    finally:
        panel.sh = original
    assert len(rows) == 1, rows
    return rows[0]


def test_ap_panel_firmware_check_follows_the_device_model():
    """接 BE3600 却拿 AX3000E 的基准判定，会永远误报"非实测基准"——本轮修的正是这个。"""
    row = _firmware_row(ap_panel, "BE3600", "1.0.81")
    assert row["icon"] == "✅" and "实测基准" in row["detail"], row
    row = _firmware_row(ap_panel, "RN07", "1.0.24")
    assert row["icon"] == "✅", row
    row = _firmware_row(ap_panel, "RN07", "1.0.88")            # 真偏离基准才告警
    assert row["icon"] == "⚠️" and "1.0.24" in row["detail"], row
    row = _firmware_row(ap_panel, "XX99", "9.9.9")             # 未收录机型不伪装成偏差
    assert row["icon"] == "ℹ️" and "无基准可比" in row["detail"], row


def test_firmware_baseline_is_never_hardcoded_in_a_comparison():
    """基准版本字面量只允许待在 device_profile 能力表里。
    挡的是"再抄一份 == 1.0.24"：这种写法曾同时存在于两块面板加一键体检三处。"""
    scanned = sorted(sum((glob.glob(os.path.join(_ROOT, sub, "*.py"))
                          for sub in ("panel", "tools", "deploy")), [])
                     + glob.glob(os.path.join(_ROOT, "router", "*.sh")))
    names = [os.path.basename(p) for p in scanned]
    assert len(scanned) >= 12, "扫描范围异常，守卫等于没扫"
    for anchor in ("router_monitor_ap.py", "router_monitor_ax3000e.py",
                   "kit_doctor.py", "device_profile.py"):
        assert anchor in names, anchor + " 未纳入扫描，守卫有盲区"
    offenders = ["%s:%d %s" % (os.path.basename(p), n, line.strip())
                 for p in scanned for n, line in enumerate(open(p, encoding="utf-8"), 1)
                 if _FW_COMPARE_RE.search(line)]
    assert not offenders, "写死了基准固件版本，应改查 device_profile.firmware_pin：%s" % offenders


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
    a, p, j = mw.parse_act_body('{"action":"led_schedule","params":{"on":"08:00"}}')
    assert (a, p, j) == ("led_schedule", {"on": "08:00"}, True)
    from urllib.parse import quote
    form = "json=" + quote(json.dumps({"action": "reboot", "params": {"confirm": "yes"}}))
    a2, p2, j2 = mw.parse_act_body(form)
    assert a2 == "reboot" and p2 == {"confirm": "yes"} and j2 is False
    form2 = form + "&extra=1"
    a3, p3, j3 = mw.parse_act_body(form2)
    assert p3.get("extra") == "1" and j3 is False


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


# ---------- /api/act 应答格式：解析与应答判定同源（真实 HTTP 回环） ----------

import json
import threading
import http.client
import urllib.parse
from http.server import ThreadingHTTPServer


class _ActServer:
    """起真实共享层服务器（随机端口），do_action 可注入。"""

    def __init__(self, do_action):
        mw.Handler.ctx = {"lan": False, "token": "", "do_action": do_action}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), mw.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


def _post(port, body, headers=None):
    assert isinstance(port, int) and 0 < port < 65536
    h = {"Content-Type": "application/x-www-form-urlencoded"}   # 模拟 curl/表单默认
    if headers:
        h.update(headers)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", "/api/act", body=body, headers=h)
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read().decode("utf-8", "replace")
    finally:
        conn.close()


def test_act_json_body_with_form_content_type_returns_json():
    """审计案例①：curl -d '{...}' 默认 form Content-Type → 应答仍应为 200 JSON。"""
    s = _ActServer(lambda a, p: "pong")
    try:
        code, _, body = _post(s.port, b'{"action":"ping","params":{}}')
        assert code == 200
        assert json.loads(body) == {"ok": True, "msg": "pong"}
    finally:
        s.stop()


def test_act_form_error_redirects_with_friendly_msg():
    """审计案例②：表单请求 + do_action 抛异常 → 302 回跳带友好消息，不裸渲染 JSON。"""
    def boom(a, p):
        raise RuntimeError("路由器连接超时")
    s = _ActServer(boom)
    try:
        body = ("json=" + urllib.parse.quote(json.dumps({"action": "x", "params": {}}))).encode()
        code, headers, _ = _post(s.port, body)
        assert code == 302
        loc = headers.get("Location", "")
        assert loc.startswith("/?msg=") and "操作出错" in urllib.parse.unquote(loc)
    finally:
        s.stop()


def test_act_form_success_redirects():
    s = _ActServer(lambda a, p: "已完成")
    try:
        body = ("json=" + urllib.parse.quote(json.dumps({"action": "x", "params": {}}))).encode()
        code, headers, _ = _post(s.port, body)
        assert code == 302
        assert "msg=" in headers.get("Location", "")
    finally:
        s.stop()
