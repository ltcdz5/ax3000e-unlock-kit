# -*- coding: utf-8 -*-
"""
AX3000E 监控 + 配置中心 v5.1（中文版）
- 性能监控: CPU/内存/温度/流量/连接数
- 配置中心: 全中文 + 每项带建议设置说明，事件委托实现
用法: python router_monitor.py
"""
import sys, os, json, time, threading, argparse, re
import urllib.parse
from collections import deque

import monitor_web
import device_profile

HOST = os.environ.get("ROUTER_HOST", "192.168.31.1")
SSHPORT = int(os.environ.get("ROUTER_SSH_PORT", "22"))
USER = os.environ.get("ROUTER_USER", "root")
PASSWD = os.environ.get("ROUTER_PASSWD", "<改成你的路由器SSH密码>")
WEBPORT = 8787
INTERVAL = 3
MAX_POINTS = 300

try:
    import paramiko
except ImportError:
    print("[!] 缺少 paramiko: pip install paramiko")
    sys.exit(1)

ssh_client = None
ssh_lock = threading.Lock()
data_lock = threading.Lock()

history = {k: deque(maxlen=MAX_POINTS) for k in
           ["cpu", "mem_used_mb", "mem_total_mb", "temp", "rx", "tx", "conn", "ts"]}
last_net = {}
last_stat = {}
collect_ms = 0
collect_fails = 0


def ssh_connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=SSHPORT, username=USER, password=PASSWD,
              timeout=8, allow_agent=False, look_for_keys=False,
              disabled_algorithms={'keys': ['rsa-sha2-256', 'rsa-sha2-512']})
    return c


def sh(cmd, timeout=10):
    global ssh_client
    with ssh_lock:
        for _ in range(2):
            try:
                if ssh_client is None or not ssh_client.get_transport() or not ssh_client.get_transport().is_active():
                    ssh_client = ssh_connect()
                stdin, stdout, stderr = ssh_client.exec_command(cmd, timeout=timeout)
                return stdout.read().decode("utf-8", "replace").strip()
            except Exception:
                try:
                    ssh_client = ssh_connect()
                except Exception:
                    time.sleep(2)
        return ""


def _sh_nolock(cmd, timeout=10):
    """仅供 ssh_lock 持有期间使用：直接执行并返回 stdout（不获取锁，防死锁）"""
    global ssh_client
    try:
        if ssh_client is None or not ssh_client.get_transport() or not ssh_client.get_transport().is_active():
            ssh_client = ssh_connect()
        _, stdout, _ = ssh_client.exec_command(cmd, timeout=timeout)
        return stdout.read().decode("utf-8", "replace").strip()
    except Exception:
        return ""


def sh_write(cmd, data, timeout=15):
    """执行命令并通过 stdin 写入数据；写后回读字节数校验（/data 卷满时截断重定向会
    "成功"但内容静默丢失）。校验不过返回 False"""
    global ssh_client
    wm = re.match(r"cat\s*(>>?)\s*(\S+)", cmd.strip())
    path, mode = (wm.group(2), wm.group(1)) if wm else (None, None)
    nbytes = len(data.encode("utf-8"))
    with ssh_lock:
        before = ""
        if path and mode == ">>":
            before = _sh_nolock("wc -c < %s 2>/dev/null" % path)
        for _ in range(2):
            try:
                if ssh_client is None or not ssh_client.get_transport() or not ssh_client.get_transport().is_active():
                    ssh_client = ssh_connect()
                stdin, stdout, stderr = ssh_client.exec_command(cmd, timeout=timeout)
                stdin.write(data)
                stdin.channel.shutdown_write()
                stdout.read()
                if not path:
                    return True
                after = _sh_nolock("wc -c < %s 2>/dev/null" % path)
                if not after.isdigit():
                    return False
                if mode == ">>":
                    return int(after) >= (int(before) if before.isdigit() else 0)
                return int(after) == nbytes
            except Exception:
                try:
                    ssh_client = ssh_connect()
                except Exception:
                    time.sleep(2)
        return False


def collect():
    global collect_ms, collect_fails
    now = time.time()
    t0 = now
    # 单次 SSH 拉回全部采集数据(替代 5 次独立往返), WAN 口用 eth1.4(主路由)
    raw = sh(
        "head -1 /proc/stat; echo '@@'; "
        "grep -E 'MemTotal|MemFree|Buffers|^Cached' /proc/meminfo; echo '@@'; "
        "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null; echo '@@'; "
        "grep eth1.4 /proc/net/dev; echo '@@'; "
        "cat /proc/net/tcp | wc -l"
    )
    dt_ms = (time.time() - t0) * 1000
    collect_ms = round(dt_ms) if collect_ms == 0 else round(collect_ms * 0.7 + dt_ms * 0.3)
    cpu_pct = 0.0
    parts = raw.split("@@")
    if len(parts) < 5:
        collect_fails += 1
        return
    collect_fails = 0

    st = parts[0].strip()
    if st.startswith("cpu"):
        p = st.split()
        try:
            idle = int(p[4]) + int(p[5])
            total = sum(int(x) for x in p[1:8])
            if last_stat:
                dt = total - last_stat["total"]
                if dt > 0:
                    cpu_pct = (1 - (idle - last_stat["idle"]) / dt) * 100
            last_stat["idle"], last_stat["total"] = idle, total
        except Exception:
            pass

    mem_total_mb = mem_used_mb = 0
    kv = {}
    for line in parts[1].splitlines():
        if ":" in line:
            try:
                kv[line.split(":")[0].strip()] = int(line.split(":")[1].strip().split()[0])
            except Exception:
                pass
    if "MemTotal" in kv:
        used_kb = kv["MemTotal"] - kv.get("MemFree", 0) - kv.get("Buffers", 0) - kv.get("Cached", 0)
        mem_total_mb, mem_used_mb = kv["MemTotal"] // 1024, used_kb // 1024

    temp = 0.0
    for v in parts[2].strip().split():
        if v.isdigit() and int(v) > 0:
            raw_t = int(v)
            temp = raw_t / 1000.0 if raw_t > 100 else float(raw_t)
            break

    rx_rate = tx_rate = 0.0
    nd = parts[3].strip()
    if nd:
        p = nd.split()
        if len(p) >= 10:
            try:
                rx, tx = int(p[1]), int(p[9])
            except Exception:
                rx, tx = 0, 0
            if last_net:
                dt = now - last_net["t"]
                if dt > 0:
                    rx_rate = max(0, (rx - last_net["rx"])) / dt / 1024
                    tx_rate = max(0, (tx - last_net["tx"])) / dt / 1024
            last_net["rx"], last_net["tx"], last_net["t"] = rx, tx, now

    conn = 0
    tc = parts[4].strip()
    if tc.isdigit():
        conn = int(tc) - 1

    with data_lock:
        history["cpu"].append(round(cpu_pct, 1))
        history["mem_used_mb"].append(mem_used_mb)
        history["mem_total_mb"].append(mem_total_mb)
        history["temp"].append(round(temp, 1))
        history["rx"].append(round(rx_rate, 1))
        history["tx"].append(round(tx_rate, 1))
        history["conn"].append(conn)
        history["ts"].append(now)


def collector_loop():
    global collect_fails
    interval = INTERVAL
    while True:
        try:
            collect()
        except Exception:
            collect_fails += 1
        if collect_fails >= 3:
            interval = 6
        elif collect_fails == 0 and interval > INTERVAL:
            interval = INTERVAL
        time.sleep(interval)


def get_config():
    """配置中心数据：单次 SSH 批处理全部只读命令（约 30 段），本地解析。"""
    parts = sh("; echo '@@'; ".join([
        "cat /tmp/dnsmasq.d/98-upstream.conf 2>/dev/null",                                 # 0
        "uci get dhcp.@dnsmasq[0].cachesize 2>/dev/null",                                  # 1
        "test -f /tmp/dnsmasq.d/hagezi.conf && wc -l < /tmp/dnsmasq.d/hagezi.conf || echo 0",  # 2
        "test -f /tmp/dnsmasq.d/96-antiad.conf && wc -l < /tmp/dnsmasq.d/96-antiad.conf || echo 0",  # 3
        "ls /tmp/dnsmasq.d/ 2>/dev/null",                                                   # 4
        "cat /tmp/dnsmasq.d/97-custom.conf 2>/dev/null",                                   # 5
        "ps | grep dropbear | grep -v grep",                                               # 6
        "test -f /data/auto_ssh/auto_ssh.sh && echo y",                                    # 7
        "uptime",                                                                          # 8
        "uci show firewall 2>/dev/null | grep redirect | head -40",                         # 9
        "cat /tmp/dhcp.leases 2>/dev/null",                                                # 10
        "uci show dhcp 2>/dev/null | grep 'host['",                                       # 11
        "cat /etc/crontabs/root 2>/dev/null",                                              # 12
        "uci show firewall 2>/dev/null | grep '@rule['",                                   # 13
        "uci get xiaoqiang.common.XLED 2>/dev/null",                                       # 14
        "uci get wireless.guest_2G.disabled 2>/dev/null",                                  # 15
        "uci get wireless.guest_5G.disabled 2>/dev/null",                                  # 16
        "uci show wireless 2>/dev/null",                                                   # 17
        "ps | grep miniupnpd | grep -v grep",                                              # 18
        "uci get upnpd.config.download 2>/dev/null",                                       # 19
        "uci get upnpd.config.upload 2>/dev/null",                                         # 20
        "uci get miqos.settings.enabled 2>/dev/null",                                      # 21
        "uci get miqos.settings.upload 2>/dev/null",                                       # 22
        "uci get miqos.settings.download 2>/dev/null",                                     # 23
        "uci get dhcp.lan.leasetime 2>/dev/null",                                          # 24
        "test -f /data/adblock.hosts && wc -l < /data/adblock.hosts || echo 0",            # 25
        device_profile.HW_CMD,                                                             # 26
        "cat /etc/device_info 2>/dev/null",                                                # 27
        "cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null",                        # 28
        "cat /proc/sys/net/core/netdev_max_backlog 2>/dev/null",                           # 29
    ])).split("@@")
    if len(parts) < 30:
        parts += [""] * (30 - len(parts))

    def seg(i):
        return parts[i].strip()

    def num(i):
        s = seg(i).split()
        return int(s[0]) if s and s[0].isdigit() else 0

    cfg = {"kit_version": monitor_web.KIT_VERSION}
    cfg["dns_upstreams"] = [l.replace("server=", "").strip()
                            for l in parts[0].splitlines() if l.startswith("server=")]
    cfg["cache_size"] = seg(1) or "150"
    hz = num(2)
    ad = num(3)
    cfg["adblock_antiad"] = max(hz, ad)
    cfg["adblock_yhosts"] = num(25)
    dns_dir = parts[4]
    cfg["adblock_enabled"] = "99-adblock.conf" in dns_dir
    cfg["log_queries"] = "93-logqueries" in dns_dir
    cfg["custom_adblock"] = [re.sub(r"^address=/(.*)/.*$", r"\1", l).strip()
                             for l in parts[5].splitlines() if l.startswith("address=/")]
    cfg["upnp"] = "miniupnpd" in parts[18]
    cfg["upnp_download"] = seg(19)
    cfg["upnp_upload"] = seg(20)
    cfg["ssh"] = "dropbear" in parts[6]
    cfg["qos"] = seg(21)
    cfg["qos_up"] = seg(22)
    cfg["qos_down"] = seg(23)
    cfg["auto_ssh"] = seg(7) == "y"
    cfg["device"] = device_profile.parse_device_profile(seg(27), seg(26))
    cfg["dhcp_lease"] = seg(24) or "12h"
    cfg["uptime"] = seg(8).split(",")[0].strip() if seg(8) else ""
    cfg["temp"] = history["temp"][-1] if history["temp"] else 0
    # 端口转发
    cfg["port_forwards"] = []
    cur = {}
    for line in parts[9].splitlines():
        m = re.match(r"firewall\.@redirect\[(\d+)\]\.(\w+)='(.*)'", line.strip())
        if m:
            cur.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    for idx, d in cur.items():
        if d.get("name") or d.get("dest_ip"):
            cfg["port_forwards"].append({"id": idx, "name": d.get("name", ""),
                                         "src_dport": d.get("src_dport", ""),
                                         "dest_ip": d.get("dest_ip", ""),
                                         "dest_port": d.get("dest_port", ""),
                                         "proto": d.get("proto", "")})
    # 设备列表（DHCP 租约）
    devices = []
    for line in parts[10].splitlines():
        p = line.split()
        if len(p) >= 4:
            devices.append({"ip": p[2], "mac": p[1], "host": p[3]})
    cfg["devices"] = devices
    # 静态绑定（dhcp host）
    binds = []
    cur = {}
    for line in parts[11].splitlines():
        m = re.match(r"dhcp\.@host\[(\d+)\]\.(\w+)='(.*)'", line.strip())
        if m:
            cur.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    for idx, d in cur.items():
        binds.append({"id": idx, "mac": d.get("mac", ""), "ip": d.get("ip", ""), "name": d.get("name", "")})
    cfg["binds"] = binds
    # 用户定时任务（行尾 #panel 标记）
    crontab = parts[12].splitlines()
    cfg["cron_tasks"] = [l.rstrip()[:-7].strip() for l in crontab if l.rstrip().endswith("#panel")]
    # 防火墙规则
    fw_rules = []
    cur = {}
    for line in parts[13].splitlines():
        m = re.match(r"firewall\.@rule\[(\d+)\]\.(\w+)='(.*)'", line.strip())
        if m:
            cur.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    for idx, d in cur.items():
        fw_rules.append({"id": idx, "name": d.get("name", ""), "target": d.get("target", d.get("action", "")),
                         "src": d.get("src_ip", d.get("src", "")), "dest_port": d.get("dest_port", ""),
                         "proto": d.get("proto", ""), "family": d.get("family", "")})
    cfg["fw_rules"] = fw_rules
    cfg["adstats"] = get_ad_stats()
    # LED 状态
    cfg["led_blue"] = seg(14) == "1"
    # Guest WiFi 状态
    g2 = seg(15); g5 = seg(16)
    cfg["guest_wifi"] = {"2g": "off" if g2 == "1" or g2 == "" else "on",
                         "5g": "off" if g5 == "1" or g5 == "" else "on"}
    # WiFi 状态（本地解析 uci show wireless）
    uci = {}
    for line in parts[17].splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            uci[k.strip()] = v.strip().strip("'")
    def get(key):
        return uci.get(key, "")
    cfg["wifi"] = {
        "g_channel": get("wireless.wifi0.channel"),
        "g_htmode": get("wireless.wifi0.htmode"),
        "g_power": get("wireless.wifi0.txpwr"),
        "g_ssid": get("wireless.@wifi-iface[0].ssid"),
        "g_disabled": get("wireless.@wifi-iface[0].disabled"),
        "g_hidden": get("wireless.@wifi-iface[0].hidden"),
        "a_channel": get("wireless.wifi1.channel"),
        "a_htmode": get("wireless.wifi1.htmode"),
        "a_power": get("wireless.wifi1.txpwr"),
        "a_ssid": get("wireless.@wifi-iface[1].ssid"),
        "a_disabled": get("wireless.@wifi-iface[1].disabled"),
        "a_hidden": get("wireless.@wifi-iface[1].hidden"),
    }
    # 性能参数状态
    cfg["perf_cache"] = int(seg(1)) if seg(1).isdigit() else 0
    cfg["perf_conntrack"] = num(28)
    cfg["perf_backlog"] = num(29)
    return cfg


def dns_latency(server, domain="www.baidu.com", tries=3):
    """UDP 直连测 DNS 上游延迟(ms)，失败返回 -1"""
    import socket, struct, time, random
    ok = []
    for _ in range(tries):
        tid = random.randint(0, 0xFFFF)
        hdr = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
        q = b"".join(bytes([len(p)]) + p.encode() for p in domain.split(".")) + b"\x00"
        pkt = hdr + q + struct.pack(">HH", 1, 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        t0 = time.time()
        try:
            s.sendto(pkt, (server, 53))
            s.recvfrom(4096)
            ok.append((time.time() - t0) * 1000)
        except Exception:
            pass
        s.close()
    return round(sum(ok) / len(ok), 1) if ok else -1


def do_action(action, params=None):
    params = params or {}
    if action == "adblock_toggle":
        if sh("ls /tmp/dnsmasq.d/ 2>/dev/null").count("99-adblock.conf") > 0:
            sh("rm -f /tmp/dnsmasq.d/99-adblock.conf")
            msg = "去广告已关闭"
        else:
            sh("echo 'addn-hosts=/data/adblock.hosts' > /tmp/dnsmasq.d/99-adblock.conf")
            msg = "去广告已开启"
        sh("/etc/init.d/dnsmasq restart")
        return msg
    if action == "antiad_update":
        # 下载耗时远超 sh() 默认超时，单独放宽；先落临时文件，校验通过才覆盖生效列表
        sh("curl -sL 'https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@main/dnsmasq/light.txt' "
           "-o /tmp/hagezi_new.conf --connect-timeout 15 --max-time 90", timeout=100)
        raw = sh("wc -c < /tmp/hagezi_new.conf 2>/dev/null").strip()
        n = sh("grep -c '^address=/' /tmp/hagezi_new.conf 2>/dev/null").strip()
        if not raw.isdigit() or int(raw) < 100000 or not n.isdigit() or int(n) < 1000:
            sh("rm -f /tmp/hagezi_new.conf")
            return "下载未完成（%s 字节 / %s 条），已保留原 hagezi 列表" % (raw or "0", n or "0")
        # 规则瘦身：只保留游戏/常用域名
        KEEP = "steam|epicgames|battle|blizzard|origin|riotgames|microsoft|windowsupdate|doubleclick|googlead|facebook|twitter|ad.*baidu|qq.*com|taobao|tmall|alibaba|xiaomi|bilibili|youku|iqiyi|163.*com|sina|sohu|jd.*com|meituan|adservice|adnxs|openx|rubicon|pubmatic|appnexus|outbrain|taboola"
        sh("mv /tmp/hagezi_new.conf /tmp/hagezi_new.conf.full; "
           "grep -iE '" + KEEP + "' /tmp/hagezi_new.conf.full > /tmp/hagezi_new.conf; "
           "rm -f /tmp/hagezi_new.conf.full")
        n = sh("wc -l < /tmp/hagezi_new.conf 2>/dev/null").strip()
        sh("mv -f /tmp/hagezi_new.conf /tmp/dnsmasq.d/hagezi.conf; /etc/init.d/dnsmasq restart")
        return "hagezi 已更新: " + n + " 条"
    if action == "dnsmasq_restart":
        sh("/etc/init.d/dnsmasq restart")
        return "dnsmasq 已重启"
    if action == "log_toggle":
        was_on = sh("test -f /tmp/dnsmasq.d/93-logqueries.conf && echo y") == "y"
        if was_on:
            r = sh("rm -f /tmp/dnsmasq.d/93-logqueries.conf /data/logqueries.conf; /etc/init.d/dnsmasq restart; "
                   "test -f /tmp/dnsmasq.d/93-logqueries.conf || echo DONE")
            ok_msg = "DNS 查询日志已关闭（持久生效，重启后仍关闭；dnsmasq CPU 负担降低）"
        else:
            r = sh("printf 'log-queries\\nlog-facility=/tmp/dnsquery.log\\n' > /data/logqueries.conf; "
                   "cp /data/logqueries.conf /tmp/dnsmasq.d/93-logqueries.conf; /etc/init.d/dnsmasq restart; "
                   "test -f /tmp/dnsmasq.d/93-logqueries.conf && echo DONE")
            ok_msg = "DNS 查询日志已开启（写入 tmpfs /tmp/dnsquery.log，不耗闪存）"
        if "DONE" not in r:
            return "SSH 执行失败（路由器无响应或过载），状态未变更，请稍后重试"
        return ok_msg
    if action == "upnp_toggle":
        on = "miniupnpd" in sh("ps | grep miniupnpd | grep -v grep")
        if on:
            sh("uci set upnpd.config.enable_upnp='0'; uci commit upnpd; /etc/init.d/miniupnpd stop")
            return "UPnP 已关闭"
        else:
            sh("uci set upnpd.config.enable_upnp='1'; uci commit upnpd; /etc/init.d/miniupnpd start")
            return "UPnP 已开启"
    if action == "upnp_rate":
        d, u = str(int(params.get("download", 1024))), str(int(params.get("upload", 512)))
        sh("uci set upnpd.config.download='" + d + "'; uci set upnpd.config.upload='" + u + "'; uci commit upnpd")
        sh("killall miniupnpd 2>/dev/null; /usr/sbin/miniupnpd -S -f /var/etc/miniupnpd.conf >/dev/null 2>&1 &")
        return "UPnP 速率已设置: 下行 " + d + " KB/s, 上行 " + u + " KB/s"
    if action == "qos_toggle":
        if sh("uci get miqos.settings.enabled 2>/dev/null").strip() == "1":
            sh("uci set miqos.settings.enabled='0'; uci commit miqos")
            return "QoS 已关闭"
        else:
            sh("uci set miqos.settings.enabled='1'; uci commit miqos")
            return "QoS 已开启"
    if action == "qos_band":
        try:
            d, u = int(params.get("download", 0)), int(params.get("upload", 0))
        except (TypeError, ValueError):
            return "带宽须为数字"
        if not (0 <= d <= 1000000 and 0 <= u <= 1000000):
            return "带宽范围 0-1000000"
        d, u = str(d), str(u)
        sh("uci set miqos.settings.download='" + d + "'; uci set miqos.settings.upload='" + u + "'; uci commit miqos")
        return "QoS 带宽已设置: 下行 " + d + ", 上行 " + u
    if action == "cache_set":
        v = str(params.get("size", "")).strip()
        if not v.isdigit() or not (64 <= int(v) <= 100000):
            return "缓存大小须为 64-100000 的整数"
        v = str(int(v))
        sh("uci set dhcp.@dnsmasq[0].cachesize='" + v + "'; uci commit dhcp; /etc/init.d/dnsmasq restart")
        return "DNS 缓存已设为 " + v
    if action == "dns_add":
        s = params.get("server", "").strip()
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", s):
            return "无效 IP"
        sh("echo 'server=" + s + "' >> /tmp/dnsmasq.d/98-upstream.conf; /etc/init.d/dnsmasq restart; cp /tmp/dnsmasq.d/98-upstream.conf /data/upstreams.conf")
        return "已添加上游 " + s
    if action == "dns_del":
        s = params.get("server", "").strip()
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", s):
            return "无效 IP"
        path = "/tmp/dnsmasq.d/98-upstream.conf"
        cur = sh("cat " + path + " 2>/dev/null").splitlines()
        keep = [l for l in cur if l.strip() != "server=" + s]
        if len(keep) == len(cur):
            return "未找到上游 " + s
        if not keep:
            return "至少保留一个上游，否则域名解析会中断"
        if not sh_write("cat > " + path, "\n".join(keep) + "\n"):
            return "写入失败"
        sh("/etc/init.d/dnsmasq restart; cp " + path + " /data/upstreams.conf")
        return "已删除上游 " + s
    if action == "ad_custom_add":
        d = params.get("domain", "").strip().lower()
        if not re.match(r"^[a-z0-9\-\.]+$", d):
            return "无效域名"
        sh("echo 'address=/" + d + "/0.0.0.0' >> /tmp/dnsmasq.d/97-custom.conf; "
           "cp /tmp/dnsmasq.d/97-custom.conf /data/custom.conf; /etc/init.d/dnsmasq restart")
        return "已屏蔽 " + d
    if action == "ad_custom_del":
        d = params.get("domain", "").strip().lower()
        if not re.match(r"^[a-z0-9\-\.]+$", d):
            return "无效域名"
        path = "/tmp/dnsmasq.d/97-custom.conf"
        cur = sh("cat " + path + " 2>/dev/null").splitlines()
        keep = [l for l in cur if l.strip() != "address=/" + d + "/0.0.0.0"]
        if len(keep) == len(cur):
            return "未找到屏蔽记录 " + d
        if not sh_write("cat > " + path, ("\n".join(keep) + "\n") if keep else ""):
            return "写入失败"
        sh("cp " + path + " /data/custom.conf; /etc/init.d/dnsmasq restart")
        return "已解除屏蔽 " + d
    if action == "dhcp_lease":
        v = str(params.get("lease", "12h")).strip()
        if not re.match(r"^\d{1,4}h$", v):
            return "租期格式须为 数字+h（如 12h）"
        sh("uci set dhcp.lan.leasetime='" + v + "'; uci commit dhcp; /etc/init.d/dnsmasq restart")
        return "DHCP 租期已设为 " + v
    if action == "port_add":
        name = str(params.get("name", "")).strip(); ext = str(params.get("ext", "")).strip()
        ip = str(params.get("ip", "")).strip(); inner = str(params.get("inner", ext)).strip()
        proto = str(params.get("proto", "tcp")).strip().lower()
        if not re.match(r"^[\w\- \u4e00-\u9fa5]{0,32}$", name):
            return "名称含非法字符（限 32 位内字母/数字/中文/连字符/空格）"
        if not (ext.isdigit() and 1 <= int(ext) <= 65535):
            return "外网端口须为 1-65535"
        if not (re.match(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$", ip)
                and all(int(o) <= 255 for o in ip.split("."))):
            return "内网 IP 无效"
        if inner != ext and not (inner.isdigit() and 1 <= int(inner) <= 65535):
            return "内网端口须为 1-65535"
        if proto not in ("tcp", "udp", "tcpudp"):
            return "协议无效"
        for c in ["uci add firewall redirect",
                  "uci set firewall.@redirect[-1].name='" + name + "'",
                  "uci set firewall.@redirect[-1].src='wan'",
                  "uci set firewall.@redirect[-1].dest='lan'",
                  "uci set firewall.@redirect[-1].proto='" + proto + "'",
                  "uci set firewall.@redirect[-1].src_dport='" + ext + "'",
                  "uci set firewall.@redirect[-1].dest_ip='" + ip + "'",
                  "uci set firewall.@redirect[-1].dest_port='" + inner + "'",
                  "uci commit firewall"]:
            sh(c)
        sh("fw3 reload 2>/dev/null")
        return "端口转发已添加: " + ext + " -> " + ip + ":" + inner
    if action == "port_del":
        idx = params.get("id", "")
        if idx.isdigit():
            sh("uci delete firewall.@redirect[" + idx + "]; uci commit firewall; fw3 reload 2>/dev/null")
            return "端口转发已删除 #" + idx
        return "无效 ID"
    if action == "wifi_channel":
        band = str(params.get("band", "5g")).strip().lower()
        ch = str(params.get("channel", "0")).strip()
        if band not in ("2g", "5g"):
            return "band 无效"
        ifname = "wl1" if band == "2g" else "wl0"
        if not ch.isdigit():
            return "信道须为数字"
        if ch == "0":
            return "请选择具体信道（自动模式重启后恢复）"
        if band == "2g" and not (1 <= int(ch) <= 13):
            return "2.4G 信道范围 1-13"
        if band == "5g" and not (32 <= int(ch) <= 177):
            return "5G 信道范围 32-177"
        # 实际函数名是 _set_channel, 直接调 iwconfig 更可靠
        sh("iwconfig " + ifname + " channel " + ch)
        return "WiFi " + band + " 信道已即时切换为 " + ch + "（重启后恢复自动，如需持久请在小米管理页设置）"
    if action == "wifi_htmode":
        return "WiFi 设置请用小米管理页 192.168.31.1（本面板 uci 修改会被系统覆盖）"
    if action == "wifi_ssid":
        return "WiFi 设置请用小米管理页 192.168.31.1（本面板 uci 修改会被系统覆盖）"
    if action == "wifi_pass":
        return "WiFi 设置请用小米管理页 192.168.31.1（本面板 uci 修改会被系统覆盖）"
    if action == "wifi_toggle":
        return "WiFi 设置请用小米管理页 192.168.31.1（本面板 uci 修改会被系统覆盖）"
    if action == "wifi_hidden":
        return "WiFi 设置请用小米管理页 192.168.31.1（本面板 uci 修改会被系统覆盖）"
    if action == "device_bind":
        mac = params.get("mac", "").strip().lower()
        ip = params.get("ip", "").strip()
        name = str(params.get("name", "")).strip()
        if not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac):
            return "MAC 格式须为 aa:bb:cc:dd:ee:ff"
        if not (re.match(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$", ip)
                and all(int(o) <= 255 for o in ip.split("."))):
            return "IP 无效"
        if name and not re.match(r"^[\w\- \u4e00-\u9fa5]{1,32}$", name):
            return "名称含非法字符（限 32 位内字母/数字/中文/连字符/空格）"
        cmds = ["uci add dhcp host",
                "uci set dhcp.@host[-1].mac='" + mac + "'",
                "uci set dhcp.@host[-1].ip='" + ip + "'",
                "uci set dhcp.@host[-1].name='" + (name or mac) + "'",
                "uci commit dhcp"]
        for cc in cmds:
            sh(cc)
        sh("/etc/init.d/dnsmasq restart")
        return "已绑定 " + mac + " -> " + ip
    if action == "device_unbind":
        idx = params.get("id", "")
        if idx.isdigit():
            sh("uci delete dhcp.@host[" + idx + "]; uci commit dhcp; /etc/init.d/dnsmasq restart")
            return "已解除绑定 #" + idx
        return "无效 ID"
    if action == "cron_add":
        sched = params.get("schedule", "").strip()
        cmd = params.get("command", "").strip()
        if not (sched and cmd):
            return "时间和命令必填"
        fields = sched.split()
        if len(fields) != 5 or not all(re.match(r"^[\d*,/-]+$", f) for f in fields):
            return "时间须为 5 段「分 时 日 月 周」，例 0 4 * * *"
        if len(cmd) > 200 or "\n" in cmd or cmd.endswith("#panel"):
            return "命令无效"
        line = sched + " " + cmd + " #panel"
        if line in sh("cat /etc/crontabs/root 2>/dev/null").splitlines():
            return "该任务已存在"
        if not sh_write("cat >> /etc/crontabs/root", line + "\n"):
            return "写入失败"
        sh("/etc/init.d/cron restart")
        return "定时任务已添加: " + sched + " " + cmd
    if action == "cron_del":
        line = params.get("line", "").strip()
        if not (line and len(line) <= 220):
            return "参数无效"
        target = line + " #panel"
        cur = sh("cat /etc/crontabs/root 2>/dev/null").splitlines()
        keep = [l for l in cur if l.strip() != target]
        if len(keep) == len(cur):
            return "未找到该任务"
        if not sh_write("cat > /etc/crontabs/root", "\n".join(keep) + "\n"):
            return "写入失败"
        sh("/etc/init.d/cron restart")
        return "定时任务已删除: " + line
    if action == "dns_speedtest":
        upstreams = [l.strip().split("server=")[1] for l in sh("cat /tmp/dnsmasq.d/98-upstream.conf").splitlines() if l.startswith("server=")]
        if not upstreams:
            return "未找到上游配置"
        speeds = [{"ip": ip, "ms": dns_latency(ip)} for ip in upstreams]
        cfg_out = "DNS 上游延迟:\n" + "\n".join((str(s["ms"]) + "ms " + s["ip"]) for s in sorted(speeds, key=lambda x: x["ms"]))
        return cfg_out
    if action == "dns_fastest":
        upstreams = [l.strip().split("server=")[1] for l in sh("cat /tmp/dnsmasq.d/98-upstream.conf").splitlines() if l.startswith("server=")]
        if not upstreams:
            return "未找到上游配置"
        speeds = [(dns_latency(ip), ip) for ip in upstreams]
        ok = sorted([s for s in speeds if s[0] >= 0], key=lambda x: x[0])
        if not ok:
            return "所有上游均超时，请检查网络"
        top = [ip for _, ip in ok[:4]]
        sh("rm -f /tmp/dnsmasq.d/98-upstream.conf")
        for ip in top:
            sh("echo 'server=" + ip + "' >> /tmp/dnsmasq.d/98-upstream.conf")
        sh("uci set dhcp.@dnsmasq[0].server='" + " ".join(top) + "' 2>/dev/null; uci commit dhcp 2>/dev/null")
        sh("/etc/init.d/dnsmasq restart; cp /tmp/dnsmasq.d/98-upstream.conf /data/upstreams.conf")
        return "已启用最快 4 个上游: " + " ".join(top) + "（DNS 已重启，已持久化）"
    if action == "wifi_power":
        band = params.get("band", "5g")
        pw = str(params.get("power", "28"))
        ifname = "wl1" if band == "2g" else "wl0"
        if not pw.isdigit() or not (0 <= int(pw) <= 30):
            return "功率须 0-30 dBm"
        sh("iwconfig " + ifname + " txpower " + pw + "dBm")
        return "WiFi " + band + " 功率已设为 " + pw + " dBm（即时生效，重启恢复）"
    if action == "fw_rule_add":
        name = str(params.get("name", "")).strip() or "rule"
        target = str(params.get("target", "DROP")).strip().upper()
        src = str(params.get("src", "")).strip()
        dport = str(params.get("dest_port", "")).strip()
        proto = str(params.get("proto", "")).strip().lower()
        if target not in ("ACCEPT", "DROP", "REJECT"):
            return "动作必须是 ACCEPT/DROP/REJECT"
        if not re.match(r"^[\w\- \u4e00-\u9fa5]{1,32}$", name):
            return "规则名含非法字符（限 32 位内字母/数字/中文/连字符/空格）"
        if src and not (re.match(r"^(\d{1,3}\.){3}\d{1,3}$", src)
                        and all(int(o) <= 255 for o in src.split("."))):
            return "来源 IP 无效"
        if dport and not re.match(r"^\d{1,5}(-\d{1,5})?$", dport):
            return "端口须为数字或范围（如 100-200）"
        if proto and proto not in ("tcp", "udp", "tcp udp"):
            return "协议无效"
        cmds = ["uci add firewall rule",
                "uci set firewall.@rule[-1].name='" + name + "'",
                "uci set firewall.@rule[-1].target='" + target + "'"]
        if src:
            cmds.append("uci set firewall.@rule[-1].src_ip='" + src + "'")
        if dport:
            cmds.append("uci set firewall.@rule[-1].dest_port='" + dport + "'")
        if proto:
            cmds.append("uci set firewall.@rule[-1].proto='" + proto + "'")
        cmds.append("uci commit firewall")
        for cc in cmds:
            sh(cc)
        sh("fw3 reload 2>/dev/null")
        return "防火墙规则已添加: " + target + " " + (src or "any") + ((":" + dport) if dport else "")
    if action == "fw_rule_del":
        idx = params.get("id", "")
        if idx.isdigit():
            sh("uci delete firewall.@rule[" + idx + "]; uci commit firewall; fw3 reload 2>/dev/null")
            return "防火墙规则已删除 #" + idx
        return "无效 ID"
    if action == "led_toggle":
        cur = sh("uci get xiaoqiang.common.XLED 2>/dev/null").strip()
        if cur == "1":
            sh("/usr/sbin/led_ctl led_off")
            return "LED 已关闭（灯灭）"
        else:
            sh("/usr/sbin/led_ctl led_on")
            return "LED 已开启"
    if action == "perf_optimize":
        out = sh(
            "uci set dhcp.@dnsmasq[0].cachesize=2048; uci commit dhcp; "
            "echo 32768 > /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null; "
            "echo 2048 > /proc/sys/net/core/netdev_max_backlog 2>/dev/null; "
            "echo '4096 87380 16777216' > /proc/sys/net/ipv4/tcp_rmem 2>/dev/null; "
            "echo '4096 65536 16777216' > /proc/sys/net/ipv4/tcp_wmem 2>/dev/null; "
            "echo OK")
        sh("/etc/init.d/dnsmasq restart")
        return "路由器优化已执行：DNS缓存 2048，连接跟踪 32768，网卡队列 2048，TCP缓冲区扩大"
    if action == "backup":
        total = sh("uci show 2>/dev/null | wc -l")
        return "配置项共 " + total + " 行（完整配置在路由器 /etc/config/，本面板可查看摘要）"
    if action == "reboot":
        if params.get("confirm") != "yes":
            return "已取消：需确认（confirm=yes）才执行重启"
        sh("(sleep 2; reboot) &")
        return "路由器 2 秒后重启，请等待约 2 分钟"
    if action == "backup":
        cfg_out = sh("uci show 2>/dev/null | head -100")
        return "配置快照已获取（前 100 行），可查看"
    if action == "svc_stop" or action == "svc_start":
        name = params.get("name", "")
        scripts = {"messagingagent": "messagingagent.sh", "mosquitto": "mosquitto", "xq_info_sync_mqtt": "xq_info_sync_mqtt"}
        if name not in scripts:
            return "未知服务"
        if action == "svc_stop":
            rc_links = {"messagingagent": "S49messagingagent.sh", "mosquitto": "S90mosquitto",
                        "xq_info_sync_mqtt": "S99xq_info_sync_mqtt"}
            sh("/etc/init.d/%s stop 2>/dev/null; killall %s 2>/dev/null; "
               "ln -sf ../init.d/%s /etc/rc.d/%s 2>/dev/null" %
               (scripts[name], name, scripts[name], rc_links.get(name, "")))
            return "已停止 " + name + "（重启路由器后自动恢复）"
        sh("/etc/init.d/%s start 2>/dev/null" % scripts[name])
        return "已启动 " + name + "（若未起来请重启路由器）"
    if action == "restart_panel":
        import subprocess, webbrowser
        subprocess.Popen([sys.executable] + sys.argv,
                         creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0)
        webbrowser.open("http://127.0.0.1:%s" % WEBPORT)
        os._exit(0)
    return "未知操作"


PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>AX3000E 监控+配置中心</title>
<style>
body{font-family:'Microsoft YaHei','Segoe UI',sans-serif;background:#14161a;color:#e8eaed;margin:0;padding:20px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8a9099;font-size:12px;margin-bottom:16px}
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tab{padding:8px 20px;border-radius:8px;cursor:pointer;background:#1f242c;border:1px solid #2a3038}
.tab.active{background:#2d5d8a;border-color:#3a7ab5}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.card{background:#1f242c;border-radius:12px;padding:14px 18px;min-width:130px;border:1px solid #2a3038}
.card .v{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.card .l{font-size:12px;color:#8a9099;margin-top:5px}
.card .s{font-size:11px;margin-top:3px}
.ok{color:#66bb6a}.warn{color:#ffa726}.bad{color:#ef5350}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.panel{background:#1f242c;border-radius:12px;padding:12px 14px;border:1px solid #2a3038}
.panel h3{font-size:13px;margin:0 0 6px;color:#8a9099}
canvas{width:100%;height:150px;display:block}
.cfg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px}
.cfg-panel{background:#1f242c;border-radius:12px;padding:16px;border:1px solid #2a3038}
.cfg-panel h3{font-size:14px;margin:0 0 8px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap}
.badge{font-size:11px;padding:3px 10px;border-radius:20px}
.badge.on{background:#1b3a24;color:#66bb6a}.badge.off{background:#3a1b1b;color:#ef5350}
.tip{font-size:11px;color:#9fc3e8;background:#1b2430;border-left:3px solid #3a7ab5;padding:6px 10px;border-radius:4px;margin-bottom:10px;line-height:1.5}
.cfg-panel .desc{font-size:12px;color:#8a9099;margin-bottom:10px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center}
.btn{padding:6px 14px;border-radius:6px;border:1px solid #3a7ab5;background:#2d5d8a;color:#fff;cursor:pointer;font-size:12px}
.btn:hover{background:#3a7ab5}
.btn.gray{background:#3a4149;border-color:#4a525c}.btn.gray:hover{background:#4a525c}
.btn.red{border-color:#8a2d2d;background:#6b2323}.btn.red:hover{background:#8a2d2d}
.inp{padding:6px 10px;border-radius:6px;border:1px solid #3a4149;background:#161a20;color:#e8eaed;font-size:12px;width:110px}
.inp.wide{width:180px}
.val{font-family:monospace;font-size:12px;color:#9fc3e8;background:#161a20;padding:4px 8px;border-radius:6px;display:inline-block;margin:2px}
.list{max-height:150px;overflow-y:auto;margin-top:8px}
.list .item{display:flex;justify-content:space-between;align-items:center;padding:4px 6px;border-bottom:1px solid #2a3038;font-size:12px}
.msg{position:fixed;bottom:20px;right:20px;background:#2d5d8a;color:#fff;padding:10px 18px;border-radius:8px;display:none;font-size:13px;z-index:9}
</style></head><body>
<h1>AX3000E 监控 + 配置中心</h1>
<div class="sub">SSH: %HOST% · 所有修改即时生效（操作有确认提示）· 温度阈值：绿&lt;85 / 橙85-90 / 红&gt;90</div>
<div class="tabs">
 <div class="tab active" id="tb-mon" onclick="switchTab('mon')">性能监控</div>
 <div class="tab" id="tb-cfg" onclick="switchTab('cfg')">配置中心</div>
</div>
<div id="tab-mon">
<div class="cards">
 <div class="card"><div class="v" id="c-cpu">--</div><div class="l">CPU 使用率</div><div class="s" id="s-cpu"></div></div>
 <div class="card"><div class="v" id="c-mem">--</div><div class="l">内存 (已用/总)</div><div class="s" id="s-mem"></div></div>
 <div class="card"><div class="v" id="c-temp">--</div><div class="l">温度</div><div class="s" id="s-temp"></div></div>
 <div class="card"><div class="v" id="c-rx">--</div><div class="l">下行</div><div class="s" id="s-rx"></div></div>
 <div class="card"><div class="v" id="c-tx">--</div><div class="l">上行</div><div class="s" id="s-tx"></div></div>
 <div class="card"><div class="v" id="c-conn">--</div><div class="l">TCP 连接</div><div class="s" id="s-conn"></div></div>
</div>
<div class="grid">
 <div class="panel"><h3>CPU 使用率 <span id="t-cpu" style="color:#4fc3f7"></span></h3><canvas id="g-cpu"></canvas></div>
 <div class="panel"><h3>内存 <span id="t-mem" style="color:#81c784"></span></h3><canvas id="g-mem"></canvas></div>
 <div class="panel"><h3>温度 <span id="t-temp" style="color:#ffb74d"></span></h3><canvas id="g-temp"></canvas></div>
 <div class="panel"><h3>流量 <span id="t-net" style="color:#f06292"></span></h3><canvas id="g-net"></canvas></div>
</div>
</div>
<div id="tab-cfg" style="display:none"><div class="cfg-grid" id="cfg-grid">加载中...</div></div>
<div class="msg" id="msg"></div>
<script>
function switchTab(t){
 document.getElementById('tab-mon').style.display=t==='mon'?'':'none';
 document.getElementById('tab-cfg').style.display=t==='cfg'?'':'none';
 document.getElementById('tb-mon').className='tab'+(t==='mon'?' active':'');
 document.getElementById('tb-cfg').className='tab'+(t==='cfg'?' active':'');
 if(t==='cfg')loadCfg(0);
}
function showMsg(t){var m=document.getElementById('msg');m.textContent=t;m.style.display='block';setTimeout(function(){m.style.display='none';},3000);}
function initCanvas(id){var cv=document.getElementById(id),dpr=window.devicePixelRatio||1;cv.width=560*dpr;cv.height=150*dpr;cv.style.width='100%';cv.style.height='150px';return cv;}
function draw(id,data,color,fill,ymax,unit){
 var cv=initCanvas(id),ctx=cv.getContext('2d'),dpr=window.devicePixelRatio||1;
 ctx.setTransform(dpr,0,0,dpr,0,0);var w=cv.width/dpr,h=cv.height/dpr;ctx.clearRect(0,0,w,h);
 if(!data||data.length<2)return;
 var mx=ymax||Math.max.apply(null,data)*1.2;if(mx<=0)mx=1;
 ctx.font='10px sans-serif';ctx.textBaseline='middle';
 for(var g=0;g<=3;g++){var gy=h-4-(g/3)*(h-14);ctx.strokeStyle='#2e343d';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(28,gy);ctx.lineTo(w,gy);ctx.stroke();ctx.fillStyle='#8a9099';ctx.fillText(((mx*g/3)>=100?Math.round(mx*g/3):(mx*g/3).toFixed(mx>=20?0:1))+(unit||''),2,gy);}
 ctx.strokeStyle=color;ctx.lineWidth=2;ctx.lineJoin='round';ctx.beginPath();
 for(var i=0;i<data.length;i++){var x=30+(i/(data.length-1))*(w-34),y=h-4-(data[i]/mx)*(h-14);if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);}
 ctx.stroke();
 if(fill){ctx.lineTo(w-4,h-4);ctx.lineTo(30,h-4);ctx.closePath();ctx.fillStyle=color+'33';ctx.fill();}
 var lx=w-4,ly=h-4-(data[data.length-1]/mx)*(h-14);
 ctx.beginPath();ctx.arc(lx,ly,3,0,7);ctx.fillStyle=color;ctx.fill();
 ctx.font='bold 12px sans-serif';ctx.fillStyle='#e8eaed';
 var txt=(data[data.length-1]>=100?Math.round(data[data.length-1]):data[data.length-1].toFixed(1))+(unit||'');
 ctx.fillText(txt,lx-70,ly>20?ly-8:ly+14);
}
function fmtKB(k){return k>=1024?(k/1024).toFixed(1)+' MB/s':Math.round(k)+' KB/s';}
function refresh(){
 fetch('/api').then(function(r){return r.json();}).then(function(d){
  var L=d.latest;
  document.getElementById('c-cpu').textContent=L.cpu+'%';
  var sc=document.getElementById('s-cpu');sc.textContent=L.cpu<50?'空闲':(L.cpu<85?'中载':'高载');sc.className='s '+(L.cpu<50?'ok':(L.cpu<85?'warn':'bad'));
  var mu=L.mem_used_mb/1024,mt=L.mem_total_mb/1024;
  var pct=L.mem_total_mb>0?Math.round(L.mem_used_mb/L.mem_total_mb*100):0;
  document.getElementById('c-mem').textContent=mu.toFixed(1)+'/'+mt.toFixed(1)+' GB';
  var sm=document.getElementById('s-mem');sm.textContent=pct+'% 已用';sm.className='s '+(pct<70?'ok':(pct<90?'warn':'bad'));
  document.getElementById('c-temp').textContent=L.temp+' °C';
  var st=document.getElementById('s-temp');st.textContent=L.temp<85?'正常':(L.temp<90?'偏热':'过热');st.className='s '+(L.temp<85?'ok':(L.temp<90?'warn':'bad'));
  document.getElementById('c-rx').textContent=fmtKB(L.rx);
  document.getElementById('c-tx').textContent=fmtKB(L.tx);
  document.getElementById('c-conn').textContent=L.conn;
  document.getElementById('t-cpu').textContent='当前 '+L.cpu+'%';
  document.getElementById('t-mem').textContent='已用 '+mu.toFixed(1)+' GB / '+mt.toFixed(1)+' GB';
  document.getElementById('t-temp').textContent='当前 '+L.temp+'°C';
  document.getElementById('t-net').textContent='↓'+fmtKB(L.rx)+' ↑'+fmtKB(L.tx);
  draw('g-cpu',d.cpu,'#4fc3f7',true,100,'%');
  draw('g-mem',d.mem_used_mb,'#81c784',true,null,'MB');
  draw('g-temp',d.temp,'#ffb74d',true,null,'°C');
  var sum=d.rx.map(function(v,i){return v+(d.tx[i]||0);});
  draw('g-net',sum,'#f06292',true,null,'KB/s');
 }).catch(function(){});
}
setInterval(refresh,2000);refresh();
setTimeout(function(){showMsg('✅ 已连接');},300);

function badge(on){return '<span class="badge '+(on?'on':'off')+'">'+(on?'已开':'已关')+'</span>';}
function tip(t){return '<div class="tip">💡 建议：'+t+'</div>';}
function panel(title,badgeHtml,tipHtml,desc,bodyHtml,btns){return '<div class="cfg-panel"><h3>'+title+' '+badgeHtml+'</h3>'+tipHtml+'<div class="desc">'+desc+'</div>'+bodyHtml+'<div class="row">'+btns+'</div></div>';}
function btn(text,cls,data,confirmTxt){
 var b='<button class="btn '+cls+'" data-act="'+data.act+'"';
 ['server','domain','id','size'].forEach(function(k){if(data[k])b+=' data-'+k+'="'+data[k]+'"';});
 ['inp','inp2','inp3','inp4','inp5'].forEach(function(k){if(data[k])b+=' data-'+k+'="'+data[k]+'"';});
 if(confirmTxt)b+=' data-confirm="'+confirmTxt+'"';
 b+='>'+text+'</button>';
 return b;
}
document.addEventListener('click',function(e){
 var b=e.target.closest('[data-act]');
 if(!b||b.disabled)return;
 var params={};
 ['server','domain','id','size'].forEach(function(k){if(b.dataset[k])params[k]=b.dataset[k];});
 ['inp','inp2','inp3','inp4','inp5'].forEach(function(k){
  if(b.dataset[k]){var el=document.getElementById(b.dataset[k]);if(el)params[k.replace('inp','')]=el.value;}
 });
 var actName=b.dataset.act;
 var LONG={antiad_update:'更新中…',adblock_update:'更新中…',dns_fastest:'测速优选中…',dns_speedtest:'测速中…',backup:'备份中…',restore:'恢复中…'};
 var busy=LONG[actName];
 if(b.dataset.confirm){if(confirm(b.dataset.confirm)){if(b.dataset.confirmValue)params.confirm=b.dataset.confirmValue;}else return;}
 if(busy){b.disabled=true;b.dataset.orig=b.textContent;b.textContent=busy;}
 fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:actName,params:params})})
 .then(function(r){return r.json();}).then(function(d){
  showMsg(d.ok?('✅ '+d.msg):('❌ '+(d.error||'失败')));
  if(d.ok)loadCfg(0);
  if(busy){b.disabled=false;b.textContent=b.dataset.orig;}
 }).catch(function(){
  showMsg('操作失败：连接中断');
  if(busy){b.disabled=false;b.textContent=b.dataset.orig;}
 });
});
function E(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function renderCfgBody(d){
 var g=document.getElementById('cfg-grid'),h='';
 if(d.ok===false){g.textContent='服务器错误: '+d.error;return;}
  // DNS 上游
  var ups='';d.dns_upstreams.forEach(function(s){ups+='<div class="item"><span class="val">'+E(s)+'</span>'+btn('删除','red',{act:'dns_del',server:s},'删除 '+E(s)+' 吗？')+'</div>';});
  h+=panel('DNS 上游','','国内优先（阿里/腾讯/电信），海外备选。填 IP 即可。',
    '当前 '+d.dns_upstreams.length+' 个上游',
    '<div class="row"><input class="inp" id="dns-add" placeholder="例: 223.5.5.5"><button class="btn" id="b-add">添加</button></div><div class="list">'+ups+'</div>',
    '<button class="btn gray" data-act="dnsmasq_restart">重启 DNS 服务</button><button class="btn gray" data-act="log_toggle" data-confirm="'+(c.log_queries?'关闭后 dnsmasq 不再记录查询，降低 CPU 开销。确认？':'开启后将记录全部查询（写入 tmpfs）。确认？')+'">'+(c.log_queries?'关闭查询日志':'开启查询日志')+'</button>');
  // DNS 缓存
  h+=panel('DNS 缓存','','默认 150 太小，已设为 1024。越大缓存命中率越高，一般 512-2048 合适，别超 4096。',
    '当前 cache-size = '+d.cache_size,
    '<div class="row"><input class="inp" id="cache-inp" value="'+d.cache_size+'">'+btn('保存','',{act:'cache_set',inp:'cache-inp'})+'</div>','');
  // 自定义屏蔽
  var cust='';d.custom_adblock.forEach(function(x){cust+='<div class="item"><span class="val">'+E(x)+'</span>'+btn('解除','red',{act:'ad_custom_del',domain:x},'解除屏蔽 '+E(x)+' 吗？')+'</div>';});
  h+=panel('自定义屏蔽域名','','去广告列表没覆盖的域名，手动加这里。填域名如 ads.example.com（不含 http）。',
    '已屏蔽 '+d.custom_adblock.length+' 个',
    '<div class="row"><input class="inp wide" id="cust-inp" placeholder="例: ads.example.com">'+btn('屏蔽','',{act:'ad_custom_add',inp:'cust-inp'})+'</div><div class="list">'+cust+'</div>','');
  // 去广告
  var adOn=d.adblock_enabled;
  h+=panel('去广告 (hagezi+yhosts)',badge(adOn),'hagezi 每日更新（国外+跟踪），yhosts 国内广告。更新按钮手动拉最新列表。',
    'hagezi '+d.adblock_antiad+' 条 + yhosts '+d.adblock_yhosts+' 条',
    '','<button class="btn" data-act="adblock_toggle" data-confirm="确定'+(adOn?'关闭':'开启')+'去广告吗？">'+(adOn?'关闭':'开启')+'</button><button class="btn" data-act="antiad_update">更新列表</button>');
  // UPnP
  h+=panel('UPnP',badge(d.upnp),'自动端口映射（P2P/游戏语音用）。速率限制单位 KB/s，建议保持默认。',
    '速率限制 下行 '+d.upnp_download+' / 上行 '+d.upnp_upload+' KB/s',
    '<div class="row"><input class="inp" id="up-dl" value="'+d.upnp_download+'"> <input class="inp" id="up-ul" value="'+d.upnp_upload+'">'+btn('设置','',{act:'upnp_rate',inp2:'up-dl',inp3:'up-ul'})+'</div>',
    '<button class="btn" data-act="upnp_toggle" data-confirm="确定'+(d.upnp?'关闭':'开启')+' UPnP 吗？">'+(d.upnp?'关闭':'开启')+'</button>');
  // QoS
  var q=d.qos==='1';
  h+=panel('QoS 限速',badge(q),'注意：此 QoS 只管连本路由器的设备，家人设备在 K2P 下无效（需 K2P 限速）。带宽填 0=自动。',
    '带宽 下行 '+d.qos_down+' / 上行 '+d.qos_up,
    '<div class="row"><input class="inp" id="q-dl" value="'+d.qos_down+'"> <input class="inp" id="q-ul" value="'+d.qos_up+'">'+btn('设置','',{act:'qos_band',inp2:'q-dl',inp3:'q-ul'})+'</div>',
    '<button class="btn" data-act="qos_toggle" data-confirm="确定'+(q?'关闭':'开启')+' QoS 吗？">'+(q?'关闭':'开启')+'</button>');
  // 端口转发
  var pf='';d.port_forwards.forEach(function(x){pf+='<div class="item"><span class="val">'+E(x.name)+'</span><span class="val">'+E(x.src_dport)+'→'+E(x.dest_ip)+':'+E(x.dest_port)+' ('+E(x.proto)+')</span>'+btn('删除','red',{act:'port_del',id:x.id},'删除该规则吗？')+'</div>';});
  h+=panel('端口转发',d.port_forwards.length+' 条','外网访问内网设备用。例：CS2 服务器 UDP 27015 → 电脑内网IP:27015。',
    '外部端口 → 内网IP:内端口',
    '<div class="row"><input class="inp" id="pf-name" placeholder="名称"><input class="inp" id="pf-ext" placeholder="外端口"><input class="inp" id="pf-ip" placeholder="内网IP"><input class="inp" id="pf-int" placeholder="内端口"><select class="inp" id="pf-proto"><option>tcp</option><option>udp</option><option>tcpudp</option></select>'+btn('添加','',{act:'port_add',inp:'pf-name',inp2:'pf-ext',inp3:'pf-ip',inp4:'pf-int',inp5:'pf-proto'})+'</div><div class="list">'+pf+'</div>','');
  // DHCP 租期
  h+=panel('DHCP 租期','','设备 IP 租用时长。默认 12h 即可，设备多可改短。',
    '当前 '+d.dhcp_lease,
    '<div class="row"><input class="inp" id="lease-inp" value="'+d.dhcp_lease+'">'+btn('保存','',{act:'dhcp_lease',inp:'lease-inp'})+'</div>','');
  // SSH / 系统
  // WiFi 信道（即时切换，官方接口）
  var asel='<select class="inp" id="a-ch">';
  var achans=[36,40,44,48,149,153,157,161];
  for(var ai=0;ai<achans.length;ai++){var at=(achans[ai]===36||achans[ai]===149)?' (推荐)':'';asel+='<option value="'+achans[ai]+'"'+(d.wifi.a_channel===String(achans[ai])?' selected':'')+'>'+achans[ai]+at+'</option>';}
  asel+='</select>';
  var gsel='<select class="inp" id="g-ch">';
  var gchans=[1,6,11,3,9,13];
  for(var gi=0;gi<gchans.length;gi++){var gt=(gi<3)?' (推荐)':'';gsel+='<option value="'+gchans[gi]+'"'+(d.wifi.g_channel===String(gchans[gi])?' selected':'')+'>'+gchans[gi]+gt+'</option>';}
  gsel+='</select>';
  h+=panel('WiFi 信道','','信道即时切换（官方接口，不断网）。5G 推荐 36/149（避开雷达）；2.4G 推荐 1/6/11。注意：重启后恢复自动，持久设置请在小米管理页。',
    '5G: '+E(d.wifi.a_ssid)+' 当前信道'+(d.wifi.a_channel==='0'?'自动':E(d.wifi.a_channel))+' (频宽160MHz)<br>2.4G: '+E(d.wifi.g_ssid)+' 当前信道'+(d.wifi.g_channel==='0'?'自动':E(d.wifi.g_channel))+'',
    '<div class="row">5G 信道 '+asel+'<button class="btn" data-act="wifi_channel" data-band="5g" data-inp="a-ch">切换5G信道</button></div>'+
    '<div class="row">2.4G 信道 '+gsel+'<button class="btn" data-act="wifi_channel" data-band="2g" data-inp="g-ch">切换2.4G信道</button></div>',
    '');
  h+=panel('SSH 解锁',badge(d.ssh),'root · 开机自愈 '+(d.auto_ssh?'已开':'已关')+'。别升级固件（1.0.24）否则全丢。','','','');
  // 设备管理
  var dv='';d.devices.forEach(function(x){dv+='<div class="item"><span class="val">'+E(x.host)+'</span><span class="val">'+E(x.ip)+'</span><span class="val">'+E(x.mac)+'</span></div>';});
  h+=panel('在线设备',d.devices.length+' 台','当前 DHCP 分配的设备','<div class="list">'+dv+'</div>','');
  // 静态绑定
  var bd='';d.binds.forEach(function(x){bd+='<div class="item"><span class="val">'+E(x.name)+'</span><span class="val">'+E(x.mac)+'</span><span class="val">'+E(x.ip)+'</span><button class="btn red" data-act="device_unbind" data-id="'+E(x.id)+'" data-confirm="解除绑定？">解绑</button></div>';});
  h+=panel('静态 IP 绑定','','把设备固定为指定 IP（端口转发前提）。填设备的 MAC 和想固定的 IP。','<div class="row"><input class="inp" id="bd-mac" placeholder="MAC 如 aa:bb:cc:dd:ee:ff"><input class="inp" id="bd-ip" placeholder="IP 如 192.168.31.50"><input class="inp" id="bd-name" placeholder="名称(可选)">'+btn('绑定','',{act:'device_bind',inp:'bd-mac',inp2:'bd-ip',inp3:'bd-name'})+'</div><div class="list">'+bd+'</div>','');
  // 定时任务
  var ct='';d.cron_tasks.forEach(function(x){ct+='<div class="item"><span class="val">'+E(x)+'</span><button class="btn red" data-act="cron_del" data-line="'+E(x)+'" data-confirm="删除该任务？">删</button></div>';});
  h+=panel('定时任务','','格式: 分 时 日 月 周 命令。例 "0 4 * * * reboot" = 每天4点重启路由器。','<div class="row"><input class="inp" id="cr-s" placeholder="如 0 4 * * *"><input class="inp wide" id="cr-c" placeholder="命令 如 reboot"><button class="btn" data-act="cron_add" data-inp="cr-s" data-inp2="cr-c">添加</button></div><div class="list">'+ct+'</div>','');
  // 系统操作
  // LED + 备份 + Guest
  // 性能优化（高级）
  var perfDesc='DNS 缓存 '+d.perf_cache+' 条 · 连接跟踪 '+d.perf_conntrack+' · 网卡队列 '+d.perf_backlog;
  h+=panel('性能优化','','对游戏/网页有帮助的深度优化：DNS 上游测速排序（解析更快）、WiFi 功率即时调整（信号强度/省电）。硬件 NAT 已启用。',
    '硬件 NAT: 已启用 (NSS 加速) · ' + perfDesc,
    '<div class="row"><button class="btn" data-act="dns_speedtest">DNS 测速</button><button class="btn green" data-act="dns_fastest" data-confirm="用最快的4个上游并重启DNS？">一键用最快</button></div>'+
    '<div class="row">5G 功率 <select class="inp" id="pw5"><option value="28">28dBm 满</option><option value="24">24</option><option value="20">20</option><option value="16">16</option><option value="12">12</option><option value="8">8</option></select><button class="btn" data-act="wifi_power" data-band="5g" data-inp="pw5">设5G功率</button></div>'+
    '<div class="row">2.4G 功率 <select class="inp" id="pw2"><option value="28">28dBm 满</option><option value="24">24</option><option value="20">20</option><option value="16">16</option><option value="12">12</option><option value="8">8</option></select><button class="btn" data-act="wifi_power" data-band="2g" data-inp="pw2">设2.4G功率</button></div>',
    '<button class="btn green" data-act="perf_optimize" data-confirm="一键优化路由器参数：DNS缓存→2048、连接跟踪→32768、网卡队列→2048、TCP缓冲区扩大。确认？">一键路由器优化</button>');
  // 防火墙规则（高级）
  var fr='';
  d.fw_rules.forEach(function(x){
    var col=(x.target==='DROP'||x.target==='REJECT')?'red':'green';
    fr+='<div class="item"><span class="val '+col+'">'+x.target+'</span><span class="val">'+x.name+'</span><span class="val">'+(x.src||'any')+(x.dest_port?':'+x.dest_port:'')+'/'+(x.proto||'all')+'</span><button class="btn red" data-act="fw_rule_del" data-id="'+x.id+'" data-confirm="删除规则？">删</button></div>';
  });
  h+=panel('防火墙规则',''+d.fw_rules.length+' 条','高级：按 IP/端口/协议 允许(ACCEPT)或拒绝(DROP/REJECT)流量。例：拒绝某设备访问外网 = DROP + 来源IP填设备IP；屏蔽某端口 = DROP + 目标端口。','<div class="row"><input class="inp" id="fw-name" placeholder="名称"><select class="inp" id="fw-target"><option value="DROP">拒绝 DROP</option><option value="ACCEPT">允许 ACCEPT</option><option value="REJECT">拒绝 REJECT</option></select></div><div class="row"><input class="inp" id="fw-src" placeholder="来源IP(空=所有)"><input class="inp" id="fw-port" placeholder="目标端口(空=所有)"><select class="inp" id="fw-proto"><option value="">协议:全部</option><option value="tcp">TCP</option><option value="udp">UDP</option><option value="tcp udp">TCP+UDP</option></select>'+btn('添加规则','',{act:'fw_rule_add',inp:'fw-name',sel:'fw-target',inp2:'fw-src',inp3:'fw-port',sel2:'fw-proto'})+'</div><div class="list">'+fr+'</div>','');
  h+=panel('LED 指示灯',d.led_blue?'亮':'灭','关闭指示灯（路由器灯灭，不影响功能）','','<button class="btn" data-act="led_toggle">'+(d.led_blue?'关闭':'开启')+'</button>');
  h+=panel('配置备份','','配置存在路由器 /etc/config/（67 个文件）。重启/升级前建议先备份。','','<button class="btn gray" data-act="backup">查看配置摘要</button>');
  h+=panel('Guest 访客网络','','访客 2.4G: '+d.guest_wifi['2g']+' / 5G: '+d.guest_wifi['5g']+'。开启访客网络请用小米管理页 192.168.31.1（本面板不做 wifi 写入避免断网风险）。','','');
  h+=panel('系统操作','','重启路由器(2秒后执行) · 需等待约2分钟恢复','','<button class="btn red" data-act="reboot" data-confirm="确定重启路由器？约2分钟断网" data-confirm-value="yes">重启路由器</button><button class="btn gray" data-act="dnsmasq_restart">重启DNS</button>');
  h+=panel('系统','','运行 '+d.uptime+' · 温度 '+d.temp+'°C · WiFi: '+d.wifi.ssid+' (信道'+d.wifi.channel+')','','','');
  g.innerHTML=h;
  document.getElementById('b-add').onclick=function(){var v=document.getElementById('dns-add').value;if(v){if(confirm('添加 DNS '+v+' 吗？'))post({action:'dns_add',params:{server:v}});}};
}
function loadCfg(retry){
 retry=retry||0;
 if(window.__CFG__){var d=window.__CFG__;window.__CFG__=null;renderCfgBody(d);return;}
 fetch('/api/config').then(function(r){return r.json();}).then(function(d){renderCfgBody(d);}).catch(function(){if(retry<3){setTimeout(function(){loadCfg(retry+1);},2000);}else{document.getElementById('cfg-grid').textContent='加载失败，请检查连接（已重试3次）';}});
}
function post(body){
 fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
 .then(function(r){return r.json();}).then(function(d){
  showMsg(d.ok?('✅ '+d.msg):('❌ '+(d.error||'失败')));
  if(d.ok)loadCfg(0);
 }).catch(function(){showMsg('操作失败：连接中断');});
}
</script></body></html>
"""


# ---------- 服务层（与 AP 面板共享 monitor_web，认证/校验单一来源） ----------


def get_health():
    """一键体检（面板版）：单次 SSH 往返完成全部检查，返回 [{icon, title, detail}]。"""
    items = []
    def add(icon, title, detail=""):
        items.append({"icon": icon, "title": title, "detail": detail})
    raw = sh(
        "curl -s --max-time 3 http://127.0.0.1/cgi-bin/luci/api/xqsystem/init_info 2>/dev/null; echo @@;"
        "curl -s --max-time 3 http://127.0.0.1/cgi-bin/luci/api/xqsystem/upgrade_status 2>/dev/null; echo @@;"
        "uci get otapred.settings.auto 2>/dev/null; echo @@;"
        "test -f /data/auto_ssh/auto_ssh.sh && grep -c auto_ssh /etc/crontabs/root 2>/dev/null || echo 0; echo @@;"
        "test -f /tmp/dnsmasq.d/96-antiad.conf && echo y || (test -f /tmp/dnsmasq.d/hagezi.conf && echo y || echo n); echo @@;"
        "df /data | tail -n 1; echo @@;"
        "ps w | grep -E 'messagingagent|mosquitto|xq_info_sync_mqtt' | grep -v grep | wc -l", timeout=20)
    parts = raw.split("@@")
    def seg(i):
        return parts[i].strip() if i < len(parts) else ""
    if not seg(0):
        add("❌", "路由器连接", "SSH 不通")
        return items
    add("✅", "路由器连接", "SSH 在线")
    import json
    try:
        info = json.loads(seg(0))
        rv = info.get("romversion", "?")
        add("✅" if rv == "1.0.24" else "⚠️", "固件版本", "%s（%s）" % (rv, "实测基准" if rv == "1.0.24" else "校准后可用"))
        if seg(1):
            u = json.loads(seg(1))
            add("✅" if u.get("status") == 0 else "⚠️", "固件升级",
                "无新版本" if u.get("status") == 0 else "告警：有新固件，切勿升级")
    except Exception:
        add("ℹ️", "固件版本", "读取失败")
    auto = seg(2)
    add("✅" if auto == "0" else "⚠️", "自动升级", "已关闭" if auto == "0" else "仍开启")
    heal = seg(3)
    add("✅" if heal.isdigit() and int(heal) >= 1 else "⚠️",
        "三层自愈", "已安装" if heal.isdigit() and int(heal) >= 1 else "未装全")
    ad = seg(4)
    add("✅" if ad == "y" else "⚠️", "去广告列表", "已加载" if ad == "y" else "未加载")
    df = seg(5).split()
    free = df[-3] if len(df) >= 4 else "?"
    ok = free.isdigit() and int(free) >= 200
    add("✅" if ok else "⚠️", "/data 容量", "%sK 剩余" % free)
    svc = seg(6)
    add("✅" if svc.strip() == "0" else "ℹ️", "米家云服务", "已精简" if svc.strip() == "0" else "运行中")
    return items


def get_ad_stats():
    """去广告统计：列表规模、今日拦截数、拦截率。单次 SSH 往返。"""
    raw = sh(
        "wc -l /tmp/dnsmasq.d/96-antiad.conf /tmp/dnsmasq.d/hagezi.conf 2>/dev/null | tail -n 1; echo @@;"
        "grep -c 'is 0\\.0\\.0\\.0' /tmp/dnsquery.log 2>/dev/null || echo 0; echo @@;"
        "grep -c 'query\\[' /tmp/dnsquery.log 2>/dev/null || echo 0; echo @@;"
        "grep -c cached /tmp/dnsquery.log 2>/dev/null || echo 0", timeout=15)
    parts = raw.split("@@")
    def seg(i):
        return parts[i].strip() if i < len(parts) else ""
    total = seg(0).split()[0] if seg(0) else "0"
    blocked = seg(1)
    queries = seg(2)
    cached = seg(3)
    rate = 0
    if queries.isdigit() and int(queries) > 0:
        rate = round(int(blocked or "0") * 100.0 / int(queries) * 100, 1)
    return {"total_domains": total, "blocked_today": blocked or "0",
            "total_queries": queries or "0", "cached": cached or "0", "block_rate": rate}


def api_snapshot():
    with data_lock:
        return {"cpu": list(history["cpu"]), "mem_used_mb": list(history["mem_used_mb"]),
                "mem_total_mb": list(history["mem_total_mb"]), "temp": list(history["temp"]),
                "rx": list(history["rx"]), "tx": list(history["tx"]), "conn": list(history["conn"]),
                "latest": {"cpu": history["cpu"][-1] if history["cpu"] else 0,
                           "mem_used_mb": history["mem_used_mb"][-1] if history["mem_used_mb"] else 0,
                           "mem_total_mb": history["mem_total_mb"][-1] if history["mem_total_mb"] else 0,
                           "temp": history["temp"][-1] if history["temp"] else 0,
                           "rx": history["rx"][-1] if history["rx"] else 0,
                           "tx": history["tx"][-1] if history["tx"] else 0,
                           "conn": history["conn"][-1] if history["conn"] else 0,
                           "collect_ms": collect_ms,
                           "ssh_ok": collect_fails < 3}}


def _page():
    """每请求渲染主页：读模板（%HOST% 占位），服务端零 SSH。配置由前端 loadCfg() 经 /api/config 拉取。"""
    return PAGE.replace("%HOST%", HOST).encode("utf-8")


def main():
    global HOST, SSHPORT, USER, PASSWD, WEBPORT
    ap = argparse.ArgumentParser(description="小米路由器 主路由模式 监控+配置中心")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=SSHPORT)
    ap.add_argument("--user", default=USER)
    ap.add_argument("--passwd", default=PASSWD)
    ap.add_argument("--web", type=int, default=WEBPORT)
    ap.add_argument("--lan", action="store_true",
                    help="绑定所有网卡允许局域网访问（必须同时提供 --token）")
    ap.add_argument("--token", default=os.environ.get("ROUTER_PANEL_TOKEN", ""),
                    help="面板访问令牌（HTTP Basic，用户名任意、密码为令牌）")
    a = ap.parse_args()
    HOST, SSHPORT, USER, PASSWD, WEBPORT = a.host, a.port, a.user, a.passwd, a.web
    if a.lan and not a.token:
        print("[!] --lan 会暴露到局域网，必须设置 --token（或环境变量 ROUTER_PANEL_TOKEN）")
        return

    threading.Thread(target=collector_loop, daemon=True).start()
    time.sleep(2)
    print("AX3000E 主路由版监控+配置中心(中文): http://127.0.0.1:" + str(WEBPORT))
    print("  路由器: " + USER + "@" + HOST + ":" + str(SSHPORT) + "  (Ctrl+C 停止)")
    monitor_web.serve({"webport": WEBPORT, "lan": a.lan, "token": a.token,
                       "page": _page, "api": api_snapshot,
                       "get_config": get_config, "do_action": do_action,
                       "health_check": get_health, "ad_stats": get_ad_stats})


if __name__ == "__main__":
    main()
