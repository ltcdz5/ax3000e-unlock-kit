# -*- coding: utf-8 -*-
"""小米路由器解锁套件 · 桌面客户端
启动面板 HTTP 服务 → 打开系统浏览器。
免 pywebview 依赖，PyInstaller 打包更稳定。
"""
import sys, os, threading, json, socket, urllib.request, webbrowser, re, time, traceback
from concurrent.futures import ThreadPoolExecutor

# 添加上级目录或 PyInstaller 打包目录到路径
if getattr(sys, 'frozen', False):
    _BASE = sys._MEIPASS
    sys.path.insert(0, _BASE)
else:
    _BASE = os.path.join(os.path.dirname(__file__), "..")
    sys.path.insert(0, _BASE)

WEBPORT = 8787
_LOG = os.path.join(os.path.expanduser("~"), "Desktop", "panel_error.log")


def _log(txt):
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), txt))
    except Exception:
        pass


def find_router():
    for ip in [getattr(find_router, "_ip", None)] if hasattr(find_router, "_ip") else []:
        pass
    cands = ["192.168.31.1", "192.168.2.106", "192.168.2.105", "192.168.2.100"]
    base = local_subnet()
    with ThreadPoolExecutor(64) as ex:
        for ip, hw in zip(cands + [base + str(i) for i in range(1, 255)],
                          ex.map(fingerprint, cands + [base + str(i) for i in range(1, 255)])):
            if hw:
                find_router._ip = ip
                return ip, hw
    return None, None


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
    try:
        with urllib.request.urlopen("http://%s/cgi-bin/luci/web/home" % ip, timeout=1.5) as r:
            page = r.read().decode("utf-8", "replace")
        m = re.search(r"hardware = '(.*?)'", page) or re.search(r"hardwareVersion: '(.*?)'", page)
        return m.group(1) if m else None
    except Exception:
        return None


def gui_input(prompt, default=""):
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        return simpledialog.askstring("路由器面板", prompt, initialvalue=default) or default
    except Exception:
        return default


def start_panel():
    """在后台线程启动面板 HTTP 服务（直接 import 模块，不走 panel.xxx 包路径）"""
    # 把 panel 目录加进 sys.path，让 router_monitor_ap.py 能 import monitor_web
    panel_dir = os.path.join(_BASE, "panel")
    sys.path.insert(0, panel_dir)
    import monitor_web as mw
    import router_monitor_ap as ap
    os.environ["ROUTER_HOST"] = router_ip
    os.environ["ROUTER_PASSWD"] = router_pass
    ap.HOST = router_ip
    ap.PASSWD = router_pass
    t = threading.Thread(target=ap.collector_loop, daemon=True)
    t.start()
    time.sleep(2)
    t2 = threading.Thread(target=lambda: mw.serve({
        "webport": WEBPORT, "host": router_ip,
        "api": ap.api_snapshot, "get_config": ap.get_config,
        "do_action": ap.do_action, "net_test": ap.run_net_test,
        "dns_queries": ap.get_dns_queries, "dns_stats": ap.get_dns_stats,
        "read_backup": ap.read_backup, "health_check": ap.get_health,
        "ad_stats": ap.get_ad_stats,
    }), daemon=True)
    t2.start()


def main():
    global router_ip, router_pass
    try:
        router_ip = os.environ.get("ROUTER_HOST", "")
        router_pass = os.environ.get("ROUTER_PASSWD", "")

        if not router_ip:
            _log("扫描路由器...")
            ip, hw = find_router()
            if ip:
                router_ip = ip
                _log("发现 %s (%s)" % (ip, hw))
            else:
                router_ip = gui_input("未发现路由器，请输入路由器 IP", "192.168.2.106")
                _log("手动输入 IP: %s" % router_ip)

        if not router_pass:
            router_pass = gui_input("请输入 SSH 密码", "admin")

        _log("启动面板服务...")
        start_panel()

        for i in range(30):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api" % WEBPORT, timeout=1):
                    break
            except Exception:
                time.sleep(1)
        else:
            _log("面板启动超时")
            webbrowser.open("http://127.0.0.1:%d" % WEBPORT)
            return

        _log("面板就绪，打开浏览器")
        webbrowser.open("http://127.0.0.1:%d" % WEBPORT)

        # 保持进程存活（浏览器关闭后面板仍需运行）
        while True:
            time.sleep(60)
    except Exception as e:
        _log("崩溃: %s" % traceback.format_exc())
        raise


if __name__ == "__main__":
    main()