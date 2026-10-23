# DSH 会话格式、归一化与缓存

本文记录 `dsh-session-exporter` 读取的 DSH 持久化格式，以及工具如何把它转换成统一的 `message` / `part` 模型。

## 1. 文件位置

DSH / DSH Desktop Beta 默认把会话放在：

```text
~/.dsh-beta/sessions/
  --<normalized-cwd>--/
    <session-id>/
      session.v4.jsonl.zstd
```

也支持 `~/.dsh/sessions`、`DSH_SESSIONS_DIR` 和 `--dsh-sessions`。

同一会话目录可能同时存在多个 generation：

```text
session.jsonl
session.v1.jsonl
session.v4.jsonl.zstd
```

工具选择最高的 `N`。同一 generation 同时存在 `.zstd` 与原始 `.jsonl` 时：

- 有 zstd 解码器：优先 `.zstd`
- 没有解码器：回退到同 generation 的 `.jsonl`

## 2. 事件流

文件是 append-only JSONL。第一行是 header：

```json
{"type":"session","version":4,"id":"sess_...","createdAt":"2026-10-03T12:00:00Z","cwd":"D:\work"}
```

后续每行是一个事件信封：

```json
{"type":"assistant/message","seq":8,"time":1791000004000,"data":{...},"surfaceOp":"append"}
```

常见字段：

| 字段 | 含义 |
|---|---|
| `type` | 事件类型 |
| `seq` | 会话内单调序号 |
| `time` | 毫秒时间戳 |
| `data` | 事件负载 |
| `surfaceOp` | `"append"` 或 `{"op":"replace","startSeq":..,"endSeq":..}` |
| `sourceEventSeqs` | 该 surface 事件引用的原始日志序号 |

## 3. Surface 事件

只有以下事件进入“人类可读转录”：

- `user/message`
- `assistant/message`
- `system/message`
- `developer/message`
- `tool/result`

`surfaceOp` 语义：

| 值 | 含义 |
|---|---|
| 缺省 / `"append"` | 持久追加，属于用户实际看到的转录 |
| `{"op":"replace",...}` | 模型专用改写，可能覆盖此前内容 |

导出模式：

| 模式 | 包含 |
|---|---|
| `append` | 仅 append |
| `append+replace` | append + replace |
| `replace` | 仅 replace |

## 4. 归一化映射

| DSH 事件 / 内容块 | 导出消息 role | part 类型 |
|---|---|---|
| `user/message` + text | `user` | `text` |
| `assistant/message` + text | `assistant` | `text` |
| assistant `reasoning` | `assistant` | `reasoning` |
| assistant `tool-call` | `assistant` | `tool`（pending） |
| `tool/call` | 合并进上一条 tool part | `tool` 的 input |
| `tool/result` | 更新对应 tool part | `tool` 的 output/status |
| `system/message` / `developer/message` | `system` / `developer` | `text` |
| image / file block | 原消息 | `file`（仅引用） |
| `model/selection` | 合成 `system` | `timeline` |
| `compaction/summary` | 合成 `system` | `compaction` |
| `assistant/attempt` / `turn/end` 错误 | 合成 `assistant` | `notice` |
| `session/title` | 写入会话标题 | 不产生消息 |
| `step/start` / `step/end` | 当前跳过 | 不产生消息 |

未识别的日志事件不会进入可读 TXT；需要完整原始事件时使用 DSH 官方 `/export`。

## 5. Zstd

`.jsonl.zstd` 通常是多个独立 zstd frame 顺序拼接。

- Python 3.14+：`compression.zstd.ZstdFile`
- Python 3.9-3.13：可选 `zstandard`，使用 `stream_reader(..., read_across_frames=True)`
- 尾部 frame 撕裂：保留已成功解码的完整前缀，不因最后一帧损坏而丢弃整个会话

没有解码器时，DSH 来源会显示明确错误，不影响 Demo 或其他来源。

## 6. SQLite 缓存

首次访问会话时，工具流式解码、归一化，并写入 exporter 自己的缓存目录：

```text
<cache-dir>/<sha256>.sqlite
```

缓存表：

- `session`：id、title、directory、时间、task_type、parent_id、slug
- `message`：id、session_id、时间、JSON data、sequence
- `part`：id、message_id、session_id、时间、JSON data、sequence

缓存 key：

```text
realpath + mtime_ns + size + surface_mode + schema_version
```

因此：

- 源文件变化会生成新缓存
- 不同 surface mode 各自缓存
- 缓存 schema 升级后旧缓存不会被误用
- 构建先写临时文件，成功后原子 rename

缓存目录：

| 系统 | 路径 |
|---|---|
| Windows | `%LOCALAPPDATA%\dsh-session-exporter\cache` |
| macOS | `~/Library/Caches/dsh-session-exporter` |
| Linux | `$XDG_CACHE_HOME/dsh-session-exporter` 或 `~/.cache/dsh-session-exporter` |

缓存可以随时删除；下次打开会话会重新生成。

## 7. 已知边界

- `step/*` 目前不进入可读转录，避免长会话被大量机械事件淹没
- 附件只保留引用和元数据，不读取二进制
- 子会话（subagent）按独立 session 展示，`parent_id` 来自 header
- DSH 格式升级时，优先更新 `sources/dsh.py` 与 `CACHE_SCHEMA_VERSION`
