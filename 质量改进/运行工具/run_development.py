"""Run isolated real LLM scenarios. Expected answers never enter model context."""
import argparse
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--rules", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--ids", default="")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output already exists; keep the original evidence.")
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    if corpus["split"] != "development":
        raise SystemExit("This runner only accepts the development split.")
    selected = set(args.ids.split(",")) if args.ids else None
    cases = [c for c in corpus["scenarios"] if selected is None or c["id"] in selected]
    if not cases or (selected and {c["id"] for c in cases} != selected):
        raise SystemExit("Unknown or empty scenario selection.")
    sys.path.insert(0, str(args.source.resolve()))
    from engine import Workspace, now, process
    from model import ModelClient, ModelError, PROTOCOL

    client = ModelClient(args.rules.read_text(encoding="utf-8"))
    secret = getpass.getpass("DeepSeek API key (hidden): ")
    client.configure({"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key": secret})
    secret = ""
    run = {"variant": args.variant, "status": "running", "started_at": now(),
           "corpus_sha256": digest(args.corpus), "corpus_version": corpus["version"],
           "rules_title": client.rules.splitlines()[0], "rules_sha256": digest(args.rules),
           "protocol_sha256": hashlib.sha256(PROTOCOL.encode()).hexdigest(),
           "source_sha256": {name: digest(args.source / name) for name in ("engine.py", "model.py")},
           "model": client.public(), "temperature": 0, "retry_limit": 0,
           "planned_cases": len(cases), "planned_turns": sum(len(c["turns"]) for c in cases), "cases": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        temporary = args.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, args.output)

    save()
    all_calls = []
    for case in cases:
        ws = Workspace("eval-" + args.variant + "-" + case["id"], case["id"])
        record = {"id": case["id"], "planned_turns": len(case["turns"]), "steps": []}
        blocked = False
        for index, turn in enumerate(case["turns"], 1):
            if blocked:
                record["steps"].append({"turn": index, "status": "blocked_by_previous_failure", "calls": []})
                continue
            try:
                entry = process(ws, turn["text"], turn["role"], model=client.decide)
                metadata = entry["model_call"]
                calls = metadata.get("stages") or [metadata]
                record["steps"].append({"turn": index, "status": "saved", "calls": calls})
            except (ModelError, ValueError) as error:
                calls = getattr(error, "calls", [])
                record["steps"].append({"turn": index, "status": "failed", "error": str(error), "calls": calls})
                blocked = True
            all_calls.extend(calls)
        record["workspace"] = ws.public()
        run["cases"].append(record)
        run["api_attempts_recorded"] = len(all_calls)
        save()
        print(f"{args.variant} {case['id']}: {len(ws.turns)}/{len(case['turns'])} saved; calls={len(all_calls)}", flush=True)
        if any("HTTP 401" in s.get("error", "") or "HTTP 402" in s.get("error", "") for s in record["steps"]):
            run["status"] = "stopped_credentials_or_balance"
            break
    else:
        run["status"] = "finished"
    steps = [s for c in run["cases"] for s in c["steps"]]
    run.update(finished_at=now(), saved_turns=sum(s["status"] == "saved" for s in steps),
               failed_turns=sum(s["status"] == "failed" for s in steps),
               blocked_turns=sum(s["status"] == "blocked_by_previous_failure" for s in steps),
               api_attempts_recorded=len(all_calls),
               provider_request_ids=len({c["request_id"] for c in all_calls if c.get("request_id")}),
               known_usage={k: sum(c.get("usage", {}).get(k, 0) for c in all_calls) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
               usage_missing_calls=sum(not c.get("usage") for c in all_calls))
    save()
    print(json.dumps({k: run[k] for k in ("variant", "status", "saved_turns", "failed_turns", "blocked_turns", "api_attempts_recorded", "known_usage")}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
