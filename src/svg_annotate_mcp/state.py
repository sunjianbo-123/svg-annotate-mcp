"""共享状态:单例会话、批注收件箱、SSE 客户端表、文件 watcher。

线程模型:
- MCP 工具跑在 asyncio 事件循环(主线程);
- HTTP handler 跑在 ThreadingHTTPServer 工作线程;
- watcher 是独立 daemon 线程,换图时靠 generation 计数退役旧线程。
跨线程共享数据一律经 self._lock;SSE 每个连接一个 queue.Queue。
"""

from __future__ import annotations

import base64
import logging
import os
import queue
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path

log = logging.getLogger("svg-annotate")

WATCH_INTERVAL_S = 0.5
PASTE_DIR = Path(tempfile.gettempdir()) / "svg_annotate_pastes"
MAX_IMAGES_PER_ANNOTATION = 3
MAX_PENDING_BATCHES = 50


def persist_annotation_images(ann: dict, batch_id: int, ann_index: int) -> None:
    """把批注里粘贴的截图(data URL)落盘为文件,payload 中只留路径。

    回传给 Claude 的批注绝不携带 base64(会撑爆工具结果);Claude 用 Read
    打开 images[].path 即可看图。解码/写盘失败的条目跳过并告警,不阻塞
    提交;有丢弃时在批注上留 images_dropped 计数,让 Claude 知道有截图缺失。
    文件名带 uuid 成分:跨会话/换图后 batch 计数重置也绝不互相覆盖。
    """
    images = ann.get("images")
    if not isinstance(images, list) or not images:
        ann.pop("images", None)
        return
    saved: list[dict] = []
    for i, img in enumerate(images[:MAX_IMAGES_PER_ANNOTATION]):
        if not isinstance(img, dict):
            continue
        data_url = img.get("data_url")
        if not isinstance(data_url, str) or "," not in data_url:
            continue
        header, b64 = data_url.split(",", 1)
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:
            log.warning("批注 #%d 第 %d 张截图 base64 解码失败,已跳过", ann_index, i + 1)
            continue
        if not raw:
            log.warning("批注 #%d 第 %d 张截图为空负载,已跳过", ann_index, i + 1)
            continue
        ext = ".jpg" if "image/jpeg" in header else ".png"
        try:
            PASTE_DIR.mkdir(parents=True, exist_ok=True)
            path = PASTE_DIR / f"batch{batch_id}_ann{ann_index}_{i + 1}_{uuid.uuid4().hex[:8]}{ext}"
            path.write_bytes(raw)
        except OSError as e:
            log.warning("批注截图写盘失败(%s),已跳过", e)
            continue
        saved.append({
            "path": str(path),
            "media_type": img.get("media_type") or ("image/jpeg" if ext == ".jpg" else "image/png"),
            "w": img.get("w"),
            "h": img.get("h"),
            "bytes": len(raw),
        })
    dropped = len(images) - len(saved)
    if dropped > 0:
        ann["images_dropped"] = dropped
    if saved:
        ann["images"] = saved
    else:
        ann.pop("images", None)


def _round2(v: float) -> float:
    return round(v, 2)


def norm_to_svg(geom: dict, view_box: list[float]) -> dict:
    """把 0-1 归一化几何换算成 viewBox 坐标。

    识别的键:x/y/w/h、x1/y1/x2/y2、points([[x,y],...])。其余键原样丢弃。
    """
    vx, vy, vw, vh = view_box
    out: dict = {}
    for k, val in geom.items():
        if not isinstance(val, (int, float)) and k != "points":
            continue
        if k in ("x", "x1", "x2"):
            out[k] = _round2(vx + float(val) * vw)
        elif k in ("y", "y1", "y2"):
            out[k] = _round2(vy + float(val) * vh)
        elif k == "w":
            out[k] = _round2(float(val) * vw)
        elif k == "h":
            out[k] = _round2(float(val) * vh)
        elif k == "points" and isinstance(val, list):
            out[k] = [
                [_round2(vx + float(p[0]) * vw), _round2(vy + float(p[1]) * vh)]
                for p in val
                if isinstance(p, (list, tuple)) and len(p) >= 2
            ]
    return out


def bbox_norm_to_svg(bbox: list[float], view_box: list[float]) -> list[float]:
    """[x,y,w,h] 归一化包围盒 → viewBox 坐标。"""
    vx, vy, vw, vh = view_box
    x, y, w, h = bbox
    return [_round2(vx + x * vw), _round2(vy + y * vh), _round2(w * vw), _round2(h * vh)]


class AppState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.session: dict | None = None
        self._batch_queue: deque[dict] = deque()
        self._last_taken: dict | None = None
        self._batch_counter = 0
        self._sse_clients: dict[int, queue.Queue] = {}
        self._sse_counter = 0
        self.http_port: int | None = None
        self._watcher_gen = 0

    # ---------- 会话 ----------

    def set_session(self, session: dict) -> None:
        with self._lock:
            self.session = session
            # 换图后旧图的批注坐标/锚点全部作废,清空收件队列
            self._batch_queue.clear()
            self._last_taken = None
            self._batch_counter = 0

    def get_session(self) -> dict | None:
        with self._lock:
            return dict(self.session) if self.session else None

    # ---------- SSE ----------

    def register_sse(self) -> tuple[int, queue.Queue]:
        with self._lock:
            self._sse_counter += 1
            cid = self._sse_counter
            q: queue.Queue = queue.Queue()
            self._sse_clients[cid] = q
            log.info("SSE 客户端 #%d 接入(现存 %d)", cid, len(self._sse_clients))
            return cid, q

    def unregister_sse(self, cid: int) -> None:
        with self._lock:
            self._sse_clients.pop(cid, None)
            log.info("SSE 客户端 #%d 断开(现存 %d)", cid, len(self._sse_clients))

    def sse_client_count(self) -> int:
        with self._lock:
            return len(self._sse_clients)

    def broadcast(self, event: str, data: dict) -> int:
        with self._lock:
            clients = list(self._sse_clients.values())
        for q in clients:
            q.put({"event": event, "data": data})
        return len(clients)

    # ---------- 批注收件箱 ----------

    def submit_batch(self, annotations: list[dict]) -> dict:
        """HTTP 线程收到 /api/submit 后调用:换算坐标、编号、入箱。"""
        with self._lock:
            if self.session is None:
                raise RuntimeError("没有活跃会话")
            view_box = self.session["view_box"]
            self._batch_counter += 1
            batch_id = self._batch_counter
        for ann_index, ann in enumerate(annotations, start=1):
            persist_annotation_images(ann, batch_id, ann_index)
            if isinstance(ann.get("geometry_norm"), dict):
                ann["geometry_svg"] = norm_to_svg(ann["geometry_norm"], view_box)
            for hit in ann.get("hits", []) or []:
                bbox = hit.pop("bbox_norm", None)
                if isinstance(bbox, list) and len(bbox) == 4:
                    hit["bbox_svg"] = bbox_norm_to_svg(bbox, view_box)
            target = ann.get("target")
            if isinstance(target, dict):
                bbox = target.pop("bbox_norm", None)
                if isinstance(bbox, list) and len(bbox) == 4:
                    target["bbox_svg"] = bbox_norm_to_svg(bbox, view_box)
        batch = {
            "batch_id": batch_id,
            "submitted_at": time.time(),
            "annotations": annotations,
        }
        with self._lock:
            # FIFO 队列:快速连提不覆盖、不丢批(旧实现单槽 latest_batch 会被顶掉)
            self._batch_queue.append(batch)
            if len(self._batch_queue) > MAX_PENDING_BATCHES:
                dropped = self._batch_queue.popleft()
                log.warning("批注队列超过 %d 批,丢弃最旧批次 #%d",
                            MAX_PENDING_BATCHES, dropped["batch_id"])
        log.info("收到批注批次 #%d(%d 条)", batch_id, len(annotations))
        return batch

    def take_new_batch(self) -> dict | None:
        """按提交顺序取走一批(FIFO);带 pending=队列剩余批数,无待取批次返回 None。"""
        with self._lock:
            if not self._batch_queue:
                return None
            batch = self._batch_queue.popleft()
            self._last_taken = batch
            return {**batch, "pending": len(self._batch_queue)}

    def peek_latest_batch(self) -> dict | None:
        """兜底恢复:队列有货按 FIFO 取走一批;队列空则重读最后取走的那批。"""
        with self._lock:
            if self._batch_queue:
                batch = self._batch_queue.popleft()
                self._last_taken = batch
                return {**batch, "pending": len(self._batch_queue)}
            if self._last_taken:
                return {**self._last_taken, "pending": 0, "already_taken": True}
            return None

    # ---------- 文件 watcher ----------

    def start_watcher(self, svg_path: str) -> None:
        """换图/开图时调用;旧 watcher 因 generation 不匹配自行退出。"""
        with self._lock:
            self._watcher_gen += 1
            gen = self._watcher_gen
        t = threading.Thread(
            target=self._watch_loop, args=(svg_path, gen), daemon=True, name=f"svg-watcher-{gen}"
        )
        t.start()

    def _current_gen(self) -> int:
        with self._lock:
            return self._watcher_gen

    def _watch_loop(self, svg_path: str, gen: int) -> None:
        log.info("watcher #%d 开始监视 %s", gen, svg_path)
        try:
            st = os.stat(svg_path)
            known = (st.st_mtime_ns, st.st_size)
        except OSError:
            known = None
        pending: tuple | None = None  # 双拍稳定:第一次见到的新 (mtime_ns, size)
        while self._current_gen() == gen:
            time.sleep(WATCH_INTERVAL_S)
            try:
                st = os.stat(svg_path)
                cur = (st.st_mtime_ns, st.st_size)
            except OSError:
                pending = None
                continue
            if cur == known:
                pending = None
                continue
            if pending != cur:
                pending = cur  # 第一拍,下一拍再确认
                continue
            # 连续两拍相同 → 校验文件完整(matplotlib savefig 可能写一半)
            try:
                with open(svg_path, "rb") as f:
                    f.seek(max(0, st.st_size - 512))
                    tail = f.read()
                if b"</svg>" not in tail:
                    continue
            except OSError:
                continue
            known = cur
            pending = None
            mtime = st.st_mtime_ns / 1e9
            with self._lock:
                if self.session and self.session["svg_path"] == svg_path:
                    self.session["svg_mtime"] = mtime
            log.info("watcher #%d 检测到文件更新,推送 reload", gen)
            self.broadcast("reload", {"svg_mtime": mtime})
        log.info("watcher #%d 退役", gen)


STATE = AppState()
