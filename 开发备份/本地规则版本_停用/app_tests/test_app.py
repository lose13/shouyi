"""Exercise the real HTTP handler using in-memory streams, without opening sockets."""
import io
import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
from model import ModelClient


class Socket:
    def __init__(self, raw): self.raw, self.output = raw, bytearray()
    def makefile(self, mode, *args): return io.BytesIO(self.raw)
    def sendall(self, data): self.output.extend(data)


def request(path, body=None, headers=None):
    method = "POST" if body is not None else "GET"
    payload = json.dumps(body).encode() if body is not None else b""
    fields = {"Host": "127.0.0.1:8137", "Connection": "close"}
    if body is not None: fields.update({"X-Shouyi-Request": "1", "Content-Type": "application/json", "Content-Length": str(len(payload))})
    fields.update(headers or {})
    raw = (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n").encode() + payload
    sock = Socket(raw)
    app.Handler(sock, ("127.0.0.1", 12345), SimpleNamespace(server_name="127.0.0.1", server_port=8137))
    response = bytes(sock.output)
    header, content = response.split(b"\r\n\r\n", 1)
    status = int(header.split(b" ")[1])
    return status, header.decode(), content


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = (app.ROOT / ".test-tmp").resolve()
        self.temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.temp_root)
        self.patches = [patch.object(app, "STATE_PATH", Path(self.temp.name) / "workspaces.json"),
                        patch.object(app, "WORKSPACES", {"official": app.run_baseline()}),
                        patch.object(app, "MODEL", ModelClient(app.RULES))]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        # Verify the recursive cleanup stays inside this project's named test directory.
        self.assertEqual(Path(self.temp.name).resolve().parent, self.temp_root)
        self.temp.cleanup()

    def test_bootstrap_has_actual_snapshots(self):
        status, headers, body = request("/api/bootstrap")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["workspace"]["turns"]), 28)
        self.assertEqual(data["workspace"]["turns"][6]["snapshot"][-1]["owner_id"], "LIMING")
        self.assertIn("Content-Security-Policy", headers)

    def test_static_source_files_and_state_not_public(self):
        for path in ("/app.py", "/model.py", "/.state/workspaces.json", "/data/official.json", "/../memory-rules.md"):
            with self.subTest(path=path): self.assertEqual(request(path)[0], 404)
        for path in ("/", "/style.css", "/app.js", "/favicon.svg"):
            with self.subTest(path=path): self.assertEqual(request(path)[0], 200)

    def test_feed_persist_export_and_reload(self):
        status, _, body = request("/api/new", {"title": "测试空间"})
        self.assertEqual(status, 200)
        wid = json.loads(body)["workspace"]["id"]
        status, _, body = request("/api/feed", {"id": wid, "text": "我在南京青山科技做工程师。", "role": "owner"})
        self.assertEqual(status, 200)
        self.assertTrue(app.STATE_PATH.exists())
        self.assertEqual(len(json.loads(body)["workspace"]["turns"]), 1)
        status, headers, body = request("/api/export?id=" + wid + "&turn=1")
        self.assertEqual(status, 200)
        self.assertIn("南京青山科技", body.decode())
        self.assertIn("memory-LIMING.md", headers)
        app.WORKSPACES.pop(wid)
        app.load_sandboxes()
        self.assertEqual(app.WORKSPACES[wid].current("company")["value"], "南京青山科技")

    def test_branch_and_delete_do_not_mutate_official(self):
        status, _, body = request("/api/fork", {"id": "official", "turn": 7})
        self.assertEqual(status, 200)
        wid = json.loads(body)["workspace"]["id"]
        status, _, _ = request("/api/feed", {"id": wid, "text": "删除我的住址。", "role": "owner"})
        self.assertEqual(status, 200)
        self.assertIsNone(app.WORKSPACES[wid].current("home"))
        self.assertEqual(app.WORKSPACES["official"].current("home")["value"], "曲江新区")
        self.assertEqual(request("/api/feed", {"id": "official", "text": "我住在其他地方。"})[0], 400)

    def test_cross_origin_mutations_denied(self):
        self.assertEqual(request("/api/new", {}, {"Origin": "https://untrusted.invalid"})[0], 403)
        self.assertEqual(request("/api/new", {}, {"X-Shouyi-Request": "0"})[0], 403)

    def test_model_key_never_returned(self):
        status, _, body = request("/api/model", {"enabled": False, "base_url": "https://example.invalid/v1", "model": "test-model", "api_key": "unit-test-secret"})
        self.assertEqual(status, 200)
        self.assertNotIn("unit-test-secret", body.decode())
        self.assertNotIn("unit-test-secret", request("/api/bootstrap")[2].decode())
        self.assertFalse(app.STATE_PATH.exists())

    def test_export_uses_selected_round(self):
        early = request("/api/export?id=official&turn=1")[2].decode()
        final = request("/api/export?id=official&turn=28")[2].decode()
        self.assertIn("产品经理", early)
        self.assertNotIn("曲江新区", early)
        self.assertIn("已通过PMP考试并取得证书", final)
        self.assertNotIn("610102199103154817", final)
        self.assertEqual(request("/api/export?turn=99")[0], 400)

    def test_markup_has_unique_controls_and_no_inline_handlers(self):
        class Parser(HTMLParser):
            def __init__(self): super().__init__(); self.ids=[]; self.inline=[]
            def handle_starttag(self, tag, attrs):
                data=dict(attrs)
                if "id" in data: self.ids.append(data["id"])
                self.inline += [k for k,v in attrs if k.startswith("on")]
        parser=Parser(); parser.feed((app.ROOT/"static"/"index.html").read_text(encoding="utf-8"))
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertEqual(parser.inline, [])
        for required in ("storyContent", "memoryCards", "composer", "timeline", "rulesDialog", "modelDialog"):
            self.assertIn(required, parser.ids)


if __name__ == "__main__": unittest.main()
