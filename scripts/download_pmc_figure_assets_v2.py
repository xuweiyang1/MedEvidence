#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Optional MedEvidence figure download step; reads verified PMC media sidecar.

Python 3.11+ standard library. Only retrieves image files linked by original JATS
AND listed in per-article official PMC AWS JSON metadata media_urls.

Commands:
    python scripts/download_pmc_figure_assets_v2.py --limit 10
    python scripts/download_pmc_figure_assets_v2.py

Local-only research. No image interpretation and no third-party copyright approval.
Downloaded files go to data/processed/media/assets/, not original raw folder.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs
from urllib.request import Request, urlopen

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.tif', '.tiff', '.svg', '.webp', '.bmp'}
AWS_HOST = 'pmc-oa-opendata.s3.amazonaws.com'
VERSION = re.compile(r'^PMC\d+\.\d+$', re.I)
PMCID = re.compile(r'^PMC\d+$', re.I)
MD5_RE = re.compile(r'^[0-9a-f]{32}$', re.I)
REPORT_FIELDS = ['document_id', 'article_version', 'file_name', 'status', 'bytes', 'md5_verified', 'local_path', 'source_url', 'error']


def validate_url(url, version, filename):
    p = urlparse(url)
    if p.scheme != 'https' or p.hostname != AWS_HOST or p.username or p.password:
        return False
    path_prefix = '/' + version + '/'
    if not p.path.startswith(path_prefix):
        return False
    rest = unquote(p.path[len(path_prefix):])
    return rest == filename and '/' not in rest and '\\' not in rest


def digest_md5(filepath):
    h = hashlib.md5()
    with filepath.open('rb') as f:
        for data in iter(lambda: f.read(1024*1024), b''):
            h.update(data)
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--limit', type=int, default=0, help='最多下载/核验多少个媒体文件(非文章数)')
    p.add_argument('--max-mb', type=int, default=30, help='单图片文件体积上限，默认30MB')
    args = p.parse_args()
    if args.limit < 0 or args.max_mb <= 0:
        p.error('--limit>=0, --max-mb>0')
    root = args.root.expanduser().resolve()
    records = root / 'data/processed/media/pmc_media_v2.jsonl'
    if not records.is_file():
        print('[ERROR] 先运行 recover_pmc_tables_figures_v2.py 生成完整图表清单:', records, file=sys.stderr)
        return 2
    targets, seen = [], set()
    with records.open('r', encoding='utf-8') as fin:
        for line in fin:
            if not line.strip():
                continue
            article = json.loads(line)
            did, ver = article.get('document_id', ''), article.get('article_version', '')
            if not PMCID.fullmatch(did) or not VERSION.fullmatch(ver) or ver.split('.')[0] != did:
                print('[ERROR] 文章PMCID或版本不合法:', did, ver, file=sys.stderr)
                return 2
            if article.get('rights', {}).get('audit_status') != 'CANDIDATE':
                print('[ERROR] 非候选文献，不下载:', did, file=sys.stderr)
                return 2
            for asset in article.get('image_references', []):
                u = asset.get('media_url', '')
                f = asset.get('file_name', '')
                if not u or not f or Path(f).suffix.lower() not in IMAGE_EXTS:
                    continue
                if f in {'.', '..'} or '/' in f or '\\' in f or not validate_url(u, ver, f):
                    continue
                # Do not download a second copy if the original folder already has this graphic.
                if asset.get('status') == 'LOCAL_FILE_FOUND':
                    relative = str(asset.get('local_path') or '').replace('\\', '/')
                    if relative and not relative.startswith('/') and not re.match(r'^[A-Za-z]:/', relative):
                        existing = (root / relative).resolve()
                        if existing.is_relative_to(root) and existing.is_file():
                            continue
                key = (ver, f)
                if key not in seen:
                    targets.append((did, ver, f, u))
                    seen.add(key)
    if args.limit:
        targets = targets[:args.limit]
    folder = root / 'data/processed/media/assets'
    report_folder = root / 'reports'
    report_folder.mkdir(parents=True, exist_ok=True)
    suffix = '_smoke' if args.limit else ''
    report = report_folder / f'pmc_image_download_report_v2{suffix}.csv'
    summary = report_folder / f'pmc_image_download_summary_v2{suffix}.txt'
    totals = Counter()
    rows = []
    max_bytes = args.max_mb * 1024 * 1024
    for n, (did, ver, filename, url) in enumerate(targets, 1):
        dst_dir = folder / ver
        dst_dir.mkdir(parents=True, exist_ok=True)
        target = dst_dir / filename
        md5_values = parse_qs(urlparse(url).query).get('md5', [])
        expected_md5 = md5_values[0].lower() if len(md5_values) == 1 and MD5_RE.fullmatch(md5_values[0]) else ''
        entry = {'document_id': did, 'article_version': ver, 'file_name': filename,
                 'status': 'ERROR', 'bytes': '', 'md5_verified': '',
                 'local_path': target.relative_to(root).as_posix(), 'source_url': url, 'error': ''}
        try:
            if target.is_file() and (not expected_md5 or digest_md5(target) == expected_md5):
                entry.update(status='ALREADY_PRESENT', bytes=target.stat().st_size,
                             md5_verified='TRUE' if expected_md5 else 'NOT_PROVIDED')
            else:
                req = Request(url, headers={'User-Agent': 'MedEvidence-Research/1.0'})
                temporary = target.with_name(target.name + '.download')
                try:
                    with urlopen(req, timeout=50) as response:
                        if not validate_url(response.geturl(), ver, filename):
                            raise ValueError('发现不受信任的URL跳转')
                        length = response.headers.get('Content-Length', '')
                        if length.isdigit() and int(length) > max_bytes:
                            raise ValueError('单文件超过体积上限')
                        h, total = hashlib.md5(), 0
                        with temporary.open('wb') as f:
                            while data := response.read(1024 * 1024):
                                total += len(data)
                                if total > max_bytes:
                                    raise ValueError('单文件超过体积上限')
                                h.update(data)
                                f.write(data)
                    if expected_md5 and h.hexdigest().lower() != expected_md5:
                        raise ValueError('下载文件MD5与PMC元数据不一致')
                    os.replace(temporary, target)
                    entry.update(status='DOWNLOADED', bytes=total,
                                 md5_verified='TRUE' if expected_md5 else 'NOT_PROVIDED')
                finally:
                    if temporary.exists():
                        temporary.unlink()
                time.sleep(0.1)
        except (OSError, ValueError, TimeoutError) as e:
            entry['error'] = f'{type(e).__name__}: {e}'
        totals[entry['status']] += 1
        rows.append(entry)
        if n % 20 == 0 or n == len(targets):
            print(f'进度 {n}/{len(targets)}; 成功下载 {totals["DOWNLOADED"]}; 失败 {totals["ERROR"]}')
    with report.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        w.writeheader(); w.writerows(rows)
    lines = [
        '=== MedEvidence PMC 图像文件增量下载 ===',
        f'UTC: {datetime.now(timezone.utc).isoformat(timespec="seconds")}',
        f'模式: {"SMOKE" if args.limit else "FULL"}',
        f'具备官方PMC媒体URL的目标图片: {len(targets)}',
        f'已下载: {totals["DOWNLOADED"]}', f'已存在: {totals["ALREADY_PRESENT"]}',
        f'失败: {totals["ERROR"]}',
        '仅下载JATS图形引用且PMC元数据media_urls明确列出的官方AWS图片。',
        '无实际图片URL的图注不会推断下载地址；网络失败不会修改已有原始论文。',
        '下载文件不代表已完成第三方版权审核；不自动解释图形或读取曲线数值。',
    ]
    summary.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
    print('\n'.join(lines))
    print('下载报告:', report)
    return 1 if totals['ERROR'] else 0


if __name__ == '__main__':
    sys.exit(main())
