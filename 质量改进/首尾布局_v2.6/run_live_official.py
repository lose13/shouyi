"""Trigger and record the actual local app's official run. No protocol override."""
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
APP = ROOT.parents[1] / "守忆" / "app"
BASE = "http://127.0.0.1:8137"


def request(path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json",
                                                               "X-Shouyi-Request": "1", "Origin": BASE})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(value):
    target = ROOT / "official-llm-run.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    for name in ("app.py", "model.py", "engine.py"):
        if digest(APP / name) != digest(ROOT / "frozen-source" / name):
            raise SystemExit("Source changed after snapshot.")
    bootstrap = request("/api/bootstrap")
    rule_text = (ROOT / "memory-rules-v2.6.md").read_text(encoding="utf-8")
    if bootstrap["rules"] != rule_text:
        raise SystemExit("Live server has not loaded the frozen v2.6 rules.")
    if bootstrap["job"]["status"] == "running":
        raise SystemExit("An official run is already running; do not replace it.")
    if (ROOT / "official-llm-run.json").exists():
        raise SystemExit("Evidence already exists; keep the original run.")
    secret = getpass.getpass("DeepSeek API key (hidden): ")
    request("/api/model", {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key": secret})
    secret = ""
    started = request("/api/official/start", {"reset": True})["job"]
    manifest = {"run_id": started["run_id"], "entry": "actual app HTTP /api/official/start; reset=true",
                "runtime_protocol_override": False, "message_roles": ["system", "user", "system"],
                "prompt_layout": "rules_protocol,context,trusted_final_check", "provider": "https://api.deepseek.com",
                "requested_model": "deepseek-flash", "thinking": "disabled", "temperature": 0,
                "rules_sha256": started["rules_sha256"], "protocol_sha256": started["protocol_sha256"],
                "source_sha256": {name: digest(APP / name) for name in ("app.py", "model.py", "engine.py")},
                "official_input_sha256": digest(ROOT / "official-input.json")}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    previous = None
    while True:
        state = request("/api/official/status")
        job = state["job"]
        if job["run_id"] != manifest["run_id"]:
            raise SystemExit("Official job changed unexpectedly.")
        value = {"workspace": state["workspace"], "job": job}
        marker = (job["completed"], job["status"], job.get("api_requests", 0))
        if marker != previous:
            save(value)
            print(json.dumps({k: job.get(k) for k in ("status", "completed", "current_turn", "attempts", "api_requests", "error")}, ensure_ascii=False), flush=True)
            previous = marker
        if job["status"] == "completed":
            exported = APP.parent / "memory-LIMING.md"
            # Completion and final export are saved inside the server lock.
            (ROOT / "memory-LIMING.md").write_bytes(exported.read_bytes())
            break
        if job["status"] == "failed":
            raise SystemExit("Run stopped; failure evidence retained.")
        time.sleep(2)


if __name__ == "__main__":
    main()
