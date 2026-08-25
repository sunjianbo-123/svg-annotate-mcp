"""本地 HTTP 服务:UI 页面、SVG 原文、会话 API、批注提交、SSE 事件流。

只绑 127.0.0.1;stdout 属于 stdio MCP 通道,日志一律走 logging(stderr)。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .state import STATE

log = logging.getLogger("svg-annotate")

WEB_DIR = Path(__file__).parent / "web"
SSE_PING_S = 15
MAX_SUBMIT_BYTES = 20 * 1024 * 1024  # 批注可含粘贴截图(base64),放宽到 20MB


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # BaseHTTPRequestHandler 默认往 stderr 写访问日志,改走 logging 统一控制
    def log_message(self, fmt: str, *args) -> None:
        log.debug("http %s", fmt % args)

    # ---------- 小工具 ----------

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                page = (WEB_DIR / "index.html").read_bytes()
                self._send_bytes(page, "text/html; charset=utf-8")
            elif path == "/svg":
                self._serve_svg()
            elif path == "/api/session":
                self._serve_session()
            elif path == "/api/events":
                self._serve_events()
            else:
                self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/submit":
                self._handle_submit()
            else:
                self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- 各端点 ----------

    def _serve_svg(self) -> None:
        session = STATE.get_session()
        if not session:
            self._send_json({"error": "no session"}, 404)
            return
        try:
            body = Path(session["svg_path"]).read_bytes()
        except OSError as e:
            self._send_json({"error": f"读取 SVG 失败: {e}"}, 500)
            return
        self._send_bytes(body, "image/svg+xml; charset=utf-8")

    def _serve_session(self) -> None:
        session = STATE.get_session()
        if not session:
            self._send_json({"error": "no session"}, 404)
            return
        self._send_json(session)

    def _handle_submit(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_SUBMIT_BYTES:
            self._send_json({"error": "请求体大小非法"}, 400)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            annotations = payload["annotations"]
            assert isinstance(annotations, list)
        except Exception:
            self._send_json({"error": "JSON 解析失败,需要 {annotations: [...]}"}, 400)
            return
        if not annotations:
            self._send_json({"error": "批注为空"}, 400)
            return
        try:
            batch = STATE.submit_batch(annotations)
        except RuntimeError as e:
            self._send_json({"error": str(e)}, 409)
            return
        self._send_json({"status": "ok", "batch_id": batch["batch_id"]})

    def _serve_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        cid, q = STATE.register_sse()
        try:
            # 接入即推一次会话快照,页面据此初始化/换图
            session = STATE.get_session()
            if session:
                self._write_event("session", session)
            while True:
                try:
                    ev = q.get(timeout=SSE_PING_S)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._write_event(ev["event"], ev["data"])
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            STATE.unregister_sse(cid)

    def _write_event(self, event: str, data: dict) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()


def start_http_server() -> int:
    """起 127.0.0.1 临时端口的 HTTP 线程,返回端口;已在跑则直接返回。"""
    if STATE.http_port is not None:
        return STATE.http_port
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    STATE.http_port = port
    threading.Thread(target=httpd.serve_forever, daemon=True, name="svg-http").start()
    log.info("HTTP 服务已启动: http://127.0.0.1:%d/", port)
    return port
