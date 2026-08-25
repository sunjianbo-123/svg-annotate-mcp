"""svg-annotate MCP server:4 个工具 + stdio 入口。

stdout 只属于 MCP JSON-RPC,所有日志经 logging 走 stderr。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys

from mcp.server.mcpserver import MCPServer

from .http_app import start_http_server
from .state import STATE

log = logging.getLogger("svg-annotate")

MAX_SVG_BYTES = 20 * 1024 * 1024
WAIT_POLL_S = 0.25
WAIT_TIMEOUT_MAX_S = 540

mcp = MCPServer("svg-annotate")

_VIEWBOX_RE = re.compile(r'viewBox\s*=\s*["\']([^"\']+)["\']')
_DIM_RE = {
    "width": re.compile(r'<svg[^>]*?\swidth\s*=\s*["\']([\d.]+)\s*(?:pt|px|mm|cm|in)?["\']'),
    "height": re.compile(r'<svg[^>]*?\sheight\s*=\s*["\']([\d.]+)\s*(?:pt|px|mm|cm|in)?["\']'),
}
_EXTERNAL_REF_RE = re.compile(r'(?:xlink:)?href\s*=\s*["\'](?!#|data:)|url\(\s*["\']?https?:')


def _parse_view_box(svg_text: str) -> list[float] | None:
    m = _VIEWBOX_RE.search(svg_text)
    if m:
        parts = m.group(1).replace(",", " ").split()
        if len(parts) == 4:
            try:
                vb = [float(p) for p in parts]
                if vb[2] > 0 and vb[3] > 0:
                    return vb
            except ValueError:
                pass
    mw = _DIM_RE["width"].search(svg_text)
    mh = _DIM_RE["height"].search(svg_text)
    if mw and mh:
        try:
            w, h = float(mw.group(1)), float(mh.group(1))
            if w > 0 and h > 0:
                return [0.0, 0.0, w, h]
        except ValueError:
            pass
    return None


@mcp.tool()
async def open_svg(svg_path: str, source_script: str = "", title: str = "") -> dict:
    """在浏览器中打开一个 SVG 图,供用户圈选区域写批注。

    打开后应调用 wait_for_annotations 等待用户提交批注。
    svg_path 必须是绝对路径。source_script 可选:该 SVG 的生成脚本路径
    (如 matplotlib 的 fig_*.py);之后每批批注都会原样带回该路径,
    便于判断是改 SVG 文件还是改脚本重跑。title 可选,显示在页面顶栏。
    """
    if not os.path.isabs(svg_path):
        return {"status": "error", "message": f"svg_path 必须是绝对路径: {svg_path}"}
    if not os.path.isfile(svg_path):
        return {"status": "error", "message": f"文件不存在: {svg_path}"}
    if not svg_path.lower().endswith(".svg"):
        return {"status": "error", "message": f"不是 .svg 文件: {svg_path}"}
    size = os.path.getsize(svg_path)
    if size > MAX_SVG_BYTES:
        return {"status": "error", "message": f"SVG 超过 20MB({size} 字节),拒绝加载"}

    try:
        svg_text = open(svg_path, encoding="utf-8", errors="replace").read()
    except OSError as e:
        return {"status": "error", "message": f"读取失败: {e}"}

    view_box = _parse_view_box(svg_text)
    if view_box is None:
        return {"status": "error", "message": "无法从 SVG 解析 viewBox 或 width/height"}

    warnings: list[str] = []
    if _EXTERNAL_REF_RE.search(svg_text):
        warnings.append("SVG 含非 data:/# 的外部引用,页面内可能渲染不完整(第一版不代理外链资源)")
    if source_script and not os.path.isfile(source_script):
        warnings.append(f"source_script 不存在: {source_script}")

    session = {
        "svg_path": svg_path,
        "filename": os.path.basename(svg_path),
        "source_script": source_script,
        "title": title or os.path.basename(svg_path),
        "view_box": view_box,
        "svg_mtime": os.path.getmtime(svg_path),
    }
    STATE.set_session(session)
    port = start_http_server()
    STATE.start_watcher(svg_path)
    url = f"http://127.0.0.1:{port}/"

    reused = STATE.sse_client_count() > 0
    if reused:
        STATE.broadcast("session", session)
        log.info("复用已打开的浏览器页(推 session 事件换图)")
    elif os.environ.get("SVG_ANNOTATE_NO_OPEN"):
        log.info("SVG_ANNOTATE_NO_OPEN 已设,跳过打开浏览器")
    else:
        try:
            subprocess.run(["open", url], check=False)
        except OSError as e:
            warnings.append(f"自动打开浏览器失败({e}),请手动访问 {url}")

    return {
        "status": "ok",
        "url": url,
        "reused_tab": reused,
        "warnings": warnings,
        **session,
    }


@mcp.tool()
async def wait_for_annotations(timeout_s: int = 120) -> dict:
    """阻塞等待用户在浏览器里提交批注,提交后立即返回批注内容。

    重要协议:返回 status="timeout" 表示用户还没提交,这不是错误——
    若仍在等待用户批注,应立即再次调用本工具继续等待,直到拿到
    status="submitted" 或用户在对话里明确说不批注了。
    返回的每条批注含:kind(rect/arrow/freehand/text)、note(用户文字)、
    geometry_svg(viewBox 坐标)、hits(命中的 SVG 元素:id/文字/层级链,
    用于在 SVG 源文件或生成脚本中定位)、texts_in_region(选区内的文字)。
    批注可含 images(用户粘贴在批注里的截图,已存为本地文件):
    动手改图前,先用 Read 工具逐个打开 images[].path 查看截图内容,
    截图与 note 文字是同一条修改意见的图文上下文。
    批次按提交顺序排队(FIFO),快速连提不丢批:返回的 pending 字段
    表示队列中还有几批在等——pending>0 时处理完本批应立即再调本工具,
    会立刻返回下一批。
    """
    if STATE.get_session() is None:
        return {"status": "no_session", "message": "还没有打开任何 SVG,请先调用 open_svg"}
    timeout_s = max(5, min(int(timeout_s), WAIT_TIMEOUT_MAX_S))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        batch = STATE.take_new_batch()
        if batch:
            session = STATE.get_session() or {}
            return {
                "status": "submitted",
                "svg_path": session.get("svg_path"),
                "source_script": session.get("source_script"),
                "svg_mtime": session.get("svg_mtime"),
                **batch,
            }
        await asyncio.sleep(WAIT_POLL_S)
    return {
        "status": "timeout",
        "waited_s": timeout_s,
        "has_client": STATE.sse_client_count() > 0,
        "hint": "用户尚未提交批注。若仍在等待,请立即再次调用 wait_for_annotations。",
    }


@mcp.tool()
async def get_annotations() -> dict:
    """非阻塞地取一批已提交的批注(wait_for_annotations 的兜底)。

    用于超时链中断后恢复现场:队列有货按提交顺序取走一批(pending
    表示剩余);队列空则重读最后取走的那批(带 already_taken=true,
    表示该批此前已交付过,谨防重复施工);从未有批注返回 status="empty"。
    """
    session = STATE.get_session()
    if session is None:
        return {"status": "no_session", "message": "还没有打开任何 SVG,请先调用 open_svg"}
    batch = STATE.peek_latest_batch()
    if not batch:
        return {"status": "empty", "has_client": STATE.sse_client_count() > 0}
    return {
        "status": "submitted",
        "svg_path": session.get("svg_path"),
        "source_script": session.get("source_script"),
        "has_client": STATE.sse_client_count() > 0,
        **batch,
    }


@mcp.tool()
async def set_status(message: str) -> dict:
    """向批注页面顶栏推送一条状态文字,如「正在修改 fig_xxx.py 并重跑…」。

    修改开始前调用一次,能让用户知道 Claude 正在干活;改完文件后
    页面会因文件变化自动刷新,无需再调用本工具通知。
    """
    n = STATE.broadcast("status", {"message": message})
    return {"delivered_to": n}


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("svg-annotate MCP server 启动(stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
