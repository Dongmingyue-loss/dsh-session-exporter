# 架构

`dsh-session-exporter` 是一个本地 Web 应用：一个通用导出 core + 一个 DSH source adapter + 一个 GUI。

```text
app.py                  HTTP 服务、token、静态文件、API、任务控制
core.py                 ExportOptions / RotatingWriter / ExportJob / Exporter / 格式化
sources/dsh.py          DSH 发现、zstd 解码、事件归一化、SQLite 缓存
static/                 index.html / app.js / styles.css
demo/dsh-sessions/      合成 DSH v4 会话
scripts/                demo 生成、打包、密钥扫描
test/                   单元、API、Playwright UI
docs/                   格式、架构、截图
```

## 分层

### core.py

只依赖 `SessionSource` 协议：

```text
validate() -> SourceInfo
session_stats() -> list[dict]
list_sessions(query, sort, offset, limit) -> (sessions, total)
get_messages(session_id, offset, limit, max_chars, surface_mode) -> dict
get_part(part_id, max_chars) -> dict | None
connect_session(session_id, surface_mode) -> sqlite3.Connection
```

core 不关心 DSH 事件、zstd 或文件布局。它负责：

- 导出选项与校验
- 流式写文件、按大小拆分
- 可读 TXT / 规范化 JSONL 格式化
- 导出任务状态、进度、取消、下载
- 预览分页

### sources/dsh.py

只负责 DSH：

- 发现 `session.vN.jsonl[.zstd]`
- 选择最高 generation
- 解码 raw / zstd
- 把 surface 事件归一化为 `message` / `part`
- 构建并复用 SQLite 缓存
- 提供与 core 兼容的查询接口

### app.py

组合 source 与 GUI：

- 只绑定 loopback
- 随机 token，除 `/api/health` 外全部鉴权
- 静态文件、API、导出任务
- 自动打开浏览器
- 输出目录、缓存目录、Demo 模式

### static/

无构建步骤的原生 HTML/CSS/JS：

- 来源 tab（DSH / Demo）
- 会话搜索、排序、分页
- 消息分页预览
- 导出选项、surface mode、自定义角色名
- 导出预览弹窗、进度、下载、打开目录

## 数据流

```text
DSH session.vN.jsonl[.zstd]
        │
        ▼
sources/dsh.py  discover + decode + normalize
        │
        ▼
SQLite cache (exporter-owned)
        │
        ├── /api/sessions   -> 列表
        ├── /api/messages   -> 分页预览
        ├── /api/preview    -> 导出预览
        └── Exporter        -> TXT / JSONL
```

## 为什么不是一个巨大的 Source 类

- core 可以独立测试，不依赖 DSH 格式
- DSH 格式变化只影响 `sources/dsh.py`
- 未来 ZCode / DSH 架构分化时，可以拆成独立包或独立入口
- 当前仍是一个仓库、一个 GUI，发布和安装简单

## 并发与一致性

- 同一缓存 key 使用进程内锁合并构建
- 构建写临时 SQLite，成功后 `os.replace` 原子替换
- 查询连接只读、`query_only=1`
- 导出在后台线程运行，支持取消和进度
- 服务只处理本机请求；不提供跨域 API

## 测试边界

- `test/test_dsh_source.py`：解析、归一化、三种 surface 模式、工具合并、缓存复用、zstd
- `test/test_server.py`：健康检查、来源、会话、消息、预览、导出、下载、静态资源
- `test/ui_smoke.py`：Playwright 打开 GUI、分页预览、改角色名、导出
- CI：三平台 × Python 3.9 / 3.12 / 3.14；三平台 Playwright
