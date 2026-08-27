# -*- coding: utf-8 -*-
"""面板 HTTP 服务层：静态页 + JSON API + 认证。不含业务逻辑（见 router_monitor_ap.py）。"""
import os
import json
import hmac
import base64
import urllib.parse
import socket
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

_DIR = os.path.dirname(os.path.abspath(__file__))


def load_page():
    with open(os.path.join(_DIR, "monitor_page.html"), encoding="utf-8") as f:
        return f.read().encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    ctx = None

    def _auth_ok(self):
        tok = self.ctx.get("token") or ""
        if not tok:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                raw = base64.b64decode(hdr[6:]).decode("utf-8", "replace")
                _, _, pw = raw.partition(":")
            except Exception:
                return False
            return hmac.compare_digest(pw, tok)
        return False

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="router-panel"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code, data, ctype, disp=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if disp:
            self.send_header("Content-Disposition", disp)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._auth_ok():
            return self._deny()
        p = self.path.split("?")[0]
        c = self.ctx
        if p == "/":
            self._send_bytes(200, c["page"], "text/html; charset=utf-8")
        elif p == "/api":
            self._send(200, c["api"]())
        elif p == "/api/dnsquery":
            self._send(200, {"queries": c["dns_queries"]()})
        elif p == "/api/dnshitrate":
            stats = c["dns_stats"]()
            if stats:
                self._send(200, stats)
            else:
                self._send(200, {"ok": False, "error": "未读到 dnsmasq 统计"})
        elif p.startswith("/download/"):
            name = urllib.parse.unquote(p[len("/download/"):])
            blob = c["read_backup"](name)
            if blob is None:
                self._send(404, {"ok": False, "error": "备份文件不存在"})
            else:
                safe = os.path.basename(name)
                self._send_bytes(200, blob, "application/gzip",
                                 'attachment; filename="%s"' % safe)
        elif p == "/api/config":
            try:
                self._send(200, c["get_config"]())
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})
        else:
            self._send(404, {"ok": False})

    def do_POST(self):
        if not self._auth_ok():
            return self._deny()
        p = self.path.split("?")[0]
        c = self.ctx
        if p == "/api/nettest":
            try:
                self._send(200, {"ok": True, "msg": c["net_test"]()})
            except Exception as e:
                self._send(200, {"ok": False, "msg": str(e)})
        elif p == "/api/act":
            try:
                ln = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(ln).decode("utf-8", "replace") or "{}")
                msg = c["do_action"](data.get("action", ""), data.get("params") or {})
                self._send(200, {"ok": True, "msg": msg})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
        else:
            self._send(404, {"ok": False})

    def log_message(self, *a):
        pass


def serve(ctx):
    ctx = dict(ctx)
    ctx["page"] = load_page()
    Handler.ctx = ctx
    port = ctx["webport"]
    if ctx.get("lan"):
        class Srv(ThreadingHTTPServer):
            address_family = socket.AF_INET6
            daemon_threads = True
            def server_bind(self):
                try:
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except OSError:
                    pass
                super().server_bind()
        srv = Srv(("::", port), Handler)
        where = "0.0.0.0(双栈)/%d" % port
    else:
        class Srv4(ThreadingHTTPServer):
            daemon_threads = True
        srv = Srv4(("127.0.0.1", port), Handler)
        where = "127.0.0.1:%d (仅本机)" % port
    print("  面板地址: http://127.0.0.1:%d  监听: %s%s" % (port, where, "  · 已启用令牌认证" if ctx.get("token") else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
