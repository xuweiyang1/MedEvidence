#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence 数据完整性校验（迁移到新电脑后运行）

用法：
  cd <项目根目录>
  py -X utf8 scripts\verify_data.py

期望输出全部为 ✅（PMC 936 / WHO 16 / CDC 61 / MeSH 2 / DO 2 / PubMed 931+10）。
任何 ❌ 说明拷贝不完整，请补齐对应目录。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent


def csv_rows(path: Path) -> int:
    if not path.exists():
        return -1
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def count_files(path: Path, pattern: str = "*", dirs_only: bool = False) -> int:
    if not path.exists():
        return -1
    if dirs_only:
        return len([d for d in path.glob(pattern) if d.is_dir()])
    return len([f for f in path.glob(pattern) if f.is_file()])


def main() -> int:
    checks: list[tuple[str, int, int]] = []

    def add(name: str, actual: int, expected: int) -> None:
        checks.append((name, actual, expected))

    add("PMC 文章目录（每篇一个文件夹）",
        count_files(ROOT / "data/raw/pmc/articles", dirs_only=True), 936)
    add("PMC 提取文本（.txt）",
        count_files(ROOT / "data/processed/pmc_text", "*.txt"), 936)
    add("WHO 指南 PDF",
        count_files(ROOT / "data/raw/who", "*.pdf"), 16)
    add("CDC 正文条目（manifest.csv 行数）",
        csv_rows(ROOT / "data/raw/cdc/manifest.csv"), 61)
    add("MeSH 文件",
        count_files(ROOT / "data/raw/mesh"), 2)
    add("Disease Ontology 文件",
        count_files(ROOT / "data/raw/do"), 2)
    add("PubMed 元数据（CSV 行数）",
        csv_rows(ROOT / "data/raw/pubmed/pubmed_meta.csv"), 931)
    add("PubMed XML 批次文件",
        count_files(ROOT / "data/raw/pubmed/xml", "*.xml"), 10)
    add("配置（configs）",
        count_files(ROOT / "configs", "*.json"), 4)
    add("脚本（scripts）",
        count_files(ROOT / "scripts", "*.py"), 10)

    print(f"MedEvidence 数据校验  |  项目根：{ROOT}")
    print(f"Python {sys.version.split()[0]} @ {sys.platform}")
    print("-" * 58)
    all_ok = True
    for name, actual, expected in checks:
        ok = actual >= expected
        all_ok = all_ok and ok
        flag = "✅" if ok else "❌"
        print(f"{flag} {name}: {actual} / 期望 >= {expected}")

    total = 0
    if (ROOT / "data").exists():
        total = sum(f.stat().st_size for f in (ROOT / "data").rglob("*") if f.is_file())
    print("-" * 58)
    print(f"data 目录合计：{total / 1e6:.1f} MB")

    if all_ok:
        print("\n✅ 校验通过，可以继续工作。")
        print("   常用命令：")
        print("     py -X utf8 scripts\\extract_pmc_text.py        # 重建提取文本")
        print("     py -X utf8 scripts\\download_pmc_oa.py --email 你的邮箱 --per-disease 3   # 增量下载")
        print("   下一步工序与说明见 docs\\迁移指南.md")
        return 0
    print("\n❌ 有缺失：如果是完整拷贝，请重拷对应目录；如果是选择性拷贝，请补齐 data/raw。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
