#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V1 — CDC 指定页面补充抓取（少量人工核验过的链接）

背景：CDC syndication API 在"肾脏病"等主题下可用正文很少（仅 1 篇），
因此对少量人工核验（HTTP 200）的 cdc.gov 页面做定向补充，抽取纯文本。
CDC 内容为美国政府公有领域作品，使用时注明来源。

注意：cdc.gov 对 Python 默认 UA 返回 403；本脚本使用浏览器头，
并在仍被拦时退回系统自带的 curl.exe 获取。

用法：
  py -X utf8 download_cdc_pages.py

输入：configs/cdc_direct_pages.json（disease + url 列表）
输出：
  data/raw/cdc/<疾病域>/direct_<slug>.txt
  data/raw/cdc/manifest.csv 追加行（topicid 标记为 direct）
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG = PROJECT_ROOT / "configs" / "cdc_direct_pages.json"
OUT_DIR = PROJECT_ROOT / "data" / "raw" / "cdc"
MANIFEST = OUT_DIR / "manifest.csv"
MANIFEST_FIELDS = ["disease", "topicid", "media_id", "name", "source_url",
                   "content_url", "txt_chars", "sha256", "retrieved_at", "path"]
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


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


def fetch(url: str) -> str:
    """优先 urllib（浏览器头）；被 403 拒绝时退回系统自带的 curl.exe。"""
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=90, context=SSL_CONTEXT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise
        print("  （403 被拦，改用 curl.exe 重试）")
        proc = subprocess.run(["curl.exe", "-sL", "--max-time", "90", url],
                              capture_output=True)
        if proc.returncode != 0 or len(proc.stdout) < 200:
            raise RuntimeError(f"curl 回退失败（exit={proc.returncode}）") from exc
        return proc.stdout.decode("utf-8", errors="replace")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    pages = json.loads(CONFIG.read_text(encoding="utf-8"))

    rows: list[dict] = []
    if MANIFEST.exists():
        with MANIFEST.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    seen_sources = {r.get("source_url", "") for r in rows}

    added = 0
    for page in pages:
        url = page["url"]
        disease = page["disease"]
        if url in seen_sources:
            print(f"[skip] 已存在：{url}")
            continue
        try:
            html = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"[fail] {url}: {exc}")
            continue
        text = html_to_text(html)
        if len(text) < 200:
            print(f"[skip] 正文过短（{len(text)} 字符）：{url}")
            continue

        path = urllib.parse.urlparse(url).path
        segs = [s for s in path.split("/") if s and s != "index.html"]
        slug = re.sub(r"[^a-z0-9]+", "_", "_".join(segs).lower()).strip("_") or "page"
        media_id = f"direct_{slug}"
        dest = OUT_DIR / disease / f"{media_id}.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")

        rows.append({
            "disease": disease, "topicid": "direct", "media_id": media_id,
            "name": slug.replace("_", " "),
            "source_url": url, "content_url": url,
            "txt_chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "retrieved_at": now_iso(),
            "path": str(dest.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        })
        seen_sources.add(url)
        added += 1
        print(f"[ok] {disease}  {media_id}  ({len(text)} 字符)")

    rows.sort(key=lambda r: (r.get("disease", ""), r.get("media_id", "")))
    with MANIFEST.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in MANIFEST_FIELDS})

    print(f"\n新增 {added} 篇；manifest 共 {len(rows)} 条：{MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
