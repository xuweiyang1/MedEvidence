"""Agent control loop with bounded searches and auditable run logs."""
from __future__ import annotations
import hashlib
import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any
from .models import AgentResult, Evidence
from .planner import Planner
from .evidence_evaluator import EvidenceEvaluator
from .decision_policy import decide
from .answer_generator import AnswerGenerator
from .citation_verifier import CitationVerifier


class MedEvidenceAgent:
    def __init__(self, llm, retriever, cfg):
        self.cfg = cfg
        self.retriever = retriever
        self.planner = Planner(llm)
        self.evaluator = EvidenceEvaluator(llm)
        self.generator = AnswerGenerator(llm)
        self.verifier = CitationVerifier(llm, semantic=cfg.semantic_verify)

    def run(self, question: str) -> AgentResult:
        question = question.strip()
        if not question or len(question) > 2000:
            raise ValueError("问题必须是 1~2000 字符的文本")
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        logs: list[dict[str, Any]] = []
        def record(step: str, **data):
            logs.append({"phase": step, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **data})
        record("start", question_sha256=hashlib.sha256(question.encode()).hexdigest(),
               question=question if self.cfg.trace_content else "[redacted]",
               model=self.cfg.model, collection=self.cfg.collection, retriever=self.cfg.retriever,
               max_rounds=self.cfg.max_rounds, semantic_verify=self.cfg.semantic_verify)
        evidence_by_source_id: dict[str, Evidence] = {}
        search_history: list[str] = []
        plan = None
        coverage = None
        decision = None
        rounds = 0
        limitations: list[str] = []
        accepted: list[dict] = []
        verification: dict = {"method": "not_run", "checks": [], "valid": 0}
        status = "insufficient_evidence"
        answer = "未找到足够可核验的文献证据，因此不生成医学结论。"
        try:
            plan = self.planner.plan(question)
            record("plan", aspects=plan.aspects if self.cfg.trace_content else [hashlib.sha256(x.encode()).hexdigest()[:12] for x in plan.aspects],
                   aspect_count=len(plan.aspects), query_count=len(plan.queries), high_risk=plan.high_risk)
            pending = plan.queries
            for r in range(1, self.cfg.max_rounds + 1):
                if not pending:
                    break
                rounds = r
                new_count = 0
                for query in pending[:3]:
                    if query in search_history:
                        continue
                    search_history.append(query)
                    hits = self.retriever.search(query, self.cfg.top_k)
                    selected = []
                    for item in hits:
                        if not item.text.strip():
                            continue
                        unique_id = item.id
                        if unique_id not in evidence_by_source_id:
                            new_count += 1
                            evidence_by_source_id[unique_id] = item
                        elif item.score > evidence_by_source_id[unique_id].score:
                            evidence_by_source_id[unique_id] = item
                        selected.append(unique_id)
                    record("search", round=r, query=query if self.cfg.trace_content else "[redacted]",
                           hit_count=len(hits), new_count=new_count, result_ids=selected)
                evidence = sorted(evidence_by_source_id.values(), key=lambda x: -x.score)[:self.cfg.max_evidence]
                # Give stable run-local opaque IDs to LLM, independent of chunk-ID format.
                run_evidence = [Evidence(**{**asdict(x), "id": f"E{i}"}) for i, x in enumerate(evidence, 1)]
                coverage = self.evaluator.evaluate(plan.aspects, run_evidence)
                record("coverage", round=r, ratio=coverage.ratio, covered_count=len(coverage.covered),
                       aspect_count=len(plan.aspects), gaps=coverage.gaps if self.cfg.trace_content else ["[redacted]"] * len(coverage.gaps),
                       conflicts_count=len(coverage.conflicts), evidence_count=len(run_evidence))
                decision = decide(coverage, r, self.cfg.max_rounds, self.cfg.coverage_threshold,
                                  can_search=new_count > 0)
                record("decision", round=r, action=decision.action, reason=decision.reason)
                if decision.action != "search_again":
                    break
                next_queries = self.planner.reformulate(question, coverage.gaps, search_history)
                pending = [q for q in next_queries if q not in search_history]
                if not pending:
                    record("stop", reason="planner returned no novel queries")
                    break
            evidence = sorted(evidence_by_source_id.values(), key=lambda x: -x.score)[:self.cfg.max_evidence]
            run_evidence = [Evidence(**{**asdict(x), "id": f"E{i}"}) for i, x in enumerate(evidence, 1)]
            # Re-evaluate only if final candidate set changed (normal loop already evaluated it).
            if coverage is None:
                from .models import Coverage
                coverage = Coverage(gaps=plan.aspects)
            limitations = list(dict.fromkeys(coverage.gaps + coverage.conflicts))[:8]
            if plan.high_risk:
                limitations.insert(0, "此系统不能根据个人病情提出诊断、处方、停药或剂量调整建议。")
            if coverage.covered and run_evidence:
                generated = self.generator.generate(question, coverage, run_evidence, plan.high_risk)
                accepted, verification = self.verifier.verify(generated["claims"], run_evidence)
                limitations.extend(generated.get("limitations", []))
                record("verification", method=verification["method"], proposed=len(generated["claims"]),
                       accepted=len(accepted), verdicts=[x["verdict"] for x in verification["checks"]])
            if accepted:
                # Do not display the unverified freeform draft; rebuild final from accepted atomized claims.
                answer = "\n".join("- " + c["text"] + " " + "".join(f"[{ref}]" for ref in c["citation_ids"])
                                   for c in accepted)
                fully_supported = (coverage.ratio >= self.cfg.coverage_threshold
                                   and len(accepted) == len(generated["claims"])
                                   and generated.get("status") == "answered"
                                   and not coverage.conflicts and not plan.high_risk)
                status = "answered" if fully_supported else "partial"
            elif coverage.covered:
                answer = "检索到部分相关文献，但未形成通过引用支持核验的医学主张，故不输出结论。"
            record("finish", status=status, rounds=rounds, accepted_claims=len(accepted))
            public_evidence = [x.public(include_text=self.cfg.trace_content) for x in run_evidence]
            # Source IDs are local and stable, but returned to the user only as a mapping, never invented.
            for row, origin in zip(public_evidence, evidence):
                row["source_chunk_id"] = origin.id
            result = AgentResult(run_id, status, answer, accepted, public_evidence,
                                 list(dict.fromkeys(limitations))[:12], verification, "", rounds, coverage.ratio)
            self._write_trace(run_id, logs, result)
            return result
        except Exception as exc:
            record("error", error_type=type(exc).__name__, error=str(exc)[:500])
            self._write_trace(run_id, logs, None)
            raise

    def _write_trace(self, run_id: str, events: list[dict], result: AgentResult | None) -> None:
        logdir: Path = self.cfg.log_dir
        logdir.mkdir(parents=True, exist_ok=True)
        path = logdir / f"{run_id}.json"
        if result is not None:
            result.trace_path = str(path)
        doc = {"schema": "MedEvidenceAgentTrace_v0.1", "run_id": run_id,
               "events": events, "result": result.to_dict() if result else None,
               "privacy_note": "原始用户问题/查询默认不记录；若启用 TRACE_CONTENT 将保存问题证据原文。"}
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
