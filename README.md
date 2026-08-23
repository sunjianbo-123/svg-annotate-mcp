# svg-annotate-mcp

在浏览器里批注 SVG 论文图——**点击元素**(悬停即高亮语义元素,Claude Design 式)或圈选区域,批注(含命中的 SVG 元素信息)通过 MCP 回传给 Claude,由 Claude 修改源文件(SVG 或生成脚本),页面监视文件变化自动刷新——形成「批注 → 修改 → 刷新 → 再批注」闭环。

- 后端:Python + 官方 `mcp` SDK(`MCPServer`,stdio),进程内起 stdlib HTTP 线程(127.0.0.1 临时端口)。
- 前端:单文件 `src/svg_annotate_mcp/web/index.html`。SVG 经 fetch + DOMParser 注入 **Shadow DOM**(与页面样式/id 双向隔离),批注画在独立 overlay 上;提交时在浏览器端做元素命中检测。

## 安装与注册

```bash
cd ~/Projects/svg-annotate-mcp && uv sync
claude mcp add --scope user svg-annotate -- \
  uv run --directory /Users/boryant/Projects/svg-annotate-mcp svg-annotate-mcp
```

可选:放宽 MCP 客户端超时,让单次 `wait_for_annotations` 可以等更久(不设也能工作,循环会更频繁):在 shell profile 或 Claude Code settings 的 `env` 里设 `MCP_TOOL_TIMEOUT=600000`。

## 工具面(4 个)

| 工具 | 作用 |
|---|---|
| `open_svg(svg_path, source_script="", title="")` | 在浏览器打开 SVG。`source_script` 传该图的生成脚本路径(如 matplotlib 的 `fig_*.py`),之后每批批注原样带回。已有页面打开时复用 tab(推 session 事件换图)。 |
| `wait_for_annotations(timeout_s=120)` | 阻塞等用户点「提交给 Claude」。**超时返回 `status:"timeout"` 不是错误——继续等就立即再调一次**(循环协议,工具 description 里也写了)。 |
| `get_annotations()` | 非阻塞兜底:取最近一批已提交批注(超时链断掉后恢复现场)。 |
| `set_status(message)` | 向页面顶栏推一条状态(如「正在改 fig_xxx.py 重跑…」)。改完文件不用调它,页面会自动刷新。 |

## 元素级点选(v2)

默认「选择」工具下,悬停即实时高亮光标下的语义元素(matplotlib 的 `text_N`/`line2d_N`/`legend_N` 等 `g[id]` 组,取最小面积命中,标签片显示 id+文字);点击进入**元素检查器**(侧栏显示 id、逐字文字、祖先面包屑——点面包屑可改选父组,如 `text_54 → legend_1`);**在「修改说明」里输入内容才生成批注**(纯点击=检查器,不产生垃圾批注),Esc 或「取消选择」退出。元素批注在图上是虚线框+编号。

## 批注回传结构(设计给 Claude 定位用)

每条批注含:

- `kind`(element/rect/arrow/freehand/text)、`note`(用户的修改说明)、`number`(画布上的编号);
- `kind:"element"` 时带 **`target`**:被点选元素的 `tag`/`id`/`text`(逐字)/`ancestors`/`d_prefix`/`bbox_svg`——**这是最强定位锚,优先用它**(id 或 text 直接 grep 生成脚本/SVG);
- `geometry_norm`(0-1)与 `geometry_svg`(viewBox 坐标,服务端换算好);
- `hits[]`:选区命中的 SVG 元素,每个含 `tag` / `id`(matplotlib 的 `text_N`/`line2d_N` 等语义组)/ `text`(元素文字,逐字)/ `ancestors`(祖先 id 链,如 `["figure_1","legend_1","text_54"]`)/ `bbox_svg` / `coverage` / `d_prefix`(path 的 `d` 前 30 字符,直接改 SVG 时的 grep 锚)。已做去噪:背景容器剔除、语义组优先(不重复报组内叶子)、每条上限 10;
- `texts_in_region`:选区内全部文字(阅读序、去重)——在生成脚本里 grep 定位的第一线索。

**改哪里由 Claude 判断**:批注带回 `source_script` 时优先改脚本再重跑(改 SVG 产物会被下次重跑覆盖);没有脚本的图直接改 SVG 文件。页面对修改方式不做假设,只认文件 mtime 变化(500ms 轮询、双拍稳定、`</svg>` 尾校验防写一半)。

## 批注生命周期

未提交草稿在图刷新后保留;点「提交给 Claude」后该批变 35% 半透明留在图上(编号保留,便于对照);Claude 改完文件触发的下一次刷新会清掉半透明批注。

## 典型闭环

```
用户: 帮我改 figure11,我来圈
Claude: open_svg("/path/figures/figure11.svg", source_script="/path/fig_tri_complement.py")
        wait_for_annotations()          # 挂起
用户: (浏览器里圈图例写「图例移到右上」,点提交)
Claude: 收到批注 → set_status("正在改 fig_tri_complement.py…")
        → 改脚本 → 重跑出图 → 页面自动刷新
        → wait_for_annotations()        # 等下一轮
```

## 测试

```bash
uv run python tests/smoke_test.py     # 端到端:握手/HTTP/阻塞等待/坐标换算/SSE reload/复用 tab
uv run python tests/manual_driver.py <svg> [script]   # 起 server 供手动/浏览器自动化测试,批次落盘 tests/out/batches.jsonl
```

页面调试参数:`?nosse=1` 跳过 SSE(headless 截图用);`?autotest=x,y,w,h` 载入后自动画一个矩形批注并提交(0-1 归一化坐标);`?autotest_click=x,y` 点选该点的元素并转正提交(加 `&autotest_stage=pick` 则只点选高亮,截图检查器用)。

环境变量:`SVG_ANNOTATE_NO_OPEN=1` 让 `open_svg` 不自动开浏览器(测试用)。

## 已知边界(第一版)

单例会话(一次一张图,再 open 即换图;多 Claude 会话=多 server 实例互不干扰);不导出 annotations.json(批注只经内存回传,server 重启即失,靠 Claude 上下文留底);SVG 里的外链资源不代理(open_svg 会给 warning;matplotlib 产物均为内嵌,不触发);仅 macOS `open` 打开浏览器;localhost 无鉴权。
