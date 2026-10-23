#!/usr/bin/env python3
"""Tests for the DSH JSONL/Zstd session source."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import ExportOptions, Exporter  # noqa: E402
from sources.dsh import DshSource, decoder_available  # noqa: E402

DEMO = ROOT / "demo" / "dsh-sessions"


def wait_job(job, timeout=30):
    deadline = time.time() + timeout
    while job.status in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.05)
    if job.status in {"queued", "running"}:
        raise TimeoutError("export job did not finish")
    if job.status == "error":
        raise RuntimeError(job.error)
    return job


class DshSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-source-test-")
        self.cache = Path(self.temp.name) / "cache"
        self.source = DshSource("demo", "Demo", DEMO, self.cache)
        self.exporter = Exporter({"demo": self.source}, Path(self.temp.name) / "out")

    def tearDown(self):
        self.temp.cleanup()

    def test_stats(self):
        info = self.source.validate()
        self.assertTrue(info.exists)
        self.assertEqual(info.session_count, 2)
        stats = {item["id"]: item for item in self.source.session_stats()}
        self.assertEqual(stats["sess_demo_config"]["message_count"], 5)
        self.assertEqual(stats["sess_demo_long"]["message_count"], 60)
        self.assertIn("DSH", stats["sess_demo_config"]["title"])

    def test_surface_modes(self):
        append = self.source.get_messages("sess_demo_config", 0, 20, 1000, "append")
        self.assertEqual(append["total"], 5)
        append_text = "\n".join(
            part.get("text", "")
            for message in append["messages"]
            for part in message["parts"]
        )
        self.assertNotIn("replace 事件", append_text)

        both = self.source.get_messages("sess_demo_config", 0, 20, 1000, "append+replace")
        self.assertEqual(both["total"], 6)
        both_text = "\n".join(
            part.get("text", "")
            for message in both["messages"]
            for part in message["parts"]
        )
        self.assertIn("replace 事件", both_text)

        replace = self.source.get_messages("sess_demo_config", 0, 20, 1000, "replace")
        replace_text = "\n".join(
            part.get("text", "")
            for message in replace["messages"]
            for part in message["parts"]
        )
        self.assertIn("replace 事件", replace_text)

    def test_tool_part(self):
        result = self.source.get_messages("sess_demo_config", 0, 20, 1000, "append")
        tool_part = next(
            part
            for message in result["messages"]
            for part in message["parts"]
            if part["type"] == "tool"
        )
        self.assertEqual(tool_part["tool"], "Bash")
        self.assertEqual(tool_part["status"], "completed")
        self.assertIn("sessions: 2", tool_part["output"])
        full = self.source.get_part(tool_part["id"])
        self.assertIn("sessions: 2", full["output"])

    def test_preview_and_export(self):
        options = ExportOptions(
            source_id="demo",
            session_ids=["sess_demo_config"],
            output_dir=str(Path(self.temp.name) / "out"),
            format="readable",
            split_mb=0,
            surface_mode="append",
        )
        preview = self.exporter.preview(options, "sess_demo_config", 30, 200000)
        self.assertIn("[tool] Bash", preview["text"])
        self.assertNotIn("replace 事件", preview["text"])

        job = self.exporter.start(options)
        wait_job(job)
        text = Path(job.files[0]).read_text(encoding="utf-8")
        self.assertIn("SESSION: Demo: DSH 会话导出", text)
        self.assertIn("[tool] Bash", text)
        self.assertIn("sessions: 2", text)
        self.assertNotIn("replace 事件", text)

    def test_cache_reuse(self):
        session = self.source._find("sess_demo_config")
        first = self.source.ensure_cache(session, "append")
        second = self.source.ensure_cache(session, "append")
        self.assertEqual(first, second)
        self.assertTrue(first.exists())

    @unittest.skipUnless(decoder_available(), "zstd decoder not available")
    def test_zstd_session(self):
        root = Path(self.temp.name) / "zstd-sessions"
        target = root / "--demo--" / "sess_zstd"
        target.mkdir(parents=True)
        raw = (DEMO / "--Demo-workspace--" / "sess_demo_config" / "session.v4.jsonl").read_bytes()

        if sys.version_info >= (3, 14):
            from compression import zstd
            compressed = zstd.compress(raw)
        else:
            import zstandard as zstd
            compressed = zstd.ZstdCompressor().compress(raw)
        (target / "session.v4.jsonl.zstd").write_bytes(compressed)

        source = DshSource("zstd", "Zstd", root, Path(self.temp.name) / "zstd-cache")
        self.assertEqual(source.validate().session_count, 1)
        result = source.get_messages("sess_demo_config", 0, 20, 1000, "append")
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["messages"][1]["role"], "user")


if __name__ == "__main__":
    unittest.main()
