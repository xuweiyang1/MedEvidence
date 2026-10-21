#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V1 — 为 PMC 语料批量拉取 PubMed 元数据（PMID / 文献类型 / MeSH 词）

用途：对 data/raw/pmc 里已下载的每篇文章（其 JSON 元数据含 pmid），
批量 efetch PubMed 记录，保存 XML，并解析出精筛与实体标准化所需字段：
  pmid, pmcid, doi, journal, year, pubtypes, mesh, title

用法：
  py -X utf8 fetch_pubmed_meta.py

输出：
  data/raw/pubmed/xml/pubmed_batch_*.xml   （原始 XML，官方"筛选结果"存档）
  data/raw/pubmed/pubmed_meta.csv          （解析后的表）
  licenses/registry.csv 追加 PubMed 来源登记
"""
from __future__ import annotations

import csv
import hashlib
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
PMC_DIR = ROOT / "data" / "raw" / "pmc" / "articles"
OUT_DIR = ROOT / "data" / "raw" / "pubmed"
XML_DIR = OUT_DIR / "xml"
CSV_PATH = OUT_DIR / "pubmed_meta.csv"
REGISTRY = ROOT / "licenses" / "registry.csv"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/eutils/efetch.fcgi"
UA = "MedEvidence/0.1 (research project; PubMed efetch)"
FIELDS = ["pmid", "pmcid", "doi", "journal", "year", "pubtypes", "mesh", "title"]
BATCH = 100


def build_ssl_context() -> ssl.SSLContext:
    try:
        import truststore  # type: ignore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CONTEXT = build_ssl_context()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url: str, timeout: int = 120, retries: int = 3) -> bytes:
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET 失败：{url}（{last_exc!r}）")


def collect_mapping() -> list[dict]:
    mapping = []
    for jf in sorted(PMC_DIR.glob("*/*.json")):
        try:
            meta = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        pmid = str(meta.get("pmid") or "").strip()
        mapping.append({
            "pmcid": str(meta.get("pmcid") or "").strip(),
            "pmid": pmid,
            "doi": str(meta.get("doi") or "").strip(),
        })
    return mapping


def parse_batch(raw: bytes, pmcid_by_pmid: dict) -> list[dict]:
    rows = []
    root = ET.fromstring(raw)
    for art in root.iter("PubmedArticle"):
        pmid_el = art.find(".//MedlineCitation/PMID")
        pmid = pmid_el.text.strip() if pmid_el is not None and pmid_el.text else ""

        title = ""
        t = art.find(".//Article/ArticleTitle")
        if t is not None:
            title = " ".join("".join(t.itertext()).split())

        journal = ""
        j = art.find(".//Article/Journal/ISOAbbreviation")
        if j is None:
            j = art.find(".//Article/Journal/Title")
        if j is not None and j.text:
            journal = j.text.strip()

        year = ""
        y = art.find(".//Article/Journal/JournalIssue/PubDate/Year")
        if y is not None and y.text:
            year = y.text.strip()
        else:
            md = art.find(".//Article/Journal/JournalIssue/PubDate/MedlineDate")
            if md is not None and md.text:
                year = md.text.strip()[:4]

        doi = ""
        for aid in art.iter("ArticleId"):
            if aid.get("IdType") == "doi" and aid.text:
                doi = aid.text.strip()
                break

        pubtypes = [p.text.strip() for p in art.iter("PublicationType") if p.text]
        mesh = [d.text.strip() for d in art.iter("DescriptorName") if d.text]

        rows.append({
            "pmid": pmid,
            "pmcid": pmcid_by_pmid.get(pmid, ""),
            "doi": doi,
            "journal": journal,
            "year": year,
            "pubtypes": "; ".join(pubtypes),
            "mesh": "; ".join(mesh),
            "title": title,
        })
    return rows


def upsert_registry(row: dict) -> None:
    fields = ["source", "item", "file", "url", "sha256", "bytes",
              "license", "page", "retrieved_at"]
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if REGISTRY.exists():
        with REGISTRY.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    rows = [r for r in rows
            if not (r.get("source") == row["source"] and r.get("item") == row["item"])]
    rows.append(row)
    rows.sort(key=lambda r: (r.get("source", ""), r.get("item", "")))
    with REGISTRY.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fields})


def main() -> int:
    mapping = collect_mapping()
    pmcid_by_pmid = {m["pmid"]: m["pmcid"] for m in mapping if m["pmid"]}
    pmids = sorted(set(pmcid_by_pmid))
    no_pmid = [m for m in mapping if not m["pmid"]]
    print(f"PMC 文章 {len(mapping)} 篇；带 PMID 的 {len(pmids)} 篇；缺 PMID 的 {len(no_pmid)} 篇")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    XML_DIR.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict] = []
    total_bytes = 0
    for i in range(0, len(pmids), BATCH):
        chunk = pmids[i:i + BATCH]
        batch_no = i // BATCH + 1
        params = {
            "db": "pubmed", "id": ",".join(chunk), "retmode": "xml",
            "tool": "medevidence", "email": "medevidence-demo@example.com",
        }
        url = EUTILS + "?" + urllib.parse.urlencode(params)
        raw = http_get(url)
        total_bytes += len(raw)
        (XML_DIR / f"pubmed_batch_{batch_no:03d}.xml").write_bytes(raw)
        rows = parse_batch(raw, pmcid_by_pmid)
        all_rows.extend(rows)
        print(f"  批次 {batch_no}: 请求 {len(chunk)} 篇，解析 {len(rows)} 条")
        time.sleep(0.6)

    all_rows.sort(key=lambda r: r.get("pmcid", ""))
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in all_rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})

    print(f"\nXML 合计 {total_bytes / 1e6:.1f} MB；CSV: {CSV_PATH}")

    counter = Counter()
    for row in all_rows:
        for pt in [p.strip() for p in row["pubtypes"].split(";") if p.strip()]:
            counter[pt] += 1
    print("\n文献类型分布（前 15）：")
    for name, n in counter.most_common(15):
        print(f"  {name}: {n}")

    digest = hashlib.sha256(CSV_PATH.read_bytes()).hexdigest()
    upsert_registry({
        "source": "PubMed", "item": f"pubmed_meta_{datetime.now().strftime('%Y-%m-%d')}",
        "file": str(CSV_PATH.relative_to(ROOT)).replace("\\", "/"),
        "url": EUTILS, "sha256": digest, "bytes": CSV_PATH.stat().st_size,
        "license": "NLM 数据；摘要版权随出版商，本表仅存元数据（PMID/文献类型/MeSH）",
        "page": "https://pubmed.ncbi.nlm.nih.gov/",
        "retrieved_at": now_iso(),
    })
    print(f"许可登记已更新：{REGISTRY.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
