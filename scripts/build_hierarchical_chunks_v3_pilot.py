#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence OpenMedKB -- hierarchical retrieval prototype (PMC pilot).

Run in PyCharm Terminal:
    python scripts/build_hierarchical_chunks_v3_pilot.py --sample 20
    python scripts/build_hierarchical_chunks_v3_pilot.py --sample 20 --target-tokens 380 --max-tokens 600
    python scripts/build_hierarchical_chunks_v3_pilot.py --all     # ONLY after validating pilot

Python 3.11, stdlib only; no API, network, GPU or data rewrites.
Reads existing PMC enriched JSONL + V3 candidate manifest. It does not yet ingest
WHO/CDC; those require their own adapters. Keeps full text and media in parent units;
short child chunks are retrieval views, not replacements for source documents.
Token estimates are heuristics, NOT BGE-M3 tokenizer counts.
The script NEVER judges evidence strength or medical applicability.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PMC_RE = re.compile(r'PMC\d+', re.I)
SHA_RE = re.compile(r'[a-f0-9]{64}', re.I)
LATIN_WORDS = re.compile(r'[A-Za-z0-9]+')
CJK = re.compile(r'[\u3400-\u9fff]')
BOUNDARY = re.compile(r'(?:[.!?;。！？；](?:\s+|$)|\n+|\s+)')


def sha_text(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def approx_tokens(text: str) -> int:
    """Conservative rough estimate, for pilot segmentation only."""
    latin = len(LATIN_WORDS.findall(text))
    cjk = len(CJK.findall(text))
    # For English academic text, char/4 usually costs more than word count.
    return max(1, (len(text) + 3) // 4, int(latin * 1.25 + cjk)) if text else 0


def read_candidates(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f'缺少候选清单: {path}')
    ans = {}
    with path.open(encoding='utf-8-sig', newline='') as f:
        rd = csv.DictReader(f)
        needed = {'document_id', 'sha256_expected', 'v3_decision'}
        if not needed.issubset(rd.fieldnames or []):
            raise ValueError(f'候选清单缺少字段: {sorted(needed-set(rd.fieldnames or []))}')
        for row in rd:
            did = row['document_id'].upper().strip()
            digest = row['sha256_expected'].strip().lower()
            if (not PMC_RE.fullmatch(did) or not SHA_RE.fullmatch(digest)
                    or row['v3_decision'] != 'CANDIDATE' or did in ans):
                raise ValueError(f'非候选、重复或哈希无效: {did}')
            ans[did] = digest
    return ans


def load_articles(path: Path, candidates: dict[str,str]) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f'缺少合并文章文件: {path}')
    ans = []
    seen = set()
    with path.open(encoding='utf-8-sig') as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            doc = json.loads(line)
            did = str(doc.get('document_id', '')).upper()
            if did in seen or did not in candidates:
                raise ValueError(f'重复或不在候选白名单: {did}, 行 {line_no}')
            seen.add(did)
            if doc.get('source') != 'PMC' or doc.get('rights', {}).get('audit_status') != 'CANDIDATE':
                raise ValueError(f'来源/许可状态不符: {did}')
            if doc.get('provenance', {}).get('source_xml_sha256', '').lower() != candidates[did]:
                raise ValueError(f'源XML哈希登记不一致: {did}')
            if not isinstance(doc.get('media'), dict):
                raise ValueError(f'尚未合并媒体内容: {did}')
            ans.append(doc)
    if seen != set(candidates):
        raise ValueError(f'候选清单与合并文章不同，缺少{len(set(candidates)-seen)}篇')
    return ans


def text_size(doc: dict) -> int:
    return sum(len(x.get('text') or '') for x in doc.get('abstract_sections') or []) + sum(
        len(b.get('text') or '') for sec in doc.get('body_sections') or [] for b in sec.get('blocks') or [])


def pick_diverse(docs: list[dict], n: int) -> list[dict]:
    """Balance original download domains, then select short/medium/long articles per domain."""
    if n >= len(docs):
        return sorted(docs, key=lambda x: x['document_id'])
    buckets = defaultdict(list)
    for d in docs:
        tags = d.get('source_topic_tags') or ['unclassified']
        buckets[str(tags[0])].append(d)
    for b in buckets.values():
        b.sort(key=lambda x: (text_size(x), x['document_id']))
    domains = sorted(buckets)
    picked = []
    used = set()
    # One quantile from each domain in successive rounds, deterministic.
    rounds = (n + len(domains) - 1) // len(domains)
    for rnd in range(rounds):
        for domain in domains:
            if len(picked) >= n:
                break
            bucket = buckets[domain]
            q = (rnd + .5) / rounds
            center = round((len(bucket) - 1) * q)
            for idx in sorted(range(len(bucket)), key=lambda i: (abs(i-center), i)):
                doc = bucket[idx]
                if doc['document_id'] not in used:
                    used.add(doc['document_id'])
                    picked.append(doc)
                    break
    if len(picked) < n:
        for d in sorted(docs, key=lambda x: (text_size(x), x['document_id'])):
            if d['document_id'] not in used:
                picked.append(d)
                used.add(d['document_id'])
                if len(picked) == n:
                    break
    return picked


def slice_block(text: str, max_tokens: int):
    """Sentence/word-aware cuts, with exact contiguous source character offsets."""
    start = 0
    while start < len(text):
        if approx_tokens(text[start:]) <= max_tokens:
            yield start, len(text)
            break
        lo, hi = start+1, len(text)
        while lo < hi:
            mid = (lo + hi + 1)//2
            if approx_tokens(text[start:mid]) <= max_tokens:
                lo = mid
            else:
                hi = mid-1
        hard_end = lo
        if hard_end <= start:
            raise ValueError('切分函数无进展')
        min_pos = start + (hard_end-start)//2
        boundaries = [m.end()+start for m in BOUNDARY.finditer(text[start:hard_end])
                      if m.end()+start >= min_pos]
        end = max(boundaries) if boundaries else hard_end
        if end <= start:
            end = hard_end
        yield start, end
        start = end


def infer_role(path, kind: str) -> str:
    """Section-label hint only: NEVER an evidence-grade or outcome determination."""
    if kind == 'abstract':
        return 'ABSTRACT'
    name = ' '.join(str(p) for p in path).lower()
    if re.search(r'limitation|strengths and limitations|caveat', name):
        return 'LIMITATIONS'
    if re.search(r'adverse|safety|side effect', name):
        return 'SAFETY'
    if re.search(r'result|finding|outcome', name):
        return 'RESULTS'
    if re.search(r'method|material|participant|eligib|trial design', name):
        return 'METHODS'
    if re.search(r'discussion|interpretation', name):
        return 'DISCUSSION'
    if re.search(r'conclusion', name):
        return 'CONCLUSION'
    if re.search(r'introduction|background', name):
        return 'INTRODUCTION'
    return 'UNCLASSIFIED'


def make_text_sections(doc: dict):
    for i, x in enumerate(doc.get('abstract_sections') or [], 1):
        t = x.get('text', '')
        if t:
            sid = f'a{i:04d}'
            yield 'abstract', sid, [], [x.get('heading') or 'Abstract'], [
                {'block_id': f'{sid}-b0001', 'block_type': 'abstract', 'text': t}]
    for i, sec in enumerate(doc.get('body_sections') or [], 1):
        sid = str(sec.get('section_id') or f's{i:04d}')
        rawpath = sec.get('heading_path') or [sec.get('heading') or 'Body']
        path = [str(x) for x in rawpath] if isinstance(rawpath,list) else [str(rawpath)]
        yield 'body', sid, [sec.get('parent_section_id')] if sec.get('parent_section_id') else [], path, sec.get('blocks') or []


def build_text(doc: dict, target: int, ceiling: int):
    did = doc['document_id']
    parents, chunks, edges = [], [], []
    lengths = {}  # raw source block -> total original length
    covered = defaultdict(list)
    block_to_chunks = defaultdict(set)
    seen_block = set()
    for kind, sid, ancestor, path, source_blocks in make_text_sections(doc):
        pid = f'{did}:parent:{kind}:{sid}'
        safe_blocks = []
        for i, b in enumerate(source_blocks,1):
            text = b.get('text') or ''
            if not isinstance(text, str):
                raise ValueError(f'文本字段不是字符串: {did} {sid}')
            bid = str(b.get('block_id') or f'{sid}-b{i:04d}')
            if bid in seen_block:
                raise ValueError(f'原文block_id重复: {did}/{bid}')
            seen_block.add(bid)
            if text:
                lengths[bid] = len(text)
                safe_blocks.append({'block_id':bid,'block_type':b.get('block_type','p'), 'text':text})
        parents.append({'parent_id':pid, 'document_id':did, 'node_type':'section',
                        'section_id':sid,'section_kind':kind,'heading_path':path,
                        'original_parent_section_id':ancestor[0] if ancestor else '',
                        'evidence_role_hint':infer_role(path,kind),
                        'blocks':safe_blocks,
                        'full_text':'\n\n'.join(b['text'] for b in safe_blocks),
                        'rights':doc.get('rights',{}), 'provenance':doc.get('provenance',{})})
        edges.append({'src':did,'dst':pid,'type':'DOCUMENT_HAS_PARENT'})
        if ancestor:
            edges.append({'src':f'{did}:parent:body:{ancestor[0]}','dst':pid,'type':'SECTION_HAS_SUBSECTION'})

        units = []
        for b in safe_blocks:
            bid = b['block_id']; t=b['text']
            for s,e in slice_block(t, ceiling):
                units.append({'block_id':bid,'block_type':b['block_type'],
                              'start_char':s, 'end_char':e,'text':t[s:e]})
        group=[]
        def flush():
            if not group:
                return
            idx = 1 + sum(1 for c in chunks if c['parent_id']==pid)
            cid = f'{pid}:child:{idx:04d}'
            pure = '\n\n'.join(u['text'] for u in group)
            segs=[]
            for u in group:
                bid=u['block_id']; st=u['start_char']; en=u['end_char']
                covered[bid].append((st,en))
                block_to_chunks[bid].add(cid)
                segs.append({'block_id':bid,'block_type':u['block_type'], 'start_char':st,
                             'end_char':en,'text_sha256':sha_text(u['text'])})
            record={'chunk_id':cid,'document_id':did,'parent_id':pid,'content_kind':kind,
                    'section_id':sid,'section_heading_path':path,
                    'evidence_role_hint':infer_role(path,kind),'text':pure,
                    'embedding_text':'\n'.join(v for v in (doc.get('title',''), ' > '.join(path), pure) if v),
                    'source_segments':segs,'token_count_estimate':approx_tokens(pure),
                    'actual_tokenizer_checked':False,'original_text_sha256':sha_text(pure),
                    'document_tags': {'doids':doc.get('disease_ontology_ids',[]),
                                      'mesh_terms_raw':doc.get('mesh_terms_raw',''),
                                      'scope':'DOCUMENT_LEVEL_ONLY'},
                    'related_media_ids':[], 'rights_status':'CANDIDATE_LOCAL_RESEARCH_ONLY'}
            chunks.append(record)
            edges.append({'src':pid,'dst':cid,'type':'PARENT_HAS_CHILD'})
            group.clear()
        for u in units:
            if group and approx_tokens('\n\n'.join(x['text'] for x in group+[u])) > target:
                flush()
            group.append(u)
        flush()
    for bid, length in lengths.items():
        spans=sorted(covered.get(bid, []))
        cursor=0
        for st,en in spans:
            if st != cursor or en <= st or en > length:
                raise ValueError(f'{did}/{bid} 原文覆盖不连续: {cursor}->{st}:{en}')
            cursor=en
        if cursor!=length:
            raise ValueError(f'{did}/{bid} 原文被遗漏 {cursor}/{length}')
    by_parent=defaultdict(list)
    for c in chunks:
        by_parent[c['parent_id']].append(c)
    for pcs in by_parent.values():
        for i,c in enumerate(pcs):
            c['previous_chunk_id']=pcs[i-1]['chunk_id'] if i else ''
            c['next_chunk_id']=pcs[i+1]['chunk_id'] if i+1 < len(pcs) else ''
            if i:
                edges.append({'src':pcs[i-1]['chunk_id'],'dst':c['chunk_id'],'type':'NEXT_IN_SECTION'})
    return parents,chunks,edges,block_to_chunks,sum(lengths.values()),sum(e-s for spans in covered.values() for s,e in spans)


def table_row_text(row, index: int) -> str:
    return f'Row {index+1}: ' + ' | '.join(str(c.get('text','')) for c in row)


def build_media(doc:dict, target:int, ceiling:int, block_to_chunks: dict):
    did=doc['document_id']
    parents,chunks,edges,registry=[],[],[],[]
    table_rows=table_rows_covered=0
    for mtype,key in [('table','tables'),('figure','figures')]:
        for media in doc.get('media',{}).get(key) or []:
            ident=str(media.get('id') or '')
            if not ident:
                raise ValueError(f'{did} 媒体缺少ID: {mtype}')
            mid=f'{did}:{mtype}:{ident}'
            pid=mid+':parent'
            graphics=media.get('graphics') or []
            registry.append({'media_id':mid,'document_id':did,'type':mtype,'jats_id':media.get('jats_id',''),
                             'section_id':media.get('section_id',''),'caption':media.get('caption',''),
                             'image_files':[{'xlink_href':g.get('xlink_href',''), 'local_path':g.get('local_path',''),
                                             'status':g.get('status','')} for g in graphics],
                             'visual_interpretation_status':'NOT_INTERPRETED',
                             'media_rights':'THIRD_PARTY_REVIEW_REQUIRED'})
            parents.append({'parent_id':pid,'document_id':did,'node_type':mtype,
                            'section_id':media.get('section_id',''), 'section_heading_path':media.get('section_heading_path',[]),
                            'content':media,'rights':doc.get('rights',{}),
                            'source_xml_sha256':doc.get('provenance',{}).get('source_xml_sha256','')})
            edges.append({'src':did,'dst':pid,'type':'DOCUMENT_HAS_PARENT'})
            edges.append({'src':pid,'dst':mid,'type':'PARENT_OF_MEDIA'})
            if media.get('section_id'):
                edges.append({'src':f"{did}:parent:body:{media['section_id']}",'dst':pid,'type':'SECTION_CONTAINS_MEDIA'})
            caption='\n'.join(str(x) for x in (media.get('label',''),media.get('caption','')) if x)
            if mtype=='figure':
                alts='\n'.join(str(x) for x in media.get('alt_text') or [] if x)
                value='\n'.join(x for x in (caption, alts) if x)
                if value:
                    cid=mid+':caption'
                    chunks.append({'chunk_id':cid,'document_id':did,'parent_id':pid,
                                   'content_kind':'figure_caption','text':value,
                                   'embedding_text':'\n'.join(x for x in (doc.get('title',''),value) if x),
                                   'section_id':media.get('section_id',''),
                                   'media_id':mid,'token_count_estimate':approx_tokens(value),
                                   'actual_tokenizer_checked':False,'image_data_read':False,
                                   'rights_status':'CANDIDATE_LOCAL_RESEARCH_ONLY'})
                    edges.append({'src':pid,'dst':cid,'type':'PARENT_HAS_CHILD'})
            else:
                rows=media.get('rows') or []
                table_rows+=len(rows)
                header_indices=[]
                for i,row in enumerate(rows):
                    if any(bool(c.get('is_header')) for c in row):
                        header_indices.append(i)
                    else:
                        break
                meta='\n'.join(x for x in (caption, 'Footnotes: '+str(media.get('footnotes')) if media.get('footnotes') else '') if x)
                header='\n'.join(table_row_text(rows[i],i) for i in header_indices)
                data_indices=[i for i in range(len(rows)) if i not in header_indices]
                table_rows_covered+=len(header_indices)
                groups=[]; current=[]
                def table_flush():
                    if not current:
                        return
                    groups.append(current[:]);current.clear()
                for row_i in data_indices:
                    row_txt=table_row_text(rows[row_i],row_i)
                    if current:
                        candidate='\n'.join(table_row_text(rows[k],k) for k in current+[row_i])
                        if approx_tokens('\n'.join((meta,header,candidate)))>target:
                            table_flush()
                    current.append(row_i)
                table_flush()
                if not groups:
                    groups=[[]]
                indexes_seen=set(header_indices)
                for k,group in enumerate(groups,1):
                    indexes_seen.update(group)
                    data='\n'.join(table_row_text(rows[i],i) for i in group)
                    value='\n'.join(x for x in (caption,header,data,'Footnotes: '+str(media.get('footnotes') or '') if media.get('footnotes') else '') if x)
                    cid=f'{mid}:rows:{k:04d}'
                    chunks.append({'chunk_id':cid,'document_id':did,'parent_id':pid,
                                   'content_kind':'table','text':value,
                                   'embedding_text':'\n'.join(x for x in (doc.get('title',''),value) if x),
                                   'media_id':mid,'table_row_indices':group,'table_header_indices':header_indices,
                                   'section_id':media.get('section_id',''),
                                   'token_count_estimate':approx_tokens(value),
                                   'actual_tokenizer_checked':False,
                                   'oversized_for_pilot':approx_tokens(value)>ceiling,
                                   'data_source':'ORIGINAL_STRUCTURED_TABLE_OR_CAPTION',
                                   'rights_status':'CANDIDATE_LOCAL_RESEARCH_ONLY'})
                    edges.append({'src':pid,'dst':cid,'type':'PARENT_HAS_CHILD'})
                if indexes_seen!=set(range(len(rows))):
                    raise ValueError(f'{mid} 结构化表格行不完整')
                table_rows_covered+=len(data_indices)
            ref_seen=set()
            for ref in media.get('referenced_by') or []:
                for cid in block_to_chunks.get(ref.get('source_block_id',''), set()):
                    if (cid,mid) not in ref_seen:
                        ref_seen.add((cid,mid))
                        edges.append({'src':cid,'dst':mid,'type':'TEXT_REFERENCES_MEDIA',
                                      'jats_id':media.get('jats_id','')})
    return parents,chunks,edges,registry,table_rows,table_rows_covered


def build_one(doc:dict, target:int, ceiling:int):
    did=doc['document_id']
    tp,tc,te,block_map,source_chars,covered_chars=build_text(doc,target,ceiling)
    mp,mc,me,registry,tr,cr=build_media(doc,target,ceiling,block_map)
    parents=tp+mp;children=tc+mc;edges=te+me
    all_ids=[c['chunk_id'] for c in children]
    if len(all_ids)!=len(set(all_ids)):
        raise ValueError(f'{did}: child IDs duplicated')
    parent_ids={p['parent_id'] for p in parents}
    if any(c['parent_id'] not in parent_ids for c in children):
        raise ValueError(f'{did}: child parent missing')
    docrec={'document_id':did,'source':'PMC','title':doc.get('title',''),
            'pmid':doc.get('pmid',''),'doi':doc.get('doi',''),
            'publication_year':doc.get('publication_year'),
            'publication_types_raw':doc.get('publication_types_raw',''),
            'source_topic_tags':doc.get('source_topic_tags',[]),
            'disease_ontology_ids':doc.get('disease_ontology_ids',[]),
            'rights':doc.get('rights',{}),'provenance':doc.get('provenance',{}),
            'original_enriched_source':'data/processed/enriched/pmc_articles_with_media_v2.jsonl',
            'full_source_available':True,'scope':'LOCAL_RESEARCH_CANDIDATE'}
    summary={'document_id':did,'domain':';'.join(doc.get('source_topic_tags') or []),
             'body_chars':source_chars,'covered_chars':covered_chars,
             'parents':len(parents),'text_chunks':len(tc),'table_chunks':sum(c['content_kind']=='table' for c in mc),
             'figure_caption_chunks':sum(c['content_kind']=='figure_caption' for c in mc),
             'table_rows':tr,'table_rows_covered':cr,
             'oversized_table_chunks':sum(c.get('oversized_for_pilot',False) for c in mc),
             'figure_objects':len(doc.get('media',{}).get('figures') or []),
             'text_verified':source_chars==covered_chars,'table_verified':tr==cr}
    if not summary['text_verified'] or not summary['table_verified']:
        raise ValueError(f'{did} 分块完整性失败')
    return docrec,parents,children,edges,registry,summary


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    p=ap.add_mutually_exclusive_group()
    p.add_argument('--sample',type=int,default=20,help='按疾病域和文章长短抽样，默认20篇')
    p.add_argument('--all',action='store_true',help='处理全部921篇，建议验收小样本后使用')
    ap.add_argument('--target-tokens',type=int,default=380,help='启发式估算token的目标长度')
    ap.add_argument('--max-tokens',type=int,default=600,help='单块长段落拆分估算上限（非BGE tokenizer）')
    args=ap.parse_args()
    if args.sample<1 or args.target_tokens<80 or args.max_tokens < args.target_tokens:
        ap.error('--sample>=1, --target-tokens>=80, --max-tokens>=target-tokens')
    root=args.root.expanduser().resolve()
    cand=read_candidates(root/'data/processed/eligibility/pmc_candidate_manifest.csv')
    docs=load_articles(root/'data/processed/enriched/pmc_articles_with_media_v2.jsonl', cand)
    chosen=docs if args.all else pick_diverse(docs,min(len(docs),args.sample))
    folder=root/'data/processed/knowledge'/('v3_all' if args.all else f'v3_pilot_{len(chosen)}')
    files={n:folder/f'{n}.jsonl' for n in ('documents','parent_units','retrieval_chunks','evidence_relations','media_registry')}
    report=root/'reports'/('hierarchical_chunk_v3_all.csv' if args.all else f'hierarchical_chunk_v3_pilot_{len(chosen)}.csv')
    summary=root/'reports'/('hierarchical_chunk_v3_all_summary.txt' if args.all else f'hierarchical_chunk_v3_pilot_{len(chosen)}_summary.txt')
    folder.mkdir(parents=True,exist_ok=True);report.parent.mkdir(parents=True,exist_ok=True)
    tmp={n:p.with_name(p.name+'.tmp') for n,p in files.items()}
    tmp_report=report.with_name(report.name+'.tmp')
    streams={}
    try:
        for n,p in tmp.items():
            streams[n]=p.open('w',encoding='utf-8',newline='\n')
        fields=['document_id','domain','body_chars','covered_chars','parents','text_chunks','table_chunks',
                'figure_caption_chunks','table_rows','table_rows_covered','oversized_table_chunks',
                'figure_objects','text_verified','table_verified']
        with tmp_report.open('w',encoding='utf-8-sig',newline='') as csvout:
            writer=csv.DictWriter(csvout,fieldnames=fields);writer.writeheader()
            totals=Counter()
            for n,doc in enumerate(chosen,1):
                docrec,parents,children,edges,media,st=build_one(doc,args.target_tokens,args.max_tokens)
                for filename,records in [('documents',[docrec]),('parent_units',parents),
                                         ('retrieval_chunks',children),('evidence_relations',edges),('media_registry',media)]:
                    for rec in records:
                        streams[filename].write(json.dumps(rec,ensure_ascii=False,separators=(',',':'))+'\n')
                writer.writerow(st)
                totals.update({k:int(v) for k,v in st.items() if isinstance(v,int) and not isinstance(v,bool)})
                if n%5==0:
                    print(f'[进度] {n}/{len(chosen)} 篇, 正文chunks={totals["text_chunks"]}, 表格chunks={totals["table_chunks"]}')
        for f in streams.values():f.close()
        for n,p in files.items():os.replace(tmp[n],p)
        os.replace(tmp_report,report)
        lines=[
            '=== MedEvidence Hierarchical Evidence Chunk V3 原型验收 ===',
            f'时间UTC: {datetime.now(timezone.utc).isoformat(timespec="seconds")}',
            f'模式: {"ALL" if args.all else "PILOT"}',
            f'处理文章: {len(chosen)} / {len(docs)}',
            f'正文原始字符: {totals["body_chars"]}',
            f'正文覆盖字符: {totals["covered_chars"]}',
            f'正文Chunk: {totals["text_chunks"]}',
            f'表格Chunk: {totals["table_chunks"]}',
            f'图注Chunk: {totals["figure_caption_chunks"]}',
            f'表格行覆盖: {totals["table_rows_covered"]} / {totals["table_rows"]}',
            f'超估算token上限的表格块: {totals["oversized_table_chunks"]}',
            f'父节点: {totals["parents"]}',
            f'目录: {folder.relative_to(root)}',
            f'逐篇报告: {report.relative_to(root)}',
            '注意：仅保证已解析的摘要/正文字符覆盖及结构化表格行覆盖；图像像素、补充材料未自动解释。',
            '此阶段为PMC候选文献原型，WHO PDF/CDC TXT须另建解析适配器。',
            'token数仅为估计值，建立BGE-M3索引前必须用实际tokenizer复核。',
            'CANDIDATE是本地科研候选，不代表媒体内容具备可再分发授权。',
        ]
        summary.write_text('\n'.join(lines)+'\n',encoding='utf-8-sig')
        print('\n'.join(lines))
        return 0
    finally:
        for stream in streams.values():
            if not stream.closed:stream.close()
        for p in (*tmp.values(),tmp_report):
            if p.exists():p.unlink()


if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError,KeyError,TypeError,json.JSONDecodeError) as e:
        print(f'[ERROR] {type(e).__name__}: {e}',file=sys.stderr)
        sys.exit(2)
