#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V1 — WHO 指南 PDF 批量下载（列表见 configs/who_items.json）

用法：
  py -X utf8 download_who.py
  py -X utf8 download_who.py --only 10665/344424,10665/331710

输出：
  data/raw/who/<文件名>.pdf
  licenses/registry.csv 追加 WHO 条目（item = handle）
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG = PROJECT_ROOT / "configs" / "who_items.json"
OUT_DIR = PROJECT_ROOT / "data" / "raw" / "who"
REGISTRY = PROJECT_ROOT / "licenses" / "registry.csv"
UA = "MedEvidence/0.1 (research project; WHO IRIS)"
REGISTRY_FIELDS = ["source", "item", "file", "url", "sha256", "bytes",
                   "license", "page", "retrieved_at"]


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


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def download(url: str, dest: Path, timeout: int = 600) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp, \
            dest.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  下载中 {done / 1e6:,.2f} / {total / 1e6:,.2f} MB",
                      end="", flush=True)
    print()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def upsert_registry(row: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if REGISTRY.exists():
        with REGISTRY.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    rows = [r for r in rows
            if not (r.get("source") == row["source"] and r.get("item") == row["item"])]
    rows.append(row)
    rows.sort(key=lambda r: (r.get("source", ""), r.get("item", "")))
    with REGISTRY.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REGISTRY_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in REGISTRY_FIELDS})


def main() -> int:
    ap = argparse.ArgumentParser(description="WHO 指南 PDF 批量下载")
    ap.add_argument("--only", default="", help="仅下载指定 handle（逗号分隔）")
    args = ap.parse_args()

    items = json.loads(CONFIG.read_text(encoding="utf-8"))
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        items = [it for it in items if it["handle"] in wanted]
        if not items:
            print(f"--only 没有匹配到条目：{wanted}")
            return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for it in items:
        dest = OUT_DIR / it["file"]
        expected = it.get("bytes")
        exp_txt = f"{expected:,}" if isinstance(expected, int) else "?"
        print(f"\n=== {it['name']} ({it['handle']})")
        try:
            download(it["url"], dest)
        except Exception as exc:  # noqa: BLE001
            print(f"  [失败] {exc}")
            continue
        size = dest.stat().st_size
        digest = sha256_file(dest)
        total += size
        flag = "" if not isinstance(expected, int) or size == expected else "  ⚠ 大小与预期不符"
        print(f"  完成 {it['file']}  {size:,} B（预期 {exp_txt} B）{flag}")
        upsert_registry({
            "source": "WHO", "item": it["handle"],
            "file": str(dest.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "url": it["url"], "sha256": digest, "bytes": size,
            "license": it["license"],
            "page": f"https://iris.who.int/handle/{it['handle']}",
            "retrieved_at": now_iso(),
        })

    print(f"\n合计 {total / 1e6:.1f} MB；登记表：{REGISTRY.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
