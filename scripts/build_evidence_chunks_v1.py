#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence: full-text, traceable Evidence Chunk builder (V1).

Python 3.11+, standard library only. No GPU/API/network required.
Place under: MedEvidence/scripts/build_evidence_chunks_v1.py

Run:
    python scripts/build_evidence_chunks_v1.py --limit 10
    python scripts/build_evidence_chunks_v1.py

Input (read only):
  data/processed/eligibility/pmc_candidate_manifest.csv
  data/processed/ontology/pmc_articles_disease_mapped.jsonl (preferred, optional)
  data/processed/normalized/pmc_articles.jsonl (fallback)

Output (separate smoke/full paths; prior output of the *same* run mode is replaced):
  data/processed/chunks/pmc_evidence_chunks[_smoke].jsonl
  reports/evidence_chunk_report[_smoke].csv
  reports/evidence_chunk_summary[_smoke].txt

Key guarantees:
  - Only V3 CANDIDATE articles with matching registered source-XML hash.
  - Every nonempty abstract/body block is included exactly once, no keyword filtering.
  - Chunks do not cross section boundaries; each stores block char offsets for traceability.
  - Long paragraphs split at a nearby boundary without dropping characters.
  - MeSH/DO tags are DOCUMENT metadata, not asserted mentions in each chunk.
  - CANDIDATE permits controlled local experiments, not public redistribution.
  - Tables, figure images, equations not parsed upstream remain coverage gaps.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PMCID_RE = re.compile(r"PMC\d+", re.I)
SHA_RE = re.compile(r"[a-f0-9]{64}", re.I)
REPORT_COLUMNS = [
    "document_id", "status", "reason", "total_chunks", "abstract_chunks",
    "body_chunks", "source_blocks", "long_blocks_split", "source_chars",
    "covered_chars", "coverage_percent", "doid_count", "pubmed_meta_matched",
    "unparsed_tables", "unparsed_figures", "quality_warnings"
]


def load_candidates(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"缺少 V3 候选清单: {path}")
    data = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        needed = {"document_id", "v3_decision", "sha256_expected"}
        if not reader.fieldnames or not needed.issubset(reader.fieldnames):
            raise ValueError(f"候选清单字段缺失: {sorted(needed-set(reader.fieldnames or []))}")
        for row in reader:
            did = (row.get("document_id") or "").strip().upper()
            digest = (row.get("sha256_expected") or "").strip().lower()
            if (not PMCID_RE.fullmatch(did) or did in data
                    or row.get("v3_decision") != "CANDIDATE" or not SHA_RE.fullmatch(digest)):
                raise ValueError(f"候选状态、文献ID、SHA256或唯一性异常: {did}")
            data[did] = digest
    if not data:
        raise ValueError("候选清单为空")
    return data


def safe_source_text(node):
    if not isinstance(node, dict):
        return ""
    val = node.get("text", "")
    if not isinstance(val, str):
        raise ValueError("章节块的 text 应为字符串")
    return val


def split_without_loss(text: str, max_chars: int):
    """Yield (start, end) offsets. Split long text without deleting whitespace."""
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            lo = start + max(1, max_chars // 2)
            # Prefer nearby paragraph, sentence, then whitespace boundary.
            candidates = []
            for marker in ("\n", ". ", "; ", ".\n", "。", "！", "？", "? ", "! ", " "):
                pos = text.rfind(marker, lo, end)
                if pos >= lo:
                    candidates.append(pos + len(marker))
            if candidates:
                end = max(candidates)
        if end <= start:
            end = min(start + max_chars, n)
        yield start, end
        start = end


def source_sections(article):
    for i, item in enumerate(article.get("abstract_sections") or [], 1):
        value = safe_source_text(item)
        if value:
            yield ("abstract", f"a{i:04d}", [item.get("heading", "") or "Abstract"],
                   [(f"a{i:04d}-b0001", "abstract", value)])
    for i, item in enumerate(article.get("body_sections") or [], 1):
        if not isinstance(item, dict):
            raise ValueError("正文 section 必须是对象")
        sid = str(item.get("section_id") or f"s{i:04d}")
        headings = item.get("heading_path") or [item.get("heading") or "Untitled section"]
        if not isinstance(headings, list):
            headings = [str(headings)]
        heading_path = [str(x) for x in headings if x]
        blocks = []
        for j, block in enumerate(item.get("blocks") or [], 1):
            value = safe_source_text(block)
            if value:
                bid = str(block.get("block_id") or f"{sid}-b{j:04d}")
                blocks.append((bid, str(block.get("block_type", "p")), value))
        if blocks:
            yield ("body", sid, heading_path, blocks)


def sha256_string(text: str):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_article(article: dict, max_chars: int):
    did = article["document_id"].upper()
    result = []
    count = Counter()
    total_original = 0
    total_covered = 0
    serial = 0
    document_title = str(article.get("title", ""))
    document_tags = {
        "doids": article.get("disease_ontology_ids", []),
        "disease_entities": article.get("disease_entities", []),
        "mesh_disease_descriptors": article.get("mesh_disease_descriptors", []),
        "source_topic_tags": article.get("source_topic_tags", []),
        "tag_scope": "DOCUMENT_ONLY_NOT_CHUNK_ENTITY_CLAIM",
    }

    for kind, section_id, headings, blocks in source_sections(article):
        units = []
        for block_id, block_type, value in blocks:
            count["source_blocks"] += 1
            total_original += len(value)
            parts = list(split_without_loss(value, max_chars))
            if len(parts) > 1:
                count["long_blocks_split"] += 1
            # Validate exact byte-for-byte character coverage of every source block.
            cursor = 0
            for start, end in parts:
                if start != cursor or not (start < end <= len(value)):
                    raise ValueError(f"文本偏移不连续: {did}/{block_id}")
                units.append({"block_id": block_id, "block_type": block_type,
                              "start_char": start, "end_char": end,
                              "text": value[start:end]})
                cursor = end
            if cursor != len(value):
                raise ValueError(f"源段落未完整覆盖: {did}/{block_id}")

        group = []
        group_chars = 0

        def flush():
            nonlocal serial, group, group_chars, total_covered
            if not group:
                return
            serial += 1
            joined_text = "\n\n".join(unit["text"] for unit in group)
            original_segments = [
                {"block_id": u["block_id"], "block_type": u["block_type"],
                 "start_char": u["start_char"], "end_char": u["end_char"],
                 "segment_sha256": sha256_string(u["text"])}
                for u in group
            ]
            total_covered += sum(u["end_char"] - u["start_char"] for u in group)
            header = " | ".join(x for x in [document_title, " > ".join(headings)] if x)
            rec = {
                "schema_version": "openmedkb.chunk.v1",
                "chunk_id": f"{did}-{kind}-{section_id}-c{serial:05d}",
                "document_id": did,
                "source": "PMC",
                "title": document_title,
                "pmcid": article.get("pmcid", did),
                "pmid": article.get("pmid", ""),
                "doi": article.get("doi", ""),
                "publication_year": article.get("publication_year"),
                "publication_types_raw": article.get("publication_types_raw", ""),
                "content_kind": kind,
                "section_id": section_id,
                "section_heading_path": headings,
                "text": joined_text,
                "embedding_text": (header + "\n" + joined_text) if header else joined_text,
                "text_sha256": sha256_string(joined_text),
                "source_char_count": sum(u["end_char"] - u["start_char"] for u in group),
                "source_segments": original_segments,
                "document_tags": document_tags,
                "rights": article.get("rights", {}),
                "provenance": article.get("provenance", {}),
                "document_quality_warnings": article.get("quality_warnings", []),
                "unparsed_table_count_in_document": article.get("skipped_table_count", 0),
                "unparsed_figure_count_in_document": article.get("skipped_figure_count", 0),
                "note": "No chunk-level PICO/NER claim is made; source data may omit tables/figures."
            }
            result.append(rec)
            count[kind + "_chunks"] += 1
            group = []
            group_chars = 0

        for unit in units:
            proposed = group_chars + (2 if group else 0) + len(unit["text"])
            if group and proposed > max_chars:
                flush()
            group.append(unit)
            group_chars += (2 if len(group) > 1 else 0) + len(unit["text"])
        flush()

    if total_original != total_covered:
        raise ValueError(f"分块覆盖率不等于100%: {did}: {total_covered}/{total_original}")
    if not result:
        raise ValueError(f"文章没有可分块的摘要和正文: {did}")
    count["total_chunks"] = len(result)
    count["source_chars"] = total_original
    count["covered_chars"] = total_covered
    return result, count


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--limit", type=int, default=0, help="Smoke test first N documents")
    p.add_argument("--max-chars", type=int, default=1200, help="Max raw chunk text chars, default 1200")
    p.add_argument("--input", type=Path, default=None, help="Optional input JSONL relative to project root")
    a = p.parse_args()
    if a.limit < 0 or a.max_chars < 300:
        p.error("--limit must be >= 0 and --max-chars must be >= 300")
    root = a.root.expanduser().resolve()
    cands_file = root / "data/processed/eligibility/pmc_candidate_manifest.csv"
    mapped = root / "data/processed/ontology/pmc_articles_disease_mapped.jsonl"
    normal = root / "data/processed/normalized/pmc_articles.jsonl"
    if a.input is not None:
        inp_path = (root / a.input).resolve()
        if not inp_path.is_relative_to(root):
            print("[ERROR] 输入必须位于项目目录内", file=sys.stderr)
            return 2
    else:
        inp_path = mapped if mapped.exists() else normal
    suffix = "_smoke" if a.limit else ""
    out_path = root / f"data/processed/chunks/pmc_evidence_chunks{suffix}.jsonl"
    report_path = root / f"reports/evidence_chunk_report{suffix}.csv"
    summary_path = root / f"reports/evidence_chunk_summary{suffix}.txt"
    tmp_out = out_path.with_name(out_path.name + ".tmp")
    tmp_report = report_path.with_name(report_path.name + ".tmp")
    try:
        candidates = load_candidates(cands_file)
        if not inp_path.is_file():
            raise FileNotFoundError(f"结构化文章文件不存在: {inp_path}")
        for dst in (out_path, report_path, summary_path):
            dst.parent.mkdir(parents=True, exist_ok=True)
        doc_count = 0
        observed_ids = set()
        aggregate = Counter()
        with inp_path.open("r", encoding="utf-8-sig") as inp, \
                tmp_out.open("w", encoding="utf-8", newline="\n") as out, \
                tmp_report.open("w", encoding="utf-8-sig", newline="") as report:
            writer = csv.DictWriter(report, fieldnames=REPORT_COLUMNS)
            writer.writeheader()
            for line_number, line in enumerate(inp, 1):
                if not line.strip():
                    continue
                if a.limit and doc_count >= a.limit:
                    break
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"输入JSONL第 {line_number} 行不是合法JSON: {exc}") from exc
                did = str(doc.get("document_id", "")).strip().upper()
                if did not in candidates or did in observed_ids:
                    raise ValueError(f"出现非候选/重复文献: {did} (line {line_number})")
                if doc.get("source") != "PMC" or doc.get("rights", {}).get("audit_status") != "CANDIDATE":
                    raise ValueError(f"文献状态不属于PMC候选集: {did}")
                actual_hash = str(doc.get("provenance", {}).get("source_xml_sha256", "")).lower()
                if actual_hash != candidates[did]:
                    raise ValueError(f"文献原始XML哈希与候选登记不一致: {did}")
                chunks, stats = chunk_article(doc, a.max_chars)
                for chunk in chunks:
                    out.write(json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n")
                observed_ids.add(did)
                doc_count += 1
                aggregate.update(stats)
                writer.writerow({
                    "document_id": did, "status": "OK", "reason": "",
                    "total_chunks": stats["total_chunks"],
                    "abstract_chunks": stats["abstract_chunks"],
                    "body_chunks": stats["body_chunks"],
                    "source_blocks": stats["source_blocks"],
                    "long_blocks_split": stats["long_blocks_split"],
                    "source_chars": stats["source_chars"],
                    "covered_chars": stats["covered_chars"],
                    "coverage_percent": "100.00",
                    "doid_count": len(doc.get("disease_ontology_ids", [])),
                    "pubmed_meta_matched": doc.get("pubmed_meta_matched", False),
                    "unparsed_tables": doc.get("skipped_table_count", 0),
                    "unparsed_figures": doc.get("skipped_figure_count", 0),
                    "quality_warnings": ";".join(doc.get("quality_warnings", [])),
                })
                if doc_count % 100 == 0:
                    print(f"[Progress] articles={doc_count}, chunks={aggregate['total_chunks']}")
        if not a.limit and observed_ids != set(candidates):
            missing = sorted(set(candidates) - observed_ids)
            raise ValueError(f"正式语料不完整: 已处理{len(observed_ids)}/{len(candidates)}篇；缺失示例: {missing[:5]}")
        os.replace(tmp_out, out_path)
        os.replace(tmp_report, report_path)
        summary = [
            "=== MedEvidence OpenMedKB Evidence Chunk V1 ===",
            f"时间UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"模式: {'SMOKE' if a.limit else 'FULL'}",
            f"输入JSONL: {inp_path.relative_to(root)}",
            f"V3候选集文章: {len(candidates)}",
            f"成功处理文章: {doc_count}",
            f"生成chunk数: {aggregate['total_chunks']}",
            f"摘要chunk数: {aggregate['abstract_chunks']}",
            f"正文chunk数: {aggregate['body_chunks']}",
            f"原文文本块数: {aggregate['source_blocks']}",
            f"过长而切分的源块数: {aggregate['long_blocks_split']}",
            f"原文字符总数: {aggregate['source_chars']}",
            f"已覆盖原文字符数: {aggregate['covered_chars']}",
            f"原文段落覆盖率: {100 if aggregate['source_chars']==aggregate['covered_chars'] else 'ERROR'}%",
            f"每个chunk原文长度上限: {a.max_chars}",
            f"Chunk JSONL: {out_path.relative_to(root)}",
            f"逐篇报告: {report_path.relative_to(root)}",
            "MeSH/DO只作为文档级标签，不用于筛掉段落；无标签文章也全部保留。",
            "此处的100%覆盖率仅指上游已解析的摘要和正文块，不包括被上游跳过的表格/图像/公式等。",
            "许可证CANDIDATE仅允许受控本地试验，尚未授予公开再分发许可。",
        ]
        summary_path.write_text("\n".join(summary) + "\n", encoding="utf-8-sig")
        print("\n".join(summary))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        for tmp in (tmp_out, tmp_report):
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        return 2


if __name__ == "__main__":
    sys.exit(main())
