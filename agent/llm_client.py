"""DashScope/Qwen OpenAI-compatible chat endpoint using stdlib HTTPS."""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.request
from typing import Any

REPAIR_PROMPT = (
    "上一条输出无法解析为 JSON（{error}）。请只输出一个合法 JSON 对象，"
    "不要 Markdown 代码块、不要任何解释，并保持原任务要求的字段与数量限制。"
)


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
        # Accumulated across every successful response; used for cost reporting.
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.last_error = ""

    def _post(self, messages: list[dict[str, str]], max_tokens: int) -> str:
        payload = json.dumps({
            "model": self.model, "messages": messages,
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
                if not isinstance(content, str) or not content.strip():
                    raise LLMError("API 无文本 content")
                usage = data.get("usage") or {}
                self.usage["calls"] += 1
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = usage.get(key)
                    if isinstance(value, int):
                        self.usage[key] += value
                return content
            except urllib.error.HTTPError as exc:
                body = exc.read(300).decode("utf-8", "replace")
                self.last_error = f"HTTP {exc.code}"
                if exc.code in (408, 409, 429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1 + attempt * 2)
                    continue
                raise LLMError(f"API HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self.last_error = type(exc).__name__
                if attempt < 2:
                    time.sleep(1 + attempt * 2)
                    continue
                raise LLMError(f"API 网络错误: {exc}") from exc
        raise LLMError("API 重试次数耗尽")

    def json(self, system: str, user: str, max_tokens: int = 1400) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        content = self._post(messages, max_tokens)
        try:
            return parse_json(content)
        except LLMError as exc:
            self.last_error = "json_parse: " + str(exc)[:200]
            error_text = str(exc)[:200]
        # One repair round: the model sees its own invalid output plus the parse error.
        messages.extend([
            {"role": "assistant", "content": content[:4000]},
            {"role": "user", "content": REPAIR_PROMPT.format(error=error_text)},
        ])
        return parse_json(self._post(messages, max_tokens))
