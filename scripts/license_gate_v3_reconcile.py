#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence License Gate V3: reconcile CDC line-endings and PMC license scopes.

Place at: MedEvidence/scripts/license_gate_v3_reconcile.py
Run: python scripts/license_gate_v3_reconcile.py
     python scripts/license_gate_v3_reconcile.py --root E:\\Download\\Agent\\MedEvidence

Python 3.11 standard library only. Reads V2 audit and raw CDC/PMC files. Never
modifies raw files, manifests, source registry, or V2 outputs.

The script never grants legal approval. CANDIDATE means preliminary local
research candidate, not redistributable/commercial-use approval.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CCBY4 = re.compile(r"(?:creativecommons\.org/licenses/by/4\.0/?|creative\s+commons\s+attribution\s+4\.0)", re.I)
CC0URL = re.compile(r"creativecommons\.org/publicdomain/zero/1\.0/?", re.I)
CC0_TEXT = re.compile(r"(?:public\s+domain\s+dedication\s+waiver|CC0\s*(?:1\.0)?)", re.I)
DATA_SCOPE = re.compile(r"(?:appl(?:y|ies)\s+to\s+(?:the\s+)?data\b|data\s+made\s+available\s+in\s+this\s+article)", re.I)
SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
ADDED_FIELDS = ["v2_decision", "integrity_status_v3", "sha256_normalized_lf", "article_license_scope_v3", "cc0_data_waiver_signal", "v3_review_note", "v3_checked_at"]


def read_csv(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"找不到: {path}")
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"CSV没有表头: {path}")
        return list(reader), list(reader.fieldnames)


def resolve_project_file(root: Path, relative: str) -> Path | None:
    path_str = (relative or "").strip().replace('\\', '/')
    if not path_str or re.match(r"^[A-Za-z]:/", path_str) or path_str.startswith('/'):
        return None
    p = (root / path_str).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def find_article_license_scope(xml_path: Path):
    """Detect the common article-CC-BY/data-CC0 boilerplate in actual JATS XML.

    Returns only a cautious signal, not a legal determination.
    """
    root = ET.parse(xml_path).getroot()
    nodes = [n for n in root.iter() if n.tag.rsplit('}', 1)[-1] == 'license']
    if not nodes:
        return 'NO_LICENSE_NODE', False, 'XML没有<license>节点'
    texts, urls = [], []
    for node in nodes:
        texts.append(' '.join(''.join(node.itertext()).split()))
        for child in node.iter():
            for k, v in child.attrib.items():
                if k.rsplit('}', 1)[-1].lower() in {'href','url'}:
                    urls.append(v)
    whole_text = ' '.join(texts)
    combined = whole_text + ' ' + ' '.join(urls)
    has_ccby = bool(CCBY4.search(combined))
    has_cc0 = bool(CC0URL.search(combined) or CC0_TEXT.search(whole_text))
    has_data_scope = bool(DATA_SCOPE.search(whole_text))
    if has_ccby and has_cc0 and has_data_scope:
        return 'POSSIBLE_ARTICLE_CC_BY_4.0_DATA_CC0', True, 'XML同一许可区域显示文章CC BY 4.0且含与数据相关的CC0声明；需人工确认完整适用范围'
    if has_ccby and has_cc0:
        return 'MIXED_CC_BY_4.0_AND_CC0_SCOPE_UNKNOWN', False, 'XML有CC BY 4.0和CC0，但未提取到明确的数据适用范围'
    return 'NOT_CONFIRMED', False, '未能确认文章CC BY与数据CC0的作用范围'


def audit(root: Path):
    v2 = root / 'licenses/document_license_audit_v2.csv'
    rows, fields = read_csv(v2)
    needed = {'source','document_id','decision','local_path','sha256_expected','hash_check','size_check','is_manuscript','is_retracted','xml_license'}
    if missing := needed - set(fields):
        raise ValueError(f'V2登记缺少字段: {sorted(missing)}')

    timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    counts = Counter()
    info = []
    for row in rows:
        d = dict(row)
        source = d['source']
        old = d['decision']
        d.update(v2_decision=old, integrity_status_v3=d.get('hash_check',''), sha256_normalized_lf='',
                 article_license_scope_v3='', cc0_data_waiver_signal='', v3_review_note='',v3_checked_at=timestamp)
        target = resolve_project_file(root, d.get('local_path',''))
        has_file = bool(target is not None and target.is_file())
        if source == 'CDC':
            # Recheck both bytes and hashes, not trusting a prior diagnostics CSV.
            if not has_file:
                d['decision']='HOLD'
                d['integrity_status_v3']='MISSING'
                d['v3_review_note']='CDC原始TXT缺失或路径无效'
            else:
                b = target.read_bytes()
                real_raw = digest(b)
                normal = digest(b.replace(b'\r\n',b'\n'))
                expected = d.get('sha256_expected','').strip().lower()
                d['sha256_actual'] = real_raw
                d['sha256_normalized_lf'] = normal
                if not SHA256.fullmatch(expected):
                    d['decision']='REVIEW'
                    d['integrity_status_v3']='NO_EXPECTED_HASH'
                    d['v3_review_note']='没有有效登记哈希；CDC第三方内容仍需核对'
                elif real_raw == expected:
                    d['decision']='REVIEW'
                    d['integrity_status_v3']='RAW_MATCH'
                    d['v3_review_note']='字节哈希一致；CDC页面仍需逐篇检查第三方版权与来源'
                elif normal == expected:
                    d['decision']='REVIEW'
                    d['integrity_status_v3']='NORMALIZED_LF_MATCH_RAW_MISMATCH'
                    d['v3_review_note']='CRLF转LF后与原登记哈希一致；保留原始字节哈希和规范化哈希；仍需核对第三方材料'
                else:
                    d['decision']='HOLD'
                    d['integrity_status_v3']='UNEXPLAINED_MISMATCH'
                    d['v3_review_note']='规范化后仍无法匹配原登记SHA256；暂停使用'
        elif source == 'PMC' and old == 'HOLD' and 'CONFLICT:' in d.get('xml_license',''):
            # A scope check is not sufficient to grant CANDIDATE/APPROVED.
            if d.get('hash_check') != 'MATCH' or d.get('size_check') not in {'MATCH','NOT_REGISTERED'}:
                d['v3_review_note']='XML完整性未通过，不处理许可证冲突'
            elif d.get('is_retracted','').lower()=='true':
                d['v3_review_note']='撤稿文章保持HOLD'
            elif not has_file:
                d['v3_review_note']='缺少原始XML，保持HOLD'
            else:
                try:
                    scope, waiver, note = find_article_license_scope(target)
                    d['article_license_scope_v3']=scope
                    d['cc0_data_waiver_signal']=str(waiver)
                    d['v3_review_note']=note
                    if scope == 'POSSIBLE_ARTICLE_CC_BY_4.0_DATA_CC0':
                        d['decision']='REVIEW'  # Not automatically approved.
                except (OSError, ET.ParseError, ValueError) as e:
                    d['v3_review_note']=f'XML复核失败，保持HOLD: {type(e).__name__}: {e}'
        elif source == 'WHO':
            d['v3_review_note']='WHO的NC/SA/IGO限制及逐份PDF第三方权利未变化，保持原判定'
        elif source == 'PMC' and old == 'CANDIDATE':
            d['v3_review_note']='保持V2候选；仅限本地试验准备，未完成最终权利批准'
        elif source == 'PMC' and old == 'REVIEW':
            d['v3_review_note']='PMC作者稿版本和重用权利仍需人工复核'
        counts[(source, d['decision'])] += 1
        info.append(d)

    out = root / 'licenses/document_license_audit_v3.csv'
    out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('w', encoding='utf-8-sig', newline='') as f:
        w=csv.DictWriter(f, fieldnames=fields + [x for x in ADDED_FIELDS if x not in fields]);w.writeheader();w.writerows(info)

    # Local-only, traceable candidate dataset; not a redistribution whitelist.
    candidate_fields=['document_id','disease','title','local_path','license_normalized','license_url','sha256_expected','v3_decision','warning']
    candidates=[]
    for d in info:
        if (d['source']=='PMC' and d['decision']=='CANDIDATE' and d.get('hash_check')=='MATCH'
                and d.get('size_check') in {'MATCH','NOT_REGISTERED'}
                and d.get('is_manuscript','').lower()!='true'
                and d.get('is_retracted','').lower()!='true'):
            candidates.append({'document_id':d['document_id'],'disease':d['disease'],
                'title':d['title'],'local_path':d['local_path'],
                'license_normalized':d.get('license_normalized',''), 'license_url':d.get('license_url',''),
                'sha256_expected':d.get('sha256_expected',''), 'v3_decision':d['decision'],
                'warning':'LOCAL_RESEARCH_ONLY_NOT_LEGAL_APPROVAL'})
    stage = root / 'data/processed/eligibility/pmc_candidate_manifest.csv'
    stage.parent.mkdir(parents=True,exist_ok=True)
    with stage.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=candidate_fields);w.writeheader();w.writerows(candidates)

    report = root / 'reports/license_audit_v3.txt'
    report.parent.mkdir(parents=True, exist_ok=True)
    status_counts = Counter(d['decision'] for d in info)
    integ_counts = Counter(d['integrity_status_v3'] for d in info if d['source']=='CDC')
    scopes = Counter(d['article_license_scope_v3'] for d in info if d['source']=='PMC' and d['article_license_scope_v3'])
    lines = [
        '=== MedEvidence License Gate V3: 完整性与许可范围复核 ===',
        f'生成时间UTC: {timestamp}', f'项目目录: {root}',
        f'总文档: {len(info)}',f'审核状态: {dict(status_counts)}',
        f'分来源状态: {dict(counts)}',
        f'CDC完整性状态: {dict(integ_counts)}',
        f'PMC冲突许可范围信号: {dict(scopes)}',
        f'PMC本地实验候选清单行数: {len(candidates)}',
        'CDC字节哈希不同于规范化文本哈希；程序没有覆盖原登记SHA256。',
        'PMC出现文章CC-BY和数据CC0的信号只使其进入REVIEW，不能自动获得最终批准。',
        'WHO保持受限制状态；所有CANDIDATE仅表示待人工最终核对的本地实验候选。',
        '原始数据、已有manifest、registries、V2审计文件均未修改。',
    ]
    report.write_text('\n'.join(lines)+'\n',encoding='utf-8-sig')
    print('\n'.join(lines))
    print('\n已生成:')
    print(' ', out)
    print(' ', stage)
    print(' ', report)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    args=p.parse_args()
    root=args.root.expanduser().resolve()
    try: audit(root)
    except (OSError, ValueError) as e:
        print(f'ERROR: {e}',file=sys.stderr);return 2
    return 0

if __name__=='__main__':
    sys.exit(main())
