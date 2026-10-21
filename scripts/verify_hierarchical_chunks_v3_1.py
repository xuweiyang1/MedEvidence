#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V3.1 quality gate: validate parent-child provenance, table rows, and graph.

Usage from project root:
  python scripts/verify_hierarchical_chunks_v3_1.py --mode pilot
  python scripts/verify_hierarchical_chunks_v3_1.py --mode all

Requires only Python 3.11 standard library. Read-only on the knowledge data.
This is a structural validator, NOT a medical-semantic or legal audit.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f'找不到数据文件: {path}')
    result = []
    with path.open('r', encoding='utf-8-sig') as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f'{path.name} 第 {n} 行不是有效JSON: {e}') from e
    return result


def unique_dict(rows: list[dict], key: str, issues: list[str]) -> dict[str,dict]:
    out = {}
    for row in rows:
        rid = row.get(key)
        if not isinstance(rid, str) or not rid:
            issues.append(f'{key} 为空')
        elif rid in out:
            issues.append(f'{key} 重复: {rid}')
        else:
            out[rid] = row
    return out


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def verify(base: Path):
    issues, warnings = [], []
    docs = load_jsonl(base/'documents.jsonl')
    parents = load_jsonl(base/'parent_units.jsonl')
    chunks = load_jsonl(base/'retrieval_chunks.jsonl')
    edges = load_jsonl(base/'evidence_relations.jsonl')
    media = load_jsonl(base/'media_registry.jsonl')

    d_by = unique_dict(docs, 'document_id', issues)
    p_by = unique_dict(parents, 'parent_id', issues)
    c_by = unique_dict(chunks, 'chunk_id', issues)
    m_by = unique_dict(media, 'media_id', issues)

    for p in parents:
        if p.get('document_id') not in d_by:
            issues.append(f"父节点的文章不存在: {p.get('parent_id')}")
    for c in chunks:
        cid, pid = c.get('chunk_id'), c.get('parent_id')
        if pid not in p_by:
            issues.append(f'Chunk无效parent: {cid} -> {pid}')
        elif c.get('document_id') != p_by[pid].get('document_id'):
            issues.append(f'Chunk与父节点document_id不符: {cid}')
        if not str(c.get('text') or '').strip():
            warnings.append(f'空文本Chunk: {cid}')

    all_ids = set(d_by)|set(p_by)|set(c_by)|set(m_by)
    edge_types = Counter()
    for i, e in enumerate(edges, 1):
        et, src, dst = e.get('type'), e.get('src'), e.get('dst')
        edge_types[et] += 1
        if src not in all_ids:
            issues.append(f'关系 {i} 源节点无效: {et} {src}')
        if dst not in all_ids:
            issues.append(f'关系 {i} 目标节点无效: {et} {dst}')
    # Structural-only root must really be a root with no retrieval text.
    for p in parents:
        if p.get('structural_only'):
            pid=p.get('parent_id','')
            did=p.get('document_id','')
            if pid != f'{did}:parent:body:s0001' or p.get('node_type')!='section_container':
                issues.append(f'不允许的合成章节节点: {pid}')
            if p.get('blocks') or p.get('full_text'):
                issues.append(f'结构根节点不应伪造正文: {pid}')
    for e in edges:
        if e.get('type')=='SECTION_HAS_SUBSECTION':
            to=p_by.get(e.get('dst'))
            if not to or not to.get('original_parent_section_id'):
                issues.append(f'章节下级关系没有原始父节点证据: {e.get("dst")}')
            elif e.get('src') != f'{to["document_id"]}:parent:body:{to["original_parent_section_id"]}':
                issues.append(f'章节下级关系不符合JATS parent_section_id: {e.get("dst")}')

    parent_child_edges = {(e.get('src'),e.get('dst')) for e in edges if e.get('type')=='PARENT_HAS_CHILD'}
    for c in chunks:
        if (c.get('parent_id'),c.get('chunk_id')) not in parent_child_edges:
            issues.append(f"缺PARENT_HAS_CHILD关系: {c.get('chunk_id')}")

    source_spans = defaultdict(list)
    source_lengths = {}
    text_blocks = {}
    for p in parents:
        if p.get('node_type') not in {'section'}:
            continue
        pid = p['parent_id']
        for b in p.get('blocks') or []:
            key = (pid, b.get('block_id'))
            if key in text_blocks:
                issues.append(f'父节点内重复block: {key}')
            txt = b.get('text') or ''
            text_blocks[key] = txt
            source_lengths[key] = len(txt)

    for c in chunks:
        if c.get('content_kind') not in {'abstract','body'}:
            continue
        cid, pid = c['chunk_id'], c['parent_id']
        segments = c.get('source_segments') or []
        reconstructed = []
        for s in segments:
            key=(pid,s.get('block_id'))
            if key not in text_blocks:
                issues.append(f'Chunk包含未知原文block: {cid}/{key[1]}')
                continue
            start, end = s.get('start_char'), s.get('end_char')
            original = text_blocks[key]
            if not isinstance(start,int) or not isinstance(end,int) or not (0 <= start < end <= len(original)):
                issues.append(f'Chunk无效原文区间: {cid}/{key[1]} {start}:{end}')
                continue
            value = original[start:end]
            reconstructed.append(value)
            source_spans[key].append((start,end,cid))
            if sha_text(value) != s.get('text_sha256'):
                issues.append(f'Chunk的source_segments哈希不符: {cid}')
        if '\n\n'.join(reconstructed) != c.get('text',''):
            issues.append(f'Chunk原文重建不一致: {cid}')
        if sha_text(c.get('text','')) != c.get('original_text_sha256'):
            issues.append(f'Chunk文本哈希不一致: {cid}')

    covered = original_chars = 0
    for key, length in source_lengths.items():
        original_chars += length
        spans=sorted(source_spans[key])
        cursor=0
        for start,end,cid in spans:
            if start != cursor:
                issues.append(f'正文区间缺失/重叠: {key} 在 {cursor}->{start}，chunk={cid}')
            cursor=end
            covered += end-start
        if cursor!=length:
            issues.append(f'正文没有完整覆盖: {key} {cursor}/{length}')

    # Table header can appear in each chunk; every non-header row must appear exactly once.
    table_rows = 0
    verified_data_rows = 0
    t_chunks=defaultdict(list)
    for c in chunks:
        if c.get('content_kind') == 'table':
            t_chunks[c.get('media_id')].append(c)
    for mid, m in m_by.items():
        if m.get('type')!='table':
            continue
        parent=p_by.get(mid+':parent')
        if not parent:
            issues.append(f'找不到表格父节点: {mid}')
            continue
        rows=(parent.get('content') or {}).get('rows') or []
        table_rows += len(rows)
        chunks_for_table=t_chunks.get(mid,[])
        if not chunks_for_table:
            issues.append(f'表格没有检索chunk: {mid}')
            continue
        covered_index=[]
        header_indices=chunks_for_table[0].get('table_header_indices') or []
        for c in chunks_for_table:
            indexes=c.get('table_row_indices') or []
            covered_index.extend(indexes)
            if c.get('table_header_indices') != header_indices:
                issues.append(f'表头索引在chunk间不一致: {mid}')
            # Verify each indexed data row is actually represented by cell values.
            for i in indexes:
                if not isinstance(i,int) or not 0<=i<len(rows):
                    issues.append(f'表格行编号无效: {mid}/{i}')
                    continue
                rowtxt = f"Row {i+1}: " + ' | '.join(str(cell.get('text','')) for cell in rows[i])
                if rowtxt not in c.get('text',''):
                    issues.append(f'表格chunk未包含原始行文字: {mid}/{i}')
            for i in header_indices:
                if not isinstance(i,int) or not 0<=i<len(rows):
                    issues.append(f'表头编号无效: {mid}/{i}')
                    continue
                htxt=f"Row {i+1}: "+' | '.join(str(cell.get('text','')) for cell in rows[i])
                if htxt not in c.get('text',''):
                    issues.append(f'表格chunk未重复表头: {mid}/{i}')
        want=set(range(len(rows)))-set(header_indices)
        if sorted(covered_index) != sorted(want):
            issues.append(f'表格数据行遗漏或重复: {mid}')
        verified_data_rows += len(covered_index)

    type_counts=Counter(c.get('content_kind') for c in chunks)
    report=[
        '=== MedEvidence Hierarchical Chunk V3.1 独立结构验收 ===',
        f'文章: {len(docs)}', f'父节点: {len(parents)}', f'子Chunk: {len(chunks)}',
        f'Chunk类型: {dict(type_counts)}',
        f'已解析正文覆盖: {covered}/{original_chars}',
        f'结构化表格行总数: {table_rows}',
        f'被验证的表格数据行引用: {verified_data_rows}',
        f'关系条数: {len(edges)}', f'关系类型: {dict(edge_types)}',
        f'图表登记数: {len(media)}',
        f'结构错误: {len(issues)}', f'其他提示: {len(warnings)}',
        '注意：结构检查通过不代表医学语义完整，不代表已完成图片视觉识别或许可审批。',
    ]
    if issues:
        report += ['结构错误样例:']+[f'- {s}' for s in issues[:30]]
    if warnings:
        report += ['提示样例:']+[f'- {s}' for s in warnings[:10]]

    # Provide representative records to inspect in PyCharm, not a replacement for human checking.
    examples=[]
    wanted=['RESULTS','METHODS','SAFETY','LIMITATIONS','table','figure_caption','body']
    used=set()
    for kind in wanted:
        for c in chunks:
            cid=c['chunk_id']
            if cid in used:
                continue
            if c.get('evidence_role_hint') == kind or c.get('content_kind')==kind:
                used.add(cid)
                examples.append(f"[{kind}] {cid}\nparent={c.get('parent_id')}\n"+
                                f"section={c.get('section_heading_path') or c.get('section_id','')}\n"+
                                str(c.get('text',''))[:1300].replace('\r','')+'\n')
                break
    return report, examples, len(issues)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--mode',choices=['pilot','all'],default='pilot')
    parser.add_argument('--sample',type=int,default=20,help='pilot文章数，默认20')
    args=parser.parse_args()
    root=args.root.resolve()
    base=root/'data/processed/knowledge'/('v3_1_pilot_'+str(args.sample) if args.mode=='pilot' else 'v3_1_all')
    lines, examples, problems=verify(base)
    reports=root/'reports';reports.mkdir(parents=True,exist_ok=True)
    name='v3_1_pilot_'+str(args.sample) if args.mode=='pilot' else 'v3_1_all'
    result=reports/f'{name}_quality_check.txt'
    samples=reports/f'{name}_manual_examples.txt'
    result.write_text('\n'.join(lines)+'\n',encoding='utf-8-sig')
    samples.write_text('\n\n---\n\n'.join(examples)+'\n',encoding='utf-8-sig')
    print('\n'.join(lines))
    print(f'\n生成：{result}\n样例：{samples}')
    return 2 if problems else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError,ValueError,TypeError,KeyError) as e:
        print(f'[ERROR] {type(e).__name__}: {e}',file=sys.stderr)
        sys.exit(2)
