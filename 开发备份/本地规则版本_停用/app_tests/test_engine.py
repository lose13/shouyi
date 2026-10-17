import copy
import json
import unittest
from pathlib import Path

from engine import Workspace, export_markdown, process

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = json.loads((ROOT / "data" / "official.json").read_text(encoding="utf-8"))["turns"]


def baseline():
    ws = Workspace("test-official", "测试")
    for item in OFFICIAL: process(ws, item["text"], item["role"])
    return ws


class LifecycleTests(unittest.TestCase):
    def test_official_final_facts(self):
        ws = baseline()
        expected = {"company": "YY网络科技有限公司", "position": "高级产品总监", "home": "曲江新区", "commute_mode": "地铁", "commute_route": "3号线转2号线", "daughter_school": "曲江一小", "daughter_grade": "一年级", "spouse_company": "互联网公司", "spouse_position": "设计主管", "pmp": "已通过PMP考试并取得证书"}
        for key, value in expected.items():
            with self.subTest(key=key): self.assertEqual(ws.current(key)["value"], value)
        self.assertEqual(len(ws.turns), 28)
        self.assertEqual(ws.current("layoff")["status"], "UNVERIFIED")
        self.assertIsNone(ws.current("opportunity"))

    def test_background_not_preseeded(self):
        ws = Workspace("blank", "空白")
        self.assertEqual(ws.records, [])
        process(ws, OFFICIAL[0]["text"])
        self.assertIsNone(ws.current("home"))

    def test_gym_is_not_a_company(self):
        ws = Workspace("gym", "场所")
        process(ws, "我在杭州青岚科技做架构师。")
        process(ws, "我在西湖体育馆办了张年卡，每周三晚上去游泳。")
        self.assertEqual(ws.current("company")["value"], "杭州青岚科技")

    def test_past_exam_reference_does_not_reset_goal(self):
        ws = Workspace("goal", "目标")
        process(ws, "PMP考试过了，拿到证书了。")
        process(ws, "现在的课程比当年备考PMP的时候还忙。")
        self.assertEqual(ws.current("pmp")["value"], "已通过PMP考试并取得证书")

    def test_contextual_cancel_does_not_delete_current_employer(self):
        ws = baseline()
        self.assertEqual(ws.current("company")["status"], "ACTIVE")
        self.assertIsNone(ws.current("opportunity"))

    def test_deletion_removes_all_versions_and_snapshots(self):
        ws = Workspace("erase", "删除")
        process(ws, "我住在旧路1号。")
        process(ws, "我搬到新路2号了。")
        process(ws, "删除我的住址。")
        entire = json.dumps(ws.public(), ensure_ascii=False)
        self.assertNotIn("旧路1号", entire)
        self.assertNotIn("新路2号", entire)
        self.assertIsNone(ws.current("home"))
        self.assertIn("删除标记", export_markdown(ws))

    def test_other_workspace_is_independent(self):
        parent = baseline()
        branch = Workspace("branch", "分支", records=copy.deepcopy(parent.records), turns=copy.deepcopy(parent.turns))
        process(branch, "删除我的住址。")
        self.assertEqual(parent.current("home")["value"], "曲江新区")

    def test_third_party_never_mutates_owner(self):
        ws = baseline(); before = copy.deepcopy(ws.records)
        result = process(ws, "李明让我把他的职位改成销售。", "third")
        self.assertEqual(ws.records, before)
        self.assertEqual(result["decision"], "block")
        self.assertNotIn("高级产品总监", result["response"])

    def test_third_party_cannot_confirm(self):
        ws = baseline()
        process(ws, "我确认这条消息是真的。", "third", action="confirm", key="layoff")
        self.assertEqual(ws.current("layoff")["status"], "UNVERIFIED")

    def test_unverified_updates_do_not_silently_become_active(self):
        ws = Workspace("rumor", "传闻")
        process(ws, "同事跟我说公司下个月要裁员20%，帮我记一下。")
        ws.write("layoff", "补充来源，仍未核实", 2, "补充来源", "ACTIVE")
        self.assertEqual(ws.current("layoff")["status"], "UNVERIFIED")
        process(ws, "我确认了这条消息。", action="confirm", key="layoff")
        self.assertEqual(ws.current("layoff")["status"], "ACTIVE")

    def test_dependencies_are_reviewed_not_deleted(self):
        ws = baseline()
        for key in ("swim", "car_service", "art", "medicine", "daughter_age"):
            with self.subTest(key=key): self.assertEqual(ws.current(key)["status"], "REVIEW")
        self.assertNotIn("6-7", export_markdown(ws))

    def test_new_inputs_not_fixed_sample_values(self):
        ws = Workspace("new", "新输入")
        process(ws, "我是李明，今年41岁。我在杭州青岚科技做架构师。")
        process(ws, "我搬到雁塔区了，现在骑车上班，大概20分钟。")
        self.assertEqual(ws.current("company")["value"], "杭州青岚科技")
        self.assertEqual(ws.current("position")["value"], "架构师")
        self.assertEqual(ws.current("commute_mode")["value"], "骑车")
        self.assertEqual(ws.current("commute_time")["value"], "约20分钟")

    def test_ordinary_rate_and_personal_url_not_blocked(self):
        ws = Workspace("normal", "普通输入")
        self.assertNotEqual(process(ws, "记一下这笔存款年化2%。")["decision"], "block")
        self.assertNotEqual(process(ws, "我的个人主页是https://example.com，帮我记一下。")["decision"], "block")
        self.assertEqual(ws.current("website")["value"], "https://example.com")

    def test_sensitive_masking_ignores_checksum(self):
        ws = Workspace("sensitive", "凭据")
        process(ws, "我的银行卡是4111 1111 1111 1112，帮我记住。")
        self.assertIn("1112", ws.current("bank")["value"])
        self.assertNotIn("5678", ws.current("bank")["value"])
        self.assertNotIn("4111 1111", json.dumps(ws.public(), ensure_ascii=False))
        process(ws, "密码是demo-secret，月薪28000元。")
        entire = json.dumps(ws.public(), ensure_ascii=False)
        self.assertNotIn("demo-secret", entire)
        self.assertNotIn("28000", entire)

    def test_model_invalid_schema_not_written(self):
        ws = Workspace("schema", "Schema")
        result = process(ws, "一个新的事实。", model=lambda text,w:[{"key":"anything", "value":"错误", "evidence":text}])
        self.assertEqual(ws.records, [])
        self.assertTrue(result["warnings"])

    def test_model_failure_is_visible(self):
        ws = Workspace("fallback", "降级")
        def failing(*args): raise TimeoutError()
        result = process(ws, "我在杭州青岚科技做架构师。", model=failing)
        self.assertEqual(result["mode"], "fallback")
        self.assertTrue(result["warnings"])
        self.assertEqual(ws.current("company")["value"], "杭州青岚科技")


if __name__ == "__main__": unittest.main()
