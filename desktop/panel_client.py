# -*- coding: utf-8 -*-
"""小米路由器解锁套件 · 桌面客户端
启动面板 HTTP 服务 → 内嵌浏览器窗口 → 托盘图标后台运行。
依赖: pip install pywebview
"""
import sys, os, threading, json, socket, urllib.request, webbrowser, re, time
from concurrent.futures import ThreadPoolExecutor

# 添加上级目录到路径，使 panel 模块可导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

WEBPORT = 8787
PANEL_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "panel", "router_monitor_ap.py")


def find_router():
    """零凭据扫描局域网内的 AX3000E (RN07)"""
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


def start_panel():
    """在后台线程启动面板 HTTP 服务"""
    import panel.monitor_web as mw
    import panel.router_monitor_ap as ap
    # 设置环境变量
    os.environ["ROUTER_HOST"] = router_ip
    os.environ["ROUTER_PASSWD"] = router_pass
    ap.HOST = router_ip
    ap.PASSWD = router_pass
    # 启动采集线程
    t = threading.Thread(target=ap.collector_loop, daemon=True)
    t.start()
    time.sleep(2)
    # 启动 HTTP 服务（阻塞，在后台线程运行）
    t2 = threading.Thread(target=lambda: mw.serve({
        "webport": WEBPORT, "host": router_ip,
        "api": ap.api_snapshot, "get_config": ap.get_config,
        "do_action": ap.do_action, "net_test": ap.run_net_test,
        "dns_queries": ap.get_dns_queries, "dns_stats": ap.get_dns_stats,
        "read_backup": ap.read_backup, "health_check": ap.get_health,
        "ad_stats": ap.get_ad_stats,
    }), daemon=True)
    t2.start()
    return None


def gui_input(prompt, default=""):
    """GUI 输入框（窗口化 exe 没有 stdin 时的替代）"""
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        return simpledialog.askstring("路由器面板", prompt, initialvalue=default) or default
    except Exception:
        return default


def main():
    global router_ip, router_pass
    router_ip = os.environ.get("ROUTER_HOST", "")
    router_pass = os.environ.get("ROUTER_PASSWD", "")

    # 如果没设 IP 则自动扫描
    if not router_ip:
        print("正在扫描路由器...")
        ip, hw = find_router()
        if ip:
            router_ip = ip
            print("发现路由器 %s (%s)" % (ip, hw))
        else:
            router_ip = gui_input("未发现路由器，请输入路由器 IP", "192.168.2.106")

    if not router_pass:
        router_pass = gui_input("请输入 SSH 密码", "admin")

    # 启动面板 HTTP 服务（后台线程）
    start_panel()

    # 等待面板就绪
    for i in range(30):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api" % WEBPORT, timeout=1):
                break
        except Exception:
            time.sleep(1)
    else:
        print("面板启动超时，请手动打开浏览器访问 http://127.0.0.1:%d" % WEBPORT)
        return

    # 打开浏览器
    try:
        import webview
        webview.create_window("小米路由器面板", "http://127.0.0.1:%d" % WEBPORT, width=1200, height=800)
        webview.start()
    except ImportError:
        webbrowser.open("http://127.0.0.1:%d" % WEBPORT)
        input("面板已启动，按回车退出...")


if __name__ == "__main__":
    main()