#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence License Gate V2 — 文档级许可证据核查（只读原始资料）。

用法（放在 MedEvidence/scripts/license_gate_v2.py）：
    python scripts/license_gate_v2.py
    python scripts/license_gate_v2.py --root E:\\Download\\Agent\\MedEvidence
    python scripts/license_gate_v2.py --no-hash   # 仅调试，正式验收不要使用

仅读取：
  data/raw/pmc/manifest.csv; data/raw/pmc/articles/**/*.xml
  data/raw/cdc/manifest.csv; data/raw/cdc/**/*.txt
  licenses/registry.csv; data/raw/who/*.pdf
不会修改源数据、旧的 licenses/registry.csv 或旧的 document_license_draft.csv。
仅写入：
  licenses/document_license_audit_v2.csv
  reports/license_audit_v2.txt

四种准入状态：
  CANDIDATE  具备可核对的许可候选依据；还需人工批准及排除第三方材料
  REVIEW     信息缺失/版本含糊/作者稿/资料来源待查
  RESTRICTED 非商业或其他限制许可，需独立的使用政策；禁止自动进入通用库
  HOLD       内容/哈希冲突、撤稿或严重许可不符，暂停使用

本脚本不是法律意见，也不会给出最终 APPROVED 的状态。
使用 Python 3.11 标准库即可，无需安装第三方包。
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

FIELDS = [
    "source", "document_id", "disease", "title", "version", "local_path",
    "source_url", "license_raw", "xml_license", "license_normalized",
    "license_url", "license_evidence", "decision", "review_reason",
    "is_manuscript", "is_retracted", "sha256_expected", "sha256_actual",
    "hash_check", "file_exists", "size_check", "record_origin", "checked_at",
]


def csv_rows(path: Path, required: bool = True) -> list[dict[str, str]]:
    if not path.is_file():
        if required:
            raise FileNotFoundError(f"找不到必需的CSV: {path}")
        return []
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法读取CSV编码: {path}")


def checked_path(root: Path, base: Path, raw_path: str) -> Path | None:
    """处理 manifest 中的项目相对路径和子目录相对路径，不允许逃逸项目。"""
    raw_path = (raw_path or "").strip().replace("\\", "/")
    if not raw_path or re.match(r"^[A-Za-z]:/", raw_path) or raw_path.startswith("/"):
        return None
    p = Path(raw_path)
    if raw_path.startswith("data/") or raw_path.startswith("licenses/"):
        candidate = root / p
    else:
        candidate = base / p
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def display_path(root: Path, path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def integrity(path: Path | None, expected_hash: str, expected_bytes: str, do_hash: bool):
    exists = bool(path and path.is_file())
    if not exists:
        return "MISSING", "", "MISSING", "MISSING"
    assert path is not None
    try:
        size_ok = "MATCH" if (expected_bytes and path.stat().st_size == int(expected_bytes)) else ("MISMATCH" if expected_bytes else "NOT_REGISTERED")
    except ValueError:
        size_ok = "BAD_REGISTERED_SIZE"
    if not do_hash:
        return "EXISTS", "", "NOT_CHECKED", size_ok
    actual = sha256_file(path)
    if re.fullmatch(r"[a-fA-F0-9]{64}", (expected_hash or "").strip()):
        hash_status = "MATCH" if actual.lower() == expected_hash.lower() else "MISMATCH"
    else:
        hash_status = "NOT_REGISTERED"
    return "EXISTS", actual, hash_status, size_ok


def normalize_license(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "UNKNOWN"
    u = s.upper().replace("_", "-")
    url = re.search(r"CREATIVECOMMONS\.ORG/LICENSES/(BY(?:-(?:NC|ND|SA))*)/(\d+(?:\.\d+)?)/?", u)
    if url:
        return "CC-" + url.group(1) + "-" + url.group(2)
    if re.search(r"CREATIVECOMMONS\.ORG/PUBLICDOMAIN/ZERO/|\bCC\s*0\b|\bCC0\b", u):
        return "CC0"
    if "PUBLIC DOMAIN" in u or "PUBLIC-DOMAIN" in u:
        return "PUBLIC-DOMAIN"
    compact = re.sub(r"\s+", "-", u)
    m = re.search(r"\bCC-BY(?:-(?:NC|ND|SA)){0,2}(?:-\d+(?:\.\d+)?)?\b", compact)
    if m:
        return m.group().replace("--", "-")
    if "CREATIVE COMMONS" in u and "ATTRIBUTION" in u:
        mod = ""
        if "NONCOMMERCIAL" in compact or "NON-COMMERCIAL" in compact:
            mod += "-NC"
        if "NODERIVATIVES" in compact or "NO-DERIVATIVES" in compact:
            mod += "-ND"
        if "SHAREALIKE" in compact or "SHARE-ALIKE" in compact:
            mod += "-SA"
        v = re.search(r"\b([34]\.0)\b", compact)
        return "CC-BY" + mod + ("-" + v.group(1) if v else "")
    return compact


def license_family(code: str) -> str:
    if code in {"CC0", "PUBLIC-DOMAIN"}:
        return code
    x = re.fullmatch(r"(CC-BY(?:-(?:NC|ND|SA)){0,2})(?:-\d+(?:\.\d+)?)?", code)
    return x.group(1) if x else code


def parse_jats_license(path: Path) -> tuple[str, str, str, str]:
    """从真正的 <license> 节点取出处，不能把文章正文中的CC声明误当许可证。"""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError, ValueError) as e:
        return "", "", "", f"XML解析失败: {type(e).__name__}: {e}"
    evidences: list[str] = []
    codes: set[str] = set()
    best_url = ""
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "license":
            continue
        contents = " ".join("".join(node.itertext()).split())
        urls = []
        for item in node.iter():
            for attr, value in item.attrib.items():
                if attr.rsplit("}", 1)[-1].lower() in {"href", "url"}:
                    urls.append(value)
            if item.text and re.search(r"https?://(?:www\.)?creativecommons\.org/", item.text, re.I):
                urls.extend(re.findall(r"https?://[^\s<>\"']+", item.text))
        for url in urls:
            c = normalize_license(url)
            if c.startswith("CC-") or c == "CC0":
                codes.add(c)
                if not best_url:
                    best_url = url
        # 仅当没有有效 URL 时从许可声明文本提取，避免把正文的引用当作证据。
        if not any(normalize_license(x).startswith("CC-") or normalize_license(x) == "CC0" for x in urls):
            c = normalize_license(contents)
            if c.startswith("CC-") or c == "CC0":
                codes.add(c)
        evidences.append(contents[:260])
    if len(codes) == 1:
        code = next(iter(codes))
    elif len(codes) > 1:
        code = "CONFLICT:" + ",".join(sorted(codes))
    else:
        code = "UNKNOWN"
    return code, best_url, " | ".join(evidences)[:400], "" if evidences else "XML没有<license>声明"


def base_entry(source: str, document_id: str, checked_at: str) -> dict[str, str]:
    row = {f: "" for f in FIELDS}
    row.update(source=source, document_id=document_id, checked_at=checked_at)
    return row


def pmc_records(root: Path, rows: list[dict[str, str]], do_hash: bool, checked_at: str):
    base = root / "data/raw/pmc"
    for r in rows:
        pmcid = r.get("pmcid", "").strip()
        d = base_entry("PMC", pmcid, checked_at)
        path = checked_path(root, base, r.get("xml_path", ""))
        path_str = display_path(root, path)
        declared = normalize_license(r.get("license_code", ""))
        exists, actual_hash, hash_status, size_status = integrity(path, r.get("xml_sha256", ""), r.get("xml_bytes", ""), do_hash)
        manuscript = r.get("is_manuscript", "").strip().lower() in {"1", "true", "yes"}
        retracted = r.get("is_retracted", "").strip().lower() in {"1", "true", "yes"}
        xml_license = url = evidence = error = ""
        if exists == "EXISTS" and path is not None:
            xml_license, url, evidence, error = parse_jats_license(path)
        reasons: list[str] = []
        decision = "REVIEW"
        if exists != "EXISTS":
            reasons.append("找不到原始XML或路径非法")
        if hash_status == "MISMATCH" or size_status == "MISMATCH":
            decision = "HOLD"
            reasons.append("XML与manifest的SHA256/字节数不一致")
        if error:
            reasons.append(error)
        # 原始manifest使用不带版本的'CC BY'，绝不擅自标成CC-BY-4.0。
        if declared == "CC-BY" and xml_license.startswith("CC-BY-") and xml_license in {"CC-BY-3.0", "CC-BY-4.0", "CC-BY-2.0", "CC-BY-2.5"}:
            if decision != "HOLD" and exists == "EXISTS" and not error and hash_status == "MATCH" and size_status in {"MATCH", "NOT_REGISTERED"}:
                decision = "CANDIDATE"
        elif declared == "CC-BY" and xml_license == "CC-BY":
            reasons.append("XML许可只有CC BY，版本未明确：需要人工确认")
        elif declared == "CC-BY" and xml_license == "UNKNOWN":
            reasons.append("manifest为CC BY，但XML未提取到有效的许可声明")
        elif declared == "CC-BY" and xml_license.startswith("CC-BY") and license_family(xml_license) != "CC-BY":
            decision = "HOLD"
            reasons.append("manifest与XML许可条件冲突")
        elif declared != "CC-BY":
            reasons.append("manifest许可不属于当前CC BY自动核对范围")
        elif xml_license.startswith("CONFLICT:"):
            decision = "HOLD"
            reasons.append("原文存在多种不一致许可")
        else:
            reasons.append("无法确立原文明确、相符的CC BY版本")
        if manuscript:
            if decision != "HOLD":
                decision = "REVIEW"
            reasons.append("作者稿需额外检查版本和重用权利")
        if retracted:
            decision = "HOLD"
            reasons.append("已撤稿，不能进入默认循证检索库")
        if hash_status == "NOT_CHECKED":
            if decision == "CANDIDATE":
                decision = "REVIEW"
            reasons.append("未执行哈希核验")
        if hash_status == "NOT_REGISTERED" and decision == "CANDIDATE":
            decision = "REVIEW"
            reasons.append("缺少预期SHA256")
        if decision == "CANDIDATE":
            reasons.append("许可初筛通过；仍需核对署名与第三方材料后人工批准")
        d.update(
            disease=r.get("disease", ""), title=r.get("title", ""), version=r.get("version", ""),
            local_path=path_str, source_url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/" if re.fullmatch(r"PMC\d+", pmcid) else "",
            license_raw=r.get("license_code", ""), xml_license=xml_license,
            license_normalized=xml_license if xml_license and xml_license != "UNKNOWN" else declared,
            license_url=url, license_evidence=evidence, decision=decision,
            review_reason="；".join(reasons), is_manuscript=str(manuscript), is_retracted=str(retracted),
            sha256_expected=r.get("xml_sha256", ""), sha256_actual=actual_hash,
            hash_check=hash_status, size_check=size_status, file_exists=exists,
            record_origin="data/raw/pmc/manifest.csv",
        )
        yield d


def cdc_records(root: Path, rows: list[dict[str, str]], do_hash: bool, checked_at: str):
    base = root / "data/raw/cdc"
    for idx, r in enumerate(rows, 1):
        identifier = r.get("media_id") or r.get("topicid") or f"ROW_{idx}"
        d = base_entry("CDC", f"CDC-{identifier}", checked_at)
        path = checked_path(root, base, r.get("path", ""))
        exists, actual, status, _ = integrity(path, r.get("sha256", ""), "", do_hash)
        reason = "原始manifest无文章级许可字段；检查来源页面及是否包含第三方版权内容"
        decision = "REVIEW"
        if exists != "EXISTS":
            reason += "；本地TXT缺失"
        if status == "MISMATCH":
            decision = "HOLD"
            reason += "；SHA256不一致"
        if status == "NOT_CHECKED":
            reason += "；未执行哈希核验"
        d.update(
            disease=r.get("disease", ""), title=r.get("name", ""), local_path=display_path(root, path),
            source_url=r.get("source_url", "") or r.get("content_url", ""),
            license_raw="", license_normalized="UNKNOWN", decision=decision,
            review_reason=reason, sha256_expected=r.get("sha256", ""), sha256_actual=actual,
            hash_check=status, size_check="NOT_REGISTERED", file_exists=exists,
            license_evidence="CDC 官方来源索引（非页面逐项许可确认）", record_origin="data/raw/cdc/manifest.csv",
        )
        yield d


def who_records(root: Path, registry_rows: list[dict[str, str]], do_hash: bool, checked_at: str):
    dir_who = root / "data/raw/who"
    registered = [r for r in registry_rows if r.get("source", "").strip().upper() == "WHO"]
    by_path = {display_path(root, checked_path(root, root, r.get("file", ""))): r for r in registered}
    paths = {p.relative_to(root).as_posix() for p in dir_who.rglob("*.pdf")} if dir_who.is_dir() else set()
    # 联集：不允许因为找不到文件就默默跳过登记记录。
    all_paths = sorted(paths | set(by_path))
    for path_str in all_paths:
        r = by_path.get(path_str)
        path = root / path_str
        d = base_entry("WHO", (r.get("item", "") if r else "") or Path(path_str).stem, checked_at)
        expected = (r or {}).get("sha256", "")
        exists, actual, hash_status, size_status = integrity(path, expected, (r or {}).get("bytes", ""), do_hash)
        raw_license = (r or {}).get("license", "")
        normalized = normalize_license(raw_license)
        # 此处仅继承来源级声明，不声称机器已经读取/识别PDF中的许可条款。
        decision = "RESTRICTED" if "-NC" in normalized or "-SA" in normalized else "REVIEW"
        reasons = ["仅核对来源级登记，需人工逐份确认PDF自身许可和第三方内容"]
        if decision == "RESTRICTED":
            reasons.append("NC/SA条件：不得自动混入可商业再分发的通用语料")
        if r is None:
            reasons.append("PDF没有对应的licenses/registry.csv登记")
        if exists != "EXISTS":
            decision = "HOLD"
            reasons.append("文件不存在")
        if hash_status == "MISMATCH" or size_status == "MISMATCH":
            decision = "HOLD"
            reasons.append("SHA256/字节数与登记不一致")
        if hash_status == "NOT_CHECKED":
            reasons.append("未执行哈希核验")
        d.update(
            local_path=path_str, source_url=(r or {}).get("page", "") or (r or {}).get("url", ""),
            license_raw=raw_license, license_normalized=normalized, decision=decision,
            review_reason="；".join(reasons), sha256_expected=expected, sha256_actual=actual,
            hash_check=hash_status, size_check=size_status, file_exists=exists,
            license_evidence=(r or {}).get("url", ""), record_origin="licenses/registry.csv" if r else "unregistered PDF",
        )
        yield d


def main() -> int:
    parser = argparse.ArgumentParser(description="MedEvidence 文档级许可证据核验")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="MedEvidence项目根目录")
    parser.add_argument("--no-hash", action="store_true", help="调试时跳过哈希，输出不会自动成为CANDIDATE")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    pmc_csv = root / "data/raw/pmc/manifest.csv"
    cdc_csv = root / "data/raw/cdc/manifest.csv"
    reg_csv = root / "licenses/registry.csv"
    try:
        pmc = csv_rows(pmc_csv)
        cdc = csv_rows(cdc_csv)
        sources = csv_rows(reg_csv)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records = []
    print(f"项目根目录: {root}")
    print(f"加载：PMC {len(pmc)} / CDC {len(cdc)} / 来源登记 {len(sources)}")
    for kind, iterator in [
        ("PMC", pmc_records(root, pmc, not args.no_hash, checked_at)),
        ("CDC", cdc_records(root, cdc, not args.no_hash, checked_at)),
        ("WHO", who_records(root, sources, not args.no_hash, checked_at)),
    ]:
        chunk = list(iterator)
        records.extend(chunk)
        print(f"{kind}: {len(chunk)} 条 => {dict(Counter(x['decision'] for x in chunk))}")
    keys = [(d["source"], d["document_id"]) for d in records]
    duplicates = sorted({k for k, n in Counter(keys).items() if n > 1})
    by_status = Counter(x["decision"] for x in records)
    broken = [d for d in records if d["hash_check"] in {"MISMATCH", "MISSING"} or d["size_check"] in {"MISMATCH", "MISSING"}]
    output = root / "licenses/document_license_audit_v2.csv"
    report = root / "reports/license_audit_v2.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)
    lines = [
        "=== MedEvidence License Gate V2 ===", f"生成时间UTC: {checked_at}",
        f"项目根目录: {root}", f"文档数: {len(records)}", f"总体状态: {dict(by_status)}",
        f"来源计数: {dict(Counter(x['source'] for x in records))}",
        f"PMC manifest许可证字样: {dict(Counter(x.get('license_code','') for x in pmc))}",
        f"PMC 作者稿数量: {sum(x.get('is_manuscript','').strip().lower() in {'true','yes','1'} for x in pmc)}",
        f"SHA/大小缺失或不符: {len(broken)}",
        f"重复(source,document_id)数量: {len(duplicates)}",
        "注意：CANDIDATE不是最终合法准入，须人工批准与第三方权利排查。",
        "注意：WHO NC/SA有单独义务，RESTRICTED不得自动进入通用可再分发索引。",
        "注意：CDC页面不得被默认全归为公共领域，需检查第三方材料。",
        "注意：脚本没有修改数据原文及已有licenses/registry.csv。",
    ]
    if duplicates:
        lines.append("重复ID例子: " + repr(duplicates[:20]))
    if broken:
        lines.append("异常样本（前20条）: " + repr([(x['document_id'],x['local_path'],x['hash_check'],x['size_check']) for x in broken[:20]]))
    report.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    print("已生成:")
    print(" -", output)
    print(" -", report)
    print("状态说明：CANDIDATE=可核对的候选，REVIEW=待审，RESTRICTED=附加许可条件，HOLD=暂停使用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
