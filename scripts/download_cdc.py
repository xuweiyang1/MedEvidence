#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V1 — CDC 正文批量下载（按 configs/cdc_topics.json 的 topicid）

说明：CDC media 接口不支持自由文本搜索，只能按 topicid 过滤；
每条目的 contentUrl 是正文 HTML，本脚本抽取纯文本后保存。

用法：
  py -X utf8 download_cdc.py
  py -X utf8 download_cdc.py --per-disease 8

输出：
  data/raw/cdc/<疾病域>/<mediaid>_<slug>.txt
  data/raw/cdc/manifest.csv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG = PROJECT_ROOT / "configs" / "cdc_topics.json"
OUT_DIR = PROJECT_ROOT / "data" / "raw" / "cdc"
MANIFEST = OUT_DIR / "manifest.csv"
MEDIA_URL = "https://tools.cdc.gov/api/v2/resources/media"
UA = "MedEvidence/0.1 (research project; CDC syndication)"
MANIFEST_FIELDS = ["disease", "topicid", "media_id", "name", "source_url",
                   "content_url", "txt_chars", "sha256", "retrieved_at", "path"]


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


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript"}
    BREAK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr", "section", "article"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip() + " ")


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get_bytes(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
        return resp.read()


def api_get(url: str):
    return json.loads(http_get_bytes(url))


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    with MANIFEST.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_manifest(rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: (r.get("disease", ""), r.get("media_id", "")))
    with MANIFEST.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in MANIFEST_FIELDS})


def main() -> int:
    ap = argparse.ArgumentParser(description="CDC 正文批量下载")
    ap.add_argument("--per-disease", type=int, default=10, help="每个疾病域最多保存几篇（默认 10）")
    args = ap.parse_args()

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rows = load_manifest()
    seen = {(r.get("disease", ""), r.get("media_id", "")) for r in rows}
    stats: dict[str, int] = {}

    for disease, topic_ids in config.items():
        saved = 0
        seen_sources: set[str] = set()
        print(f"\n=== {disease} ===")
        for tid in topic_ids:
            if saved >= args.per_disease:
                break
            try:
                data = api_get(f"{MEDIA_URL}?topicid={tid}&max=100")
            except Exception as exc:  # noqa: BLE001
                print(f"  [fail] topicid={tid}: {exc}")
                continue
            for item in data.get("results") or []:
                if saved >= args.per_disease:
                    break
                media_id = str(item.get("id"))
                if (disease, media_id) in seen:
                    continue
                lang = (item.get("language") or {}).get("isoCode", "")
                if lang and lang != "eng":
                    continue
                if (item.get("mediaType") or "") != "HTML":
                    continue
                content_url = item.get("contentUrl") or ""
                source_url = item.get("sourceUrl") or ""
                if not content_url or source_url in seen_sources:
                    continue
                try:
                    html = http_get_bytes(content_url).decode("utf-8", errors="replace")
                except Exception as exc:  # noqa: BLE001
                    print(f"  [fail] {media_id}: {exc}")
                    continue
                text = html_to_text(html)
                if len(text) < 200:
                    continue
                seen_sources.add(source_url)
                name = (item.get("name") or "").strip()
                slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:40] or "page"
                dest = OUT_DIR / disease / f"{media_id}_{slug}.txt"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8")
                rows.append({
                    "disease": disease, "topicid": tid, "media_id": media_id,
                    "name": name, "source_url": source_url, "content_url": content_url,
                    "txt_chars": len(text),
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "retrieved_at": now_iso(),
                    "path": str(dest.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                })
                seen.add((disease, media_id))
                saved += 1
                print(f"  [ok] {media_id}  {name[:60]}  ({len(text)} 字符)")

        stats[disease] = saved

    write_manifest(rows)
    print("\n---- 汇总 ----")
    for disease, count in stats.items():
        print(f"{disease}: {count} 篇")
    print(f"manifest: {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
