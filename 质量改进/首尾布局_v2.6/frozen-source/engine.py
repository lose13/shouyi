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


def _scrub(value, strings):
    """Remove identified erased text from nested stored output, including this turn."""
    if isinstance(value, str):
        for original in sorted(strings, key=len, reverse=True):
            value = value.replace(original, "[已清除]")
        return value
    if isinstance(value, list):
        return [_scrub(x, strings) for x in value]
    if isinstance(value, dict):
        return {k: _scrub(v, strings) for k, v in value.items()}
    return value


def _erased_strings(records, key):
    strings = set()
    for record in records:
        if record["key"] != key or record["status"] == "DELETED":
            continue
        strings.update(v for v in (record["value"], record.get("evidence", "")) if len(v) >= 2)
        # Temporal fields are removed by key. Shared phrases such as "下个月"
        # must not become global erasure tokens for unrelated appointments.
    return strings


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
        strings = _erased_strings(self.records, key)

        def clear(record):
            record.update(value="[已清除]", evidence="", reason="已按删除决策清除", status="DELETED", updated_turn=n)
            record.pop("validity", None)

        for record in matches:
            clear(record)
        for i, turn in enumerate(self.turns):
            touched = turn["turn"] in source_turns or key in turn.get("used_keys", [])
            for change in turn["changes"]:
                if change["key"] == key:
                    change.update(before=None, after="[已清除]", reason="相关内容已清除")
                    change.pop("before_validity", None)
                    change.pop("after_validity", None)
                    touched = True
            for record in turn["snapshot"]:
                if record["key"] == key:
                    clear(record)
            if touched:
                turn.update(input="[该轮相关原文已按删除请求清除]", response="[相关回复已清除]", reason="相关文本已清除，保留操作类型")
                turn["warnings"] = []
            self.turns[i] = _scrub(turn, strings)
        return {"key": key, "label": sample["label"], "category": sample["category"], "action": "遗忘",
                "before": None, "after": "内容已清除", "status": "DELETED", "reason": reason, "rule": rule}


def _string(obj, name, limit=2000, empty=False, path=""):
    value = obj.get(name)
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        requirement = f"需为{'可为空的' if empty else '非空'}字符串，最多{limit}字符"
        raise ValueError(f"{path}{name}：{requirement}")
    return redact(value.strip())


def _validity(op, turn, path=""):
    """Validate AI-produced temporal metadata without interpreting dates or text."""
    value = op.get("validity")
    if not isinstance(value, dict) or set(value) != {"kind", "reference_turn", "time_text", "end_condition"}:
        raise ValueError(path + "validity：需且仅含kind/reference_turn/time_text/end_condition四个字段")
    anchor = value.get("reference_turn")
    if value.get("kind") not in ("ongoing", "temporary"):
        raise ValueError(path + "validity.kind：仅允许ongoing或temporary")
    if type(anchor) is not int or not 1 <= anchor <= turn:
        raise ValueError(f"{path}validity.reference_turn：需为1至{turn}的整数")
    return {"kind": value["kind"], "reference_turn": anchor,
            "time_text": _string(value, "time_text", 500, empty=True, path=path + "validity."),
            "end_condition": _string(value, "end_condition", 800, empty=True, path=path + "validity.")}


def validity_text(value):
    if not isinstance(value, dict):
        return "未标注（旧运行未提供时效）"
    label = "短期有效" if value.get("kind") == "temporary" else "持续适用（可变更）"
    parts = [label, f"参照第{value['reference_turn']}轮"]
    parts.append("时间：" + value["time_text"] if value.get("time_text") else "未明确日期")
    if value.get("end_condition"):
        parts.append("结束条件：" + value["end_condition"])
    return "；".join(parts)


def apply_decision(workspace, text, role, result, metadata):
    """Apply the AI-selected operations atomically after structural/authority checks."""
    if not isinstance(result, dict):
        raise ValueError("模型未返回JSON对象")
    decision = result.get("decision")
    if decision not in ("change", "block", "ignore", "query", "clarify"):
        raise ValueError("decision：仅允许change/block/ignore/query/clarify")
    reason = _string(result, "reason", 1500)
    response = _string(result, "response", 6000)
    rule = _string(result, "rule", 120)
    operations = result.get("operations")
    used_keys = result.get("used_keys", [])
    if not isinstance(operations, list) or len(operations) > 40:
        raise ValueError("operations：需为数组，最多40项")
    if not isinstance(used_keys, list) or any(not isinstance(k, str) for k in used_keys):
        raise ValueError("used_keys：需为字符串数组")
    if role != "owner" and operations:
        raise ValueError("operations：当前来源无主人档案写权限，需为空数组")
    if decision in ("block", "ignore", "query", "clarify") and operations:
        raise ValueError("decision/operations：仅change可携带实际操作")
    if decision == "change" and not operations:
        raise ValueError("decision/operations：change必须携带至少一项实际操作")
    # Optional diagnostics are not executable memory and cannot gate a transaction.
    ws = copy.deepcopy(workspace)
    n, changes, seen, erased_strings = len(ws.turns) + 1, [], set(), set()
    for index, op in enumerate(operations):
        path = f"operations[{index}]."
        if not isinstance(op, dict):
            raise ValueError(f"operations[{index}]：需为操作对象")
        action = op.get("action")
        key = _string(op, "key", 100, path=path)
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", key):
            raise ValueError(path + "key：需为小写字母开头的有效属性ID")
        path = f"operations[{index}]({key})."
        if key in seen:
            raise ValueError(path + "key：本轮同一属性已有操作，请合并为一项")
        seen.add(key)
        op_reason = _string(op, "reason", 1500, path=path)
        op_rule = _string(op, "rule", 120, path=path)
        evidence = _string(op, "evidence", 1000, path=path)
        if evidence not in text:
            raise ValueError(path + "evidence：需逐字取自本轮input，不可改写或引用历史原文")
        old = ws.current(key)
        if action == "forget":
            if not any(r["key"] == key and r["status"] != "DELETED" for r in ws.records):
                raise ValueError(path + "key：不存在可删除的当前或历史属性，不能虚构删除")
            erased_strings.update(_erased_strings(ws.records, key))
            changes.append(ws.erase(key, n, op_reason, op_rule))
            continue
        if action in ("review", "invalidate"):
            if not old:
                raise ValueError(path + "key：没有可复核或失效的当前记忆")
            status = "REVIEW" if action == "review" else "SUPERSEDED"
            validity = copy.deepcopy(old.get("validity"))
            old.update(status=status, updated_turn=n, reason=op_reason)
            changes.append({"key": key, "label": old["label"], "category": old["category"], "action": "待确认" if action == "review" else "失效",
                            "before": old["value"], "after": old["value"], "status": status, "reason": op_reason, "rule": op_rule,
                            "before_validity": validity, "after_validity": copy.deepcopy(validity)})
            continue
        if action != "upsert":
            raise ValueError(path + "action：仅允许upsert/review/invalidate/forget")
        category, status = op.get("category"), op.get("status")
        label, value = _string(op, "label", 100, path=path), _string(op, "value", 1200, path=path)
        validity = _validity(op, n, path=path)
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ValueError(path + "category：仅允许FACT/HEALTH/GOAL/PREFERENCE/EVENT/SENSITIVE")
        if status not in LIVE:
            raise ValueError(path + "status：upsert仅允许ACTIVE/REVIEW/UNVERIFIED")
        if old and old["status"] in ("REVIEW", "UNVERIFIED") and status == "ACTIVE" and op.get("confirmed") is not True:
            raise ValueError(path + "confirmed：由存疑状态写入ACTIVE需显式true，且新值须有主人明确证据")
        if old and old["value"] == value and old["status"] == status and old.get("validity") == validity and old["label"] == label and old["category"] == category:
            continue
        before = old["value"] if old else None
        before_validity = copy.deepcopy(old.get("validity")) if old else None
        for record in ws.live():
            if record["key"] == key:
                record.update(status="SUPERSEDED", updated_turn=n)
        ws.records.append({"id": f"{ws.id}:{key}:{n}", "owner_id": ws.owner_id, "key": key, "label": label, "category": category,
                           "value": value, "status": status, "source_turn": n, "updated_turn": n, "evidence": evidence, "reason": op_reason,
                           "validity": validity})
        changes.append({"key": key, "label": label, "category": category, "action": "更新" if old else "新增", "before": before,
                        "after": value, "status": status, "reason": op_reason, "rule": op_rule,
                        "before_validity": before_validity, "after_validity": copy.deepcopy(validity)})
    entry = {"turn": n, "role": role, "speaker": ws.owner_name if role == "owner" else ROLE_LABELS[role],
             "input": "[本轮删除请求已执行，原文已清除]" if any(c["action"] == "遗忘" for c in changes) else text,
             "response": response, "decision": decision, "reason": reason, "rule": rule, "mode": "llm", "warnings": [], "used_keys": used_keys,
             "changes": changes, "model_call": metadata,
             "checks": [{"label": "输入来源", "value": ROLE_LABELS[role]}, {"label": "决策者", "value": metadata["model"]},
                        {"label": "实际操作", "value": f"{len(changes)}项变化" if changes else "未改动记忆"}], "snapshot": copy.deepcopy(ws.records)}
    if metadata.get("review_verdict") in ("accept", "revise"):
        entry["checks"].append({"label": "模型复核", "value": "修正后提交" if metadata["review_verdict"] == "revise" else "复核后提交"})
    if metadata.get("format_repairs"):
        entry["checks"].append({"label": "格式修正", "value": "模型修正一次后通过校验"})
    if any(c["action"] == "遗忘" for c in changes):
        entry.update(response="[模型已执行删除，回应中的相关内容已清除]", reason="模型依据守则决定清除记忆，相关细节不再保留")
        for change in entry["changes"]:
            if change["action"] == "遗忘":
                change["reason"] = "模型依据守则决定清除，原值与相关细节已清除"
        ws.records = _scrub(ws.records, erased_strings)
        entry = _scrub(entry, erased_strings)
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
             "ACTIVE为有效；REVIEW为持续有效性待确认；UNVERIFIED为真实性待核实。失效历史不作为当前记忆输出。",
             "时效是独立维度；短期有效不等于不重要。参照轮次保留原说话时点；缺少字段的旧结果显示未标注，不补推日期。", ""]
    for category, label in CATEGORIES.items():
        group = [r for r in records if r["category"] == category and r["status"] in LIVE]
        if not group:
            continue
        lines.extend([f"## {label}", "", "| 属性 | 内容 | 状态 | 时效与结束条件 | 来源轮次 |", "| --- | --- | --- | --- | --- |"])
        lines.extend(f"| {cell(r['label'])} | {cell(r['value'])} | {r['status']} | {cell(validity_text(r.get('validity')))} | {r['source_turn']} |" for r in group)
        lines.append("")
    deleted = sorted({r["label"] for r in records if r["status"] == "DELETED"})
    if deleted:
        lines.extend(["## 删除标记", ""] + [f"- {cell(label)}：内容已清除。" for label in deleted] + [""])
    return "\n".join(lines) + "\n"
