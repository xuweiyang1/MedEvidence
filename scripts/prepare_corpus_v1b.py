#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V1b: 从已暂存 PDF/JATS XML 生成独立的 V3.2 试验语料。

不写入现有 V3.1、旧 Qdrant、旧 BM25 或 Streamlit 审核数据库。
仅建立 ISOLATED_TRIAL_UNREVIEWED 语料；不得将其作为临床验证证据发布。
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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from build_hierarchical_chunks_v3_1 import build_one

NAMES = ("documents", "parent_units", "retrieval_chunks", "evidence_relations", "media_registry")
STAGED_MAX = 25 * 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_jsonl(path: Path):
    with path.open(encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, 1):
            if line.strip():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"JSONL 错误 {path}:{lineno}: {e}") from e
                if not isinstance(record, dict):
                    raise ValueError(f"JSONL 行不是对象 {path}:{lineno}")
                yield record


def local_tag(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def node_text(node) -> str:
    return re.sub(r"\s+", " ", " ".join(node.itertext())).strip() if node is not None else ""


def direct(node, name: str):
    return next((x for x in node if local_tag(x.tag) == name), None) if node is not None else None


def direct_all(node, name: str):
    return [x for x in node if local_tag(x.tag) == name] if node is not None else []


def safe_xml(blob: bytes):
    # stdlib ElementTree; avoid DTD and entity declarations in untrusted uploads.
    if b"<!DOCTYPE" in blob.upper() or b"<!ENTITY" in blob.upper():
        raise ValueError("含 DTD / ENTITY 的 XML 拒绝解析")
    root = ET.fromstring(blob)
    if local_tag(root.tag) != "article":
        raise ValueError("只接受 JATS <article> XML")
    return root


def parse_jats(blob: bytes, manifest: dict, did: str) -> tuple[dict, list[str]]:
    root = safe_xml(blob)
    article_meta = root.find(".//front/article-meta")
    if article_meta is None:
        article_meta = next((x for x in root.iter() if local_tag(x.tag) == "article-meta"), None)
    if article_meta is None:
        raise ValueError("JATS 缺少 article-meta，不允许猜测元数据")
    title_node = next((x for x in article_meta.iter() if local_tag(x.tag) == "article-title"), None)
    extracted_title = node_text(title_node)
    ins = manifest["inspection"]
    # Article metadata IDs are authoritative *candidates* within the uploaded XML;
    # never trust IDs from References or paragraph text.
    ids = {}
    for element in article_meta.iter():
        if local_tag(element.tag) == "article-id":
            t = str(element.attrib.get("pub-id-type") or "").lower()
            if t in ("pmc", "doi", "pmid"):
                ids[{"pmc":"pmcid", "doi":"doi", "pmid":"pmid"}[t]] = node_text(element)
    for field, extracted in ids.items():
        given = str(ins.get(field) or "").strip()
        a = normalize_doi(given) if field == "doi" else given.upper() if field == "pmcid" else given
        b = normalize_doi(extracted) if field == "doi" else extracted.upper() if field == "pmcid" else extracted
        if a and b and a != b:
            raise ValueError(f"XML 主书目 {field} 与暂存填写值不一致：{given} != {extracted}")
        if not given and extracted:
            ins[field] = extracted
    title = str(ins.get("title") or extracted_title).strip()
    if not title or title.startswith("（"):
        raise ValueError("缺少可核对标题")
    warnings = []
    if extracted_title and extracted_title.casefold() != title.casefold():
        warnings.append("暂存标题与 JATS article-title 不一致，需人工核对")
    abstract_sections = []
    for a in article_meta.iter():
        if local_tag(a.tag) == "abstract":
            t = node_text(a)
            if t:
                abstract_sections.append({"heading": "Abstract", "text": t})
    body = direct(root, "body")
    if body is None:
        raise ValueError("JATS 没有 <body>，本版不接收仅摘要文献")
    sections = []
    media = {"tables": [], "figures": []}
    sid_no = 0
    med_no = 0
    consumed_paragraphs = set()
    consumed_media = set()

    def scan_section(node, parent_sid: str, path: list[str]):
        nonlocal sid_no, med_no
        sid_no += 1
        sid = f"s{sid_no:04d}"
        header = node_text(direct(node, "title")) if local_tag(node.tag) == "sec" else "Body"
        header = header or "Untitled section"
        heading_path = path + [header]
        blocks = []
        child_sections = []
        for child in node:
            typ = local_tag(child.tag)
            if typ == "sec":
                child_sections.append(child)
            elif typ == "p":
                consumed_paragraphs.add(id(child))
                txt = node_text(child)
                if txt:
                    blocks.append({"block_id": f"{sid}-b{len(blocks)+1:04d}", "block_type": "p", "text": txt})
            elif typ == "table-wrap":
                consumed_media.add(id(child))
                med_no += 1
                table_name = str(child.attrib.get("id") or f"table{med_no:04d}")
                caption = node_text(direct(child, "caption"))
                label = node_text(direct(child, "label"))
                footnotes = node_text(direct(child, "table-wrap-foot"))
                rows = []
                has_span = False
                table = next((x for x in child.iter() if local_tag(x.tag) == "table"), None)
                if table is not None:
                    for tr in table.iter():
                        if local_tag(tr.tag) != "tr":
                            continue
                        cells = []
                        for cell in tr:
                            if local_tag(cell.tag) not in ("td", "th"):
                                continue
                            if cell.attrib.get("colspan", "1") != "1" or cell.attrib.get("rowspan", "1") != "1":
                                has_span = True
                            cells.append({"text": node_text(cell), "is_header": local_tag(cell.tag) == "th"})
                        if cells:
                            rows.append(cells)
                if not rows:
                    warnings.append(f"{table_name} 无可提取单元格；仅表题可以检索")
                if has_span:
                    warnings.append(f"{table_name} 包含跨行/跨列单元格，尚未验证表格结构")
                media["tables"].append({"id": table_name, "jats_id": table_name, "section_id": sid,
                    "section_heading_path": heading_path, "caption": caption, "label": label,
                    "rows": rows, "footnotes": footnotes, "graphics": [], "referenced_by": []})
            elif typ == "fig":
                consumed_media.add(id(child))
                med_no += 1
                fid = str(child.attrib.get("id") or f"fig{med_no:04d}")
                imgrefs = []
                for graphic in child.iter():
                    if local_tag(graphic.tag) == "graphic":
                        href = next((v for k, v in graphic.attrib.items() if local_tag(k) == "href"), "")
                        if href:
                            imgrefs.append({"xlink_href": href, "status": "NOT_DOWNLOADED"})
                media["figures"].append({"id": fid, "jats_id": fid, "section_id": sid,
                    "section_heading_path": heading_path, "caption": node_text(direct(child, "caption")),
                    "label": node_text(direct(child, "label")), "alt_text": [],
                    "graphics": imgrefs, "referenced_by": []})
        sections.append({"section_id": sid, "parent_section_id": parent_sid,
            "heading_path": heading_path, "blocks": blocks})
        for child_section in child_sections:
            scan_section(child_section, sid, heading_path)

    scan_section(body, "", [])
    # Refuse unsupported JATS nesting instead of silently omitting medical body text.
    # Paragraphs inside figures/tables are handled as media captions/footnotes.
    parent_map = {child: parent for parent in body.iter() for child in parent}
    missing_body_p = []
    for p in body.iter():
        if local_tag(p.tag) != "p" or id(p) in consumed_paragraphs:
            continue
        node = p
        within_media = False
        while node in parent_map:
            node = parent_map[node]
            if local_tag(node.tag) in ("fig", "table-wrap"):
                within_media = True
                break
        if not within_media and node_text(p):
            missing_body_p.append(node_text(p)[:70])
    missing_media = [m for m in body.iter() if local_tag(m.tag) in ("fig", "table-wrap") and id(m) not in consumed_media]
    if missing_body_p or missing_media:
        raise ValueError(f"JATS 存在未支持的嵌套结构：未解析正文段落={len(missing_body_p)}，媒体={len(missing_media)}；拒绝静默丢失文本。示例={missing_body_p[:2]}")
    if not abstract_sections and not any(s["blocks"] for s in sections) and not media["tables"]:
        raise ValueError("无法从 XML 提取摘要/正文/表格")
    return {"document_id": did, "title": title, "abstract_sections": abstract_sections,
        "body_sections": sections, "media": media}, warnings


def parse_pdf(blob: bytes, manifest: dict, did: str) -> tuple[dict, list[str]]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF 入库需要 PyMuPDF：python -m pip install PyMuPDF") from exc
    ins = manifest["inspection"]
    title = str(ins.get("title") or "").strip()
    if not title or title.startswith("（"):
        raise ValueError("PDF 标题未确认")
    sections, total_chars, short_pages = [], 0, 0
    warnings = ["PDF 文字提取不验证双栏阅读顺序、表格行列、公式或图像语义；仅隔离试验使用"]
    with fitz.open(stream=blob, filetype="pdf") as pdf:
        if pdf.needs_pass:
            raise ValueError("加密 PDF 暂不支持")
        if len(pdf) > 300:
            raise ValueError("PDF 超过300页；当前版本需要单独处理大文档")
        for i, page in enumerate(pdf):
            txt = page.get_text("text", sort=True) or ""
            total_chars += len(txt)
            if total_chars > 2_000_000:
                raise ValueError("PDF 正文超过200万字符，需要长文档处理通道")
            if len(txt.strip()) < 100:
                short_pages += 1
            if txt.strip():
                sid = f"s{i+1:04d}"
                sections.append({"section_id": sid, "parent_section_id": "",
                    "heading_path": [f"PDF Page {i+1}"],
                    "blocks": [{"block_id": f"{sid}-b0001", "block_type": "pdf_page_text", "text": txt}]})
        if len(pdf) > 0 and short_pages > max(1, len(pdf) // 4):
            warnings.append(f"PDF {short_pages}/{len(pdf)} 页文字过少，可能是扫描页/图片型内容")
        if total_chars < 500:
            raise ValueError("PDF 可提取文字不足500字符，不允许建立试验索引")
    return {"document_id": did, "title": title, "abstract_sections": [],
        "body_sections": sections, "media": {"tables": [], "figures": []}}, warnings


def normalize_doi(value: str) -> str:
    v = str(value or "").strip().lower()
    v = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", v)
    return re.sub(r"^doi:\s*", "", v)


def select_manifest(path: Path, root: Path) -> tuple[dict, Path, bytes]:
    staging = (root / "data/staging/medical_imports").resolve()
    mp = path.resolve()
    if staging not in mp.parents or mp.name != "manifest.json":
        raise ValueError("仅允许指定 data/staging/medical_imports/<hash>/manifest.json")
    rec = json.loads(mp.read_text(encoding="utf-8"))
    if rec.get("publication_status") != "NOT_INDEXED":
        raise ValueError("只能从未发布的暂存文件构建试验语料")
    digest = str(rec.get("file_sha256") or "").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or mp.parent.name != digest:
        raise ValueError("manifest SHA256 或目录不符合预期")
    filename = str(rec.get("source_file") or "")
    if filename not in ("source.pdf", "source.xml"):
        raise ValueError("暂存文件名不符合预期")
    src = mp.parent / filename
    if not src.is_file() or src.stat().st_size > STAGED_MAX or sha256_file(src) != digest:
        raise ValueError("暂存源文件丢失、过大或 SHA256 不匹配")
    return rec, src, src.read_bytes()


def copy_jsonl(source: Path, dest, seen_doc: set[str], seen_chunks: set[str], counters: Counter, name: str):
    if not source.is_file():
        raise FileNotFoundError(source)
    for row in read_jsonl(source):
        if name == "documents":
            did = str(row.get("document_id") or "")
            if not did or did in seen_doc:
                raise ValueError(f"旧语料论文 ID 缺失或重复: {did}")
            seen_doc.add(did)
        if name == "retrieval_chunks":
            cid = str(row.get("chunk_id") or "")
            if not cid or cid in seen_chunks:
                raise ValueError(f"旧语料 Chunk ID 缺失或重复: {cid}")
            seen_chunks.add(cid)
        dest.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        counters[name] += 1


def make_doc_record(enriched: dict, rec: dict, source: Path, xml: bool, warnings: list[str], name: str):
    ins = rec["inspection"]
    id_fields = {"doi": normalize_doi(ins.get("doi", "")), "pmid": str(ins.get("pmid") or ""),
        "pmcid": str(ins.get("pmcid") or "")}
    enriched["rights"] = {"audit_status": "NOT_REVIEWED", "source_license": rec.get("source_license", "NOT_REVIEWED")}
    enriched["provenance"] = {"source_file_sha256": rec["file_sha256"],
        "source_format": "PMC_JATS_XML" if xml else "PDF_TEXT_UNVERIFIED_LAYOUT",
        "staging_manifest": str(source.parent / "manifest.json"),
        "source_filename": rec.get("original_filename", "")}
    enriched.update(id_fields)
    enriched["source"] = "PMC_JATS_XML" if xml else "USER_PDF"
    enriched["publication_year"] = ins.get("year")
    enriched["authors"] = ins.get("authors", "")
    enriched["journal"] = ins.get("journal", "")
    enriched["version_identity"] = ins.get("version_identity", {})
    enriched["metadata_provenance"] = ins.get("metadata_provenance", {})
    return enriched


def run(args):
    root = args.root.resolve()
    base = root / "data/processed/knowledge" / args.base_version
    target = root / "data/processed/knowledge" / args.version
    if not re.fullmatch(r"[a-zA-Z0-9_\-]{3,64}", args.version) or args.version == args.base_version:
        raise ValueError("--version 必须是不同的合法新版本名")
    if target.exists():
        raise FileExistsError(f"目标版本目录已存在，绝不覆盖：{target}")
    if not args.allow_unreviewed_trial:
        raise ValueError("该工具只建立未审查试验语料；请明确传入 --allow-unreviewed-trial")
    if not args.manifest:
        raise ValueError("至少提供一个 --manifest")
    manifests = [select_manifest(Path(m) if Path(m).is_absolute() else root / m, root) for m in args.manifest]
    digests = [rec["file_sha256"] for rec, _, _ in manifests]
    if len(set(digests)) != len(digests):
        raise ValueError("重复指定同一暂存文件")
    seen_docs, seen_chunks, existing_dois, existing_pmcids, existing_pmids, existing_hashes = set(), set(), set(), set(), set(), set()
    # Precheck version identities, integrity, and duplicates BEFORE any output is committed.
    for doc in read_jsonl(base / "documents.jsonl"):
        did = str(doc.get("document_id") or "")
        if did in seen_docs:
            raise ValueError(f"旧语料重复文献: {did}")
        seen_docs.add(did)
        doi = normalize_doi(doc.get("doi") or "")
        pmcid = str(doc.get("pmcid") or (did if re.fullmatch(r"PMC\d+", did, re.I) else "")).upper()
        pmid = str(doc.get("pmid") or "").strip()
        if pmcid:
            existing_pmcids.add(pmcid)
        if pmid:
            existing_pmids.add(pmid)
        prov = doc.get("provenance") or {}
        old_hash = str(prov.get("source_file_sha256") or prov.get("source_xml_sha256") or "").lower()
        if old_hash:
            existing_hashes.add(old_hash)
        if doi:
            existing_dois.add(doi)
    new_docs = []
    for rec, src, blob in manifests:
        ins = rec.get("inspection")
        if not isinstance(ins, dict):
            raise ValueError(f"暂存 manifest 缺少 inspection: {src}")
        if not bool(ins.get("metadata_confirmed")) and not args.allow_unconfirmed_metadata_trial:
            raise ValueError(f"{src.name}: 尚未人工核对标题；必须确认后再试验，或单独指定 --allow-unconfirmed-metadata-trial")
        if ins.get("duplicates") and not args.allow_flagged_duplicates_trial:
            raise ValueError("暂存记录已有重复警示，需要先核对，不能直接准备试验语料: " + "; ".join(ins["duplicates"]))
        if rec["file_sha256"] in existing_hashes:
            raise ValueError("上传文件内容已存在于旧语料（SHA256 重复）")
        existing_hashes.add(rec["file_sha256"])
        did = f"IMPORT_{rec['file_sha256'][:24].upper()}"
        # Keep a stable internal identifier per exact uploaded file version.
        if did in seen_docs:
            raise ValueError(f"文献 ID 已存在: {did}")
        seen_docs.add(did)
        xml = src.suffix.lower() == ".xml"
        enriched, warnings = (parse_jats(blob, rec, did) if xml else parse_pdf(blob, rec, did))
        pmcid = str(ins.get("pmcid") or "").strip().upper()
        pmid = str(ins.get("pmid") or "").strip()
        if pmcid and pmcid in existing_pmcids:
            raise ValueError(f"PMCID 已存在于当前试验快照：{pmcid}")
        if pmid and pmid in existing_pmids:
            raise ValueError(f"PMID 已存在于当前试验快照：{pmid}")
        if pmcid:
            existing_pmcids.add(pmcid)
        if pmid:
            existing_pmids.add(pmid)
        # XML may complete DOI from article-meta; re-check against old+new versions.
        doi = normalize_doi(ins.get("doi") or "")
        if doi and doi in existing_dois:
            raise ValueError(f"新旧文献主 DOI 重复：{doi}")
        if doi:
            existing_dois.add(doi)
        make_doc_record(enriched, rec, src, xml, warnings, src.name)
        result = build_one(enriched, args.target_tokens, args.max_tokens)
        docrow, parents, chunks, edges, registry, summary = result
        docrow.update({"source": enriched["source"], "pmid": enriched["pmid"], "doi": enriched["doi"],
            "pmcid": enriched["pmcid"], "publication_year": enriched["publication_year"],
            "authors": enriched["authors"], "journal": enriched["journal"],
            "version_identity": enriched["version_identity"],
            "metadata_provenance": enriched["metadata_provenance"],
            "original_enriched_source": str(src), "full_source_available": True,
            "scope": "ISOLATED_TRIAL_UNREVIEWED", "parser_warnings": warnings})
        for chunk in chunks:
            chunk["rights_status"] = "NOT_REVIEWED_TRIAL_ONLY"
            chunk["parser_quality"] = "JATS_TEXT_TABLE_STRUCTURE_UNREVIEWED" if xml else "PDF_LAYOUT_UNVERIFIED"
        new_docs.append({"records": (docrow, parents, chunks, edges, registry),
                         "summary": summary, "warnings": warnings, "original_filename": rec.get("original_filename"),
                         "document_id": did, "sha256": rec["file_sha256"]})
    tmp = target.with_name("." + target.name + ".tmp_" + uuid.uuid4().hex[:8])
    tmp.mkdir(parents=True, exist_ok=False)
    try:
        counters = Counter()
        # Write all records as one consistent snapshot, never append to v3_1_all.
        with __import__("contextlib").ExitStack() as stack:
            streams = {name: stack.enter_context((tmp / f"{name}.jsonl").open("w", encoding="utf-8", newline="\n")) for name in NAMES}
            old_docs, old_chunks = set(), set()
            for name in NAMES:
                copy_jsonl(base / f"{name}.jsonl", streams[name], old_docs, old_chunks, counters, name)
            if args.expected_base_docs and counters["documents"] != args.expected_base_docs:
                raise ValueError(f"旧文献数量不符：{counters['documents']} != {args.expected_base_docs}")
            if args.expected_base_chunks and counters["retrieval_chunks"] != args.expected_base_chunks:
                raise ValueError(f"旧 Chunk 数不符：{counters['retrieval_chunks']} != {args.expected_base_chunks}")
            for item in new_docs:
                d, parents, children, edges, medias = item["records"]
                if d["document_id"] in old_docs:
                    raise ValueError("新文件文献 ID 与旧库冲突")
                old_docs.add(d["document_id"])
                for name, rows in zip(NAMES, ([d], parents, children, edges, medias)):
                    for row in rows:
                        if name == "retrieval_chunks":
                            cid = row["chunk_id"]
                            if cid in old_chunks:
                                raise ValueError(f"Chunk ID 冲突: {cid}")
                            old_chunks.add(cid)
                        streams[name].write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                        counters[name] += 1
        report = {"status": "ISOLATED_TRIAL_UNREVIEWED_NOT_PUBLISHED",
                  "version": args.version, "base_version": args.base_version,
                  "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "counts": dict(counters), "base_documents": counters["documents"] - len(new_docs),
                  "new_document_count": len(new_docs),
                  "new_documents": [{k:v for k,v in doc.items() if k != "records"} for doc in new_docs],
                  "warning": "未验证 PDF 阅读顺序、复杂表格、版权许可、证据准确性；未经评审不得作为临床证据。"}
        (tmp / "ingestion_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        # Hash all 5 files to support version identity.
        report["file_sha256"] = {name: sha256_file(tmp / f"{name}.jsonl") for name in NAMES}
        (tmp / "ingestion_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    print(f"隔离语料准备成功：{target}")
    print(f"旧论文={report['base_documents']}；新增={len(new_docs)}；总论文={counters['documents']}；总Chunk={counters['retrieval_chunks']}")
    for item in new_docs:
        print(f"  {item['document_id']} | {item['original_filename']} | {len(item['warnings'])} 条解析警告")
    print("状态：未审查试验版，尚未建立 BM25 / Dense 索引，旧库未修改。")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--base-version", default="v3_1_all")
    ap.add_argument("--version", default="v3_2_trial")
    ap.add_argument("--manifest", action="append", required=True, help="暂存区 manifest.json；多篇可重复传参")
    ap.add_argument("--allow-unreviewed-trial", action="store_true", help="允许建立隔离试验语料，不允许直接作为临床证据")
    ap.add_argument("--allow-unconfirmed-metadata-trial", action="store_true")
    ap.add_argument("--allow-flagged-duplicates-trial", action="store_true", help="仅测试，不推荐：可能产生重复论文")
    ap.add_argument("--expected-base-docs", type=int, default=921)
    ap.add_argument("--expected-base-chunks", type=int, default=42526)
    ap.add_argument("--target-tokens", type=int, default=380)
    ap.add_argument("--max-tokens", type=int, default=600)
    args = ap.parse_args()
    if args.target_tokens < 80 or args.max_tokens < args.target_tokens:
        ap.error("目标 Token 参数不正确")
    run(args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, ET.ParseError) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
