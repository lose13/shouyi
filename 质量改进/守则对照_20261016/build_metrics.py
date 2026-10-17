"""Describe observed runs without assigning semantic accuracy scores."""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys


ROOT = Path(__file__).resolve().parent


def metrics(run, historical=False):
    workspace = run["workspace"]
    turns, records = workspace["turns"], workspace["records"]
    calls = ([c for a in run["attempts"] for c in a["calls"]] if not historical else
             [c for t in turns for c in (t["model_call"].get("stages") or [t["model_call"]])])
    live = [r for r in records if r["status"] in ("ACTIVE", "REVIEW", "UNVERIFIED")]
    input_tokens = [c.get("usage", {}).get("prompt_tokens", 0) for c in calls]
    starts, ends = run.get("started_at"), run.get("finished_at")
    if historical:
        starts, ends = run["job"].get("started_at"), run["job"].get("finished_at")
    return {
        "status": run.get("status", run.get("job", {}).get("status")),
        "saved_turns": len(turns), "api_requests": len(calls),
        "failed_attempts": run.get("failed_attempts", 0 if historical else None),
        "logical_attempts": len(run.get("attempts", turns)),
        "unique_request_ids": len({c["request_id"] for c in calls if c.get("request_id")}),
        "rules_characters": run.get("rules_characters"),
        "live_records": len(live), "live_statuses": dict(Counter(r["status"] for r in live)),
        "live_categories": dict(Counter(r["category"] for r in live)),
        "record_statuses": dict(Counter(r["status"] for r in records)),
        "deleted_attributes": sorted({r["key"] for r in records if r["status"] == "DELETED"}),
        "decisions": dict(Counter(t["decision"] for t in turns)),
        "changes": dict(Counter(c["action"] for t in turns for c in t["changes"])),
        "format_repairs": sum(c.get("stage") == "repair" for c in calls),
        "nonowner_changes": sum(len(t["changes"]) for t in turns if t["role"] != "owner"),
        "usage": {k: sum(c.get("usage", {}).get(k, 0) for c in calls)
                  for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
        "mean_prompt_tokens": round(statistics.mean(input_tokens), 1) if input_tokens else 0,
        "first_prompt_tokens": input_tokens[0] if input_tokens else 0,
        "last_prompt_tokens": input_tokens[-1] if input_tokens else 0,
        "provider_seconds": round(sum(c.get("elapsed_seconds", 0) for c in calls), 3),
        "experiment_wall_seconds": round((datetime.fromisoformat(ends) - datetime.fromisoformat(starts)).total_seconds(), 3)
                                   if starts and ends else None,
        "call_model_names": sorted({c.get("model", "") for c in calls}),
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    summary = {}
    for name in ("gemini_delivery_v14", "shouyi_v24"):
        path = ROOT / (name + ".json")
        if path.exists():
            summary[name] = metrics(json.loads(path.read_text(encoding="utf-8")))
    path = ROOT / "historical-v2.2.1-run.json"
    if path.exists():
        summary["historical_shouyi_v221"] = metrics(json.loads(path.read_text(encoding="utf-8")), True)
    (ROOT / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
