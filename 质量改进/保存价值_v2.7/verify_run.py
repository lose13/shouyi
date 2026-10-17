"""Verify saved provenance and exports, without grading semantic correctness."""
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
APP = ROOT.parents[1] / "守忆" / "app"
sys.path.insert(0, str(ROOT / "frozen-source"))
from engine import Workspace, export_markdown
from model import PROTOCOL


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    run = json.loads((ROOT / "official-llm-run.json").read_text(encoding="utf-8"))
    baseline = json.loads((ROOT / "baseline-v2.6-run.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    ws, job = run["workspace"], run["job"]
    calls = [call for turn in ws["turns"] for call in (turn["model_call"].get("stages") or [turn["model_call"]])]
    calls += [call for failure in job.get("failures", []) for call in failure.get("calls", [])]
    rules = (ROOT / "memory-rules-v2.7.md").read_text(encoding="utf-8")
    rules_hash = hashlib.sha256(rules.encode()).hexdigest()
    protocol_hash = hashlib.sha256(PROTOCOL.encode()).hexdigest()
    current = json.loads((APP / "data" / "official-llm-run.json").read_text(encoding="utf-8"))
    official = json.loads((ROOT / "official-input.json").read_text(encoding="utf-8"))
    checks = {
        "complete_28": job["status"] == "completed" and len(ws["turns"]) == 28,
        "new_run_id": job["run_id"] != baseline["job"]["run_id"],
        "rules_match_live_and_manifest": rules == (APP.parent / "memory-rules.md").read_text(encoding="utf-8") and rules_hash == manifest["rules_sha256"] == job["rules_sha256"],
        "source_unchanged": all(sha(APP / name) == sha(ROOT / "frozen-source" / name) == manifest["source_sha256"][name] for name in ("app.py", "model.py", "engine.py")),
        "source_matches_baseline": manifest["source_sha256"] == json.loads((ROOT.parent / "首尾布局_v2.6" / "manifest.json").read_text(encoding="utf-8"))["source_sha256"],
        "official_input_unchanged": sha(ROOT / "official-input.json") == sha(APP / "data" / "official.json") == manifest["official_input_sha256"],
        "protocol_unchanged": protocol_hash == manifest["protocol_sha256"] == baseline["job"]["protocol_sha256"],
        "all_call_rules_protocol": bool(calls) and all(c.get("rules_sha256") == rules_hash and c.get("protocol_sha256") == protocol_hash for c in calls),
        "all_call_layout": bool(calls) and all(c.get("message_roles") == ["system", "user", "system"] for c in calls),
        "request_ids_distinct_and_complete": len({c.get("request_id") for c in calls if c.get("request_id")}) == len(calls) == job["api_requests"],
        "nonowner_no_writes": all(not t["changes"] for t in ws["turns"] if t["role"] != "owner"),
        "turns_order_and_roles": [t["turn"] for t in ws["turns"]] == list(range(1, 29)) and [t["role"] for t in ws["turns"]] == [t["role"] for t in official["turns"]],
        "live_export_from_state": (APP.parent / "memory-LIMING.md").read_text(encoding="utf-8") == export_markdown(Workspace(**ws)),
        "archive_export_identical": sha(ROOT / "memory-LIMING.md") == sha(APP.parent / "memory-LIMING.md"),
        "live_and_archive_same_state": current["workspace"] == ws and current["job"]["run_id"] == job["run_id"],
        "no_key_in_output": not re.search(r"sk-[A-Za-z0-9]{20,}", json.dumps(run, ensure_ascii=False)),
        "no_full_long_number_in_output": not re.search(r"(?<!\d)(?:\d[ -]?){15,18}(?!\d)", json.dumps(ws, ensure_ascii=False)),
    }
    result = {"checks": checks, "all_passed": all(checks.values()), "semantic_accuracy_verified": False}
    (ROOT / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
