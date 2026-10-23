#!/usr/bin/env python3
"""Generate synthetic DSH v4 JSONL sessions for the demo and UI tests."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_ROOT = ROOT / "demo" / "dsh-sessions"


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_session(path: Path, header: dict, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps(header) + "\n")
        for event in events:
            handle.write(dumps(event) + "\n")


def event(seq: int, event_type: str, time_ms: int, data: dict, **extra) -> dict:
    value = {"type": event_type, "seq": seq, "time": time_ms, "data": data}
    value.update(extra)
    return value


def build_config_session() -> tuple[dict, list[dict]]:
    header = {
        "type": "session",
        "version": 4,
        "id": "sess_demo_config",
        "createdAt": 1791000000000,
        "cwd": r"D:\Demo\workspace",
        "isSeeded": False,
        "delegationDepth": 0,
    }
    events = [
        event(0, "session/title", 1791000000100, {"title": "Demo: DSH 会话导出"}),
        event(1, "model/selection", 1791000000200, {"provider": "demo-provider", "model": "demo-model"}),
        event(
            2,
            "user/message",
            1791000001000,
            {
                "role": "user",
                "id": "msg_user_1",
                "source": {"kind": "user"},
                "content": [{"type": "text", "text": "帮我把这个 DSH 会话导出成可读 TXT。"}],
            },
            surfaceOp="append",
        ),
        event(3, "step/start", 1791000001100, {"turn": 0, "step": 0}),
        event(
            4,
            "assistant/message",
            1791000002000,
            {
                "turn": 0,
                "step": 0,
                "message": {
                    "role": "assistant",
                    "id": "msg_assistant_1",
                    "source": {"kind": "model", "provider": "demo-provider", "model": "demo-model"},
                    "content": [
                        {"type": "reasoning", "text": "先读取会话，再归一化消息和工具调用，最后生成 TXT。"},
                        {"type": "text", "text": "我先检查会话结构，然后导出。"},
                        {
                            "type": "tool-call",
                            "id": "call_demo_1",
                            "name": "Bash",
                            "arguments": dumps({"cmd": "dsh-session-exporter scan"}),
                        },
                    ],
                },
                "usage": {"inputTokens": 1200, "outputTokens": 180, "reasoningTokens": 60},
                "finish": "tool-calls",
            },
            surfaceOp="append",
        ),
        event(
            5,
            "tool/call",
            1791000002100,
            {
                "turn": 0,
                "step": 0,
                "callId": "call_demo_1",
                "name": "Bash",
                "arguments": dumps({"cmd": "dsh-session-exporter scan"}),
            },
        ),
        event(
            6,
            "tool/result",
            1791000003000,
            {
                "turn": 0,
                "step": 0,
                "message": {
                    "role": "tool",
                    "id": "msg_tool_1",
                    "toolCallId": "call_demo_1",
                    "content": [{"type": "text", "text": "sessions: 2\nmessages: 65\ntools: 1\n"}],
                },
            },
            surfaceOp="append",
            sourceEventSeqs=[5],
        ),
        event(7, "step/end", 1791000003100, {"turn": 0, "step": 0, "reason": "tool-calls"}),
        event(
            8,
            "assistant/message",
            1791000004000,
            {
                "turn": 0,
                "step": 1,
                "message": {
                    "role": "assistant",
                    "id": "msg_assistant_2",
                    "source": {"kind": "model", "provider": "demo-provider", "model": "demo-model"},
                    "content": [
                        {"type": "text", "text": "导出完成。默认只导出 append 转录。"},
                    ],
                },
                "usage": {"inputTokens": 1600, "outputTokens": 90, "reasoningTokens": 0},
                "finish": "stop",
            },
            surfaceOp="append",
        ),
        event(
            9,
            "compaction/summary",
            1791000004100,
            {
                "trigger": "auto",
                "reason": "context_limit",
                "phase": "mid_turn",
                "preCompactTokenCount": 48000,
            },
        ),
        event(
            10,
            "assistant/message",
            1791000005000,
            {
                "turn": 0,
                "step": 2,
                "message": {
                    "role": "assistant",
                    "id": "msg_assistant_replace",
                    "source": {"kind": "model", "provider": "demo-provider", "model": "demo-model"},
                    "content": [
                        {"type": "text", "text": "这是 replace 事件：模型专用改写，不属于默认 append 转录。"},
                    ],
                },
                "usage": {"inputTokens": 900, "outputTokens": 50, "reasoningTokens": 0},
                "finish": "stop",
            },
            surfaceOp={"op": "replace", "startSeq": 4, "endSeq": 4},
        ),
    ]
    return header, events


def build_long_session() -> tuple[dict, list[dict]]:
    header = {
        "type": "session",
        "version": 4,
        "id": "sess_demo_long",
        "createdAt": 1791001000000,
        "cwd": r"D:\Demo\long",
        "isSeeded": False,
        "delegationDepth": 0,
    }
    events = [
        event(0, "session/title", 1791001000100, {"title": "Demo: 长会话分页"}),
    ]
    seq = 1
    for index in range(60):
        role = "user" if index % 2 == 0 else "assistant"
        text = (
            f"这是第 {index + 1} 条演示消息。"
            if role == "user"
            else f"这是第 {index + 1} 条助手回复，用于演示分页和流式导出。"
        )
        time_ms = 1791001001000 + index * 1000
        if role == "user":
            data = {
                "role": "user",
                "id": f"msg_long_user_{index}",
                "source": {"kind": "user"},
                "content": [{"type": "text", "text": text}],
            }
        else:
            data = {
                "turn": index // 2,
                "step": 0,
                "message": {
                    "role": "assistant",
                    "id": f"msg_long_assistant_{index}",
                    "source": {"kind": "model", "provider": "demo-provider", "model": "demo-model"},
                    "content": [{"type": "text", "text": text}],
                },
                "usage": {"inputTokens": 100 + index, "outputTokens": 20, "reasoningTokens": 0},
                "finish": "stop",
            }
        events.append(event(seq, f"{role}/message", time_ms, data, surfaceOp="append"))
        seq += 1
    return header, events


def main() -> None:
    DEMO_ROOT.mkdir(parents=True, exist_ok=True)
    header, events = build_config_session()
    write_session(
        DEMO_ROOT / "--Demo-workspace--" / "sess_demo_config" / "session.v4.jsonl",
        header,
        events,
    )
    header, events = build_long_session()
    write_session(
        DEMO_ROOT / "--Demo-long--" / "sess_demo_long" / "session.v4.jsonl",
        header,
        events,
    )
    print(f"demo sessions: {DEMO_ROOT}")


if __name__ == "__main__":
    main()
