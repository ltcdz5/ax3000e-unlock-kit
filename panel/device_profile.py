# -*- coding: utf-8 -*-
"""设备识别模块（与面板/传输解耦）：型号识别 + 能力表。

设计原则：
- 本模块不认识 paramiko/SSH/面板——纯解析函数 + 一个接收 sh 回调的 collect()。
  面板把自己的 sh（单连接/批处理/重试策略各异）注入进来即可。
- 新机型适配 = 往 DEVICE_PROFILES 加一行 + 探针实测（deploy/device_probe.py），
  不改任何面板代码。
- 未验证机型一律走 UNKNOWN_PROFILE：设备特化功能键不得盲开。
"""
import re

# key = 机型识别源输出（AX3000E: `nvram get model`）；字段供面板/自愈/部署做分支判断
# capabilities: 设备特有能力，面板据此动态显示对应功能按钮
DEVICE_PROFILES = {
    "RN07": {
        "name": "小米 AX3000E", "soc": "IPQ5018（双核 A53）", "firmware_pin": "1.0.24",
        "ssh_rsa_only": True,   # dropbear 2017.75 只认 ssh-rsa，paramiko 需禁 rsa-sha2-*
        "led_ctl": True,        # /usr/sbin/led_ctl 可用
        "ram_mb": 186, "nand_mb": 128, "data_kb": 1700,  # 128MB NAND，/data 仅 1.7MB（双系统分区占用）
        "capabilities": {
            "full_adblock": False,  # /data 太小，放不下全量列表
            "nlbwmon": False,       # 确认无 nlbwmon
            "etherwake": False,     # 确认无 etherwake
            "big_cache": False,     # 内存有限，缓存不宜过大
            "vpn": False,           # 性能不足跑 VPN
            "perf_optimize": True,  # 基础优化可用
        },
        "notes": "本套件实测基准；128MB NAND 中 /data 仅 1.7MB（双系统分区），资源受限",
    },
    # 新机型：用 deploy/device_probe.py 实机校准后在此添加条目
    "BE3600": {
        "name": "小米 BE3600 2.5G", "soc": "IPQ5312（四核 A53）", "firmware_pin": "1.0.81",
        "ssh_rsa_only": False,   # 较新 dropbear，支持 rsa-sha2
        "led_ctl": False,        # 待实机验证
        "ram_mb": 256, "nand_mb": 128, "data_kb": 0,  # 128MB NAND，/data 大小待实机 df 确认
        "capabilities": {
            "full_adblock": False,  # 同 AX3000E：128MB NAND 双系统分区，/data 空间同样受限
            "nlbwmon": False,       # 待实机验证
            "etherwake": False,     # 待实机验证
            "big_cache": True,      # 256MB RAM 可设更大 dnsmasq 缓存（内存缓存，不占 /data）
            "vpn": True,           # 四核可跑轻量 VPN
            "perf_optimize": True,  # 基础优化
        },
        "notes": "预适配占位，待实机校准；v1.0.87 修复了注入漏洞需先降级；128MB NAND 双系统分区，/data 同 AX3000E 受限",
    },
}

# 机型识别源（AX3000E 实测：model=RN07，hardware/product_name 为空；
# 新机型若键位不同，探针确认后只改这一行）
HW_CMD = "nvram get model 2>/dev/null || nvram get hardware 2>/dev/null || nvram get product_name 2>/dev/null"

UNKNOWN_PROFILE = {"name": "未知型号", "ssh_rsa_only": False, "led_ctl": False,
                   "notes": "不在能力表中，勿盲开设备特化功能；先跑 deploy/device_probe.py 校准"}

_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)='?([^']*)'?\s*$")


def _lookup(hardware):
    """按机型取能力表条目。同一设备在不同采集来源下大小写不一致（`nvram get model`
    与 init_info 给 RN07，管理页指纹给 rn07），故按忽略大小写匹配。"""
    want = (hardware or "").strip().upper()
    for key, prof in DEVICE_PROFILES.items():
        if key.upper() == want:
            return prof
    return None


def parse_device_profile(device_info_text, hardware_code, uname_text=""):
    """解析设备标识并映射能力表。纯函数可单测。
    device_info_text: /etc/device_info 原文（KEY='value' 行）
    hardware_code: 机型识别源输出（AX3000E 为 `nvram get model` = RN07）；空/未知按未验证处理。"""
    info = {}
    for line in (device_info_text or "").splitlines():
        m = _KV_RE.match(line.strip())
        if m:
            info[m.group(1)] = m.group(2)
    hw = (hardware_code or "").strip()
    entry = _lookup(hw)
    prof = dict(entry or UNKNOWN_PROFILE)
    prof["hardware"] = hw or "(未知)"
    prof["verified"] = entry is not None
    return {"hardware": hw, "device_info": info, "uname": (uname_text or "").strip(),
            "profile": prof}


def collect(sh):
    """用注入的 sh 回调采集并解析设备画像（3 次只读命令）。
    适合逐命令执行的调用方；批处理单往返的调用方可自行采集后直接调 parse_device_profile。"""
    return parse_device_profile(
        sh("cat /etc/device_info 2>/dev/null"),
        sh(HW_CMD),
        sh("uname -a 2>/dev/null"))


def firmware_pin(hardware):
    """该机型的实测基准固件版本，全仓唯一取值入口。
    机型未收录返回 None——调用方须报"无基准可比"，不得拿别机型的版本判定。"""
    prof = _lookup(hardware)
    return prof.get("firmware_pin") if prof else None
