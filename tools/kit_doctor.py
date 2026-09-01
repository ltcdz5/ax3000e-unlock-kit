# -*- coding: utf-8 -*-
"""一键体检（kit_doctor）：自动发现路由器 IP + 状态体检 + 能自动修的就修。

用法:
  python kit_doctor.py            只体检，列出状态与手动待办
  python kit_doctor.py --fix      体检 + 自动修复（启动器 IP 回写、关闭固件自动升级）

检测原理（零凭据）：小米管理页 /cgi-bin/luci/web/home 无需登录即可取到 hardware 标识，
用作局域网指纹扫描；SSH/路由器内部项用面板同款 paramiko 连接（密码默认 admin，可 --passwd 覆盖）。
"""
import sys, os, re, io, socket, argparse, subprocess, json, urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")

# 套件版本（与 monitor_web.py 同步）
KIT_VERSION = "2.4.0"


def _ver_tuple(v):
    parts = (v or "").strip().split(".")
    try:
        return tuple(int(p) for p in parts) if parts else ()
    except ValueError:
        return ()


# 检查 GitHub 最新版本
def check_latest_version():
    try:
        req = urllib.request.urlopen(
            "https://api.github.com/repos/ltcdz5/xiaomi-router-unlock-kit/releases/latest",
            timeout=5)
        data = json.loads(req.read())
        latest = data.get("tag_name", "").lstrip("v")
        if latest and _ver_tuple(latest) > _ver_tuple(KIT_VERSION):
            return latest
    except Exception:
        pass
    return None

P = argparse.ArgumentParser()
P.add_argument("--fix", action="store_true", help="自动修复可修复项")
P.add_argument("--ip", help="跳过扫描，直接指定路由器 IP")
P.add_argument("--passwd", default=os.environ.get("ROUTER_PASSWD", "admin"))
A = P.parse_args()

FIXED, MANUAL = [], []


def say(icon, title, detail=""):
    print("%s %s%s" % (icon, title, (" —— " + detail) if detail else ""))


def local_subnet():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip.rsplit(".", 1)[0] + "."
    except OSError:
        return "192.168.2."


def fingerprint(ip):
    """返回 hardware 标识（如 RN07），非小米设备返回 None。零凭据。"""
    try:
        with urllib.request.urlopen("http://%s/cgi-bin/luci/web/home" % ip, timeout=1.5) as r:
            page = r.read().decode("utf-8", "replace")
        m = re.search(r"hardware = '(.*?)'", page) or re.search(r"hardwareVersion: '(.*?)'", page)
        return m.group(1) if m else None
    except Exception:
        return None


def find_router():
    if A.ip:
        hw = fingerprint(A.ip)
        return (A.ip, hw) if hw else (A.ip, "?")
    cands = ["192.168.31.1", "192.168.2.106", "192.168.2.105", "192.168.2.100"]
    base = local_subnet()
    say("ℹ️", "扫描局域网 %s0/24（小米管理页指纹）…" % base)
    found = []
    with ThreadPoolExecutor(64) as ex:
        for ip, hw in zip(cands + [base + str(i) for i in range(1, 255)],
                          ex.map(fingerprint, cands + [base + str(i) for i in range(1, 255)])):
            if hw:
                found.append((ip, hw))
    return found[0] if found else (None, None)


def ssh_exec(ip, cmd, passwd):
    import paramiko
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username="root", password=passwd, timeout=6,
              allow_agent=False, look_for_keys=False,
              disabled_algorithms={"keys": ["rsa-sha2-256", "rsa-sha2-512"]})
    _, o, _ = c.exec_command(cmd, timeout=10)
    out = o.read().decode("utf-8", "replace").strip()
    c.close()
    return out


def port_open(ip, port, t=1.5):
    try:
        socket.create_connection((ip, port), timeout=t).close()
        return True
    except OSError:
        return False


def parse_dns_servers(ps_output):
    """解析 Get-DnsClientServerAddress 输出 → IPv4 DNS 列表（去重保序；兼容表格/单列格式）。"""
    out = []
    for line in ps_output.splitlines():
        parts = line.split()
        if parts and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[-1]):
            if parts[-1] not in out:
                out.append(parts[-1])
    return out


def main():
    print("=" * 62)
    print("小米路由器解锁套件 · 一键体检")
    print("=" * 62)
    print("套件版本: v%s" % KIT_VERSION)
    latest = check_latest_version()
    if latest:
        print("⚠️  新版本 v%s 可用，请前往 GitHub 下载更新" % latest)
    print()

    ip, hw = find_router()
    if not ip:
        say("❌", "局域网内未发现小米路由器管理页", "确认设备通电联网、与管理页同网段；或 --ip 指定")
        return 1
    say("✅", "发现路由器 %s（hardware=%s）" % (ip, hw))

    # 固件版本（零凭据）
    try:
        info = json.loads(urllib.request.urlopen(
            "http://%s/cgi-bin/luci/api/xqsystem/init_info" % ip, timeout=3).read())
        rom = info.get("romversion", "?")
    except Exception:
        rom = "?"
    if rom == "1.0.24":
        say("✅", "固件 1.0.24（实测基准版本）")
    else:
        say("⚠️", "固件 %s（非实测基准 1.0.24）" % rom, "≤1.0.24 理论可用需校准；更高版本解锁可能失效")

    # 固件升级状态（零凭据）
    try:
        up = json.loads(urllib.request.urlopen(
            "http://%s/cgi-bin/luci/api/xqsystem/upgrade_status" % ip, timeout=3).read())
        if up.get("status") == 0:
            say("✅", "固件升级状态：无新版本（当前 1.0.24 为最新）")
        else:
            say("⚠️", "固件升级状态异常（status=%s）" % up.get("status"),
                "可能有新固件推送——升级固件=解锁全部报废，切勿升级；体检 --fix 已关闭自动升级")
            MANUAL.append("确认勿手动升级固件")
    except Exception:
        pass

    # SSH
    ssh_ok = port_open(ip, 22)
    if ssh_ok:
        say("✅", "SSH(22) 在线")
    else:
        say("❌", "SSH(22) 不通", "运行解锁向导: python tools/unlock_wizard.py %s" % ip)

    # 路由器内部项
    if ssh_ok:
        try:
            heal = ssh_exec(ip, "test -f /data/auto_ssh/auto_ssh.sh && grep -c auto_ssh /etc/crontabs/root", A.passwd)
            if heal and int(heal.splitlines()[-1] or 0) >= 1:
                say("✅", "三层自愈已安装（auto_ssh.sh + cron）")
            else:
                say("⚠️", "自愈未装全", "SSH 通了但缺自愈：运行 deploy/一键部署.py %s" % ip)
            ad = ssh_exec(ip, "test -f /tmp/dnsmasq.d/96-antiad.conf && echo y", A.passwd)
            say("✅" if ad == "y" else "⚠️", "DNS 去广告列表" + ("已加载" if ad == "y" else "未加载（一键部署可装）"))
            df = ssh_exec(ip, "df /data | tail -n 1", A.passwd).split()
            free = df[-3] if len(df) >= 4 else "?"
            say("✅" if free.isdigit() and int(free) >= 200 else "⚠️",
                "/data 剩余 %sK" % free, "" if free.isdigit() and int(free) >= 200 else "偏紧，勿再加大文件")
            svc = ssh_exec(ip, "ps w | grep -E 'messagingagent|mosquitto|xq_info_sync_mqtt' | grep -v grep | wc -l", A.passwd)
            n = svc.strip() or "0"
            say("✅" if n == "0" else "ℹ️", "米家云服务" + ("已精简（停止）" if n == "0" else "仍在运行，面板可临时停止"))
            pw_admin = True
            try:
                ssh_exec(ip, "true", "admin")
            except Exception:
                pw_admin = False
            if pw_admin:
                say("⚠️", "SSH 仍是弱密码 admin", "手动：ssh 登录后 passwd 修改，并同步启动器 ROUTER_PASSWD")
                MANUAL.append("改 SSH 弱密码 admin 并同步启动器")
            # 模式分支（与 deploy/oneclick_deploy.py 同款判断）
            gw = ssh_exec(ip, "ip route show default 2>/dev/null | grep -o 'via [0-9.]*' | head -1", A.passwd)
            if gw:
                say("ℹ️", "当前模式：AP/中继（上级网关 %s）" % gw.replace("via ", ""),
                    "端口转发/QoS/DHCP 由上级路由管理；切主路由模式后重跑体检可查下列功能")
            else:
                say("✅", "当前模式：主路由")
                wan = ssh_exec(ip, "ip route show default | wc -l", A.passwd)
                say("✅" if wan.strip() != "0" else "❌", "WAN 链路" + ("已连通" if wan.strip() != "0" else "无默认路由，检查拨号/上级光猫"))
                upnp = ssh_exec(ip, "ps w | grep miniupnpd | grep -v grep | wc -l", A.passwd)
                say("✅" if upnp.strip() != "0" else "ℹ️", "UPnP " + ("运行中" if upnp.strip() != "0" else "未运行（面板可开关）"))
                qos = ssh_exec(ip, "uci get miqos.settings.enabled 2>/dev/null", A.passwd)
                say("✅" if qos == "1" else "ℹ️", "QoS " + ("已启用" if qos == "1" else "未启用（面板可配置）"))
                binds = ssh_exec(ip, "uci show dhcp 2>/dev/null | grep -c 'dhcp.@host'", A.passwd)
                say("✅", "DHCP 静态绑定 %s 条" % (binds or 0), "面板可增删" if (binds or "0") != "0" else "")
                fwds = ssh_exec(ip, "uci show firewall 2>/dev/null | grep -c '@redirect'", A.passwd)
                say("✅", "端口转发规则 %s 条" % (fwds or 0), "面板可增删" if (fwds or "0") != "0" else "")
            # 去广告 DNS 链路：上级路由下发的 DNS 必须指向本机，否则静默失效
            if gw and not port_open(gw.replace("via ", ""), 53, t=2):
                say("✅", "去广告 DNS 链路", "上级网关未提供 DNS，客户端只能走本机")
            elif gw:
                servers = []
                if os.name == "nt":
                    try:
                        ps = subprocess.run(
                            ["powershell", "-NoProfile", "-Command",
                             "(Get-DnsClientServerAddress -AddressFamily IPv4 | "
                             "Select-Object -ExpandProperty ServerAddresses) -join \"`n\""],
                            capture_output=True, timeout=15)
                        servers = parse_dns_servers(ps.stdout.decode("utf-8", "replace"))
                    except Exception:
                        servers = []
                if ip in servers:
                    say("✅", "去广告 DNS 链路生效（本机 DNS 含 %s）" % ip)
                elif servers:
                    say("ℹ️", "去广告链路检测：本机 DNS 是 %s，不含 %s" % ("、".join(servers), ip),
                        "本机可能有特殊 DNS 配置，其他设备不受影响")
                else:
                    say("ℹ️", "手动确认：上级路由 DHCP 的 DNS 已指向 %s" % ip, "指错则去广告静默失效")
            else:
                say("✅", "去广告 DNS 链路", "主路由模式，客户端直连本机")
        except Exception as e:
            say("⚠️", "SSH 登录失败（密码不是 %s？）" % A.passwd, "用 --passwd 指定；手动：改密码后同步启动器")
            MANUAL.append("确认 SSH 密码并同步启动器 ROUTER_PASSWD")

    # 本机项
    bat = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop", "路由器面板.bat")
    if os.path.exists(bat):
        cur = re.search(r"set ROUTER_HOST=(\S+)", open(bat, encoding="gbk", errors="replace").read())
        cur_ip = cur.group(1) if cur else "?"
        if cur_ip == ip:
            say("✅", "桌面启动器 IP 一致（%s）" % ip)
        elif A.fix:
            src = open(bat, encoding="gbk", errors="replace").read()
            open(bat, "w", encoding="gbk").write(re.sub(r"set ROUTER_HOST=\S+", "set ROUTER_HOST=%s" % ip, src))
            say("🔧", "已自动修复：启动器 ROUTER_HOST %s → %s" % (cur_ip, ip))
            FIXED.append("启动器 IP")
        else:
            say("⚠️", "启动器 IP 过期（%s ≠ %s）" % (cur_ip, ip), "加 --fix 自动改写")
            MANUAL.append("更新启动器 IP（或重跑 --fix）")
        if port_open("127.0.0.1", 8787, t=0.5):
            say("✅", "面板已在运行（8787）")
        else:
            say("ℹ️", "面板未运行", "双击桌面 路由器面板.bat 启动")
    else:
        say("ℹ️", "未找到桌面启动器", "直接用 panel/Start-*.bat 并设 ROUTER_HOST=%s" % ip)

    # 固件自动升级开关（可自动检测/关闭）
    if ssh_ok:
        try:
            auto = ssh_exec(ip, "uci get otapred.settings.auto 2>/dev/null", A.passwd).strip()
            if auto == "0":
                say("✅", "固件自动升级已关闭")
            elif auto == "1":
                if A.fix:
                    ssh_exec(ip, "uci set otapred.settings.auto=0; uci commit otapred", A.passwd)
                    verify = ssh_exec(ip, "uci get otapred.settings.auto 2>/dev/null", A.passwd).strip()
                    if verify == "0":
                        say("🔧", "已自动关闭固件自动升级（otapred.settings.auto=0）", "升级固件=解锁报废")
                        FIXED.append("关闭自动升级")
                    else:
                        say("⚠️", "自动关闭固件自动升级未生效", "手动：管理页关闭『自动升级』")
                        MANUAL.append("管理页关闭自动升级")
                else:
                    say("⚠️", "固件自动升级仍开启（otapred.settings.auto=1）", "升级固件=解锁报废；加 --fix 自动关闭")
                    MANUAL.append("关闭固件自动升级（--fix 或管理页）")
            else:
                say("⚠️", "手动确认：管理页已关闭『自动升级』", "本机读不到 otapred 开关；升级固件=解锁报废")
                MANUAL.append("管理页关闭自动升级")
        except Exception:
            say("⚠️", "手动确认：管理页已关闭『自动升级』", "升级固件=解锁报废")
            MANUAL.append("管理页关闭自动升级")
    say("ℹ️", "手动确认：上级路由按 MAC 绑了静态 IP", "防 IP 漂移（绑定后本体检的扫描也不再需要）")
    MANUAL.append("上级路由绑静态 IP")

    print("-" * 62)
    if FIXED:
        print("已自动修复：%s" % "、".join(FIXED))
    if MANUAL:
        print("需手动完成 %d 项：" % len(MANUAL))
        for i, m in enumerate(MANUAL, 1):
            print("  %d. %s" % (i, m))
    if not MANUAL and not FIXED:
        print("状态良好，无待办。")
    if sys.stdin.isatty() and not os.environ.get("KIT_DOCTOR_NOPAUSE"):
        try:
            input("\n按回车键退出…")
        except EOFError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
