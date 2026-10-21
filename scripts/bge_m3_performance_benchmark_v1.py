#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MedEvidence BGE-M3 性能基准 V1

用途：
- 在 100 Chunk Smoke Test 通过后运行。
- 先测试 1,000 / 5,000 Chunk，再决定是否构建 42,526 Chunk 全量索引。
- 记录吞吐、截断率、失败批次、向量维度、峰值显存、全量预计耗时。
- 不用于宣称医学检索质量。

依赖：
  python -m pip install FlagEmbedding transformers numpy

示例：
  python scripts/bge_m3_performance_benchmark_v1.py --sizes 1000,5000
"""
from __future__ import annotations
import argparse, json, random, time
from collections import Counter, defaultdict
from pathlib import Path

MODEL_NAME = "BAAI/bge-m3"
SEED = 20261014

def jload_lines(path: Path):
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)

def load_data(root: Path):
    chunks = list(jload_lines(root / "data/processed/knowledge/v3_1_all/retrieval_chunks.jsonl"))
    docs_path = root / "data/processed/knowledge/v3_1_all/documents.jsonl"
    docs = {r.get("document_id",""): r for r in jload_lines(docs_path)} if docs_path.is_file() else {}
    return chunks, docs

def stratified(rows, n):
    rng = random.Random(SEED)
    weights = {"body": .60, "table": .20, "abstract": .10, "figure_caption": .10}
    by_kind = defaultdict(list)
    for r in rows:
        by_kind[r.get("content_kind","unknown")].append(r)
    out, used = [], set()
    for kind, w in weights.items():
        pool = by_kind.get(kind, [])[:]
        rng.shuffle(pool)
        take = min(len(pool), int(round(n*w)))
        for r in pool[:take]:
            out.append(r); used.add(r["chunk_id"])
    if len(out) < n:
        rest = [r for r in rows if r["chunk_id"] not in used]
        rng.shuffle(rest)
        out.extend(rest[:n-len(out)])
    return out[:n]

def embed_text(row, docs):
    d = docs.get(row.get("document_id",""), {})
    title = row.get("title") or d.get("title") or ""
    section = row.get("section_path") or ""
    if isinstance(section, list):
        section = " > ".join(section)
    parts = []
    if title: parts.append(f"Title: {title}")
    if section: parts.append(f"Section: {section}")
    parts.append(row.get("text",""))
    return "\n".join(parts)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--model", default=MODEL_NAME)
    ap.add_argument("--sizes", default="1000,5000")
    ap.add_argument("--batch-size", type=int, default=2)
    ap.add_argument("--max-length", type=int, default=768)
    ap.add_argument("--fp32", action="store_true")
    args = ap.parse_args()

    try:
        import torch
        from transformers import AutoTokenizer
        from FlagEmbedding import BGEM3FlagModel
    except Exception as e:
        raise SystemExit("缺少依赖，请执行: python -m pip install FlagEmbedding transformers numpy") from e

    root = args.root.resolve()
    rows, docs = load_data(root)
    total_corpus = len(rows)
    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    sizes = [x for x in sizes if 0 < x <= total_corpus]

    device = "cuda" if torch.cuda.is_available() else "cpu"
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model = BGEM3FlagModel(
        args.model,
        use_fp16=bool(torch.cuda.is_available() and not args.fp32),
        devices=device,
    )

    reports = []
    for n in sizes:
        sample = stratified(rows, n)
        texts = [embed_text(r, docs) for r in sample]
        lengths = [
            len(tokenizer(t, add_special_tokens=True, truncation=False)["input_ids"])
            for t in texts
        ]

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()

        failures, vector_count, dim = [], 0, None
        t0 = time.perf_counter()

        for start in range(0, n, args.batch_size):
            batch = texts[start:start+args.batch_size]
            try:
                out = model.encode(
                    batch,
                    batch_size=args.batch_size,
                    max_length=args.max_length,
                    return_dense=True,
                    return_sparse=False,
                    return_colbert_vecs=False,
                )
                vecs = out["dense_vecs"]
                vector_count += len(vecs)
                if dim is None and len(vecs):
                    dim = len(vecs[0])
            except Exception as e:
                failures.append({
                    "start": start,
                    "size": len(batch),
                    "error": f"{type(e).__name__}: {e}"
                })

        if torch.cuda.is_available():
            torch.cuda.synchronize()
        elapsed = time.perf_counter() - t0
        peak = torch.cuda.max_memory_allocated()/(1024**3) if torch.cuda.is_available() else 0.0
        cps = vector_count/elapsed if elapsed and vector_count else 0.0

        report = {
            "sample_size": n,
            "kind_distribution": dict(Counter(r.get("content_kind","unknown") for r in sample)),
            "device": device,
            "model": args.model,
            "batch_size": args.batch_size,
            "max_length": args.max_length,
            "token_length_min": min(lengths) if lengths else 0,
            "token_length_avg": sum(lengths)/len(lengths) if lengths else 0,
            "token_length_max": max(lengths) if lengths else 0,
            "truncated_chunks": sum(x > args.max_length for x in lengths),
            "truncation_rate": sum(x > args.max_length for x in lengths)/len(lengths) if lengths else 0,
            "failed_batches": len(failures),
            "failed_batch_details": failures[:20],
            "encoded_vectors": vector_count,
            "embedding_dimension": dim,
            "encoding_seconds": elapsed,
            "chunks_per_second": cps,
            "peak_gpu_memory_gib": peak,
            "estimated_full_corpus_seconds": total_corpus/cps if cps else None,
            "full_corpus_size": total_corpus,
        }
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))

        if failures:
            print("检测到失败批次，停止后续更大规模测试。")
            break

    final = {
        "purpose": "PERFORMANCE_BENCHMARK_ONLY",
        "reports": reports,
        "note": "性能测试不能用于宣称医学检索质量。",
    }
    out = root / "reports/bge_m3_performance_benchmark_v1.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {out}")

if __name__ == "__main__":
    main()
