"""Configuration defaults that affect latency/cost."""
from __future__ import annotations
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.config import Config


class TestConfig(unittest.TestCase):
    def test_judge_model_defaults_to_main_model(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "MEDEVIDENCE_MODEL": "qwen-plus",
            "MEDEVIDENCE_JUDGE_MODEL": "",
        }):
            cfg = Config.from_env(Path(td))
        self.assertEqual(cfg.model, "qwen-plus")
        self.assertEqual(cfg.judge_model, "qwen-plus")

    def test_judge_model_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "MEDEVIDENCE_MODEL": "qwen-plus",
            "MEDEVIDENCE_JUDGE_MODEL": "qwen-flash",
        }):
            cfg = Config.from_env(Path(td))
        self.assertEqual(cfg.judge_model, "qwen-flash")


if __name__ == "__main__":
    unittest.main()
