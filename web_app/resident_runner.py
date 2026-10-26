"""Persistent, single-worker adapter for the existing MedEvidence V0.3 SDK.

Keeps one local-Qdrant client, BM25 connection, and lazily loaded BGE-M3
encoder alive across user questions. Does NOT modify any core Agent code.

IMPORTANT: only call in the one background HistoryService worker; do not run
multiple processes on the embedded/local Qdrant collection.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

CODE_ROOT = Path(__file__).resolve().parent.parent
_RUNTIME_LOCK = Lock()
_RUNTIME: ResidentRunner | None = None


class TimedRetriever:
    """Transparent read-only wrapper for per-question retrieval timing."""

    def __init__(self, raw: Any):
        self.raw = raw
        self.seconds = 0.0
        self.calls = 0
        if hasattr(raw, "lookup_document"):
            self.lookup_document = self._timed(raw.lookup_document)

    def reset(self) -> None:
        self.seconds = 0.0
        self.calls = 0

    def _timed(self, method):
        def call(*args, **kwargs):
            start = time.perf_counter()
            try:
                return method(*args, **kwargs)
            finally:
                self.seconds += time.perf_counter() - start
                self.calls += 1
        return call

    def search(self, *args, **kwargs):
        start = time.perf_counter()
        try:
            return self.raw.search(*args, **kwargs)
        finally:
            self.seconds += time.perf_counter() - start
            self.calls += 1

    def __getattr__(self, name: str):
        return getattr(self.raw, name)


class ResidentRunner:
    """One process, one Qdrant handle, one encoder; fresh Agent state per question."""

    def __init__(self, root: Path = CODE_ROOT):
        start = time.perf_counter()
        root = Path(root).resolve()
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        # Prevent optional TF/Flax imports from increasing memory/boot times.
        os.environ["USE_TF"] = "0"
        os.environ["USE_FLAX"] = "0"
        os.environ["USE_TORCH"] = "1"
        os.environ["MEDEVIDENCE_TRACE_CONTENT"] = "0"
        from agent.project_locator import find_project_root
        from agent.config import Config
        from agent.retrieval_tools import make_retriever
        from agent.sdk_runtime import sdk_available

        project_root = find_project_root(root, None)
        cfg = Config.from_env(project_root, code_root=root)
        cfg = replace(cfg, log_dir=root / "outputs" / "agent_runs_v030", trace_content=False)
        if not cfg.api_key or cfg.api_key == "your_key_here":
            raise ValueError("未配置有效的 DASHSCOPE_API_KEY")
        ready, message = sdk_available()
        if not ready:
            raise RuntimeError("Agent SDK 不可用：" + str(message)[:160])
        self.config = cfg
        self.retriever = TimedRetriever(make_retriever(cfg))
        self.online = None
        if cfg.online_enabled:
            from agent.online_literature import OnlineLiteratureService
            self.online = OnlineLiteratureService(
                cache_dir=cfg.online_cache_dir, sources=cfg.online_sources,
                timeout=cfg.online_timeout_sec, ttl_hours=cfg.online_cache_ttl_hours,
                email=cfg.ncbi_email, api_key=cfg.ncbi_api_key)
        self.init_seconds = time.perf_counter() - start
        self.log_file = root / "web_app" / "data" / "performance.jsonl"
        self._agent_run = None

    def run(self, question: str) -> dict[str, Any]:
        # SDKState and citation/evidence registries are created anew per call.
        from agent.sdk_runtime import run_sdk_agent
        self.retriever.reset()
        start = time.perf_counter()
        result = asyncio.run(run_sdk_agent(question, self.config, self.retriever,
                                           online_service=self.online))
        seconds = time.perf_counter() - start
        answer = result.to_dict()
        assert isinstance(answer, dict)
        record = {
            "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "engine": "persistent_sdk_v030", "model": self.config.model,
            "embedding_device": self.config.device,
            "configured_max_rounds": self.config.max_rounds,
            "initialization_seconds_first_use": round(self.init_seconds, 2),
            "run_seconds": round(seconds, 2),
            "retrieval_seconds": round(self.retriever.seconds, 2),
            "retrieval_calls": self.retriever.calls,
            "rounds_used": answer.get("rounds"),
            "status": answer.get("status"),
            "note": "No user question, evidence text, API key, or patient data stored.",
        }
        # A performance log failure must not discard a successful clinical QA result.
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return answer


def get_runner() -> ResidentRunner:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = ResidentRunner()
        return _RUNTIME


def run_resident_question(question: str) -> dict[str, Any]:
    return get_runner().run(question)
