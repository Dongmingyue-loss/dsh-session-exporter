"""DSH (DeepSeek Harness) session source.

Reads append-only `session.vN.jsonl[.zstd]` logs, normalizes surface events
into the same message/part vocabulary used by the core exporter, and caches the
result in a local SQLite database for fast pagination and export.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from core import SourceInfo, preview_part, safe_json

CACHE_SCHEMA_VERSION = 1
FILE_RE = re.compile(r"^session(?:\.v(\d+))?\.jsonl(?:\.zstd)?$")
SURFACE_TYPES = {
    "user/message",
    "assistant/message",
    "system/message",
    "developer/message",
    "tool/result",
}

try:
    if sys.version_info >= (3, 14):
        from compression import zstd as _std_zstd  # type: ignore
    else:
        _std_zstd = None
except Exception:
    _std_zstd = None

try:
    import zstandard as _zstandard  # type: ignore
except Exception:
    _zstandard = None


def decoder_available() -> bool:
    return _std_zstd is not None or _zstandard is not None


def decoder_name() -> str:
    if _std_zstd is not None:
        return "stdlib compression.zstd"
    if _zstandard is not None:
        return "zstandard"
    return "unavailable"


def to_ms(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            pass
        try:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def parse_arguments(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def iter_lines(path: Path) -> Iterator[str]:
    """Yield decoded JSONL lines, keeping the valid prefix of a torn zstd tail."""
    if path.suffix.lower() != ".zstd":
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                yield line
        return

    if _std_zstd is not None:
        try:
            with _std_zstd.ZstdFile(str(path), "rb") as handle:
                for raw in handle:
                    yield raw.decode("utf-8", "replace")
        except (EOFError, OSError):
            return
        except Exception:
            # Corrupt or torn frame: keep the successfully decoded prefix.
            return
        return

    if _zstandard is not None:
        try:
            dctx = _zstandard.ZstdDecompressor()
            with path.open("rb") as raw:
                try:
                    reader = dctx.stream_reader(raw, read_across_frames=True)
                except TypeError:
                    reader = dctx.stream_reader(raw)
                with reader:
                    text = io.TextIOWrapper(reader, encoding="utf-8", errors="replace")
                    for line in text:
                        yield line
        except Exception:
            return
        return

    raise RuntimeError(
        "zstd decoder unavailable: use Python 3.14+ or install the optional "
        "'zstandard' package (requirements-dsh.txt)"
    )


@dataclass
class SessionFile:
    id: str
    path: Path
    session_dir: Path
    project_dir: Path
    version: int
    header: dict
    mtime_ns: int
    size: int


class DshSource:
    def __init__(self, source_id: str, label: str, root: Path, cache_dir: Path):
        self.id = source_id
        self.label = label
        self.db_path = Path(root)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._stats_cache: Optional[list[dict]] = None
        self._stats_key: Optional[tuple] = None
        self._lock = threading.Lock()
        self._cache_locks: dict[str, threading.Lock] = {}
        self._cache_locks_lock = threading.Lock()

    # -- discovery --------------------------------------------------------
    def discover(self) -> list[SessionFile]:
        if not self.db_path.exists():
            return []
        sessions: dict[Path, list[tuple[int, Path]]] = {}
        for project_dir in self.db_path.iterdir():
            if not project_dir.is_dir():
                continue
            for session_dir in project_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                for file in session_dir.iterdir():
                    if not file.is_file():
                        continue
                    match = FILE_RE.match(file.name)
                    if not match:
                        continue
                    version = int(match.group(1) or 0)
                    sessions.setdefault(session_dir, []).append((version, file))

        result: list[SessionFile] = []
        for session_dir, candidates in sessions.items():
            # Highest generation wins. At the same generation, prefer zstd when
            # a decoder exists, otherwise prefer raw JSONL.
            candidates.sort(key=lambda item: (item[0], item[1].suffix.lower() == ".zstd"))
            version, path = candidates[-1]
            if path.suffix.lower() == ".zstd" and not decoder_available():
                raw = next((p for v, p in candidates if v == version and p.suffix.lower() != ".zstd"), None)
                if raw is not None:
                    path = raw
            header = {}
            try:
                for line in iter_lines(path):
                    value = safe_json(line)
                    if isinstance(value, dict) and value.get("type") == "session":
                        header = value
                        break
            except Exception:
                header = {}
            stat = path.stat()
            session_id = str(header.get("id") or session_dir.name)
            result.append(
                SessionFile(
                    id=session_id,
                    path=path,
                    session_dir=session_dir,
                    project_dir=session_dir.parent,
                    version=version,
                    header=header,
                    mtime_ns=stat.st_mtime_ns,
                    size=stat.st_size,
                )
            )
        return result

    def _find(self, session_id: str) -> Optional[SessionFile]:
        for session in self.discover():
            if session.id == session_id:
                return session
        return None

    # -- metadata scan ----------------------------------------------------
    def _scan_meta(self, session: SessionFile, mode: str = "append") -> dict:
        title = ""
        header = session.header or {}
        message_count = 0
        part_count = 0
        part_chars = 0
        tool_count = 0
        reasoning_count = 0
        time_updated = to_ms(header.get("createdAt")) or int(session.mtime_ns / 1_000_000)

        try:
            for line in iter_lines(session.path):
                event = safe_json(line)
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if event_type == "session":
                    header = event
                    continue
                event_time = to_ms(event.get("time"))
                if event_time:
                    time_updated = max(time_updated, event_time)
                if event_type == "session/title":
                    data = event.get("data") or {}
                    title = str(data.get("title") or data.get("text") or title)
                    continue
                if event_type in {"model/selection", "compaction/summary"}:
                    message_count += 1
                    part_count += 1
                    continue
                if event_type == "tool/result":
                    # Tool results are merged into the assistant tool part.
                    continue
                if event_type not in SURFACE_TYPES or not include_surface(event, mode):
                    continue
                data = event.get("data") or {}
                if event_type == "user/message":
                    message = data if isinstance(data, dict) else None
                else:
                    message = data.get("message") if isinstance(data, dict) else None
                if not isinstance(message, dict):
                    continue
                message_count += 1
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        part_count += 1
                        if block_type == "text":
                            part_chars += len(str(block.get("text") or ""))
                        elif block_type == "reasoning":
                            reasoning_count += 1
                            part_chars += len(str(block.get("text") or ""))
                        elif block_type == "tool-call":
                            tool_count += 1
                            part_chars += len(str(block.get("arguments") or ""))
                        elif block_type in {"image", "file"}:
                            part_chars += len(str(block.get("filename") or ""))
        except RuntimeError:
            raise
        except Exception:
            pass

        created = to_ms(header.get("createdAt"))
        directory = str(header.get("cwd") or session.project_dir.name.strip("-") or "")
        task_type = "subagent" if (
            header.get("origin") == "subagent"
            or int(header.get("delegationDepth") or 0) > 0
        ) else "interactive"
        return {
            "id": session.id,
            "title": title or str(header.get("title") or session.session_dir.name),
            "directory": directory,
            "time_created": created,
            "time_updated": time_updated,
            "task_type": task_type,
            "parent_id": header.get("parentSession"),
            "slug": session.session_dir.name,
            "message_count": message_count,
            "part_count": part_count,
            "message_chars": 0,
            "part_chars": part_chars,
            "tool_count": tool_count,
            "reasoning_count": reasoning_count,
            "estimated_tokens": max(1, part_chars // 2),
            "path": str(session.path),
        }

    def _stats_key_now(self) -> tuple:
        sessions = self.discover()
        return tuple(
            (str(item.path), item.mtime_ns, item.size) for item in sessions
        )

    def session_stats(self, force: bool = False) -> list[dict]:
        key = self._stats_key_now()
        with self._lock:
            if not force and self._stats_cache is not None and self._stats_key == key:
                return self._stats_cache
        stats = [self._scan_meta(item, "append") for item in self.discover()]
        stats.sort(key=lambda item: item.get("time_updated") or 0, reverse=True)
        with self._lock:
            self._stats_cache = stats
            self._stats_key = key
        return stats

    def list_sessions(
        self,
        query: str = "",
        sort: str = "updated",
        offset: int = 0,
        limit: int = 500,
    ) -> tuple[list[dict], int]:
        stats = self.session_stats()
        if query:
            needle = query.lower()
            stats = [
                item for item in stats
                if needle in str(item.get("title", "")).lower()
                or needle in str(item.get("directory", "")).lower()
                or needle in str(item.get("id", "")).lower()
            ]
        sorters = {
            "updated": lambda item: item.get("time_updated") or 0,
            "created": lambda item: item.get("time_created") or 0,
            "messages": lambda item: item.get("message_count") or 0,
            "chars": lambda item: item.get("part_chars") or 0,
            "title": lambda item: str(item.get("title", "")).lower(),
        }
        stats.sort(key=sorters.get(sort, sorters["updated"]), reverse=sort != "title")
        return stats[offset : offset + limit], len(stats)

    # -- cache ------------------------------------------------------------
    def _cache_key(self, session: SessionFile, mode: str) -> str:
        raw = (
            f"{session.path.resolve()}|{session.mtime_ns}|{session.size}|"
            f"{mode}|{CACHE_SCHEMA_VERSION}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_lock(self, key: str) -> threading.Lock:
        with self._cache_locks_lock:
            return self._cache_locks.setdefault(key, threading.Lock())

    def _cache_path(self, session: SessionFile, mode: str) -> Path:
        return self.cache_dir / f"{self._cache_key(session, mode)}.sqlite"

    def ensure_cache(self, session: SessionFile, mode: str = "append") -> Path:
        cache_path = self._cache_path(session, mode)
        if cache_path.exists():
            return cache_path
        lock = self._cache_lock(cache_path.name)
        with lock:
            if cache_path.exists():
                return cache_path
            temp = cache_path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
            if temp.exists():
                temp.unlink()
            con = sqlite3.connect(temp)
            con.row_factory = sqlite3.Row
            try:
                create_cache_schema(con)
                build_cache(con, session, mode)
                con.commit()
            except Exception:
                con.close()
                if temp.exists():
                    temp.unlink()
                raise
            else:
                con.close()
            os.replace(temp, cache_path)
        return cache_path

    def connect_session(self, session_id: str, surface_mode: str = "append"):
        session = self._find(session_id)
        if not session:
            raise ValueError(f"session not found: {session_id}")
        cache_path = self.ensure_cache(session, surface_mode)
        uri = f"{cache_path.resolve().as_uri()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = 1")
        con.execute("PRAGMA busy_timeout = 30000")
        return con

    # -- API-compatible methods -------------------------------------------
    def validate(self) -> SourceInfo:
        info = SourceInfo(self.id, self.label, self.db_path)
        if not self.db_path.exists():
            info.error = "sessions directory not found"
            return info
        try:
            sessions = self.discover()
            info.exists = True
            info.session_count = len(sessions)
            info.size = sum(item.size for item in sessions)
            info.mtime = max((item.mtime_ns / 1_000_000_000 for item in sessions), default=0)
            if sessions and not decoder_available() and any(
                item.path.suffix.lower() == ".zstd" for item in sessions
            ):
                info.error = (
                    "zstd decoder unavailable: use Python 3.14+ or install "
                    "the optional 'zstandard' package (requirements-dsh.txt)"
                )
        except Exception as exc:  # noqa: BLE001
            info.error = str(exc)
        return info

    def get_session(self, session_id: str, surface_mode: str = "append") -> Optional[dict]:
        stats = {item["id"]: item for item in self.session_stats()}
        base = stats.get(session_id)
        if not base:
            return None
        result = dict(base)
        con = self.connect_session(session_id, surface_mode)
        try:
            counts: dict[str, dict] = {}
            for row in con.execute(
                "SELECT data FROM part WHERE session_id = ?", (session_id,)
            ):
                data = safe_json(row["data"]) or {}
                part_type = data.get("type") if isinstance(data, dict) else "unknown"
                entry = counts.setdefault(
                    part_type or "unknown",
                    {"type": part_type or "unknown", "count": 0, "chars": 0},
                )
                entry["count"] += 1
                entry["chars"] += len(row["data"] or "")
            result["part_types"] = sorted(
                counts.values(), key=lambda item: item["chars"], reverse=True
            )
            counts_row = con.execute(
                """
                SELECT COUNT(*) AS message_count,
                       COALESCE(SUM(LENGTH(data)), 0) AS message_chars
                FROM message WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            part_row = con.execute(
                """
                SELECT COUNT(*) AS part_count,
                       COALESCE(SUM(LENGTH(data)), 0) AS part_chars
                FROM part WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            result["message_count"] = int(counts_row["message_count"] or 0)
            result["part_count"] = int(part_row["part_count"] or 0)
            result["part_chars"] = int(part_row["part_chars"] or 0)
            result["estimated_tokens"] = max(1, result["part_chars"] // 2)
            row = con.execute(
                """
                SELECT MIN(time_created) AS first_message_at,
                       MAX(time_created) AS last_message_at
                FROM message WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            result["first_message_at"] = row["first_message_at"] if row else None
            result["last_message_at"] = row["last_message_at"] if row else None
        finally:
            con.close()
        return result

    def get_messages(
        self,
        session_id: str,
        offset: int = 0,
        limit: int = 20,
        max_chars: int = 8000,
        surface_mode: str = "append",
    ) -> dict:
        limit = max(1, min(limit, 100))
        max_chars = max(0, min(max_chars, 200_000))
        con = self.connect_session(session_id, surface_mode)
        try:
            total = con.execute(
                "SELECT COUNT(*) FROM message WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            rows = con.execute(
                """
                SELECT id, sequence, time_created, time_updated, data
                FROM message WHERE session_id = ?
                ORDER BY sequence, id LIMIT ? OFFSET ?
                """,
                (session_id, limit, offset),
            ).fetchall()
            message_ids = [row["id"] for row in rows]
            parts: dict[str, list[dict]] = {message_id: [] for message_id in message_ids}
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                for part in con.execute(
                    f"""
                    SELECT id, message_id, sequence, time_created, data
                    FROM part WHERE message_id IN ({placeholders})
                    ORDER BY sequence, id
                    """,
                    message_ids,
                ):
                    parts.setdefault(part["message_id"], []).append(
                        preview_part(part, max_chars)
                    )
            messages = []
            for row in rows:
                data = safe_json(row["data"]) or {}
                messages.append(
                    {
                        "id": row["id"],
                        "sequence": row["sequence"],
                        "time_created": row["time_created"],
                        "time_updated": row["time_updated"],
                        "role": data.get("role") or "unknown",
                        "modelID": data.get("modelID"),
                        "providerID": data.get("providerID"),
                        "variant": data.get("variant"),
                        "mode": data.get("mode"),
                        "agent": data.get("agent"),
                        "tokens": data.get("tokens"),
                        "finish": data.get("finish"),
                        "summary": data.get("summary"),
                        "parts": parts.get(row["id"], []),
                    }
                )
        finally:
            con.close()
        return {
            "messages": messages,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(messages) < total,
        }

    def get_part(
        self,
        part_id: str,
        max_chars: int = 2_000_000,
        surface_mode: str = "append",
    ) -> Optional[dict]:
        parts = part_id.split(":", 2)
        if len(parts) != 3 or parts[0] != "dsh":
            return None
        session_id = parts[1]
        max_chars = max(0, min(max_chars, 5_000_000))
        modes = [surface_mode] + [
            mode for mode in ("append", "append+replace", "replace")
            if mode != surface_mode
        ]
        for mode in modes:
            con = self.connect_session(session_id, mode)
            try:
                row = con.execute(
                    "SELECT id, message_id, sequence, time_created, data FROM part WHERE id = ?",
                    (part_id,),
                ).fetchone()
            finally:
                con.close()
            if row:
                return preview_part(row, max_chars)
        return None


def include_surface(event: dict, mode: str) -> bool:
    op = event.get("surfaceOp")
    is_replace = isinstance(op, dict) and op.get("op") == "replace"
    is_append = op == "append" or op is None
    if mode == "replace":
        return is_replace
    if mode == "append+replace":
        return is_append or is_replace
    return is_append


def create_cache_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            title TEXT,
            directory TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            task_type TEXT,
            parent_id TEXT,
            slug TEXT
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT,
            sequence INTEGER
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT,
            sequence INTEGER
        );
        CREATE INDEX message_session_sequence_idx ON message(session_id, sequence);
        CREATE INDEX part_message_sequence_idx ON part(message_id, sequence);
        """
    )


def content_parts(content) -> tuple[list[dict], list[tuple[str, dict]]]:
    """Return generic parts and tool-call metadata from a DSH content list."""
    parts: list[dict] = []
    tools: list[tuple[str, dict]] = []
    if isinstance(content, str):
        return [{"type": "text", "text": content}], []
    if not isinstance(content, list):
        return parts, tools
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append({"type": "text", "text": str(block.get("text") or "")})
        elif block_type == "reasoning":
            parts.append({"type": "reasoning", "text": str(block.get("text") or "")})
        elif block_type == "tool-call":
            call_id = str(block.get("id") or block.get("callId") or "")
            tool_part = {
                "type": "tool",
                "tool": block.get("name") or "unknown",
                "callID": call_id,
                "state": {
                    "status": "pending",
                    "input": parse_arguments(block.get("arguments")),
                    "output": "",
                    "error": "",
                    "metadata": {},
                },
            }
            parts.append(tool_part)
            tools.append((call_id, tool_part))
        elif block_type in {"image", "file"}:
            attachment = block.get("attachment") if isinstance(block.get("attachment"), dict) else {}
            parts.append(
                {
                    "type": "file",
                    "filename": attachment.get("filename") or block.get("filename") or block_type,
                    "mime": attachment.get("mime") or block.get("mime") or "",
                    "url": attachment.get("id") or attachment.get("path") or block.get("url") or "",
                    "metadata": {
                        "sizeBytes": attachment.get("sizeBytes") or attachment.get("size"),
                        "attachmentId": attachment.get("id"),
                    },
                }
            )
    return parts, tools


def map_usage(usage) -> dict:
    if not isinstance(usage, dict):
        return {}
    return {
        "input": usage.get("inputTokens", usage.get("input", 0)),
        "output": usage.get("outputTokens", usage.get("output", 0)),
        "reasoning": usage.get("reasoningTokens", usage.get("reasoning", 0)),
    }


def build_cache(con: sqlite3.Connection, session: SessionFile, mode: str) -> None:
    header = session.header or {}
    title = str(header.get("title") or "")
    created = to_ms(header.get("createdAt"))
    updated = created or int(session.mtime_ns / 1_000_000)
    directory = str(header.get("cwd") or session.project_dir.name.strip("-") or "")
    task_type = "subagent" if (
        header.get("origin") == "subagent"
        or int(header.get("delegationDepth") or 0) > 0
    ) else "interactive"

    message_seq = 0
    part_seq = 0
    tool_calls: dict[str, dict] = {}
    tool_parts: dict[str, str] = {}

    def insert_message(role: str, event_time: int, data: dict, parts: list[dict]) -> tuple[str, list[str]]:
        nonlocal message_seq, part_seq
        message_id = f"dsh:{session.id}:m{message_seq}"
        data = {"role": role, "time": {"created": event_time}, "agent": "dsh", **data}
        con.execute(
            "INSERT INTO message VALUES (?,?,?,?,?,?)",
            (message_id, session.id, event_time, event_time, json_dumps(data), message_seq),
        )
        message_seq += 1
        part_ids = []
        for index, part in enumerate(parts):
            part_id = f"dsh:{session.id}:p{part_seq}"
            part_seq += 1
            con.execute(
                "INSERT INTO part VALUES (?,?,?,?,?,?,?)",
                (part_id, message_id, session.id, event_time, event_time, json_dumps(part), index),
            )
            part_ids.append(part_id)
        return message_id, part_ids

    def update_tool(call_id: str, output: str, error: str) -> None:
        part_id = tool_parts.get(call_id)
        if not part_id:
            return
        row = con.execute("SELECT data FROM part WHERE id = ?", (part_id,)).fetchone()
        if not row:
            return
        data = safe_json(row["data"]) or {}
        state = data.setdefault("state", {})
        state["status"] = "error" if error else "completed"
        state["output"] = output
        state["error"] = error
        con.execute("UPDATE part SET data = ? WHERE id = ?", (json_dumps(data), part_id))

    for line in iter_lines(session.path):
        event = safe_json(line)
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "session":
            continue
        event_time = to_ms(event.get("time")) or updated
        updated = max(updated, event_time)
        if event_type == "session/title":
            data = event.get("data") or {}
            title = str(data.get("title") or data.get("text") or title)
            continue

        if event_type == "tool/call":
            data = event.get("data") or {}
            call_id = str(data.get("callId") or data.get("id") or "")
            if call_id:
                tool_calls[call_id] = {
                    "name": data.get("name") or "unknown",
                    "arguments": data.get("arguments"),
                }
                part_id = tool_parts.get(call_id)
                if part_id:
                    row = con.execute("SELECT data FROM part WHERE id = ?", (part_id,)).fetchone()
                    if row:
                        part_data = safe_json(row["data"]) or {}
                        part_data.setdefault("state", {})["input"] = parse_arguments(data.get("arguments"))
                        con.execute("UPDATE part SET data = ? WHERE id = ?", (json_dumps(part_data), part_id))
            continue

        if event_type in SURFACE_TYPES:
            if not include_surface(event, mode):
                continue
            data = event.get("data") or {}
            if event_type == "user/message":
                message = data if isinstance(data, dict) else None
            else:
                message = data.get("message") if isinstance(data, dict) else None
            if not isinstance(message, dict):
                continue

            if event_type == "tool/result":
                call_id = str(message.get("toolCallId") or "")
                if not call_id and isinstance(event.get("sourceEventSeqs"), list) and event["sourceEventSeqs"]:
                    call_id = str(event["sourceEventSeqs"][0])
                content = message.get("content")
                output = ""
                if isinstance(content, list):
                    output = "\n".join(
                        str(block.get("text") or "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                elif isinstance(content, str):
                    output = content
                error = str(data.get("error") or "")
                if call_id and call_id in tool_parts:
                    update_tool(call_id, output, error)
                    continue
                call = tool_calls.get(call_id, {})
                part = {
                    "type": "tool",
                    "tool": call.get("name") or "tool",
                    "callID": call_id,
                    "state": {
                        "status": "error" if error or message.get("isError") else "completed",
                        "input": parse_arguments(call.get("arguments")),
                        "output": output,
                        "error": error,
                        "metadata": {},
                    },
                }
                insert_message("tool", event_time, {}, [part])
                continue

            if event_type == "user/message":
                parts, _ = content_parts(message.get("content"))
                if parts:
                    insert_message("user", event_time, {"source": message.get("source")}, parts)
                continue

            if event_type == "assistant/message":
                parts, tools = content_parts(message.get("content"))
                if not parts:
                    continue
                source = message.get("source") if isinstance(message.get("source"), dict) else {}
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                message_id, part_ids = insert_message(
                    "assistant",
                    event_time,
                    {
                        "providerID": source.get("provider"),
                        "modelID": source.get("model"),
                        "tokens": map_usage(usage),
                        "finish": data.get("finish"),
                    },
                    parts,
                )
                for index, part in enumerate(parts):
                    if part.get("type") != "tool" or not part.get("callID"):
                        continue
                    call_id = part["callID"]
                    part_id = part_ids[index]
                    tool_parts[call_id] = part_id
                    call = tool_calls.get(call_id)
                    if call and call.get("arguments") is not None:
                        part["state"]["input"] = parse_arguments(call.get("arguments"))
                        con.execute(
                            "UPDATE part SET data = ? WHERE id = ?",
                            (json_dumps(part), part_id),
                        )
                continue

            if event_type in {"system/message", "developer/message"}:
                parts, _ = content_parts(message.get("content"))
                if parts:
                    insert_message(event_type.split("/")[0], event_time, {}, parts)
                continue

        if event_type == "model/selection":
            data = event.get("data") or {}
            model = data.get("model") or data.get("modelId") or ""
            provider = data.get("provider") or data.get("providerId") or ""
            label = f"{provider}/{model}" if provider or model else "model"
            insert_message(
                "system",
                event_time,
                {},
                [{
                    "type": "timeline",
                    "timelineType": "model_change",
                    "toModel": {"label": label},
                    "status": "completed",
                }],
            )
            continue

        if event_type == "compaction/summary":
            data = event.get("data") or {}
            insert_message(
                "system",
                event_time,
                {},
                [{
                    "type": "compaction",
                    "trigger": data.get("trigger"),
                    "compactReason": data.get("reason") or data.get("compactReason"),
                    "phase": data.get("phase"),
                    "preCompactTokenCount": data.get("preCompactTokenCount"),
                    "timelineStatus": "completed",
                }],
            )
            continue

        if event_type in {"step/start", "step/end"}:
            # Step events are log-only metadata; keep the transcript focused on
            # messages. They can be added later behind an explicit option.
            continue

        if event_type in {"assistant/attempt", "turn/end"}:
            data = event.get("data") or {}
            error = data.get("error") or data.get("reason") or ""
            if error and isinstance(error, dict):
                error = error.get("message") or error.get("kind") or str(error)
            if error:
                insert_message(
                    "assistant",
                    event_time,
                    {},
                    [{"type": "notice", "level": "error", "text": str(error)}],
                )

    con.execute(
        """
        INSERT OR REPLACE INTO session VALUES (?,?,?,?,?,?,?,?)
        """,
        (session.id, title or session.session_dir.name, directory, created, updated, task_type, header.get("parentSession"), session.session_dir.name),
    )
