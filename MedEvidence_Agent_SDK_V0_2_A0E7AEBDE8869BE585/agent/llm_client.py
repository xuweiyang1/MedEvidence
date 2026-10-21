"""DashScope/Qwen OpenAI-compatible chat endpoint using stdlib HTTPS."""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.request
from typing import Any


class LLMError(RuntimeError):
    pass


def parse_json(raw: str) -> dict[str, Any]:
    t = raw.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.endswith("```"):
            t = t[:-3].strip()
    try:
        out = json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            raise LLMError("模型未返回 JSON 对象；请检查 model 或 max_tokens") from None
        try:
            out = json.loads(t[i:j + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型返回的 JSON 无法解析: {exc}") from exc
    if not isinstance(out, dict):
        raise LLMError("模型 JSON 顶层必须是 object")
    return out


class QwenLLM:
    def __init__(self, api_key: str, model: str, api_base: str, timeout: int = 90):
        if not api_key:
            raise LLMError("缺少 DASHSCOPE_API_KEY。请先复制 .env.agent.example 为 .env.agent 并填写 key")
        if not api_base.startswith("https://"):
            raise LLMError("API 地址必须是 https://")
        self.api_key = api_key
        self.model = model
        self.url = api_base.rstrip("/") + "/chat/completions"
        self.timeout = timeout

    def json(self, system: str, user: str, max_tokens: int = 1400) -> dict[str, Any]:
        payload = json.dumps({
            "model": self.model, "messages": [
                {"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0, "max_tokens": max_tokens,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=payload, headers={
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }, method="POST")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as rsp:
                    data = json.load(rsp)
                choices = data.get("choices", [])
                if not choices:
                    raise LLMError("API 无 choices 字段")
                content = choices[0].get("message", {}).get("content", "")
                if not isinstance(content, str):
                    raise LLMError("API 无文本 content")
                return parse_json(content)
            except urllib.error.HTTPError as exc:
                body = exc.read(500).decode("utf-8", "replace")
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1 + attempt * 2)
                    continue
                raise LLMError(f"API HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < 2:
                    time.sleep(1 + attempt * 2)
                    continue
                raise LLMError(f"API 网络错误: {exc}") from exc
        raise LLMError("API 重试次数耗尽")
