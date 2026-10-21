#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MedEvidence V1 — PMC JATS XML → 纯文本提取（仅使用 Python 标准库）

用法：
  python -X utf8 extract_pmc_text.py
  python -X utf8 extract_pmc_text.py --raw data/raw/pmc --out data/processed/pmc_text

输出：
  <out>/<vename>.txt          每篇一个文本文件（标题 + 摘要 + 正文；跳过参考文献/图表）
  <out>/all_articles.jsonl    合并版（含 pmcid / 疾病 / 许可 / 正文字符数），供切分建库
  控制台摘要：字符数分布 + 疑似提取失败（低于 --min-chars）的篇目列表
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 提取时跳过的块级元素（版本库不需要，也避免把参考文献排进正文）
SKIP_TAGS = {
    "ref-list", "table-wrap", "fig", "supplementary-material",
    "disp-formula", "inline-formula", "table", "graphic",
}


def strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def text_of(elem) -> str:
    return " ".join("".join(elem.itertext()).split())


def walk(elem, out: list[str]) -> None:
    for child in list(elem):
        tag = strip_ns(child.tag)
        if tag in SKIP_TAGS:
            continue
        if tag == "sec":
            title = next((text_of(sub) for sub in child
                          if strip_ns(sub.tag) == "title"), "")
            if title:
                out.append(f"\n## {title}")
            walk(child, out)
        elif tag == "title":
            continue
        elif tag == "p":
            txt = text_of(child)
            if txt:
                out.append(txt)
        else:
            walk(child, out)


def extract_article(path: Path) -> tuple[str, str]:
    root = ET.parse(path).getroot()

    title = ""
    for el in root.iter():
        if strip_ns(el.tag) == "article-title":
            title = text_of(el)
            break

    parts: list[str] = []
    if title:
        parts.append(f"# {title}")

    for el in root.iter():
        if strip_ns(el.tag) == "abstract":
            abs_parts: list[str] = []
            walk(el, abs_parts)
            if abs_parts:
                parts.append("\n## Abstract")
                parts.extend(abs_parts)
            break

    for el in root.iter():
        if strip_ns(el.tag) == "body":
            walk(el, parts)
            break

    text = "\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text


def main() -> int:
    parser = argparse.ArgumentParser(description="PMC JATS XML → 纯文本")
    parser.add_argument("--raw", default=str(Path.cwd() / "data" / "raw" / "pmc"))
    parser.add_argument("--out", default=str(Path.cwd() / "data" / "processed" / "pmc_text"))
    parser.add_argument("--min-chars", type=int, default=1500,
                        help="低于该字符数视为疑似提取失败（默认 1500）")
    args = parser.parse_args()

    raw_dir = Path(args.raw).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, dict] = {}
    manifest_path = raw_dir / "manifest.csv"
    if manifest_path.exists():
        with manifest_path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                manifest[row.get("xml_path", "")] = row

    xml_files = sorted((raw_dir / "articles").glob("*/*.xml"))
    if not xml_files:
        print(f"未找到 XML：{raw_dir / 'articles'}")
        print("请先运行 download_pmc_oa.py")
        return 1

    lengths: list[int] = []
    warnings: list[tuple[str, str]] = []
    jsonl_path = out_dir / "all_articles.jsonl"

    with jsonl_path.open("w", encoding="utf-8") as jf:
        for xml_path in xml_files:
            vename = xml_path.stem
            try:
                title, text = extract_article(xml_path)
            except ET.ParseError as exc:
                warnings.append((vename, f"XML 解析失败：{exc}"))
                continue

            (out_dir / f"{vename}.txt").write_text(text, encoding="utf-8")

            rel = f"articles/{vename}/{vename}.xml"
            meta = manifest.get(rel, {})
            record = {
                "vename": vename,
                "pmcid": meta.get("pmcid", ""),
                "disease": meta.get("disease", ""),
                "license_code": meta.get("license_code", ""),
                "is_retracted": meta.get("is_retracted", ""),
                "title": title,
                "chars": len(text),
                "text": text,
            }
            jf.write(json.dumps(record, ensure_ascii=False) + "\n")

            lengths.append(len(text))
            if len(text) < args.min_chars:
                warnings.append((vename, f"字符数偏少（{len(text)}）"))

    print(f"处理 {len(lengths)} 篇；字符数 min / avg / max = "
          f"{min(lengths)} / {sum(lengths) // len(lengths)} / {max(lengths)}")
    print(f"文本输出: {out_dir}")
    print(f"合并 JSONL: {jsonl_path}")
    if warnings:
        print(f"\n需要人工检查 {len(warnings)} 篇：")
        for vename, why in warnings:
            print(f"  - {vename}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
