"""Read-only diagnostics for MedEvidence dense/BM25 retrieval quality.
Does not print chunk bodies or credentials and does not call the DashScope API.
It loads the local embedding model and reads Qdrant/SQLite only.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT))
from agent.config import Config
from agent.project_locator import find_project_root
from agent.retrieval_tools import LocalQdrantRetriever, make_retriever


def terms_for(text: str) -> list[str]:
    vals = re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}|[\u3400-\u9fff]{2,}", text.lower())
    # Keep order, drop duplicates and common low-information tokens.
    stop = {"the", "and", "for", "with", "from", "versus", "days", "day", "course", "treatment", "patient", "patients"}
    return list(dict.fromkeys(t for t in vals if t not in stop))


def vector_config_shape(vectors):
    def one(v):
        return {
            "size": getattr(v, "size", None),
            "distance": str(getattr(v, "distance", None)),
            "on_disk": getattr(v, "on_disk", None),
        }
    if isinstance(vectors, dict):
        return {str(k): one(v) for k, v in vectors.items()}
    return {"default": one(vectors)}


def evidence_summary(e, query_terms):
    haystack = f"{e.title} {e.section} {e.text}".lower()
    matched = [t for t in query_terms if t in haystack]
    return {
        "id": e.id,
        "title": e.title[:180],
        "pmcid": e.pmcid,
        "raw_score_or_rank_score": e.score,
        "text_length": len(e.text),
        "query_terms_found_in_title_or_text": matched,
        "query_term_match_count": len(matched),
    }


def fts_term_counts(r: LocalQdrantRetriever, terms: list[str]):
    if r.fts is None or not r.fts_table:
        return {t: None for t in terms}
    table = '"' + r.fts_table.replace('"', '""') + '"'
    out = {}
    for term in terms:
        # Each term is quoted so punctuation cannot alter FTS query syntax.
        expr = '"' + term.replace('"', '') + '"'
        try:
            row = r.fts.execute(f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH ?", (expr,)).fetchone()
            out[term] = int(row[0])
        except Exception as exc:
            out[term] = f"ERROR:{type(exc).__name__}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Inspect raw dense and BM25 retrieval without printing evidence text")
    ap.add_argument("--project-root", help="MedEvidence root; defaults to auto-detection")
    ap.add_argument("--question", action="append", help="Question/query; can be repeated")
    ap.add_argument("--top-k", type=int, default=10)
    args = ap.parse_args()
    try:
        root = find_project_root(CODE_ROOT, args.project_root)
        cfg = Config.from_env(root, code_root=CODE_ROOT)
        retriever = make_retriever(cfg)
        if not isinstance(retriever, LocalQdrantRetriever):
            raise RuntimeError("当前配置不是 qdrant 模式；本诊断脚本只检查 LocalQdrantRetriever")
        try:
            info = retriever.client.get_collection(cfg.collection)
            vectors = info.config.params.vectors
            qdrant = {
                "collection": cfg.collection,
                "points_count": getattr(info, "points_count", None),
                "vectors_config": vector_config_shape(vectors),
                "qdrant_path": cfg.qdrant_path,
                "sqlite_db": cfg.bm25_db,
                "fts_table": retriever.fts_table or None,
                "chunks_table": retriever.chunks_table or None,
            }
            coverage_terms = ["COPD", "AECOPD", "exacerbation", "corticosteroid", "corticosteroids",
                              "prednisone", "prednisolone", "glucocorticoid", "glucocorticoids", "steroid", "steroids"]
            corpus_term_counts = fts_term_counts(retriever, coverage_terms)
            questions = args.question or ["COPD acute exacerbation systemic corticosteroids prednisone 40 mg 5 days treatment duration"]
            reports = []
            for question in questions:
                query_terms = terms_for(question)
                dense = retriever._dense(question, max(1, min(30, args.top_k)))
                sparse = retriever._sparse(question, max(1, min(30, args.top_k)))
                reports.append({
                    "query": question,
                    "dense_top_hits": [evidence_summary(e, query_terms) for e in dense],
                    "bm25_top_hits": [evidence_summary(e, query_terms) for e in sparse],
                    "dense_hit_count": len(dense),
                    "bm25_hit_count": len(sparse),
                })
            print(json.dumps({
                "diagnostic": "read_only_dense_bm25_retrieval_quality",
                "project_root": str(root),
                "embedding_model_config": cfg.embed_model,
                "device": cfg.device,
                "network_or_generation_api_called": False,
                "evidence_body_printed": False,
                "api_key_printed": False,
                "qdrant": qdrant,
                "fts_term_chunk_counts": corpus_term_counts,
                "queries": reports,
                "interpretation_hint": "dense_top_hits 的分数是 Qdrant 原始向量分数；bm25_top_hits 的 raw_score_or_rank_score 是适配器的倒数排名分数，不是 BM25 原始 BM25() 值。看标题、匹配词和单词命中数，不要把分数跨来源直接比较。",
            }, ensure_ascii=False, indent=2))
            return 0
        finally:
            if getattr(retriever, "fts", None) is not None:
                retriever.fts.close()
            if getattr(retriever, "client", None) is not None:
                retriever.client.close()
    except Exception as exc:
        print(json.dumps({"diagnostic_error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
