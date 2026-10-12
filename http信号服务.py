#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地 HTTP 交易信号接收服务。

只负责 HTTP 鉴权、JSON 解析和格式校验；下单规则由 FollowerEngine 统一处理。

鉴权: 必须配置 http_api_token, 请求需带 `Authorization: Bearer <token>`。

接口:
    GET  /health      健康检查(免鉴权)
    GET  /            端点列表(免鉴权, 机器可读)
    POST /signal      接收交易信号 -> 交给 callback
    GET  /<query>     查询接口, 由构造时的 queries 决定(如 /positions /orders)
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qsl


class _SignalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class HTTPSignalServer:
    """将 POST /signal 请求转换为标准信号并交给 callback。

    queries: {"/positions": fn(params)->dict, ...} 形式的查询路由。
             这些路由同样要求 Bearer 鉴权, 返回值会并入 {"ok": true, ...}。
    """

    def __init__(self, host, port, token, callback, log=None, queries=None):
        self.host = str(host or "127.0.0.1")
        self.port = int(port)
        self.token = str(token or "")
        self.callback = callback
        self.log = log or (lambda message, level="INFO": None)
        self.queries = dict(queries or {})
        self._server = None
        self._thread = None

    @property
    def address(self):
        if self._server is None:
            return self.host, self.port
        return self._server.server_address

    def start(self):
        if self._server is not None:
            return
        if not self.token:
            raise ValueError("HTTP API 必须配置 http_api_token")
        callback = self.callback
        token = self.token
        log = self.log
        queries = self.queries

        class Handler(BaseHTTPRequestHandler):
            server_version = "THSOrderHTTP/1.0"

            def log_message(self, format_string, *args):
                return

            def _send_json(self, status, payload):
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()

            def _authed(self):
                """校验 Bearer Token; 没通过就直接回 401 并返回 False"""
                if self.headers.get("Authorization", "") != "Bearer " + token:
                    self._send_json(401, {"ok": False, "error": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                # 免鉴权的两个端点: 只暴露"活着"和"有哪些接口", 不含任何账户数据
                if path == "/health":
                    self._send_json(200, {"ok": True, "service": "ths-order-http"})
                    return
                if path == "/":
                    self._send_json(200, {"ok": True, "service": "ths-order-http",
                                          "endpoints": ["GET /health", "POST /signal"]
                                          + ["GET " + p for p in sorted(queries)]})
                    return
                fn = queries.get(path)
                if fn is None:
                    self._send_json(404, {"ok": False, "error": "not_found"})
                    return
                if not self._authed():
                    return
                try:
                    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
                    result = fn(params) or {}
                    self._send_json(200, {"ok": True, **result})
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                except RuntimeError as exc:
                    # 业务上"暂时拿不到"(比如同花顺没开/权限没给) 用 503 更贴切
                    self._send_json(503, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    log("HTTP 查询处理异常: %s" % exc, "ERROR")
                    self._send_json(500, {"ok": False, "error": "internal_error"})

            def do_POST(self):
                if self.path != "/signal":
                    self._send_json(404, {"ok": False, "error": "not_found"})
                    return
                if not self._authed():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 64 * 1024:
                        raise ValueError("请求体大小无效")
                    body = self.rfile.read(length)
                    data = json.loads(body.decode("utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("请求体必须是 JSON 对象")
                    result = callback(data)
                    self._send_json(200, {"ok": True, **result})
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    log("HTTP 信号处理异常: %s" % exc, "ERROR")
                    self._send_json(500, {"ok": False, "error": "internal_error"})

        self._server = _SignalHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="http-signal-server",
            daemon=True,
        )
        self._thread.start()
        log("HTTP 信号服务已启动: http://%s:%d/signal" % self.address, "OK")

    def stop(self):
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        if server is not None:
            self.log("HTTP 信号服务已停止", "INFO")
