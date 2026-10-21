#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MedEvidence V1 — PMC Open Access 分疾病下载器（仅使用 Python 标准库）

数据渠道（官方允许的自动化渠道，见 https://pmc-oa-opendata.s3.amazonaws.com/README.txt）：
  * E-utilities eSearch（db=pmc）    —— 按疾病关键词 + 许可过滤找 PMCID
  * PMC Cloud Service（AWS S3 桶）  —— 按 PMCID 列出版本并下载 XML / JSON

流程：
  1. 逐疾病执行 eSearch（疾病词[Title/Abstract] + CC 许可过滤 + open_access）
  2. 对每个 PMCID 列出桶内版本（PMCxxxx.1/、PMCxxxx.2/ ...），读取各版本 JSON 元数据
  3. 选择许可合格的版本（优先"非作者手稿"版本），下载 XML 全文与 JSON 元数据
  4. 结果写入 manifest.csv（许可、撤稿标记、SHA-256、来源疾病等），并保留检索出处

用法示例：
  python -X utf8 download_pmc_oa.py --email you@example.com --per-disease 50
  python -X utf8 download_pmc_oa.py --email you@example.com --per-disease 3 --dry-run
  python -X utf8 download_pmc_oa.py --email you@example.com --diseases cardiovascular,renal

注意：
  * 只收录允许商用的许可（CC0 / CC BY / CC BY-SA）；CC BY-NC、CC BY-ND 等一律跳过。
  * E-utilities 无 key 时约 3 请求/秒（脚本内置限速）；--api-key 可提速到 10 请求/秒。
  * 重复运行自动跳过 manifest 中已有的 PMCID；--force 强制重下。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EUTILS = "https://eutils.ncbi.nlm.nih.gov/eutils/esearch.fcgi"
S3_BASE = "https://pmc-oa-opendata.s3.amazonaws.com"
ALLOWED_LICENSES = {"cc0", "cc by", "cc by sa"}
MANIFEST_FIELDS = [
    "disease", "pmcid", "version", "license_code", "is_manuscript", "is_retracted",
    "title", "xml_bytes", "xml_sha256", "downloaded_at", "xml_path", "json_path",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canon_license(code: str) -> str:
    """'CC BY-SA' -> 'cc by sa'，统一大小写与分隔符后比较。"""
    return re.sub(r"[\s-]+", " ", (code or "").strip().lower())


def build_ssl_context() -> ssl.SSLContext:
    """选择 HTTPS 证书上下文，兼容企业代理/安全软件环境：
    1) truststore（若已安装）：走操作系统信任库；
    2) certifi（若已安装）：公开根证书包；
    3) 系统默认。
    """
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


def http_get(url: str, user_agent: str, timeout: int = 60, retries: int = 3,
             not_found_ok: bool = False):
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not_found_ok:
                return None
            last_exc = exc
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET 失败：{url}（{last_exc!r}）")


class PmcDownloader:
    def __init__(self, email: str, tool: str, api_key: str):
        self.ua = f"MedEvidence/0.1 (research project; {tool}; {email})"
        self.tool = tool
        self.email = email
        self.api_key = api_key
        self.ncbi_sleep = 0.12 if api_key else 0.34
        self.s3_sleep = 0.10

    # ---------- E-utilities ----------
    def esearch(self, term: str, retmax: int) -> dict:
        params = {
            "db": "pmc", "term": term, "retmax": retmax,
            "retmode": "json", "tool": self.tool, "email": self.email,
        }
        if self.api_key:
            params["api_key"] = self.api_key
        raw = http_get(EUTILS + "?" + urllib.parse.urlencode(params), self.ua)
        time.sleep(self.ncbi_sleep)
        return json.loads(raw).get("esearchresult", {})

    # ---------- PMC Cloud Service（AWS） ----------
    def list_versions(self, pmcid: str) -> list[str]:
        url = f"{S3_BASE}/?list-type=2&prefix={pmcid}.&delimiter=/"
        raw = http_get(url, self.ua, not_found_ok=True)
        time.sleep(self.s3_sleep)
        if not raw:
            return []
        root = ET.fromstring(raw)
        versions = set()
        for el in root.iter():
            if (el.tag.endswith("Prefix") and el.text
                    and el.text.startswith(pmcid + ".") and el.text.endswith("/")):
                versions.add(el.text.strip("/"))
        return sorted(versions, key=self._version_number)

    @staticmethod
    def _version_number(name: str) -> int:
        try:
            return int(name.rsplit(".", 1)[1])
        except (IndexError, ValueError):
            return 0

    def fetch_meta(self, vename: str):
        raw = http_get(f"{S3_BASE}/{vename}/{vename}.json", self.ua, not_found_ok=True)
        time.sleep(self.s3_sleep)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def fetch_xml(self, vename: str):
        raw = http_get(f"{S3_BASE}/{vename}/{vename}.xml", self.ua, not_found_ok=True)
        time.sleep(self.s3_sleep)
        return raw


def build_term(terms: list[str]) -> str:
    quoted = [f'"{t.strip()}"[Title/Abstract]' for t in terms if t.strip()]
    disease_expr = "(" + " OR ".join(quoted) + ")"
    license_expr = "(cc0_license[Filter] OR cc_by_license[Filter] OR cc_by-sa_license[Filter])"
    return f"{disease_expr} AND {license_expr} AND open_access[Filter]"


def license_allowed(meta: dict) -> bool:
    return canon_license(meta.get("license_code", "")) in ALLOWED_LICENSES


def is_manuscript(meta: dict) -> bool:
    return str(meta.get("is_manuscript") or "no").strip().lower() == "yes"


def pick_version(metas: list[tuple[str, dict]]):
    """优先：许可合格且非作者手稿；其次：许可合格；都没有则 None。"""
    allowed = [(v, m) for v, m in metas if license_allowed(m)]
    preferred = [(v, m) for v, m in allowed if not is_manuscript(m)]
    chosen = preferred or allowed
    return chosen[-1] if chosen else None


def load_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_manifest(path: Path, rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: (r.get("disease", ""), r.get("pmcid", "")))
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in MANIFEST_FIELDS})


def main() -> int:
    parser = argparse.ArgumentParser(description="MedEvidence V1 — PMC OA 下载器")
    parser.add_argument("--per-disease", type=int, default=50,
                        help="每个疾病域最多取多少篇（默认 50）")
    parser.add_argument("--diseases", default="",
                        help="逗号分隔的疾病域子集（默认配置文件里的全部）")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "configs" / "diseases.json"))
    parser.add_argument("--out", default=str(Path.cwd() / "data" / "raw" / "pmc"))
    parser.add_argument("--email", required=True,
                        help="联系邮箱（NCBI 要求，请填真实邮箱）")
    parser.add_argument("--tool", default="medevidence")
    parser.add_argument("--api-key", default="", help="NCBI API key（可选，提速用）")
    parser.add_argument("--dry-run", action="store_true", help="只检索并打印，不下载")
    parser.add_argument("--force", action="store_true", help="忽略 manifest，重下已有条目")
    args = parser.parse_args()

    out_dir = Path(args.out).resolve()
    articles_dir = out_dir / "articles"
    provenance_dir = out_dir / "provenance"
    articles_dir.mkdir(parents=True, exist_ok=True)
    provenance_dir.mkdir(parents=True, exist_ok=True)

    diseases = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.diseases:
        wanted = [d.strip() for d in args.diseases.split(",") if d.strip()]
        unknown = [d for d in wanted if d not in diseases]
        if unknown:
            print(f"[错误] 配置中没有这些疾病域：{unknown}", file=sys.stderr)
            return 2
        diseases = {k: diseases[k] for k in wanted}

    manifest_path = out_dir / "manifest.csv"
    existing_rows = load_manifest(manifest_path)
    rows = list(existing_rows)
    seen_pmcids = {r.get("pmcid", "") for r in existing_rows}

    dl = PmcDownloader(args.email, args.tool, args.api_key)
    stats = {"new": 0, "dup": 0, "no_license": 0, "failed": 0, "no_version": 0}

    for disease, terms in diseases.items():
        term = build_term(terms)
        print(f"\n=== {disease} ===")
        print(f"query: {term}")
        result = dl.esearch(term, args.per_disease)
        idlist = result.get("idlist", []) or []
        total = result.get("count", "?")
        print(f"命中总数 {total}，本次候选 {len(idlist)} 篇")

        provenance = {
            "disease": disease, "term": term, "total_hits": total,
            "retmax": args.per_disease, "fetched_at": now_iso(), "idlist": idlist,
        }
        (provenance_dir / f"{disease}.esearch.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.dry_run:
            continue

        for num in idlist:
            pmcid = "PMC" + num
            if pmcid in seen_pmcids and not args.force:
                stats["dup"] += 1
                continue
            try:
                versions = dl.list_versions(pmcid)
            except RuntimeError as exc:
                print(f"[fail] {pmcid}: 列版本失败 {exc}")
                stats["failed"] += 1
                continue
            if not versions:
                print(f"[skip] {pmcid}: 桶内无版本（可能已下架）")
                stats["no_version"] += 1
                continue

            metas: list[tuple[str, dict]] = []
            for vename in versions:
                meta = dl.fetch_meta(vename)
                if meta:
                    metas.append((vename, meta))
            chosen = pick_version(metas)
            if chosen is None:
                codes = ", ".join(sorted({(m.get("license_code") or "无") for _, m in metas}))
                print(f"[skip] {pmcid}: 现有版本许可不合格（{codes}）")
                stats["no_license"] += 1
                continue

            vename, meta = chosen
            xml = dl.fetch_xml(vename)
            if xml is None:
                print(f"[fail] {vename}: XML 下载失败")
                stats["failed"] += 1
                continue

            folder = articles_dir / vename
            folder.mkdir(parents=True, exist_ok=True)
            xml_path = folder / f"{vename}.xml"
            json_path = folder / f"{vename}.json"
            xml_path.write_bytes(xml)
            json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

            row = {
                "disease": disease,
                "pmcid": pmcid,
                "version": vename.rsplit(".", 1)[1],
                "license_code": meta.get("license_code", ""),
                "is_manuscript": meta.get("is_manuscript", ""),
                "is_retracted": meta.get("is_retracted", ""),
                "title": (meta.get("title") or "").replace("\n", " ").strip(),
                "xml_bytes": len(xml),
                "xml_sha256": sha256_bytes(xml),
                "downloaded_at": now_iso(),
                "xml_path": str(xml_path.relative_to(out_dir)).replace("\\", "/"),
                "json_path": str(json_path.relative_to(out_dir)).replace("\\", "/"),
            }
            rows = [r for r in rows if r.get("pmcid") != pmcid]  # 重下时替换旧行
            rows.append(row)
            seen_pmcids.add(pmcid)
            stats["new"] += 1
            print(f"[ok] {vename}  license={row['license_code']}  {len(xml)} B")
            if stats["new"] % 25 == 0:
                write_manifest(manifest_path, rows)  # 大批量下载时定期落盘，防中断丢进度

    if not args.dry_run:
        write_manifest(manifest_path, rows)

    print("\n---- 汇总 ----")
    print(f"新下载 {stats['new']} 篇；重复跳过 {stats['dup']}；"
          f"许可不合格 {stats['no_license']}；桶内无版本 {stats['no_version']}；失败 {stats['failed']}")
    if not args.dry_run:
        print(f"manifest: {manifest_path}")
        print(f"文章目录: {articles_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
