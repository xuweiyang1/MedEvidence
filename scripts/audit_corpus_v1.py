#!/usr/bin/env python3
"""MedEvidence V1: read-only corpus inventory and preliminary license audit.

Place this file in <project>/scripts/audit_corpus_v1.py.
Uses Python standard library only. Never alters any existing data or registry.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
REPORTS = ROOT / "reports"
LICENSES = ROOT / "licenses"

# These are preliminary handling rules, not a legal opinion. Verify article-level rights.
ALLOW = {"CC0", "CC-BY-3.0", "CC-BY-4.0", "PUBLIC-DOMAIN"}


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                reader = csv.DictReader(f)
                cols = list(reader.fieldnames or [])
                rows = [dict(row) for row in reader]
            return cols, rows
        except UnicodeDecodeError:
            pass
    raise ValueError(f"无法解析 CSV 文件编码: {path}")


def cell(row: dict[str, str], *names: str) -> str:
    aliases = {str(k).strip().lower().replace(" ", "_").replace("-", "_"): k
               for k in row if k}
    for name in names:
        key = aliases.get(name.lower())
        if key is not None:
            val = str(row.get(key) or "").strip()
            if val:
                return val
    return ""


def license_cell(row: dict[str, str]) -> str:
    first = cell(row, "license", "licence", "license_type", "license_name",
                 "license_spdx", "license_id", "licence_type", "license_url")
    if first:
        return first
    for k, v in row.items():
        if k and ("license" in k.lower() or "licence" in k.lower()) and v:
            return str(v).strip()
    return ""


def normalize_license(value: str) -> str:
    s = value.strip().upper()
    if not s:
        return "UNKNOWN"
    # Creative Commons URLs are often the most reliable machine-readable form.
    m = re.search(r"CREATIVECOMMONS\.ORG/(?:LICENSES/(BY(?:-[A-Z]+)*)/([0-9.]+)|PUBLICDOMAIN/ZERO/([0-9.]+))", s)
    if m:
        if m.group(3):
            return "CC0"
        code, version = m.group(1), m.group(2).strip(".")
        return f"CC-{code}-{version}"
    s = re.sub(r"[\s_]+", "-", s).replace("CC-BY--", "CC-BY-")
    if re.search(r"\bCC0\b|CC-ZERO|PUBLIC-DOMAIN-DEDICATION", s):
        return "CC0"
    if "PUBLIC-DOMAIN" in s or "PUBLIC DOMAIN" in value.upper():
        return "PUBLIC-DOMAIN"
    m = re.search(r"(?:CC-)?BY(?:-(NC|ND|SA|NC-SA|NC-ND|ND-SA))?-(\d+(?:\.\d+)?)", s)
    if m:
        modifier, version = m.groups()
        return f"CC-BY{('-' + modifier) if modifier else ''}-{version}"
    return s


def policy_for(value: str, source: str) -> tuple[str, str]:
    normalized = normalize_license(value)
    if normalized in ALLOW:
        return "CANDIDATE", "许可字样符合V1候选白名单；仍需核对原文许可证据、署名和第三方内容"
    if source == "CDC" and normalized == "UNKNOWN":
        return "REVIEW", "不能因来自CDC就默认全部公共领域；核查页面及第三方材料"
    if source == "WHO" and normalized == "UNKNOWN":
        return "REVIEW", "逐份核实WHO PDF许可证、第三方版权和NC/SA条件"
    if normalized == "UNKNOWN":
        return "REVIEW", "缺少可核查的单篇许可证字段，不得自动准入"
    if "-ND-" in normalized or "ALL-RIGHTS" in normalized:
        return "HOLD", "存在禁止衍生或保留权利条件，须人工评估；V1不自动准入"
    if "-NC-" in normalized or "-SA-" in normalized:
        return "REVIEW", "许可证含NC/SA条件，不自动准入V1通用语料"
    return "REVIEW", "许可证非当前白名单或名称未识别；需人工审查"


def count_matching(dir_rel: str, glob_pattern: str) -> tuple[int, int]:
    base = ROOT / dir_rel
    if not base.is_dir():
        return 0, 0
    count = bytes_total = 0
    for p in base.rglob(glob_pattern):
        if p.is_file():
            count += 1
            bytes_total += p.stat().st_size
    return count, bytes_total


def size_mb(n: int) -> str:
    return f"{n / 1024 / 1024:.2f}"


def write_csv(path: Path, headers: list[str], rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(rows)


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def main():
    if not (ROOT / "data").is_dir():
        print(f"ERROR: 未找到 {ROOT / 'data'}，请将脚本放在项目 scripts 文件夹。", file=sys.stderr)
        return 2
    REPORTS.mkdir(parents=True, exist_ok=True)

    categories = [
        ("PMC 原始 XML", "data/raw/pmc/articles", "*.xml"),
        ("PMC 原始 JSON", "data/raw/pmc/articles", "*.json"),
        ("PMC provenance", "data/raw/pmc/provenance", "*.json"),
        ("PMC 已解析 TXT", "data/processed/pmc_text", "*.txt"),
        ("WHO PDF", "data/raw/who", "*.pdf"),
        ("CDC TXT", "data/raw/cdc", "*.txt"),
        ("MeSH XML", "data/raw/mesh", "*.xml"),
        ("Disease Ontology 本体", "data/raw/do", "*.obo"),
        ("Disease Ontology OWL", "data/raw/do", "*.owl"),
        ("PubMed XML", "data/raw/pubmed/xml", "*.xml"),
    ]
    inventory = []
    for label, folder, pattern in categories:
        num, size = count_matching(folder, pattern)
        inventory.append({"category": label, "folder": folder,
                          "pattern": pattern, "file_count": num,
                          "size_bytes": size, "size_MiB": size_mb(size)})

    manifests = {
        "pmc_manifest": RAW / "pmc/manifest.csv",
        "cdc_manifest": RAW / "cdc/manifest.csv",
        "source_registry": LICENSES / "registry.csv",
        "pubmed_meta": RAW / "pubmed/pubmed_meta.csv",
    }
    loaded = {name: read_csv(p) for name, p in manifests.items()}
    for name, path in manifests.items():
        _, rows = loaded[name]
        inventory.append({"category": f"{name} 记录", "folder": rel(path),
                          "pattern": "CSV rows", "file_count": len(rows),
                          "size_bytes": path.stat().st_size if path.exists() else 0,
                          "size_MiB": size_mb(path.stat().st_size if path.exists() else 0)})

    write_csv(REPORTS / "data_inventory.csv",
              ["category", "folder", "pattern", "file_count", "size_bytes", "size_MiB"], inventory)

    pmc_dirs = {}
    article_dir = RAW / "pmc/articles"
    if article_dir.is_dir():
        for p in article_dir.iterdir():
            if p.is_dir():
                found = re.search(r"PMC\d+", p.name, flags=re.IGNORECASE)
                if found:
                    pmc_dirs[found.group().upper()] = p

    all_docs = []
    pmc_cols, pmc_rows = loaded["pmc_manifest"]
    cdc_cols, cdc_rows = loaded["cdc_manifest"]
    for idx, row in enumerate(pmc_rows, start=1):
        raw_id = cell(row, "pmcid", "pmc_id", "pmc", "article_id", "article", "id", "uid")
        match = re.search(r"PMC\s*\d+|^\d+$", raw_id, flags=re.IGNORECASE)
        if match:
            pmcid = match.group().replace(" ", "").upper()
            if not pmcid.startswith("PMC"):
                pmcid = "PMC" + pmcid
        else:
            pmcid = f"PMC_MANIFEST_ROW_{idx:05d}"
        lval = license_cell(row)
        pol, note = policy_for(lval, "PMC")
        mapped = pmc_dirs.get(pmcid)
        if mapped is None:
            note += "; 未自动关联对应PMC目录，请核对ID列"
        all_docs.append({
            "source": "PMC", "document_id": pmcid,
            "local_path": rel(mapped) if mapped else "",
            "source_url": cell(row, "url", "source_url", "article_url", "oa_url"),
            "license_raw": lval, "license_normalized": normalize_license(lval),
            "decision": pol, "note": note,
            "registered_sha256": cell(row, "sha256", "sha_256", "hash"),
            "record_origin": rel(manifests["pmc_manifest"]),
            "row_number": idx,
        })
    for idx, row in enumerate(cdc_rows, start=1):
        doc_id = cell(row, "document_id", "content_id", "id", "topic_id", "title") or f"CDC_ROW_{idx:04d}"
        lval = license_cell(row)
        pol, note = policy_for(lval, "CDC")
        all_docs.append({
            "source": "CDC", "document_id": doc_id,
            "local_path": cell(row, "file_path", "path", "local_path", "output_file"),
            "source_url": cell(row, "source_url", "url", "canonical_url"),
            "license_raw": lval, "license_normalized": normalize_license(lval),
            "decision": pol, "note": note,
            "registered_sha256": cell(row, "sha256", "sha_256", "hash"),
            "record_origin": rel(manifests["cdc_manifest"]), "row_number": idx,
        })
    who_dir = RAW / "who"
    if who_dir.is_dir():
        for idx, p in enumerate(sorted(who_dir.rglob("*.pdf")), start=1):
            pol, note = policy_for("", "WHO")
            all_docs.append({
                "source": "WHO", "document_id": p.stem,
                "local_path": rel(p), "source_url": "",
                "license_raw": "", "license_normalized": "UNKNOWN",
                "decision": pol, "note": note + "; 待与licenses/registry.csv中的WHO条目人工对应",
                "registered_sha256": "", "record_origin": "PDF file scan", "row_number": idx,
            })

    registry_fields = ["source", "document_id", "local_path", "source_url",
                       "license_raw", "license_normalized", "decision", "note",
                       "registered_sha256", "record_origin", "row_number"]
    write_csv(LICENSES / "document_license_draft.csv", registry_fields, all_docs)
    header_info = {k: {"path": rel(manifests[k]), "exists": manifests[k].is_file(),
                       "headers": loaded[k][0], "rows": len(loaded[k][1])}
                   for k in manifests}
    (REPORTS / "csv_schema.json").write_text(
        json.dumps(header_info, ensure_ascii=False, indent=2), encoding="utf-8")

    source_summary = dict(Counter(x["source"] for x in all_docs))
    decision_summary = dict(Counter(x["decision"] for x in all_docs))
    notes = [
        f"生成时间(UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "此报告只读原始数据；未修改 licenses/registry.csv 或任何原始文章。",
        "CANDIDATE 不是法律确认或最终准入。REVIEW / HOLD 不得自动进入可再分发语料库。",
        "PubMed元数据不能当成可以任意重用的摘要/全文。",
        "PMC每篇许可证须核对manifest声明和原文XML证据；WHO按PDF逐份核对；CDC须排除第三方版权内容。",
        "缺失源URL、许可URL、SHA256的记录后续需要补齐；本程序不会猜测它们。",
        "现有 source-level registry 不等于 document-level 许可审计。",
        f"PMC manifest行数: {len(pmc_rows)}; PMC文章目录数: {len(pmc_dirs)}",
        f"CDC manifest行数: {len(cdc_rows)}; WHO PDF数: {source_summary.get('WHO', 0)}",
        f"PubMed meta行数: {len(loaded['pubmed_meta'][1])}; 来源级registry行数: {len(loaded['source_registry'][1])}",
        f"文档级草稿记录: {len(all_docs)}; 来源分布: {source_summary}; 许可状态分布: {decision_summary}",
    ]
    if pmc_rows and pmc_dirs and len(pmc_rows) != len(pmc_dirs):
        notes.append("注意: PMC manifest数与PMC目录数不一致；需检查是否存在版本目录/重复项/遗漏。")
    (REPORTS / "audit_notes.txt").write_text("\n".join(notes) + "\n", encoding="utf-8-sig")

    print("=== MedEvidence 数据盘点 ===")
    for x in inventory:
        print(f"{x['category']:<26} {x['file_count']:>5}  {x['size_MiB']:>9} MiB")
    print(f"\n文档许可证草稿: {len(all_docs)} 条")
    print(f"许可候选与待审: {decision_summary}")
    print("已生成 reports/data_inventory.csv")
    print("已生成 reports/csv_schema.json")
    print("已生成 reports/audit_notes.txt")
    print("已生成 licenses/document_license_draft.csv")
    print("注意: 并未修改原有 registry.csv 或任何原始文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
