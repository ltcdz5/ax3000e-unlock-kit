# -*- coding: utf-8 -*-
"""
小米路由器 有线中继模式 监控 + 配置中心
- 性能监控: CPU/内存/温度/流量/连接数
- 配置中心: DNS上游 / DNS缓存 / 去广告 / 自定义屏蔽 / WiFi信道功率 / LED / 定时任务
- 中继模式下已移除: QoS / 端口转发 / 防火墙规则 / DHCP租期 / UPnP / 静态绑定（均由上级路由器管理）
用法: python router_monitor_ap.py
"""
import sys, json, time, threading, argparse, re
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from collections import deque

HOST = "192.168.2.106"
SSHPORT = 22
USER = "root"
PASSWD = os.environ.get("ROUTER_PASSWD", "<改成你的路由器SSH密码>")
WEBPORT = 8787
INTERVAL = 2
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


def collect():
    now = time.time()
    # 单次 SSH 往返拉回全部采集数据, 本地解析(替代原来的 5 次独立往返, 大幅降负载)
    raw = sh(
        "head -1 /proc/stat; echo '@@'; "
        "grep -E 'MemTotal|MemFree|Buffers|^Cached' /proc/meminfo; echo '@@'; "
        "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null; echo '@@'; "
        "grep br-lan /proc/net/dev; echo '@@'; "
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
    # DNS 上游
    up = sh("cat /tmp/dnsmasq.d/98-upstream.conf 2>/dev/null")
    cfg["dns_upstreams"] = [l.replace("server=", "").strip() for l in up.splitlines() if l.startswith("server=")]
    cfg["cache_size"] = sh("uci get dhcp.@dnsmasq[0].cachesize 2>/dev/null") or "150"
    # 去广告
    cfg["adblock_hagezi"] = sh("wc -l /tmp/dnsmasq.d/96-antiad.conf 2>/dev/null").split()[0] if sh("test -f /tmp/dnsmasq.d/96-antiad.conf && echo y") == "y" else 0
    cfg["adblock_yhosts"] = sh("wc -l /data/adblock.hosts 2>/dev/null").split()[0] if sh("test -f /data/adblock.hosts && echo y") == "y" else 0
    cfg["adblock_enabled"] = "99-adblock.conf" in sh("ls /tmp/dnsmasq.d/ 2>/dev/null")
    # 自定义屏蔽
    custom = sh("cat /tmp/dnsmasq.d/97-custom.conf 2>/dev/null")
    cfg["custom_adblock"] = [re.sub(r"^address=/(.*)/.*$", r"\1", l).strip() for l in custom.splitlines() if l.startswith("address=/")]
    # 服务状态
    cfg["ssh"] = "dropbear" in sh("ps | grep dropbear | grep -v grep")
    cfg["auto_ssh"] = sh("test -f /data/auto_ssh/auto_ssh.sh && echo y") == "y"
    cfg["uptime"] = sh("uptime").split(",")[0].strip() if sh("uptime") else ""
    cfg["temp"] = history["temp"][-1] if history["temp"] else 0
    # 在线设备（中继模式：实时 ARP 邻居表，DHCP 租约仅补主机名）
    leases = {}
    for line in sh("cat /tmp/dhcp.leases 2>/dev/null").splitlines():
        p = line.split()
        if len(p) >= 4:
            leases[p[1]] = p[3]
    devices = []
    for line in sh("ip neigh show dev br-lan 2>/dev/null").splitlines():
        p = line.split()
        if len(p) >= 4 and p[0].startswith("192.168.") and "lladdr" in p:
            if p[0] == cfg.get("gateway", "192.168.2.1"):
                continue
            mac = p[p.index("lladdr") + 1]
            devices.append({"ip": p[0], "mac": mac, "host": leases.get(mac, ""), "state": p[-1]})
    cfg["devices"] = devices
    # DNS 查询记录（log-queries，实时；文件过大自动截断）
    try:
        sz = sh("wc -c < /tmp/dnsquery.log")
        if sz.strip().isdigit() and int(sz.strip()) > 300 * 1024:
            sh("> /tmp/dnsquery.log")
    except Exception:
        pass
    queries = []
    for line in sh("tail -n 80 /tmp/dnsquery.log 2>/dev/null").splitlines():
        m = re.search(r"query\[([A-Z0-9]+)\] ([^ ]+) from ([0-9.]+)", line)
        if m:
            queries.append({"type": m.group(1), "domain": m.group(2), "ip": m.group(3)})
    cfg["dns_queries"] = queries[-40:]
    # 定时任务（#panel 标记的行）
    crontab = sh("cat /etc/crontabs/root 2>/dev/null")
    cfg["cron_tasks"] = [l for l in crontab.splitlines() if l.startswith("#panel")]
    # LED（用官方 XLED 状态, 与 led_ctl 一致; 读 uci 而非直接写 sysfs）
    led_b = sh("uci get xiaoqiang.common.XLED 2>/dev/null")
    cfg["led_blue"] = led_b.strip() == "1"
    # WiFi 状态（单次 uci show 拉回, 本地解析, 替代 12 次独立 SSH）
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
    # 中继模式信息
    cfg["mode"] = "ap"
    cfg["gateway"] = sh("ip route show default 2>/dev/null | awk '{print $3}'").strip() or "192.168.2.1"
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
    # 模式提示
    h.append('<div class="cfg-panel" style="grid-column:1/-1;background:#1a2a1a;border-color:#2a4a2a"><h3>📡 当前模式：有线中继 (AP)</h3>' +
             '<div class="tip" style="background:#1a2a1a;border-left-color:#4caf50">中继模式下，本路由器仅负责 WiFi 接入 + DNS 去广告。' +
             'IP/DHCP/NAT/防火墙/QoS/端口转发/UPnP 均由上级路由器 ' + esc(cfg.get("gateway","192.168.2.1")) + ' 管理，本面板不提供这些功能。</div>' +
             '<div class="desc">运行 ' + esc(cfg.get("uptime","")) + ' · 温度 ' + esc(cfg.get("temp",0)) + '°C · SSH ' + ('已开' if cfg.get("ssh") else '已关') + ' · auto_ssh开机自愈 ' + ('已开' if cfg.get("auto_ssh") else '已关') + '</div></div>')

    # DNS 上游
    ups = ""
    for s in cfg.get("dns_upstreams", []):
        ups += '<div class="item"><span class="val">' + esc(s) + '</span>' + frm("dns_del", {"server": s}, confirm="删除 " + s + " 吗？", btn_txt="删除", btn_cls="btn red") + '</div>'
    h.append('<div class="cfg-panel"><h3>DNS 上游</h3><div class="tip">💡 中继模式下本路由器作为 DNS 服务器提供去广告。设备需手动设置 DNS 为 ' + HOST + ' 或在上级路由器 DHCP 中指定。国内优先（阿里/腾讯/114），海外备选。</div><div class="desc">当前 ' + str(len(cfg.get("dns_upstreams", []))) + ' 个上游</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;dns_add&quot;}"><input class="inp" name="server" placeholder="例: 223.5.5.5"><button class="btn" type="submit">添加</button></form>' +
             '<div class="list">' + ups + '</div><div class="row">' + frm("dnsmasq_restart", btn_txt="重启 DNS 服务", btn_cls="btn gray") + '</div></div>')

    # DNS 缓存
    h.append('<div class="cfg-panel"><h3>DNS 缓存</h3><div class="tip">💡 缓存越大命中率越高。512-4096 合适，当前 ' + esc(cfg.get("cache_size","")) + '。</div><div class="desc">当前 cache-size = ' + esc(cfg.get("cache_size", "")) + '</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;cache_set&quot;}"><input class="inp" name="size" value="' + esc(cfg.get("cache_size", "1024")) + '"><button class="btn" type="submit">保存</button></form></div>')

    # 去广告
    ad_on = cfg.get("adblock_enabled", True)
    h.append('<div class="cfg-panel"><h3>去广告</h3><div class="tip">💡 anti-AD(10万条国内+国外) + yhosts。DNS 请求走本路由器时生效。</div><div class="desc"><span class="badge ' + ('on' if ad_on else 'off') + '">' + ('已开' if ad_on else '已关') + '</span> anti-AD ' + esc(cfg.get("adblock_hagezi", "")) + ' 条 / yhosts ' + esc(cfg.get("adblock_yhosts", "")) + ' 条</div>' +
             '<div class="row">' + frm("adblock_toggle", confirm="确定" + ("关闭" if ad_on else "开启") + "去广告吗？", btn_txt=("关闭" if ad_on else "开启") + "去广告") +
             frm("hagezi_update", confirm="重新下载 anti-AD 列表？", btn_txt="更新列表") + '</div></div>')

    # 自定义屏蔽
    cust = ""
    for d in cfg.get("custom_adblock", []):
        cust += '<div class="item"><span class="val">' + esc(d) + '</span>' + frm("ad_custom_del", {"domain": d}, confirm="解除屏蔽 " + d + " 吗？", btn_txt="解除", btn_cls="btn red") + '</div>'
    h.append('<div class="cfg-panel"><h3>自定义屏蔽域名</h3><div class="tip">💡 去广告列表没覆盖的域名手动加这里。填域名如 ads.example.com（不含 http）。</div><div class="desc">已屏蔽 ' + str(len(cfg.get("custom_adblock", []))) + ' 个</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;ad_custom_add&quot;}"><input class="inp" name="domain" placeholder="例: ads.example.com"><button class="btn" type="submit">屏蔽</button></form>' +
             '<div class="list">' + cust + '</div></div>')

    # WiFi 信道
    w = cfg.get("wifi", {})
    a_ch = str(w.get("a_channel", "0"))
    g_ch = str(w.get("g_channel", "0"))
    g_disabled = str(w.get("g_disabled", "")) == "1"
    asel = '<select class="inp" name="channel">'
    for ch in [36, 40, 44, 48, 149, 153, 157, 161]:
        asel += '<option value="' + str(ch) + '"' + (' selected' if a_ch == str(ch) else '') + '>' + str(ch) + (' (推荐)' if ch in (36, 149) else '') + '</option>'
    asel += '</select>'
    gsel = '<select class="inp" name="channel">'
    for ch in [1, 6, 11, 3, 9, 13]:
        gsel += '<option value="' + str(ch) + '"' + (' selected' if g_ch == str(ch) else '') + '>' + str(ch) + (' (推荐)' if ch in (1, 6, 11) else '') + '</option>'
    gsel += '</select>'
    h.append('<div class="cfg-panel"><h3>WiFi 信道</h3><div class="tip">💡 信道即时切换（官方接口，不断网）。5G 推荐 36/149（避开雷达）；2.4G 推荐 1/6/11。重启后恢复自动。</div>' +
             '<div class="desc">5G: ' + esc(w.get("a_ssid", "")) + ' 信道' + (a_ch if a_ch != "0" else "自动") + ' · 2.4G: ' + esc(w.get("g_ssid", "")) + (' <span class="badge off">已禁用</span>' if g_disabled else ' 信道' + (g_ch if g_ch != "0" else "自动")) + '</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;wifi_channel&quot;,&quot;params&quot;:{&quot;band&quot;:&quot;5g&quot;}}">5G ' + asel + '<button class="btn" type="submit">切换5G信道</button></form>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;wifi_channel&quot;,&quot;params&quot;:{&quot;band&quot;:&quot;2g&quot;}}">2.4G ' + gsel + '<button class="btn" type="submit">切换2.4G信道</button></form></div>')

    # WiFi 功率
    h.append('<div class="cfg-panel"><h3>WiFi 功率</h3><div class="tip">💡 即时调整发射功率。28dBm=满功率(约630mW)，不需要覆盖可降低省电。</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;wifi_power&quot;,&quot;params&quot;:{&quot;band&quot;:&quot;5g&quot;}}">5G功率<select class="inp" name="power"><option value="28">28dBm 满</option><option value="24">24</option><option value="20">20</option><option value="16">16</option><option value="12">12</option></select><button class="btn" type="submit">设5G功率</button></form>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;wifi_power&quot;,&quot;params&quot;:{&quot;band&quot;:&quot;2g&quot;}}">2.4G功率<select class="inp" name="power"><option value="28">28dBm 满</option><option value="24">24</option><option value="20">20</option><option value="16">16</option><option value="12">12</option></select><button class="btn" type="submit">设2.4G功率</button></form></div>')

    # 在线设备
    dv = ""
    for x in cfg.get("devices", []):
        dv += '<div class="item"><span class="val">' + esc(x.get("host", "")) + '</span><span class="val">' + esc(x.get("ip", "")) + '</span><span class="val">' + esc(x.get("mac", "")) + '</span><span class="val" style="color:' + ('#66bb6a' if x.get("state") == "REACHABLE" else '#ffa726') + '">' + esc(x.get("state", "")) + '</span></div>'
    h.append('<div class="cfg-panel"><h3>在线设备</h3><div class="tip">💡 实时 ARP 邻居表（当前与路由器通信的设备，含状态）。DHCP 由上级路由器分配，主机名仅在有租约记录时显示。</div><div class="desc">' + str(len(cfg.get("devices", []))) + ' 台（实时）</div><div class="list">' + dv + '</div></div>')
    # DNS 查询记录
    dq = ""
    for x in cfg.get("dns_queries", []):
        dq += '<div class="item"><span class="val">' + esc(x.get("ip", "")) + '</span><span class="val">[' + esc(x.get("type", "")) + ']</span><span class="val">' + esc(x.get("domain", "")) + '</span></div>'
    h.append('<div class="cfg-panel"><h3>DNS 查询记录</h3><div class="tip">💡 实时显示各设备最近 DNS 查询（谁在查什么域名）。日志超 300KB 自动清空。</div><div class="desc">最近 ' + str(len(cfg.get("dns_queries", []))) + ' 条查询</div><div class="list">' + dq + '</div></div>')

    # 定时任务
    ct = ""
    for line in cfg.get("cron_tasks", []):
        t = line.replace("#panel ", "")
        ct += '<div class="item"><span class="val">' + esc(t) + '</span>' + frm("cron_del", {"line": t}, confirm="删除该任务？", btn_txt="删", btn_cls="btn red") + '</div>'
    h.append('<div class="cfg-panel"><h3>定时任务</h3><div class="tip">💡 格式: 分 时 日 月 周 命令。例 "0 4 * * * reboot" = 每天4点重启路由器。</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;cron_add&quot;}"><input class="inp" name="schedule" placeholder="如 0 4 * * *"><input class="inp" name="command" placeholder="命令 如 reboot"><button class="btn" type="submit">添加</button></form>' +
             '<div class="list">' + ct + '</div></div>')

    # LED
    h.append('<div class="cfg-panel"><h3>LED 指示灯</h3><div class="tip">💡 关闭指示灯（路由器灯灭，不影响功能）。</div><div class="desc">' + ('亮' if cfg.get("led_blue") else "灭") + '</div><div class="row">' + frm("led_toggle", btn_txt=("关闭" if cfg.get("led_blue") else "开启")) + '</div></div>')

    # 性能优化 / DNS测速
    h.append('<div class="cfg-panel"><h3>DNS 测速优化</h3><div class="tip">💡 对 DNS 上游测速，用最快的几个。硬件 NAT 由上级路由器负责。</div>' +
             '<div class="row">' + frm("dns_speedtest", btn_txt="DNS 测速") + frm("dns_fastest", confirm="用最快的4个上游并重启DNS？", btn_txt="一键用最快", btn_cls="btn green") + '</div></div>')
    # 实时测试
    h.append('<div class="cfg-panel"><h3>实时测试</h3><div class="tip">💡 宽带测速用中科大标准测速（新窗口）；实时测试查延迟/DNS/手机链路（约5秒）。</div>'
             '<div class="row"><button class="btn green" onclick="window.open(\'https://test.ustc.edu.cn\',\'_blank\')">中科大宽带测速</button>'
             '<button class="btn" id="nt-btn" onclick="netTest()">实时测试</button></div>'
             '<div id="nt-progress" style="display:none;margin-top:10px"><div style="height:6px;background:#2a3038;border-radius:3px;overflow:hidden"><div id="nt-bar" style="height:100%;width:0%;background:#4caf50;transition:width .3s"></div></div><div class="desc" id="nt-stage" style="margin-top:6px">准备中...</div></div>'
             '<div id="nt-result" style="margin-top:10px;font-family:monospace;font-size:12px;white-space:pre-line;line-height:1.8"></div></div>')

    # 配置备份
    h.append('<div class="cfg-panel"><h3>配置备份</h3><div class="tip">💡 配置存在路由器 /etc/config/。重启/升级前建议先备份。</div><div class="row">' + frm("backup", btn_txt="查看配置摘要", btn_cls="btn gray") + '</div></div>')

    # 系统操作
    h.append('<div class="cfg-panel"><h3>系统操作</h3><div class="tip">💡 重启路由器(2秒后执行) · 需等待约2分钟恢复。中继模式下重启不影响上级网络。</div><div class="row">' +
             frm("reboot", {"confirm": "yes"}, confirm="确定重启路由器？约2分钟断网", btn_txt="重启路由器", btn_cls="btn red") +
             frm("dnsmasq_restart", btn_txt="重启DNS", btn_cls="btn gray") + '</div></div>')

    return '<div class="cfg-grid">' + "".join(h) + '</div>'


def run_net_test():
    """实时网络测试：外网延迟 + DNS + 手机链路（宽带测速走中科大）"""
    import subprocess, time as _t
    res = []
    for host in ["223.5.5.5", "119.29.29.29"]:
        try:
            out = subprocess.run(["ping", "-n", "4", "-w", "2000", host], capture_output=True, text=True, timeout=15)
            m = re.search(r"(?:平均|Average)\s*=\s*(\d+)", out.stdout)
            res.append("延迟 %s: %s ms" % (host, m.group(1) if m else "?"))
        except Exception:
            res.append("延迟 %s: 失败" % host)
    t0 = _t.time()
    sh("nslookup www.baidu.com 127.0.0.1 >/dev/null 2>&1")
    res.append("DNS解析: %.0f ms" % ((_t.time() - t0) * 1000))
    # 手机链路：动态找 DHCP 租约里的一个主机（替代硬编码 MAC）
    mip = ""
    leases = sh("cat /tmp/dhcp.leases 2>/dev/null").splitlines()
    candidates = []
    for line in leases:
        p = line.split()
        if len(p) >= 4 and p[1].startswith("192.168."):
            candidates.append(p[1])
    # 排除自身路由网关和上级路由, 取最后一个(通常是最近接入的设备)
    for ip in candidates:
        if ip not in ("192.168.2.106", "192.168.2.1"):
            mip = ip
    if mip:
        r = sh("ping -c 3 " + mip + " 2>/dev/null | grep -o 'avg = [0-9.]*'")
        res.append("设备WiFi链路(" + mip + "): " + (r if r else "设备未响应"))
    else:
        res.append("设备WiFi链路: 无在线设备")
    return "\n".join(res)


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
    if action == "hagezi_update":
        sh("curl -sL 'https://anti-ad.net/anti-ad-for-dnsmasq.conf' -o /tmp/antiad_raw --connect-timeout 15 --max-time 60")
        sh("grep -vE 'byteimg|pstatp|douyinpic|douyin|bytecdn|bytedance' /tmp/antiad_raw > /tmp/dnsmasq.d/96-antiad.conf 2>/dev/null")
        n = sh("wc -l /tmp/dnsmasq.d/96-antiad.conf 2>/dev/null").split()[0]
        sh("gzip -c /tmp/dnsmasq.d/96-antiad.conf > /data/antiad.gz; /etc/init.d/dnsmasq restart")
        return "anti-AD 已更新: " + n + " 条"
    if action == "dnsmasq_restart":
        sh("/etc/init.d/dnsmasq restart")
        return "dnsmasq 已重启"
    if action == "cache_set":
        v = str(int(params.get("size", 1024)))
        sh("uci set dhcp.@dnsmasq[0].cachesize='" + v + "'; uci commit dhcp; /etc/init.d/dnsmasq restart")
        return "DNS 缓存已设为 " + v
    if action == "dns_add":
        s = params.get("server", "").strip()
        if not re.match(r"^[\d\.]+$", s):
            return "无效 IP"
        sh("echo 'server=" + s + "' >> /tmp/dnsmasq.d/98-upstream.conf; /etc/init.d/dnsmasq restart; cp /tmp/dnsmasq.d/98-upstream.conf /data/upstreams.conf")
        return "已添加上游 " + s
    if action == "dns_del":
        s = params.get("server", "").strip()
        sh("sed -i '/server=" + re.escape(s) + "$/d' /tmp/dnsmasq.d/98-upstream.conf; /etc/init.d/dnsmasq restart; cp /tmp/dnsmasq.d/98-upstream.conf /data/upstreams.conf")
        return "已删除上游 " + s
    if action == "ad_custom_add":
        d = params.get("domain", "").strip().lower()
        if not re.match(r"^[a-z0-9\-\.]+$", d):
            return "无效域名"
        sh("echo 'address=/" + d + "/0.0.0.0' >> /tmp/dnsmasq.d/97-custom.conf; /etc/init.d/dnsmasq restart")
        return "已屏蔽 " + d
    if action == "ad_custom_del":
        d = params.get("domain", "").strip()
        sh("grep -v 'address=/" + d + "/0.0.0.0' /tmp/dnsmasq.d/97-custom.conf > /tmp/c.tmp; mv /tmp/c.tmp /tmp/dnsmasq.d/97-custom.conf; /etc/init.d/dnsmasq restart")
        return "已解除屏蔽 " + d
    if action == "wifi_channel":
        band = params.get("band", "5g")
        ch = str(params.get("channel", "0"))
        ifname = "wl1" if band == "2g" else "wl0"
        if ch == "0":
            return "请选择具体信道（自动模式重启后恢复）"
        # 注意: 实际函数名是 _set_channel(带下划线), 直接调 iwconfig 更可靠
        sh("iwconfig " + ifname + " channel " + ch)
        return "WiFi " + band + " 信道已即时切换为 " + ch + "（重启后恢复自动）"
    if action == "wifi_power":
        band = params.get("band", "5g")
        pw = str(params.get("power", "28"))
        ifname = "wl1" if band == "2g" else "wl0"
        if not pw.isdigit() or not (0 <= int(pw) <= 30):
            return "功率须 0-30 dBm"
        sh("iwconfig " + ifname + " txpower " + pw + "dBm")
        return "WiFi " + band + " 功率已设为 " + pw + " dBm（即时生效，重启恢复）"
    if action == "net_test":
        return run_net_test()
    if action == "cron_add":
        sched = params.get("schedule", "").strip()
        cmd = params.get("command", "").strip()
        if not (sched and cmd):
            return "时间和命令必填"
        sh("echo '#panel " + sched + " " + cmd + "' >> /etc/crontabs/root; /etc/init.d/cron restart")
        return "定时任务已添加: " + sched + " " + cmd
    if action == "cron_del":
        line = params.get("line", "").strip()
        if line:
            sh("sed -i '/#panel " + re.escape(line) + "/d' /etc/crontabs/root; /etc/init.d/cron restart")
            return "定时任务已删除: " + line
        return "参数无效"
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
        return "配置项共 " + total + " 行（完整配置在路由器 /etc/config/）"
    if action == "reboot":
        if params.get("confirm") != "yes":
            return "已取消：需确认（confirm=yes）才执行重启"
        sh("(sleep 2; reboot) &")
        return "路由器 2 秒后重启，请等待约 2 分钟"
    return "未知操作"


PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>小米路由器 中继模式 监控+配置中心</title>
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
.btn.green{border-color:#2d8a4a;background:#236b33}.btn.green:hover{background:#2d8a4a}
.inp{padding:6px 10px;border-radius:6px;border:1px solid #3a4149;background:#161a20;color:#e8eaed;font-size:12px;width:110px}
.inp.wide{width:180px}
.val{font-family:monospace;font-size:12px;color:#9fc3e8;background:#161a20;padding:4px 8px;border-radius:6px;display:inline-block;margin:2px}
.list{max-height:150px;overflow-y:auto;margin-top:8px}
.list .item{display:flex;justify-content:space-between;align-items:center;padding:4px 6px;border-bottom:1px solid #2a3038;font-size:12px}
.msg{position:fixed;bottom:20px;right:20px;background:#2d5d8a;color:#fff;padding:10px 18px;border-radius:8px;display:none;font-size:13px;z-index:9}
</style></head><body>
<h1>小米路由器 有线中继模式 监控 + 配置中心</h1>
<div class="sub">SSH: %HOST% · 中继模式（AP）· DNS去广告 + WiFi管理 · 温度阈值：绿&lt;85 / 橙85-90 / 红&gt;90</div>
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
}
function showUrlMsg(){var q=location.search.match(/[?&]msg=([^&]+)/);if(q){showMsg(decodeURIComponent(q[1]));}}
function netTest(){
 var btn=document.getElementById('nt-btn'),bar=document.getElementById('nt-bar'),st=document.getElementById('nt-stage'),pg=document.getElementById('nt-progress'),rs=document.getElementById('nt-result');
 if(btn.disabled)return;
 btn.disabled=true;btn.textContent='测试中...';
 pg.style.display='block';rs.textContent='';
 var stages=['准备中...','外网延迟测试中...','DNS解析测试中...','手机链路测试中...'];
 var p=0,t0=Date.now();
 var timer=setInterval(function(){
  var el=Date.now()-t0;
  p=Math.min(97,Math.round(el/5000*97));
  bar.style.width=p+'%';
  var si=Math.min(3,Math.floor(el/1600));
  st.textContent=stages[si]+' '+p+'%';
 },200);
 fetch('/api/nettest',{method:'POST'}).then(function(r){return r.json();}).then(function(d){
  clearInterval(timer);bar.style.width='100%';st.textContent='完成';
  btn.disabled=false;btn.textContent='开始实时测试';
  rs.textContent=(d.ok?'':'错误: ')+d.msg;
  setTimeout(function(){pg.style.display='none';},1500);
 }).catch(function(){
  clearInterval(timer);bar.style.width='100%';st.textContent='失败';
  btn.disabled=false;btn.textContent='开始实时测试';
  rs.textContent='测试失败：请检查连接';
 });
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
        if self.path == "/api":
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
        elif self.path == "/api/config":
            try:
                self._send(200, get_config())
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})
        else:
            try:
                cfg_data = get_config()
                cfg_html = render_config_html(cfg_data)
            except Exception as e:
                cfg_html = '<div class="cfg-grid"><div class="cfg-panel"><h3>错误</h3><div class="desc">' + esc(str(e)) + '</div></div></div>'
            body = PAGE.replace("%HOST%", HOST).replace('<div class="cfg-grid" id="cfg-grid">加载中...</div>', cfg_html).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/nettest":
            try:
                msg = run_net_test()
                self._send(200, {"ok": True, "msg": msg})
            except Exception as e:
                self._send(500, {"ok": False, "msg": str(e)})
        elif self.path == "/api/act":
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
        else:
            self._send(404, {"ok": False})

    def log_message(self, *a):
        pass


def main():
    global HOST, SSHPORT, USER, PASSWD, WEBPORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=SSHPORT)
    ap.add_argument("--user", default=USER)
    ap.add_argument("--passwd", default=PASSWD)
    ap.add_argument("--web", type=int, default=WEBPORT)
    a = ap.parse_args()
    HOST, SSHPORT, USER, PASSWD, WEBPORT = a.host, a.port, a.user, a.passwd, a.web

    threading.Thread(target=collector_loop, daemon=True).start()
    time.sleep(2)
    import socket as _sock
    class DualStackServer(ThreadingHTTPServer):
        address_family = _sock.AF_INET6
        def server_bind(self):
            try:
                self.socket.setsockopt(_sock.IPPROTO_IPV6, _sock.IPV6_V6ONLY, 0)
            except OSError:
                pass
            super().server_bind()
    srv = DualStackServer(("::", WEBPORT), Handler)
    print("小米路由器 中继模式 监控+配置中心: http://127.0.0.1:" + str(WEBPORT))
    print("  路由器: " + USER + "@" + HOST + ":" + str(SSHPORT) + "  (Ctrl+C 停止)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
