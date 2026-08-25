"""端到端冒烟:stdio 握手 → open_svg → HTTP 端点 → 阻塞等待+提交 → SSE reload。

跑法:cd 项目根目录后 `uv run python tests/smoke_test.py`,全绿输出 PASS。
不引 pytest,断言即协议。
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "mini.svg")


class Stdio:
    def __init__(self) -> None:
        env = dict(os.environ, SVG_ANNOTATE_NO_OPEN="1")
        self.p = subprocess.Popen(
            ["uv", "run", "--directory", ROOT, "svg-annotate-mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=env,
        )
        self._id = 0

    def send(self, method: str, params: dict | None = None, notify: bool = False) -> int | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        return None if notify else self._id

    def recv(self, want_id: int, timeout_s: float = 40) -> dict:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("server stdout 关闭")
            msg = json.loads(line)  # 解析失败即 stdout 被污染,直接抛
            if msg.get("id") == want_id:
                return msg
        raise TimeoutError(f"等待 id={want_id} 超时")

    def call_tool(self, name: str, args: dict, timeout_s: float = 40) -> dict:
        rid = self.send("tools/call", {"name": name, "arguments": args})
        msg = self.recv(rid, timeout_s)
        result = msg["result"]
        if "structuredContent" in result and result["structuredContent"]:
            sc = result["structuredContent"]
            return sc.get("result", sc)
        return json.loads(result["content"][0]["text"])


def sse_listen(port: int, events: list, stop: threading.Event) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    cur_event = None
    while not stop.is_set():
        raw = resp.fp.readline()
        if not raw:
            break
        line = raw.decode("utf-8").strip()
        if line.startswith("event:"):
            cur_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and cur_event:
            events.append((cur_event, json.loads(line.split(":", 1)[1])))
            cur_event = None


def wait_for(pred, timeout_s: float, what: str):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise TimeoutError(what)


def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="svg-annotate-smoke-")
    svg_path = os.path.join(tmpdir, "mini.svg")
    shutil.copy(FIXTURE, svg_path)
    s = Stdio()
    try:
        # 1. 握手
        rid = s.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "smoke", "version": "0"}})
        init = s.recv(rid)
        assert init["result"]["serverInfo"]["name"] == "svg-annotate"
        s.send("notifications/initialized", notify=True)
        print("[1] stdio 握手 ok")

        # 2. open_svg + HTTP 端点
        r = s.call_tool("open_svg", {"svg_path": svg_path, "source_script": "", "title": "smoke"})
        assert r["status"] == "ok", r
        assert r["view_box"] == [0, 0, 200, 100], r["view_box"]
        assert r["reused_tab"] is False
        port = int(r["url"].rsplit(":", 1)[1].strip("/"))
        sess = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/session"))
        assert sess["svg_path"] == svg_path
        svg_body = urllib.request.urlopen(f"http://127.0.0.1:{port}/svg").read()
        assert b"</svg>" in svg_body and b"text_1" in svg_body
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read()
        assert b"<" in page
        print(f"[2] open_svg + HTTP 端点 ok (port={port})")

        # 3. SSE 接入(接入即收 session 快照)
        events: list = []
        stop = threading.Event()
        t = threading.Thread(target=sse_listen, args=(port, events, stop), daemon=True)
        t.start()
        wait_for(lambda: any(e[0] == "session" for e in events), 5, "SSE 未收到 session 快照")
        print("[3] SSE 接入 + session 快照 ok")

        # 4. 阻塞等待 + 提交 + 坐标换算
        result_box: dict = {}
        def waiter():
            result_box["r"] = s.call_tool("wait_for_annotations", {"timeout_s": 30})
        wt = threading.Thread(target=waiter, daemon=True)
        wt.start()
        time.sleep(0.8)
        ann = {"number": 1, "kind": "rect", "note": "把这行字加大",
               "geometry_norm": {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.5},
               "hits": [{"tag": "text", "id": "text_1", "text": "hello legend",
                         "ancestors": ["figure_1", "axes_1", "text_1"],
                         "bbox_norm": [0.1, 0.2, 0.3, 0.1], "coverage": 1.0}],
               "texts_in_region": ["hello legend"]}
        png_b64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                   "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        ann_el = {"number": 2, "kind": "element", "note": "该文字改 Arial",
                  "geometry_norm": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1},
                  "target": {"tag": "text", "id": "text_1", "text": "hello legend",
                             "ancestors": ["figure_1", "axes_1", "text_1"],
                             "bbox_norm": [0.1, 0.2, 0.3, 0.1]},
                  "images": [{"data_url": f"data:image/png;base64,{png_b64}",
                              "w": 1, "h": 1, "media_type": "image/png"},
                             {"data_url": "data:image/png;base64,",  # 空负载应被丢弃并计数
                              "w": 1, "h": 1, "media_type": "image/png"}],
                  "hits": [], "texts_in_region": ["hello legend"]}
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/submit",
                                     data=json.dumps({"annotations": [ann, ann_el]}).encode(),
                                     headers={"Content-Type": "application/json"})
        sub = json.load(urllib.request.urlopen(req))
        assert sub["batch_id"] == 1, sub
        wt.join(10)
        r = result_box["r"]
        assert r["status"] == "submitted" and r["batch_id"] == 1, r
        g = r["annotations"][0]["geometry_svg"]
        assert g == {"x": 20.0, "y": 20.0, "w": 100.0, "h": 50.0}, g
        bb = r["annotations"][0]["hits"][0]["bbox_svg"]
        assert bb == [20.0, 20.0, 60.0, 10.0], bb
        assert "bbox_norm" not in r["annotations"][0]["hits"][0]
        el = r["annotations"][1]
        assert el["kind"] == "element" and el["target"]["id"] == "text_1", el
        assert el["target"]["bbox_svg"] == [20.0, 20.0, 60.0, 10.0], el["target"]
        assert "bbox_norm" not in el["target"]
        assert el["geometry_svg"] == {"x": 20.0, "y": 20.0, "w": 60.0, "h": 10.0}, el
        assert len(el["images"]) == 1 and el["images_dropped"] == 1, el  # 空负载被丢弃并计数
        img = el["images"][0]
        assert "data_url" not in img, img  # 回传 payload 不携带 base64
        expected_png = base64.b64decode(png_b64)
        img_path = pathlib.Path(img["path"])
        assert img_path.is_file() and img_path.read_bytes() == expected_png, img
        assert img["media_type"] == "image/png" and img["bytes"] == len(expected_png), img
        assert img["w"] == 1 and img["h"] == 1, img
        print("[4] 阻塞等待 + 提交 + geometry_svg/bbox_svg/target 换算 + 截图落盘 ok")

        # 4b. FIFO 队列:快速连提两批,逐批按序取回,不覆盖不丢批
        for note in ("第一批", "第二批"):
            ann_q = {"number": 1, "kind": "rect", "note": note,
                     "geometry_norm": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                     "hits": [], "texts_in_region": []}
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/submit",
                                         data=json.dumps({"annotations": [ann_q]}).encode(),
                                         headers={"Content-Type": "application/json"})
            json.load(urllib.request.urlopen(req))
        r1 = s.call_tool("wait_for_annotations", {"timeout_s": 5}, timeout_s=15)
        r2 = s.call_tool("wait_for_annotations", {"timeout_s": 5}, timeout_s=15)
        assert r1["annotations"][0]["note"] == "第一批" and r1["pending"] == 1, r1
        assert r2["annotations"][0]["note"] == "第二批" and r2["pending"] == 0, r2
        r3 = s.call_tool("get_annotations", {})
        assert r3["annotations"][0]["note"] == "第二批" and r3["already_taken"] is True, r3
        print("[4b] FIFO 队列连提两批不丢 + 兜底重读 ok")

        # 5. wait 超时语义(无新批注时返回 timeout 而非报错)
        r = s.call_tool("wait_for_annotations", {"timeout_s": 5}, timeout_s=15)
        assert r["status"] == "timeout" and r["has_client"] is True, r
        print("[5] wait 超时返回 timeout ok")

        # 6. 文件更新 → SSE reload(watcher 双拍稳定,约 1.5s)
        time.sleep(0.2)
        os.utime(svg_path)
        wait_for(lambda: any(e[0] == "reload" for e in events), 6, "SSE 未收到 reload")
        print("[6] watcher → SSE reload ok")

        # 7. set_status + 复用 tab(SSE 在册时 open 不再开新 tab)
        r = s.call_tool("set_status", {"message": "正在修改…"})
        assert r["delivered_to"] >= 1, r
        r = s.call_tool("open_svg", {"svg_path": svg_path, "title": "再次打开"})
        assert r["reused_tab"] is True, r
        wait_for(lambda: sum(1 for e in events if e[0] == "session") >= 2, 5, "换图未推 session")
        print("[7] set_status + 复用 tab ok")

        stop.set()
        print("PASS")
    finally:
        s.p.terminate()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
