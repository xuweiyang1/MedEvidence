#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence: diagnose CDC SHA mismatches and PMC license exceptions.

Run from project root:
    python scripts/diagnose_license_issues.py
    python scripts/diagnose_license_issues.py --root E:\\Download\\Agent\\MedEvidence

Reads only the existing License Gate V2 audit + referenced raw files.
Does NOT change source documents, manifests, license registries or V2 results.
Writes diagnostic reports under reports/.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到审核文件：{path}")
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def resolve_safe(root: Path, relative: str) -> Path | None:
    raw = (relative or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        return None
    p = (root / raw).resolve()
    try:
        p.relative_to(root)
        return p
    except ValueError:
        return None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cdc_diagnose(rows: list[dict[str, str]], root: Path) -> list[dict[str, str]]:
    result = []
    for r in rows:
        if r.get("source") != "CDC":
            continue
        relative = r.get("local_path", "")
        p = resolve_safe(root, relative)
        expected = (r.get("sha256_expected") or "").strip().lower()
        out = {
            "document_id": r.get("document_id", ""),
            "local_path": relative,
            "expected_sha256": expected,
            "actual_raw_sha256": "",
            "raw_size_bytes": "",
            "crlf_count": "",
            "lf_only_count": "",
            "match_mode": "",
            "diagnosis": "",
            "original_v2_decision": r.get("decision", ""),
        }
        if not p or not p.is_file():
            out["match_mode"] = "FILE_MISSING"
            out["diagnosis"] = "本地文件不存在或路径不安全"
            result.append(out)
            continue
        b = p.read_bytes()
        out["actual_raw_sha256"] = sha256(b)
        out["raw_size_bytes"] = str(len(b))
        crlf = b.count(b"\r\n")
        out["crlf_count"] = str(crlf)
        out["lf_only_count"] = str(b.count(b"\n") - crlf)
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            out["match_mode"] = "EXPECTED_MISSING_OR_INVALID"
            out["diagnosis"] = "manifest中的预期SHA256缺失或格式错误"
            result.append(out)
            continue
        # Only diagnostic byte-level transforms. No file is modified.
        variants = [
            ("RAW", b),
            ("CRLF_TO_LF", b.replace(b"\r\n", b"\n")),
            ("CRLF_TO_LF_AND_CR_TO_LF", b.replace(b"\r\n", b"\n").replace(b"\r", b"\n")),
            ("LF_TO_CRLF", b.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")),
        ]
        if b.startswith(b"\xef\xbb\xbf"):
            no_bom = b[3:]
            variants.extend([
                ("DROP_UTF8_BOM", no_bom),
                ("DROP_BOM_CRLF_TO_LF", no_bom.replace(b"\r\n", b"\n")),
            ])
        matching = [label for label, data in variants if sha256(data) == expected]
        if matching:
            out["match_mode"] = matching[0]  # 优先报告最简单的匹配变换
            if "RAW" in matching:
                out["diagnosis"] = "原始二进制哈希匹配"
            else:
                out["diagnosis"] = "仅文本字节变换后匹配；应区分原始文件哈希与规范化文本哈希，不可直接修改登记值"
        else:
            out["match_mode"] = "UNEXPLAINED"
            out["diagnosis"] = "不能用常见换行符/BOM差异解释；需检查CDC下载脚本、manifest生成方式及原始内容"
        result.append(out)
    return result


def tag_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_license_evidence(rows: list[dict[str, str]], root: Path) -> list[dict[str, str]]:
    result = []
    for r in rows:
        if r.get("source") != "PMC" or r.get("decision") != "HOLD" or not r.get("xml_license", "").startswith("CONFLICT:"):
            continue
        p = resolve_safe(root, r.get("local_path", ""))
        if p is None or not p.is_file():
            result.append({
                "document_id": r.get("document_id", ""), "local_path": r.get("local_path", ""),
                "license_node_no": "", "node_location": "", "node_attributes": "",
                "license_urls": "", "license_text_excerpt": "", "error": "XML文件缺失",
            })
            continue
        try:
            root_xml = ET.parse(p).getroot()
            parent_map = {child: parent for parent in root_xml.iter() for child in parent}
            nodes = [n for n in root_xml.iter() if tag_local(n.tag) == "license"]
            if not nodes:
                result.append({
                    "document_id": r.get("document_id", ""), "local_path": r.get("local_path", ""),
                    "license_node_no": "", "node_location": "", "node_attributes": "",
                    "license_urls": "", "license_text_excerpt": "", "error": "未找到license节点",
                })
            for n, node in enumerate(nodes, 1):
                ancestry = []
                cur = node
                while cur is not None:
                    ancestry.append(tag_local(cur.tag))
                    cur = parent_map.get(cur)
                ancestry.reverse()
                urls = []
                for e in node.iter():
                    for k, value in e.attrib.items():
                        if tag_local(k).lower() in ("href", "url"):
                            urls.append(value)
                text = " ".join("".join(node.itertext()).split())
                result.append({
                    "document_id": r.get("document_id", ""),
                    "local_path": r.get("local_path", ""),
                    "license_node_no": str(n),
                    "node_location": "/".join(ancestry),
                    "node_attributes": str({tag_local(k): v for k,v in node.attrib.items()}),
                    "license_urls": " | ".join(dict.fromkeys(urls))[:1000],
                    "license_text_excerpt": text[:650],
                    "error": "",
                })
        except (ET.ParseError, OSError, ValueError) as e:
            result.append({
                "document_id": r.get("document_id", ""), "local_path": r.get("local_path", ""),
                "license_node_no": "", "node_location": "", "node_attributes": "",
                "license_urls": "", "license_text_excerpt": "", "error": str(e),
            })
    return result


def write_csv(path: Path, records: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def main() -> int:
    parser = argparse.ArgumentParser(description="MedEvidence许可异常诊断（只读原始数据）")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    input_file = root / "licenses/document_license_audit_v2.csv"
    try:
        rows = read_csv(input_file)
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    cdc = cdc_diagnose(rows, root)
    pmc = xml_license_evidence(rows, root)
    authors = [{
        "document_id": r.get("document_id", ""),
        "title": r.get("title", ""),
        "xml_license": r.get("xml_license", ""),
        "decision": r.get("decision", ""),
        "review_reason": r.get("review_reason", ""),
        "local_path": r.get("local_path", ""),
    } for r in rows if r.get("source") == "PMC" and r.get("is_manuscript") == "True"]

    reports = root / "reports"
    reports.mkdir(exist_ok=True, parents=True)
    write_csv(reports / "cdc_hash_diagnostics.csv", cdc, [
        "document_id", "local_path", "expected_sha256", "actual_raw_sha256",
        "raw_size_bytes", "crlf_count", "lf_only_count", "match_mode", "diagnosis", "original_v2_decision",
    ])
    write_csv(reports / "pmc_license_conflicts.csv", pmc, [
        "document_id", "local_path", "license_node_no", "node_location", "node_attributes",
        "license_urls", "license_text_excerpt", "error",
    ])
    write_csv(reports / "pmc_manuscripts_review.csv", authors, [
        "document_id", "title", "xml_license", "decision", "review_reason", "local_path",
    ])
    cdc_modes = Counter(r["match_mode"] for r in cdc)
    conflicting_ids = {r["document_id"] for r in pmc}
    lines = [
        "=== MedEvidence License V2 异常诊断 ===",
        f"运行时间UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"项目目录: {root}",
        f"CDC共 {len(cdc)} 条，SHA256诊断分布: {dict(cdc_modes)}",
        f"PMC冲突文献: {len(conflicting_ids)} 篇，许可证节点: {len(pmc)} 条",
        f"PMC作者稿待复核: {len(authors)} 篇",
        "说明：字节规范化后哈希匹配，不等于原始文件哈希匹配；不修改原始CSV，也不自动放行HOLD/REVIEW。",
        "说明：PMC多license节点可能对应不同材料或不同权利，必须检查原文许可作用范围。",
    ]
    (reports / "license_diagnostics_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    print("\n".join(lines))
    print("\n已生成：")
    for name in ["cdc_hash_diagnostics.csv", "pmc_license_conflicts.csv", "pmc_manuscripts_review.csv", "license_diagnostics_summary.txt"]:
        print(" -", reports / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
