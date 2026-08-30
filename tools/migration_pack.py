# -*- coding: utf-8 -*-
"""一键迁移包：把本机的解锁/自愈/去广告全套状态打包，迁移到新的小米路由器。

用法:
  python migration_pack.py export [路由器IP]        导出迁移包到本地
  python migration_pack.py import <迁移包.zip> [目标路由器IP]   导入到新设备

导出内容：自愈脚本、DNS/去广告配置与列表缓存（冷启动免联网）、设备画像与 uci 快照（仅参考）。
导入动作：容量预检 → 分块上传并逐件 md5 回读校验 → 安装自愈 → 触发一次恢复。
uci 系统配置不自动应用（不同机型结构不同，快照仅供参考）。
"""
import sys, os, re, io, json, time, base64, hashlib, zipfile, argparse

sys.stdout.reconfigure(encoding="utf-8")

DATA_FILES = [
    "/data/auto_ssh/auto_ssh.sh",
    "/data/upstreams.conf",
    "/data/custom.conf",
    "/data/microsoft.conf",
    "/data/noipv6.conf",
    "/data/noresolv.conf",
    "/data/logqueries.conf",
    "/data/antiad.gz",
    "/data/awavenue.gz",
]
UCI_FILES = ["network", "wireless", "dhcp", "firewall", "miqos", "upnpd", "port_map", "system", "otapred"]
FORBIDDEN_NAMES = ("dropbear_rsa_host_key", "dropbear_ecdsa_host_key",
                   "dropbear_ed25519_host_key", "dropbear_dss_host_key")
PACK_FORMAT = 1
KIT_VERSION = "2.0.0"


def log(msg):
    print(msg)


def valid_host(s):
    return bool(re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", s or ""))


def connect(ip, passwd):
    import paramiko
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username="root", password=passwd, timeout=10,
              allow_agent=False, look_for_keys=False,
              disabled_algorithms={"keys": ["rsa-sha2-256", "rsa-sha2-512"]})
    return c


def run(sh, cmd, timeout=30):
    _, o, _ = sh.exec_command(cmd, timeout=timeout)
    return o.read().decode("utf-8", "replace")


def fetch_b64(sh, path):
    out = run(sh, "base64 '%s' 2>/dev/null" % path, timeout=90).strip()
    if not out:
        return None
    return base64.b64decode("".join(out.split()))


def capacity_ok(df_line, needed_bytes):
    """df /data 尾行 + 需要的字节数 → 空间是否够（留 64KB 余量）。纯函数。"""
    parts = (df_line or "").split()
    if len(parts) < 4 or not parts[-3].isdigit():
        return False
    avail = int(parts[-3]) * 1024
    return avail >= needed_bytes + 65536


def safe_pack_name(remote_path):
    name = remote_path.rsplit("/", 1)[-1]
    if name in FORBIDDEN_NAMES or ".." in name or name.startswith("."):
        return None
    return name


def build_manifest(entries):
    """entries: {包内路径: bytes} → manifest 文本（md5 + 大小）。纯函数。"""
    lines = ["# 迁移包清单 format=%d" % PACK_FORMAT]
    for name in sorted(entries):
        b = entries[name]
        lines.append("%s  md5=%s  size=%d" % (name, hashlib.md5(b).hexdigest(), len(b)))
    return "\n".join(lines) + "\n"


def parse_manifest(text):
    """manifest 文本 → {包内路径: (md5, size)}。纯函数。"""
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^(\S+)  md5=([0-9a-f]{32})  size=(\d+)$", line.strip())
        if m:
            out[m.group(1)] = (m.group(2), int(m.group(3)))
    return out


def device_meta(sh):
    model = run(sh, "nvram get model 2>/dev/null").strip() or "UNKNOWN"
    fw = run(sh, "cat /etc/xiaoqiang/version 2>/dev/null || nvram get firmware_version 2>/dev/null").strip()
    ver = run(sh, "grep -m1 'VER=' /data/auto_ssh/auto_ssh.sh 2>/dev/null").strip()
    return {"model": model, "firmware": fw, "auto_ssh": ver,
            "export_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kit_version": KIT_VERSION, "pack_format": PACK_FORMAT}


def do_export(ip, passwd, outdir):
    log("连接 %s …" % ip)
    sh = connect(ip, passwd)
    try:
        meta = device_meta(sh)
        entries = {}
        for path in DATA_FILES:
            name = safe_pack_name(path)
            if not name or not run(sh, "test -f '%s' && echo y" % path).strip():
                log("  跳过（不存在或禁止导出）: %s" % path)
                continue
            b = fetch_b64(sh, path) or b""
            entries["data/" + name] = b
            log("  收集 %s（%d 字节）" % (path, len(b)))
        for f in UCI_FILES:
            b = fetch_b64(sh, "/etc/config/" + f)
            if b:
                entries["uci/" + f] = b
        cron = run(sh, "cat /etc/crontabs/root 2>/dev/null").encode()
        if cron:
            entries["crontab.txt"] = cron
        entries["meta.json"] = json.dumps(meta, ensure_ascii=False, indent=2).encode()
        entries["manifest.txt"] = build_manifest(entries).encode()
        entries["恢复说明.txt"] = (
            "迁移到新设备：python migration_pack.py import 本包.zip <新设备IP>\n"
            "uci/ 目录仅为参考快照，导入不会自动应用；新设备请先完成 SSH 解锁。\n").encode()
        stamp = time.strftime("%Y%m%d-%H%M")
        out = os.path.join(outdir, "router_migration_%s_%s.zip" % (meta["model"], stamp))
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for name in sorted(entries):
                z.writestr(name, entries[name])
        log("✅ 迁移包已生成: %s（%d 项，机型 %s）" % (out, len(entries), meta["model"]))
        return out
    finally:
        sh.close()


def push_file(sh, data, remote_path):
    """单通道流式上传 + md5 回读校验，成功返回 True。"""
    dst_dir = remote_path.rsplit("/", 1)[0]
    run(sh, "mkdir -p '%s'" % dst_dir)
    if not data:
        run(sh, ": > '%s'" % remote_path)
        got = run(sh, "md5sum '%s' 2>/dev/null" % remote_path).split()
        return bool(got) and got[0] == hashlib.md5(b"").hexdigest()
    tmp = "/tmp/mig_%d" % (time.time_ns() % 1000000)
    run(sh, "rm -f '%s'" % tmp)
    chan = sh.get_transport().open_session()
    chan.exec_command("cat > '%s'" % tmp)
    for i in range(0, len(data), 16384):
        chan.send(data[i:i + 16384])
        time.sleep(0.05)
    chan.shutdown_write()
    chan.recv_exit_status()
    chan.close()
    want = hashlib.md5(data).hexdigest()
    got = run(sh, "md5sum '%s' 2>/dev/null" % tmp).split()
    if not got or got[0] != want:
        run(sh, "rm -f '%s'" % tmp)
        return False
    run(sh, "cat '%s' > '%s' && rm -f '%s'" % (tmp, remote_path, tmp), timeout=60)
    verify = run(sh, "md5sum '%s' 2>/dev/null" % remote_path).split()
    return bool(verify) and verify[0] == want


def do_import(pack, ip, passwd, assume_yes):
    if not os.path.exists(pack):
        log("❌ 找不到迁移包: %s" % pack)
        return False
    z = zipfile.ZipFile(pack)
    names = [n for n in z.namelist() if not n.endswith("/")]
    if "manifest.txt" not in names or "meta.json" not in names:
        log("❌ 不是有效的迁移包（缺 manifest/meta）")
        return False
    meta = json.loads(z.read("meta.json"))
    manifest = parse_manifest(z.read("manifest.txt").decode())
    data_items = {n: manifest[n] for n in manifest if n.startswith("data/")}
    needed = sum(s for _, s in data_items.values())
    log("迁移包：%s 机型导出，%d 个数据文件，约 %dKB" %
        (meta.get("export_time", "?"), len(data_items), needed // 1024))
    log("连接目标 %s …" % ip)
    sh = connect(ip, passwd)
    try:
        t_model = run(sh, "nvram get model 2>/dev/null").strip() or "UNKNOWN"
        if t_model != meta.get("model"):
            log("⚠️ 机型不同：包来自 %s，目标是 %s（配置按通用格式迁移，功能键以实机为准）"
                % (meta.get("model"), t_model))
        df = run(sh, "df /data | tail -n 1")
        net = 0
        for name, (md5, size) in data_items.items():
            remote = "/data/auto_ssh/" + name[5:] if name == "data/auto_ssh.sh" else "/data/" + name[5:]
            cur = run(sh, "wc -c < '%s' 2>/dev/null || echo 0" % remote).strip()
            net += max(0, size - int(cur or 0))
        if not capacity_ok(df, net):
            log("❌ 目标 /data 空间不足（净增需 %dKB）：\n%s" % (net // 1024, df))
            return False
        if not assume_yes:
            if input("确认导入到 %s（%s）？输入 yes 继续: " % (ip, t_model)).strip() != "yes":
                log("已取消。")
                return False
        ok = fail = 0
        for name in sorted(data_items):
            md5, size = data_items[name]
            remote = "/data/auto_ssh/" + name[5:] if name == "data/auto_ssh.sh" else "/data/" + name[5:]
            if safe_pack_name(remote) is None:
                log("  跳过禁止项: %s" % name)
                continue
            pushed = False
            for attempt, backoff in enumerate((10, 30, 60)):
                try:
                    pushed = push_file(sh, z.read(name), remote)
                    break
                except Exception as e:
                    log("  ⚠️ 连接中断（%s），%d 秒后重连重试（限流恢复需耐心）…"
                        % (e.__class__.__name__, backoff))
                    try:
                        sh.close()
                    except Exception:
                        pass
                    time.sleep(backoff)
                    sh = connect(ip, passwd)
            if pushed:
                log("  ✅ %s（%d 字节，md5 校验通过）" % (remote, size))
                ok += 1
            else:
                log("  ❌ %s 上传校验失败" % remote)
                fail += 1
        if fail:
            log("❌ %d 项失败，中止自愈安装（请重跑）。" % fail)
            return False
        run(sh, "chmod +x /data/auto_ssh/auto_ssh.sh")
        inst = run(sh, "/bin/sh /data/auto_ssh/auto_ssh.sh install 2>&1 | tail -n 3", timeout=60)
        log("自愈安装输出: %s" % (inst.strip() or "（无）"))
        run(sh, "/bin/sh /data/auto_ssh/auto_ssh.sh 2>/dev/null", timeout=90)
        cron = run(sh, "grep -c auto_ssh /etc/crontabs/root 2>/dev/null").strip() or "0"
        log("✅ 导入完成：%d 项写入成功；自愈 cron 行数 %s；重启路由器后自愈会全量恢复一次。" % (ok, cron))
        return True
    finally:
        sh.close()


def main():
    p = argparse.ArgumentParser(description="路由器迁移包（导出/导入）")
    sub = p.add_subparsers(dest="cmd")
    pe = sub.add_parser("export", help="从当前路由器导出迁移包")
    pe.add_argument("ip", nargs="?", default="192.168.2.106")
    pe.add_argument("-o", "--outdir", default=os.path.dirname(os.path.abspath(__file__)))
    pi = sub.add_parser("import", help="把迁移包导入目标路由器")
    pi.add_argument("pack", help="迁移包 zip 路径")
    pi.add_argument("ip", nargs="?", default="192.168.31.1")
    pi.add_argument("--yes", action="store_true", help="跳过确认")
    A = p.parse_args()
    if not A.cmd:
        p.print_help()
        return 1
    if not valid_host(A.ip):
        log("❌ 非法 IP/主机名")
        return 1
    passwd = os.environ.get("ROUTER_PASSWD")
    if not passwd:
        import getpass
        passwd = getpass.getpass("路由器 SSH 密码（root）: ")
    if not passwd:
        log("❌ 未提供密码")
        return 1
    if A.cmd == "export":
        return 0 if do_export(A.ip, passwd, A.outdir) else 1
    return 0 if do_import(A.pack, A.ip, passwd, A.yes) else 1


if __name__ == "__main__":
    code = main()
    if sys.stdin.isatty():
        try:
            input("\n按回车键退出…")
        except EOFError:
            pass
    sys.exit(code)
