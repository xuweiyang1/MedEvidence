#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V1 — 语料完整性校验（迁移后 / 定期体检）

用法：
  py -X utf8 verify_corpus.py

检查内容：
  1. PMC：文章目录数、manifest 条数、随机抽查 15 篇的 SHA-256
  2. 许可登记（MeSH / DO / WHO / PubMed）：文件存在、大小、SHA-256 全量核对
  3. CDC：manifest 与文本文件一一对应
  4. PubMed：元数据 CSV 行数
输出：逐项通过/失败 + 总结论（有失败时退出码为 1）
"""
from __future__ import annotations

import csv
import hashlib
import random
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
OK = "✅"
BAD = "❌"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    failures = 0

    # ---- 1. PMC ----
    articles_dir = ROOT / "data" / "raw" / "pmc" / "articles"
    dirs = [d for d in articles_dir.glob("*") if d.is_dir()]
    manifest = read_csv(ROOT / "data" / "raw" / "pmc" / "manifest.csv")
    print(f"PMC: 文章目录 {len(dirs)} 篇；manifest {len(manifest)} 条")
    if len(dirs) != len(manifest):
        print(f"  {BAD} 目录数与 manifest 不一致（可能复制不完整）")
        failures += 1
    rng = random.Random(42)
    sample = rng.sample(manifest, min(15, len(manifest)))
    bad = 0
    for row in sample:
        p = ROOT / "data" / "raw" / "pmc" / row["xml_path"]
        if not p.exists():
            print(f"  缺失: {row['xml_path']}")
            bad += 1
            continue
        if sha256_file(p) != row.get("xml_sha256", ""):
            print(f"  哈希不符: {row['xml_path']}")
            bad += 1
    print(f"  PMC 抽查 {len(sample)} 篇: " + ("通过 " + OK if bad == 0 else f"{bad} 个问题 {BAD}"))
    failures += bad

    # ---- 2. 许可登记（MeSH / DO / WHO / PubMed 等）----
    reg = read_csv(ROOT / "licenses" / "registry.csv")
    reg_bad = 0
    for row in reg:
        f = ROOT / row["file"]
        if not f.exists():
            print(f"  缺失: {row['file']}（{row.get('source')} / {row.get('item')}）")
            reg_bad += 1
            continue
        if str(row.get("bytes", "")).strip() and int(row["bytes"]) != f.stat().st_size:
            print(f"  大小不符: {row['file']}")
            reg_bad += 1
            continue
        if row.get("sha256") and sha256_file(f) != row["sha256"]:
            print(f"  哈希不符: {row['file']}")
            reg_bad += 1
    print(f"许可登记: {len(reg)} 个文件: " + ("全部通过 " + OK if reg_bad == 0 else f"{reg_bad} 个问题 {BAD}"))
    failures += reg_bad

    # ---- 3. CDC ----
    cdc = read_csv(ROOT / "data" / "raw" / "cdc" / "manifest.csv")
    cdc_bad = sum(1 for r in cdc if not (ROOT / r["path"]).exists())
    print(f"CDC: manifest {len(cdc)} 条，缺失 {cdc_bad} " + (OK if cdc_bad == 0 else BAD))
    failures += cdc_bad

    # ---- 4. PubMed ----
    pm_path = ROOT / "data" / "raw" / "pubmed" / "pubmed_meta.csv"
    if pm_path.exists():
        pm = read_csv(pm_path)
        print(f"PubMed 元数据: {len(pm)} 条 " + OK)
    else:
        print(f"PubMed 元数据: 未发现 {BAD}")
        failures += 1

    print()
    if failures == 0:
        print(f"结论: 全部通过 {OK}（本机语料完整，可直接继续）")
        return 0
    print(f"结论: 发现 {failures} 个问题 {BAD}（建议重新复制缺失部分）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
