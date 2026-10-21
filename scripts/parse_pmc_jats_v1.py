#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence / OpenMedKB: parse License Gate V3 candidates into traceable JATS JSONL.

Run from the PyCharm terminal (Python 3.11; standard library only):
    python scripts/parse_pmc_jats_v1.py --limit 10   # smoke run, separate outputs
    python scripts/parse_pmc_jats_v1.py              # full run

Inputs (read-only):
    data/processed/eligibility/pmc_candidate_manifest.csv
    data/raw/pmc/articles/.../*.xml
    data/raw/pubmed/pubmed_meta.csv (optional metadata enrichment)

Outputs (generated files only; old output for the same mode is replaced atomically):
    data/processed/normalized/pmc_articles[ _smoke ].jsonl
    reports/pmc_parse_report[ _smoke ].csv
    reports/pmc_parse_summary[ _smoke ].txt

This script does not approve licenses. CANDIDATE permits controlled local experiments
only; do not publicly redistribute the resulting documents without rights review.
Tables, figure images, formulas and supplementary files are NOT fully parsed in V1.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

WHITESPACE = re.compile(r"\s+")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}", re.I)
PMCID_PATTERN = re.compile(r"PMC\d+", re.I)
REPORT_FIELDS = [
    "document_id", "topic", "status", "reason", "source_xml", "sha256_ok",
    "pmid", "doi", "pubmed_match", "sections", "body_blocks", "abstract_chars",
    "body_chars", "skipped_tables", "skipped_figures",
]


def tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def child(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    for item in node:
        if tag(item) == name:
            return item
    return None


def children(node: ET.Element | None, name: str) -> list[ET.Element]:
    return [] if node is None else [item for item in node if tag(item) == name]


def nested(node: ET.Element | None, *names: str) -> ET.Element | None:
    for name in names:
        node = child(node, name)
    return node


def text(node: ET.Element | None) -> str:
    """Flatten inline JATS markup while keeping text in document order."""
    return WHITESPACE.sub(" ", "".join(node.itertext())).strip() if node is not None else ""


def project_file(root: Path, relative: str) -> Path:
    """Resolve only paths within the project, avoiding absolute/path traversal inputs."""
    value = (relative or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        raise ValueError("invalid relative source XML path")
    target = (root / value).resolve()
    if not target.is_relative_to(root):
        raise ValueError("XML path points outside the project")
    return target


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for piece in iter(lambda: f.read(1024 * 1024), b""):
            h.update(piece)
    return h.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"CSV 缺少表头: {path}")
        return list(reader)


def read_pubmed(root: Path) -> dict[str, dict[str, str]]:
    file = root / "data/raw/pubmed/pubmed_meta.csv"
    if not file.is_file():
        print("[WARN] 未找到 pubmed_meta.csv，跳过 PMID/MeSH 补充。")
        return {}
    out = {}
    duplicates = 0
    for row in read_csv(file):
        match = PMCID_PATTERN.search(row.get("pmcid", "") or "")
        if match:
            key = match.group().upper()
            if key in out:
                duplicates += 1
            else:
                out[key] = row
    if duplicates:
        print(f"[WARN] PubMed 元数据有 {duplicates} 个重复 PMCID；使用首条记录。")
    return out


def ids(article_meta: ET.Element | None) -> dict[str, str]:
    result = {}
    for item in children(article_meta, "article-id"):
        kind = (item.attrib.get("pub-id-type") or "").lower().strip()
        if kind in {"pmc", "pmcid", "pmid", "doi"}:
            result[kind] = text(item)
    return result


def abstract_sections(article_meta: ET.Element | None) -> list[dict[str, str]]:
    result = []
    if article_meta is None:
        return result
    for abstract in children(article_meta, "abstract") + children(article_meta, "trans-abstract"):
        label = abstract.attrib.get("abstract-type", "")
        structured = children(abstract, "sec")
        if structured:
            # Preserve structured abstract headings (Background/Methods/Results/...).
            lead = [text(p) for p in children(abstract, "p")]
            lead_value = "\n".join(p for p in lead if p)
            if lead_value:
                result.append({"abstract_type": label,
                               "heading": text(child(abstract, "title")),
                               "text": lead_value})
            for sec in structured:
                parts = [text(p) for p in sec.iter() if tag(p) == "p"]
                value = "\n".join(p for p in parts if p)
                if value:
                    result.append({"abstract_type": label,
                                   "heading": text(child(sec, "title")), "text": value})
        else:
            parts = [text(n) for n in abstract.iter() if tag(n) == "p"]
            parts = [p for p in parts if p]
            if not parts:
                # Some JATS abstracts have plain text without <p> elements.
                v = text(abstract)
                if v:
                    parts.append(v)
            if parts:
                result.append({"abstract_type": label,
                               "heading": text(child(abstract, "title")),
                               "text": "\n".join(parts)})
    return result


def get_blocks(node: ET.Element, section_key: str, block_count: int) -> tuple[list[dict[str, str]], int, int, int]:
    """Read only direct content children to prevent duplicated nested-section text."""
    blocks = []
    table_count = figure_count = 0
    for n in node:
        kind = tag(n)
        if kind == "sec" or kind == "title":
            continue
        if kind == "table-wrap":
            table_count += 1
            continue  # v1 intentionally omits unstructured table contents
        if kind in {"fig", "fig-group"}:
            figure_count += 1
            continue
        if kind in {"p", "disp-quote", "preformat", "speech", "verse-group"}:
            value = text(n)
            block_type = kind
        elif kind == "list":
            elements = [text(x) for x in n.iter() if tag(x) == "list-item"]
            value = "\n".join(f"- {item}" for item in elements if item)
            block_type = "list"
        else:
            # Exclude graphics, formulas, acknowledgments, permissions,
            # and other objects until a dedicated parser handles them.
            continue
        if value:
            block_count += 1
            blocks.append({"block_id": f"{section_key}-b{block_count:04d}",
                           "block_type": block_type, "text": value})
    return blocks, block_count, table_count, figure_count


def body_sections(body: ET.Element | None) -> tuple[list[dict], int, int]:
    if body is None:
        return [], 0, 0
    sections = []
    skipped_tables = skipped_figures = 0
    serial = 0

    def walk(node: ET.Element, headings: list[str], ancestor_id: str, prefix: str) -> None:
        nonlocal serial, skipped_tables, skipped_figures
        title = text(child(node, "title")) if tag(node) == "sec" else ""
        path = headings + ([title] if title else [])
        serial += 1
        section_id = f"s{serial:04d}"
        blocks, _, tables, figures = get_blocks(node, section_id, 0)
        skipped_tables += tables
        skipped_figures += figures
        if blocks or tag(node) == "sec":
            sections.append({
                "section_id": section_id,
                "jats_section_id": node.attrib.get("id", ""),
                "parent_section_id": ancestor_id,
                "heading": title or prefix,
                "heading_path": path,
                "blocks": blocks,
            })
        # Traverse nested sections even if their parent section contains no paragraphs.
        for n in node:
            if tag(n) == "sec":
                walk(n, path, section_id, "Untitled section")

    walk(body, [], "", "Body (unsectioned)")
    return sections, skipped_tables, skipped_figures


def normalize_year(value: str) -> int | None:
    v = (value or "").strip()
    return int(v) if re.fullmatch(r"(?:19|20)\d{2}", v) else None


def parse_article(xml_path: Path, row: dict[str, str], meta: dict[str, str] | None,
                  actual_sha: str) -> dict:
    try:
        article = ET.parse(xml_path).getroot()
    except ET.ParseError as e:
        raise ValueError(f"JATS XML 格式无法解析: {e}") from e
    if tag(article) != "article":
        raise ValueError(f"根节点不是 <article>，而是 <{tag(article)}>")
    front = child(article, "front")
    article_meta = child(front, "article-meta")
    if article_meta is None:
        raise ValueError("缺少 front/article-meta")
    xml_ids = ids(article_meta)
    expected_pmcid = (row.get("document_id") or "").strip().upper()
    xml_pmcid = xml_ids.get("pmc") or xml_ids.get("pmcid") or ""
    if xml_pmcid and not xml_pmcid.upper().startswith("PMC"):
        xml_pmcid = "PMC" + xml_pmcid
    if xml_pmcid and xml_pmcid.upper() != expected_pmcid:
        raise ValueError(f"PMCID 不一致: manifest={expected_pmcid}, XML={xml_pmcid}")
    heading = text(nested(article_meta, "title-group", "article-title"))
    title = heading or (row.get("title") or "").strip()
    if not title:
        raise ValueError("XML/manifest 均无标题")
    abstracts = abstract_sections(article_meta)
    body, skipped_tables, skipped_figures = body_sections(child(article, "body"))
    if not abstracts and not body:
        raise ValueError("未解析出摘要或正文段落")
    meta = meta or {}
    journal_title = text(nested(front, "journal-meta", "journal-title-group", "journal-title"))
    # Use the XML metadata when the PubMed enrichment record is unavailable.
    if not journal_title:
        journal_title = text(nested(front, "journal-meta", "journal-title"))
    year_xml = ""
    for date_node in children(article_meta, "pub-date"):
        year_xml = text(child(date_node, "year"))
        if normalize_year(year_xml) is not None:
            break
    keywords = [text(k) for group in children(article_meta, "kwd-group")
                for k in children(group, "kwd")]
    xml_pmid = xml_ids.get("pmid", "")
    xml_doi = xml_ids.get("doi", "")
    pmid = xml_pmid or meta.get("pmid", "")
    doi = xml_doi or meta.get("doi", "")
    warnings = []
    if xml_pmid and meta.get("pmid") and xml_pmid != meta["pmid"]:
        warnings.append("XML_PUBMED_PMID_DIFFERENCE")
    if xml_doi and meta.get("doi") and xml_doi.lower() != meta["doi"].lower():
        warnings.append("XML_PUBMED_DOI_DIFFERENCE")
    if not body:
        warnings.append("BODY_PARAGRAPHS_NOT_FOUND")
    if skipped_tables:
        warnings.append("TABLE_CONTENT_NOT_PARSED")
    if skipped_figures:
        warnings.append("FIGURE_CONTENT_NOT_PARSED")
    original_path = row["local_path"].replace("\\", "/")
    doc = {
        "schema_version": "openmedkb.article.v1",
        "document_id": expected_pmcid,
        "source": "PMC",
        "pmcid": expected_pmcid,
        "pmid": pmid,
        "doi": doi,
        "title": title,
        "journal": meta.get("journal", "") or journal_title,
        "publication_year": normalize_year(meta.get("year", "")) or normalize_year(year_xml),
        "publication_types_raw": meta.get("pubtypes", ""),
        "jats_article_type": article.attrib.get("article-type", ""),
        "keywords": [k for k in keywords if k],
        "mesh_terms_raw": meta.get("mesh", ""),
        "pubmed_meta_matched": bool(meta),
        "source_topic_tags": [v.strip() for v in re.split(r"[;,|]", row.get("disease", "")) if v.strip()],
        "disease_ontology_ids": [],  # Filled in ontology mapping stage, not guessed.
        "rights": {
            "license_normalized": row.get("license_normalized", ""),
            "license_url": row.get("license_url", ""),
            "audit_status": row.get("v3_decision", ""),
            "restriction": "LOCAL_RESEARCH_ONLY_NOT_LEGAL_APPROVAL",
        },
        "provenance": {
            "source_url": f"https://pmc.ncbi.nlm.nih.gov/articles/{expected_pmcid}/",
            "source_xml": original_path,
            "source_xml_sha256": actual_sha,
            "sha256_expected": row.get("sha256_expected", ""),
            "parser": "parse_pmc_jats_v1",
        },
        "abstract_sections": abstracts,
        "body_sections": body,
        "quality_warnings": warnings,
        "skipped_table_count": skipped_tables,
        "skipped_figure_count": skipped_figures,
    }
    return doc


def atomic_replace(temp: Path, destination: Path) -> None:
    os.replace(temp, destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="MedEvidence project root (by default: script's parent project)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Run a separate smoke test with N articles (never overwrites full outputs)")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be >= 0")
    root = args.root.expanduser().resolve()
    candidate_path = root / "data/processed/eligibility/pmc_candidate_manifest.csv"
    try:
        candidates = read_csv(candidate_path)
    except (OSError, ValueError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    required = {"document_id", "local_path", "v3_decision", "sha256_expected", "license_normalized"}
    if not candidates or not required.issubset(candidates[0]):
        print(f"[ERROR] 候选表为空或缺少字段: {sorted(required - set(candidates[0]) if candidates else required)}", file=sys.stderr)
        return 2
    # Fail closed if the eligibility manifest has unsafe records.
    unauthorized = [r for r in candidates if r.get("v3_decision") != "CANDIDATE"]
    if unauthorized:
        print(f"[ERROR] 清单含 {len(unauthorized)} 条非 CANDIDATE 记录；停止处理。", file=sys.stderr)
        return 2
    ids_seen = set()
    for r in candidates:
        docid = r.get("document_id", "").upper()
        if not PMCID_PATTERN.fullmatch(docid) or docid in ids_seen:
            print(f"[ERROR] PMCID 缺失、非法或重复: {docid}", file=sys.stderr)
            return 2
        ids_seen.add(docid)
    pubmed = read_pubmed(root)
    if args.limit:
        candidates = candidates[:args.limit]
    suffix = "_smoke" if args.limit else ""
    normalized_out = root / f"data/processed/normalized/pmc_articles{suffix}.jsonl"
    report_out = root / f"reports/pmc_parse_report{suffix}.csv"
    summary_out = root / f"reports/pmc_parse_summary{suffix}.txt"
    for path in [normalized_out, report_out, summary_out]:
        path.parent.mkdir(parents=True, exist_ok=True)

    tmp_data = normalized_out.with_name(normalized_out.name + ".tmp")
    tmp_report = report_out.with_name(report_out.name + ".tmp")
    status = Counter()
    parsed = 0
    body_blocks = 0
    section_count = 0
    matched_pubmed = 0
    total_tables = total_figures = 0
    try:
        with tmp_data.open("w", encoding="utf-8", newline="\n") as out, \
             tmp_report.open("w", encoding="utf-8-sig", newline="") as report_file:
            report = csv.DictWriter(report_file, fieldnames=REPORT_FIELDS)
            report.writeheader()
            for idx, row in enumerate(candidates, 1):
                docid = row["document_id"].strip().upper()
                record = dict.fromkeys(REPORT_FIELDS, "")
                record.update(document_id=docid, topic=row.get("disease", ""), status="ERROR",
                              source_xml=row.get("local_path", ""), sha256_ok="False")
                try:
                    expected = row.get("sha256_expected", "").strip().lower()
                    if not SHA256_PATTERN.fullmatch(expected):
                        raise ValueError("候选清单缺少合法的预期 SHA-256，拒绝解析")
                    xml_path = project_file(root, row.get("local_path", ""))
                    if not xml_path.is_file():
                        raise FileNotFoundError(f"文件不存在: {xml_path}")
                    actual = sha256_file(xml_path)
                    if actual != expected:
                        raise ValueError("XML SHA-256 与候选登记不一致，拒绝解析")
                    record["sha256_ok"] = "True"
                    metadata = pubmed.get(docid)
                    doc = parse_article(xml_path, row, metadata, actual)
                    # Safe only after per-document hash/license eligibility checks.
                    out.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n")
                    parsed += 1
                    count = sum(len(section["blocks"]) for section in doc["body_sections"])
                    body_blocks += count
                    section_count += len(doc["body_sections"])
                    matched_pubmed += bool(metadata)
                    total_tables += doc["skipped_table_count"]
                    total_figures += doc["skipped_figure_count"]
                    record.update(status="OK", reason=";".join(doc["quality_warnings"]),
                                  pmid=doc["pmid"], doi=doc["doi"],
                                  pubmed_match=str(doc["pubmed_meta_matched"]),
                                  sections=len(doc["body_sections"]), body_blocks=count,
                                  abstract_chars=sum(len(x["text"]) for x in doc["abstract_sections"]),
                                  body_chars=sum(len(b["text"]) for s in doc["body_sections"] for b in s["blocks"]),
                                  skipped_tables=doc["skipped_table_count"],
                                  skipped_figures=doc["skipped_figure_count"])
                except (ValueError, OSError, ET.ParseError) as e:
                    record["reason"] = f"{type(e).__name__}: {e}"
                status[record["status"]] += 1
                report.writerow(record)
                if idx % 100 == 0 or idx == len(candidates):
                    print(f"[Progress] {idx}/{len(candidates)}; parsed={parsed}; errors={status['ERROR']}")
        atomic_replace(tmp_data, normalized_out)
        atomic_replace(tmp_report, report_out)
    except (OSError, UnicodeError) as e:
        print(f"[ERROR] 写出失败，保留旧正式文件: {e}", file=sys.stderr)
        for p in [tmp_data, tmp_report]:
            if p.exists():
                p.unlink()
        return 2

    summary = [
        "=== MedEvidence OpenMedKB JATS 标准化 V1 ===",
        f"生成时间UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"项目目录: {root}",
        f"运行模式: {'SMOKE' if args.limit else 'FULL'}",
        f"本次候选输入: {len(candidates)}",
        f"成功解析: {status['OK']}",
        f"失败/跳过: {status['ERROR']}",
        f"成功关联PubMed元数据: {matched_pubmed}",
        f"正文sections: {section_count}",
        f"正文文本块: {body_blocks}",
        f"未完整解析的表格: {total_tables}",
        f"未完整解析的图片: {total_figures}",
        f"输出JSONL: {normalized_out.relative_to(root)}",
        f"逐篇报告: {report_out.relative_to(root)}",
        "仅处理 V3 候选清单；逐篇复核 XML 的 SHA256；不修改任何原始数据/许可表。",
        "source_topic_tags 是原检索域标签，不是 MeSH/DO 标准疾病实体。",
        "V1 未提取完整表格、图片、公式、附录；不能将未解析内容视为不存在。",
        "CANDIDATE 仅用于受控本地实验，不代表版权最终批准或允许公开再分发。",
    ]
    summary_out.write_text("\n".join(summary) + "\n", encoding="utf-8-sig")
    print("\n".join(summary))
    print("\n下一个步骤：检查 reports/pmc_parse_report*.csv 中的 ERROR，再上传摘要报告。")
    return 0 if not status["ERROR"] else 1


if __name__ == "__main__":
    sys.exit(main())
