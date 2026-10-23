#!/usr/bin/env python3
"""HTTP API smoke tests for the DSH web GUI."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import Handler, Server  # noqa: E402
from core import Exporter  # noqa: E402
from sources.dsh import DshSource  # noqa: E402

DEMO = ROOT / "demo" / "dsh-sessions"


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-server-test-")
        self.root = Path(self.temp.name)
        self.source = DshSource("demo", "Demo", DEMO, self.root / "cache")
        self.exporter = Exporter({"demo": self.source}, self.root / "out")
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.service = SimpleNamespace(
            sources={"demo": self.source},
            exporter=self.exporter,
            token="testtoken",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, method="GET", payload=None, token="testtoken"):
        data = None
        headers = {}
        if token:
            headers["X-Auth-Token"] = token
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        with self.opener.open(request, timeout=20) as response:
            return response.status, response.headers, response.read()

    def test_health_and_sources(self):
        status, _headers, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        status, _headers, body = self.request("/api/sources")
        self.assertEqual(status, 200)
        sources = json.loads(body)["sources"]
        self.assertEqual(sources[0]["id"], "demo")
        self.assertEqual(sources[0]["session_count"], 2)

    def test_sessions_and_messages(self):
        status, _headers, body = self.request("/api/sessions?source=demo")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["total"], 2)

        status, _headers, body = self.request(
            "/api/messages?source=demo&id=sess_demo_config&offset=0&limit=20"
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["total"], 5)

    def test_surface_mode_api(self):
        status, _headers, body = self.request(
            "/api/messages?source=demo&id=sess_demo_config&offset=0&limit=20&surface_mode=append%2Breplace"
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], 6)

        status, _headers, body = self.request(
            "/api/session?source=demo&id=sess_demo_config&surface_mode=replace"
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["session"]["message_count"], 3)

        status, _headers, body = self.request(
            "/api/messages?source=demo&id=sess_demo_config&offset=0&limit=20&surface_mode=replace"
        )
        messages = json.loads(body)["messages"]
        part_id = next(
            part["id"]
            for message in messages
            for part in message["parts"]
            if part["type"] == "text"
        )
        status, _headers, body = self.request(
            f"/api/part?source=demo&id={part_id}&surface_mode=replace"
        )
        self.assertEqual(status, 200)
        self.assertIn("replace", json.loads(body)["part"]["text"])

    def test_preview(self):
        status, _headers, body = self.request(
            "/api/preview",
            method="POST",
            payload={
                "source": "demo",
                "session_id": "sess_demo_config",
                "format": "readable",
                "preview_messages": 30,
                "preview_offset": 0,
                "preview_chars": 200000,
                "surface_mode": "append",
                "user_label": "我",
                "agent_label": "助手",
            },
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("[tool] Bash", data["text"])
        self.assertIn("[000001] 我", data["text"])
        self.assertNotIn("replace 事件", data["text"])

    def test_export_and_download(self):
        status, _headers, body = self.request(
            "/api/export",
            method="POST",
            payload={
                "source": "demo",
                "session_ids": ["sess_demo_config"],
                "format": "readable",
                "output_dir": str(self.root / "out"),
                "split_mb": 0,
                "utf8_bom": False,
                "surface_mode": "append",
            },
        )
        self.assertEqual(status, 200)
        job = json.loads(body)["job"]
        current = None
        deadline = time.time() + 30
        while time.time() < deadline:
            status, _headers, body = self.request(f"/api/job?id={job['id']}")
            self.assertEqual(status, 200)
            current = json.loads(body)["job"]
            if current["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(0.05)
        self.assertEqual(current["status"], "done")
        self.assertTrue(current["download_path"])

        status, headers, body = self.request(
            f"/api/job/download?id={job['id']}&token=testtoken", token=None
        )
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("SESSION: Demo: DSH".encode("utf-8"), body)

    def test_static_assets(self):
        for path, marker in [
            ("/", "DSH 会话导出器"),
            ("/static/app.js", "previewExport"),
            ("/static/styles.css", "--cyan"),
        ]:
            status, _headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(marker.encode("utf-8"), body)


if __name__ == "__main__":
    unittest.main()
