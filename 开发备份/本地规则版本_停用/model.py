"""Optional JSON extraction adapter. Credentials only live in server memory."""
import json
import re
import urllib.request
from urllib.parse import urlparse

from engine import SLOTS


class ModelClient:
    def __init__(self, rules):
        self.rules = rules
        self.base_url = ""
        self.model = ""
        self.api_key = ""
        self.enabled = False

    def public(self):
        return {"enabled": self.enabled, "configured": bool(self.base_url and self.model and self.api_key),
                "base_url": self.base_url, "model": self.model}

    def configure(self, body):
        url = str(body.get("base_url", "")).strip().rstrip("/")
        enabled = bool(body.get("enabled"))
        if url:
            parsed = urlparse(url)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("接口地址不能含凭据、查询参数或片段")
            if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
                raise ValueError("远程接口必须使用HTTPS，本机接口可使用HTTP")
        self.base_url = url
        self.model = str(body.get("model", "")).strip()[:100]
        if body.get("api_key"):
            self.api_key = str(body["api_key"]).strip()
        if body.get("clear_key"):
            self.api_key = ""
        if enabled and not (self.base_url and self.model and self.api_key):
            self.enabled = False
            raise ValueError("启用模型前，请填写接口地址、模型名和本次运行的密钥")
        self.enabled = enabled
        return self.public()

    def extract(self, safe_text, workspace):
        registry = {k: {"category": v[0], "label": v[1]} for k, v in SLOTS.items()}
        system = self.rules + "\n\n你只负责提出记忆候选，不决定权限或删除。只返回JSON：{\"proposals\":[{\"key\":\"合法字段ID\",\"value\":\"明确陈述的值\",\"status\":\"ACTIVE或UNVERIFIED或REVIEW\",\"evidence\":\"本轮输入中逐字存在的证据\",\"reason\":\"简短依据\"}]}。不要推断年龄或编造值。没有事实则空数组。字段表：" + json.dumps(registry, ensure_ascii=False)
        user = json.dumps({"owner": workspace.owner_name, "current": workspace.live(), "input": safe_text}, ensure_ascii=False)
        payload = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0}
        endpoint = self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"
        request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=35) as response:
            result = json.loads(response.read(2_000_000).decode())
        content = result["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        parsed = json.loads(content)
        proposals = parsed.get("proposals")
        if not isinstance(proposals, list) or len(proposals) > 30:
            raise ValueError("模型返回结构不符合约定")
        return proposals
