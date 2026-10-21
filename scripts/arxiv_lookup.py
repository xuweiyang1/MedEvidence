#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arXiv ID detection and *opt-in* lookup for MedEvidence V1a.3.

Only a user click initiates a request; no upload bytes, hash or corpus are sent.
The returned arXiv metadata identifies a PREPRINT, not an associated journal PDF.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "ax": "http://arxiv.org/schemas/atom"}
# Deliberately require either arXiv label or an arxiv-containing filename to
# prevent arbitrary DOI numbers or decimals being mistaken for arXiv IDs.
ARXIV_PATTERN = re.compile(r"(?i)arxiv\s*[:_\-\s]*((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?)")
BARE_ID = re.compile(r"(?i)^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?$")


def clean_arxiv_id(text: str) -> tuple[str, str]:
    text = str(text or "").strip()
    match = ARXIV_PATTERN.search(text)
    if match:
        text = match.group(1)
    if not BARE_ID.fullmatch(text):
        return "", ""
    m = re.fullmatch(r"(.+?)(v\d+)?", text, re.I)
    return m.group(1), (m.group(2) or "")


def detect_arxiv_id(filename: str, page_text: str = "") -> dict:
    """Return candidate with source, never assert PDF-version identity."""
    file_hit = ARXIV_PATTERN.search(filename or "")
    text_hit = ARXIV_PATTERN.search((page_text or "")[:16000])
    file_id, file_version = clean_arxiv_id(file_hit.group(1) if file_hit else "")
    text_id, text_version = clean_arxiv_id(text_hit.group(1) if text_hit else "")
    warnings = []
    if file_id and text_id and file_id.casefold() != text_id.casefold():
        warnings.append("文件名和 PDF 内文出现不同的 arXiv 编号，必须人工核对；不会自动合并")
        return {"arxiv_id": "", "arxiv_version": "", "source": "CONFLICT", "warnings": warnings}
    arxiv_id = text_id or file_id
    return {
        "arxiv_id": arxiv_id,
        "arxiv_version": text_version or file_version,
        "source": "PDF_TEXT" if text_id else ("FILENAME" if file_id else "NONE"),
        "warnings": warnings,
    }


def filename_title_candidate(filename: str) -> str:
    """Only trust arXiv-tagged, human-readable filenames as provisional titles."""
    if not ARXIV_PATTERN.search(filename or ""):
        return ""
    stem = re.sub(r"(?i)\.pdf$", "", filename or "")
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)
    stem = ARXIV_PATTERN.sub(" ", stem)
    stem = re.sub(r"[_\s-]+", " ", stem).strip(" -_.")
    if not 18 <= len(stem) <= 160 or not 3 <= len(stem.split()) <= 22:
        return ""
    if any(x in stem.casefold() for x in ("university", "department", "toronto,", "@")):
        return ""
    return stem


def lookup_arxiv_by_id(arxiv_id: str, *, opener=None) -> dict:
    """Query the official arXiv Atom API by ID; raise on unavailable service.

    For preprints, journal DOI (if present in the Atom response) is returned as
    a hint for a RELATED journal version, never as preprint's own DOI.
    """
    base_id, _ = clean_arxiv_id(arxiv_id)
    if not base_id:
        raise ValueError("arXiv 编号无效，例如 2304.12306")
    opener = opener or urlopen
    url = ARXIV_API + "?" + urlencode({"id_list": base_id, "max_results": 1})
    payload = None
    for attempt in range(2):
        try:
            req = Request(url, headers={"User-Agent": "MedEvidence-LiteratureImport/1.3", "Accept": "application/atom+xml"})
            with opener(req, timeout=18) as response:
                payload = response.read(1_000_001)
            if len(payload) > 1_000_000:
                raise ValueError("arXiv 响应超过 1 MB")
            break
        except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RuntimeError(f"arXiv API 请求失败：{exc}") from exc
    if payload is None or b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("arXiv 响应为空或含不安全 XML 定义")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError("arXiv 响应不是可解析的 Atom XML") from exc
    entry = root.find("atom:entry", ARXIV_NS)
    if entry is None:
        raise LookupError("arXiv 未返回该编号的论文记录")
    url_id = (entry.findtext("atom:id", default="", namespaces=ARXIV_NS) or "").strip()
    entry_id_str = urlsplit(url_id).path.rsplit("/", 1)[-1]
    remote_id, remote_version = clean_arxiv_id(entry_id_str)
    if remote_id.casefold() != base_id.casefold():
        raise LookupError("arXiv 返回的编号与查询编号不一致，拒绝采用")
    title = re.sub(r"\s+", " ", entry.findtext("atom:title", default="", namespaces=ARXIV_NS) or "").strip()
    if not title:
        raise LookupError("arXiv 记录没有标题")
    authors = []
    for author in entry.findall("atom:author", ARXIV_NS):
        author_name = author.findtext("atom:name", default="", namespaces=ARXIV_NS)
        if author_name:
            authors.append(author_name.strip())
    published = entry.findtext("atom:published", default="", namespaces=ARXIV_NS) or ""
    updated = entry.findtext("atom:updated", default="", namespaces=ARXIV_NS) or ""
    journal_ref = entry.findtext("ax:journal_ref", default="", namespaces=ARXIV_NS) or ""
    journal_doi = entry.findtext("ax:doi", default="", namespaces=ARXIV_NS) or ""
    return {
        "source": "arXiv API", "arxiv_id": remote_id, "arxiv_version": remote_version,
        "title": title, "authors": ", ".join(authors), "year": published[:4],
        "journal": "arXiv preprint", "published_utc": published, "updated_utc": updated,
        "record_url": f"https://arxiv.org/abs/{remote_id}",
        "published_journal_hint": re.sub(r"\s+", " ", journal_ref).strip(),
        "published_doi_hint": journal_doi.strip(),
        "record_type": "PREPRINT",
    }
