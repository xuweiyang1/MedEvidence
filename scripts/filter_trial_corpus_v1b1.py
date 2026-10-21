#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V1b.1: Create a *new*, filtered retrieval snapshot from an isolated V1b trial.

The original source and parent_units are preserved verbatim; only new USER_PDF
retrieval children that are clearly page-number-only or reference-list pages are
excluded. This is a conservative retrieval quality filter, not verified PDF parsing.

Default: DRY RUN (prints exclusions). Add --apply to write an independent version.
Never changes the source snapshot, existing BM25/Qdrant, or human review DB.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

FILES = ("documents", "parent_units", "retrieval_chunks", "evidence_relations", "media_registry")
PAGE_RE = re.compile(r"\bPDF Page\s+(\d+)\b", re.I)
SECTION_ID_RE = re.compile(r"^s(\d{4})$")
FOOTER_RE = re.compile(r"^(?:page\s+)?\d{1,4}$", re.I)
# PDF bibliography usually has at least two consecutive bracketed entries on a page.
REF_ENTRY_RE = re.compile(r"(?m)^\s*\[\s*\d{1,3}\s*\]\s+\S")
REF_HEAD_RE = re.compile(r"(?mi)^\s*(?:references|bibliography|works cited)\s*$")


def rows(path: Path):
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8-sig") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{n} is not an object")
            yield obj


def digest(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for part in iter(lambda: f.read(1024*1024), b""):
            h.update(part)
    return h.hexdigest()


def get_pdf_page(chunk: dict) -> int | None:
    match = PAGE_RE.search(str(chunk.get("section_heading_path") or ""))
    if match:
        return int(match.group(1))
    # The page ID exists on original imported text chunks but not other kinds.
    raw = str(chunk.get("section_id") or "")
    match = SECTION_ID_RE.fullmatch(raw)
    if match and str(chunk.get("parent_id") or "").endswith(":parent:body:" + raw):
        return int(match.group(1))
    return None


def inspect(source: Path):
    original_report = json.loads((source / "ingestion_report.json").read_text(encoding="utf-8"))
    if original_report.get("status") != "ISOLATED_TRIAL_UNREVIEWED_NOT_PUBLISHED":
        raise ValueError("只支持 V1b 未发布的隔离试验语料")
    docs = list(rows(source / "documents.jsonl"))
    pdf_docs = {d["document_id"] for d in docs if d.get("source") == "USER_PDF"}
    if not pdf_docs:
        raise ValueError("未找到 USER_PDF 文献；本清理器不处理 JATS 或 PMC 原文")
    chunks = list(rows(source / "retrieval_chunks.jsonl"))
    page_texts = defaultdict(list)
    pages = defaultdict(list)
    for chunk in chunks:
        did = str(chunk.get("document_id") or "")
        if did not in pdf_docs or chunk.get("content_kind") != "body":
            continue
        pg = get_pdf_page(chunk)
        if pg is None:
            continue
        key = (did, pg)
        pages[key].append(chunk)
        page_texts[key].append(chunk.get("text") or "")
    excluded = {}
    flagged_pages = {}
    for key, pieces in page_texts.items():
        full = "\n".join(pieces)
        if len(REF_ENTRY_RE.findall(full)) >= 2 or REF_HEAD_RE.search(full[:200]):
            flagged_pages[key] = "BIBLIOGRAPHY_PAGE_HEURISTIC"
    for key, piece_list in pages.items():
        for chunk in piece_list:
            cid = chunk["chunk_id"]
            text = str(chunk.get("text") or "").strip()
            if FOOTER_RE.fullmatch(text):
                excluded[cid] = "PAGE_NUMBER_ONLY"
            elif key in flagged_pages:
                excluded[cid] = flagged_pages[key]
    excluded_counts = Counter(excluded.values())
    new_chunks = [ch for ch in chunks if ch.get("chunk_id") not in excluded]
    old_by_doc = Counter(c.get("document_id") for c in chunks)
    kept_by_doc = Counter(c.get("document_id") for c in new_chunks)
    for did, count in old_by_doc.items():
        if count and kept_by_doc[did] == 0:
            raise ValueError(f"清理后文献没有可检索 Chunk，拒绝创建新版本：{did}")
    return {"report": original_report, "docs": docs, "chunks": chunks,
            "new_chunks": new_chunks, "excluded": excluded, "excluded_counts": excluded_counts,
            "pdf_docs": pdf_docs, "flagged_pages": flagged_pages,
            "pages_detected": len(pages)}


def create_version(source: Path, target: Path, state: dict, target_version: str):
    if target.exists():
        raise FileExistsError(f"目标语料已存在，拒绝覆盖：{target}")
    # Protect existing version even if user supplies an absolute path or bad name.
    if target.resolve() == source.resolve():
        raise ValueError("输出目录不得等于输入目录")
    tmp = target.with_name("." + target.name + ".tmp_" + uuid.uuid4().hex[:8])
    tmp.mkdir(parents=True, exist_ok=False)
    try:
        for name in ("documents", "parent_units", "media_registry"):
            shutil.copy2(source / f"{name}.jsonl", tmp / f"{name}.jsonl")
        excluded = state["excluded"]
        keep = {c["chunk_id"] for c in state["new_chunks"]}
        if len(keep) != len(state["new_chunks"]):
            raise ValueError("原检索语料中存在重复 Chunk ID")
        # Reconnect surviving sibling chunks (don't leave references to excluded IDs).
        by_parent = defaultdict(list)
        for c in state["new_chunks"]:
            by_parent[str(c.get("parent_id") or "")].append(c)
        affected_parents = {str(c.get("parent_id") or "") for c in state["chunks"]
                            if c.get("chunk_id") in excluded}
        for parent_id, group in by_parent.items():
            if parent_id not in affected_parents:
                continue
            for i, c in enumerate(group):
                if "previous_chunk_id" in c:
                    c["previous_chunk_id"] = group[i-1]["chunk_id"] if i else ""
                if "next_chunk_id" in c:
                    c["next_chunk_id"] = group[i+1]["chunk_id"] if i + 1 < len(group) else ""
        new_next_edges = []
        for parent_id, group in by_parent.items():
            if parent_id not in affected_parents:
                continue
            for a, b in zip(group, group[1:]):
                # Only build new sibling links if original file had them.
                if "previous_chunk_id" in b or "next_chunk_id" in a:
                    new_next_edges.append({"src": a["chunk_id"], "dst": b["chunk_id"], "type": "NEXT_IN_SECTION"})
        with (tmp / "retrieval_chunks.jsonl").open("w", encoding="utf-8") as fp:
            for c in state["new_chunks"]:
                fp.write(json.dumps(c, ensure_ascii=False, separators=(",", ":")) + "\n")
        removed_edges = 0
        edges = []
        for e in rows(source / "evidence_relations.jsonl"):
            if e.get("src") in excluded or e.get("dst") in excluded:
                removed_edges += 1
                continue
            if e.get("type") == "NEXT_IN_SECTION" and (
                any(e.get("src", "").startswith(parent + ":child:") for parent in affected_parents)
                or any(e.get("dst", "").startswith(parent + ":child:") for parent in affected_parents)
            ):
                # Recreate next links consistently for surviving children.
                continue
            edges.append(e)
        edges.extend(new_next_edges)
        known_nodes = {d["document_id"] for d in state["docs"]}
        for p in rows(tmp / "parent_units.jsonl"):
            known_nodes.add(p["parent_id"])
        known_nodes.update(keep)
        for m in rows(tmp / "media_registry.jsonl"):
            known_nodes.add(m["media_id"])
        dangling = [e for e in edges if e.get("src") not in known_nodes or e.get("dst") not in known_nodes]
        if dangling:
            raise ValueError(f"存在 {len(dangling)} 条关系指向不存在的节点（示例：{dangling[0]}）")
        with (tmp / "evidence_relations.jsonl").open("w", encoding="utf-8") as fp:
            for e in edges:
                fp.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
        with (tmp / "excluded_retrieval_chunks.jsonl").open("w", encoding="utf-8") as fp:
            for ch in state["chunks"]:
                if ch["chunk_id"] in excluded:
                    fp.write(json.dumps({"chunk_id": ch["chunk_id"], "document_id": ch["document_id"],
                                         "reason": excluded[ch["chunk_id"]], "text_preview": (ch.get("text") or "")[:160]},
                                        ensure_ascii=False) + "\n")
        counts = {name: sum(1 for _ in rows(tmp / (name + ".jsonl"))) for name in FILES}
        if counts["retrieval_chunks"] != len(state["new_chunks"]):
            raise ValueError("新的 Chunk 数量校验失败")
        report = dict(state["report"])
        report.update({"version": target_version, "derived_from": source.name,
                       "status": "ISOLATED_TRIAL_UNREVIEWED_NOT_PUBLISHED",
                       "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "counts": counts,
                       "retrieval_filter": {"version": "V1b1_BIBLIOGRAPHY_AND_FOOTERS",
                          "status": "HEURISTIC_NOT_MEDICALLY_VERIFIED",
                          "excluded_chunks": len(excluded),
                          "reasons": dict(state["excluded_counts"]),
                          "removed_original_edges": removed_edges,
                          "notes": "Parent 原文保留；排除片段另存 excluded_retrieval_chunks.jsonl；检索视图不等于完整原文"}})
        report["file_sha256"] = {name: digest(tmp / (name + ".jsonl")) for name in FILES}
        (tmp / "ingestion_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        return report
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--from-version", default="v3_2_trial")
    p.add_argument("--to-version", default="v3_2_trial_clean")
    p.add_argument("--apply", action="store_true", help="经过输出审查后才写入新的隔离快照")
    args = p.parse_args()
    for name in (args.from_version, args.to_version):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{3,64}", name):
            raise ValueError("版本名只能含英文字母、数字、_ 或 -")
    if args.from_version == args.to_version:
        raise ValueError("输入和输出版本不能相同")
    root = args.root.resolve()
    source = root / "data/processed/knowledge" / args.from_version
    target = root / "data/processed/knowledge" / args.to_version
    state = inspect(source)
    print(f"输入版本：{source}; 原 Chunk={len(state['chunks'])}")
    print(f"新 PDF 文献={len(state['pdf_docs'])}; 识别 PDF 页面={state['pages_detected']}")
    print(f"排除检索片段={len(state['excluded'])}，类型={dict(state['excluded_counts'])}")
    print(f"清理后 Chunk={len(state['new_chunks'])}，减少={len(state['chunks'])-len(state['new_chunks'])}")
    for cid in list(state["excluded"])[:18]:
        print(f"  {state['excluded'][cid]}  {cid}")
    if not args.apply:
        print("\n当前是 DRY RUN，尚未修改任何文件。确认后追加 --apply 构建新快照。")
        return
    report = create_version(source, target, state, args.to_version)
    print(f"\n隔离清理版本已生成：{target}")
    print(f"文献数={report['counts']['documents']}; Chunk 数={report['counts']['retrieval_chunks']}")
    print("原来的 v3_1_all、v3_2_trial 及旧索引均未修改。")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError, FileExistsError, OSError, KeyError) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
