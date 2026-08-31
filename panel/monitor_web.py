# -*- coding: utf-8 -*-
"""面板 HTTP 服务层（双面板共享）：统一认证/校验/路由，业务逻辑经 ctx 注入。

ctx 契约：
  webport  监听端口
  lan      True=绑定全网卡（必须配 token）；False=仅 127.0.0.1
  token    HTTP Basic 令牌（密码，用户名任意）；空=免认证
  page     bytes 或 callable()->bytes          GET /
  api      callable() -> obj                   GET /api
  get_config / do_action                      GET /api/config · POST /api/act
  net_test / dns_queries / dns_stats / read_backup（可选，缺省路由返回 404）

纯函数 host_ok / origin_ok / auth_ok / escape_inline_json / parse_act_body
单独导出，供单元测试与两个面板复用。
"""
import os
import json
import hmac
import base64
import urllib.parse
import socket
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

_DIR = os.path.dirname(os.path.abspath(__file__))

# 套件版本（单一来源，页脚展示；发版时与 release 号同步更新）
KIT_VERSION = "2.3.0"

# GitHub API 远程版本缓存（避免每次请求都调用 API）
_REMOTE_VER = None
_REMOTE_VER_TS = 0


def get_remote_version():
    """获取 GitHub 最新 release 版本号（缓存 10 分钟）"""
    global _REMOTE_VER, _REMOTE_VER_TS
    import time
    now = time.time()
    if _REMOTE_VER is not None and now - _REMOTE_VER_TS < 600:
        return _REMOTE_VER
    try:
        import urllib.request, json
        req = urllib.request.urlopen(
            "https://api.github.com/repos/ltcdz5/xiaomi-router-unlock-kit/releases/latest",
            timeout=5)
        data = json.loads(req.read())
        _REMOTE_VER = data.get("tag_name", "").lstrip("v")
        _REMOTE_VER_TS = now
        return _REMOTE_VER
    except Exception:
        return None


def load_page():
    with open(os.path.join(_DIR, "monitor_page.html"), encoding="utf-8") as f:
        return f.read().encode("utf-8")


# ---------- 纯函数（可单测） ----------

def host_ok(lan, host_header):
    """防 DNS rebinding：本机模式下 Host 必须是回环地址（--lan 时不限制）。"""
    if lan:
        return True
    h = (host_header or "").split(":")[0].strip("[]").lower()
    return h in ("127.0.0.1", "localhost", "::1")


def origin_ok(lan, origin_header, host_header):
    """防 CSRF：浏览器跨站 POST 必带 Origin/Referer 且与 Host 同源；
    无 Origin/Referer 视为非浏览器直连（本地 CLI），仅本机模式放行。"""
    origin = (origin_header or "").strip()
    if not origin:
        return not lan
    try:
        net = urllib.parse.urlparse(origin).netloc.lower()
    except Exception:
        return False
    return net == (host_header or "").lower()


def auth_ok(token, authorization_header):
    """HTTP Basic：密码即令牌（用户名忽略），常时比较。token 为空=免认证。"""
    tok = token or ""
    if not tok:
        return True
    hdr = authorization_header or ""
    if not hdr.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(hdr[6:]).decode("utf-8", "replace")
        _, _, pw = raw.partition(":")
    except Exception:
        return False
    return hmac.compare_digest(pw, tok)


def escape_inline_json(s):
    """内联进 <script> 的 JSON 先逃逸：防 </script> 截断注入与 JS 行分隔符。"""
    for a, b in (("<", "\\u003c"), (">", "\\u003e"),
                 (chr(0x2028), "\\u2028"), (chr(0x2029), "\\u2029")):
        s = s.replace(a, b)
    return s


def parse_act_body(raw):
    """POST /api/act 请求体：JSON 或表单（json= 字段，其余字段并入 params）。
    返回 (action, params, is_json)——is_json 同时决定应答格式，解析与应答判定同源。"""
    raw = raw or ""
    if raw.lstrip().startswith("{"):
        data = json.loads(raw)
        return data.get("action", ""), dict(data.get("params") or {}), True
    qs = urllib.parse.parse_qs(raw)
    data = json.loads(qs.get("json", ["{}"])[0])
    params = dict(data.get("params") or {})
    for k, v in qs.items():
        if k != "json":
            params[k] = v[0]
    return data.get("action", ""), params, False


# ---------- Handler ----------

class Handler(BaseHTTPRequestHandler):
    ctx = None

    def _gate(self, post=False):
        c = self.ctx
        if not host_ok(c.get("lan"), self.headers.get("Host")):
            self._send(403, {"ok": False, "error": "illegal host"})
            return False
        if post and not origin_ok(c.get("lan"),
                                  self.headers.get("Origin") or self.headers.get("Referer"),
                                  self.headers.get("Host")):
            self._send(403, {"ok": False, "error": "origin check failed"})
            return False
        if not auth_ok(c.get("token"), self.headers.get("Authorization")):
            self._deny()
            return False
        return True

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
        if not self._gate():
            return
        p = self.path.split("?")[0]
        c = self.ctx
        page = c.get("page")
        if p == "/" and page is not None:
            data = page() if callable(page) else page
            self._send_bytes(200, data, "text/html; charset=utf-8")
        elif p == "/api" and c.get("api"):
            self._send(200, c["api"]())
        elif p == "/api/config" and c.get("get_config"):
            try:
                self._send(200, c["get_config"]())
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})
        elif p == "/api/dnsquery" and c.get("dns_queries"):
            self._send(200, {"queries": c["dns_queries"]()})
        elif p == "/api/dnshitrate" and c.get("dns_stats"):
            stats = c["dns_stats"]()
            self._send(200, stats if stats else {"ok": False, "error": "未读到 dnsmasq 统计"})
        elif p == "/api/healthtest":
            self._send(200, {"items": [{"icon": "✅", "title": "测试项", "detail": "API 正常"}]})
        elif p == "/api/adstats" and c.get("ad_stats"):
            self._send(200, c["ad_stats"]())
        elif p == "/healthpage":
            try:
                items = c["health_check"]()
                lines = ['<div class="item"><span class="icon">%s</span><span class="title">%s</span><span class="detail">%s</span></div>' %
                         (it.get("icon", ""), it.get("title", ""), it.get("detail", "")) for it in items]
                body = '<h3>一键体检</h3>' + "".join(lines)
            except Exception as e:
                body = '<h3>体检失败</h3><div class="detail">%s</div>' % str(e)
            html = ('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
                    '<title>健康检查</title><style>'
                    'body{font-family:sans-serif;background:#13161a;color:#d0d4d9;padding:20px;max-width:600px;margin:0 auto}'
                    '.item{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid #2a3038}'
                    '.icon{font-size:20px}.title{flex:1}.detail{color:#7d8896;font-size:13px}'
                    'h3{color:#e0e4e9;margin-bottom:16px}'
                    '</style></head><body>%s</body></html>' % body)
            self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif p == "/api/health" and c.get("health_check"):
            try:
                self._send(200, {"items": c["health_check"]()})
            except BrokenPipeError:
                pass
        elif p == "/api/version":
            remote = get_remote_version()
            self._send(200, {"local": KIT_VERSION, "latest": remote or KIT_VERSION,
                             "update_available": remote is not None and remote != KIT_VERSION})
        elif p.startswith("/download/") and c.get("read_backup"):
            name = urllib.parse.unquote(p[len("/download/"):])
            blob = c["read_backup"](name)
            if blob is None:
                self._send(404, {"ok": False, "error": "备份文件不存在"})
            else:
                self._send_bytes(200, blob, "application/gzip",
                                 'attachment; filename="%s"' % os.path.basename(name))
        else:
            self._send(404, {"ok": False})

    def do_POST(self):
        if not self._gate(post=True):
            return
        p = self.path.split("?")[0]
        c = self.ctx
        if p == "/api/act" and c.get("do_action"):
            is_json = False
            try:
                ln = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(ln).decode("utf-8", "replace")
                action, params, is_json = parse_act_body(raw)
                msg = c["do_action"](action, params)
            except Exception as e:
                # 应答形态与解析形态同源：表单提交出错也走 302 回跳，浏览器不再裸渲染 JSON
                if is_json:
                    self._send(200, {"ok": False, "error": str(e)})
                else:
                    self.send_response(302)
                    self.send_header("Location", "/?msg=" + urllib.parse.quote("操作出错: " + str(e)))
                    self.end_headers()
                return
            if is_json:
                self._send(200, {"ok": True, "msg": msg})
            else:
                # 表单提交（浏览器按钮）：302 回首页经 ?msg= 展示
                self.send_response(302)
                self.send_header("Location", "/?msg=" + urllib.parse.quote(str(msg)))
                self.end_headers()
        elif p == "/api/nettest" and c.get("do_action"):
            try:
                fn = c.get("net_test")
                msg = fn() if fn else c["do_action"]("net_test", {})
                self._send(200, {"ok": True, "msg": msg})
            except Exception as e:
                self._send(200, {"ok": False, "msg": str(e)})
        else:
            self._send(404, {"ok": False})

    def log_message(self, *a):
        pass


def serve(ctx):
    ctx = dict(ctx)
    # 默认每次请求都从磁盘读模板（HTML 热加载；业务侧可传 bytes 覆盖此行为）
    if "page" not in ctx:
        ctx["page"] = load_page
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
        where = "0.0.0.0(双栈)/%d · HTTP Basic 已启用" % port
    else:
        class Srv4(ThreadingHTTPServer):
            daemon_threads = True
        srv = Srv4(("127.0.0.1", port), Handler)
        where = "127.0.0.1:%d (仅本机)" % port
    print("  面板地址: http://127.0.0.1:%d  监听: %s" % (port, where))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
