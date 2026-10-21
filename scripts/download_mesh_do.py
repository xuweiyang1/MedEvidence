#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MedEvidence V1 — MeSH + Disease Ontology 最小下载（仅使用 Python 标准库）

下载内容（合计约 25 MB 传输 / 约 320 MB 落盘）：
  MeSH  desc2026  描述符（zip 约 16.8 MB，解开后 XML 约 313 MB）
  MeSH  qual2026  限定词（约 0.29 MB）
  DO    doid.obo  疾病本体（约 7.3 MB；PURL 会跳转到 GitHub raw，受限网络下自动换备用源）
  DO    doid.owl  疾病本体（OWL 格式）

用法：
  py -X utf8 download_mesh_do.py
  py -X utf8 download_mesh_do.py --only doid.obo,doid.owl

提示：若你所在网络无法直连 GitHub raw，先设置本机代理再运行，例如 PowerShell：
  $env:HTTPS_PROXY="http://127.0.0.1:7897"; $env:HTTP_PROXY="http://127.0.0.1:7897"

输出：
  data/raw/mesh/desc2026.xml、qual2026.xml
  data/raw/do/doid.obo、doid.owl
  licenses/registry.csv  来源级许可登记（URL、SHA-256、大小、许可、时间）
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import ssl
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
REGISTRY = PROJECT_ROOT / "licenses" / "registry.csv"
UA = "MedEvidence/0.1 (research project; data bootstrap)"

MESH_BASE = "https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh"
DO_GH = "DiseaseOntology/HumanDiseaseOntology"

ITEMS = [
    {
        "source": "MeSH", "item": "desc2026",
        "candidates": [f"{MESH_BASE}/desc2026.zip", f"{MESH_BASE}/desc2026.gz",
                       f"{MESH_BASE}/desc2026.xml"],
        "dest": "mesh/desc2026.xml",
        "license": "NLM Terms and Conditions（可自由使用/再利用，须致谢 NLM）",
        "page": "https://www.nlm.nih.gov/databases/download/mesh.html",
    },
    {
        "source": "MeSH", "item": "qual2026",
        "candidates": [f"{MESH_BASE}/qual2026.xml"],
        "dest": "mesh/qual2026.xml",
        "license": "NLM Terms and Conditions（可自由使用/再利用，须致谢 NLM）",
        "page": "https://www.nlm.nih.gov/databases/download/mesh.html",
    },
    {
        "source": "Disease Ontology", "item": "doid.obo",
        "candidates": [
            "http://purl.obolibrary.org/obo/doid.obo",
            f"https://fastly.jsdelivr.net/gh/{DO_GH}@main/src/ontology/doid.obo",
            f"https://cdn.jsdelivr.net/gh/{DO_GH}@main/src/ontology/doid.obo",
            f"https://github.com/{DO_GH}/raw/main/src/ontology/doid.obo",
        ],
        "dest": "do/doid.obo",
        "license": "CC0 1.0",
        "page": "https://disease-ontology.org",
    },
    {
        "source": "Disease Ontology", "item": "doid.owl",
        "candidates": [
            "http://purl.obolibrary.org/obo/doid.owl",
            f"https://fastly.jsdelivr.net/gh/{DO_GH}@main/src/ontology/doid.owl",
            f"https://cdn.jsdelivr.net/gh/{DO_GH}@main/src/ontology/doid.owl",
            f"https://github.com/{DO_GH}/raw/main/src/ontology/doid.owl",
        ],
        "dest": "do/doid.owl",
        "license": "CC0 1.0",
        "page": "https://disease-ontology.org",
    },
]

REGISTRY_FIELDS = ["source", "item", "file", "url", "sha256", "bytes",
                   "license", "page", "retrieved_at"]


def build_ssl_context() -> ssl.SSLContext:
    """选择 HTTPS 证书上下文（truststore → certifi → 系统默认）。"""
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


def download(url: str, dest: Path, timeout: int = 900) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp, dest.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  下载中 {done / 1e6:,.1f} / {total / 1e6:,.1f} MB", end="",
                      flush=True)
    print()
    return url


def fetch_first_ok(candidates: list[str], dest: Path) -> str | None:
    for url in candidates:
        try:
            print(f"  尝试 {url}")
            download(url, dest)
            return url
        except Exception as exc:  # noqa: BLE001
            print(f"    失败：{exc}")
            continue
    return None


def postprocess(dest: Path, original_url: str) -> None:
    """若下到的是压缩包，就地解压出 XML。"""
    raw = dest.read_bytes()
    if original_url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml_name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
            if xml_name:
                dest.write_bytes(zf.read(xml_name))
    elif original_url.endswith(".gz"):
        dest.write_bytes(gzip.decompress(raw))


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
    parser = argparse.ArgumentParser(description="MeSH + Disease Ontology 下载")
    parser.add_argument("--only", default="",
                        help="仅下载指定条目（逗号分隔，如 doid.obo,doid.owl）")
    args = parser.parse_args()

    items = ITEMS
    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        items = [it for it in ITEMS if it["item"] in wanted]
        if not items:
            print(f"--only 没有匹配到条目：{wanted}")
            return 2

    for item in items:
        dest = RAW_DIR / item["dest"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"\n=== {item['source']} / {item['item']} ===")
        url = fetch_first_ok(item["candidates"], dest)
        if not url:
            print("  [失败] 所有候选 URL 均不可用，跳过")
            continue
        postprocess(dest, url)
        size = dest.stat().st_size
        digest = sha256_file(dest)
        print(f"  完成 {dest.relative_to(PROJECT_ROOT)}  {size:,} B  sha256={digest[:12]}…")
        upsert_registry({
            "source": item["source"], "item": item["item"],
            "file": str(dest.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "url": url, "sha256": digest, "bytes": size,
            "license": item["license"], "page": item["page"],
            "retrieved_at": now_iso(),
        })

    print(f"\n许可登记表：{REGISTRY.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
