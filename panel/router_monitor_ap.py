# -*- coding: utf-8 -*-
"""
小米路由器 有线中继模式 监控 + 配置中心
- 性能监控: CPU/内存/温度/流量/连接数
- 配置中心: DNS上游 / DNS缓存 / 去广告 / 自定义屏蔽 / WiFi信道功率 / LED / 定时任务
- 中继模式下已移除: QoS / 端口转发 / 防火墙规则 / DHCP租期 / UPnP / 静态绑定（均由上级路由器管理）
用法: python router_monitor_ap.py
"""
import sys, os, json, time, threading, argparse, re
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from collections import deque

HOST = "192.168.2.106"
SSHPORT = 22
USER = "root"
PASSWD = os.environ.get("ROUTER_PASSWD", "")
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
           ["cpu", "mem_used_mb", "mem_total_mb", "temp", "rx", "tx", "conn", "load", "ts"]}
last_net = {}
last_stat = {}
collect_ms = 0


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


def sh_write(cmd, data, timeout=15):
    """执行命令并通过 stdin 写入数据(避免命令行拼接的引号/注入问题)，成功返回 True"""
    global ssh_client
    with ssh_lock:
        for _ in range(2):
            try:
                if ssh_client is None or not ssh_client.get_transport() or not ssh_client.get_transport().is_active():
                    ssh_client = ssh_connect()
                stdin, stdout, stderr = ssh_client.exec_command(cmd, timeout=timeout)
                stdin.write(data)
                stdin.channel.shutdown_write()
                stdout.read()
                return True
            except Exception:
                try:
                    ssh_client = ssh_connect()
                except Exception:
                    time.sleep(2)
        return False


BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")


def make_backup():
    """路由器配置打包 → cat 经 SSH 拉回本地 backups/ 目录，返回文件名或 None"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    remote = "/tmp/panel_backup_%s.tar.gz" % ts
    global ssh_client
    with ssh_lock:
        try:
            if ssh_client is None or not ssh_client.get_transport() or not ssh_client.get_transport().is_active():
                ssh_client = ssh_connect()
            stdin, stdout, stderr = ssh_client.exec_command(
                "tar czf %s -C / etc/config etc/crontabs/root tmp/dnsmasq.d data/auto_ssh data/upstreams.conf data/adblock.hosts 2>/dev/null; wc -c < %s" % (remote, remote),
                timeout=40)
            size = stdout.read().decode().strip()
            if not size.isdigit() or int(size) == 0:
                return None
            stdin2, stdout2, stderr2 = ssh_client.exec_command("cat " + remote, timeout=60)
            blob = stdout2.read()
            ssh_client.exec_command("rm -f " + remote)
            if not blob:
                return None
            os.makedirs(BACKUP_DIR, exist_ok=True)
            fn = "router_backup_%s.tar.gz" % ts
            with open(os.path.join(BACKUP_DIR, fn), "wb") as f:
                f.write(blob)
            return fn
        except Exception:
            return None


def list_backups():
    """本地 backups/ 目录里的历史备份(最新在前, 最多8条)"""
    if not os.path.isdir(BACKUP_DIR):
        return []
    items = []
    for fn in os.listdir(BACKUP_DIR):
        if fn.endswith(".tar.gz"):
            try:
                st = os.stat(os.path.join(BACKUP_DIR, fn))
                items.append({"name": fn, "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                pass
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:8]


def collect():
    global collect_ms
    now = time.time()
    t0 = now
    # 单次 SSH 往返拉回全部采集数据, 本地解析(替代原来的 5 次独立往返, 大幅降负载)
    raw = sh(
        "head -1 /proc/stat; echo '@@'; "
        "grep -E 'MemTotal|MemFree|Buffers|^Cached' /proc/meminfo; echo '@@'; "
        "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null; echo '@@'; "
        "grep br-lan /proc/net/dev; echo '@@'; "
        "cat /proc/net/tcp | wc -l; echo '@@'; "
        "cat /proc/loadavg"
    )
    dt_ms = (time.time() - t0) * 1000
    collect_ms = round(dt_ms) if collect_ms == 0 else round(collect_ms * 0.7 + dt_ms * 0.3)
    cpu_pct = 0.0
    parts = raw.split("@@")
    if len(parts) < 6:
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

    load = 0.0
    lp = parts[5].strip().split()
    if lp:
        try:
            load = float(lp[0])
        except ValueError:
            pass

    with data_lock:
        history["cpu"].append(round(cpu_pct, 1))
        history["mem_used_mb"].append(mem_used_mb)
        history["mem_total_mb"].append(mem_total_mb)
        history["temp"].append(round(temp, 1))
        history["rx"].append(round(rx_rate, 1))
        history["tx"].append(round(tx_rate, 1))
        history["conn"].append(conn)
        history["load"].append(round(load, 2))
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
    auto_ssh_raw = sh("test -f /data/auto_ssh/auto_ssh.sh && sed -n 2p /data/auto_ssh/auto_ssh.sh 2>/dev/null")
    cfg["auto_ssh"] = bool(auto_ssh_raw.strip())
    vm = re.search(r"v\d+\s*\([^)]*\)", auto_ssh_raw)
    cfg["auto_ssh_ver"] = vm.group(0) if vm else ""
    cfg["uptime"] = sh("uptime").split(",")[0].strip() if sh("uptime") else ""
    cfg["temp"] = history["temp"][-1] if history["temp"] else 0
    # IPv6 拦截状态（全局拦截=ip6tables 规则; 设备实际使用数=非fe80邻居）
    v6_raw = sh("ip6tables -C FORWARD -j REJECT --reject-with icmp6-adm-prohibited 2>/dev/null && echo BLOCKED; echo @@; "
                "ip -6 neigh show dev br-lan 2>/dev/null | grep lladdr | grep -cv '^fe80'")
    v6p = v6_raw.split("@@")
    cfg["ipv6_blocked"] = "BLOCKED" in (v6p[0] if v6p else "")
    cfg["ipv6_clients"] = int(v6p[1].strip()) if len(v6p) > 1 and v6p[1].strip().isdigit() else 0
    # 可精简的米家云服务（停止后重启路由器自动恢复）
    ps_raw = sh("ps w | grep -E 'messagingagent|mosquitto|xq_info_sync_mqtt' | grep -v grep")
    svc_running = {n: (n in ps_raw) for n in ("messagingagent", "mosquitto", "xq_info_sync_mqtt")}
    cfg["cloud_services"] = [{"name": n, "running": svc_running[n]} for n in ("messagingagent", "mosquitto", "xq_info_sync_mqtt")]
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
    cfg["dns_queries"] = queries[-40:][::-1]
    # 定时任务（#panel 标记的行）
    crontab = sh("cat /etc/crontabs/root 2>/dev/null")
    cfg["cron_tasks"] = [l for l in crontab.splitlines() if l.startswith("#panel")]
    # LED 定时计划（从 crontab 的 led_ctl 行解析）
    led_on_t, led_off_t = "", ""
    for l in crontab.splitlines():
        lm = re.match(r"(\d+)\s+(\d+)\s+\*\s+\*\s+\*\s+.*/usr/sbin/led_ctl\s+(led_on|led_off)", l)
        if lm:
            t = "%02d:%02d" % (int(lm.group(2)), int(lm.group(1)))
            if lm.group(3) == "led_on":
                led_on_t = t
            else:
                led_off_t = t
    cfg["led_schedule"] = {"on": led_on_t, "off": led_off_t}
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
    # 本地历史备份列表（不走 SSH）
    cfg["backups"] = list_backups()
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
    h.append('<div class="cfg-panel" style="grid-column:1/-1"><h3>📡 当前模式：有线中继 (AP) <span class="badge ' + ('on' if cfg.get("ssh") else 'off') + '">SSH ' + ('在线' if cfg.get("ssh") else '离线') + '</span></h3>' +
             '<div class="tip">中继模式下，本路由器仅负责 WiFi 接入 + DNS 去广告。' +
             'IP/DHCP/NAT/防火墙/QoS/端口转发/UPnP 均由上级路由器 ' + esc(cfg.get("gateway", "192.168.2.1")) + ' 管理，本面板不提供这些功能。</div>' +
             '<div class="desc">运行 ' + esc(cfg.get("uptime", "")) + ' · 温度 ' + esc(cfg.get("temp", 0)) + '°C · ' +
             'SSH自愈 ' + ('<span class="ok">' + esc(cfg.get("auto_ssh_ver") or "已启用") + '</span>' if cfg.get("auto_ssh") else '<span class="bad">未检测到</span>') + ' · ' +
             'IPv6 ' + ('<span class="ok">已拦截</span>' if cfg.get("ipv6_blocked") else '<span class="warn">未拦截</span>') +
             '（' + str(cfg.get("ipv6_clients", 0)) + ' 台设备在用 IPv6）</div></div>')

    # DNS 上游
    ups = ""
    for s in cfg.get("dns_upstreams", []):
        ups += '<div class="item"><span class="val">' + esc(s) + '</span>' + frm("dns_del", {"server": s}, confirm="删除 " + s + " 吗？", btn_txt="删除", btn_cls="btn red") + '</div>'
    h.append('<div class="cfg-panel"><h3>DNS 上游</h3><div class="tip">💡 中继模式下本路由器作为 DNS 服务器提供去广告。设备需手动设置 DNS 为 ' + HOST + ' 或在上级路由器 DHCP 中指定。国内优先（阿里/腾讯/114），海外备选。</div><div class="desc">当前 ' + str(len(cfg.get("dns_upstreams", []))) + ' 个上游</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;dns_add&quot;}"><input class="inp" name="server" placeholder="例: 223.5.5.5"><button class="btn" type="submit">添加</button></form>' +
             '<div class="list">' + ups + '</div><div class="row">' + frm("dnsmasq_restart", btn_txt="重启 DNS 服务", btn_cls="btn gray") + '</div></div>')

    # DNS 缓存
    h.append('<div class="cfg-panel"><h3>DNS 缓存</h3><div class="tip">💡 缓存越大命中率越高。512-4096 合适，当前 ' + esc(cfg.get("cache_size","")) + '。</div><div class="desc">当前 cache-size = ' + esc(cfg.get("cache_size", "")) + '</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;cache_set&quot;}"><input class="inp" name="size" value="' + esc(cfg.get("cache_size", "1024")) + '"><button class="btn" type="submit">保存</button>' + frm("dns_stats", btn_txt="查看命中率", btn_cls="btn gray") + '</form></div>')

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
    h.append('<div class="cfg-panel"><h3>DNS 查询记录 <span class="badge on">实时</span></h3><div class="tip">💡 实时显示各设备最近 DNS 查询（谁在查什么域名），每 3 秒自动刷新。日志超 300KB 自动清空。</div><div class="desc" id="dq-count">最近 ' + str(len(cfg.get("dns_queries", []))) + ' 条查询</div><div class="list" id="dq-list">' + dq + '</div></div>')

    # 定时任务
    ct = ""
    for line in cfg.get("cron_tasks", []):
        t = line.replace("#panel ", "")
        ct += '<div class="item"><span class="val">' + esc(t) + '</span>' + frm("cron_del", {"line": t}, confirm="删除该任务？", btn_txt="删", btn_cls="btn red") + '</div>'
    h.append('<div class="cfg-panel"><h3>定时任务</h3><div class="tip">💡 格式: 分 时 日 月 周 命令。例 "0 4 * * * reboot" = 每天4点重启路由器。</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;cron_add&quot;}"><input class="inp" name="schedule" placeholder="如 0 4 * * *"><input class="inp" name="command" placeholder="命令 如 reboot"><button class="btn" type="submit">添加</button></form>' +
             '<div class="list">' + ct + '</div></div>')

    # LED
    sched = cfg.get("led_schedule", {})
    on_t = sched.get("on") or "08:00"
    off_t = sched.get("off") or "00:00"
    h.append('<div class="cfg-panel"><h3>LED 指示灯 <span class="badge ' + ('on' if cfg.get("led_blue") else 'off') + '">' + ('亮' if cfg.get("led_blue") else '灭') + '</span></h3>' +
             '<div class="tip">💡 手动开关 + 定时计划。当前计划：' + esc(sched.get("on") or "--:--") + ' 开灯 / ' + esc(sched.get("off") or "--:--") + ' 关灯（定时任务会覆盖手动操作）。</div>' +
             '<div class="row">' + frm("led_toggle", btn_txt=("关灯" if cfg.get("led_blue") else "开灯")) + '</div>' +
             '<form method="post" action="/api/act" class="row"><input type="hidden" name="json" value="{&quot;action&quot;:&quot;led_schedule&quot;}">' +
             '开灯 <input class="inp" name="on" value="' + esc(on_t) + '" style="width:78px" placeholder="HH:MM">' +
             '关灯 <input class="inp" name="off" value="' + esc(off_t) + '" style="width:78px" placeholder="HH:MM">' +
             '<button class="btn gray" type="submit">更新定时</button></form></div>')

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
    bl = ""
    for b in cfg.get("backups", []):
        mt = time.strftime("%m-%d %H:%M", time.localtime(b["mtime"]))
        kb = "%.0f KB" % (b["size"] / 1024.0) if b["size"] < 1024 * 1024 else "%.1f MB" % (b["size"] / 1048576.0)
        bl += '<div class="item"><span class="val">' + esc(b["name"]) + '</span><span style="display:flex;gap:10px;align-items:center"><span style="color:#64748b;font-size:11px">' + mt + ' · ' + kb + '</span><a class="dl" href="/download/' + urllib.parse.quote(b["name"]) + '">下载</a></span></div>'
    h.append('<div class="cfg-panel"><h3>配置备份</h3><div class="tip">💡 一键打包 /etc/config、dnsmasq 配置、定时任务、自愈脚本与去广告列表，拉回本机保存。重启/升级固件前建议先备份。</div>' +
             '<div class="row">' + frm("backup", btn_txt="立即备份", btn_cls="btn green") + '</div>' +
             ('<div class="list">' + bl + '</div>' if bl else '<div class="desc">暂无备份，点击上方按钮创建第一个</div>') + '</div>')

    # 服务精简
    svcs = ""
    for s in cfg.get("cloud_services", []):
        svcs += ('<div class="item"><span class="val">' + s["name"] + '</span><span style="display:flex;gap:8px;align-items:center">' +
                 ('<span class="badge on">运行中</span>' if s["running"] else '<span class="badge off">已停止</span>') +
                 (frm("svc_stop", {"name": s["name"]}, confirm="停止 " + s["name"] + "？重启路由器前不会自动恢复", btn_txt="停止", btn_cls="btn red") if s["running"]
                  else frm("svc_start", {"name": s["name"]}, btn_txt="启动", btn_cls="btn gray")) + '</span></div>')
    h.append('<div class="cfg-panel"><h3>服务精简（降负载）</h3><div class="tip">💡 这三个是米家 App 远程控制用的云服务，自用可停，重启路由器后自动恢复。停用期间米家远程/智能联动不可用；管理页(nginx)与系统日志不受影响。</div><div class="list">' + svcs + '</div></div>')

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
            sh("/usr/sbin/led_ctl led_off; uci set xiaoqiang.common.XLED=0; uci commit xiaoqiang")
            return "LED 已关闭（若处于定时开灯时段，会被定时任务重新打开）"
        sh("/usr/sbin/led_ctl led_on; uci set xiaoqiang.common.XLED=1; uci commit xiaoqiang")
        return "LED 已开启"
    if action == "led_schedule":
        on_t = params.get("on", "").strip()
        off_t = params.get("off", "").strip()
        for t in (on_t, off_t):
            if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t):
                return "时间格式须为 HH:MM（如 08:00）"
        def _cron(t):
            hh, mm = t.split(":")
            return mm + " " + hh
        lines = sh("cat /etc/crontabs/root 2>/dev/null").splitlines()
        if not lines:
            return "读取 crontab 失败"
        keep = [l for l in lines if "led_ctl" not in l]
        keep.append("%s * * * /usr/sbin/led_ctl led_on > /dev/null 2>&1" % _cron(on_t))
        keep.append("%s * * * /usr/sbin/led_ctl led_off > /dev/null 2>&1" % _cron(off_t))
        if not sh_write("cat > /etc/crontabs/root", "\n".join(keep) + "\n"):
            return "写入 crontab 失败"
        sh("/etc/init.d/cron restart")
        return "LED 定时已更新：%s 开灯 / %s 关灯" % (on_t, off_t)
    if action == "backup":
        fn = make_backup()
        if fn:
            return "备份完成：" + fn + "（在下方备份列表可下载）"
        return "备份失败：请检查 SSH 连接后重试"
    if action == "reboot":
        if params.get("confirm") != "yes":
            return "已取消：需确认（confirm=yes）才执行重启"
        sh("(sleep 2; reboot) &")
        return "路由器 2 秒后重启，请等待约 2 分钟"
    if action in ("svc_stop", "svc_start"):
        name = params.get("name", "")
        scripts = {"messagingagent": "messagingagent.sh", "mosquitto": "mosquitto", "xq_info_sync_mqtt": "xq_info_sync_mqtt"}
        if name not in scripts:
            return "未知服务"
        if action == "svc_stop":
            sh("/etc/init.d/%s stop 2>/dev/null; killall %s 2>/dev/null" % (scripts[name], name))
            return "已停止 " + name + "（重启路由器后自动恢复）"
        sh("/etc/init.d/%s start 2>/dev/null" % scripts[name])
        return "已启动 " + name + "（若未起来请重启路由器）"
    if action == "dns_stats":
        sh("kill -USR1 $(pidof dnsmasq)")
        time.sleep(1.5)
        lines = sh("tail -n 30 /tmp/messages").splitlines()
        fwd = local = csize = None
        for l in lines:
            m1 = re.search(r"queries forwarded (\d+), queries answered locally (\d+)", l)
            if m1:
                fwd, local = int(m1.group(1)), int(m1.group(2))
            m2 = re.search(r"cache size (\d+)", l)
            if m2:
                csize = int(m2.group(1))
        if fwd is None or local is None:
            return "未读到 dnsmasq 统计，请稍后重试"
        total = fwd + local
        rate = (local * 100.0 / total) if total else 0
        return "缓存命中率 %.1f%%（本地应答 %d / 转发 %d · 缓存 %s 条）自上次开机累计" % (rate, local, fwd, csize)
    return "未知操作"


PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>小米路由器 中继模式 监控+配置中心</title>
<style>
*{box-sizing:border-box}
:root{--bg0:#0d1117;--bg1:#101820;--card:#161d27;--card2:#1a2330;--line:#263040;--tx:#e8edf4;--tx2:#94a3b8;--acc:#38bdf8;--acc2:#0ea5e9;--ok:#34d399;--warn:#fbbf24;--bad:#f87171}
body{font-family:'Microsoft YaHei','Segoe UI',sans-serif;background:radial-gradient(1100px 560px at 85% -10%,rgba(14,74,110,.35),transparent),radial-gradient(900px 520px at -10% 110%,rgba(6,78,59,.28),transparent),linear-gradient(160deg,var(--bg0),var(--bg1));background-attachment:fixed;color:var(--tx);margin:0;padding:22px;min-height:100vh}
.wrap{max-width:1320px;margin:0 auto}
h1{font-size:21px;margin:0 0 4px;letter-spacing:.5px}
.sub{color:var(--tx2);font-size:12px;margin-bottom:14px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.chip{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--tx2);background:rgba(255,255,255,.03);border:1px solid var(--line);padding:5px 13px;border-radius:99px}
.chip b{color:var(--tx);font-weight:600;font-variant-numeric:tabular-nums}
.dot{width:7px;height:7px;border-radius:50%;background:var(--tx2)}
.dot.ok{background:var(--ok);box-shadow:0 0 7px var(--ok)}
.dot.bad{background:var(--bad);box-shadow:0 0 7px var(--bad)}
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tab{padding:8px 24px;border-radius:10px;cursor:pointer;background:rgba(255,255,255,.03);border:1px solid var(--line);color:var(--tx2);font-size:13px;transition:all .2s;user-select:none}
.tab:hover{color:var(--tx);border-color:#3a4a60}
.tab.active{background:linear-gradient(135deg,#0c4a6e,#075985);border-color:#0284c7;color:#fff;box-shadow:0 2px 14px rgba(2,132,199,.35)}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.card{background:linear-gradient(160deg,var(--card),var(--card2));border-radius:14px;padding:14px 18px;min-width:138px;border:1px solid var(--line);box-shadow:0 4px 18px rgba(0,0,0,.28);transition:all .2s}
.card:hover{transform:translateY(-2px);border-color:#3b5675}
.card .v{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.card .l{font-size:12px;color:var(--tx2);margin-top:5px}
.card .s{font-size:11px;margin-top:3px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.panel{background:linear-gradient(160deg,var(--card),var(--card2));border-radius:14px;padding:14px 16px;border:1px solid var(--line);box-shadow:0 4px 18px rgba(0,0,0,.28)}
.panel h3{font-size:13px;margin:0 0 8px;color:var(--tx2);display:flex;justify-content:space-between}
canvas{width:100%;height:150px;display:block}
.cfg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(370px,1fr));gap:14px}
.cfg-panel{background:linear-gradient(160deg,var(--card),var(--card2));border-radius:14px;padding:16px 18px;border:1px solid var(--line);box-shadow:0 4px 18px rgba(0,0,0,.28)}
.cfg-panel h3{font-size:14px;margin:0 0 8px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.badge{font-size:11px;padding:3px 10px;border-radius:20px;font-weight:600}
.badge.on{background:#123527;color:var(--ok);border:1px solid #1f5c40}
.badge.off{background:#3a1b1f;color:var(--bad);border:1px solid #6e2730}
.tip{font-size:11px;color:#a5c8e8;background:#122131;border-left:3px solid var(--acc2);padding:7px 10px;border-radius:6px;margin-bottom:10px;line-height:1.6}
.cfg-panel .desc{font-size:12px;color:var(--tx2);margin-bottom:10px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center}
.btn{padding:6px 14px;border-radius:8px;border:1px solid #0284c7;background:linear-gradient(135deg,#0c4a6e,#075985);color:#fff;cursor:pointer;font-size:12px;transition:all .15s}
.btn:hover{filter:brightness(1.25);box-shadow:0 2px 10px rgba(2,132,199,.3)}
.btn.gray{background:#2a333f;border-color:#3c4856}
.btn.red{border-color:#b3454f;background:linear-gradient(135deg,#5f1f27,#7f2733)}
.btn.green{border-color:#2f9e57;background:linear-gradient(135deg,#14532d,#166534)}
.inp{padding:6px 10px;border-radius:8px;border:1px solid #334155;background:#0f1520;color:var(--tx);font-size:12px;width:110px;outline:none;transition:all .15s}
.inp:focus{border-color:var(--acc2);box-shadow:0 0 0 2px rgba(2,132,199,.22)}
.inp.wide{width:180px}
.val{font-family:Consolas,monospace;font-size:12px;color:#a5c8e8;background:#0f1520;padding:4px 8px;border-radius:6px;display:inline-block;margin:2px;border:1px solid #1f2c3d}
.list{max-height:160px;overflow-y:auto;margin-top:8px;border-radius:8px}
.list .item{display:flex;justify-content:space-between;align-items:center;gap:6px;padding:5px 8px;border-bottom:1px solid #1f2937;font-size:12px}
.list .item:hover{background:rgba(255,255,255,.04)}
.msg{position:fixed;bottom:20px;right:20px;background:rgba(12,74,110,.94);color:#fff;padding:11px 20px;border-radius:10px;display:none;font-size:13px;z-index:9;border:1px solid #0284c7;box-shadow:0 6px 24px rgba(0,0,0,.45)}
a.dl{color:var(--acc);text-decoration:none;font-size:12px}
a.dl:hover{text-decoration:underline}
@media (max-width:900px){.grid{grid-template-columns:1fr}}
</style></head><body>
<div class="wrap">
<h1>小米路由器 有线中继模式 监控 + 配置中心</h1>
<div class="sub">SSH: %HOST% · 中继模式（AP）· DNS去广告 + WiFi管理 · 温度阈值：绿&lt;85 / 橙85-90 / 红&gt;90</div>
<div class="chips">
 <span class="chip"><span class="dot" id="chip-dot"></span>面板 <b id="chip-ssh">连接中</b></span>
 <span class="chip">运行 <b>%UPTIME%</b></span>
 <span class="chip">负载 <b id="chip-load">--</b></span>
 <span class="chip">采集开销 <b id="chip-collect">--</b></span>
 <span class="chip">SSH自愈 <b>%AUTOSSH%</b></span>
 <span class="chip">IPv6 <b>%IPV6%</b></span>
</div>
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
function fmtMB(m){return m>=1024?(m/1024).toFixed(1)+' GB':Math.round(m)+' MB';}
function refresh(){
 fetch('/api').then(function(r){return r.json();}).then(function(d){
  var L=d.latest;
  var cs=document.getElementById('chip-ssh'),cd=document.getElementById('chip-dot'),cl=document.getElementById('chip-load'),cc=document.getElementById('chip-collect');
  if(cs)cs.textContent='在线';if(cd)cd.className='dot ok';if(cl)cl.textContent=L.load;if(cc)cc.textContent=L.collect_ms+' ms';
  document.getElementById('c-cpu').textContent=L.cpu+'%';
  var sc=document.getElementById('s-cpu');sc.textContent=L.cpu<50?'空闲':(L.cpu<85?'中载':'高载');sc.className='s '+(L.cpu<50?'ok':(L.cpu<85?'warn':'bad'));
  var pct=L.mem_total_mb>0?Math.round(L.mem_used_mb/L.mem_total_mb*100):0;
  document.getElementById('c-mem').textContent=fmtMB(L.mem_used_mb)+'/'+fmtMB(L.mem_total_mb);
  var sm=document.getElementById('s-mem');sm.textContent=pct+'% 已用';sm.className='s '+(pct<70?'ok':(pct<90?'warn':'bad'));
  document.getElementById('c-temp').textContent=L.temp+' °C';
  var st=document.getElementById('s-temp');st.textContent=L.temp<85?'正常':(L.temp<90?'偏热':'过热');st.className='s '+(L.temp<85?'ok':(L.temp<90?'warn':'bad'));
  document.getElementById('c-rx').textContent=fmtKB(L.rx);
  document.getElementById('c-tx').textContent=fmtKB(L.tx);
  document.getElementById('c-conn').textContent=L.conn;
  document.getElementById('t-cpu').textContent='当前 '+L.cpu+'%';
  document.getElementById('t-mem').textContent='已用 '+fmtMB(L.mem_used_mb)+' / 总 '+fmtMB(L.mem_total_mb);
  document.getElementById('t-temp').textContent='当前 '+L.temp+'°C';
  document.getElementById('t-net').textContent='↓'+fmtKB(L.rx)+' ↑'+fmtKB(L.tx);
  draw('g-cpu',d.cpu,'#4fc3f7',true,100,'%');
  draw('g-mem',d.mem_used_mb,'#81c784',true,null,'MB');
  draw('g-temp',d.temp,'#ffb74d',true,null,'°C');
  var sum=d.rx.map(function(v,i){return v+(d.tx[i]||0);});
  var mx=Math.max.apply(null,sum.length?sum:[0])||0,net=sum,nu=' KB/s';
  if(mx>=1024){net=sum.map(function(v){return v/1024;});nu=' MB/s';}
  draw('g-net',net,'#f06292',true,null,nu);
 }).catch(function(){
  var cs=document.getElementById('chip-ssh'),cd=document.getElementById('chip-dot');
  if(cs)cs.textContent='离线';if(cd)cd.className='dot bad';
 });
}
setInterval(refresh,2000);refresh();
function jesc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function refreshDq(){
 var tc=document.getElementById('tab-cfg');
 if(!tc||tc.style.display==='none')return;
 fetch('/api/dnsquery').then(function(r){return r.json();}).then(function(d){
  var el=document.getElementById('dq-list');if(!el)return;
  var h='';
  d.queries.forEach(function(x){h+='<div class="item"><span class="val">'+jesc(x.ip)+'</span><span class="val">['+jesc(x.type)+']</span><span class="val">'+jesc(x.domain)+'</span></div>';});
  el.innerHTML=h||'<div class="desc">暂无查询</div>';
  var dc=document.getElementById('dq-count');if(dc)dc.textContent='最近 '+d.queries.length+' 条查询';
 }).catch(function(){});
}
setInterval(refreshDq,3000);
setTimeout(showUrlMsg,300);
</script></div></body></html>
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
                                "conn": history["conn"][-1] if history["conn"] else 0,
                                "load": history["load"][-1] if history["load"] else 0,
                                "collect_ms": collect_ms}}
            self._send(200, d)
        elif self.path.startswith("/download/"):
            name = os.path.basename(urllib.parse.unquote(self.path[len("/download/"):]))
            fpath = os.path.join(BACKUP_DIR, name)
            if not name.endswith(".tar.gz") or not os.path.isfile(fpath):
                self._send(404, {"ok": False, "error": "备份文件不存在"})
                return
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/dnsquery":
            queries = []
            for line in sh("tail -n 80 /tmp/dnsquery.log 2>/dev/null").splitlines():
                m = re.search(r"query\[([A-Z0-9]+)\] ([^ ]+) from ([0-9.]+)", line)
                if m:
                    queries.append({"type": m.group(1), "domain": m.group(2), "ip": m.group(3)})
            queries.reverse()
            self._send(200, {"queries": queries})
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
                cfg_data = {}
                cfg_html = '<div class="cfg-grid"><div class="cfg-panel"><h3>错误</h3><div class="desc">' + esc(str(e)) + '</div></div></div>'
            body = PAGE.replace("%HOST%", HOST)
            body = body.replace("%UPTIME%", esc(cfg_data.get("uptime", "?")))
            body = body.replace("%AUTOSSH%", esc(cfg_data.get("auto_ssh_ver") or ("已启用" if cfg_data.get("auto_ssh") else "未启用")))
            body = body.replace("%IPV6%", "已拦截" if cfg_data.get("ipv6_blocked") else "未拦截")
            body = body.replace('<div class="cfg-grid" id="cfg-grid">加载中...</div>', cfg_html).encode("utf-8")
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
    if not PASSWD:
        print("[!] 未提供路由器密码：请设置环境变量 ROUTER_PASSWD 或使用 --passwd 参数")
        sys.exit(1)

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
