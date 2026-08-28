# -*- coding: utf-8 -*-
"""
小米路由器 有线中继模式 监控 + 配置中心
- 性能监控: CPU/内存/温度/流量/连接数
- 配置中心: DNS上游 / DNS缓存 / 去广告 / 自定义屏蔽 / WiFi信道功率 / LED / 定时任务
- 中继模式下已移除: QoS / 端口转发 / 防火墙规则 / DHCP租期 / UPnP / 静态绑定（均由上级路由器管理）
用法: python router_monitor_ap.py
"""
import sys, os, json, time, threading, argparse, re
from collections import deque

import monitor_web

HOST = os.environ.get("ROUTER_HOST", "192.168.31.1")
SSHPORT = int(os.environ.get("ROUTER_SSH_PORT", "22"))
USER = os.environ.get("ROUTER_USER", "root")
PASSWD = os.environ.get("ROUTER_PASSWD", "")
WEBPORT = 8787
INTERVAL = 3
MAX_POINTS = 300
# 面板管理的 LED 定时行标准格式（只匹配它，用户手写的其它 led_ctl 变体不会被当成面板的）
LED_CRON_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+\*\s+\*\s+\*\s+/usr/sbin/led_ctl\s+led_(on|off)\b")

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
            sent = False
            try:
                if ssh_client is None or not ssh_client.get_transport() or not ssh_client.get_transport().is_active():
                    ssh_client = ssh_connect()
                stdin, stdout, stderr = ssh_client.exec_command(cmd, timeout=timeout)
                sent = True
                return stdout.read().decode("utf-8", "replace").strip()
            except Exception:
                if sent:
                    break  # 命令已下发（写读超时），重发会使非幂等命令执行两次
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
    """执行命令并通过 stdin 写入数据；写后回读字节数校验（关键防线：/data 卷满时
    截断重定向会"成功"但内容静默丢失，曾把 crontab 清成 0 字节）。校验不过返回 False"""
    global ssh_client
    wm = re.match(r"cat\s*(>>?)\s*(\S+)", cmd.strip())
    path, mode = (wm.group(2), wm.group(1)) if wm else (None, None)
    nbytes = len(data.encode("utf-8"))
    with ssh_lock:
        before = ""
        if path and mode == ">>":
            before = _sh_nolock("wc -c < %s 2>/dev/null" % path)
        for _ in range(2):
            sent = False
            try:
                if ssh_client is None or not ssh_client.get_transport() or not ssh_client.get_transport().is_active():
                    ssh_client = ssh_connect()
                stdin, stdout, stderr = ssh_client.exec_command(cmd, timeout=timeout)
                sent = True
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
                if sent:
                    break
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
                "tar czf %s -C / etc/config etc/crontabs/root tmp/dnsmasq.d data 2>/dev/null; wc -c < %s" % (remote, remote),
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
    global collect_ms, collect_fails
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
        collect_fails += 1
        return
    collect_fails = 0

    st = parts[0].strip()
    if st.startswith("cpu"):
        p = st.split()
        try:
            idle = int(p[4]) + int(p[5])
            total = sum(int(x) for x in p[1:9])
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
    global collect_fails
    while True:
        try:
            collect()
        except Exception:
            collect_fails += 1
        time.sleep(INTERVAL)


def get_config():
    cfg = {"host": HOST}
    # 只读状态合并为单次 SSH 往返（原 20+ 次独立命令会与 3 秒采集循环抢 ssh_lock，界面卡顿）
    parts = sh("; echo '@@'; ".join([
        "cat /tmp/dnsmasq.d/98-upstream.conf 2>/dev/null",                                 # 0
        "uci get dhcp.@dnsmasq[0].cachesize 2>/dev/null",                                  # 1
        "test -f /tmp/dnsmasq.d/96-antiad.conf && wc -l < /tmp/dnsmasq.d/96-antiad.conf",  # 2
        "test -f /tmp/dnsmasq.d/90-awavenue.conf && wc -l < /tmp/dnsmasq.d/90-awavenue.conf",  # 3
        "ls /tmp/dnsmasq.d/ 2>/dev/null; test -f /data/.adblock_off && echo ADOFF",           # 4
        "cat /tmp/dnsmasq.d/97-custom.conf 2>/dev/null",                                   # 5
        "ps | grep dropbear | grep -v grep",                                               # 6
        "test -f /data/auto_ssh/auto_ssh.sh && sed -n 2p /data/auto_ssh/auto_ssh.sh",      # 7
        "uptime",                                                                          # 8
        "ip6tables -C FORWARD -j REJECT --reject-with icmp6-adm-prohibited 2>/dev/null && echo BLOCKED",  # 9
        "ip -6 neigh show dev br-lan 2>/dev/null | grep lladdr | grep -cv '^fe80'",        # 10
        "ps w | grep -E 'messagingagent|mosquitto|xq_info_sync_mqtt' | grep -v grep",      # 11
        "ip route show default 2>/dev/null | awk '{print $3; exit}'",                      # 12
        "cat /tmp/dhcp.leases 2>/dev/null",                                                # 13
        "ip neigh show dev br-lan 2>/dev/null",                                            # 14
        "cat /etc/crontabs/root 2>/dev/null",                                              # 15
        "uci get xiaoqiang.common.XLED 2>/dev/null",                                       # 16
        "uci show wireless 2>/dev/null",                                                   # 17
        # 截断必须跟 SIGHUP，否则 dnsmasq 写入偏移不变会造出稀疏空洞
        "[ $(wc -c < /tmp/dnsquery.log 2>/dev/null || echo 0) -gt 307200 ] "
        "&& { > /tmp/dnsquery.log; kill -HUP $(pidof dnsmasq); }; "
        "grep -F 'query[' /tmp/dnsquery.log 2>/dev/null | tail -n 120",                    # 18
        "stat -c %Y /data/awavenue.gz 2>/dev/null",                                        # 19
        "stat -c %Y /data/antiad.gz 2>/dev/null",                                          # 20
    ])).split("@@")
    if len(parts) < 21:
        parts += [""] * (21 - len(parts))

    def seg(i):
        return parts[i].strip()

    def num(i):
        s = seg(i).split()
        return int(s[0]) if s and s[0].isdigit() else 0

    cfg["dns_upstreams"] = [l.replace("server=", "").strip()
                            for l in parts[0].splitlines() if l.startswith("server=")]
    cfg["cache_size"] = seg(1) or "150"
    cfg["adblock_antiad"] = num(2)
    cfg["adblock_domestic"] = num(3)

    def age(i):
        s = seg(i)
        if not s.isdigit():
            return None
        return max(0.0, round((time.time() - int(s)) / 86400.0, 1))

    cfg["adblock_domestic_age_d"] = age(19)
    cfg["adblock_antiad_age_d"] = age(20)
    cfg["adblock_enabled"] = "ADOFF" not in parts[4]
    cfg["custom_adblock"] = [re.sub(r"^address=/(.*)/.*$", r"\1", l).strip()
                             for l in parts[5].splitlines() if l.startswith("address=/")]
    cfg["ssh"] = "dropbear" in parts[6]
    auto_ssh_raw = seg(7)
    cfg["auto_ssh"] = bool(auto_ssh_raw)
    vm = re.search(r"v\d+\s*\([^)]*\)", auto_ssh_raw)
    cfg["auto_ssh_ver"] = vm.group(0) if vm else ""
    cfg["uptime"] = seg(8).split(",")[0].strip()
    cfg["temp"] = history["temp"][-1] if history["temp"] else 0
    cfg["ipv6_blocked"] = "BLOCKED" in seg(9)
    cfg["ipv6_clients"] = num(10)
    # 可精简的米家云服务（停止后重启路由器自动恢复）
    ps_raw = parts[11]
    svc_running = {n: (n in ps_raw) for n in ("messagingagent", "mosquitto", "xq_info_sync_mqtt")}
    cfg["cloud_services"] = [{"name": n, "running": svc_running[n]} for n in ("messagingagent", "mosquitto", "xq_info_sync_mqtt")]
    cfg["gateway"] = seg(12)
    # 在线设备（中继模式：实时 ARP 邻居表，DHCP 租约仅补主机名）
    leases = {}
    for line in parts[13].splitlines():
        p = line.split()
        if len(p) >= 4:
            leases[p[1]] = p[3]
    devices = []
    for line in parts[14].splitlines():
        p = line.split()
        if len(p) >= 4 and p[0].startswith("192.168.") and "lladdr" in p:
            if cfg["gateway"] and p[0] == cfg["gateway"]:
                continue
            mac = p[p.index("lladdr") + 1]
            devices.append({"ip": p[0], "mac": mac, "host": leases.get(mac, ""), "state": p[-1]})
    cfg["devices"] = devices
    cfg["dns_queries"] = parse_dns_queries(parts[18])
    # 定时任务（行尾 #panel 标记归属面板的 cron 行；LED 定时行单独可见）
    crontab = parts[15].splitlines()
    cfg["cron_tasks"] = [l.rstrip()[:-7].strip() for l in crontab if l.rstrip().endswith("#panel")] + \
                        [l.rstrip() for l in crontab if LED_CRON_RE.search(l) and not l.rstrip().endswith("#panel")]
    # LED 定时计划（只认面板管理的标准格式行，避免把手写的其它 LED 定时当成自己的）
    led_on_t, led_off_t = "", ""
    for l in crontab:
        lm = LED_CRON_RE.match(l)
        if lm:
            t = "%02d:%02d" % (int(lm.group(2)), int(lm.group(1)))
            if lm.group(3) == "led_on":
                led_on_t = t
            else:
                led_off_t = t
    cfg["led_schedule"] = {"on": led_on_t, "off": led_off_t}
    # LED（用官方 XLED 状态, 与 led_ctl 一致; 读 uci 而非直接写 sysfs）
    cfg["led_blue"] = seg(16) == "1"
    # WiFi 状态（单次 uci show 拉回, 本地解析）
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
    # 中继模式信息
    cfg["mode"] = "ap"
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
            data, _ = s.recvfrom(4096)
            # 校验应答 tid 与查询一致，避免把无关包当成功
            if len(data) >= 2 and struct.unpack(">H", data[:2])[0] == tid:
                ok.append((time.time() - t0) * 1000)
        except Exception:
            pass
        s.close()
    return round(sum(ok) / len(ok), 1) if ok else -1


def run_net_test():
    """实时网络测试：外网延迟 + DNS + 手机链路（宽带测速走中科大）"""
    import subprocess, time as _t
    res = []
    # Windows ping 用 -n(次数)/-w(毫秒超时)，POSIX 用 -c/-W(秒)
    ping_args = ["ping", "-n", "4", "-w", "2000"] if os.name == "nt" else ["ping", "-c", "4", "-W", "2"]
    for host in ["223.5.5.5", "119.29.29.29"]:
        try:
            out = subprocess.run(ping_args + [host], capture_output=True, text=True, timeout=15)
            m = re.search(r"(?:平均|Average)\s*=\s*(\d+)", out.stdout) or \
                re.search(r"=\s*[\d.]+/([\d.]+)/", out.stdout)
            res.append("本机ping外网 %s: %s ms" % (host, m.group(1) if m else "?"))
        except Exception:
            res.append("本机ping外网 %s: 失败" % host)
    t0 = _t.time()
    sh("nslookup www.baidu.com 127.0.0.1 >/dev/null 2>&1")
    res.append("路由器侧解析(含SSH往返): %.0f ms" % ((_t.time() - t0) * 1000))
    # 手机链路：ARP 邻居表按状态优先级取一台确实可达的主机（dhcp.leases 既不按时序排序、字段序也随固件变化）
    gw = sh("ip route show default 2>/dev/null | awk '{print $3; exit}'").strip()
    neigh = sh("ip neigh show dev br-lan 2>/dev/null").splitlines()
    mip = ""
    for state in ("REACHABLE", "STALE", "DELAYED", "PROBE"):
        for line in neigh:
            p = line.split()
            if len(p) >= 4 and p[0].startswith("192.168.") and p[-1] == state \
                    and p[0] != gw and p[0] != HOST:
                mip = p[0]
                break
        if mip:
            break
    if mip:
        r = sh("ping -c 3 " + mip + " 2>/dev/null | grep -o 'avg = [0-9.]*'")
        res.append("设备WiFi链路(" + mip + "): " + (r if r else "设备未响应"))
    else:
        res.append("设备WiFi链路: 无在线设备")
    return "\n".join(res)


def get_dns_stats():
    """dnsmasq 内置统计（SIGUSR1 转储）。等待放在路由器侧：出现新转储行即刻返回，
    固定 sleep 在负载高时会读到上一次的旧统计。"""
    raw = sh(
        "n=$(grep -c 'queries forwarded' /tmp/dnsquery.log 2>/dev/null); n=${n:-0}; "
        "kill -USR1 $(pidof dnsmasq); i=0; "
        "while [ $i -lt 25 ]; do i=$((i+1)); sleep 0.2; "
        "[ -e /tmp/dnsquery.log ] || break; "
        "m=$(grep -c 'queries forwarded' /tmp/dnsquery.log 2>/dev/null); m=${m:-0}; "
        "[ $m -gt $n ] && break; done; "
        "grep 'queries forwarded' /tmp/dnsquery.log 2>/dev/null | tail -n 1; echo '@@'; "
        "grep 'cache size' /tmp/dnsquery.log 2>/dev/null | tail -n 1", timeout=20)
    p = raw.split("@@")
    fwd = local = csize = None
    m1 = re.search(r"queries forwarded (\d+), queries answered locally (\d+)", p[0] if p else "")
    if m1:
        fwd, local = int(m1.group(1)), int(m1.group(2))
    if len(p) > 1:
        m2 = re.search(r"cache size (\d+)", p[1])
        if m2:
            csize = int(m2.group(1))
    if fwd is None or local is None:
        return None
    total = fwd + local
    return {"rate": round(local * 100.0 / total, 1) if total else 0, "local": local, "fwd": fwd, "cache": csize}


AD_SKIP = "byteimg|pstatp|douyinpic|douyin|bytecdn|bytedance"
AWAVENUE_URLS = [
    "https://cdn.jsdelivr.net/gh/TG-Twilight/AWAvenue-Ads-Rule@main/Filters/AWAvenue-Ads-Rule-Dnsmasq.conf",
    "https://raw.githubusercontent.com/TG-Twilight/AWAvenue-Ads-Rule/main/Filters/AWAvenue-Ads-Rule-Dnsmasq.conf",
]


def _adblock_off():
    return sh("test -f /data/.adblock_off && echo y") == "y"


def update_antiad():
    # 下载耗时远超 sh() 默认超时，必须单独放宽，否则读流超时会被判失败并重复触发下载
    sh("curl -sL 'https://anti-ad.net/anti-ad-for-dnsmasq.conf' -o /tmp/antiad_raw "
       "--connect-timeout 15 --max-time 90", timeout=100)
    raw = sh("wc -c < /tmp/antiad_raw 2>/dev/null").strip()
    # 与 auto_ssh.sh 相同的体积门槛：下载不完整时绝不覆盖已有缓存
    if not raw.isdigit() or int(raw) < 500000:
        sh("rm -f /tmp/antiad_raw")
        return "下载未完成（%s 字节），已保留原有 anti-AD 缓存" % (raw or "0")
    sh("grep -vE '" + AD_SKIP + "' /tmp/antiad_raw > /tmp/antiad_new.conf")
    n = sh("wc -l < /tmp/antiad_new.conf 2>/dev/null").strip()
    if not n.isdigit() or int(n) < 1000:
        sh("rm -f /tmp/antiad_raw /tmp/antiad_new.conf")
        return "过滤后条目异常（%s 行），已保留原有 anti-AD 缓存" % (n or "0")
    # /data 仅 1.7MB，.new 双份落盘会撑爆卷（crontab 清空事故根因）——与 auto_ssh v5 一致：/tmp 暂存→删旧→cat 写新
    sh("gzip -c /tmp/antiad_new.conf > /tmp/antiad_new.gz && "
       "{ rm -f /data/antiad.gz; cat /tmp/antiad_new.gz > /data/antiad.gz; }; "
       "rm -f /tmp/antiad_raw /tmp/antiad_new.gz")
    if _adblock_off():
        sh("rm -f /tmp/antiad_new.conf")
        return "缓存已更新 " + n + " 条（去广告处于关闭状态，开启后生效）"
    sh("mv -f /tmp/antiad_new.conf /tmp/dnsmasq.d/96-antiad.conf; /etc/init.d/dnsmasq restart")
    return "已更新 " + n + " 条"


def update_awavenue():
    got = False
    for u in AWAVENUE_URLS:
        sh("curl -sL '" + u + "' -o /tmp/awv_raw --connect-timeout 15 --max-time 60", timeout=80)
        sz = sh("wc -c < /tmp/awv_raw 2>/dev/null").strip()
        if sz.isdigit() and int(sz) > 12000:
            got = True
            break
        sh("rm -f /tmp/awv_raw")
    if not got:
        return "两个镜像均未下载成功，已保留原有 AWAvenue 缓存"
    sh("grep '^address=/' /tmp/awv_raw | grep -vE '" + AD_SKIP + "' > /tmp/awv_new.conf; rm -f /tmp/awv_raw")
    n = sh("wc -l < /tmp/awv_new.conf 2>/dev/null").strip()
    if not n.isdigit() or int(n) < 300:
        sh("rm -f /tmp/awv_new.conf")
        return "有效条目异常（%s 行），已保留原有 AWAvenue 缓存" % (n or "0")
    # 与 anti-AD 同款落盘：/tmp 暂存→删旧→cat 写新，防 .new 双份撑爆 /data
    sh("gzip -c /tmp/awv_new.conf > /tmp/awv_new.gz && "
       "{ rm -f /data/awavenue.gz; cat /tmp/awv_new.gz > /data/awavenue.gz; }; "
       "rm -f /tmp/awv_new.gz")
    if _adblock_off():
        sh("rm -f /tmp/awv_new.conf")
        return "缓存已更新 " + n + " 条（去广告处于关闭状态，开启后生效）"
    sh("mv -f /tmp/awv_new.conf /tmp/dnsmasq.d/90-awavenue.conf; /etc/init.d/dnsmasq restart")
    return "已更新 " + n + " 条"


def do_action(action, params=None):
    params = params or {}
    if action == "adblock_toggle":
        off = sh("test -f /data/.adblock_off && echo y") == "y"
        if off:
            sh("rm -f /data/.adblock_off /tmp/dnsmasq.d/99-adblock.conf; "
               "[ -s /data/antiad.gz ] && zcat /data/antiad.gz > /tmp/dnsmasq.d/96-antiad.conf 2>/dev/null; "
               "[ -s /data/awavenue.gz ] && zcat /data/awavenue.gz > /tmp/dnsmasq.d/90-awavenue.conf 2>/dev/null; "
               "/etc/init.d/dnsmasq restart")
            return "去广告已开启（anti-AD 主列表 + AWAvenue 国内补充，自 /data 缓存恢复）"
        sh("touch /data/.adblock_off; rm -f /tmp/dnsmasq.d/96-antiad.conf /tmp/dnsmasq.d/90-awavenue.conf "
           "/tmp/dnsmasq.d/99-adblock.conf; /etc/init.d/dnsmasq restart")
        return "去广告已全部关闭（含 anti-AD 主列表 + AWAvenue）；状态持久化，重启后自愈脚本也不会拉起"
    if action == "awavenue_update":
        return update_awavenue()
    if action == "antiad_update":
        return update_antiad()
    if action == "adblock_update":
        return "anti-AD: " + update_antiad() + " ｜ AWAvenue: " + update_awavenue()
    if action == "dnsmasq_restart":
        sh("/etc/init.d/dnsmasq restart")
        return "dnsmasq 已重启"
    if action == "cache_set":
        v = str(params.get("size", "")).strip()
        if not v.isdigit() or not (64 <= int(v) <= 100000):
            return "缓存大小须为 64-100000 的整数"
        v = str(int(v))
        sh("uci set dhcp.@dnsmasq[0].cachesize='" + v + "'; uci commit dhcp; /etc/init.d/dnsmasq restart")
        return "DNS 缓存已设为 " + v
    if action == "dns_add":
        s = params.get("server", "").strip()
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", s) or any(int(o) > 255 for o in s.split(".")):
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
        sh("echo 'address=/" + d + "/0.0.0.0' >> /tmp/dnsmasq.d/97-custom.conf; /etc/init.d/dnsmasq restart")
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
        sh("/etc/init.d/dnsmasq restart")
        return "已解除屏蔽 " + d
    if action == "wifi_channel":
        band = params.get("band", "5g")
        ch = str(params.get("channel", "0")).strip()
        ifname = "wl1" if band == "2g" else "wl0"
        if not ch.isdigit():
            return "信道须为数字"
        if ch == "0":
            return "请选择具体信道（自动模式重启后恢复）"
        lo, hi = (1, 13) if band == "2g" else (32, 177)
        if not (lo <= int(ch) <= hi):
            return "2.4G 信道范围 1-13，5G 信道范围 32-177"
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
        cn_pool = ["223.5.5.5", "223.6.6.6", "119.29.29.29", "182.254.116.116", "114.114.114.114",
                   "114.114.115.115", "1.2.4.8", "101.101.101.101", "180.184.1.1", "180.184.2.2",
                   "4.2.2.1", "4.2.2.2"]
        os_pool = ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9", "208.67.222.222"]
        cur = [l.strip().split("server=")[1] for l in sh("cat /tmp/dnsmasq.d/98-upstream.conf").splitlines() if l.startswith("server=")]
        cn_pool = cn_pool + [ip for ip in cur if ip not in os_pool and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) and ip not in cn_pool]

        def pick(pool, n):
            alive = [ip for ip in pool if dns_latency(ip, tries=1) >= 0]
            ok = sorted((dns_latency(ip, tries=2), ip) for ip in alive)
            return [ip for ms, ip in ok[:n] if ms >= 0]

        cn_top = pick(cn_pool, 4)
        os_top = pick(os_pool, 4)
        if len(cn_top) < 2:
            return "国内 DNS 可用太少，未做变更"
        ms_use = "4.2.2.1" if dns_latency("4.2.2.1", tries=1) >= 0 else "4.2.2.2"
        top = cn_top + os_top
        sh("rm -f /tmp/dnsmasq.d/98-upstream.conf")
        for ip in top:
            sh("echo 'server=" + ip + "' >> /tmp/dnsmasq.d/98-upstream.conf")
        ms_domains = "microsoft.com microsoftonline.com msftconnecttest.com windowsupdate.com live.com live.net office365.com office.com onedrive.com microsoft.io"
        sh("rm -f /tmp/dnsmasq.d/91-microsoft.conf; for d in " + ms_domains + "; do echo \"server=/" + "$d/" + ms_use + "\" >> /tmp/dnsmasq.d/91-microsoft.conf; done; cp /tmp/dnsmasq.d/91-microsoft.conf /data/microsoft.conf")
        sh("uci set dhcp.@dnsmasq[0].server='" + " ".join(top) + "' 2>/dev/null; uci commit dhcp 2>/dev/null")
        sh("uci set dhcp.@dnsmasq[0].allservers=1 2>/dev/null; uci commit dhcp 2>/dev/null")
        sh("/etc/init.d/dnsmasq restart")
        return "国内4: %s；国外4: %s；微软系域名专用→%s。%d 个通用上游并行全查、最快应答生效；DNS 已重启并持久化" % (
            " ".join(cn_top), " ".join(os_top), ms_use, len(top))
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
        if not lines or lines == [""]:
            return "读取 crontab 失败（为空或 SSH 异常），已放弃写入以防清空"
        # 只删面板管理的标准格式 LED 行，用户手写的其它 led_ctl 变体不动
        keep = [l for l in lines if not LED_CRON_RE.match(l)]
        keep.append("%s * * * /usr/sbin/led_ctl led_on > /dev/null 2>&1 #panel-led" % _cron(on_t))
        keep.append("%s * * * /usr/sbin/led_ctl led_off > /dev/null 2>&1 #panel-led" % _cron(off_t))
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
    return "未知操作"


def api_snapshot():
    with data_lock:
        return {"cpu": list(history["cpu"]), "mem_used_mb": list(history["mem_used_mb"]),
                "mem_total_mb": list(history["mem_total_mb"]), "temp": list(history["temp"]),
                "rx": list(history["rx"]), "tx": list(history["tx"]), "conn": list(history["conn"]),
                "load": list(history["load"]),
                "latest": {"cpu": history["cpu"][-1] if history["cpu"] else 0,
                           "mem_used_mb": history["mem_used_mb"][-1] if history["mem_used_mb"] else 0,
                           "mem_total_mb": history["mem_total_mb"][-1] if history["mem_total_mb"] else 0,
                           "temp": history["temp"][-1] if history["temp"] else 0,
                           "rx": history["rx"][-1] if history["rx"] else 0,
                           "tx": history["tx"][-1] if history["tx"] else 0,
                           "conn": history["conn"][-1] if history["conn"] else 0,
                           "load": history["load"][-1] if history["load"] else 0,
                           "collect_ms": collect_ms,
                           "ssh_ok": collect_fails < 3}}


def parse_dns_queries(raw, limit=40):
    out = []
    for line in raw.splitlines():
        m = re.search(r"query\[([A-Z0-9]+)\] ([^ ]+) from ([0-9.]+)", line)
        if m:
            out.append({"type": m.group(1), "domain": m.group(2), "ip": m.group(3)})
    out.reverse()
    return out[:limit]


def get_dns_queries(limit=40):
    # 必须先过滤：dnsmasq 每次查询产生 query/forwarded/reply 多行，直接 tail 会被噪音挤占窗口
    return parse_dns_queries(sh("grep -F 'query[' /tmp/dnsquery.log 2>/dev/null | tail -n 120"), limit)


def read_backup(name):
    fn = os.path.basename(name)
    if not fn.endswith(".tar.gz"):
        return None
    fpath = os.path.join(BACKUP_DIR, fn)
    if not os.path.isfile(fpath):
        return None
    with open(fpath, "rb") as f:
        return f.read()


def main():
    global HOST, SSHPORT, USER, PASSWD, WEBPORT
    ap = argparse.ArgumentParser(description="小米路由器 中继模式 监控+配置中心")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=SSHPORT)
    ap.add_argument("--user", default=USER)
    ap.add_argument("--passwd", default=PASSWD)
    ap.add_argument("--web", type=int, default=WEBPORT)
    ap.add_argument("--lan", action="store_true", help="绑定所有网卡允许局域网访问（必须同时提供 --token）")
    ap.add_argument("--token", default=os.environ.get("ROUTER_PANEL_TOKEN", ""),
                    help="面板访问令牌（HTTP Basic，用户名任意、密码为令牌）")
    a = ap.parse_args()
    HOST, SSHPORT, USER, PASSWD, WEBPORT = a.host, a.port, a.user, a.passwd, a.web
    if not PASSWD:
        print("[!] 未提供路由器密码：请设置环境变量 ROUTER_PASSWD 或使用 --passwd 参数")
        sys.exit(1)
    if a.lan and not a.token:
        print("[!] --lan 会暴露到局域网，必须设置 --token（或环境变量 ROUTER_PANEL_TOKEN）")
        sys.exit(1)

    threading.Thread(target=collector_loop, daemon=True).start()
    time.sleep(2)
    print("小米路由器 中继模式 监控+配置中心")
    print("  路由器: " + USER + "@" + HOST + ":" + str(SSHPORT) + "  (Ctrl+C 停止)")
    monitor_web.serve({"host": HOST, "webport": WEBPORT, "lan": a.lan, "token": a.token,
                       "api": api_snapshot, "get_config": get_config, "do_action": do_action,
                       "net_test": run_net_test, "dns_queries": get_dns_queries,
                       "dns_stats": get_dns_stats, "read_backup": read_backup})


if __name__ == "__main__":
    main()
