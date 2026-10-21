#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V1 — WHO IRIS 检索 + 文件直链解析（仅标准库）

用法：
  py -X utf8 who_iris.py --query "pharmacological treatment of hypertension in adults" --size 5 --top 2 --links
  py -X utf8 who_iris.py --query "pneumonia children" --links

输出：
  - 每个查询的候选条目：标题 / handle / uuid
  - --links：每个条目的 ORIGINAL 文件下载直链（可用于 wget/curl 直接下载）
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SEARCH = "https://iris.who.int/server/api/discover/search/objects"
CORE = "https://iris.who.int/server/api/core"
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
    with urllib.request.urlopen(req, timeout=90, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read())


def search(query: str, size: int):
    url = f"{SEARCH}?query={urllib.parse.quote(query)}&size={size}&dsoType=item"
    data = api_get(url)
    sr = ((data.get("_embedded") or {}).get("searchResult") or {})
    objs = (sr.get("_embedded") or {}).get("objects") or []
    items = []
    for obj in objs:
        io = (obj.get("_embedded") or {}).get("indexableObject") or {}
        items.append({
            "uuid": io.get("uuid", ""),
            "handle": io.get("handle", ""),
            "name": io.get("name", ""),
        })
    total = (sr.get("page") or {}).get("totalElements")
    return total, items


def bitstream_links(item_uuid: str):
    links = []
    bundles = api_get(f"{CORE}/items/{item_uuid}/bundles")
    for bundle in (bundles.get("_embedded") or {}).get("bundles") or []:
        if bundle.get("name") != "ORIGINAL":
            continue
        bs = api_get(f"{CORE}/bundles/{bundle['uuid']}/bitstreams")
        for x in (bs.get("_embedded") or {}).get("bitstreams") or []:
            links.append({
                "name": x.get("name", ""),
                "size": x.get("sizeBytes", 0),
                "content": f"{CORE}/bitstreams/{x['uuid']}/content",
                "download": f"https://iris.who.int/bitstreams/{x['uuid']}/download",
            })
    return links


def main() -> int:
    ap = argparse.ArgumentParser(description="WHO IRIS 检索与文件链接解析")
    ap.add_argument("--query", action="append", required=True, help="检索词（可重复）")
    ap.add_argument("--size", type=int, default=5)
    ap.add_argument("--top", type=int, default=2, help="--links 时解析前 N 个条目")
    ap.add_argument("--links", action="store_true", help="解析并打印文件下载直链")
    args = ap.parse_args()

    for query in args.query:
        print(f"\n=== 查询：{query}")
        try:
            total, items = search(query, args.size)
        except Exception as exc:  # noqa: BLE001
            print(f"  检索失败：{exc}")
            continue
        print(f"命中约 {total} 条，展示前 {len(items)} 条：")
        for idx, it in enumerate(items):
            print(f"  [{idx}] {it['name']}")
            print(f"      https://iris.who.int/handle/{it['handle']}  (uuid={it['uuid']})")
        if args.links:
            for it in items[:args.top]:
                print(f"  -- 文件链接：{it['name'][:70]}")
                try:
                    links = bitstream_links(it["uuid"])
                except Exception as exc:  # noqa: BLE001
                    print(f"     解析失败：{exc}")
                    continue
                if not links:
                    print("     （无 ORIGINAL 文件）")
                for lk in links:
                    print(f"     [{lk['size']:>10,} B] {lk['name']}")
                    print(f"        {lk['content']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
