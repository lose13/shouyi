"""Count observed requests and states, without assigning semantic accuracy."""
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def summarize(path):
    run = json.loads(path.read_text(encoding="utf-8"))
    ws, job = run["workspace"], run["job"]
    calls = [c for turn in ws["turns"] for c in (turn["model_call"].get("stages") or [turn["model_call"]])]
    calls += [c for failure in job.get("failures", []) for c in failure.get("calls", [])]
    return {
        "run_id": job["run_id"], "status": job["status"], "completed": job["completed"],
        "started_at": job["started_at"], "finished_at": job.get("finished_at"),
        "attempts": job["attempts"], "api_requests": len(calls),
        "unique_request_ids": len({c.get("request_id") for c in calls if c.get("request_id")}),
        "failures": len(job.get("failures", [])),
        "format_repair_turns": [t["turn"] for t in ws["turns"] if t["model_call"].get("format_repairs")],
        "statuses": dict(Counter(r["status"] for r in ws["records"])),
        "live_records": sum(r["status"] in ("ACTIVE", "REVIEW", "UNVERIFIED") for r in ws["records"]),
        "decisions": dict(Counter(t["decision"] for t in ws["turns"])),
        "changes": dict(Counter(c["action"] for t in ws["turns"] for c in t["changes"])),
        "usage": {k: sum(c.get("usage", {}).get(k, 0) for c in calls) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
        "usage_missing_calls": sum(not c.get("usage") for c in calls),
        "message_role_sequences": sorted({tuple(c.get("message_roles", [])) for c in calls}),
        "nonowner_changes": sum(len(t["changes"]) for t in ws["turns"] if t["role"] != "owner"),
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    summary = {"v2.5": summarize(ROOT / "baseline-v2.5-run.json")}
    current = ROOT / "official-llm-run.json"
    if current.exists():
        summary["v2.6"] = summarize(current)
    (ROOT / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
