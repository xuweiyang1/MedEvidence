#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence Dense V1b: isolated version-only inspect / build / search with BGE-M3 + Qdrant.

Usage (from MedEvidence project root):
  python scripts/medevidence_dense_v1.py inspect
  python scripts/medevidence_dense_v1.py build --expected-count 42526
  python scripts/medevidence_dense_v1.py search --limit-queries 2
  python scripts/medevidence_dense_v1.py search

Requires: FlagEmbedding, qdrant-client, torch (CUDA version). All paths below
are relative to --root; existing smoke-test index is never modified.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_CORPUS = "data/processed/knowledge/v3_1_all/retrieval_chunks.jsonl"
DEFAULT_DOCS = "data/processed/knowledge/v3_1_all/documents.jsonl"
DEFAULT_QDRANT = "data/processed/index/qdrant_bge_m3_dense_v1"
DEFAULT_BM25 = "reports/bm25_v1_1_review_candidates.csv"
DEFAULT_DENSE = "reports/dense_v1b_review_candidates.csv"
COLLECTION = "bge_m3_dense_v1"
MODEL_NAME = "BAAI/bge-m3"
VECTOR_SIZE = 1024

ID_KEYS = ("chunk_id", "retrieval_chunk_id", "evidence_chunk_id", "id")
DOC_KEYS = ("document_id", "pmcid", "paper_id", "article_id", "document_pmcid", "doc_id")
TEXT_KEYS = ("text", "content", "chunk_text", "retrieval_text", "text_for_embedding", "embedding_text", "child_text", "passage", "plain_text", "body_text", "text_content", "evidence_text", "retrieval_content", "chunk_content", "embedding_input", "passage_text", "content_text", "normalized_text")
TITLE_KEYS = ("title", "article_title", "document_title")
KIND_KEYS = ("content_kind", "kind", "chunk_kind", "type")
SECTION_KEYS = ("section", "section_path", "section_title", "heading")


def pick(obj: dict, keys: tuple[str, ...]) -> str:
    for name in keys:
        value = obj.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, dict):
            for inner in ("text", "content", "name", "value", "id"):
                v = value.get(inner)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return ""


def root_path(root: Path, value: str | Path) -> Path:
    p = Path(value)
    return p if p.is_absolute() else root / p


def jsonl_rows(path: Path) -> Iterator[tuple[int, dict]]:
    with path.open("r", encoding="utf-8-sig") as fp:
        for lineno, line in enumerate(fp, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"无效 JSON: {path}, 第 {lineno} 行: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"第 {lineno} 行不是 JSON 对象: {path}")
            yield lineno, item


def doc_id_from_chunk_id(cid: str) -> str:
    part = cid.split(":", 1)[0]
    return part if part.upper().startswith("PMC") else ""


def normalize_chunk(raw: dict, lineno: int) -> dict:
    cid = pick(raw, ID_KEYS)
    did = pick(raw, DOC_KEYS) or doc_id_from_chunk_id(cid)
    txt = pick(raw, TEXT_KEYS)
    if not cid or not did or not txt:
        raise ValueError(
            f"retrieval_chunks.jsonl 第 {lineno} 行缺少必要字段："
            f"chunk_id={bool(cid)}, document_id={bool(did)}, text={bool(txt)}; "
            f"原始键={list(raw)[:35]}。请发这行的脱敏字段结构以调整映射，勿跳过。"
        )
    return {
        "chunk_id": cid,
        "document_id": did,
        "text": txt,
        "title": pick(raw, TITLE_KEYS),
        "content_kind": pick(raw, KIND_KEYS),
        "section": pick(raw, SECTION_KEYS),
    }


def inspect(args: argparse.Namespace) -> None:
    path = root_path(args.root, args.corpus)
    if not path.is_file():
        raise FileNotFoundError(path)
    print(f"语料: {path}")
    ok = 0
    for lineno, item in jsonl_rows(path):
        print(f"\n[第 {lineno} 行] 原始键: {list(item.keys())}")
        ch = normalize_chunk(item, lineno)
        print(json.dumps({**ch, "text": ch["text"][:200]}, ensure_ascii=False, indent=2))
        ok += 1
        if ok >= args.rows:
            break
    print(f"\n预览验证成功: {ok} 条。字段如正确，再执行 build。")


def load_titles(path: Path) -> dict[str, str]:
    titles = {}
    if not path.is_file():
        raise FileNotFoundError(f"论文元数据文件不存在: {path}")
    for _, doc in jsonl_rows(path):
        did = pick(doc, DOC_KEYS + ("id",))
        title = pick(doc, TITLE_KEYS)
        if did and title:
            titles[did] = title
    return titles


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def open_qdrant(path: Path):
    from qdrant_client import QdrantClient
    path.mkdir(parents=True, exist_ok=True)
    return QdrantClient(path=str(path))


def close_qdrant(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def load_model(args: argparse.Namespace):
    import torch
    if args.device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA 不可用，请确认 (xwy) 环境中的 PyTorch/CUDA。")
    from FlagEmbedding import BGEM3FlagModel
    return BGEM3FlagModel(MODEL_NAME, use_fp16=(args.device == "cuda"), devices=["cuda:0" if args.device == "cuda" else "cpu"])


def dense_embed(model: Any, texts: list[str], args: argparse.Namespace) -> Any:
    out = model.encode(
        texts,
        batch_size=args.batch_size,
        max_length=args.max_length,
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False,
    )
    if "dense_vecs" not in out:
        raise RuntimeError("BGE-M3 未返回 dense_vecs，检查 FlagEmbedding 版本")
    vectors = out["dense_vecs"]
    if len(vectors) != len(texts):
        raise RuntimeError(f"向量数 {len(vectors)} != 文本数 {len(texts)}")
    if vectors.shape[1] != VECTOR_SIZE:
        raise RuntimeError(f"Embedding 维度不是 {VECTOR_SIZE}: {vectors.shape}")
    return vectors


def build(args: argparse.Namespace) -> None:
    from qdrant_client import models

    path = root_path(args.root, args.corpus)
    docs_path = root_path(args.root, args.docs)
    index_path = root_path(args.root, args.index)
    # Forbid reusing the default V3.1 Qdrant directory in trial builder.
    if index_path.resolve() == root_path(args.root, DEFAULT_QDRANT).resolve():
        raise ValueError("V1b 禁止使用旧 Qdrant 目录；必须显式指定 --index data/processed/index/v3_2_trial/qdrant_bge_m3")
    manifest_path = index_path / "medevidence_dense_manifest_v1.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    titles = load_titles(docs_path)
    # Verify all rows before opening/writing the database: no silently dropped evidence.
    print("预检全部 JSONL 行、文档 ID 和 Chunk ID（不执行向量计算）…", flush=True)
    total = 0
    kinds = Counter()
    seen = set()
    first: dict | None = None
    for lineno, raw in jsonl_rows(path):
        ch = normalize_chunk(raw, lineno)
        if first is None:
            first = ch
        if ch["chunk_id"] in seen:
            raise ValueError(f"存在重复 chunk_id: {ch['chunk_id']}")
        seen.add(ch["chunk_id"])
        kinds[ch["content_kind"] or "unknown"] += 1
        total += 1
    print(f"语料行数={total}; 种类={dict(kinds)}; 论文标题映射={len(titles)}")
    print(f"首条 ID={first['chunk_id'] if first else '无'}; 文本片段={(first['text'][:90] if first else '')!r}")
    if args.expected_count and total != args.expected_count:
        raise ValueError(f"实际 {total} 条与 --expected-count {args.expected_count} 不符。请检查语料文件。")
    if total == 0:
        raise ValueError("空语料，停止建库")

    fp = hash_file(path)
    config = {"model": MODEL_NAME, "device": args.device, "batch_size": args.batch_size, "max_length": args.max_length,
              "use_fp16": args.device == "cuda", "corpus": str(path), "corpus_sha256": fp, "docs": str(docs_path),
              "docs_sha256": hash_file(docs_path), "dimension": VECTOR_SIZE, "count": total, "collection": args.collection}
    if manifest_path.is_file() and not args.reset:
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if all(old.get(k) == config[k] for k in config):
            c = open_qdrant(index_path)
            try:
                points = c.count(args.collection, exact=True).count
            finally:
                close_qdrant(c)
            if points == total:
                print(f"完整索引已存在（{points} points）；无需重建。可运行 search。")
                return
        raise RuntimeError("已有索引元数据与当前输入/参数不同。确认后加 --reset 重建（仅覆盖独立 Dense v1 collection）。")
    if not args.reset and index_path.exists() and any(index_path.iterdir()):
        raise RuntimeError(f"目录已存在且没有匹配的完整 manifest: {index_path}。检查文件或加 --reset。")

    model = load_model(args)
    client = open_qdrant(index_path)
    created = False
    t0 = time.perf_counter()
    try:
        if client.collection_exists(args.collection):
            if not args.reset:
                raise RuntimeError("Collection 已存在；请检查后加 --reset")
            client.delete_collection(args.collection)
        client.create_collection(collection_name=args.collection,
                                 vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=models.Distance.COSINE))
        created = True
        batch: list[dict] = []
        inserted = 0

        def flush() -> None:
            nonlocal inserted
            if not batch:
                return
            vecs = dense_embed(model, [x["text"] for x in batch], args)
            points = []
            for ch, vec in zip(batch, vecs):
                pid = str(uuid.uuid5(uuid.NAMESPACE_URL, "MedEvidence:v3_1:" + ch["chunk_id"]))
                points.append(models.PointStruct(
                    id=pid,
                    vector=vec.tolist(),
                    payload={"chunk_id": ch["chunk_id"], "document_id": ch["document_id"],
                             "title": ch["title"] or titles.get(ch["document_id"], ""),
                             "content_kind": ch["content_kind"], "section": ch["section"],
                             "text_preview": ch["text"][:1600]},
                ))
            client.upsert(collection_name=args.collection, points=points, wait=True)
            inserted += len(batch)
            batch.clear()
            if inserted % (args.write_batch_size * 10) == 0 or inserted == total:
                print(f"进度: {inserted}/{total} chunks，耗时 {time.perf_counter()-t0:.1f}s", flush=True)

        for lineno, raw in jsonl_rows(path):
            batch.append(normalize_chunk(raw, lineno))
            if len(batch) >= args.write_batch_size:
                flush()
        flush()
        point_count = client.count(collection_name=args.collection, exact=True).count
        if point_count != total or inserted != total:
            raise RuntimeError(f"索引点数校验失败：入库={inserted}，Qdrant={point_count}，输入={total}")
    finally:
        close_qdrant(client)
    if not created:
        raise RuntimeError("Collection 未创建")
    duration = round(time.perf_counter() - t0, 3)
    manifest_path.write_text(json.dumps({**config, "encoded_seconds": duration,
                                         "point_count": total, "created_at_utc": datetime.now(timezone.utc).isoformat()},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = root_path(args.root, f"reports/dense_{args.collection}_build_summary.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"建库成功: {total} chunks，{duration}s；索引: {index_path}；报告: {summary_path}")


def load_queries(path: Path, limit: int = 0) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(path)
    items: dict[str, dict] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            qid = (row.get("query_id") or row.get("question_id") or row.get("id") or "").strip()
            query = (row.get("query") or row.get("question") or row.get("question_text") or "").strip()
            if not qid or not query:
                raise ValueError(f"{path} 第 {n} 行缺少 query_id 或 query。原始字段={list(row.keys())}")
            typ = (row.get("query_type") or "").strip()
            if qid in items and items[qid]["query"] != query:
                raise ValueError(f"同一个 query_id={qid} 对应不同查询文本，需先清洗")
            if qid not in items:
                items[qid] = {"query_id": qid, "query": query, "query_type": typ}
    result = list(items.values())
    if not result:
        raise ValueError(f"问题文件没有有效问题: {path}")
    return result[:limit] if limit else result


def qdrant_hits(client: Any, collection: str, vector: list[float], limit: int) -> list[Any]:
    if hasattr(client, "query_points"):
        return client.query_points(collection_name=collection, query=vector, limit=limit, with_payload=True).points
    return client.search(collection_name=collection, query_vector=vector, limit=limit, with_payload=True)


def search(args: argparse.Namespace) -> None:
    index_path = root_path(args.root, args.index)
    manifest_path = index_path / "medevidence_dense_manifest_v1.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"未发现完整索引 manifest: {manifest_path}。请先 build。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("model") != MODEL_NAME or manifest.get("max_length") != args.max_length:
        raise RuntimeError("检索编码参数与索引不同，请使用建库时的 max_length/model")
    if manifest.get("device") != args.device:
        print("提示：查询使用的计算设备与建库不同；FP16 差异可能造成微小排名变化。", file=sys.stderr)
    queries_file = root_path(args.root, args.queries)
    queries = load_queries(queries_file, limit=args.limit_queries)
    print(f"问题来源: {queries_file}；独立 query_id 数量: {len(queries)}")
    if args.queries == DEFAULT_BM25:
        print("提示：从 BM25 候选 CSV 取问题，可能遗漏 BM25 没有任何候选的问题；正式实验应使用完整的统一问题清单。")
    model = load_model(args)
    client = open_qdrant(index_path)
    output = root_path(args.root, args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = ("query_id", "query_type", "query", "paper_rank", "document_id", "title",
               "best_chunk_id", "content_kind", "section", "dense_score", "chunk_excerpt", "text_preview")
    total_candidates = 0
    low_hit = []
    try:
        point_count = client.count(collection_name=args.collection, exact=True).count
        if point_count != manifest["count"]:
            raise RuntimeError(f"索引数量与 manifest 不一致：{point_count} != {manifest['count']}")
        with output.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            for i in range(0, len(queries), args.query_batch_size):
                batch = queries[i:i + args.query_batch_size]
                vectors = dense_embed(model, [q["query"] for q in batch], args)
                for q, vec in zip(batch, vectors):
                    hits = qdrant_hits(client, args.collection, vec.tolist(), args.fetch_chunks)
                    used = set()
                    rank = 0
                    for hit in hits:
                        payload = hit.payload or {}
                        did = (payload.get("document_id") or "").strip()
                        if not did or did in used:
                            continue
                        used.add(did)
                        rank += 1
                        preview = payload.get("text_preview", "")
                        writer.writerow({
                            **q,
                            "paper_rank": rank,
                            "document_id": did,
                            "title": payload.get("title", ""),
                            "best_chunk_id": payload.get("chunk_id", ""),
                            "content_kind": payload.get("content_kind", ""),
                            "section": payload.get("section", ""),
                            "dense_score": float(hit.score),
                            "chunk_excerpt": preview,
                            "text_preview": preview,
                        })
                        total_candidates += 1
                        if rank >= args.topk:
                            break
                    if rank < args.topk:
                        low_hit.append({"query_id": q["query_id"], "num_documents": rank})
                print(f"搜索完成 {min(i + len(batch), len(queries))}/{len(queries)} 个问题", flush=True)
    finally:
        close_qdrant(client)
    summary_path = root_path(args.root, f"reports/dense_{args.collection}_search_summary.json")
    summary_path.write_text(json.dumps({"model": MODEL_NAME, "corpus_sha256": manifest["corpus_sha256"],
                                        "queries_file": str(queries_file), "query_count": len(queries),
                                        "candidate_rows": total_candidates, "topk_papers": args.topk,
                                        "fetch_chunks": args.fetch_chunks,
                                        "less_than_topk": low_hit, "output": str(output)},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Dense 候选文件: {output}\n问题数={len(queries)}, 候选论文行数={total_candidates}；报告: {summary_path}")
    if low_hit:
        print(f"注意：{len(low_hit)} 个问题没有获取到足够的不同论文，考虑增大 --fetch-chunks。")


def cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MedEvidence Dense V1: BGE-M3 + Qdrant (本地)")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    sub = p.add_subparsers(dest="command", required=True)
    inspect_p = sub.add_parser("inspect", help="检查检索语料字段，建库前必做")
    inspect_p.add_argument("--corpus", default=DEFAULT_CORPUS)
    inspect_p.add_argument("--rows", type=int, default=3)
    build_p = sub.add_parser("build", help="建完整 Dense 索引；不覆盖 Smoke Test")
    build_p.add_argument("--corpus", default=DEFAULT_CORPUS)
    build_p.add_argument("--docs", default=DEFAULT_DOCS)
    build_p.add_argument("--index", default=DEFAULT_QDRANT)
    build_p.add_argument("--collection", default=COLLECTION)
    build_p.add_argument("--expected-count", type=int, default=0)
    build_p.add_argument("--reset", action="store_true", help="明确允许删除并重建 Dense v1 collection")
    build_p.add_argument("--write-batch-size", type=int, default=128)
    search_p = sub.add_parser("search", help="按论文去重，导出 Dense Top-K 盲评候选")
    search_p.add_argument("--index", default=DEFAULT_QDRANT)
    search_p.add_argument("--collection", default=COLLECTION)
    search_p.add_argument("--queries", default=DEFAULT_BM25)
    search_p.add_argument("--output", default=DEFAULT_DENSE)
    search_p.add_argument("--topk", type=int, default=10)
    search_p.add_argument("--fetch-chunks", type=int, default=300)
    search_p.add_argument("--query-batch-size", type=int, default=8)
    search_p.add_argument("--limit-queries", type=int, default=0)
    for subp in (build_p, search_p):
        subp.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
        subp.add_argument("--batch-size", type=int, default=2)
        subp.add_argument("--max-length", type=int, default=768)
    args = p.parse_args()
    args.root = args.root.resolve()
    return args


if __name__ == "__main__":
    try:
        args = cli()
        {"inspect": inspect, "build": build, "search": search}[args.command](args)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"\n错误：{e}", file=sys.stderr)
        sys.exit(1)
