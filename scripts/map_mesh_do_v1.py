#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence OpenMedKB: V1 precise MeSH -> Disease Ontology mapping.

Requirements: Python >=3.11, standard library only. No GPU/API access needed.

Place at: E:/Download/Agent/MedEvidence/scripts/map_mesh_do_v1.py
Test:  python scripts/map_mesh_do_v1.py --limit 10
Full:  python scripts/map_mesh_do_v1.py

Read-only inputs:
  data/processed/eligibility/pmc_candidate_manifest.csv
  data/processed/normalized/pmc_articles.jsonl
  data/raw/mesh/desc2026.xml
  data/raw/do/doid.obo

Outputs (does not overwrite original normalized JSONL or any source/registry):
  data/processed/ontology/pmc_articles_disease_mapped[_smoke].jsonl
  reports/mesh_do_mapping_report[_smoke].csv
  reports/mesh_do_mapping_summary[_smoke].txt
  reports/mesh_unresolved_terms[_smoke].csv

V1 deliberately uses only **explicit** PubMed MeSH headings that can be
resolved against the official MeSH descriptor index, then mapped by explicit
Disease Ontology MESH xrefs. It does NOT infer disease labels from a broad
source-topic name or from title/abstract keywords. Coverage can therefore be
lower than true disease coverage, but confidence/traceability is higher.
CANDIDATE is for controlled local research, not legal reuse approval.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

MESH_ID = re.compile(r"(?<![A-Za-z0-9])D\d{6,}(?!\d)", re.I)
PMCID = re.compile(r"PMC\d+", re.I)
SHA256 = re.compile(r"[a-f0-9]{64}", re.I)
WHITESPACE = re.compile(r"\s+")
REPORT_COLUMNS = [
    "document_id", "source_topics", "pubmed_meta_matched", "mesh_raw_present",
    "mesh_headings_recognized", "mesh_disease_headings", "doid_count", "doid_list",
    "unknown_mesh_terms", "mapping_status", "body_blocks", "quality_warnings"
]


def norm(s: str) -> str:
    return WHITESPACE.sub(" ", s).strip().casefold()


def localtag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def first_child(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    return next((x for x in node if localtag(x) == name), None)


def path_child(node: ET.Element | None, *path: str) -> ET.Element | None:
    for part in path:
        node = first_child(node, part)
    return node


def elt_text(node: ET.Element | None) -> str:
    return "" if node is None else " ".join("".join(node.itertext()).split())


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        if not r.fieldnames:
            raise ValueError(f"CSV missing header: {path}")
        return list(r)


def mesh_index(path: Path):
    """Streaming parse (~300MB XML) to avoid loading the entire tree."""
    by_id = {}
    by_name = {}
    total = 0
    disease_count = 0
    for _, record in ET.iterparse(path, events=("end",)):
        if localtag(record) != "DescriptorRecord":
            continue
        total += 1
        ui = elt_text(first_child(record, "DescriptorUI")).upper()
        name = elt_text(path_child(record, "DescriptorName", "String"))
        tree_numbers = []
        tree_list = first_child(record, "TreeNumberList")
        if tree_list is not None:
            tree_numbers = [elt_text(n) for n in tree_list if localtag(n) == "TreeNumber"]
        is_disease = any(t.startswith("C") or t.startswith("F03") for t in tree_numbers)
        if MESH_ID.fullmatch(ui) and name:
            by_id[ui] = {"mesh_id": ui, "mesh_name": name, "is_disease_tree": is_disease}
            # Ambiguous normalized name is deliberately excluded.
            key = norm(name)
            if key in by_name and by_name[key] != ui:
                by_name[key] = None
            elif key not in by_name:
                by_name[key] = ui
            disease_count += int(is_disease)
        record.clear()
    if not by_id:
        raise ValueError(f"MeSH XML 未读出 DescriptorRecord: {path}")
    return by_id, by_name, total, disease_count


def do_index(path: Path):
    """Read stable, non-obsolete OBO terms and their literal MESH:D... xrefs."""
    mapping: dict[str, list[dict[str, str]]] = defaultdict(list)
    total = 0
    obsolete = 0
    current = None

    def commit(d):
        nonlocal total, obsolete
        if not d or not d.get("id", "").startswith("DOID:"):
            return
        total += 1
        if d.get("obsolete"):
            obsolete += 1
            return
        for mid in set(d["xrefs"]):
            mapping[mid].append({"doid": d["id"], "do_name": d.get("name", "")})

    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if line == "[Term]":
                commit(current)
                current = {"id": "", "name": "", "xrefs": [], "obsolete": False}
                continue
            if line.startswith("[") and line.endswith("]"):
                commit(current)
                current = None
                continue
            if current is None:
                continue
            if line.startswith("id: "):
                current["id"] = line[4:].strip()
            elif line.startswith("name: "):
                current["name"] = line[6:].strip()
            elif line == "is_obsolete: true":
                current["obsolete"] = True
            elif line.startswith("xref: "):
                v = line[6:].split()[0].strip('"')
                if re.match(r"(?i)^MESH:D\d{6,}$", v):
                    current["xrefs"].append(v.split(":", 1)[1].upper())
    commit(current)
    if not total:
        raise ValueError(f"Disease Ontology OBO 未读出 DOID Term: {path}")
    unique = {k: sorted({(v["doid"], v["do_name"]) for v in vals}) for k, vals in mapping.items()}
    return unique, total, obsolete


def flatten_mesh_value(raw):
    """Support str, JSON encoded list, and common dict descriptor key formats."""
    if isinstance(raw, list):
        result = []
        for x in raw:
            result.extend(flatten_mesh_value(x))
        return result
    if isinstance(raw, dict):
        for key in ("descriptor_ui", "DescriptorUI", "mesh_id", "ui", "descriptor", "name", "DescriptorName", "term"):
            if raw.get(key):
                return flatten_mesh_value(raw[key])
        return []
    if not isinstance(raw, str):
        return []
    v = raw.strip()
    if not v:
        return []
    if v.startswith(("[", "{")):
        try:
            return flatten_mesh_value(json.loads(v))
        except (ValueError, TypeError):
            pass
    return [p.strip() for p in re.split(r"[;|\r\n]+", v) if p.strip()]


def resolve_mesh_term(raw: str, by_id: dict, by_name: dict):
    """Strict descriptor match: ID first; else complete preferred heading name."""
    token = str(raw).strip().strip('*').strip()
    match = MESH_ID.search(token)
    if match and match.group().upper() in by_id:
        return by_id[match.group().upper()]
    # PubMed headings sometimes append /subheading, or '(Major Topic)'.
    token = re.sub(r"\s*\(major\s+topic\)\s*$", "", token, flags=re.I)
    token = token.split("/", 1)[0].strip().strip('*').strip()
    mid = by_name.get(norm(token))
    return by_id.get(mid) if mid else None


def classify(doc: dict, by_id: dict, by_name: dict, do_xrefs: dict):
    raw = doc.get("mesh_terms_raw", "")
    values = flatten_mesh_value(raw)
    resolved = {}
    unknown = []
    for token in values:
        desc = resolve_mesh_term(token, by_id, by_name)
        if desc:
            resolved[desc["mesh_id"]] = desc
        else:
            unknown.append(token)
    disease_mesh = []
    entities = []
    ambig = []
    for mid, desc in sorted(resolved.items()):
        matches = do_xrefs.get(mid, [])
        if not desc["is_disease_tree"] and not matches:
            continue
        disease_mesh.append({"mesh_id": mid, "mesh_name": desc["mesh_name"],
                             "mesh_disease_tree": desc["is_disease_tree"]})
        if len(matches) == 1:
            doid, name = matches[0]
            entities.append({"doid": doid, "name": name, "mesh_id": mid,
                             "mesh_name": desc["mesh_name"],
                             "match_method": "PUBMED_MESH_DESCRIPTOR_XREF"})
        elif len(matches) > 1:
            ambig.append({"mesh_id": mid, "candidate_doids": [a[0] for a in matches]})
    mapped_ids = sorted({e["doid"] for e in entities})
    matched = bool(doc.get("pubmed_meta_matched"))
    if mapped_ids:
        status = "MAPPED_DO"
    elif disease_mesh:
        status = "MESH_DISEASE_ONLY"
    elif not values:
        status = "NO_MESH_TERMS" if matched else "NO_PUBMED_METADATA"
    elif resolved:
        status = "NO_DISEASE_HEADING"
    else:
        status = "UNRECOGNIZED_MESH_FORMAT"
    return {
        "disease_ontology_ids": mapped_ids,
        "disease_entities": entities,
        "mesh_disease_descriptors": disease_mesh,
        "disease_mapping": {
            "method": "MeSH_2026_DO_xref_v1",
            "mapping_status": status,
            "matched_mesh_heading_count": len(resolved),
            "matched_mesh_disease_count": len(disease_mesh),
            "unresolved_mesh_terms": unknown[:25],
            "unresolved_mesh_term_count": len(unknown),
            "ambiguous_doid_xrefs": ambig,
            "warning": "MeSH/DO only; missing labels are not evidence of no disease",
        }
    }, unknown


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--limit", type=int, default=0, help="Smoke test first N; separate _smoke outputs")
    args = p.parse_args()
    if args.limit < 0:
        p.error("--limit must be >= 0")
    root = args.root.expanduser().resolve()
    candidate = root / "data/processed/eligibility/pmc_candidate_manifest.csv"
    input_jsonl = root / "data/processed/normalized/pmc_articles.jsonl"
    mesh_xml = root / "data/raw/mesh/desc2026.xml"
    obo = root / "data/raw/do/doid.obo"
    suffix = "_smoke" if args.limit else ""
    result_jsonl = root / f"data/processed/ontology/pmc_articles_disease_mapped{suffix}.jsonl"
    report_csv = root / f"reports/mesh_do_mapping_report{suffix}.csv"
    unknown_csv = root / f"reports/mesh_unresolved_terms{suffix}.csv"
    summary_txt = root / f"reports/mesh_do_mapping_summary{suffix}.txt"
    tmp_jsonl = result_jsonl.with_suffix(".jsonl.tmp")
    tmp_report = report_csv.with_suffix(".csv.tmp")
    tmp_unknown = unknown_csv.with_suffix(".csv.tmp")
    for filepath in (candidate, input_jsonl, mesh_xml, obo):
        if not filepath.is_file():
            print(f"[ERROR] 缺少输入文件: {filepath}", file=sys.stderr)
            return 2
    try:
        eligible_rows = read_csv(candidate)
        if not eligible_rows:
            raise ValueError("PMC候选清单为空")
        eligible_ids = set()
        candidate_by_id = {}
        for row in eligible_rows:
            did = row.get("document_id", "").strip().upper()
            expected = (row.get("sha256_expected") or "").strip().lower()
            if (row.get("v3_decision") != "CANDIDATE" or not PMCID.fullmatch(did)
                    or did in eligible_ids or not SHA256.fullmatch(expected)):
                raise ValueError(f"候选清单状态、PMCID、哈希或唯一性异常: {did}")
            eligible_ids.add(did)
            candidate_by_id[did] = row
        print("[1/3] 加载 MeSH 2026 描述词（流式 XML，可能需要一点时间）...")
        by_id, by_name, mesh_total, mesh_diseases = mesh_index(mesh_xml)
        print(f"      MeSH descriptors: {len(by_id)} / total records {mesh_total}, disease-tree {mesh_diseases}")
        print("[2/3] 加载 Disease Ontology OBO 的显式 MeSH xref...")
        doxref, do_total, do_obsolete = do_index(obo)
        print(f"      DO terms: {do_total}, obsolete: {do_obsolete}, MeSH links: {len(doxref)}")
        if not doxref:
            print("[WARN] 未找到任何 MESH xref；请检查 doid.obo 内容格式。")
        print("[3/3] 映射文章标签和写入报告...")
        for outfile in (result_jsonl, report_csv, unknown_csv, summary_txt):
            outfile.parent.mkdir(parents=True, exist_ok=True)
        counts = Counter()
        raw_stats = Counter()
        unknowns = Counter()
        processed = 0
        unique = set()
        mapped_doids = Counter()
        topics = Counter()
        with input_jsonl.open("r", encoding="utf-8-sig") as inp, \
             tmp_jsonl.open("w", encoding="utf-8", newline="\n") as out, \
             tmp_report.open("w", encoding="utf-8-sig", newline="") as rf:
            writer = csv.DictWriter(rf, fieldnames=REPORT_COLUMNS)
            writer.writeheader()
            for line_no, line in enumerate(inp, 1):
                if not line.strip():
                    continue
                doc = json.loads(line)
                if args.limit and processed >= args.limit:
                    break
                did = (doc.get("document_id") or "").strip().upper()
                if did in unique or did not in eligible_ids:
                    raise ValueError(f"输入JSONL第{line_no}行包含重复/非候选文档: {did}")
                unique.add(did)
                if doc.get("source") != "PMC" or doc.get("rights", {}).get("audit_status") != "CANDIDATE":
                    raise ValueError(f"输入JSONL非PMC候选文章: {did}")
                source_hash = doc.get("provenance", {}).get("source_xml_sha256", "").strip().lower()
                registered_hash = candidate_by_id[did]["sha256_expected"].strip().lower()
                if source_hash != registered_hash:
                    raise ValueError(f"原XML与候选清单的SHA256溯源信息不一致: {did}")
                newfields, unresolved = classify(doc, by_id, by_name, doxref)
                doc.update(newfields)
                doc["base_schema_version"] = doc.get("schema_version", "")
                doc["schema_version"] = "openmedkb.article.disease.v1"
                # Leave source_topic_tags untouched: retrieval-domain tag is not a disease ID.
                out.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n")
                status = newfields["disease_mapping"]["mapping_status"]
                counts[status] += 1
                if doc.get("mesh_terms_raw"):
                    raw_stats["mesh_raw_nonempty"] += 1
                if doc.get("pubmed_meta_matched"):
                    raw_stats["pubmed_metadata_found"] += 1
                for u in unresolved:
                    unknowns[u] += 1
                for disease_id in newfields["disease_ontology_ids"]:
                    mapped_doids[disease_id] += 1
                for topic in doc.get("source_topic_tags", []):
                    topics[topic] += 1
                processed += 1
                blocks = sum(len(s.get("blocks", [])) for s in doc.get("body_sections", []))
                writer.writerow({
                    "document_id": did,
                    "source_topics": ";".join(doc.get("source_topic_tags", [])),
                    "pubmed_meta_matched": doc.get("pubmed_meta_matched", False),
                    "mesh_raw_present": bool(doc.get("mesh_terms_raw")),
                    "mesh_headings_recognized": newfields["disease_mapping"]["matched_mesh_heading_count"],
                    "mesh_disease_headings": newfields["disease_mapping"]["matched_mesh_disease_count"],
                    "doid_count": len(newfields["disease_ontology_ids"]),
                    "doid_list": ";".join(newfields["disease_ontology_ids"]),
                    "unknown_mesh_terms": newfields["disease_mapping"]["unresolved_mesh_term_count"],
                    "mapping_status": status,
                    "body_blocks": blocks,
                    "quality_warnings": ";".join(doc.get("quality_warnings", [])),
                })
                if processed % 100 == 0:
                    print(f"      {processed} articles; DO-mapped={counts['MAPPED_DO']}")
        if not args.limit and unique != eligible_ids:
            raise ValueError(f"全文输入与候选清单不一致: expected={len(eligible_ids)}, observed={len(unique)}; missing={len(eligible_ids-unique)}")
        with tmp_unknown.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["raw_term", "count", "note"])
            for term, cnt in unknowns.most_common(250):
                w.writerow([term, cnt, "Not resolved to official MeSH descriptor; no fuzzy matching"])
        for src, dst in ((tmp_jsonl, result_jsonl), (tmp_report, report_csv), (tmp_unknown, unknown_csv)):
            os.replace(src, dst)
        mapped_docs = counts["MAPPED_DO"]
        summary_lines = [
            "=== MedEvidence OpenMedKB MeSH + Disease Ontology 映射 V1 ===",
            f"生成时间UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"运行模式: {'SMOKE' if args.limit else 'FULL'}",
            f"输入文章: {processed} / V3候选总数 {len(eligible_ids)}",
            f"PubMed关联标记为True: {raw_stats['pubmed_metadata_found']}",
            f"MeSH原始字段非空: {raw_stats['mesh_raw_nonempty']}",
            f"MeSH Descriptor总数: {mesh_total} (有效UI: {len(by_id)})",
            f"MeSH疾病树描述词: {mesh_diseases}",
            f"DOID Term总数: {do_total} (obsolete: {do_obsolete})",
            f"DOID-MESH显式xref键数: {len(doxref)}",
            f"疾病映射结果: {dict(counts)}",
            f"关联到至少1个DOID的文章: {mapped_docs}/{processed}",
            f"不同DOID数量: {len(mapped_doids)}",
            f"前15个DOID及文章数: {mapped_doids.most_common(15)}",
            f"主题来源分布: {dict(topics)}",
            f"未识别MeSH词项(去重): {len(unknowns)}",
            f"输出JSONL: {result_jsonl.relative_to(root)}",
            f"逐篇报告: {report_csv.relative_to(root)}",
            f"未识别词项: {unknown_csv.relative_to(root)}",
            "V1仅接受PubMed明确的MeSH descriptor名称/ID，并经DO显式xref映射；不会将下载主题当作标准疾病标签。",
            "未映射不等于无疾病；可能是MeSH字段编码方式、尚未标引、DO交叉引用覆盖不足等原因。",
            "CANDIDATE仅表示本地实验候选，未批准公开再分发；原始数据和许可表均未修改。",
        ]
        summary_txt.write_text("\n".join(summary_lines) + "\n", encoding="utf-8-sig")
        print("\n".join(summary_lines))
        return 0
    except (ValueError, OSError, ET.ParseError, json.JSONDecodeError, TypeError) as e:
        print(f"[ERROR] 映射未完成: {e}", file=sys.stderr)
        for tmp in (tmp_jsonl, tmp_report, tmp_unknown):
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        return 2


if __name__ == "__main__":
    sys.exit(main())
