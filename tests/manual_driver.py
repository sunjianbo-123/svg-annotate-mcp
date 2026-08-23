"""手动/浏览器自动化测试驱动:起 server → open_svg → 循环等批注并落盘。

用法:uv run python tests/manual_driver.py <svg_path> [source_script]
- 打印 URL(不自动开浏览器,由测试者/自动化自行访问);
- 每收到一批批注,往 stdout 打一行 `BATCH: {...}` 并追加写 tests/out/batches.jsonl;
- Ctrl-C 退出。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoke_test import ROOT, Stdio  # noqa: E402

OUT = os.path.join(ROOT, "tests", "out", "batches.jsonl")


def main() -> None:
    svg_path = os.path.abspath(sys.argv[1])
    source_script = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else ""
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    s = Stdio()
    try:
        rid = s.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "driver", "version": "0"}})
        s.recv(rid)
        s.send("notifications/initialized", notify=True)
        r = s.call_tool("open_svg", {"svg_path": svg_path, "source_script": source_script})
        print("OPEN:", json.dumps(r, ensure_ascii=False), flush=True)
        if r.get("status") != "ok":
            return
        print(f"URL: {r['url']}", flush=True)
        while True:
            r = s.call_tool("wait_for_annotations", {"timeout_s": 55}, timeout_s=70)
            if r.get("status") == "submitted":
                line = json.dumps(r, ensure_ascii=False)
                print("BATCH:", line, flush=True)
                with open(OUT, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            else:
                print("WAIT:", r.get("status"), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        s.p.terminate()


if __name__ == "__main__":
    main()
