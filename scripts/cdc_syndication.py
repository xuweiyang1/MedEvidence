#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V1 — CDC Content Syndication 工具（仅标准库）

用途：
  1) 从 CDC 主题树里按关键词/正则找话题 id（topicid）
  2) 按 topicid 列出可下载的正文条目（contentUrl 即正文 HTML 文本）

注意：CDC 的 media 接口不支持自由文本搜索（q= 无效），必须用 topicid 过滤。

用法：
  py -X utf8 cdc_syndication.py --match "blood pressure|hypertension"
  py -X utf8 cdc_syndication.py --topicid 5289 --max 20
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TOPICS = "https://tools.cdc.gov/api/v2/resources/topics"
MEDIA = "https://tools.cdc.gov/api/v2/resources/media"
UA = "MedEvidence/0.1 (research project)"


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


def api_get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read())


def walk_topics(nodes, pattern: str, out: list) -> None:
    for node in nodes or []:
        name = node.get("name") or ""
        if re.search(pattern, name, re.IGNORECASE):
            out.append({"id": node.get("id"), "name": name,
                        "count": node.get("mediaUsageCount")})
        walk_topics(node.get("items") or [], pattern, out)


def main() -> int:
    ap = argparse.ArgumentParser(description="CDC Content Syndication 工具")
    ap.add_argument("--match", default="",
                    help="在主题名中搜索的关键词/正则（不区分大小写）")
    ap.add_argument("--topicid", type=int, default=0)
    ap.add_argument("--max", type=int, default=30)
    args = ap.parse_args()

    topic_ids: list[int] = []
    if args.match:
        data = api_get(TOPICS)
        found: list[dict] = []
        walk_topics(data.get("results") or [], args.match, found)
        if not found:
            print("没有匹配的主题。")
            return 1
        for f in found:
            print(f"[topic] id={f['id']}  {f['name']}  (media={f['count']})")
        topic_ids = [f["id"] for f in found]

    if args.topicid:
        topic_ids = [args.topicid]

    if topic_ids:
        tid = topic_ids[0]
        data = api_get(f"{MEDIA}?topicid={tid}&max={args.max}")
        pagination = (data.get("meta") or {}).get("pagination") or {}
        total = pagination.get("total")
        print(f"\n=== topicid={tid} 共 {total} 条，展示 {len(data.get('results') or [])} 条：")
        for item in data.get("results") or []:
            print(f"[{item.get('id')}] {item.get('name')}")
            print(f"    正文: {item.get('contentUrl')}")
            print(f"    原页: {item.get('sourceUrl')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
