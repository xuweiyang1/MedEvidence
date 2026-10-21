#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MedEvidence BGE-M3 Smoke Test V1.1

工程 Smoke Test：验证模型加载、CUDA、显存、分层抽样、token 截断、
Dense 编码、Qdrant 本地持久化和查询。不能用于证明医学检索质量。

依赖：
  python -m pip install FlagEmbedding qdrant-client transformers numpy

构建：
  python scripts/medevidence_bge_m3_smoke_v1_1.py build
  python scripts/medevidence_bge_m3_smoke_v1_1.py build --gold-csv reports/retrieval_eval_v2.csv

查询：
  python scripts/medevidence_bge_m3_smoke_v1_1.py search --query "ABCC8 polymorphism and type 2 diabetes"
"""
from __future__ import annotations
import argparse, csv, json, random, time
from collections import Counter, defaultdict
from pathlib import Path

MODEL_NAME = "BAAI/bge-m3"
SEED = 20261014
QUOTAS = {"body": 60, "table": 20, "abstract": 10, "figure_caption": 10}

def jload_lines(path: Path):
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)

def load_corpus(root: Path):
    chunks_path = root / "data/processed/knowledge/v3_1_all/retrieval_chunks.jsonl"
    docs_path = root / "data/processed/knowledge/v3_1_all/documents.jsonl"
    if not chunks_path.is_file():
        raise FileNotFoundError(chunks_path)
    chunks = list(jload_lines(chunks_path))
    docs = {r.get("document_id", ""): r for r in jload_lines(docs_path)} if docs_path.is_file() else {}
    return chunks, {r["chunk_id"]: r for r in chunks}, docs

def load_verified_gold(path: Path | None):
    ids = set()
    if not path or not path.is_file():
        return ids
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            status = (row.get("status") or row.get("verification_status") or "").strip().upper()
            if status not in {"VERIFIED", "GOLD", "CONFIRMED"}:
                continue
            raw = (row.get("gold_chunk_ids") or "").strip()
            for x in raw.replace("|", ";").split(";"):
                if x.strip():
                    ids.add(x.strip())
    return ids

def stratified_sample(rows, by_id, gold_ids, sample_size):
    rng = random.Random(SEED)
    selected, used = [], set()
    for gid in sorted(gold_ids):
        if gid in by_id and gid not in used:
            selected.append(by_id[gid]); used.add(gid)

    buckets = defaultdict(list)
    for r in rows:
        if r["chunk_id"] not in used:
            buckets[r.get("content_kind", "unknown")].append(r)

    remain = max(0, sample_size - len(selected))
    base = sum(QUOTAS.values())
    desired = {k: int(round(v / base * remain)) for k, v in QUOTAS.items()}
    while sum(desired.values()) < remain:
        desired["body"] += 1
    while sum(desired.values()) > remain and desired["body"] > 0:
        desired["body"] -= 1

    for kind, n in desired.items():
        pool = buckets.get(kind, [])[:]
        rng.shuffle(pool)
        for r in pool[:n]:
            if r["chunk_id"] not in used:
                selected.append(r); used.add(r["chunk_id"])

    if len(selected) < sample_size:
        rest = [r for r in rows if r["chunk_id"] not in used]
        rng.shuffle(rest)
        selected.extend(rest[:sample_size-len(selected)])
    return selected[:sample_size]

def embed_text(row, docs):
    doc = docs.get(row.get("document_id", ""), {})
    title = row.get("title") or doc.get("title") or ""
    section = row.get("section_path") or ""
    if isinstance(section, list):
        section = " > ".join(section)
    parts = []
    if title: parts.append(f"Title: {title}")
    if section: parts.append(f"Section: {section}")
    parts.append(row.get("text", ""))
    return "\n".join(parts)

def imports():
    try:
        import torch
        from transformers import AutoTokenizer
        from FlagEmbedding import BGEM3FlagModel
        from qdrant_client import QdrantClient
        from qdrant_client.models import VectorParams, Distance, PointStruct
    except Exception as e:
        raise RuntimeError(
            "缺少依赖，请执行: python -m pip install FlagEmbedding qdrant-client transformers numpy"
        ) from e
    return torch, AutoTokenizer, BGEM3FlagModel, QdrantClient, VectorParams, Distance, PointStruct

def build(args):
    root = args.root.resolve()
    rows, by_id, docs = load_corpus(root)
    gold_ids = load_verified_gold(args.gold_csv.resolve() if args.gold_csv else None)
    sample = stratified_sample(rows, by_id, gold_ids, args.sample_size)

    torch, AutoTokenizer, BGEM3FlagModel, QdrantClient, VectorParams, Distance, PointStruct = imports()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model = BGEM3FlagModel(
        args.model,
        use_fp16=bool(torch.cuda.is_available() and not args.fp32),
        devices=device,
    )

    texts = [embed_text(r, docs) for r in sample]
    token_lengths = [
        len(tokenizer(t, add_special_tokens=True, truncation=False)["input_ids"])
        for t in texts
    ]

    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()

    vectors, failed = [], []
    t0 = time.perf_counter()
    for start in range(0, len(texts), args.batch_size):
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
            vectors.extend(vecs.tolist() if hasattr(vecs, "tolist") else list(vecs))
        except Exception as e:
            failed.append({"start": start, "size": len(batch), "error": f"{type(e).__name__}: {e}"})
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    elapsed = time.perf_counter() - t0
    peak = torch.cuda.max_memory_allocated()/(1024**3) if torch.cuda.is_available() else 0.0

    if failed or len(vectors) != len(sample):
        raise RuntimeError(f"编码未完整完成，failed={failed}, vectors={len(vectors)}, sample={len(sample)}")

    dim = len(vectors[0]) if vectors else 0
    db_path = root / "data/processed/index/qdrant_bge_m3_smoke_v1_1"
    db_path.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=str(db_path))
    collection = "bge_m3_smoke_v1_1"
    existing = {c.name for c in client.get_collections().collections}
    if collection in existing:
        client.delete_collection(collection_name=collection)
    client.create_collection(
        collection_name=collection,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )

    points = []
    for i, (row, vec, tok_len) in enumerate(zip(sample, vectors, token_lengths), 1):
        payload = {
            "chunk_id": row.get("chunk_id"),
            "document_id": row.get("document_id"),
            "parent_id": row.get("parent_id"),
            "content_kind": row.get("content_kind"),
            "section_path": row.get("section_path"),
            "title": row.get("title") or docs.get(row.get("document_id",""), {}).get("title",""),
            "text": row.get("text",""),
            "token_length_untruncated": tok_len,
            "embedding_model": args.model,
            "embedding_version": "smoke_v1_1",
        }
        points.append(PointStruct(id=i, vector=vec, payload=payload))
    client.upsert(collection_name=collection, points=points)
    point_count = client.count(collection_name=collection, exact=True).count

    report = {
        "purpose": "ENGINEERING_SMOKE_TEST_ONLY",
        "model": args.model,
        "device": device,
        "sample_size": len(sample),
        "sample_kind_distribution": dict(Counter(r.get("content_kind","unknown") for r in sample)),
        "verified_gold_requested": len(gold_ids),
        "verified_gold_in_sample": sum(r.get("chunk_id") in gold_ids for r in sample),
        "batch_size": args.batch_size,
        "max_length": args.max_length,
        "token_length_min": min(token_lengths),
        "token_length_avg": sum(token_lengths)/len(token_lengths),
        "token_length_max": max(token_lengths),
        "truncated_chunks": sum(n > args.max_length for n in token_lengths),
        "truncation_rate": sum(n > args.max_length for n in token_lengths)/len(token_lengths),
        "failed_chunks": 0,
        "embedding_dimension": dim,
        "encoding_seconds": elapsed,
        "chunks_per_second": len(sample)/elapsed if elapsed else None,
        "peak_gpu_memory_gib": peak,
        "qdrant_point_count": point_count,
        "qdrant_path": str(db_path),
        "collection": collection,
        "note": "仅工程 Smoke Test，不能证明医学检索精度。",
    }
    out = root / "reports/bge_m3_smoke_v1_1_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告: {out}")

def search(args):
    root = args.root.resolve()
    torch, AutoTokenizer, BGEM3FlagModel, QdrantClient, VectorParams, Distance, PointStruct = imports()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = BGEM3FlagModel(
        args.model,
        use_fp16=bool(torch.cuda.is_available() and not args.fp32),
        devices=device,
    )
    out = model.encode(
        [args.query], batch_size=1, max_length=args.max_length,
        return_dense=True, return_sparse=False, return_colbert_vecs=False
    )
    vec = out["dense_vecs"][0]
    if hasattr(vec, "tolist"):
        vec = vec.tolist()

    client = QdrantClient(path=str(root / "data/processed/index/qdrant_bge_m3_smoke_v1_1"))
    collection = "bge_m3_smoke_v1_1"
    try:
        hits = client.query_points(
            collection_name=collection, query=vec,
            limit=args.top_k, with_payload=True
        ).points
    except AttributeError:
        hits = client.search(
            collection_name=collection, query_vector=vec,
            limit=args.top_k, with_payload=True
        )

    results = []
    for rank, h in enumerate(hits, 1):
        p = h.payload or {}
        results.append({
            "rank": rank,
            "score": float(h.score),
            "chunk_id": p.get("chunk_id"),
            "document_id": p.get("document_id"),
            "content_kind": p.get("content_kind"),
            "section_path": p.get("section_path"),
            "title": p.get("title"),
            "text_preview": (p.get("text") or "")[:700],
        })
    out = root / "reports/bge_m3_smoke_v1_1_results.json"
    out.write_text(json.dumps({"query": args.query, "scope": "SMOKE_SAMPLE_ONLY", "results": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n报告: {out}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build")
    p.add_argument("--model", default=MODEL_NAME)
    p.add_argument("--sample-size", type=int, default=100)
    p.add_argument("--batch-size", type=int, default=2)
    p.add_argument("--max-length", type=int, default=768)
    p.add_argument("--gold-csv", type=Path)
    p.add_argument("--fp32", action="store_true")
    p.set_defaults(func=build)

    p = sub.add_parser("search")
    p.add_argument("--model", default=MODEL_NAME)
    p.add_argument("--query", required=True)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--max-length", type=int, default=768)
    p.add_argument("--fp32", action="store_true")
    p.set_defaults(func=search)

    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
