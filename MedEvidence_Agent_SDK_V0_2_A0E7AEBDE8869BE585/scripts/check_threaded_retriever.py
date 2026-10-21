"""Offline worker-thread smoke for MedEvidence's local Agent retriever.

This explicitly exercises the same asyncio.to_thread path that the Agents SDK
uses for tool calls. Nothing is written to the index or sent to any API.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT))
from agent.config import Config
from agent.project_locator import find_project_root
from agent.retrieval_tools import make_retriever


async def check(retriever, title: str, with_dense: bool) -> dict:
    if not hasattr(retriever, "lookup_document"):
        raise RuntimeError("当前检索器没有 lookup_document 方法")
    # SQLite connection created in main thread; retrieval runs in SDK worker.
    hits = await asyncio.to_thread(retriever.lookup_document, title, 8, "研究局限性 limitations")
    details = {
        "lookup_on_worker_thread": True,
        "lookup_hit_count": len(hits),
        "first_chunks": [{"id": e.id, "title": e.title[:140], "text_length": len(e.text)} for e in hits[:3]],
    }
    if hasattr(retriever, "_sparse"):
        sparse_hits = await asyncio.to_thread(retriever._sparse, "dupilumab COPD", 5)
        details["bm25_on_worker_thread"] = True
        details["bm25_hit_count"] = len(sparse_hits)
    if with_dense:
        fused_hits = await asyncio.to_thread(retriever.search, "COPD dupilumab study limitations", 5)
        details["dense_and_bm25_on_worker_thread"] = True
        details["fused_hit_count"] = len(fused_hits)
        details["fused_chunks"] = [e.id for e in fused_hits[:5]]
    details["threaded_retrieval_ready"] = bool(hits)
    return details


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 SDK 工作线程中的 SQLite 文献检索（离线只读）")
    parser.add_argument("--title", default="Real-world effectiveness of dupilumab in COPD")
    parser.add_argument("--with-dense", action="store_true", help="额外在工作线程中加载 BGE-M3 并运行 Dense+BM25；可能访问 HF 缓存/网络")
    parser.add_argument("--project-root", default=None)
    args = parser.parse_args()
    retriever = None
    try:
        root = find_project_root(CODE_ROOT, args.project_root)
        cfg = Config.from_env(root, code_root=CODE_ROOT)
        retriever = make_retriever(cfg)
        results = asyncio.run(check(retriever, args.title, args.with_dense))
        results.update({
            "project_root": str(root),
            "qdrant_collection": cfg.collection,
            "used_generation_api": False,
            "index_writes": False,
        })
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if results["threaded_retrieval_ready"] else 2
    except Exception as exc:
        print(json.dumps({"threaded_retrieval_ready": False,
                          "error_type": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    finally:
        if retriever is not None:
            for name in ("fts", "client"):
                obj = getattr(retriever, name, None)
                if obj is not None and hasattr(obj, "close"):
                    obj.close()


if __name__ == "__main__":
    raise SystemExit(main())
