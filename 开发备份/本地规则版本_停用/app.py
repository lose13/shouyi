from __future__ import annotations

import argparse
import copy
import json
import os
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from engine import CATEGORIES, STATUSES, Workspace, export_markdown, process
from model import ModelClient

ROOT = Path(__file__).resolve().parent
DELIVERY_ROOT = ROOT.parent
RULES = (DELIVERY_ROOT / "memory-rules.md").read_text(encoding="utf-8")
OFFICIAL = json.loads((ROOT / "data" / "official.json").read_text(encoding="utf-8"))
MODEL = ModelClient(RULES)
LOCK = threading.RLock()
STATE_PATH = ROOT / ".state" / "workspaces.json"


def run_baseline():
    workspace = Workspace("official", "官方28轮 · 本作品规则处理", "official")
    for item in OFFICIAL["turns"]:
        process(workspace, item["text"], item["role"])
    return workspace


WORKSPACES = {"official": run_baseline()}


def load_sandboxes():
    if STATE_PATH.exists():
        try:
            for data in json.loads(STATE_PATH.read_text(encoding="utf-8")):
                if data.get("kind") != "official":
                    ws = Workspace(**data)
                    WORKSPACES[ws.id] = ws
        except (ValueError, TypeError, OSError):
            pass


def persist():
    STATE_PATH.parent.mkdir(exist_ok=True)
    target = STATE_PATH.with_suffix(".tmp")
    values = [dict(id=w.id, title=w.title, kind=w.kind, owner_id=w.owner_id, owner_name=w.owner_name,
                   records=w.records, turns=w.turns, created_at=w.created_at) for w in WORKSPACES.values() if w.kind != "official"]
    target.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(target, STATE_PATH)


def catalog():
    return [{"id": w.id, "title": w.title, "kind": w.kind, "turn_count": len(w.turns)} for w in WORKSPACES.values()]


class Handler(BaseHTTPRequestHandler):
    server_version = "Shouyi/1.0"

    def send_bytes(self, data, content_type="application/json; charset=utf-8", status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if filename: self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def json(self, obj, status=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), status=status)

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        static = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
        if url.path in static:
            name, mime = static[url.path]
            self.send_bytes((ROOT / "static" / name).read_bytes(), mime + "; charset=utf-8")
            return
        with LOCK:
            if url.path == "/api/bootstrap":
                self.json({"workspaces": catalog(), "workspace": WORKSPACES["official"].public(), "categories": CATEGORIES, "statuses": STATUSES,
                           "rules": RULES, "model": MODEL.public(), "source": OFFICIAL["source"]})
            elif url.path == "/api/workspace":
                ws = WORKSPACES.get(qs.get("id", ["official"])[0])
                self.json(ws.public() if ws else {"error": "工作区不存在"}, 200 if ws else 404)
            elif url.path == "/api/export":
                ws = WORKSPACES.get(qs.get("id", ["official"])[0])
                if not ws: self.json({"error": "工作区不存在"}, 404); return
                kind = qs.get("kind", ["memory"])[0]
                if kind == "rules": self.send_bytes(RULES.encode(), "text/markdown; charset=utf-8", filename="memory-rules.md")
                else:
                    turn = qs.get("turn", [None])[0]
                    if turn is None: records = ws.records
                    else:
                        try: index = int(turn)
                        except ValueError: self.json({"error": "轮次无效"}, 400); return
                        if index < 0 or index > len(ws.turns): self.json({"error": "轮次无效"}, 400); return
                        records = ws.turns[index - 1]["snapshot"] if index else []
                    self.send_bytes(export_markdown(ws, records).encode(), "text/markdown; charset=utf-8", filename="memory-LIMING.md")
            else:
                self.json({"error": "该路径不可访问"}, 404)

    def do_POST(self):
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if self.headers.get("X-Shouyi-Request") != "1" or (origin and origin != "http://" + host):
            self.json({"error": "请求来源无效"}, 403); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 65536: raise ValueError("请求体大小无效")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict): raise ValueError("请求体必须为对象")
            path = urlparse(self.path).path
            with LOCK:
                if path == "/api/new":
                    wid = "sandbox-" + uuid.uuid4().hex[:10]
                    ws = Workspace(wid, str(body.get("title") or "自由试验")[:50])
                    WORKSPACES[wid] = ws
                elif path == "/api/fork":
                    parent = WORKSPACES.get(body.get("id"))
                    if not parent: raise ValueError("来源工作区不存在")
                    turn = int(body.get("turn", len(parent.turns)))
                    if turn < 0 or turn > len(parent.turns): raise ValueError("轮次超出范围")
                    wid = "branch-" + uuid.uuid4().hex[:10]
                    ws = Workspace(wid, f"第{turn}轮之后的试验", "branch")
                    ws.turns = copy.deepcopy(parent.turns[:turn])
                    ws.records = copy.deepcopy(ws.turns[-1]["snapshot"]) if ws.turns else []
                    for r in ws.records: r["id"] = f"{wid}:{r['key']}:{r['source_turn']}"
                    WORKSPACES[wid] = ws
                elif path == "/api/feed":
                    ws = WORKSPACES.get(body.get("id"))
                    if not ws: raise ValueError("工作区不存在")
                    if ws.kind == "official": raise ValueError("请创建自由试验或从当前轮次创建分支")
                    process(ws, body.get("text", ""), body.get("role", "owner"), model=MODEL.extract if MODEL.enabled else None,
                            action=body.get("action"), key=body.get("key"))
                elif path == "/api/reset":
                    ws = WORKSPACES.get(body.get("id"))
                    if not ws or ws.kind == "official": raise ValueError("示例回放不会被清空，请选择自由试验")
                    ws.records, ws.turns = [], []
                elif path == "/api/model":
                    self.json(MODEL.configure(body)); return
                else:
                    self.json({"error": "接口不存在"}, 404); return
                persist()
                self.json({"workspace": ws.public(), "workspaces": catalog()})
        except (ValueError, TypeError, KeyError) as exc:
            self.json({"error": str(exc)}, 400)
        except Exception:
            self.json({"error": "处理失败，未返回内部配置。请检查输入或重新启动。"}, 500)

    def log_message(self, fmt, *args):
        # Request paths only; never echo submitted private text or credentials.
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--export-baseline", action="store_true")
    args = parser.parse_args()
    if args.export_baseline:
        (DELIVERY_ROOT / "memory-LIMING.md").write_text(export_markdown(WORKSPACES["official"]), encoding="utf-8")
        print("Baseline memory exported.")
        return
    load_sandboxes()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    address = f"http://127.0.0.1:{args.port}"
    print(f"SHOUYI ready: {address}", flush=True)
    if not args.no_browser: threading.Timer(.6, lambda: webbrowser.open(address)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
