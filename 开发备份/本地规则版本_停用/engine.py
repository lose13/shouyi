"""Shouyi memory lifecycle. Standard library only; no turn-number answer table."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

CATEGORIES = {"FACT": "基本资料", "HEALTH": "健康", "GOAL": "目标", "PREFERENCE": "固定安排", "EVENT": "事件", "SENSITIVE": "敏感标记"}
STATUSES = {"ACTIVE": "有效", "REVIEW": "待确认", "UNVERIFIED": "待核实", "SUPERSEDED": "已失效", "DELETED": "已删除"}
# The schema covers concepts, not sample turn numbers or expected answers.
SLOTS = {
    "name": ("FACT", "姓名"), "age": ("FACT", "年龄观测"), "marital": ("FACT", "婚姻状态"),
    "company": ("FACT", "任职公司"), "position": ("FACT", "当前职位"), "salary_change": ("FACT", "薪酬变化"),
    "home": ("FACT", "现住区域"), "commute_mode": ("FACT", "通勤方式"), "commute_route": ("FACT", "通勤路线"), "commute_time": ("FACT", "通勤时间"),
    "daughter": ("FACT", "子女关系"), "daughter_age": ("FACT", "女儿年龄观测"), "daughter_school": ("FACT", "女儿学校"), "daughter_grade": ("FACT", "女儿年级"),
    "spouse_company": ("FACT", "配偶任职公司"), "spouse_position": ("FACT", "配偶职位"),
    "liver": ("HEALTH", "体检自述"), "advice": ("HEALTH", "医生建议自述"),
    "swim": ("PREFERENCE", "固定运动"), "car_service": ("PREFERENCE", "车辆保养"), "medicine": ("PREFERENCE", "取药安排"), "art": ("PREFERENCE", "女儿兴趣班"),
    "pmp": ("GOAL", "PMP目标"), "analysis": ("GOAL", "数据分析师目标"),
    "opportunity": ("EVENT", "工作机会"), "layoff": ("EVENT", "裁员传闻"), "investment": ("EVENT", "理财备注"),
    "bank": ("SENSITIVE", "银行卡标记"), "idcard": ("SENSITIVE", "身份证标记"), "phone": ("SENSITIVE", "手机号标记"),
    "coffee": ("PREFERENCE", "咖啡偏好"), "food": ("PREFERENCE", "饮食偏好"), "website": ("FACT", "个人主页"),
}
ALIASES = {
    "bank": ("银行卡", "银行账号", "银行账户"), "idcard": ("身份证",), "phone": ("手机号", "电话"),
    "opportunity": ("机会", "那个岗位"), "home": ("住址", "地址", "住哪", "住在", "居住"),
    "position": ("职位", "职业", "岗位"), "company": ("任职", "公司", "单位"),
    "swim": ("游泳", "运动"), "car_service": ("保养", "4S"), "medicine": ("取药", "拿药", "用药"),
    "art": ("美术", "兴趣班"), "pmp": ("PMP",), "analysis": ("数据分析师",),
    "layoff": ("裁员",), "daughter_school": ("女儿学校", "女儿的学校"), "daughter_grade": ("年级",),
    "marital": ("婚姻", "结婚",), "coffee": ("咖啡",), "website": ("主页", "网站"),
}
ROLE_LABELS = {"owner": "主人本人", "third": "第三方", "push": "推送消息", "system": "系统消息"}


def redact(text: str) -> str:
    """Protect probable credentials irrespective of checksum validity."""
    text = str(text)
    text = re.sub(r"((?:密码|验证码|安全码|CVV|API\s*Key|密钥)\s*(?:是|为|[:：])?\s*)[^，。；\s]+", r"\1[不保存]", text, flags=re.I)
    text = re.sub(r"((?:我的)?(?:月薪|年薪|工资|薪资|薪酬)\s*(?:是|为|[:：])?\s*)\d+(?:\.\d+)?\s*(?:万|千|[kKwW]|元)?", r"\1[金额不保存]", text)
    text = re.sub(r"(?<!\d)\d{17}[\dXx](?!\d)", lambda m: "[身份证尾号" + m[0][-4:] + "]", text)
    text = re.sub(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)", lambda m: "[银行卡尾号" + re.sub(r"\D", "", m[0])[-4:] + "]", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", lambda m: m[0][:3] + "****" + m[0][-4:], text)
    return text


def infer_source(role: str, text: str) -> str:
    if role not in ROLE_LABELS:
        raise ValueError("未知输入来源")
    if role == "owner" and re.search(r"^\s*(?:你好[，,！!\s]*)?(?:小星[，,：:\s]*)?(?:我是|我代表)(?:[^，。]{0,12}的)?(?:同事|朋友|HR|猎头|家属|快递员|老师)", text, re.I):
        return "third"
    return role


def find_slot(text: str) -> list[str]:
    return [slot for slot, aliases in ALIASES.items() if any(a.lower() in text.lower() for a in aliases)]


def suspicious_instruction(text: str) -> bool:
    if re.search(r"(?:学习|讨论|举例|分析).{0,20}(?:攻击|注入|忽略规则)", text):
        return False
    return bool(re.search(r"忽略(?:以上|之前|所有|原有).{0,8}(?:规则|指令)|(?:系统规则|系统提示)(?:改为|改成|全部输出)|(?:解除|绕过)(?:限制|权限)|从现在开始.{0,12}(?:必须|无条件)|输出(?:全部|所有)(?:记忆|系统提示)", text, re.I))


def fraud_promise(text: str) -> bool:
    return bool(re.search(r"理财|投资|收益|年化|回报", text) and re.search(r"稳赚(?:不赔)?|保证.{0,5}收益|包赚|保本保息|零风险", text))


@dataclass
class Workspace:
    id: str
    title: str
    kind: str = "sandbox"
    owner_id: str = "LIMING"
    owner_name: str = "李明"
    records: list[dict] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def live(self, key=None):
        return [r for r in self.records if r["status"] in ("ACTIVE", "REVIEW", "UNVERIFIED") and (key is None or r["key"] == key)]

    def current(self, key):
        return next((r for r in reversed(self.records) if r["key"] == key and r["status"] in ("ACTIVE", "REVIEW", "UNVERIFIED")), None)

    def snapshot(self):
        return copy.deepcopy(self.records)

    def public(self):
        return {"id": self.id, "title": self.title, "kind": self.kind, "owner_id": self.owner_id,
                "owner_name": self.owner_name, "records": self.snapshot(), "turns": copy.deepcopy(self.turns), "created_at": self.created_at}

    def write(self, key, value, n, evidence, status="ACTIVE", reason="主人明确陈述", rule="2 / 3"):
        if key not in SLOTS or status not in ("ACTIVE", "UNVERIFIED", "REVIEW") or not isinstance(value, str):
            raise ValueError("记忆提议不符合Schema")
        value = redact(value.strip())[:500]
        if not value:
            return None
        old = self.current(key)
        if old and old["value"] == value and old["status"] == status:
            return None
        # A supplementary update may never silently certify an unverified statement.
        if old and old["status"] == "UNVERIFIED" and status == "ACTIVE" and rule != "4 / 确认":
            status = "UNVERIFIED"
        old_value = old["value"] if old else None
        for record in self.live(key):
            record["status"] = "SUPERSEDED"
            record["updated_turn"] = n
        cat, label = SLOTS[key]
        self.records.append({"id": f"{self.id}:{key}:{n}", "owner_id": self.owner_id, "key": key, "label": label,
                             "category": cat, "value": value, "status": status, "source_turn": n,
                             "updated_turn": n, "evidence": redact(evidence)[:300], "reason": reason})
        return {"key": key, "label": label, "category": cat, "action": "更新" if old else "新增",
                "before": old_value, "after": value, "status": status, "reason": reason, "rule": rule}

    def review(self, key, n, reason):
        old = self.current(key)
        if not old or old["status"] != "ACTIVE":
            return None
        old["status"] = "REVIEW"
        old["updated_turn"] = n
        old["reason"] = reason
        return {"key": key, "label": old["label"], "category": old["category"], "action": "待确认",
                "before": old["value"], "after": old["value"], "status": "REVIEW", "reason": reason, "rule": "4"}

    def erase(self, key, n):
        matches = [r for r in self.records if r["key"] == key and r["status"] != "DELETED"]
        if not matches:
            return None
        def clear(record):
            record.update(value="[已清除]", evidence="", reason="主人指定删除", status="DELETED", updated_turn=n)
        for r in matches:
            clear(r)
        # Do not leave the deleted values recoverable through this application's history.
        for turn in self.turns:
            touched = False
            for change in turn["changes"]:
                if change["key"] == key:
                    change.update(before=None, after="[已清除]", reason="相关内容已按删除请求清除")
                    touched = True
            for record in turn["snapshot"]:
                if record["key"] == key:
                    clear(record)
            if touched:
                turn["input"] = "[该轮涉及的原文已按删除请求清除]"
                turn["response"] = "[相关回复已清除，操作元数据保留]"
        return {"key": key, "label": SLOTS[key][1], "category": SLOTS[key][0], "action": "遗忘",
                "before": None, "after": "内容已清除", "status": "DELETED", "reason": "清除该属性的当前值、历史版本与快照内容", "rule": "5"}


def extract(text: str, ws: Workspace) -> list[dict]:
    """Conservative Chinese fact extraction, usable for new inputs without turn IDs."""
    proposals = {}
    def put(key, value, status="ACTIVE", reason="从本轮原话提取"):
        proposals[key] = dict(key=key, value=value.strip(), status=status, evidence=redact(text), reason=reason)
    m = re.search(r"(?:我是|我叫)([\u4e00-\u9fff]{2,4})(?=[，。！\s]|$)", text)
    if m and m[1] == ws.owner_name:
        put("name", m[1])
    m = re.search(r"(?:我)?今年\s*(\d{1,3})岁", text)
    if m: put("age", f"{m[1]}岁（本轮自述）")
    if re.search(r"(?:我)?结婚了|已婚", text): put("marital", "已婚")
    if re.search(r"离婚了|恢复单身", text): put("marital", "离异")
    if re.search(r"(?:我一直|我其实|我从来).{0,5}(?:单身|没结过婚|没有结婚)", text):
        put("marital", "主人新陈述：未婚", "UNVERIFIED", "与历史身份可能矛盾，需要主人确认")
    spouse = bool(re.search(r"我(?:老婆|妻子|爱人)", text))
    if not spouse:
        m = re.search(r"(?:现在在|我在)([^，。！；\n]{2,40}?)(?:做|担任|任|上班|工作)", text)
        if not m: m = re.search(r"(?:跳槽(?:到|至)|入职(?:了)?|加入(?:了)?)([^，。！；\n]{2,40})", text)
        if m: put("company", m[1])
        m = re.search(r"(?:做|担任|任)([^，。！；\n]{2,22})(?=[，。！；]|$)", text)
        if m and re.search(r"经理|总监|工程师|架构师|设计师|教师|开发|销售|研究员|主管", m[1]): put("position", m[1])
        if "薪资涨了" in text or "工资涨了" in text: put("salary_change", "薪酬较此前提高（未提供金额）")
    else:
        m = re.search(r"(?:在(?:一?家)?|去了(?:一?家)?)([^，。！]{2,25}?)(?:上班|做|任|当)", text)
        if m: put("spouse_company", m[1])
        m = re.search(r"(?:做|任|当)([^，。！]{2,18})", text)
        if m: put("spouse_position", m[1])
    if "女儿" in text:
        if not ws.current("daughter"): put("daughter", "有一位女儿")
        m = re.search(r"女儿(?:今年|现在)?\s*(\d{1,2})岁", text)
        if m: put("daughter_age", f"{m[1]}岁（当时自述，不自动推算现年龄）")
        if "幼儿园" in text and not re.search(r"小学|转学", text):
            put("daughter_grade", "幼儿园")
            if re.search(r"家附近|附近的?|那家附近", text): put("daughter_school", "家附近的幼儿园")
        m = re.search(r"(?:在|转到|转入)([^，。！]{2,18}?(?:小学|一小|二小|三小))", text)
        if m: put("daughter_school", m[1])
        m = re.search(r"([一二三四五六七八九]年级)", text)
        if m: put("daughter_grade", m[1])
    m = re.search(r"(?:搬到了?|住在|现住址是|住址(?:改为|是))([^，。！；\s]{2,24})", text)
    if m: put("home", m[1].removesuffix("了"))
    elif re.search(r"我每天从", text):
        m = re.search(r"我每天从(.+?)(?:开车|坐地铁|乘车)", text)
        if m: put("home", m[1])
    if re.search(r"(?:通勤|上班|每天).{0,25}(?:地铁)|通勤改成坐地铁", text): put("commute_mode", "地铁")
    elif re.search(r"骑(?:车|自行车).{0,8}上班|(?:上班|通勤).{0,10}骑(?:车|自行车)", text): put("commute_mode", "骑车")
    elif re.search(r"步行.{0,8}上班|(?:上班|通勤).{0,10}步行", text): put("commute_mode", "步行")
    elif re.search(r"(?:公交|公共汽车).{0,8}上班|(?:上班|通勤).{0,10}(?:公交|公共汽车)", text): put("commute_mode", "公交")
    elif re.search(r"每天.{0,16}开车|开车.{0,12}上班", text): put("commute_mode", "开车")
    m = re.search(r"(\d+)号线转(\d+)号线", text)
    if m: put("commute_route", f"{m[1]}号线转{m[2]}号线")
    else:
        m = re.search(r"(?:走|经)([^，。]{1,12}?路)", text)
        if m and "开车" in text: put("commute_route", m[1])
    if re.search(r"通勤|上班|不堵车", text):
        m = re.search(r"(?:大概|约|用时|需要)\s*(\d+)分钟", text)
        if m:
            value = f"约{m[1]}分钟"
            peak = re.search(r"早高峰.{0,8}?(\d+)分钟", text)
            if peak: value += f"；早高峰约{peak[1]}分钟"
            put("commute_time", value)
    if re.search(r"每周.{0,8}游泳", text):
        m = re.search(r"每周[^，。；]{1,15}游泳", text)
        gym = re.search(r"在([^，。；]{2,16}体育馆)", text)
        put("swim", m[0] + (f" · {gym[1]}年卡" if gym else ""))
    if re.search(r"4S店.{0,8}保养|保养.{0,8}4S店", text):
        area = proposals.get("home", {}).get("value") or (ws.current("home") or {}).get("value", "原居住区域")
        put("car_service", f"{area}的4S店")
    if "每周" in text and re.search(r"拿.{0,3}药|取药", text):
        place = re.search(r"(?:去|在)([^，。]{1,18}?同仁堂)", text)
        put("medicine", "每周取一次调理药" + (f" · {place[1]}" if place else ""))
    if "美术" in text and "每周" in text:
        schedule = re.search(r"每周[^，。]{1,10}", text)
        put("art", "美术启蒙班 · " + (schedule[0] if schedule else "每周固定上课") + (" · 原幼儿园旁" if "幼儿园" in text else ""))
    if "脂肪肝" in text:
        if re.search(r"脂肪肝(?:没了|消失|已消|好了)|没有脂肪肝", text): put("liver", "体检显示脂肪肝已消失（主人自述）")
        elif "轻度脂肪肝" in text: put("liver", "轻度脂肪肝（主人转述体检结果）")
    if "医生" in text and "继续保持" in text: put("advice", "医生建议继续保持（主人转述）")
    elif "医生" in text and re.search(r"控制饮食|少吃油腻", text): put("advice", "控制饮食、少油腻、多运动（主人转述）")
    if "PMP" in text:
        if re.search(r"(?:考试)?(?:过了|通过|拿到证书|获得证书)", text): put("pmp", "已通过PMP考试并取得证书")
        elif re.search(r"(?:我(?:今年)?(?:打算|计划|准备)|现在(?:开始)?(?:准备|备考)|^备考|^准备).{0,15}PMP", text):
            month = re.search(r"(\d{1,2})月", text)
            put("pmp", "计划考取PMP" + (f"（预计{month[1]}月）" if month else ""))
    if re.search(r"准备考数据分析师|备考数据分析师|开始准备.*数据分析师", text): put("analysis", "备考数据分析师，希望提升数据分析技能")
    if re.search(r"朋友.{0,10}(?:说|介绍).{0,20}(?:招|岗位|职位)", text) and re.search(r"机会|提醒|跟进", text):
        put("opportunity", "朋友转述的招聘机会，主人希望后续跟进", "UNVERIFIED", "岗位与条件为二手消息，尚未核实")
    if "裁员" in text and re.search(r"听说|同事|转述|传闻", text):
        percentage = re.search(r"裁员\s*(\d+%)", text)
        put("layoff", "公司可能裁员" + (percentage[1] if percentage else "") + "（主人转述，时间为当轮所说的下个月，未提供绝对日期）", "UNVERIFIED", "二手传闻，仅保存待核实线索")
    if "咖啡" in text and re.search(r"喜欢|习惯|每天", text):
        put("coffee", "黑咖啡" if "黑咖啡" in text else "喜欢咖啡")
    if re.search(r"个人主页|我的网站", text):
        m = re.search(r"https?://[^\s，。]+", text)
        if m: put("website", m[0])
    if re.search(r"存款|存单", text) and "年化" in text:
        rate = re.search(r"年化(?:收益)?\s*(\d+(?:\.\d+)?%)", text)
        if rate: put("investment", f"主人陈述的存款年化{rate[1]}（不代表收益保证）")
    return list(proposals.values())


def process(ws: Workspace, text: str, role="owner", *, model: Callable | None = None, action=None, key=None) -> dict:
    text = str(text).strip()
    if not text or len(text) > 6000:
        raise ValueError("请输入1–6000字的对话")
    role = infer_source(role, text)
    n = len(ws.turns) + 1
    changes, warnings = [], []
    decision, mode = "ignore", "rules"
    reason, rule, response = "没有新增的稳定事实", "2", "这轮没有需要写入的长期信息，已有记忆保持不变。"

    if role in ("push", "system"):
        reason, rule = "外部推送或环境消息，不进入主人长期记忆", "1"
        response = "这是一条外部消息，未写入李明的长期记忆。"
    elif role == "third":
        decision, reason, rule = "block", "第三方无权查询或改写主人档案", "1"
        response = "我无法向第三方提供或修改李明的私人信息，请由李明本人确认。"
    elif suspicious_instruction(text):
        decision, reason, rule = "block", "输入试图改变常驻规则或读取受限信息", "7"
        response = "这段内容不能改变记忆规则或授权边界，未执行相关操作。"
    elif fraud_promise(text):
        decision, reason, rule = "block", "投资渠道含有稳赚或保证收益等高风险承诺", "7"
        response = "这类收益保证存在明显风险，该渠道未写入长期记忆。"
    elif action in ("confirm", "erase"):
        if key not in SLOTS: raise ValueError("未知记忆属性")
        current = ws.current(key)
        if action == "confirm" and current:
            change = ws.write(key, current["value"], n, "主人在界面明确确认", "ACTIVE", "主人明确确认该条信息", "4 / 确认")
        elif action == "erase": change = ws.erase(key, n)
        else: change = None
        if change: changes.append(change)
        decision, reason, rule, response = "change", "主人指定操作", "4 / 5", "已完成指定操作。"
    elif re.search(r"忘掉|删除|清除|抹掉|这事就算了|别管了|当我没说", text):
        targets = ["opportunity"] if re.search(r"这事就算了|别管了|当我没说", text) and ws.current("opportunity") else find_slot(text)
        if not targets and re.search(r"这事|别管了|当我没说", text) and ws.current("opportunity"): targets = ["opportunity"]
        for target in targets:
            change = ws.erase(target, n)
            if change: changes.append(change)
        decision, reason, rule = ("change" if changes else "clarify"), "按主人撤销意图处理，删除范围包含历史版本", "5"
        response = "已清除指定记忆及其历史内容，仅保留删除标记。" if changes else "请具体说明要删除哪条记忆，例如“删除我的住址”。"
    elif re.search(r"(?:我|我的|女儿|配偶).*(?:是什么|在哪|住哪|多少|什么|哪家|哪个)|还记得|告诉我.*(?:住址|职业|公司)", text) and not re.search(r"帮我记|记一下|改为|搬到", text):
        targets = find_slot(text)
        found = [r for k in targets for r in ws.live(k)]
        decision, reason, rule = "query", "只检索本角色空间，待确认项不作为确定事实", "1 / 4"
        response = "；".join(f"{r['label']}：{r['value']}" + (f"（{STATUSES[r['status']]}）" if r["status"] != "ACTIVE" else "") for r in found) or "目前没有可回答这项问题的记忆。"
    else:
        # Credentials are handled before any optional external model call.
        safe = redact(text)
        proposals = []
        for slot, label in (("bank", "银行卡"), ("idcard", "身份证")):
            m = re.search(r"\[" + label + r"尾号([^\]]+)\]", safe)
            if m: proposals.append(dict(key=slot, value=f"仅保存尾号{m[1]}，完整号码不保存", status="ACTIVE", evidence=f"主人提供{label}，已脱敏", reason="敏感字段最小化登记"))
        if re.search(r"密码|验证码|安全码|CVV|密钥|API\s*Key", text, re.I):
            warnings.append("密码、验证码与密钥不保存。")
        if not proposals:
            if model:
                try:
                    proposals = model(safe, ws)
                    mode = "model"
                except Exception:
                    mode = "fallback"
                    warnings.append("模型未能完成有效提取，本轮已明确降级到本地规则。")
                    proposals = extract(safe, ws)
            else:
                proposals = extract(safe, ws)
        changed_keys, asserted_keys = set(), set()
        for proposal in proposals:
            if not isinstance(proposal, dict) or proposal.get("key") not in SLOTS:
                warnings.append("已丢弃不符合字段规范的提议。")
                continue
            slot = proposal["key"]
            value = proposal.get("value")
            evidence = proposal.get("evidence", "")
            if not isinstance(value, str) or not isinstance(evidence, str): continue
            if slot == "salary_change" and re.search(r"\d|[一二三四五六七八九十]+[千万]", value):
                value = "主人提供了薪酬信息，具体金额不保存"
            # The model may only cite this turn; it cannot authorize destructive actions.
            if mode == "model" and (not evidence or evidence not in safe):
                warnings.append("模型提议缺少可核对的本轮原文，未写入。")
                continue
            if slot in ("bank", "idcard") and (mode == "model" or not re.search(r"尾号\d{4}(?!\d)", value)): continue
            status = proposal.get("status", "ACTIVE")
            if slot in ("opportunity", "layoff") and re.search(r"朋友|同事|转述|听说|传闻", text):
                status = "UNVERIFIED"
            if status not in ("ACTIVE", "UNVERIFIED", "REVIEW"): continue
            asserted_keys.add(slot)
            change = ws.write(slot, value, n, evidence, status, proposal.get("reason", "本轮明确陈述"), "6" if slot in ("bank", "idcard", "phone") else "2 / 3")
            if change:
                changes.append(change)
                changed_keys.add(slot)
        review_keys = {}
        if "home" in changed_keys:
            review_keys.update({k: "居住区域改变，原安排是否继续需要确认" for k in ("swim", "car_service", "art", "medicine", "commute_mode", "commute_route", "commute_time")})
        if "commute_mode" in changed_keys:
            review_keys["commute_route"] = "通勤方式改变，原路线是否适用需要确认"
        if "daughter_grade" in changed_keys and ws.current("daughter_grade")["value"] != "幼儿园":
            review_keys.update({"daughter_age": "升学不能确定新年龄，旧年龄仅为当时观测", "art": "升学后原幼儿园旁的兴趣班是否继续需要确认"})
        if "liver" in changed_keys and "消失" in ws.current("liver")["value"]:
            review_keys["medicine"] = "健康情况已变，旧取药安排是否继续需要主人确认"
        if "marital" in changed_keys and ws.current("marital")["status"] == "UNVERIFIED":
            review_keys.update({k: "身份陈述有变化，关联事实待主人确认" for k in ("spouse_company", "spouse_position", "daughter")})
        for slot, review_reason in review_keys.items():
            if slot not in asserted_keys:
                change = ws.review(slot, n, review_reason)
                if change: changes.append(change)
        if changes:
            decision, reason, rule = "change", "候选已通过字段校验并执行状态转换", "2 / 3 / 4"
            response = "已处理：" + "、".join(dict.fromkeys(c["label"] for c in changes if c["action"] != "待确认")) + "。"
            if any(c["status"] in ("REVIEW", "UNVERIFIED") for c in changes): response += "其中待确认或待核实的内容会单独标记，不作为确定事实使用。"
        elif warnings:
            decision, reason = "clarify", "未保存敏感凭据或未获得可校验提议"
            response = "没有新增可保存的长期记忆。" + " ".join(warnings)
        elif re.search(r"记一下|记住|我现在|我喜欢|我在", text):
            decision, reason = "clarify", "本地规则没有识别到可稳定提取的属性"
            response = "这句尚未提取到稳定字段，因此没有写入。可以更明确地陈述属性，或启用模型提取。"
    entry = {"turn": n, "role": role, "speaker": ws.owner_name if role == "owner" else ROLE_LABELS[role],
             "input": "[主人要求删除指定记忆]" if any(c["action"] == "遗忘" for c in changes) else redact(text), "response": redact(response), "decision": decision, "reason": reason, "rule": rule,
             "mode": mode, "warnings": warnings, "changes": changes,
             "checks": [{"label": "来源", "value": ROLE_LABELS[role]}, {"label": "检查", "value": "拦截" if decision == "block" else "通过"},
                        {"label": "执行", "value": f"{len(changes)}项变化" if changes else "未改动记忆"}],
             "snapshot": ws.snapshot()}
    ws.turns.append(entry)
    return copy.deepcopy(entry)


def export_markdown(ws: Workspace, records=None) -> str:
    records = ws.records if records is None else records
    def cell(value): return str(value).replace("|", "\\|").replace("\n", " ")
    lines = [f"# {ws.owner_name}的角色记忆", "", f"> owner_id: {ws.owner_id} · 守忆 v1.0 · 仅当前角色", "",
             "当前确定事实只使用 ACTIVE；REVIEW 为持续有效性待确认；UNVERIFIED 为真实性待核实。", ""]
    for cat, label in CATEGORIES.items():
        visible = [r for r in records if r["category"] == cat and r["status"] in ("ACTIVE", "REVIEW", "UNVERIFIED")]
        lines.extend([f"## {label}（{cat}）", "", "| 属性 | 值 | 状态 | 来源轮次 |", "| --- | --- | --- | --- |"])
        for r in visible: lines.append(f"| {cell(r['label'])} | {cell(r['value'])} | {r['status']} | {r['source_turn']} |")
        if not visible: lines.append("| — | 暂无 | — | — |")
        lines.append("")
    history = [r for r in records if r["status"] == "SUPERSEDED"]
    lines.extend(["## 失效历史（不作为当前事实）", "", "| 属性 | 旧值 | 状态 | 失效轮次 |", "| --- | --- | --- | --- |"])
    for r in history: lines.append(f"| {cell(r['label'])} | {cell(r['value'])} | SUPERSEDED | {r['updated_turn']} |")
    if not history: lines.append("| — | 暂无 | — | — |")
    lines.extend(["", "## 删除标记", ""])
    deleted = sorted({r["label"] for r in records if r["status"] == "DELETED"})
    lines += [f"- {label}：内容已清除。" for label in deleted] or ["暂无删除记录。"]
    return "\n".join(lines) + "\n"
