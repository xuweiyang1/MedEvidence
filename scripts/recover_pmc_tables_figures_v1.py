#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence: recover structured tables, figure captions and media links from ORIGINAL JATS.

Usage (Windows/PyCharm terminal, Python 3.11+ standard library):
    python scripts/recover_pmc_tables_figures_v1.py --limit 10
    python scripts/recover_pmc_tables_figures_v1.py

Never modifies PMC XML, original parsed articles, manifests, licenses or old chunks.
Produces sidecar JSONL files and audit report. Does NOT extract image pixels.
CANDIDATE = local research only; verify media-specific third-party rights.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote

SHA_RE = re.compile(r"[0-9a-f]{64}", re.I)
PMC_RE = re.compile(r"PMC\d+", re.I)
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.tif', '.tiff', '.svg', '.webp', '.bmp'}
AWS_HOST = 'pmc-oa-opendata.s3.amazonaws.com'
BLOCK_TAGS = {'p', 'disp-quote', 'preformat', 'speech', 'verse-group', 'list'}
REPORT_FIELDS = ['document_id', 'status', 'error', 'tables', 'tables_structured', 'tables_graphic_only',
                 'tables_unparsed', 'figures', 'image_links', 'image_files_locally_present',
                 'image_urls_found', 'image_links_missing', 'supplementary', 'crossrefs', 'sha256_ok']


def name(node):
    return node.tag.rsplit('}', 1)[-1] if isinstance(node.tag, str) else ''


def direct(node, child_name):
    if node is None:
        return None
    return next((c for c in node if name(c) == child_name), None)


def txt(node):
    return re.sub(r'\s+', ' ', ''.join(node.itertext())).strip() if node is not None else ''


def caption_text(node):
    if node is None:
        return ''
    parts = [txt(c) for c in node if txt(c)]
    return '\n'.join(parts) if parts else txt(node)


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def project_path(root, relative):
    v = (relative or '').strip().replace('\\', '/')
    if not v or v.startswith('/') or re.match(r'^[A-Za-z]:/', v):
        raise ValueError('禁止绝对路径或空路径')
    p = (root / v).resolve()
    if not p.is_relative_to(root):
        raise ValueError('文件路径超出项目目录')
    return p


def safe_media_name(ref):
    if not ref or '\\' in ref:
        return ''
    if ref.startswith(('https://', 'http://')):
        # Only URLs from vetted PMC metadata reach here, never from arbitrary JATS.
        ref = urlparse(ref).path.rsplit('/', 1)[-1]
    ref = unquote(ref)
    if not ref or '/' in ref or '?' in ref or ref in {'.', '..'} or '\x00' in ref:
        return ''
    return ref


def href(node):
    for key, val in node.attrib.items():
        if key.rsplit('}', 1)[-1].lower() in {'href', 'src'}:
            return val.strip()
    return ''


def good_aws_url(url, article_version):
    try:
        p = urlparse(url)
        return p.scheme == 'https' and p.hostname == AWS_HOST and \
               p.path.startswith('/' + article_version + '/') and not p.username and not p.password
    except ValueError:
        return False


def gather_metadata_media(meta_json, article_version):
    """PMC metadata provides media_urls; filter strictly to current version and official AWS host."""
    try:
        meta = json.loads(meta_json.read_text(encoding='utf-8-sig')) if meta_json.is_file() else {}
    except (ValueError, UnicodeError, OSError):
        return []
    raw = meta.get('media_urls', [])
    if not isinstance(raw, list):
        return []
    values = []
    for item in raw:
        u = item if isinstance(item, str) else ''
        if u and good_aws_url(u, article_version):
            values.append(u)
    return values


def resolve_image(ref, xml_path, root, article_version, urls):
    """Return local file indicator + URL candidate; never guess a remote URL."""
    base = safe_media_name(ref)
    if not base:
        return {'xlink_href': ref, 'file_name': '', 'local_path': '', 'media_url': '',
                'status': 'NO_SAFE_FILE_REFERENCE'}
    candidates = []
    for u in urls:
        filename = safe_media_name(u)
        if filename and (filename.lower() == base.lower() or
                         (Path(base).suffix == '' and Path(filename).stem.lower() == base.lower())):
            candidates.append((filename, u))
    if Path(base).suffix.lower() in IMAGE_EXTS:
        files = [base]
    else:
        files = [b for b, _ in candidates if Path(b).suffix.lower() in IMAGE_EXTS]
    for possible in files:
        for parent in (xml_path.parent, root / 'data/processed/media/assets' / article_version):
            p = parent / possible
            if p.is_file():
                return {'xlink_href': ref, 'file_name': possible, 'local_path': p.relative_to(root).as_posix(),
                        'media_url': next((u for n, u in candidates if n == possible), ''),
                        'status': 'LOCAL_FILE_FOUND'}
    if candidates:
        matched = next(((n, u) for n, u in candidates if Path(n).suffix.lower() in IMAGE_EXTS), candidates[0])
        return {'xlink_href': ref, 'file_name': matched[0], 'local_path': '',
                'media_url': matched[1], 'status': 'DOWNLOAD_AVAILABLE' if Path(matched[0]).suffix.lower() in IMAGE_EXTS
                else 'NON_IMAGE_MEDIA'}
    return {'xlink_href': ref, 'file_name': base, 'local_path': '', 'media_url': '',
            'status': 'METADATA_URL_NOT_FOUND'}


def table_rows(table):
    """Preserve rows, cell text and colspan/rowspan; no silent flattening."""
    result = []
    for tr in table.iter():
        if name(tr) != 'tr':
            continue
        cells = []
        for cell in tr:
            kind = name(cell)
            if kind in {'td', 'th'}:
                cells.append({'text': txt(cell), 'is_header': kind == 'th',
                              'colspan': cell.attrib.get('colspan', '1'),
                              'rowspan': cell.attrib.get('rowspan', '1')})
        if cells:
            result.append(cells)
    return result


def jats_location(el, parents, section_ids, section_paths):
    n = el
    while n is not None:
        if n in section_ids:
            return section_ids[n], section_paths.get(n, [])
        n = parents.get(n)
    return '', []


def section_index(article, parents):
    """Replicates V1 Body walk order, so sXXXX IDs correspond to existing JSONL."""
    ids, paths, block_ids = {}, {}, {}
    body = direct(article, 'body')
    serial = 0

    def walk(node, headings):
        nonlocal serial
        title = txt(direct(node, 'title')) if name(node) == 'sec' else ''
        path = headings + ([title] if title else [])
        serial += 1
        sid = f's{serial:04d}'
        ids[node] = sid
        paths[node] = path
        block_serial = 0
        for sub in node:
            if name(sub) not in BLOCK_TAGS:
                continue
            content = txt(sub) if name(sub) != 'list' else '\n'.join('- ' + txt(x) for x in sub.iter() if name(x) == 'list-item' and txt(x))
            if content:
                block_serial += 1
                block_ids[sub] = f'{sid}-b{block_serial:04d}'
        for sub in node:
            if name(sub) == 'sec':
                walk(sub, path)
    if body is not None:
        walk(body, [])
    return ids, paths, block_ids


def xref_links(article, parents, section_ids, section_paths, block_ids):
    links = defaultdict(list)
    body = direct(article, 'body')
    if body is None:
        return links
    for x in body.iter():
        if name(x) != 'xref':
            continue
        ids = (x.attrib.get('rid') or '').strip().split()
        if not ids:
            continue
        ancestors = []
        ancestor = parents.get(x)
        while ancestor is not None:
            ancestors.append(ancestor)
            ancestor = parents.get(ancestor)
        if any(name(a) in {'table-wrap', 'fig', 'ref-list'} for a in ancestors):
            continue  # Not a citation from main prose
        section_id, path = jats_location(x, parents, section_ids, section_paths)
        blk = next((block_ids[a] for a in ancestors if a in block_ids), '')
        p = next((a for a in ancestors if name(a) == 'p'), None)
        context = txt(p)[:900] if p is not None else ''
        for target_id in ids:
            links[target_id].append({'source_section_id': section_id, 'section_heading_path': path,
                                     'source_block_id': blk, 'reference_text': txt(x),
                                     'ref_type': x.attrib.get('ref-type', ''),
                                     'paragraph_context': context})
    return links


def parse_media(xml_path, row, project_root):
    article = ET.parse(xml_path).getroot()
    if name(article) != 'article':
        raise ValueError('JATS 根元素不是 article')
    did = (row['document_id'] or '').strip().upper()
    version = xml_path.parent.name
    if not re.fullmatch(re.escape(did) + r'\.\d+', version):
        raise ValueError('XML 所在文件夹版本与 document_id 不一致')
    parents = {c: p for p in article.iter() for c in p}
    section_ids, section_paths, block_ids = section_index(article, parents)
    refs = xref_links(article, parents, section_ids, section_paths, block_ids)
    metadata_urls = gather_metadata_media(xml_path.with_suffix('.json'), version)
    # Deduplicate file names within the same paper's figures.
    figure_objects, table_objects, extras = [], [], []
    figure_names_seen = set()
    table_names_seen = set()
    asset_refs = {}

    def graphic_items(node):
        result = []
        for x in node.iter():
            if name(x) in {'graphic', 'inline-graphic', 'media', 'inline-media'}:
                raw = href(x)
                if raw and raw not in asset_refs:
                    asset_refs[raw] = resolve_image(raw, xml_path, project_root, version, metadata_urls)
                if raw:
                    result.append(asset_refs[raw])
        return result

    for i, t in enumerate([n for n in article.iter() if name(n) == 'table-wrap'], 1):
        jats_id = t.attrib.get('id', '')
        tid = jats_id or f'AUTO_TABLE_{i:04d}'
        if tid in table_names_seen:
            tid = f'{tid}__{i}'
        table_names_seen.add(tid)
        table = next((n for n in t.iter() if name(n) == 'table'), None)
        rows = table_rows(table) if table is not None else []
        assets = graphic_items(t)
        if rows:
            parse_status = 'STRUCTURED_ROWS'
        elif assets:
            parse_status = 'GRAPHIC_ONLY_OR_EXTERNAL'
        else:
            parse_status = 'NO_MACHINE_READABLE_ROWS'
        location, path = jats_location(t, parents, section_ids, section_paths)
        caption = next((c for c in t if name(c) == 'caption'), None)
        foot = next((c for c in t if name(c) == 'table-wrap-foot'), None)
        table_objects.append({
            'id': tid, 'jats_id': jats_id, 'label': txt(direct(t, 'label')),
            'caption': caption_text(caption), 'footnotes': txt(foot), 'rows': rows,
            'parse_status': parse_status, 'graphics': assets, 'section_id': location,
            'section_heading_path': path, 'referenced_by': refs.get(jats_id, []),
            'rights_note': 'ARTICLE_RIGHTS_NOT_AUTOMATICALLY_MEDIA_RIGHTS',
        })

    for i, f in enumerate([n for n in article.iter() if name(n) == 'fig'], 1):
        jats_id = f.attrib.get('id', '')
        fid = jats_id or f'AUTO_FIGURE_{i:04d}'
        if fid in figure_names_seen:
            fid = f'{fid}__{i}'
        figure_names_seen.add(fid)
        cap = direct(f, 'caption')
        location, path = jats_location(f, parents, section_ids, section_paths)
        alts = [txt(a) for a in f.iter() if name(a) in {'alt-text', 'long-desc'} and txt(a)]
        figure_objects.append({
            'id': fid, 'jats_id': jats_id, 'label': txt(direct(f, 'label')),
            'caption': caption_text(cap), 'alt_text': alts, 'graphics': graphic_items(f),
            'section_id': location, 'section_heading_path': path,
            'referenced_by': refs.get(jats_id, []),
            'image_interpretation_status': 'NOT_INTERPRETED',
            'rights_note': 'ARTICLE_RIGHTS_NOT_AUTOMATICALLY_MEDIA_RIGHTS',
        })

    for i, s in enumerate([n for n in article.iter() if name(n) == 'supplementary-material'], 1):
        raw = href(s)
        caption = direct(s, 'caption')
        extras.append({'id': s.attrib.get('id') or f'SUPP_{i:04d}', 'label': txt(direct(s, 'label')),
                       'caption': caption_text(caption), 'xlink_href': raw,
                       'media_url': next((u for u in metadata_urls if safe_media_name(u).lower() == raw.lower()), ''),
                       'status': 'REFERENCE_ONLY_NOT_PARSED'})
    return {
        'schema_version': 'openmedkb.media.v1', 'document_id': did, 'article_version': version,
        'source_xml': row['local_path'].replace('\\', '/'),
        'source_xml_sha256': row['sha256_expected'], 'rights': {
            'document_license': row.get('license_normalized', ''),
            'audit_status': row.get('v3_decision', ''),
            'media_third_party_review': 'REQUIRED_BEFORE_REDISTRIBUTION',
        },
        'tables': table_objects, 'figures': figure_objects, 'supplementary': extras,
        'media_url_count_from_metadata': len(metadata_urls), 'image_references': list(asset_refs.values()),
    }


def evidence_units(doc):
    for t in doc['tables']:
        data = [' | '.join(cell['text'].replace('\n', ' ') for cell in row) for row in t['rows']]
        content = '\n'.join(filter(None, [t['label'], t['caption'], *data, t['footnotes']]))
        if not content:
            continue
        yield {'evidence_id': doc['document_id'] + ':table:' + t['id'],
               'document_id': doc['document_id'], 'media_type': 'table',
               'jats_id': t['jats_id'], 'section_id': t['section_id'],
               'text': content, 'source_xml': doc['source_xml'],
               'parse_status': t['parse_status'],
               'evidence_use': ('STRUCTURED_TABLE_ROWS_AND_CAPTION' if t['parse_status'] == 'STRUCTURED_ROWS'
                                else 'TABLE_CAPTION_ONLY_UNVERIFIED'),
               'rights': doc['rights'], 'image_interpretation_status': 'NOT_APPLICABLE'}
    for f in doc['figures']:
        content = '\n'.join(filter(None, [f['label'], f['caption'], *f['alt_text']]))
        if not content:
            continue
        yield {'evidence_id': doc['document_id'] + ':figure:' + f['id'],
               'document_id': doc['document_id'], 'media_type': 'figure',
               'jats_id': f['jats_id'], 'section_id': f['section_id'],
               'text': content, 'source_xml': doc['source_xml'],
               'parse_status': 'CAPTION_ONLY', 'evidence_use': 'CAPTION_NOT_VISUAL_VERIFICATION',
               'rights': doc['rights'], 'image_interpretation_status': 'NOT_INTERPRETED'}


def read_candidates(path):
    if not path.is_file():
        raise FileNotFoundError(f'缺少候选清单: {path}')
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        rdr = csv.DictReader(f)
        required = {'document_id', 'local_path', 'v3_decision', 'sha256_expected'}
        if not rdr.fieldnames or not required.issubset(rdr.fieldnames):
            raise ValueError(f'候选清单缺少字段: {sorted(required - set(rdr.fieldnames or []))}')
        out = list(rdr)
    seen = set()
    for r in out:
        did = r['document_id'].upper().strip()
        if not PMC_RE.fullmatch(did) or did in seen or r['v3_decision'] != 'CANDIDATE' or not SHA_RE.fullmatch(r['sha256_expected']):
            raise ValueError(f'无效或非候选记录: {did}')
        seen.add(did)
    return out


def attach_to_existing_articles(root, media_jsonl, expected_ids, suffix):
    """Write a new enriched JSONL; never overwrite existing text/disease data."""
    media = {}
    with media_jsonl.open('r', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            did = obj['document_id']
            if did in media:
                raise ValueError(f'图表 sidecar 重复文章: {did}')
            media[did] = obj
    if set(media) != set(expected_ids):
        raise ValueError('图表 sidecar 的文章ID与本次候选清单不匹配')
    mapped = root / 'data/processed/ontology/pmc_articles_disease_mapped.jsonl'
    original = root / 'data/processed/normalized/pmc_articles.jsonl'
    available = [p for p in (mapped, original) if p.is_file()]
    if not available:
        return '', 0, '未发现既有文章JSONL，仅保留图表sidecar'
    selected = None
    for source in available:
        ids = set()
        with source.open('r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    doc = json.loads(line)
                    did = str(doc.get('document_id', '')).upper()
                    if did in ids:
                        raise ValueError(f'既有文章JSONL有重复ID: {did}')
                    ids.add(did)
        if set(expected_ids).issubset(ids):
            selected = source
            break
    if selected is None:
        return '', 0, '已有文章JSONL未覆盖全部候选ID；未生成合并版，保留图表sidecar'
    dest = root / f'data/processed/enriched/pmc_articles_with_media{suffix}.jsonl'
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + '.tmp')
    total = 0
    try:
        with selected.open('r', encoding='utf-8') as f, tmp.open('w', encoding='utf-8', newline='\n') as out:
            for line in f:
                if not line.strip():
                    continue
                article = json.loads(line)
                did = str(article.get('document_id', '')).upper()
                if did not in media:
                    continue  # Smoke mode only includes a subset.
                asset = media[did]
                source_sha = str(article.get('provenance', {}).get('source_xml_sha256', '')).lower()
                media_sha = str(asset['source_xml_sha256']).lower()
                if source_sha != media_sha:
                    raise ValueError(f'{did} 既有正文与补回图表的源XML SHA256不一致')
                if article.get('rights', {}).get('audit_status') != 'CANDIDATE':
                    raise ValueError(f'{did} 已有文章非候选审核状态')
                article['media'] = {
                    'tables': asset['tables'], 'figures': asset['figures'],
                    'supplementary': asset['supplementary'],
                    'image_references': asset['image_references'],
                    'media_parser': 'recover_pmc_tables_figures_v1',
                }
                article['media_completeness_note'] = '表格结构和图注已恢复；部分表格为图片，图像像素需要另外下载与核验'
                out.write(json.dumps(article, ensure_ascii=False, separators=(',', ':')) + '\n')
                total += 1
        if total != len(expected_ids):
            raise ValueError(f'合并后的文献数 {total} != 候选数 {len(expected_ids)}')
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    return str(dest.relative_to(root)), total, str(selected.relative_to(root))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()
    if args.limit < 0:
        ap.error('--limit 不能小于0')
    root = args.root.expanduser().resolve()
    try:
        rows = read_candidates(root / 'data/processed/eligibility/pmc_candidate_manifest.csv')
    except (OSError, ValueError) as exc:
        print('[ERROR]', exc, file=sys.stderr)
        return 2
    if args.limit:
        rows = rows[:args.limit]
    suffix = '_smoke' if args.limit else ''
    dpath = root / f'data/processed/media/pmc_media{suffix}.jsonl'
    upath = root / f'data/processed/media/pmc_media_evidence{suffix}.jsonl'
    rpath = root / f'reports/pmc_media_report{suffix}.csv'
    spath = root / f'reports/pmc_media_summary{suffix}.txt'
    for p in [dpath, upath, rpath, spath]:
        p.parent.mkdir(parents=True, exist_ok=True)
    temp_paths = [p.with_suffix(p.suffix + '.tmp') for p in [dpath, upath, rpath]]
    totals = Counter()
    report_rows = []
    with temp_paths[0].open('w', encoding='utf-8', newline='\n') as dout, \
         temp_paths[1].open('w', encoding='utf-8', newline='\n') as uout:
        for idx, row in enumerate(rows, 1):
            rec = dict.fromkeys(REPORT_FIELDS, '')
            did = row['document_id'].strip().upper()
            rec.update(document_id=did, status='ERROR', sha256_ok='False')
            try:
                source = project_path(root, row['local_path'])
                if not source.is_file():
                    raise FileNotFoundError(source)
                if file_hash(source).lower() != row['sha256_expected'].strip().lower():
                    raise ValueError('原始XML与 V3 SHA256不一致，拒绝处理')
                rec['sha256_ok'] = 'True'
                doc = parse_media(source, row, root)
                dout.write(json.dumps(doc, ensure_ascii=False, separators=(',', ':')) + '\n')
                for e in evidence_units(doc):
                    uout.write(json.dumps(e, ensure_ascii=False, separators=(',', ':')) + '\n')
                    totals['evidence_units'] += 1
                rec.update(status='OK', tables=len(doc['tables']),
                           tables_structured=sum(t['parse_status'] == 'STRUCTURED_ROWS' for t in doc['tables']),
                           tables_graphic_only=sum(t['parse_status'] == 'GRAPHIC_ONLY_OR_EXTERNAL' for t in doc['tables']),
                           tables_unparsed=sum(t['parse_status'] == 'NO_MACHINE_READABLE_ROWS' for t in doc['tables']),
                           figures=len(doc['figures']), image_links=len(doc['image_references']),
                           image_files_locally_present=sum(x['status'] == 'LOCAL_FILE_FOUND' for x in doc['image_references']),
                           image_urls_found=sum(x['status'] == 'DOWNLOAD_AVAILABLE' for x in doc['image_references']),
                           image_links_missing=sum(x['status'] in {'METADATA_URL_NOT_FOUND', 'NO_SAFE_FILE_REFERENCE'} for x in doc['image_references']),
                           supplementary=len(doc['supplementary']),
                           crossrefs=sum(len(t['referenced_by']) for t in doc['tables']) +
                                     sum(len(f['referenced_by']) for f in doc['figures']))
            except (ValueError, OSError, ET.ParseError, TypeError, KeyError) as exc:
                rec['error'] = f'{type(exc).__name__}: {exc}'
            for col in REPORT_FIELDS[3:-1]:
                try:
                    totals[col] += int(rec[col] or '0')
                except (ValueError, TypeError):
                    pass
            totals[rec['status']] += 1
            report_rows.append(rec)
            if idx % 100 == 0 or idx == len(rows):
                print(f'[Progress] {idx}/{len(rows)}; OK={totals["OK"]}; ERROR={totals["ERROR"]}')
    for p in (dpath, upath):
        os.replace(p.with_suffix(p.suffix + '.tmp'), p)
    with temp_paths[2].open('w', encoding='utf-8-sig', newline='') as fout:
        writer = csv.DictWriter(fout, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(report_rows)
    os.replace(temp_paths[2], rpath)
    merged_path, merged_count, merge_source = '', 0, ''
    if not totals['ERROR']:
        try:
            merged_path, merged_count, merge_source = attach_to_existing_articles(
                root, dpath, [(r['document_id'] or '').strip().upper() for r in rows], suffix)
        except (ValueError, OSError, TypeError) as exc:
            merge_source = f'合并失败，已保留原文和图表sidecar: {type(exc).__name__}: {exc}'
            totals['merge_error'] += 1
    else:
        merge_source = '存在XML解析失败，跳过合并，等待核验'
    summary = [
        '=== MedEvidence PMC 图表恢复 V1 ===',
        f'运行时间 UTC: {datetime.now(timezone.utc).isoformat(timespec="seconds")}',
        f'模式: {"SMOKE" if args.limit else "FULL"}', f'候选文章: {len(rows)}',
        f'成功: {totals["OK"]}', f'失败: {totals["ERROR"]}',
        f'表格对象: {totals["tables"]}', f'结构化表格: {totals["tables_structured"]}',
        f'仅图片或外链的表格: {totals["tables_graphic_only"]}',
        f'未提取出机器可读行的表格: {totals["tables_unparsed"]}',
        f'图形对象: {totals["figures"]}', f'图形文件引用数(去重后): {totals["image_links"]}',
        f'已有本地文件: {totals["image_files_locally_present"]}',
        f'找到官方AWS图片下载URL: {totals["image_urls_found"]}',
        f'需补查文件URL: {totals["image_links_missing"]}',
        f'补充材料引用: {totals["supplementary"]}',
        f'正文交叉引用条数: {totals["crossrefs"]}',
        f'新增图表检索证据单元: {totals["evidence_units"]}',
        f'正文图表合并版文章: {merged_count}',
        f'合并后的独立文件: {merged_path or "未生成"}',
        f'合并来源或原因: {merge_source}',
        f'图表清单: {dpath.relative_to(root)}',
        f'图表文本单元: {upath.relative_to(root)}',
        '已严格校验 V3 候选状态和 XML SHA256；没有覆盖旧论文JSONL、旧Chunk或许可登记。',
        '图注不能代替实际图像读数；许可需另外排查第三方图像/表格权益。',
    ]
    spath.write_text('\n'.join(summary) + '\n', encoding='utf-8-sig')
    print('\n'.join(summary))
    print('逐篇结果:', rpath)
    return 1 if totals['ERROR'] or totals['merge_error'] else 0


if __name__ == '__main__':
    sys.exit(main())
