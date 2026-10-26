"""QwenLLM: JSON repair round, token accounting, and bounded network retries."""
from __future__ import annotations
import json
import unittest
import urllib.error
from unittest.mock import patch

from agent.llm_client import LLMError, QwenLLM


class FakeResponse:
    def __init__(self, content: str, usage: dict | None = None):
        self._body = json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": usage or {},
        }).encode("utf-8")

    def read(self, *_args):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def make_llm() -> QwenLLM:
    return QwenLLM("test-key", "qwen-plus", "https://example.invalid/v1", timeout=5)


class TestQwenLLM(unittest.TestCase):
    def test_valid_json_and_usage(self):
        llm = make_llm()
        payload = '{"checks": []}'
        with patch("agent.llm_client.urllib.request.urlopen",
                   return_value=FakeResponse(payload, {"prompt_tokens": 11,
                                                       "completion_tokens": 7,
                                                       "total_tokens": 18})):
            self.assertEqual(llm.json("sys", "user"), {"checks": []})
        self.assertEqual(llm.usage["calls"], 1)
        self.assertEqual(llm.usage["prompt_tokens"], 11)
        self.assertEqual(llm.usage["total_tokens"], 18)

    def test_invalid_json_gets_one_repair_round(self):
        llm = make_llm()
        with patch("agent.llm_client.urllib.request.urlopen",
                   side_effect=[FakeResponse("not json at all"),
                                FakeResponse('{"items": []}')]) as opener:
            self.assertEqual(llm.json("sys", "user"), {"items": []})
        self.assertEqual(opener.call_count, 2)
        repair_payload = opener.call_args_list[1].args[0].data.decode("utf-8")
        self.assertIn("无法解析为 JSON", repair_payload)
        self.assertEqual(llm.usage["calls"], 2)

    def test_two_invalid_outputs_raise(self):
        llm = make_llm()
        with patch("agent.llm_client.urllib.request.urlopen",
                   side_effect=[FakeResponse("nope"), FakeResponse("still nope")]):
            with self.assertRaises(LLMError):
                llm.json("sys", "user")

    def test_network_errors_are_bounded(self):
        llm = make_llm()
        with patch("agent.llm_client.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("down")), \
             patch("agent.llm_client.time.sleep", return_value=None):
            with self.assertRaises(LLMError):
                llm.json("sys", "user")


if __name__ == "__main__":
    unittest.main()
