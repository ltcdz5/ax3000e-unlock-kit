# -*- coding: utf-8 -*-
"""
小米 AX3000E 一键部署：SSH 解锁引导 + 模式检测 + 全配置应用 + 面板启动
用法: python 一键部署.py [AX3000E-IP]
AP 模式: AX3000E 接上级路由（如 192.168.2.1），IP 为上级网段分配
主路由模式: AX3000E 拨号，IP 为 192.168.31.1
"""
import sys, os, time, subprocess, base64
sys.stdout.reconfigure(encoding='utf-8')

IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.31.1"
SSHPORT, USER, PASSWD = 22, "root", os.environ.get("ROUTER_PASSWD", "<改成你的路由器SSH密码>")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    import paramiko
except ImportError:
    print("[!] 缺少 paramiko，先装: pip install paramiko")
    sys.exit(1)

def ssh_connect(ip=IP, tries=2):
    for _ in range(tries):
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(ip, port=SSHPORT, username=USER, password=PASSWD, timeout=8,
                      allow_agent=False, look_for_keys=False,
                      disabled_algorithms={'keys': ['rsa-sha2-256', 'rsa-sha2-512']})
            return c
        except Exception:
            time.sleep(3)
    return None

def q(ssh, cmd, t=15):
    try:
        _, o, _ = ssh.exec_command(cmd, timeout=t)
        return o.read().decode('utf-8', 'replace').strip()
    except Exception:
        return ""

def step(n, title):
    print("\n" + "=" * 50)
    print("[%d/%d] %s" % (n, 6, title))
    print("=" * 50)

# ============ 1. SSH 检测/解锁引导 ============
step(1, "SSH 连接检测: %s" % IP)
print("尝试 root 用户连接（密码来自环境变量 ROUTER_PASSWD）...")
ssh = ssh_connect()
if not ssh:
    print("""
[!] SSH 无法连接。AX3000E 需要先解锁 SSH，两种方式：
  方式A（推荐）: 用本仓库 README「快速开始 1」的 start_binding 注入链解锁（本机实测有效，见 docs/自救手册.md）。
  方式B: 若已用其他工具解锁（开启 telnet），把本脚本 IP 改成实际地址重跑。
  解锁完成后【重新运行本脚本】即可自动继续。
""")
    sys.exit(1)
print("SSH OK:", q(ssh, "uname -a | head -1"))

# ============ 2. 模式检测（AP / 主路由） ============
step(2, "网络模式检测")
gw = q(ssh, "ip route show default 2>/dev/null | grep -o 'via [0-9.]*'")
if gw:
    mode = "ap"
    print("模式: AP（中继/自适应） - 默认网关 %s，由上级路由分配 IP" % gw)
else:
    mode = "router"
    print("模式: 主路由 - 无上级网关，AX3000E 拨号/独立路由")

# ============ 3. 基础配置（DNS 上游 / 定向 / noipv6） ============
step(3, "DNS 优化配置")
up = ["180.184.1.1", "180.184.2.2", "223.5.5.5", "119.29.29.29", "114.114.114.114", "8.8.8.8", "9.9.9.9", "4.2.2.2"]
q(ssh, "rm -f /tmp/dnsmasq.d/98-upstream.conf")
for u in up:
    q(ssh, "echo 'server=" + u + "' >> /tmp/dnsmasq.d/98-upstream.conf")
q(ssh, "cp /tmp/dnsmasq.d/98-upstream.conf /data/upstreams.conf")
print("DNS 8 上游已配置（字节x2 + 国内x3 + 海外x3）")

btd = ["douyin.com", "douyinstatic.com", "snssdk.com", "byteimg.com", "bytedance.com",
       "toutiao.com", "ixigua.com", "douyinpic.com", "amemv.com", "bytecdn.cn", "pstatp.com", "bytefcdn.com"]
q(ssh, "rm -f /tmp/dnsmasq.d/95-bytedance.conf")
for d in btd:
    q(ssh, "echo 'server=/" + d + "/180.184.1.1' >> /tmp/dnsmasq.d/95-bytedance.conf")
q(ssh, "cp /tmp/dnsmasq.d/95-bytedance.conf /data/bytedance.conf")
print("抖音系 %d 域名定向字节 DNS" % len(btd))

noip6 = ["pstatp.com", "douyinpic.com", "byteimg.com", "douyinstatic.com", "snssdk.com",
         "bytefcdn.com", "douyin.com", "bytedance.com", "amemv.com", "bytecdn.cn", "ixigua.com", "toutiao.com"]
q(ssh, "rm -f /tmp/dnsmasq.d/92-noipv6.conf")
for d in noip6:
    q(ssh, "echo 'address=/" + d + "/::' >> /tmp/dnsmasq.d/92-noipv6.conf")
q(ssh, "cp /tmp/dnsmasq.d/92-noipv6.conf /data/noipv6.conf")
print("抖音系 %d 域名禁 IPv6（消除黑洞回退）" % len(noip6))

q(ssh, "printf 'log-queries\\nlog-facility=/tmp/dnsquery.log\\n' > /tmp/dnsmasq.d/93-logqueries.conf")
q(ssh, "cp /tmp/dnsmasq.d/93-logqueries.conf /data/logqueries.conf")
print("DNS 查询日志已启用（/tmp/dnsquery.log，面板实时显示）")

# ============ 4. 去广告（anti-AD + yhosts） ============
step(4, "去广告配置（anti-AD 10万条）")
q(ssh, "curl -sL 'https://anti-ad.net/anti-ad-for-dnsmasq.conf' -o /tmp/antiad_raw --connect-timeout 15 --max-time 90")
q(ssh, "grep -vE 'byteimg|pstatp|douyinpic|douyin|bytecdn|bytedance' /tmp/antiad_raw > /tmp/dnsmasq.d/96-antiad.conf 2>/dev/null")
n = q(ssh, "wc -l < /tmp/dnsmasq.d/96-antiad.conf 2>/dev/null")
q(ssh, "gzip -c /tmp/dnsmasq.d/96-antiad.conf > /data/antiad.gz")
print("anti-AD: %s 条（抖音系已过滤）" % n)

# ============ 5. 自愈脚本 auto_ssh + 应用 ============
step(5, "自愈脚本部署（auto_ssh）")
AUTO_SSH = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "router", "auto_ssh.sh"))
if not os.path.isfile(AUTO_SSH):
    print("[!] 找不到 router/auto_ssh.sh（需与仓库一起分发），退出")
    sys.exit(1)
with open(AUTO_SSH, encoding="utf-8") as f:
    auto_ssh = f.read()
q(ssh, "mkdir -p /data/auto_ssh")
b64 = base64.b64encode(auto_ssh.encode()).decode()
q(ssh, "echo " + b64 + " | base64 -d > /data/auto_ssh/auto_ssh.sh; chmod +x /data/auto_ssh/auto_ssh.sh")
q(ssh, "sh /data/auto_ssh/auto_ssh.sh &")
print("auto_ssh 自愈脚本已部署并首次执行")

# ============ 6. 启动面板 + 验证 ============
step(6, "启动监控面板 + 验证")
q(ssh, "/etc/init.d/dnsmasq restart")
r1 = q(ssh, "nslookup www.baidu.com 127.0.0.1 2>/dev/null | tail -1")
r2 = q(ssh, "nslookup ad.doubleclick.net 127.0.0.1 2>/dev/null | tail -1")
print("解析:", r1[:60])
print("去广告:", r2[:60])
ssh.close()

panel = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "panel", "router_monitor_ax3000e.py"))
if os.path.isfile(panel):
    print("\n启动面板: python %s" % panel)
    try:
        subprocess.Popen([sys.executable, panel], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        time.sleep(3)
        print("面板地址: http://localhost:8787")
    except OSError as e:
        print("面板自动启动失败(%s)，请手动运行: python %s" % (e, panel))
else:
    print("\n[!] 未找到面板文件 %s，请手动启动" % panel)
print("\n[完成] AX3000E 部署成功！浏览器打开 http://localhost:8787 使用")
