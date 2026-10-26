"""MedEvidence CLI v0.2: true OpenAI Agents SDK Runner; optional legacy v0.1 mode."""
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
from agent.retrieval_tools import discover_indices, make_retriever
from agent.sdk_runtime import sdk_available, run_sdk_agent


def main() -> int:
    parser = argparse.ArgumentParser(description="MedEvidence Agent SDK v0.2 (真实 Runner / Tools / Handoff)")
    parser.add_argument("--question", help="循证医学科研/教学问题，勿上传真实患者病历")
    parser.add_argument("--project-root", help="MedEvidence 项目根目录；默认自动查找 index_coordinator.py")
    parser.add_argument("--probe", action="store_true", help="扫描实际项目根目录，不调用 API")
    parser.add_argument("--self-test", action="store_true", help="离线自测：不需要 SDK、Key 或索引")
    parser.add_argument("--sdk-check", action="store_true", help="检查 SDK 安装及项目根目录")
    parser.add_argument("--retriever-check", action="store_true", help="验证 Qdrant collection 和可选 BM25 FTS5，不调用 API/Embedding")
    parser.add_argument("--retrieval-smoke", action="store_true", help="真实检索冒烟测试：本地加载 Embedding，不调用百炼 API")
    parser.add_argument("--trace-diagnose", action="store_true", help="读取最新本地 Agent 轨迹并输出脱敏故障摘要；不调用 API")
    parser.add_argument("--document-lookup", metavar="TITLE", help="按完整/足够长的论文标题查 SQLite 正文；不调用 Embedding/百炼 API")
    parser.add_argument("--trace-content", action="store_true", help="记录敏感文本到本地日志（仅虚构测试题）")
    parser.add_argument("--legacy", action="store_true", help="使用旧 V0.1 自写控制循环作为对照基线")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = parser.parse_args()
    try:
        root = find_project_root(CODE_ROOT, args.project_root)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.discover(str(CODE_ROOT / "tests"), pattern="test_agent*.py")
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if args.probe:
        out = discover_indices(root)
        out["code_root"] = str(CODE_ROOT)
        out["project_root"] = str(root)
        out["project_root_detected"] = (root / "scripts" / "index_coordinator.py").is_file()
        out["notes"] = ("这里只扫描 Qdrant 本地库及 SQLite 数据库；返回空不代表索引不存在。"
                        "若你的索引放在项目外，需 --project-root 或设置绝对路径；"
                        "BM25 若为其他格式请接入 hook。")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.sdk_check:
        ok, msg = sdk_available()
        details = {"sdk_installed": ok, "sdk_ready": False, "message": msg,
                   "code_root": str(CODE_ROOT), "project_root": str(root)}
        if ok:
            try:
                from agents import AsyncOpenAI, OpenAIChatCompletionsModel
                from agent.sdk_runtime import build_sdk_agents
                # Builds SDK objects only, does not call any API.
                check_model = OpenAIChatCompletionsModel(
                    model="qwen-plus", openai_client=AsyncOpenAI(
                        api_key="SDK_STRUCTURE_CHECK_NO_NETWORK",
                        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"))
                planner, writer = build_sdk_agents(check_model)
                details["sdk_graph"] = {
                    "planner": planner.name,
                    "writer": writer.name,
                    "planner_tools": [t.name for t in planner.tools],
                    "writer_tools": [t.name for t in writer.tools],
                    "handoffs": [getattr(h, "name", str(type(h).__name__)) for h in planner.handoffs],
                    "input_guardrails": len(planner.input_guardrails),
                    "output_guardrails": len(writer.output_guardrails),
                }
                ok = bool(planner.handoffs and planner.tools and writer.tools
                          and planner.input_guardrails and writer.output_guardrails)
            except Exception as exc:
                details["sdk_graph_error"] = f"{type(exc).__name__}: {exc}"
                ok = False
        details["sdk_ready"] = ok
        print(json.dumps(details, ensure_ascii=False, indent=2))
        return 0 if ok else 2
    if args.retriever_check:
        try:
            cfg = Config.from_env(root, code_root=CODE_ROOT)
            retriever = make_retriever(cfg)
            details = {
                "retriever_ready": True,
                "retriever_type": cfg.retriever,
                "config_file": str(CODE_ROOT / ".env.agent" if (CODE_ROOT / ".env.agent").is_file() else root / ".env.agent"),
                "api_key_configured": bool(cfg.api_key and cfg.api_key != "your_key_here"),
            }
            if cfg.retriever == "qdrant":
                collection_info = retriever.client.get_collection(cfg.collection)
                details.update({
                    "qdrant_path": str(cfg.qdrant_path),
                    "collection": cfg.collection,
                    "point_count": getattr(collection_info, "points_count", None),
                    "bm25_db": cfg.bm25_db or "(disabled)",
                    "bm25_fts_table": retriever.fts_table or "(disabled)",
                })
            print(json.dumps(details, ensure_ascii=False, indent=2))
            if getattr(retriever, "fts", None) is not None:
                retriever.fts.close()
            if getattr(retriever, "client", None) is not None:
                retriever.client.close()
            return 0
        except Exception as exc:
            print(f"[RETRIEVER ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if args.trace_diagnose:
        trace_dir = root / "outputs" / "agent_runs"
        files = sorted(trace_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
        if not files:
            print(f"[TRACE ERROR] 目录下没有 JSON 轨迹: {trace_dir}", file=sys.stderr)
            return 2
        try:
            data = json.loads(files[0].read_text(encoding="utf-8-sig"))
            events = data.get("agent_events", [])
            search_events = [ev for ev in events if ev.get("kind") == "tool_search"]
            output_guardrails = [ev for ev in events if ev.get("kind") == "output_guardrail"]
            coverage_events = [ev for ev in events if ev.get("kind") in
                               {"coverage", "coverage_auto_recheck", "coverage_auto_recheck_error", "coverage_auto_recheck_skipped"}]
            errors = [ev for ev in events if ev.get("kind") in
                      {"tool_error", "runner_error", "citation_check_error"}]
            print(json.dumps({
                "trace_file": str(files[0]),
                "status": data.get("result", {}).get("status"),
                "search_calls": data.get("result", {}).get("rounds"),
                "successful_searches": len(search_events),
                "search_hit_counts": [ev.get("hit_count") for ev in search_events],
                "registered_evidence_counts": [len(ev.get("evidence_ids", [])) for ev in search_events],
                "coverage_events": [{k: ev[k] for k in ("kind", "ratio", "covered_count", "aspect_count", "reason", "error_type") if k in ev}
                                    for ev in coverage_events],
                "guardrail_events": [{k: ev[k] for k in ("accepted", "reason", "draft_status", "claim_count", "evidence_count", "coverage_checked") if k in ev}
                                     for ev in output_guardrails],
                "errors": [{k: ev[k] for k in ("kind", "tool", "error_type") if k in ev} for ev in errors],
                "note": "仅输出脱敏元数据，不包含原问题、检索文本或 API Key。旧轨迹可能缺少新版字段。",
            }, ensure_ascii=False, indent=2))
            return 0
        except (ValueError, OSError, KeyError) as exc:
            print(f"[TRACE ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if args.document_lookup is not None:
        cfg = Config.from_env(root, code_root=CODE_ROOT)
        retriever = None
        try:
            retriever = make_retriever(cfg)
            if not hasattr(retriever, "lookup_document"):
                raise RuntimeError("当前检索器不支持按标题定位；请使用内置 qdrant 模式")
            hits = retriever.lookup_document(args.document_lookup, min(8, cfg.top_k), focus="研究局限性 limitations")
            print(json.dumps({
                "document_lookup_ready": True, "title_fragment": args.document_lookup,
                "hit_count": len(hits),
                "hits": [{"id": h.id, "title": h.title, "pmcid": h.pmcid,
                          "text_length": len(h.text), "source": h.source,
                          "has_limitations_language": any(word in h.text.casefold() for word in ("limitation", "retrospective", "control group"))}
                         for h in hits],
                "note": "精确标题检索，默认不输出正文；不会调用百炼/Embedding，不会写入索引。",
            }, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(f"[DOCUMENT LOOKUP ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        finally:
            if retriever is not None:
                if getattr(retriever, "fts", None) is not None:
                    retriever.fts.close()
                if getattr(retriever, "client", None) is not None:
                    retriever.client.close()
    if args.retrieval_smoke:
        cfg = Config.from_env(root, code_root=CODE_ROOT)
        query = args.question or "COPD exacerbation systemic corticosteroid duration 5 days"
        retriever = None
        try:
            retriever = make_retriever(cfg)
            hits = retriever.search(query, min(5, cfg.top_k))
            print(json.dumps({
                "retrieval_ready": True, "query": query,
                "hit_count": len(hits),
                "hits": [{"id": h.id, "source": h.source, "title": h.title[:160],
                          "pmcid": h.pmcid, "text_length": len(h.text), "score": h.score}
                         for h in hits],
                "note": "这是实际本地检索，不会调用百炼 API；默认不显示证据正文。",
            }, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(f"[RETRIEVAL SMOKE ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        finally:
            if retriever is not None:
                if getattr(retriever, "fts", None) is not None:
                    retriever.fts.close()
                if getattr(retriever, "client", None) is not None:
                    retriever.client.close()
    if not args.question:
        parser.error("请选择 --question、--probe、--sdk-check、--retriever-check、--retrieval-smoke、--trace-diagnose 或 --self-test")
    if args.trace_content:
        import os
        os.environ["MEDEVIDENCE_TRACE_CONTENT"] = "1"
    cfg = Config.from_env(root, code_root=CODE_ROOT)
    try:
        retriever = make_retriever(cfg)
        if args.legacy:
            from agent.llm_client import QwenLLM
            from agent.orchestrator import MedEvidenceAgent
            llm = QwenLLM(cfg.api_key, cfg.model, cfg.api_base, cfg.timeout_sec)
            result = MedEvidenceAgent(llm, retriever, cfg).run(args.question)
        else:
            ok, msg = sdk_available()
            if not ok:
                raise RuntimeError(msg)
            result = asyncio.run(run_sdk_agent(args.question, cfg, retriever))
    except Exception as exc:
        print(f"[Agent ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"\n=== MedEvidence {'legacy' if args.legacy else 'Agents SDK'} / {result.status} ===")
        print(result.answer)
        if result.evidence:
            print("\n证据来源：")
            for e in result.evidence:
                print(f"[{e['id']}] {e.get('title') or '(无标题)'} | PMCID:{e.get('pmcid') or '-'} | chunk:{e.get('source_chunk_id')}")
        if result.limitations:
            print("\n限制：", "；".join(result.limitations))
        print("\n研究/教学辅助，不构成诊断或治疗建议。")
        print(f"检索工具调用: {result.rounds} | 启发式证据覆盖率: {result.coverage_ratio} | 轨迹: {result.trace_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
