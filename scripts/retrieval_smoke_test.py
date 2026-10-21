#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V1c.2 retrieval smoke test (no indexing or mutation).

BM25: SQLite FTS5; Dense: BGE-M3 + Qdrant; Hybrid: reciprocal rank fusion.
This script is NOT a relevance/clinical accuracy benchmark.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "raw", "models", "huggingface", ".cache", "site-packages"}
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db3"}
KEY_NAMES = ("chunk_id", "chunk_key", "chunk_uid")
TEXT_NAMES = ("text", "chunk_text", "content", "body", "passage")
DOC_NAMES = ("document_id", "doc_id", "paper_id", "source_id")
STOP_WORDS = {"the", "and", "for", "with", "from", "into", "are", "was", "were", "how", "what", "when", "which", "among", "based", "adult", "adults", "patient", "patients", "management", "recommendation", "guideline", "recommendations", "treatment", "therapy", "clinical", "use", "using", "can", "does", "should", "than", "under", "after"}


def fatal(message: str) -> None:
    raise RuntimeError(message)


def ident(name: str) -> str:
    if not name or "\x00" in name:
        fatal("无效的 SQLite 标识符")
    return '"' + name.replace('"', '""') + '"'


def readonly_connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        fatal(f"SQLite 不存在：{path}")
    # SQLite read-only URI; does not silently create an empty database.
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def db_tables(conn: sqlite3.Connection) -> dict[str, str]:
    return {str(r[0]): (r[1] or "") for r in conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type IN ('table','view')").fetchall()}


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({ident(table)})")]


def find_fts(conn: sqlite3.Connection) -> list[str]:
    return [name for name, ddl in db_tables(conn).items()
            if re.search(r"\bUSING\s+fts5\s*\(", ddl, re.I)]


def walk_index_paths(root: Path):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git")]
        yield Path(base), files


def candidates(root: Path, version: str):
    found_fts: list[tuple[Path, str]] = []
    found_qdrant: list[Path] = []
    for folder, files in walk_index_paths(root):
        if "meta.json" in files and (folder / "collection").is_dir():
            found_qdrant.append(folder)
        for name in files:
            p = folder / name
            if p.suffix.lower() not in SQLITE_SUFFIXES:
                continue
            try:
                with readonly_connect(p) as conn:
                    for table in find_fts(conn):
                        found_fts.append((p, table))
            except (sqlite3.Error, OSError):
                continue
    # Prefer explicitly versioned paths, but list all candidates in inspect mode.
    return found_fts, found_qdrant


def select_fts(root: Path, version: str, db_arg: str | None, table_arg: str | None):
    if db_arg:
        p = Path(db_arg).expanduser().resolve()
        with readonly_connect(p) as conn:
            tables = find_fts(conn)
        if table_arg:
            if table_arg not in tables:
                fatal(f"{p} 中未找到 FTS5 表 {table_arg!r}；可选：{tables}")
            return p, table_arg
        if len(tables) != 1:
            fatal(f"SQLite 有 {len(tables)} 张 FTS5 表，请加 --fts-table，候选：{tables}")
        return p, tables[0]
    fts_all, _ = candidates(root, version)
    versioned = [(p, t) for p, t in fts_all if version in str(p)]
    if table_arg:
        versioned = [(p, t) for p, t in versioned if t == table_arg]
    if len(versioned) == 1:
        return versioned[0]
    if not versioned:
        fatal("未自动确定当前版本的 FTS5 数据库；先执行 --inspect，再用 --bm25-db 指定绝对路径。")
    fatal("找到多套当前版本 FTS5 索引，请用 --bm25-db 和 --fts-table 明确选择：\n" +
          "\n".join(f"  {p} -> {t}" for p, t in versioned))


def select_qdrant_path(root: Path, version: str, arg: str | None) -> Path:
    if arg:
        path = Path(arg).expanduser().resolve()
        if not path.is_dir():
            fatal(f"Qdrant 本地目录不存在：{path}")
        return path
    _, all_q = candidates(root, version)
    versioned = [p for p in all_q if version in str(p)]
    if len(versioned) == 1:
        return versioned[0]
    fatal("未能唯一确定当前版本的 Qdrant 路径；先执行 --inspect，再用 --qdrant-path 指定目录。")


def first_present(values: dict[str, Any], names: tuple[str, ...]) -> Any:
    for key in names:
        value = values.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def nested_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = dict(payload)
    for field in ("metadata", "meta"):
        if isinstance(payload.get(field), dict):
            for k, v in payload[field].items():
                result.setdefault(k, v)
    return result


def external_content_table(ddl: str) -> str | None:
    match = re.search(r"\bcontent\s*=\s*(['\"])(.*?)\1", ddl, re.I | re.S)
    return match.group(2) if match and match.group(2) else None


class BM25:
    def __init__(self, db_path: Path, fts_table: str, key_table: str | None = None,
                 key_column: str = "chunk_id", rowid_column: str = "rowid"):
        self.conn = readonly_connect(db_path)
        self.db_path = db_path
        self.table = fts_table
        tables = db_tables(self.conn)
        if fts_table not in find_fts(self.conn):
            fatal(f"不是 FTS5 表：{fts_table}")
        self.fts_cols = columns(self.conn, fts_table)
        self.key_table = key_table or external_content_table(tables[fts_table])
        self.key_column = key_column
        self.rowid_column = rowid_column
        self.key_cols: list[str] = []
        if self.key_table:
            if self.key_table not in tables:
                fatal(f"映射表 {self.key_table} 不存在；请检查 --key-table")
            self.key_cols = columns(self.conn, self.key_table)
            if key_column not in self.key_cols:
                fatal(f"映射表 {self.key_table} 缺少列 {key_column}；可选：{self.key_cols}")
            if rowid_column != "rowid" and rowid_column not in self.key_cols:
                fatal(f"映射表 {self.key_table} 缺少关联列 {rowid_column}")
        elif not any(n in self.fts_cols for n in KEY_NAMES):
            fatal("FTS5 表没有 chunk_id 字段，也没有可识别的 external-content 映射表。\n"
                  "请显式传入 --key-table、--key-column；必须保证该表关联到 FTS5 rowid。")

    @staticmethod
    def tokenize(text: str) -> str:
        # OR improves diagnostic coverage for long clinical questions; these are
        # smoke-test queries, not a tuned final BM25 retrieval configuration.
        terms = re.findall(r"[A-Za-z][A-Za-z0-9]*|[0-9]+|[\u4e00-\u9fff]{2,}", text)
        dedup = []
        for t in terms:
            if (t.lower() not in STOP_WORDS and t.lower() not in {x.lower() for x in dedup}
                    and len(t) >= 2):
                dedup.append(t)
        # Each token quoted -> FTS5 punctuation-safe, no operator injection.
        return " OR ".join('"' + x.replace('"', '""') + '"' for x in dedup[:12])

    def fetch_lookup(self, rid: int) -> dict[str, Any]:
        if not self.key_table:
            return {}
        sql = (f"SELECT * FROM {ident(self.key_table)} "
               f"WHERE {'rowid' if self.rowid_column == 'rowid' else ident(self.rowid_column)}=? LIMIT 1")
        row = self.conn.execute(sql, (rid,)).fetchone()
        return dict(row) if row else {}

    def search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        expr = self.tokenize(query)
        if not expr:
            return []
        t = ident(self.table)
        sql = (f"SELECT rowid AS __fts_rowid__, *, rank AS __rank__ "
               f"FROM {t} WHERE {t} MATCH ? ORDER BY rank LIMIT ?")
        result = []
        for row in self.conn.execute(sql, (expr, top_k)):
            data = dict(row)
            extra = self.fetch_lookup(int(data["__fts_rowid__"]))
            merged = {**data, **{k: v for k, v in extra.items() if v is not None}}
            chunk_id = extra.get(self.key_column) if self.key_table else first_present(data, KEY_NAMES)
            if chunk_id is None or not str(chunk_id).strip():
                fatal(f"BM25 命中 rowid={data['__fts_rowid__']} 无 chunk_id；需修复 ID 映射，禁止错误融合。")
            text = first_present(merged, TEXT_NAMES) or ""
            result.append({"chunk_id": str(chunk_id), "doc_id": first_present(merged, DOC_NAMES),
                           "score": float(data["__rank__"]), "text": str(text)[:800],
                           "fts_rowid": int(data["__fts_rowid__"])})
        return result

    def close(self):
        self.conn.close()


def load_questions(path: Path, count: int) -> list[dict[str, str]]:
    if not path.is_file():
        fatal(f"找不到问题文件：{path}")
    qs = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(qs, list) or not qs:
        fatal("问题文件必须是非空 JSON 数组")
    out = []
    seen = set()
    for i, item in enumerate(qs, 1):
        if isinstance(item, str):
            question = item
            qid = f"Q{i:02d}"
        elif isinstance(item, dict):
            question = item.get("question") or item.get("query")
            qid = str(item.get("id") or f"Q{i:02d}")
        else:
            fatal(f"第 {i} 题格式错误")
        if not isinstance(question, str) or not question.strip() or qid in seen:
            fatal(f"问题缺失、为空或 ID 重复：第 {i} 题")
        seen.add(qid)
        out.append({"id": qid, "question": question.strip()})
    return out[:count]


class Dense:
    def __init__(self, qdrant_path: Path, collection: str, model_path: str, device: str, max_length: int,
                 batch_size: int, vector_name: str | None):
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:
            fatal("缺少 qdrant-client：python -m pip install qdrant-client")
        # Qdrant local may place a lock; no upsert/create_collection/delete occurs.
        self.client = QdrantClient(path=str(qdrant_path))
        self.collection = collection
        names = [c.name for c in self.client.get_collections().collections]
        if collection not in names:
            fatal(f"集合 {collection!r} 不存在。当前集合：{names}")
        info = self.client.get_collection(collection)
        vectors = info.config.params.vectors
        if isinstance(vectors, dict):
            if vector_name is None:
                if len(vectors) == 1:
                    vector_name = next(iter(vectors))
                else:
                    fatal(f"当前集合含多个命名向量：{list(vectors)}；指定 --vector-name")
            if vector_name not in vectors:
                fatal(f"向量名称 {vector_name} 不在集合中：{list(vectors)}")
            self.size = int(vectors[vector_name].size)
        else:
            if vector_name:
                fatal("此集合使用默认向量，不能指定 --vector-name")
            self.size = int(vectors.size)
        self.vector_name = vector_name
        if not model_path:
            fatal("必须指定 --model-path，且应与构建索引时使用的模型一致")
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError:
            fatal("缺少 FlagEmbedding：python -m pip install FlagEmbedding")
        self.encoder = BGEM3FlagModel(model_path, use_fp16=device == "cuda",
                                    devices=["cuda:0"] if device == "cuda" else ["cpu"])
        self.max_length, self.batch_size = max_length, batch_size

    def embed(self, questions: list[str]) -> list[list[float]]:
        result = self.encoder.encode(questions, batch_size=self.batch_size,
                                     max_length=self.max_length,
                                     return_dense=True, return_sparse=False, return_colbert_vecs=False)
        vecs = result["dense_vecs"]
        if len(vecs[0]) != self.size:
            fatal(f"嵌入维度 {len(vecs[0])} 与 Qdrant 向量维度 {self.size} 不一致；检查构建模型和 named vector")
        return [vec.tolist() if hasattr(vec, "tolist") else list(vec) for vec in vecs]

    def search(self, vector: list[float], top_k: int) -> list[dict[str, Any]]:
        response = self.client.query_points(collection_name=self.collection, query=vector,
                                            using=self.vector_name, limit=top_k, with_payload=True)
        data = []
        for hit in response.points:
            payload = nested_payload(hit.payload)
            key = first_present(payload, KEY_NAMES)
            if key is None:
                fatal(f"Qdrant 命中 point={hit.id!r} 的 payload 中没有 chunk_id；禁止将 point ID 和 SQLite rowid 强行拼接。")
            value = first_present(payload, TEXT_NAMES) or ""
            data.append({"chunk_id": str(key), "doc_id": first_present(payload, DOC_NAMES),
                         "score": float(hit.score), "text": str(value)[:800], "point_id": str(hit.id)})
        return data

    def close(self):
        self.client.close()


def rrf_merge(bm25: list[dict[str, Any]], dense: list[dict[str, Any]], k: int = 60) -> list[dict[str, Any]]:
    fused: dict[str, dict[str, Any]] = {}
    for mode, items in (("bm25", bm25), ("dense", dense)):
        seen = set()
        for rank, item in enumerate(items, 1):
            key = item["chunk_id"]
            if key in seen:
                continue
            seen.add(key)
            if key not in fused:
                fused[key] = {"chunk_id": key, "doc_id": item.get("doc_id"), "text": item.get("text", ""),
                              "rrf_score": 0.0, "ranks": {"bm25": None, "dense": None}}
            row = fused[key]
            row["rrf_score"] += 1.0 / (k + rank)
            row["ranks"][mode] = rank
            if not row["text"] and item.get("text"):
                row["text"] = item["text"]
            if row["doc_id"] is None and item.get("doc_id") is not None:
                row["doc_id"] = item["doc_id"]
    return sorted(fused.values(), key=lambda x: (-x["rrf_score"],
                                                 min(v or 10**9 for v in x["ranks"].values()),
                                                 x["chunk_id"]))


def md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def write_report(output: Path, rows: list[dict[str, Any]], meta: dict[str, Any], show_k: int) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps({"meta": meta, "questions": rows}, ensure_ascii=False,
                                                   indent=2, default=str), encoding="utf-8")
    lines = ["# MedEvidence 检索冒烟测试（不是准确率评测）", "", f"- 索引版本：`{meta['version']}`",
             f"- 问题数：{len(rows)}", f"- BM25 数据库：`{meta['bm25_db']}`",
             f"- Qdrant：`{meta['qdrant_path']}` / `{meta['collection']}`",
             f"- Dense 编码模型：`{meta['model_path']}`", "- 融合方法：RRF(k=60)，没有 Cross-Encoder rerank", "",
             "## 概览", "", "| ID | BM25命中 | Dense命中 | Hybrid候选 | BM25耗时ms | Dense耗时ms |",
             "|---|---:|---:|---:|---:|---:|"]
    for item in rows:
        counts, times = item["counts"], item["latency_ms"]
        lines.append(f"| {item['id']} | {counts['bm25']} | {counts['dense']} | {counts['hybrid']} | {times['bm25']:.1f} | {times['dense']:.1f} |")
    lines += ["", "**注意**：无人工标注的相关性标签；结果数量、融合排名和延迟不等于 Recall/MRR/nDCG，也不代表医学结论正确。",
              "", "## 逐题 Top 结果", ""]
    for item in rows:
        lines.extend([f"### {item['id']} — {item['question']}", ""])
        for mode in ("bm25", "dense", "hybrid"):
            hits = item[mode][:show_k]
            lines += [f"**{mode.upper()}**", "", "| 排名 | Chunk ID | Doc ID | 分数 | 摘要 |",
                      "|---:|---|---|---:|---|"]
            for pos, hit in enumerate(hits, 1):
                score = hit.get("rrf_score", hit.get("score", 0.0))
                excerpt = (hit.get("text", "") or "").strip().replace("\n", " ")[:160]
                lines.append(f"| {pos} | {md_escape(hit['chunk_id'])} | {md_escape(hit.get('doc_id') or '')} | {score:.5g} | {md_escape(excerpt)} |")
            if not hits:
                lines.append("| — | 无命中 | | | |")
            lines.append("")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def inspect(root: Path, version: str, args) -> None:
    fts, qdrant = candidates(root, version)
    print(f"[INSPECT] root={root}, version={version}")
    if not fts:
        print("没有在扫描范围发现 FTS5 数据库；可用 --bm25-db 显式指定路径")
    for db, table in fts:
        with readonly_connect(db) as con:
            ddl = db_tables(con).get(table, "")
            ext = external_content_table(ddl)
            print(f"FTS5: {db} | table={table} | columns={columns(con, table)} | external_content={ext}")
            if ext and ext in db_tables(con):
                print(f"      base_table={ext} | columns={columns(con, ext)}")
    if not qdrant:
        print("没有自动发现 Qdrant local 的 meta.json + collection/ 目录；可用 --qdrant-path")
    for path in qdrant:
        print(f"QDRANT PATH: {path}")
    # Optional deeper inspection when concrete directory is specified.
    q_arg = args.qdrant_path
    if q_arg:
        try:
            from qdrant_client import QdrantClient
            client = QdrantClient(path=str(Path(q_arg).resolve()))
            for c in client.get_collections().collections:
                inf = client.get_collection(c.name)
                print(f"QDRANT COLLECTION: {c.name} | points={inf.points_count} | vectors={inf.config.params.vectors}")
                point, _ = client.scroll(collection_name=c.name, limit=1, with_payload=True, with_vectors=False)
                if point:
                    print(f"      sample payload fields: {list(nested_payload(point[0].payload).keys())}")
            client.close()
        except Exception as e:
            print(f"Qdrant 检查失败：{type(e).__name__}: {e}")
    print("[提示] 若字段不是 chunk_id，请先检查索引生成脚本；不要直接强制融合不同 ID 空间。")


def main() -> int:
    parser = argparse.ArgumentParser(description="MedEvidence BM25/Dense/RRF 检索冒烟测试 (不写入索引)")
    parser.add_argument("--root", default=".", help="MedEvidence 项目根目录")
    parser.add_argument("--version", required=True, help="示例：v3_2_trial_clean")
    parser.add_argument("--inspect", action="store_true", help="仅检查路径、字段、数据库，无需加载模型")
    parser.add_argument("--bm25-db", help="SQLite FTS5 文件路径；推荐明确传入")
    parser.add_argument("--fts-table", help="FTS5 表名")
    parser.add_argument("--key-table", help="FTS5 rowid 对应的基表；如果 FTS 不包含 chunk_id 则需要")
    parser.add_argument("--key-column", default="chunk_id", help="基表的 Chunk ID 列名")
    parser.add_argument("--key-rowid-column", default="rowid", help="基表与 FTS rowid 关联的列名")
    parser.add_argument("--qdrant-path", help="Qdrant local 存储目录；推荐明确传入")
    parser.add_argument("--collection", help="默认：bge_m3_<version>")
    parser.add_argument("--vector-name", help="集合有多个命名向量时必填")
    parser.add_argument("--model-path", help="构建时所用 BGE-M3 模型的本地目录或 HF 模型 ID")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--show-k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--questions", help="问题 JSON 路径；默认与本脚本同目录的 smoke_questions.json")
    parser.add_argument("--output-dir", help="输出目录；默认 outputs/retrieval_smoke/<version>/<UTC时间>")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.version):
        fatal("--version 仅允许字母、数字、下划线、点和连字符")
    if args.top_k <= 0 or args.show_k <= 0 or args.limit <= 0 or args.batch_size <= 0 or args.max_length <= 0:
        fatal("top-k/show-k/limit/batch-size/max-length 必须是正数")
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        fatal(f"项目根目录不存在：{root}")
    if args.inspect:
        inspect(root, args.version, args)
        return 0
    db, table = select_fts(root, args.version, args.bm25_db, args.fts_table)
    qpath = select_qdrant_path(root, args.version, args.qdrant_path)
    questions_path = Path(args.questions).resolve() if args.questions else Path(__file__).with_name("smoke_questions.json")
    questions = load_questions(questions_path, args.limit)
    collection = args.collection or f"bge_m3_{args.version}"
    bm25 = BM25(db, table, args.key_table, args.key_column, args.key_rowid_column)
    dense = None
    try:
        dense = Dense(qpath, collection, args.model_path, args.device, args.max_length,
                      args.batch_size, args.vector_name)
        print(f"测试开始：{args.version}; 问题 {len(questions)} 个; BM25={table}; Qdrant={collection}", flush=True)
        t0 = time.perf_counter()
        vectors = dense.embed([q["question"] for q in questions])
        embedding_ms = (time.perf_counter() - t0) * 1000
        print(f"问题向量编码完成（{embedding_ms:.0f} ms）", flush=True)
        rows = []
        for q, vec in zip(questions, vectors):
            tic = time.perf_counter()
            b = bm25.search(q["question"], args.top_k)
            b_ms = (time.perf_counter() - tic) * 1000
            tic = time.perf_counter()
            d = dense.search(vec, args.top_k)
            d_ms = (time.perf_counter() - tic) * 1000
            h = rrf_merge(b, d)
            rows.append({"id": q["id"], "question": q["question"], "bm25": b, "dense": d,
                         "hybrid": h, "counts": {"bm25": len(b), "dense": len(d), "hybrid": len(h)},
                         "latency_ms": {"bm25": b_ms, "dense": d_ms}})
            print(f"{q['id']}: BM25={len(b)} Dense={len(d)} Hybrid={len(h)} "
                  f"latency={b_ms:.0f}+{d_ms:.0f}ms", flush=True)
        out = (Path(args.output_dir).expanduser().resolve() if args.output_dir else
               root / "outputs" / "retrieval_smoke" / args.version / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        meta = {"version": args.version, "collection": collection, "model_path": args.model_path,
                "bm25_db": str(db), "fts_table": table, "qdrant_path": str(qpath),
                "top_k": args.top_k, "device": args.device, "vector_name": dense.vector_name,
                "query_embedding_total_ms": round(embedding_ms, 1), "timestamp_utc": datetime.now(timezone.utc).isoformat()}
        write_report(out, rows, meta, args.show_k)
        print(f"\n结果：{out / 'report.md'}\n原始数据：{out / 'results.json'}")
        return 0
    finally:
        bm25.close()
        if dense:
            dense.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (sqlite3.Error, RuntimeError, OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(2)
