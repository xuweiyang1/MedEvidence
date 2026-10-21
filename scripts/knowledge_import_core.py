#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V1a.3: literature inspection and isolated staging (NO INDEX WRITES).

This module is independent of Qdrant/BM25 and never changes the existing V3.1 corpus.
Not a clinical evidence validator. Staged data MUST pass human and format checks
before a separate, future publication pipeline indexes it.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from arxiv_lookup import detect_arxiv_id, filename_title_candidate

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORPUS_ROOT = PROJECT_ROOT / "data/processed/knowledge/v3_1_all"
DOCUMENTS_JSONL = CORPUS_ROOT / "documents.jsonl"
CHUNKS_JSONL = CORPUS_ROOT / "retrieval_chunks.jsonl"
STAGING_ROOT = PROJECT_ROOT / "data/staging/medical_imports"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
PDF_PREVIEW_PAGES = 30
TEXT_PREVIEW_CHARS = 3500


def local_tag(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def node_text(node) -> str:
    return re.sub(r"\s+", " ", " ".join(node.itertext())).strip() if node is not None else ""


def existing_corpus_info(doc_path: Path = DOCUMENTS_JSONL, chunk_path: Path = CHUNKS_JSONL) -> dict:
    """Read existing V3.1 manifest; no writing or indexing."""
    result = {"documents": 0, "chunks": 0, "pmcids": set(), "dois": set(), "pmids": set(), "warnings": []}
    if not doc_path.is_file():
        result["warnings"].append(f"论文元数据文件不存在：{doc_path}")
    else:
        with doc_path.open("r", encoding="utf-8-sig") as f:
            for line_number, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    result["warnings"].append(f"元数据第 {line_number} 行 JSON 解析失败")
                    continue
                result["documents"] += 1
                raw_id = record.get("document_id") or record.get("pmcid") or record.get("paper_id") or ""
                norm_id = normalize_pmcid(str(raw_id))
                if norm_id:
                    result["pmcids"].add(norm_id)
                doi = normalize_doi(str(record.get("doi") or record.get("DOI") or ""))
                if doi:
                    result["dois"].add(doi)
                pmid = normalize_pmid(str(record.get("pmid") or record.get("PMID") or ""))
                if pmid:
                    result["pmids"].add(pmid)
    if not chunk_path.is_file():
        result["warnings"].append(f"Chunk 文件不存在：{chunk_path}")
    else:
        with chunk_path.open("r", encoding="utf-8-sig") as f:
            result["chunks"] = sum(bool(line.strip()) for line in f)
    return result


def normalize_pmcid(s: str) -> str:
    m = re.fullmatch(r"(?:PMC)?\s*(\d+)", s.strip(), re.I)
    return "PMC" + m.group(1) if m else ""


def normalize_doi(s: str) -> str:
    val = s.strip().lower()
    val = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", val)
    val = re.sub(r"^doi:\s*", "", val)
    return val if val.startswith("10.") and "/" in val else ""


def normalize_pmid(s: str) -> str:
    text = re.sub(r"^PMID\s*:\s*", "", str(s or "").strip(), flags=re.I)
    return text if re.fullmatch(r"[1-9]\d{0,9}", text) else ""


def inspect_pmc_xml(content: bytes) -> dict:
    """Extract preview-only JATS metadata/content; not a complete JATS parser."""
    upper = content[:100000].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("为安全起见，暂不接受包含 DTD/ENTITY 的 XML；请使用标准 PMC JATS XML。")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"XML 不能解析：{exc}") from exc
    if local_tag(root.tag) != "article":
        raise ValueError("只支持根节点为 <article> 的 PMC/JATS XML；其他 XML 将在下一版增加适配器。")
    title = ""
    abstract_parts = []
    pmcid, doi, pmid = "", "", ""
    for element in root.iter():
        tag = local_tag(element.tag)
        if tag == "article-id":
            typ = element.attrib.get("pub-id-type", "").lower()
            if typ == "pmc":
                pmcid = normalize_pmcid(node_text(element))
            elif typ == "doi":
                doi = normalize_doi(node_text(element))
            elif typ == "pmid":
                pmid = normalize_pmid(node_text(element))
        if tag == "article-title" and not title:
            title = node_text(element)
        if tag == "abstract":
            abstract_parts.append(node_text(element))
    body = next((e for e in root.iter() if local_tag(e.tag) == "body"), None)
    paragraphs = [node_text(e) for e in body.iter() if local_tag(e.tag) == "p"] if body is not None else []
    paragraphs = [p for p in paragraphs if p]
    tables = [e for e in root.iter() if local_tag(e.tag) == "table-wrap"]
    figures = [e for e in root.iter() if local_tag(e.tag) == "fig"]
    warnings = []
    if not pmcid:
        warnings.append("未提取到 PMCID，需人工核对身份，避免重复收录")
    if not body or not paragraphs:
        warnings.append("缺少可解析的正文段落，可能只有元数据或摘要")
    if tables:
        warnings.append(f"检测到 {len(tables)} 个表格；V1a 仅预览，不保证表格行列结构已正确解析")
    if figures:
        warnings.append(f"检测到 {len(figures)} 个图片节点；V1a 不提取图像文件")
    excerpt = "\n\n".join((abstract_parts + paragraphs)[:30])[:TEXT_PREVIEW_CHARS]
    return {
        "format": "PMC_JATS_XML", "title": title or "（未解析到标题）",
        "pmcid": pmcid, "doi": doi, "pmid": pmid, "page_count": None,
        "paragraph_count": len(paragraphs), "table_count": len(tables),
        "figure_count": len(figures), "preview": excerpt,
        "warnings": warnings,
        "parser_status": "PREVIEW_ONLY_NOT_INDEXED",
    }


def _pdf_title_candidate(first_page) -> str:
    """Heuristic title suggestion based on large text near the first page top.

    Suggestions are NOT verified bibliographic metadata. User must confirm.
    """
    try:
        page_info = first_page.get_text("dict", sort=True)
        height = float(first_page.rect.height)
    except Exception:
        return ""
    lines = []
    for block in page_info.get("blocks", []):
        if "lines" not in block:
            continue
        for line in block["lines"]:
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            txt = re.sub(r"\s+", " ", "".join(s["text"] for s in spans)).strip()
            bbox = line.get("bbox", (0, 0, 0, height))
            size = max(float(s.get("size", 0)) for s in spans)
            if bbox[1] > height * 0.60:
                continue
            if not (8 <= len(txt) <= 240) or size < 8:
                continue
            if re.match(r"^(arxiv|http|www\.|doi\s*:|abstract\b|keywords\b|preprint\b|copyright\b)", txt, re.I):
                continue
            if "@" in txt or len(re.findall(r"\b[A-Za-z]+\b", txt)) < 2:
                continue
            # Affiliation blocks can be large and close to the title. They are
            # NOT trustworthy title candidates, even when font-size is large.
            low = txt.casefold()
            if txt.count(",") >= 2 or re.search(r"\b(university|institute|department|school of|affiliation|hospital|@)\b", low):
                continue
            if re.search(r"\b\d+(?:department|university|school|institute)\b", low):
                continue
            lines.append((float(bbox[1]), float(bbox[3]), size, txt))
    if not lines:
        return ""
    # Prefer the largest-font text; combine neighboring title lines.
    max_size = max(line[2] for line in lines)
    candidates = [line for line in lines if line[2] >= max(10.5, max_size * 0.78)]
    candidates.sort(key=lambda line: line[0])
    groups: list[list[tuple]] = []
    for line in candidates:
        if groups and line[0] - groups[-1][-1][1] < max(16.0, max_size * 1.4):
            groups[-1].append(line)
        else:
            groups.append([line])
    if not groups:
        return ""
    def rank(group):
        title = " ".join(item[3] for item in group)
        # Large font, substantial title text, earlier position preferred.
        return (max(item[2] for item in group), min(len(title), 140) / 140, -group[0][0])
    best = max(groups, key=rank)
    text = re.sub(r"\s+", " ", " ".join(item[3] for item in best)).strip()
    return text[:350] if len(text) >= 15 else ""


def inspect_pdf(content: bytes) -> dict:
    """Read selected PDF pages for inspection only. No OCR or index writes."""
    warnings = []
    pages_count, pages, engine, title_candidate = 0, [], "", ""
    try:
        import fitz
        engine = "PyMuPDF"
        with fitz.open(stream=content, filetype="pdf") as doc:
            if doc.needs_pass:
                raise ValueError("PDF 已加密，需要先解锁后上传")
            pages_count = len(doc)
            if pages_count:
                title_candidate = _pdf_title_candidate(doc[0])
            for idx in range(min(pages_count, PDF_PREVIEW_PAGES)):
                extracted = doc[idx].get_text("text", sort=True) or ""
                pages.append({"page": idx + 1, "chars": len(extracted),
                              "text": extracted[:12000]})
    except ImportError:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF 预览需要 PyMuPDF：python -m pip install PyMuPDF") from exc
        from io import BytesIO
        engine = "pypdf"
        try:
            reader = PdfReader(BytesIO(content))
            if reader.is_encrypted:
                raise ValueError("PDF 已加密，需要先解锁后上传")
            pages_count = len(reader.pages)
            for idx in range(min(pages_count, PDF_PREVIEW_PAGES)):
                extracted = reader.pages[idx].extract_text() or ""
                pages.append({"page": idx + 1, "chars": len(extracted),
                              "text": extracted[:12000]})
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"无法解析 PDF：{exc}") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"无法解析 PDF：{exc}") from exc

    scanned = len(pages)
    scanned_chars = sum(p["chars"] for p in pages)
    nonempty_pages = sum(p["chars"] >= 100 for p in pages)
    if pages_count > scanned:
        warnings.append(f"共 {pages_count} 页，只预览前 {scanned} 页，尚未检查剩余页；不可视为全文已解析")
    if scanned_chars < 200 or (scanned and nonempty_pages < max(1, scanned // 4)):
        warnings.append("可提取文本偏少，可能包含扫描页或图片型内容，正式入库前需要 OCR/版面检查")
    if not title_candidate:
        warnings.append("无法自动推测 PDF 标题，须人工录入并核对原文")
    else:
        warnings.append("标题根据首页字号和位置推测；必须与原文及出版记录核对")
    warnings.append("PDF 版面、公式、表格和双栏顺序未校验，不可直接用作医学结论")
    first_pages = "\n".join(p["text"] for p in pages[:2])
    doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+", first_pages[:18000], re.I)
    pmcid_match = re.search(r"\bPMC\d+\b", first_pages[:18000], re.I)
    pmid_match = re.search(r"\bPMID\s*:\s*([1-9]\d{0,9})\b", first_pages[:18000], re.I)
    if doi_match:
        warnings.append("DOI 为首页文字匹配候选，可能引用了其他论文；需人工核对")
    return {
        "format": "PDF_TEXT", "title": title_candidate or "（PDF 标题需核对）",
        "title_source": "PDF_FONT_HEURISTIC" if title_candidate else "UNKNOWN",
        "title_needs_review": True,
        "pmcid": normalize_pmcid(pmcid_match.group()) if pmcid_match else "",
        "pmid": normalize_pmid(pmid_match.group(1)) if pmid_match else "",
        "doi": normalize_doi(doi_match.group().rstrip(".,;")) if doi_match else "",
        "page_count": pages_count,
        "pages_scanned": scanned,
        "pages_with_text": nonempty_pages,
        "scanned_text_chars": scanned_chars,
        "page_previews": pages,
        "paragraph_count": None, "table_count": None, "figure_count": None,
        "preview": ("\n".join(p["text"] for p in pages))[:TEXT_PREVIEW_CHARS],
        "warnings": warnings, "parser_engine": engine,
        "parser_status": "PREVIEW_ONLY_NOT_INDEXED",
    }


def validate_version_identifiers(version_type: str, *, doi: str = "", pmid: str = "", pmcid: str = "", related_published: dict | None = None) -> None:
    """Prevent journal-record identifiers from silently becoming preprint IDs."""
    if version_type not in ("UNKNOWN", "PREPRINT", "PUBLISHED"):
        raise ValueError("未识别的文献版本类型")
    if version_type != "PREPRINT":
        return
    # This preview importer does not independently verify PubMed inclusion
    # of a preprint; avoid falsely attributing published IDs to it.
    if pmid or pmcid:
        raise ValueError("预印本不能直接使用期刊版 PMID / PMCID；请先留空，并以版本关联保存发表记录")
    record = ((related_published or {}).get("candidate") or {})
    if doi and record.get("doi") and normalize_doi(doi) == normalize_doi(record["doi"]):
        raise ValueError("预印本 DOI 与关联期刊版 DOI 相同；不得写入预印本主记录")


def check_duplicates(preview: dict, existing: dict, staged_records: list[dict] | None = None) -> list[str]:
    """Re-check duplicates after possible user metadata corrections."""
    reasons = []
    pmcid = normalize_pmcid(str(preview.get("pmcid") or ""))
    doi = normalize_doi(str(preview.get("doi") or ""))
    pmid = normalize_pmid(str(preview.get("pmid") or ""))
    digest = preview.get("sha256", "")
    if pmcid and pmcid in existing.get("pmcids", set()):
        reasons.append("PMCID 已存在于 V3.1")
    if doi and doi in existing.get("dois", set()):
        reasons.append("DOI 已存在于 V3.1")
    if pmid and pmid in existing.get("pmids", set()):
        reasons.append("PMID 已存在于 V3.1")
    for record in staged_records or []:
        meta = record.get("inspection", {})
        if digest and digest == record.get("file_sha256"):
            reasons.append("同一文件内容已在待入库区")
        if pmcid and pmcid == normalize_pmcid(str(meta.get("pmcid") or "")):
            reasons.append("PMCID 已存在于待入库区")
        if doi and doi == normalize_doi(str(meta.get("doi") or "")):
            reasons.append("DOI 已存在于待入库区")
        if pmid and pmid == normalize_pmid(str(meta.get("pmid") or "")):
            reasons.append("PMID 已存在于待入库区")
    # An arXiv preprint is not the same file version as its eventual journal
    # version. Exact arXiv-ID duplicates must still be flagged for review.
    arxiv_id = (preview.get("version_identity") or {}).get("arxiv_id") or preview.get("arxiv_id") or ""
    for record in staged_records or []:
        saved_inspection = record.get("inspection", {})
        saved_id = ((saved_inspection.get("version_identity") or {}).get("arxiv_id")
                    or saved_inspection.get("arxiv_id") or "")
        if arxiv_id and saved_id and arxiv_id.lower() == saved_id.lower():
            reasons.append("同一 arXiv 编号已在待入库区；需核对是否为不同版本")
    return list(dict.fromkeys(reasons))


def analyze_upload(file_name: str, content: bytes, existing: dict, staged_records: list[dict] | None = None) -> dict:
    """Validate file, inspect, and flag possible duplicate identity without writing."""
    ext = Path(file_name).suffix.lower()
    if ext not in (".xml", ".pdf"):
        raise ValueError("当前仅接受 .xml 或 .pdf 文件")
    if not content:
        raise ValueError("上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("单文件暂限 25 MB；请稍后使用大文件处理通道")
    result = inspect_pmc_xml(content) if ext == ".xml" else inspect_pdf(content)
    if ext == ".pdf":
        # Filename hints are especially useful for arXiv PDFs with misordered
        # author affiliations. All suggestions must still be checked by a human.
        from_name = filename_title_candidate(Path(file_name).name)
        page_text = "\n".join(p.get("text", "") for p in result.get("page_previews", [])[:2])
        arxiv = detect_arxiv_id(Path(file_name).name, page_text)
        result["arxiv_id"] = arxiv["arxiv_id"]
        result["arxiv_version"] = arxiv["arxiv_version"]
        result["arxiv_detected_from"] = arxiv["source"]
        if from_name:
            if result.get("title") and not result["title"].startswith("（"):
                result["other_title_candidate"] = result["title"]
            result["title"] = from_name
            result["title_source"] = "PDF_FILENAME_ARXIV_CANDIDATE"
            result["title_needs_review"] = True
            result["warnings"].append("标题来自包含 arXiv 编号的文件名，仅为建议；需核对 PDF 首页和 arXiv 记录")
        result["warnings"].extend(arxiv.get("warnings", []))
    else:
        result.update({"arxiv_id": "", "arxiv_version": "", "arxiv_detected_from": "NONE"})
    result["filename"] = Path(file_name).name
    result["file_size"] = len(content)
    result["sha256"] = hashlib.sha256(content).hexdigest()
    result["duplicates"] = check_duplicates(result, existing, staged_records)
    return result


def _safe_filename(filename: str) -> str:
    stem = Path(filename).name
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", stem)
    return cleaned[:100] or "paper"


def stage_upload(file_name: str, content: bytes, preview: dict, staging_root: Path = STAGING_ROOT) -> tuple[Path, bool]:
    """Save to isolated staging; immutable content-addressed folder, never index.

    Return (manifest path, newly_created). The same bytes are idempotent.
    """
    digest = hashlib.sha256(content).hexdigest()
    if preview.get("sha256") != digest:
        raise ValueError("文件内容与预览结果不一致，请重新点击解析预览")
    name = _safe_filename(file_name)
    ext = Path(name).suffix.lower()
    if ext not in (".xml", ".pdf"):
        raise ValueError("不支持的文件类型")
    folder = staging_root / digest  # Full sha avoids prefix collision.
    manifest = folder / "manifest.json"
    if manifest.exists():
        return manifest, False
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / ("source" + ext)
    # x mode prevents silent replacement, even when concurrent uploads race.
    try:
        with original.open("xb") as f:
            f.write(content)
    except FileExistsError:
        if hashlib.sha256(original.read_bytes()).hexdigest() != digest:
            raise RuntimeError("暂存目录中已有不同文件，拒绝覆盖")
    data = {
        "file_sha256": digest,
        "original_filename": Path(file_name).name,
        "source_file": original.name,
        "staged_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "DUPLICATE_REVIEW_REQUIRED" if preview.get("duplicates") else "STAGED_UNREVIEWED",
        "publication_status": "NOT_INDEXED",
        "source_license": "NOT_REVIEWED",
        "inspection": {k: v for k, v in preview.items() if k not in ("preview", "page_previews")},
    }
    # No app text can cause V3.1/Qdrant writes: staging is separate by design.
    try:
        with manifest.open("x", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except FileExistsError:
        return manifest, False
    return manifest, True


def list_staged(staging_root: Path = STAGING_ROOT) -> list[dict]:
    result = []
    if not staging_root.exists():
        return result
    for manifest in sorted(staging_root.glob("*/manifest.json"), reverse=True):
        try:
            with manifest.open("r", encoding="utf-8") as f:
                rec = json.load(f)
            rec["manifest_path"] = str(manifest)
            result.append(rec)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return result
