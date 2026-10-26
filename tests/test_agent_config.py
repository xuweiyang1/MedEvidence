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

    def test_online_defaults_off_and_bounded(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "MEDEVIDENCE_ONLINE_ENABLED": "",
            "MEDEVIDENCE_ONLINE_MAX_RESULTS": "99",
            "MEDEVIDENCE_ONLINE_MAX_ROUNDS": "9",
        }):
            cfg = Config.from_env(Path(td))
        self.assertFalse(cfg.online_enabled)
        self.assertEqual(cfg.online_max_results, 10)
        self.assertEqual(cfg.online_max_rounds, 5)

    def test_online_can_be_enabled(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "MEDEVIDENCE_ONLINE_ENABLED": "1",
            "MEDEVIDENCE_ONLINE_SOURCES": "pubmed,europepmc",
        }):
            cfg = Config.from_env(Path(td))
        self.assertTrue(cfg.online_enabled)
        self.assertEqual(cfg.online_sources, "pubmed,europepmc")


if __name__ == "__main__":
    unittest.main()
