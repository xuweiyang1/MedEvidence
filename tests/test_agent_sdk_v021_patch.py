"""Regression tests for SDK annotation inspection and nested .env.agent paths."""
from __future__ import annotations

import os
import sys
import tempfile
import types
import typing
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.config import Config
from agent.sdk_runtime import build_sdk_agents


class TestAgentSDKV021Patch(unittest.TestCase):
    def test_nested_env_file_preferred_and_outer_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "MedEvidence"
            nested = root / "MedEvidence_Agent_SDK_V0_2_standalone"
            nested.mkdir(parents=True)
            (root / ".env.agent").write_text("DASHSCOPE_API_KEY=outer_key\n", encoding="utf-8")
            (nested / ".env.agent").write_text("DASHSCOPE_API_KEY=nested_key\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                cfg = Config.from_env(root, code_root=nested)
                self.assertEqual(cfg.api_key, "nested_key")
            (nested / ".env.agent").unlink()
            with patch.dict(os.environ, {}, clear=True):
                cfg = Config.from_env(root, code_root=nested)
                self.assertEqual(cfg.api_key, "outer_key")

    def test_sdk_tool_decorator_can_resolve_runtime_context_annotation(self):
        """Simulates the SDK's type-hint inspection without API or SDK dependency."""
        fake = types.ModuleType("agents")
        T = typing.TypeVar("T")

        class RunContextWrapper(typing.Generic[T]):
            pass

        class GuardrailFunctionOutput:
            pass

        class Agent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        def function_tool(fn):
            # The real SDK inspects a tool's annotation to locate context.
            hints = typing.get_type_hints(fn)
            self.assertIs(typing.get_origin(hints["ctx"]), RunContextWrapper)
            return types.SimpleNamespace(name=fn.__name__)

        def input_guardrail(func=None, *, run_in_parallel=True):
            if func is None:
                return lambda f: f
            return func

        def output_guardrail(fn):
            return fn

        fake.Agent = Agent
        fake.RunContextWrapper = RunContextWrapper
        fake.GuardrailFunctionOutput = GuardrailFunctionOutput
        fake.function_tool = function_tool
        fake.input_guardrail = input_guardrail
        fake.output_guardrail = output_guardrail

        with patch.dict(sys.modules, {"agents": fake}):
            planner, writer = build_sdk_agents(object())
            online_planner, online_writer = build_sdk_agents(object(), online_enabled=True)
        self.assertEqual(planner.name, "EvidencePlanner")
        self.assertEqual(writer.name, "EvidenceWriter")
        self.assertEqual(len(planner.handoffs), 1)
        expected_tools = {"lookup_medical_document", "list_retrieved_evidence",
                          "search_medical_evidence", "assess_evidence_coverage"}
        self.assertEqual({t.name for t in planner.tools}, expected_tools)
        self.assertEqual({t.name for t in writer.tools}, expected_tools)
        online_tools = expected_tools | {"search_online_literature", "search_online_guidelines"}
        self.assertEqual({t.name for t in online_planner.tools}, online_tools)
        self.assertEqual({t.name for t in online_writer.tools}, online_tools)


if __name__ == "__main__":
    unittest.main()
