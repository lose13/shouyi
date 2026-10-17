"""LLM memory transaction storage. No natural-language extraction or fallback engine."""
from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

CATEGORIES = {"FACT": "基本资料", "HEALTH": "健康", "GOAL": "目标", "PREFERENCE": "偏好与安排", "EVENT": "事件", "SENSITIVE": "敏感标记"}
STATUSES = {"ACTIVE": "有效", "REVIEW": "待确认", "UNVERIFIED": "待核实", "SUPERSEDED": "已失效", "DELETED": "已删除"}
ROLE_LABELS = {"owner": "主人本人", "third": "第三方", "push": "推送消息", "system": "系统消息"}
LIVE = ("ACTIVE", "REVIEW", "UNVERIFIED")


def now():
    return datetime.now(timezone.utc).isoformat()


def redact(text):
    """Transport/storage minimization only; this does not decide memory operations."""
    text = str(text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[密钥已隐藏]", text)
    text = re.sub(r"((?:密码|验证码|安全码|CVV|API\s*Key|密钥)\s*(?:是|为|[:：])?\s*)[^，。；\s]+", r"\1[不保存]", text, flags=re.I)
    text = re.sub(r"(?<!\d)(?:\d[ -]?){12,18}[\dXx](?!\d)", lambda m: "[长号码已隐藏，尾号" + re.sub(r"[ -]", "", m[0])[-4:] + "]", text)
    return re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", lambda m: m[0][:3] + "****" + m[0][-4:], text)


@dataclass
class Workspace:
    id: str
    title: str
    kind: str = "sandbox"
    owner_id: str = "LIMING"
    owner_name: str = "李明"
    records: list[dict] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=now)
    engine: str = "llm-v2"

    def public(self):
        return copy.deepcopy(asdict(self))

    def live(self):
        return [r for r in self.records if r["status"] in LIVE]

    def current(self, key):
        return next((r for r in reversed(self.records) if r["key"] == key and r["status"] in LIVE), None)

    def erase(self, key, n, reason, rule):
        matches = [r for r in self.records if r["key"] == key and r["status"] != "DELETED"]
        if not matches:
            raise ValueError("模型要求删除不存在的记忆属性")
        sample = matches[-1]
        source_turns = {r["source_turn"] for r in matches}
        strings = {r["value"] for r in matches if len(r["value"]) >= 2}
        strings.update(r.get("evidence", "") for r in matches if len(r.get("evidence", "")) >= 2)

        def clear(record):
            record.update(value="[已清除]", evidence="", reason="已按删除决策清除", status="DELETED", updated_turn=n)

        def scrub(value):
            if isinstance(value, str):
                for original in sorted(strings, key=len, reverse=True):
                    value = value.replace(original, "[已清除]")
                return value
            if isinstance(value, list):
                return [scrub(x) for x in value]
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items()}
            return value

        for record in matches:
            clear(record)
        for i, turn in enumerate(self.turns):
            touched = turn["turn"] in source_turns or key in turn.get("used_keys", [])
            for change in turn["changes"]:
                if change["key"] == key:
                    change.update(before=None, after="[已清除]", reason="相关内容已清除")
                    touched = True
            for record in turn["snapshot"]:
                if record["key"] == key:
                    clear(record)
            if touched:
                turn.update(input="[该轮相关原文已按删除请求清除]", response="[相关回复已清除]", reason="相关文本已清除，保留操作类型")
                turn["warnings"] = []
            self.turns[i] = scrub(turn)
        return {"key": key, "label": sample["label"], "category": sample["category"], "action": "遗忘",
                "before": None, "after": "内容已清除", "status": "DELETED", "reason": reason, "rule": rule}


def _string(obj, name, limit=2000, empty=False):
    value = obj.get(name)
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        raise ValueError("模型返回字段不符合约定：" + name)
    return redact(value.strip())


def apply_decision(workspace, text, role, result, metadata):
    """Apply the AI-selected operations atomically after structural/authority checks."""
    if not isinstance(result, dict):
        raise ValueError("模型未返回JSON对象")
    decision = result.get("decision")
    if decision not in ("change", "block", "ignore", "query", "clarify"):
        raise ValueError("模型返回未知决策")
    reason = _string(result, "reason", 1500)
    response = _string(result, "response", 6000)
    rule = _string(result, "rule", 120)
    operations = result.get("operations")
    used_keys = result.get("used_keys", [])
    if not isinstance(operations, list) or len(operations) > 40:
        raise ValueError("模型操作列表不符合约定")
    if not isinstance(used_keys, list) or any(not isinstance(k, str) for k in used_keys):
        raise ValueError("模型引用的记忆属性不符合约定")
    if role != "owner" and operations:
        raise ValueError("模型提出了越权写操作，本轮未保存")
    if decision in ("block", "ignore", "query", "clarify") and operations:
        raise ValueError("模型的决策与操作列表矛盾，本轮未保存")
    if decision == "change" and not operations:
        raise ValueError("模型声明变更但没有操作，本轮未保存")
    ws = copy.deepcopy(workspace)
    n, changes, seen = len(ws.turns) + 1, [], set()
    for op in operations:
        if not isinstance(op, dict):
            raise ValueError("模型操作必须为对象")
        action = op.get("action")
        key = _string(op, "key", 100)
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", key) or key in seen:
            raise ValueError("模型属性ID无效或同轮重复；请模型使用稳定且唯一的属性ID")
        seen.add(key)
        op_reason = _string(op, "reason", 1500)
        op_rule = _string(op, "rule", 120)
        evidence = _string(op, "evidence", 1000)
        if evidence not in text:
            raise ValueError("模型证据不在本轮输入中，本轮未保存")
        old = ws.current(key)
        if action == "forget":
            changes.append(ws.erase(key, n, op_reason, op_rule))
            continue
        if action in ("review", "invalidate"):
            if not old:
                raise ValueError("模型要求改变不存在的记忆状态")
            status = "REVIEW" if action == "review" else "SUPERSEDED"
            old.update(status=status, updated_turn=n, reason=op_reason)
            changes.append({"key": key, "label": old["label"], "category": old["category"], "action": "待确认" if action == "review" else "失效",
                            "before": old["value"], "after": old["value"], "status": status, "reason": op_reason, "rule": op_rule})
            continue
        if action != "upsert":
            raise ValueError("模型操作类型无效")
        category, status = op.get("category"), op.get("status")
        label, value = _string(op, "label", 100), _string(op, "value", 1200)
        if category not in CATEGORIES or status not in LIVE:
            raise ValueError("模型分类或状态无效")
        if old and old["status"] in ("REVIEW", "UNVERIFIED") and status == "ACTIVE" and op.get("confirmed") is not True:
            raise ValueError("模型未提供明确确认标记，不能把存疑条目升级为有效")
        if old and old["value"] == value and old["status"] == status:
            continue
        before = old["value"] if old else None
        for record in ws.live():
            if record["key"] == key:
                record.update(status="SUPERSEDED", updated_turn=n)
        ws.records.append({"id": f"{ws.id}:{key}:{n}", "owner_id": ws.owner_id, "key": key, "label": label, "category": category,
                           "value": value, "status": status, "source_turn": n, "updated_turn": n, "evidence": evidence, "reason": op_reason})
        changes.append({"key": key, "label": label, "category": category, "action": "更新" if old else "新增", "before": before,
                        "after": value, "status": status, "reason": op_reason, "rule": op_rule})
    entry = {"turn": n, "role": role, "speaker": ws.owner_name if role == "owner" else ROLE_LABELS[role],
             "input": "[本轮删除请求已执行，原文已清除]" if any(c["action"] == "遗忘" for c in changes) else text,
             "response": response, "decision": decision, "reason": reason, "rule": rule, "mode": "llm", "warnings": [], "used_keys": used_keys,
             "changes": changes, "model_call": metadata,
             "checks": [{"label": "输入来源", "value": ROLE_LABELS[role]}, {"label": "决策者", "value": metadata["model"]},
                        {"label": "实际操作", "value": f"{len(changes)}项变化" if changes else "未改动记忆"}], "snapshot": copy.deepcopy(ws.records)}
    if any(c["action"] == "遗忘" for c in changes):
        entry.update(response="[模型已执行删除，回应中的相关内容已清除]", reason="模型决定清除主人指定的记忆，相关细节不再保留")
        for change in entry["changes"]:
            if change["action"] == "遗忘":
                change["reason"] = "模型根据主人撤销请求决定删除，原值与相关细节已清除"
    ws.turns.append(entry)
    workspace.records, workspace.turns = ws.records, ws.turns
    return entry


def process(ws, text, role="owner", *, model=None, action=None, key=None):
    if role not in ROLE_LABELS:
        raise ValueError("未知输入来源")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 6000:
        raise ValueError("请输入1–6000字的对话")
    if model is None:
        raise ValueError("需要配置LLM；没有本地规则或离线替代结果")
    if action not in (None, "confirm", "erase") or (action and (role != "owner" or not ws.current(key))):
        raise ValueError("指定记忆操作或来源无效")
    safe_text = redact(text.strip())
    result, metadata = model(safe_text, role, ws, {"action": action, "key": key} if action else None)
    return apply_decision(ws, safe_text, role, result, metadata)


def export_markdown(ws, records=None):
    """Serialize AI memory content; do not fabricate facts or decisions."""
    records = ws.records if records is None else records
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    models = sorted({t.get("model_call", {}).get("model", "") for t in ws.turns} - {""})
    lines = [f"# {ws.owner_name}的角色记忆", "", f"> owner_id: {ws.owner_id} · LLM真实运行 · 模型: {', '.join(models)}", "",
             "ACTIVE为有效；REVIEW为持续有效性待确认；UNVERIFIED为真实性待核实。失效历史不作为当前记忆输出。", ""]
    for category, label in CATEGORIES.items():
        group = [r for r in records if r["category"] == category and r["status"] in LIVE]
        if not group:
            continue
        lines.extend([f"## {label}", "", "| 属性 | 内容 | 状态 | 来源轮次 |", "| --- | --- | --- | --- |"])
        lines.extend(f"| {cell(r['label'])} | {cell(r['value'])} | {r['status']} | {r['source_turn']} |" for r in group)
        lines.append("")
    deleted = sorted({r["label"] for r in records if r["status"] == "DELETED"})
    if deleted:
        lines.extend(["## 删除标记", ""] + [f"- {cell(label)}：内容已清除。" for label in deleted] + [""])
    return "\n".join(lines) + "\n"
