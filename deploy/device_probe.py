# -*- coding: utf-8 -*-
"""
小米路由器实机校准探针（只读）：一次性输出设备画像，用于新设备适配核对
用法: python device_probe.py [路由器IP]
输出: 控制台 + device_profile_<IP>_<时间>.txt
对任何小米设备通用（AX3000E 可作基准对照）
"""
import sys, os, time, datetime
sys.stdout.reconfigure(encoding='utf-8')

IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.31.1"
SSHPORT, USER = 22, "root"
PASSWD = os.environ.get("ROUTER_PASSWD", "")

try:
    import paramiko
except ImportError:
    print("[!] 缺少 paramiko，先装: pip install \"paramiko<4\"")
    sys.exit(1)

# 每节: (标题, 命令)。命令全部只读；不存在的命令输出留空即可
SECTIONS = [
    ("设备标识 / 固件版本", "cat /etc/device_info 2>/dev/null; echo ---; echo \"model=$(nvram get model 2>/dev/null) hardware=$(nvram get hardware 2>/dev/null) product=$(nvram get product_name 2>/dev/null)\"; nvram get firmware_version 2>/dev/null; uci get xqcommon.sysinfo.firmware_version 2>/dev/null; echo '(若版本号为空，版本号只经管理页暴露，需人工记录)'; uname -a"),
    ("CPU / 内核", "cat /proc/cpuinfo | grep -E 'model name|Hardware|processor' | sort -u; echo ---; cat /proc/version"),
    ("内存", "free"),
    ("SSH: dropbear 版本", "dropbear -V 2>&1 | head -n 1; echo ---; grep -n 'channel=' /etc/init.d/dropbear 2>/dev/null | head -n 3"),
    ("dnsmasq 版本", "dnsmasq --version 2>/dev/null | head -n 2; echo ---; ls /tmp/dnsmasq.d/ 2>/dev/null"),
    ("存储: /data 卷容量与占用", "df -h /data /tmp /overlay 2>/dev/null; echo ---; du -sh /data/* 2>/dev/null"),
    ("WiFi: uci wireless 全量", "uci show wireless 2>/dev/null"),
    ("WiFi: 驱动与无线接口", "ls /sys/class/net 2>/dev/null; echo ---; iwinfo 2>/dev/null | head -n 40"),
    ("网络: 接口与网桥", "ifconfig -a 2>/dev/null | head -n 60"),
    ("自愈载体: crontab", "cat /etc/crontabs/root 2>/dev/null"),
    ("自愈载体: firewall 钩子", "cat /etc/firewall.include 2>/dev/null; echo ---; cat /etc/firewall.user 2>/dev/null"),
    ("uci 概览: network", "uci show network 2>/dev/null | head -n 30"),
    ("uci 概览: dhcp", "uci show dhcp 2>/dev/null | head -n 30"),
    ("开机项", "ls /etc/rc.d/ 2>/dev/null"),
]

def main():
    if not PASSWD:
        import getpass
        globals()['PASSWD'] = getpass.getpass("路由器 root SSH 密码（免输入可先 set ROUTER_PASSWD=...）: ")
        if not PASSWD:
            print("[!] 未提供密码，退出"); sys.exit(1)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(IP, port=SSHPORT, username=USER, password=PASSWD, timeout=10,
                  allow_agent=False, look_for_keys=False,
                  disabled_algorithms={'keys': ['rsa-sha2-256', 'rsa-sha2-512']})
    except Exception as e:
        print("[!] SSH 连接失败: %s（先确认已解锁且未被 dropbear 限流）" % e); sys.exit(1)

    def q(cmd, t=20):
        try:
            _, o, _ = c.exec_command(cmd, timeout=t)
            return o.read().decode('utf-8', 'replace').rstrip()
        except Exception as e:
            return "(执行失败: %s)" % e

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    out_path = os.path.join(os.getcwd(), "device_profile_%s_%s.txt" % (IP, ts))
    lines = ["设备画像 %s @ %s（只读探针，未改动路由器）" % (IP, ts), "=" * 60]
    for title, cmd in SECTIONS:
        lines += ["", "## " + title, "-" * 40, q(cmd) or "(空/命令不存在)"]
    c.close()

    report = "\n".join(lines) + "\n"
    sys.stdout.write(report)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print("[ok] 报告已保存: %s" % out_path)

if __name__ == "__main__":
    main()
