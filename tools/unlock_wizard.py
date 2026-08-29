# -*- coding: utf-8 -*-
"""解锁向导（新手入口）：自动登录管理页取 stok → start_binding 注入 → 验证 22 口。

两种前端：有 tkinter 时弹小窗口（Windows 双击即用），否则走命令行交互。
核心仅用标准库；注入链与 README「快速开始」同源，幂等可重跑。
用法: python unlock_wizard.py [路由器IP]
"""
import sys, os, re, json, time, random, socket, hashlib, subprocess, urllib.request, urllib.parse

sys.stdout.reconfigure(encoding="utf-8")

IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.31.1"

_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")


def valid_host(h):
    return bool(_HOST_RE.match(h or ""))

# 与 README 快速开始一致的 5 步注入（%0A=换行，%20/%3D 为空格/=）
UNLOCK_STEPS = [
    ("启用 SSH",  "uid=1234&key=1234'%0Anvram%20set%20ssh_en%3D1"),
    ("启用 telnet（备用通道）", "uid=1234&key=1234'%0Anvram%20set%20telnet_en%3D1"),
    ("固化 nvram", "uid=1234&key=1234'%0Anvram%20commit"),
    ("dropbear 切 debug 通道", "uid=1234&key=1234'%0Ased%20-i%20's%2Fchannel%3D.*%2Fchannel%3D%22debug%22%2Fg'%20%2Fetc%2Finit.d%2Fdropbear"),
    ("启动 SSH 服务", "uid=1234&key=1234'%0A%2Fetc%2Finit.d%2Fdropbear%20start"),
]


def http_get(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_post(url, body, timeout=8):
    req = urllib.request.Request(url, data=body.encode("utf-8"),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_login_materials(ip):
    """从管理页取 nonce_key 与 deviceId(MAC)，并探测加密模式。"""
    page = http_get("http://%s/cgi-bin/luci/web/home" % ip)
    m_key = re.search(r"key: '(.*)',", page)
    m_mac = re.search(r"var deviceId = '(.*?)'", page)
    if not m_key or not m_mac:
        raise RuntimeError("管理页结构不符（需原厂固件且已完成初始配置）")
    encryptmode = None
    try:
        info = json.loads(http_get("http://%s/cgi-bin/luci/api/xqsystem/init_info" % ip))
        if "newEncryptMode" in info:
            encryptmode = int(info["newEncryptMode"])
    except Exception:
        pass
    return m_key.group(1), m_mac.group(1), encryptmode


def web_login(ip, web_pass, log=print):
    """MiWiFi Web 登录（算法参照 xmir-patcher/gateway.py），返回 stok。"""
    nonce_key, mac, encryptmode = fetch_login_materials(ip)
    modes = [encryptmode] if encryptmode in (0, 1) else [1, 0]
    nonce = "0_%s_%d_%d" % (mac, int(time.time()), random.randint(1000, 10000))
    last_err = ""
    for mode in modes:
        def xqhash(s):
            if isinstance(s, str):
                s = s.encode("utf-8")
            return (hashlib.sha256 if mode else hashlib.sha1)(s).hexdigest()
        pwd = xqhash(nonce + xqhash(web_pass + nonce_key))
        body = "username=admin&password=%s&logtype=2&nonce=%s" % (pwd, nonce)
        try:
            text = http_post("http://%s/cgi-bin/luci/api/xqsystem/login" % ip, body)
        except Exception as e:
            last_err = str(e)
            continue
        m = re.findall(r'"token":"(.*?)"', text)
        if m:
            log("管理页登录成功（加密模式 sha%s）" % (256 if mode else 1))
            return m[0]
        last_err = text[:120]
    raise RuntimeError("管理页登录失败（密码错？结构变？）：%s" % last_err)


def inject(stok, ip, log=print):
    base = "http://%s/cgi-bin/luci/;stok=%s/api/xqsystem/start_binding" % (ip, stok)
    codes = []
    for name, body in UNLOCK_STEPS:
        try:
            resp = http_post(base, body)
            code = json.loads(resp).get("code", "?")
        except Exception as e:
            raise RuntimeError("注入「%s」失败：%s" % (name, e))
        log("  · %s → code=%s" % (name, code))
        codes.append(code)
        time.sleep(0.5)
    return codes


def port22_ok(ip, wait=10):
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            socket.create_connection((ip, 22), timeout=2).close()
            return True
        except OSError:
            time.sleep(1)
    return False


def run_unlock(ip, web_pass=None, stok=None, log=print):
    log("目标路由器: %s" % ip)
    if not stok:
        log("登录管理页取会话令牌…")
        stok = web_login(ip, web_pass, log=log)
    log("执行 start_binding 注入链…")
    codes = inject(stok, ip, log=log)
    rejected = all(c == 1541 for c in codes)
    if rejected:
        log("ℹ️ 注入被拒（code=1541）。实测（2026-08-30）：已解锁设备会拒绝重复注入，属正常；锁定设备首解才会真正执行。")
    log("验证 22 端口…")
    if port22_ok(ip):
        log("✅ SSH 在线！%s" % ("设备本就解锁，可直接部署。" if rejected else "解锁成功。"))
        log("接下来可运行一键部署: deploy/一键部署.bat %s" % ip)
        return True
    if rejected:
        log("⚠️ 注入被拒且 22 口不通：该设备当前状态不吃此注入。可参考 xmir-patcher 的降级思路，或看 docs/自救手册.md。")
    else:
        log("⚠️ 22 口暂未通。等 10-30 秒重试；仍不通请看 docs/自救手册.md")
    return False


# ---------- 前端：tkinter 小窗口 ----------
def gui():
    import tkinter as tk
    from tkinter import ttk, scrolledtext
    import threading

    root = tk.Tk()
    root.title("小米路由器解锁向导（xiaomi-router-unlock-kit）")
    root.geometry("640x460")

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="x")
    ttk.Label(frm, text="路由器 IP").grid(row=0, column=0, sticky="w")
    e_ip = ttk.Entry(frm, width=16)
    e_ip.insert(0, IP)
    e_ip.grid(row=0, column=1, sticky="w", padx=4)
    ttk.Label(frm, text="管理页密码").grid(row=0, column=2, sticky="w", padx=(12, 0))
    e_pw = ttk.Entry(frm, width=16, show="*")
    e_pw.grid(row=0, column=3, sticky="w", padx=4)
    ttk.Label(frm, text="（或粘贴 stok 手动模式）").grid(row=0, column=4, padx=(12, 0))
    e_stok = ttk.Entry(frm, width=24)
    e_stok.grid(row=0, column=5, sticky="w", padx=4)

    log_box = scrolledtext.ScrolledText(root, height=20, font=("Consolas", 9))
    log_box.pack(fill="both", expand=True, padx=10, pady=6)

    def log(msg):
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.update_idletasks()

    def on_unlock():
        threading.Thread(target=work, daemon=True).start()

    def work():
        ip = e_ip.get().strip()
        if not valid_host(ip):
            log("IP/主机名格式不合法")
            return
        stok = e_stok.get().strip()
        pw = e_pw.get().strip()
        if not stok and not pw:
            log("请填写管理页密码，或粘贴 stok 走手动模式")
            return
        try:
            ok = run_unlock(ip, web_pass=pw or None, stok=stok or None, log=log)
            if ok:
                b_deploy.configure(state="normal")
        except Exception as e:
            log("❌ %s" % e)

    def on_deploy():
        ip = e_ip.get().strip()
        if not valid_host(ip):
            log("IP/主机名格式不合法")
            return
        d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "deploy", "一键部署.py")
        log("启动一键部署…（在新窗口完成 DNS/去广告/自愈安装）")
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        subprocess.Popen([sys.executable, d, ip], **kw)

    bar = ttk.Frame(root, padding=10)
    bar.pack(fill="x")
    ttk.Button(bar, text="一键解锁", command=on_unlock).pack(side="left")
    b_deploy = ttk.Button(bar, text="解锁成功后：运行一键部署", command=on_deploy, state="disabled")
    b_deploy.pack(side="left", padx=8)
    ttk.Label(bar, text="解锁=开 SSH，不丢配置；完成后第一件事：管理页关闭自动升级",
              foreground="#888").pack(side="left")
    root.mainloop()


# ---------- 前端：命令行 ----------
def cli():
    print("== 小米路由器解锁向导 ==")
    ip = input("路由器 IP [%s]: " % IP).strip() or IP
    stok = input("已有 stok？粘贴（没有则留空，用管理页密码自动登录）: ").strip()
    pw = None
    if not stok:
        pw = input("管理页密码 [admin]: ").strip() or "admin"
    try:
        run_unlock(ip, web_pass=pw, stok=stok or None)
    except Exception as e:
        print("❌ %s" % e)
    if sys.stdin.isatty():
        try:
            input("\n按回车键退出…")
        except EOFError:
            pass


if __name__ == "__main__":
    try:
        import tkinter  # noqa: F401
        gui()
    except ImportError:
        cli()
