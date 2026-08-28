# -*- coding: utf-8 -*-
"""
AX3000E 监控 + 配置中心 v5.1（中文版）
- 性能监控: CPU/内存/温度/流量/连接数
- 配置中心: 全中文 + 每项带建议设置说明，事件委托实现
用法: python router_monitor.py
"""
import sys, os, json, time, threading, argparse, re, base64, hmac
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from collections import deque

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
    now = time.time()
    # 单次 SSH 拉回全部采集数据(替代 5 次独立往返), WAN 口用 eth1.4(主路由)
    raw = sh(
        "head -1 /proc/stat; echo '@@'; "
        "grep -E 'MemTotal|MemFree|Buffers|^Cached' /proc/meminfo; echo '@@'; "
        "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null; echo '@@'; "
        "grep eth1.4 /proc/net/dev; echo '@@'; "
        "cat /proc/net/tcp | wc -l"
    )
    cpu_pct = 0.0
    parts = raw.split("@@")
    if len(parts) < 5:
        return

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
    while True:
        try:
            collect()
        except Exception:
            pass
        time.sleep(INTERVAL)


def get_config():
    cfg = {}
    up = sh("cat /tmp/dnsmasq.d/98-upstream.conf 2>/dev/null")
    cfg["dns_upstreams"] = [l.replace("server=", "").strip() for l in up.splitlines() if l.startswith("server=")]
    cfg["cache_size"] = sh("uci get dhcp.@dnsmasq[0].cachesize 2>/dev/null") or "150"
    # 去广告: 兼容 hagezi 或 anti-AD 两种列表(取行数)
    hz = sh("wc -l /tmp/dnsmasq.d/hagezi.conf 2>/dev/null").split()[0] if sh("test -f /tmp/dnsmasq.d/hagezi.conf && echo y") == "y" else "0"
    ad = sh("wc -l /tmp/dnsmasq.d/96-antiad.conf 2>/dev/null").split()[0] if sh("test -f /tmp/dnsmasq.d/96-antiad.conf && echo y") == "y" else "0"
    cfg["adblock_antiad"] = max(int(hz or "0"), int(ad or "0"))
    cfg["adblock_yhosts"] = sh("wc -l /data/adblock.hosts 2>/dev/null").split()[0] if sh("test -f /data/adblock.hosts && echo y") == "y" else 0
    cfg["adblock_enabled"] = "99-adblock.conf" in sh("ls /tmp/dnsmasq.d/ 2>/dev/null")
    custom = sh("cat /tmp/dnsmasq.d/97-custom.conf 2>/dev/null")
    cfg["custom_adblock"] = [re.sub(r"^address=/(.*)/.*$", r"\1", l).strip() for l in custom.splitlines() if l.startswith("address=/")]
    cfg["upnp"] = "miniupnpd" in sh("ps | grep miniupnpd | grep -v grep")
    cfg["upnp_download"] = sh("uci get upnpd.config.download 2>/dev/null")
    cfg["upnp_upload"] = sh("uci get upnpd.config.upload 2>/dev/null")
    cfg["ssh"] = "dropbear" in sh("ps | grep dropbear | grep -v grep")
    cfg["qos"] = sh("uci get miqos.settings.enabled 2>/dev/null")
    cfg["qos_up"] = sh("uci get miqos.settings.upload 2>/dev/null")
    cfg["qos_down"] = sh("uci get miqos.settings.download 2>/dev/null")
    cfg["auto_ssh"] = sh("test -f /data/auto_ssh/auto_ssh.sh && echo y") == "y"
    cfg["dhcp_lease"] = sh("uci get dhcp.lan.leasetime 2>/dev/null")
    cfg["uptime"] = sh("uptime").split(",")[0].strip() if sh("uptime") else ""
    cfg["temp"] = history["temp"][-1] if history["temp"] else 0
    cfg["port_forwards"] = []
    cur = {}
    for line in sh("uci show firewall 2>/dev/null | grep redirect | head -40").splitlines():
        m = re.match(r"firewall\.@redirect\[(\d+)\]\.(\w+)='(.*)'", line.strip())
        if m:
            idx, k, v = m.group(1), m.group(2), m.group(3)
            cur.setdefault(idx, {})[k] = v
    for idx, d in cur.items():
        if d.get("name") or d.get("dest_ip"):
            cfg["port_forwards"].append({"id": idx, "name": d.get("name", ""), "src_dport": d.get("src_dport", ""),
                                         "dest_ip": d.get("dest_ip", ""), "dest_port": d.get("dest_port", ""),
                                         "proto": d.get("proto", "")})
    # 设备列表（DHCP 租约）
    devices = []
    for line in sh("cat /tmp/dhcp.leases").splitlines():
        p = line.split()
        if len(p) >= 4:
            devices.append({"ip": p[2], "mac": p[1], "host": p[3]})
    cfg["devices"] = devices
    # 静态绑定（dhcp host）
    binds = []
    cur = {}
    for line in sh(r"uci show dhcp 2>/dev/null | grep 'host\['").splitlines():
        m = re.match(r"dhcp\.@host\[(\d+)\]\.(\w+)='(.*)'", line.strip())
        if m:
            cur.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    for idx, d in cur.items():
        binds.append({"id": idx, "mac": d.get("mac", ""), "ip": d.get("ip", ""), "name": d.get("name", "")})
    cfg["binds"] = binds
    # 用户定时任务（行尾 #panel 标记归属面板的 cron 行）
    crontab = sh("cat /etc/crontabs/root 2>/dev/null")
    cfg["cron_tasks"] = [l.rstrip()[:-7].strip() for l in crontab.splitlines()
                         if l.rstrip().endswith("#panel")]
    # 防火墙规则
    fw_rules = []
    cur = {}
    for line in sh(r"uci show firewall 2>/dev/null | grep '@rule\['").splitlines():
        m = re.match(r"firewall\.@rule\[(\d+)\]\.(\w+)='(.*)'", line.strip())
        if m:
            cur.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    for idx, d in cur.items():
        fw_rules.append({"id": idx, "name": d.get("name", ""), "target": d.get("target", d.get("action", "")),
                         "src": d.get("src_ip", d.get("src", "")), "dest_port": d.get("dest_port", ""),
                         "proto": d.get("proto", ""), "family": d.get("family", "")})
    cfg["fw_rules"] = fw_rules
    # LED 状态
    led_b = sh("uci get xiaoqiang.common.XLED 2>/dev/null")
    cfg["led_blue"] = led_b.strip() == "1"
    # Guest WiFi 状态（只读）
    g2 = sh("uci get wireless.guest_2G.disabled 2>/dev/null")
    g5 = sh("uci get wireless.guest_5G.disabled 2>/dev/null")
    cfg["guest_wifi"] = {"2g": "off" if g2 == "1" or g2 == "" else "on", "5g": "off" if g5 == "1" or g5 == "" else "on"}

    # WiFi 状态（单次 uci show 拉回本地解析, 替代 12 次独立 SSH）
    wraw = sh("uci show wireless 2>/dev/null")
    uci = {}
    for line in wraw.splitlines():
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


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def frm(action, params=None, confirm=None, fields=None, btn_txt="执行", btn_cls="btn"):
    """原生 form 按钮：导航级提交，零 JS 依赖"""
    p = json.dumps({"action": action, "params": params or {}}, ensure_ascii=False)
    p = p.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    h = '<form method="post" action="/api/act" style="display:inline">'
    h += '<input type="hidden" name="json" value="' + p + '">'
    for fname, fval in (fields or {}).items():
        h += '<input class="inp" name="' + fname + '" value="' + esc(fval) + '" style="width:auto">'
    h += '<button class="' + btn_cls + '" type="submit"'
    if confirm:
        h += ' onclick="return confirm(\'' + confirm.replace("'", "&#39;") + '\')"'
    h += '>' + btn_txt + '</button></form>'
    return h

def render_config_html(cfg):
    h = []
    # DNS 上游
    ups = ""
    for s in cfg.get("dns_upstreams", []):
        ups += '<div class="item"><span class="val">' + esc(s) + '</span>' + frm("dns_del", {"server": s}, confirm="删除 " + s + " 吗？", btn_txt="删除", btn_cls="btn red") + '</div>'
    h.append('<div class="cfg-panel"><h3>DNS 上游</h3><div class="tip">💡 建议：国内优先（阿里/腾讯/电信），海外备选。填 IP 即可。</div><div class="desc">当前 ' + str(len(cfg.get("dns_upstreams", []))) + ' 个上游</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;dns_add&quot;}"><input class="inp" name="server" placeholder="例: 223.5.5.5"><button class="btn" type="submit">添加</button></form>' +
             '<div class="list">' + ups + '</div><div class="row">' + frm("dnsmasq_restart", btn_txt="重启 DNS 服务", btn_cls="btn gray") + '</div></div>')
    # DNS 缓存
    h.append('<div class="cfg-panel"><h3>DNS 缓存</h3><div class="tip">💡 建议：默认 150 太小，已设为 1024。512-2048 合适，别超 4096。</div><div class="desc">当前 cache-size = ' + esc(cfg.get("cache_size", "")) + '</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;cache_set&quot;}"><input class="inp" name="size" value="' + esc(cfg.get("cache_size", "1024")) + '"><button class="btn" type="submit">保存</button></form></div>')
    # 自定义屏蔽
    cust = ""
    for d in cfg.get("custom_adblock", []):
        cust += '<div class="item"><span class="val">' + esc(d) + '</span>' + frm("ad_custom_del", {"domain": d}, confirm="解除屏蔽 " + d + " 吗？", btn_txt="解除", btn_cls="btn red") + '</div>'
    h.append('<div class="cfg-panel"><h3>自定义屏蔽域名</h3><div class="tip">💡 建议：去广告列表没覆盖的域名，手动加这里。填域名如 ads.example.com（不含 http）。</div><div class="desc">已屏蔽 ' + str(len(cfg.get("custom_adblock", []))) + ' 个</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;ad_custom_add&quot;}"><input class="inp" name="domain" placeholder="例: ads.example.com"><button class="btn" type="submit">屏蔽</button></form>' +
             '<div class="list">' + cust + '</div></div>')
    # 去广告
    ad_on = cfg.get("adblock_enabled", True)
    h.append('<div class="cfg-panel"><h3>去广告</h3><div class="tip">💡 建议：hagezi(4.2万) + yhosts(6428) 自动更新，开机自愈。</div><div class="desc"><span class="badge ' + ('on' if ad_on else 'off') + '">' + ('已开' if ad_on else '已关') + '</span> hagezi ' + esc(cfg.get("adblock_antiad", "")) + ' 条 / yhosts ' + esc(cfg.get("adblock_yhosts", "")) + ' 条</div>' +
             '<div class="row">' + frm("adblock_toggle", confirm="确定" + ("关闭" if ad_on else "开启") + "去广告吗？", btn_txt=("关闭" if ad_on else "开启") + "去广告") +
             frm("antiad_update", confirm="重新下载 hagezi 列表？", btn_txt="更新列表") + '</div></div>')
    # UPnP
    upnp = cfg.get("upnp", True)
    h.append('<div class="cfg-panel"><h3>UPnP</h3><div class="tip">💡 建议：保持开启，P2P/游戏语音自动映射端口。</div><div class="desc"><span class="badge ' + ('on' if upnp else 'off') + '">' + ('已开' if upnp else '已关') + '</span> 当前速率 下行 ' + esc(cfg.get("upnp_download", "")) + ' / 上行 ' + esc(cfg.get("upnp_upload", "")) + '</div>' +
             '<div class="row">' + frm("upnp_toggle", confirm="确定" + ("关闭" if upnp else "开启") + " UPnP 吗？", btn_txt=("关闭" if upnp else "开启") + " UPnP") + '</div></div>')
    # QoS
    q = cfg.get("qos", "1")
    h.append('<div class="cfg-panel"><h3>QoS</h3><div class="tip">💡 建议：拨号在 K2P，本路由 QoS 管不到下载设备，建议关闭。</div><div class="desc">当前 ' + ('已开' if q == "1" else "已关") + '（下行 ' + esc(cfg.get("qos_down", "")) + ' / 上行 ' + esc(cfg.get("qos_up", "")) + '）</div>' +
             '<div class="row">' + frm("qos_toggle", confirm="确定" + ("关闭" if q == "1" else "开启") + " QoS 吗？", btn_txt=("关闭" if q == "1" else "开启") + " QoS") + '</div></div>')
    # 端口转发
    pfs = ""
    for pf in cfg.get("port_forwards", []):
        pfs += '<div class="item"><span class="val">' + esc(pf.get("name", "")) + ' ' + esc(pf.get("proto", "")) + ' ' + esc(pf.get("src_dport", "")) + '→' + esc(pf.get("dest_ip", "")) + ':' + esc(pf.get("dest_port", "")) + '</span>' + frm("port_del", {"id": pf.get("id", "")}, confirm="删除端口转发？", btn_txt="删", btn_cls="btn red") + '</div>'
    h.append('<div class="cfg-panel"><h3>端口转发</h3><div class="tip">💡 建议：游戏服务器/远程访问用。外网端口 → 内网 IP:端口。</div><div class="desc">' + str(len(cfg.get("port_forwards", []))) + ' 条</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;port_add&quot;}"><input class="inp" name="name" placeholder="名称"><input class="inp" name="ext" placeholder="外网端口"><input class="inp" name="ip" placeholder="内网IP"><input class="inp" name="inner" placeholder="内网端口(可空)"><button class="btn" type="submit">添加</button></form>' +
             '<div class="list">' + pfs + '</div></div>')
    # DHCP 租期
    h.append('<div class="cfg-panel"><h3>DHCP 租期</h3><div class="tip">💡 建议：默认 12h 合适。</div><div class="desc">当前 ' + esc(cfg.get("dhcp_lease", "")) + '</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;dhcp_lease&quot;}"><input class="inp" name="lease" value="' + esc(cfg.get("dhcp_lease", "12h")) + '"><button class="btn" type="submit">保存</button></form></div>')
    # WiFi 信道
    w = cfg.get("wifi", {})
    a_ch = str(w.get("a_channel", "0"))
    g_ch = str(w.get("g_channel", "0"))
    asel = '<select class="inp" name="channel">'
    for ch in [36, 40, 44, 48, 149, 153, 157, 161]:
        asel += '<option value="' + str(ch) + '"' + (' selected' if a_ch == str(ch) else '') + '>' + str(ch) + (' (推荐)' if ch in (36, 149) else '') + '</option>'
    asel += '</select>'
    gsel = '<select class="inp" name="channel">'
    for ch in [1, 6, 11, 3, 9, 13]:
        gsel += '<option value="' + str(ch) + '"' + (' selected' if g_ch == str(ch) else '') + '>' + str(ch) + (' (推荐)' if ch in (1, 6, 11) else '') + '</option>'
    gsel += '</select>'
    h.append('<div class="cfg-panel"><h3>WiFi 信道</h3><div class="tip">💡 建议：信道即时切换（官方接口，不断网）。5G 推荐 36/149（避开雷达）；2.4G 推荐 1/6/11。重启后恢复自动。</div><div class="desc">5G: ' + esc(w.get("a_ssid", "")) + ' 信道' + (a_ch if a_ch != "0" else "自动") + ' · 2.4G: ' + esc(w.get("g_ssid", "")) + ' 信道' + (g_ch if g_ch != "0" else "自动") + '</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;wifi_channel&quot;,&quot;params&quot;:{&quot;band&quot;:&quot;5g&quot;}}">5G ' + asel + '<button class="btn" type="submit">切换5G信道</button></form>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;wifi_channel&quot;,&quot;params&quot;:{&quot;band&quot;:&quot;2g&quot;}}">2.4G ' + gsel + '<button class="btn" type="submit">切换2.4G信道</button></form></div>')
    # 在线设备
    dv = ""
    for x in cfg.get("devices", []):
        dv += '<div class="item"><span class="val">' + esc(x.get("host", "")) + '</span><span class="val">' + esc(x.get("ip", "")) + '</span><span class="val">' + esc(x.get("mac", "")) + '</span></div>'
    h.append('<div class="cfg-panel"><h3>在线设备</h3><div class="tip">💡 当前 DHCP 分配的设备。</div><div class="desc">' + str(len(cfg.get("devices", []))) + ' 台</div><div class="list">' + dv + '</div></div>')
    # 静态绑定
    bd = ""
    for x in cfg.get("binds", []):
        bd += '<div class="item"><span class="val">' + esc(x.get("name", "")) + '</span><span class="val">' + esc(x.get("mac", "")) + '</span><span class="val">' + esc(x.get("ip", "")) + '</span>' + frm("device_unbind", {"id": x.get("id", "")}, confirm="解除绑定？", btn_txt="解绑", btn_cls="btn red") + '</div>'
    h.append('<div class="cfg-panel"><h3>静态 IP 绑定</h3><div class="tip">💡 建议：把设备固定为指定 IP（端口转发前提）。填设备的 MAC 和想固定的 IP。</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;device_bind&quot;}"><input class="inp" name="mac" placeholder="MAC 如 aa:bb:cc:dd:ee:ff"><input class="inp" name="ip" placeholder="IP 如 192.168.31.50"><input class="inp" name="name" placeholder="名称(可选)"><button class="btn" type="submit">绑定</button></form>' +
             '<div class="list">' + bd + '</div></div>')
    # 定时任务
    ct = ""
    for t in cfg.get("cron_tasks", []):
        ct += '<div class="item"><span class="val">' + esc(t) + '</span>' + frm("cron_del", {"line": t}, confirm="删除该任务？", btn_txt="删", btn_cls="btn red") + '</div>'
    h.append('<div class="cfg-panel"><h3>定时任务</h3><div class="tip">💡 格式: 分 时 日 月 周 命令。例 "0 4 * * * reboot" = 每天4点重启路由器。真实写入 crontab，重启后仍生效。</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;cron_add&quot;}"><input class="inp" name="schedule" placeholder="如 0 4 * * *"><input class="inp" name="command" placeholder="命令 如 reboot"><button class="btn" type="submit">添加</button></form>' +
             '<div class="list">' + ct + '</div></div>')
    # 防火墙规则
    fr = ""
    for x in cfg.get("fw_rules", []):
        col = "red" if x.get("target") in ("DROP", "REJECT") else "green"
        fr += '<div class="item"><span class="val ' + col + '">' + esc(x.get("target", "")) + '</span><span class="val">' + esc(x.get("name", "")) + '</span><span class="val">' + esc(x.get("src", "") or "any") + ((":" + esc(x.get("dest_port", ""))) if x.get("dest_port") else "") + '/' + esc(x.get("proto", "") or "all") + '</span>' + frm("fw_rule_del", {"id": x.get("id", "")}, confirm="删除规则？", btn_txt="删", btn_cls="btn red") + '</div>'
    h.append('<div class="cfg-panel"><h3>防火墙规则</h3><div class="tip">💡 高级：按 IP/端口/协议 允许(ACCEPT)或拒绝(DROP/REJECT)流量。</div><div class="desc">' + str(len(cfg.get("fw_rules", []))) + ' 条</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;fw_rule_add&quot;}"><input class="inp" name="name" placeholder="名称"><select class="inp" name="target"><option value="DROP">拒绝 DROP</option><option value="ACCEPT">允许 ACCEPT</option><option value="REJECT">拒绝 REJECT</option></select><input class="inp" name="src" placeholder="来源IP(空=所有)"><input class="inp" name="dest_port" placeholder="目标端口(空=所有)"><button class="btn" type="submit">添加规则</button></form>' +
             '<div class="list">' + fr + '</div></div>')
    # 性能优化
    h.append('<div class="cfg-panel"><h3>性能优化</h3><div class="tip">💡 建议：DNS 上游测速排序（解析更快）、WiFi 功率即时调整。硬件 NAT 已启用。</div><div class="desc">硬件 NAT: 已启用 (NSS 加速) · 队列: fq_codel</div>' +
             '<div class="row">' + frm("dns_speedtest", btn_txt="DNS 测速") + frm("dns_fastest", confirm="用最快的4个上游并重启DNS？", btn_txt="一键用最快", btn_cls="btn green") + '</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;wifi_power&quot;,&quot;params&quot;:{&quot;band&quot;:&quot;5g&quot;}}">5G功率<select class="inp" name="power"><option value="28">28dBm 满</option><option value="24">24</option><option value="20">20</option><option value="16">16</option><option value="12">12</option></select><button class="btn" type="submit">设5G功率</button></form>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;wifi_power&quot;,&quot;params&quot;:{&quot;band&quot;:&quot;2g&quot;}}">2.4G功率<select class="inp" name="power"><option value="28">28dBm 满</option><option value="24">24</option><option value="20">20</option><option value="16">16</option><option value="12">12</option></select><button class="btn" type="submit">设2.4G功率</button></form></div>')
    # LED + 备份 + Guest
    h.append('<div class="cfg-panel"><h3>LED 指示灯</h3><div class="tip">💡 关闭指示灯（路由器灯灭，不影响功能）。</div><div class="desc">' + ('亮' if cfg.get("led_blue") else "灭") + '</div><div class="row">' + frm("led_toggle", btn_txt=("关闭" if cfg.get("led_blue") else "开启")) + '</div></div>')
    h.append('<div class="cfg-panel"><h3>配置备份</h3><div class="tip">💡 配置存在路由器 /etc/config/。重启/升级前建议先备份。</div><div class="row">' + frm("backup", btn_txt="查看配置摘要", btn_cls="btn gray") + '</div></div>')
    gw = cfg.get("guest_wifi", {})
    h.append('<div class="cfg-panel"><h3>Guest 访客网络</h3><div class="tip">💡 访客 2.4G: ' + esc(gw.get("2g", "off")) + ' / 5G: ' + esc(gw.get("5g", "off")) + '。开启访客网络请用小米管理页 192.168.31.1（本面板不做 wifi 写入避免断网风险）。</div></div>')
    # 系统操作 + 系统
    h.append('<div class="cfg-panel"><h3>系统操作</h3><div class="tip">💡 重启路由器(2秒后执行) · 需等待约2分钟恢复</div><div class="row">' +
             frm("reboot", {"confirm": "yes"}, confirm="确定重启路由器？约2分钟断网", btn_txt="重启路由器", btn_cls="btn red") +
             frm("dnsmasq_restart", btn_txt="重启DNS", btn_cls="btn gray") + '</div></div>')
    h.append('<div class="cfg-panel"><h3>系统</h3><div class="tip">💡 运行 ' + esc(cfg.get("uptime", "")) + ' · 温度 ' + esc(cfg.get("temp", "")) + '°C · WiFi: ' + esc(w.get("ssid", "")) + ' (信道' + esc(w.get("channel", "")) + ')</div></div>')
    return '<div class="cfg-grid">' + "".join(h) + '</div>'

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
        sh("mv -f /tmp/hagezi_new.conf /tmp/dnsmasq.d/hagezi.conf; /etc/init.d/dnsmasq restart")
        return "hagezi 已更新: " + n + " 条"
    if action == "dnsmasq_restart":
        sh("/etc/init.d/dnsmasq restart")
        return "dnsmasq 已重启"
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

function showUrlMsg(){var q=location.search.match(/[?&]msg=([^&]+)/);if(q){showMsg(decodeURIComponent(q[1]));}}
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
setTimeout(showUrlMsg,300);

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
 var doIt=function(){
  var f=document.createElement('form');f.method='POST';f.action='/api/act';
  var inp=document.createElement('input');inp.type='hidden';inp.name='json';
  inp.value=JSON.stringify({action:actName,params:params});
  f.appendChild(inp);document.body.appendChild(f);f.submit();
 };
 if(b.dataset.confirm){if(confirm(b.dataset.confirm)){if(b.dataset.confirmValue)params.confirm=b.dataset.confirmValue;doIt();}}else doIt();
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
    '<button class="btn gray" data-act="dnsmasq_restart">重启 DNS 服务</button>');
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
  h+=panel('性能优化','','对游戏/网页有帮助的深度优化：DNS 上游测速排序（解析更快）、WiFi 功率即时调整（信号强度/省电）。硬件 NAT 已启用。',
    '硬件 NAT: 已启用 (NSS 加速) · 队列: fq_codel · 连接数: 483/16384',
    '<div class="row"><button class="btn" data-act="dns_speedtest">DNS 测速</button><button class="btn green" data-act="dns_fastest" data-confirm="用最快的4个上游并重启DNS？">一键用最快</button></div>'+
    '<div class="row">5G 功率 <select class="inp" id="pw5"><option value="28">28dBm 满</option><option value="24">24</option><option value="20">20</option><option value="16">16</option><option value="12">12</option><option value="8">8</option></select><button class="btn" data-act="wifi_power" data-band="5g" data-inp="pw5">设5G功率</button></div>'+
    '<div class="row">2.4G 功率 <select class="inp" id="pw2"><option value="28">28dBm 满</option><option value="24">24</option><option value="20">20</option><option value="16">16</option><option value="12">12</option><option value="8">8</option></select><button class="btn" data-act="wifi_power" data-band="2g" data-inp="pw2">设2.4G功率</button></div>',
    '');
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
 var f=document.createElement('form');f.method='POST';f.action='/api/act';
 var inp=document.createElement('input');inp.type='hidden';inp.name='json';
 inp.value=JSON.stringify(body);f.appendChild(inp);document.body.appendChild(f);f.submit();
}
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._gate():
            return
        p = self.path.split("?")[0]
        if p == "/api":
            with data_lock:
                d = {"cpu": list(history["cpu"]), "mem_used_mb": list(history["mem_used_mb"]),
                     "mem_total_mb": list(history["mem_total_mb"]), "temp": list(history["temp"]),
                     "rx": list(history["rx"]), "tx": list(history["tx"]), "conn": list(history["conn"]),
                     "latest": {"cpu": history["cpu"][-1] if history["cpu"] else 0,
                                "mem_used_mb": history["mem_used_mb"][-1] if history["mem_used_mb"] else 0,
                                "mem_total_mb": history["mem_total_mb"][-1] if history["mem_total_mb"] else 0,
                                "temp": history["temp"][-1] if history["temp"] else 0,
                                "rx": history["rx"][-1] if history["rx"] else 0,
                                "tx": history["tx"][-1] if history["tx"] else 0,
                                "conn": history["conn"][-1] if history["conn"] else 0}}
            self._send(200, d)
        elif p == "/api/config":
            try:
                self._send(200, get_config())
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})
        else:
            try:
                cfg_data = get_config()
                cfg_html = render_config_html(cfg_data)
                cfg_json = json.dumps(cfg_data, ensure_ascii=False)
            except Exception as e:
                cfg_html = '<div class="cfg-grid"><div class="cfg-panel"><h3>错误</h3><div class="desc">' + esc(str(e)) + '</div></div></div>'
                cfg_json = "{}"
            body = PAGE.replace("%HOST%", HOST).replace('<div class="cfg-grid" id="cfg-grid">加载中...</div>', cfg_html).encode("utf-8")
            # 防 </script> 截断注入（DHCP 主机名等不可信字段直接进内联 JSON）+ JS 行分隔符
            for a, b in (("<", "\\u003c"), (">", "\\u003e"),
                         (chr(0x2028), "\\u2028"), (chr(0x2029), "\\u2029")):
                cfg_json = cfg_json.replace(a, b)
            body = body.replace(b"<script>", ("<script>window.__CFG__=" + cfg_json + ";").encode("utf-8"), 1)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _gate(self):
        # 防 DNS rebinding：本地模式 Host 必须是回环地址（--lan 时不限制）
        if not LAN_MODE:
            h = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
            if h not in ("127.0.0.1", "localhost", "::1"):
                self._send(403, {"ok": False, "error": "illegal host"})
                return False
        # 防 CSRF：浏览器跨站 POST 必带 Origin/Referer 且与 Host 一致；无 Origin 的 CLI 直连放行
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if origin:
            try:
                net = urllib.parse.urlparse(origin).netloc.lower()
            except Exception:
                net = "?"
            if net != (self.headers.get("Host") or "").lower():
                self._send(403, {"ok": False, "error": "origin check failed"})
                return False
        # 令牌：--lan 模式必配；HTTP Basic（浏览器原生弹框，凭证自动附带所有后续请求）
        if PANEL_TOKEN and not self._auth_ok():
            self._deny()
            return False
        return True

    def _auth_ok(self):
        # 与 AP 面板 monitor_web.py 同款：Basic 密码即令牌，用户名忽略
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(hdr[6:]).decode("utf-8", "replace")
            _, _, pw = raw.partition(":")
        except Exception:
            return False
        return hmac.compare_digest(pw, PANEL_TOKEN)

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="router-panel"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if not self._gate():
            return
        if self.path == "/api/act":
            try:
                ln = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(ln).decode("utf-8", "replace")
                data = {}
                params = {}
                if raw.startswith("{"):
                    data = json.loads(raw or "{}")
                    params = data.get("params", {})
                else:
                    from urllib.parse import parse_qs
                    qs = parse_qs(raw)
                    data = json.loads(qs.get("json", ["{}"])[0])
                    params = dict(data.get("params", {}))
                    for k, v in qs.items():
                        if k != "json":
                            params[k] = v[0]
                msg = do_action(data.get("action", ""), params)
                self.send_response(302)
                self.send_header("Location", "/?msg=" + urllib.parse.quote(msg))
                self.end_headers()
            except Exception as e:
                self.send_response(302)
                self.send_header("Location", "/?msg=" + urllib.parse.quote("操作出错: " + str(e)))
                self.end_headers()
        elif self.path == "/api/config":
            try:
                ln = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(ln).decode() or "{}")
                msg = do_action(data.get("action", ""), data.get("params", {}))
                self._send(200, {"ok": True, "msg": msg})
            except Exception as e:
                self._send(500, {"ok": False, "msg": str(e)})
        else:
            self._send(404, {"ok": False})

    def log_message(self, *a):
        pass


LAN_MODE = False
PANEL_TOKEN = ""

def main():
    global HOST, SSHPORT, USER, PASSWD, WEBPORT, LAN_MODE, PANEL_TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=SSHPORT)
    ap.add_argument("--user", default=USER)
    ap.add_argument("--passwd", default=PASSWD)
    ap.add_argument("--web", type=int, default=WEBPORT)
    ap.add_argument("--lan", action="store_true", help="绑定所有网卡允许局域网访问（必须同时提供 --token）")
    ap.add_argument("--token", default=os.environ.get("ROUTER_PANEL_TOKEN", ""), help="访问令牌；--lan 时必填")
    a = ap.parse_args()
    HOST, SSHPORT, USER, PASSWD, WEBPORT = a.host, a.port, a.user, a.passwd, a.web
    if a.lan and not a.token:
        print("[!] --lan 会把面板暴露到局域网，必须同时设置 --token（或环境变量 ROUTER_PANEL_TOKEN）")
        return
    LAN_MODE, PANEL_TOKEN = a.lan, a.token

    threading.Thread(target=collector_loop, daemon=True).start()
    time.sleep(2)
    import socket as _sock
    if LAN_MODE:
        class DualStackServer(ThreadingHTTPServer):
            address_family = _sock.AF_INET6
            def server_bind(self):
                try:
                    self.socket.setsockopt(_sock.IPPROTO_IPV6, _sock.IPV6_V6ONLY, 0)
                except OSError:
                    pass
                super().server_bind()
        srv = DualStackServer(("::", WEBPORT), Handler)
        where = "0.0.0.0(双栈)/%d · 令牌认证已启用" % WEBPORT
    else:
        srv = ThreadingHTTPServer(("127.0.0.1", WEBPORT), Handler)
        where = "127.0.0.1:%d (仅本机)" % WEBPORT
    print("AX3000E 主路由版监控+配置中心(中文): http://127.0.0.1:" + str(WEBPORT))
    print("  监听: " + where)
    print("  路由器: " + USER + "@" + HOST + ":" + str(SSHPORT) + "  (Ctrl+C 停止)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
