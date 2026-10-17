from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from engine import CATEGORIES, STATUSES, Workspace, export_markdown, now, process
from model import ModelClient, ModelError, PROTOCOL

ROOT = Path(__file__).resolve().parent
DELIVERY_ROOT = ROOT.parent
RULES = (DELIVERY_ROOT / "memory-rules.md").read_text(encoding="utf-8")
OFFICIAL = json.loads((ROOT / "data" / "official.json").read_text(encoding="utf-8"))
RULES_HASH = hashlib.sha256(RULES.encode()).hexdigest()
PROTOCOL_HASH = hashlib.sha256(PROTOCOL.encode()).hexdigest()
SOURCE_HASH = hashlib.sha256(json.dumps(OFFICIAL["turns"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
MODEL, LOCK = ModelClient(RULES), threading.RLock()
STATE_PATH = ROOT / ".state" / "workspaces-v2.json"
RUN_PATH = ROOT / "data" / "official-llm-run.json"
WORKSPACES = {"official": Workspace("official", "官方28轮 · LLM真实运行", "official")}
JOB = {"status": "idle", "completed": 0, "total": len(OFFICIAL["turns"]), "attempts": 0,
       "error": "", "run_id": "", "rules_sha256": RULES_HASH, "source_sha256": SOURCE_HASH}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_results():
    global JOB
    if RUN_PATH.is_file():
        try:
            data = json.loads(RUN_PATH.read_text(encoding="utf-8"))
            cached = data["workspace"]
            if cached.get("engine") == "llm-v2" and cached.get("id") == "official":
                WORKSPACES["official"] = Workspace(**cached)
                JOB = data["job"]
                JOB["completed"] = len(cached["turns"])
                if JOB["status"] == "running":
                    JOB.update(status="failed", error="上次运行被中断；配置密钥后可从下一轮继续")
        except (ValueError, TypeError, KeyError, OSError):
            JOB.update(status="failed", error="已有运行文件无法读取，未生成替代结果")
    if STATE_PATH.is_file():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            for value in data.get("workspaces", []):
                if value.get("engine") == "llm-v2" and value.get("kind") != "official":
                    ws = Workspace(**value)
                    WORKSPACES[ws.id] = ws
        except (ValueError, TypeError, KeyError, OSError):
            pass


def persist():
    atomic_json(STATE_PATH, {"version": 2, "workspaces": [w.public() for w in WORKSPACES.values() if w.kind != "official"]})


def persist_run():
    atomic_json(RUN_PATH, {"workspace": WORKSPACES["official"].public(), "job": JOB})


def catalog():
    return [{"id": w.id, "title": w.title, "kind": w.kind, "turn_count": len(w.turns)} for w in WORKSPACES.values()]


def public_job():
    result = copy.deepcopy(JOB)
    result["rules_current"] = JOB.get("rules_sha256") == RULES_HASH
    result["source_current"] = JOB.get("source_sha256") == SOURCE_HASH
    turns = WORKSPACES["official"].turns
    saved_protocol = JOB.get("protocol_sha256") or (turns[0].get("model_call", {}).get("protocol_sha256") if turns else PROTOCOL_HASH)
    result["protocol_current"] = saved_protocol == PROTOCOL_HASH
    return result


def official_worker(client):
    try:
        while True:
            with LOCK:
                ws = copy.deepcopy(WORKSPACES["official"])
                n = len(ws.turns)
                if n >= len(OFFICIAL["turns"]):
                    JOB.update(status="completed", completed=n, finished_at=now(), error="")
                    persist_run()
                    target = DELIVERY_ROOT / "memory-LIMING.md"
                    temporary = target.with_suffix(".md.tmp")
                    temporary.write_text(export_markdown(ws), encoding="utf-8")
                    os.replace(temporary, target)
                    return
                item = OFFICIAL["turns"][n]
                JOB["attempts"] += 1
                JOB["current_turn"] = n + 1
            process(ws, item["text"], item["role"], model=client.decide)
            with LOCK:
                WORKSPACES["official"] = ws
                JOB.update(completed=len(ws.turns), error="")
                persist_run()
    except (ModelError, ValueError) as error:
        with LOCK:
            JOB.update(status="failed", error=str(error), stopped_at=now())
            persist_run()
    except Exception:
        with LOCK:
            JOB.update(status="failed", error="运行或保存失败；已完成轮次可查看，没有生成本地替代结果", stopped_at=now())
            try:
                persist_run()
            except OSError:
                pass


def start_official(reset=False):
    with LOCK:
        if JOB["status"] == "running":
            return public_job()
        if not MODEL.public()["configured"]:
            raise ValueError("请先在引擎设置中配置LLM密钥")
        ws = WORKSPACES["official"]
        if ws.turns and not reset:
            if JOB["status"] == "completed":
                raise ValueError("已有完整LLM结果；重新运行请明确选择重跑")
            if JOB.get("rules_sha256") != RULES_HASH or JOB.get("source_sha256") != SOURCE_HASH:
                raise ValueError("守则或输入材料已变化，不能混合续跑，请重新运行")
            if not public_job()["protocol_current"]:
                raise ValueError("模型决策协议已变化，不能混合续跑，请重新运行")
            if JOB.get("requested_model") != MODEL.model or JOB.get("provider_url") != MODEL.base_url:
                raise ValueError("模型配置与上次运行不同，请恢复原配置或重新运行")
        if reset or not ws.turns:
            WORKSPACES["official"] = Workspace("official", "官方28轮 · LLM真实运行", "official")
            JOB.clear()
            JOB.update(status="idle", completed=0, total=len(OFFICIAL["turns"]), attempts=0, error="",
                       run_id=uuid.uuid4().hex, started_at=now(), rules_sha256=RULES_HASH,
                       protocol_sha256=PROTOCOL_HASH, source_sha256=SOURCE_HASH, requested_model=MODEL.model, provider_url=MODEL.base_url)
            target = DELIVERY_ROOT / "memory-LIMING.md"
            if target.is_file():
                archive = ROOT / ".state" / ("previous-memory-" + uuid.uuid4().hex[:10] + ".md")
                archive.parent.mkdir(exist_ok=True)
                os.replace(target, archive)
        client = copy.copy(MODEL)
        JOB.update(status="running", error="")
        persist_run()
        threading.Thread(target=official_worker, args=(client,), daemon=True).start()
        return public_job()


load_results()


class Handler(BaseHTTPRequestHandler):
    server_version = "Shouyi/2.0"

    def send_bytes(self, data, content_type="application/json; charset=utf-8", status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def json(self, value, status=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), status=status)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        static = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
        if url.path in static:
            name, mime = static[url.path]
            self.send_bytes((ROOT / "static" / name).read_bytes(), mime + "; charset=utf-8")
            return
        with LOCK:
            if url.path == "/api/bootstrap":
                self.json({"workspaces": catalog(), "workspace": WORKSPACES["official"].public(), "categories": CATEGORIES, "statuses": STATUSES,
                           "rules": RULES, "model": MODEL.public(), "source": OFFICIAL["source"], "job": public_job()})
            elif url.path == "/api/official/status":
                self.json({"job": public_job(), "workspace": WORKSPACES["official"].public(), "workspaces": catalog()})
            elif url.path == "/api/workspace":
                ws = WORKSPACES.get(query.get("id", ["official"])[0])
                self.json(ws.public() if ws else {"error": "工作区不存在"}, 200 if ws else 404)
            elif url.path == "/api/export":
                if query.get("kind", ["memory"])[0] == "rules":
                    self.send_bytes(RULES.encode(), "text/markdown; charset=utf-8", filename="memory-rules.md")
                    return
                ws = WORKSPACES.get(query.get("id", ["official"])[0])
                if not ws or not ws.turns:
                    self.json({"error": "尚无LLM实际运行产生的记忆"}, 400)
                    return
                try:
                    count = int(query.get("turn", [len(ws.turns)])[0])
                    if not 1 <= count <= len(ws.turns):
                        raise ValueError()
                except ValueError:
                    self.json({"error": "轮次无效"}, 400)
                    return
                view = copy.deepcopy(ws)
                view.turns = view.turns[:count]
                view.records = copy.deepcopy(view.turns[-1]["snapshot"])
                filename = "memory-LIMING.md" if ws.kind == "official" and count == len(OFFICIAL["turns"]) else f"memory-LIMING-turn-{count}.md"
                self.send_bytes(export_markdown(view).encode(), "text/markdown; charset=utf-8", filename=filename)
            else:
                self.json({"error": "该路径不可访问"}, 404)

    def do_POST(self):
        host, origin = self.headers.get("Host", ""), self.headers.get("Origin")
        if self.headers.get("X-Shouyi-Request") != "1" or (origin and origin != "http://" + host):
            self.json({"error": "请求来源无效"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 65536:
                raise ValueError("请求体大小无效")
            body = json.loads(self.rfile.read(length).decode())
            if not isinstance(body, dict):
                raise ValueError("请求体必须为对象")
            path = urlparse(self.path).path
            if path == "/api/official/start":
                self.json({"job": start_official(body.get("reset") is True)})
                return
            if path == "/api/feed":
                with LOCK:
                    original = WORKSPACES.get(body.get("id"))
                    if not original or original.kind == "official":
                        raise ValueError("请使用独立试验或分支")
                    ws, client = copy.deepcopy(original), copy.copy(MODEL)
                process(ws, body.get("text", ""), body.get("role", "owner"), model=client.decide, action=body.get("action"), key=body.get("key"))
                with LOCK:
                    if WORKSPACES.get(ws.id) is not original:
                        raise ValueError("工作区已被另一操作修改，请重新提交")
                    WORKSPACES[ws.id] = ws
                    persist()
                    self.json({"workspace": ws.public(), "workspaces": catalog()})
                return
            with LOCK:
                if path == "/api/model":
                    self.json(MODEL.configure(body))
                    return
                if path == "/api/new":
                    ws = Workspace("sandbox-" + uuid.uuid4().hex[:10], str(body.get("title") or "自由试验")[:50])
                elif path == "/api/fork":
                    parent = WORKSPACES.get(body.get("id"))
                    if not parent:
                        raise ValueError("来源工作区不存在")
                    turn = int(body.get("turn", len(parent.turns)))
                    if not 0 <= turn <= len(parent.turns):
                        raise ValueError("轮次超出范围")
                    ws = Workspace("branch-" + uuid.uuid4().hex[:10], f"第{turn}轮之后的LLM试验", "branch")
                    ws.turns = copy.deepcopy(parent.turns[:turn])
                    ws.records = copy.deepcopy(ws.turns[-1]["snapshot"]) if ws.turns else []
                    for record in ws.records:
                        record["id"] = f"{ws.id}:{record['key']}:{record['source_turn']}"
                elif path == "/api/reset":
                    old = WORKSPACES.get(body.get("id"))
                    if not old or old.kind == "official":
                        raise ValueError("只能清空独立试验")
                    ws = Workspace(old.id, old.title, old.kind)
                else:
                    self.json({"error": "接口不存在"}, 404)
                    return
                WORKSPACES[ws.id] = ws
                persist()
                self.json({"workspace": ws.public(), "workspaces": catalog()})
        except ModelError as error:
            self.json({"error": str(error)}, 502)
        except (ValueError, TypeError, KeyError) as error:
            self.json({"error": str(error)}, 400)
        except Exception:
            self.json({"error": "模型处理或存储失败，本轮没有可确认的成功结果"}, 500)

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    address = f"http://127.0.0.1:{args.port}"
    print(f"SHOUYI LLM ready: {address}", flush=True)
    if not args.no_browser:
        threading.Timer(.6, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
