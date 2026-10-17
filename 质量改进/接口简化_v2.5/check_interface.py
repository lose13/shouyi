"""Offline regression checks for the changed transaction/API boundary."""
import copy
import json
from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[2] / "守忆" / "app"
sys.path.insert(0, str(APP))
from engine import Workspace, apply_decision, now, process
from model import ModelClient, ModelError


def metadata(stage="plan"):
    return {"model": "offline-stub", "request_id": "offline-" + stage,
            "started_at": now(), "elapsed_seconds": 0, "stage": stage, "usage": {}}


def decision():
    return {"decision": "change", "reason": "主人提供事实", "rule": "2",
            "response": "已保存测试事项。", "used_keys": [], "operations": [{
                "action": "upsert", "key": "task", "label": "事项", "category": "EVENT",
                "value": "明天寄包裹", "status": "ACTIVE", "confirmed": False,
                "evidence": "明天寄包裹", "reason": "主人明确交办", "rule": "2",
                "validity": {"kind": "temporary", "reference_turn": 1,
                             "time_text": "明天", "end_condition": ""}}]}


class InterfaceBoundary(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace("offline-check", "offline-check")

    def test_optional_diagnostics_cannot_block_valid_operations(self):
        output = decision()
        # Deliberately contradictory/invalid diagnostics must have no authority.
        output["observations"] = [{"handling": "skip", "operation_keys": ["wrong.key"]}]
        apply_decision(self.ws, "明天寄包裹", "owner", output, metadata())
        self.assertEqual(self.ws.current("task")["value"], "明天寄包裹")
        self.assertNotIn("observations", self.ws.turns[0])

    def test_diagnostics_alone_do_not_create_memory(self):
        output = {"decision": "ignore", "reason": "无操作", "rule": "2", "response": "收到。",
                  "used_keys": [], "operations": [], "observations": "arbitrary diagnostic"}
        apply_decision(self.ws, "你好", "owner", output, metadata())
        self.assertEqual(self.ws.records, [])

    def test_invalid_second_operation_is_atomic_and_located(self):
        output = decision()
        other = copy.deepcopy(output["operations"][0])
        other.update(key="other", evidence="原文没有这句话")
        output["operations"].append(other)
        with self.assertRaisesRegex(ValueError, r"operations\[1\]\(other\)\.evidence"):
            apply_decision(self.ws, "明天寄包裹", "owner", output, metadata())
        self.assertEqual(self.ws.records, [])
        self.assertEqual(self.ws.turns, [])

    def test_third_party_still_cannot_write(self):
        with self.assertRaisesRegex(ValueError, "写权限"):
            apply_decision(self.ws, "明天寄包裹", "third", decision(), metadata())
        self.assertEqual(self.ws.records, [])

    def test_nonexistent_forget_is_rejected_with_key(self):
        output = decision()
        output["operations"] = [{"action": "forget", "key": "missing", "evidence": "明天寄包裹",
                                 "reason": "删除请求", "rule": "5"}]
        with self.assertRaisesRegex(ValueError, r"operations\[0\]\(missing\)\.key"):
            apply_decision(self.ws, "明天寄包裹", "owner", output, metadata())
        self.assertEqual(self.ws.turns, [])

    def test_pending_fact_requires_explicit_confirmation(self):
        output = decision()
        output["operations"][0]["status"] = "REVIEW"
        apply_decision(self.ws, "明天寄包裹", "owner", output, metadata())
        before = self.ws.public()
        with self.assertRaisesRegex(ValueError, r"operations\[0\]\(task\)\.confirmed"):
            apply_decision(self.ws, "明天寄包裹", "owner", decision(), metadata())
        self.assertEqual(self.ws.public(), before)

    def make_client(self, outputs):
        client = ModelClient("offline contract")
        client.configure({"api_key": "offline-placeholder"})
        contexts = []
        def fake_call(context):
            contexts.append(context)
            return copy.deepcopy(outputs[len(contexts) - 1]), metadata(context["stage"])
        client._call = fake_call
        return client, contexts

    def test_model_accepts_no_candidate_list_in_one_call(self):
        client, contexts = self.make_client([decision()])
        entry = process(self.ws, "明天寄包裹", model=client.decide)
        self.assertEqual(entry["model_call"]["request_count"], 1)
        self.assertEqual(len(contexts), 1)

    def test_repair_receives_field_path_and_keeps_both_failures(self):
        bad = decision()
        bad["operations"][0]["evidence"] = "不在输入中"
        client, contexts = self.make_client([bad, bad])
        with self.assertRaises(ModelError) as caught:
            process(self.ws, "明天寄包裹", model=client.decide)
        self.assertEqual(len(contexts), 2)
        self.assertIn("operations[0](task).evidence", contexts[1]["validation_error"])
        self.assertEqual(len(caught.exception.calls), 2)
        self.assertTrue(all(c.get("validation_error") for c in caught.exception.calls))
        self.assertEqual(self.ws.turns, [])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InterfaceBoundary)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {"kind": "offline boundary checks; no API calls", "tests": result.testsRun,
               "failures": len(result.failures), "errors": len(result.errors), "passed": result.wasSuccessful()}
    Path(__file__).with_name("interface-checks.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
