"""V1c.2 operator: plan / build / verify. No publish and no old-index overwrites."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from v1c2_common import snapshot, bm25_files, dense_files


def plan(root,version):
    folder,idx,m,docs,chunks,hashes=snapshot(root,version)
    b,bm=bm25_files(idx);d,dm=dense_files(idx)
    return {'version':version,'snapshot':str(folder),'paper_count':len(docs),
        'chunk_count':len(chunks),'chunks_sha256':m['chunks_sha256'],
        'bm25':{'db':str(b),'db_exists':b.exists(),'manifest_exists':bm.exists()},
        'dense':{'dir':str(d),'dir_exists':d.exists(),'manifest_exists':dm.exists()},
        'advice':'独立索引、只读源快照。不自动发布。旧版 legacy Dense 不可复用。'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--version',required=True)
    sub=p.add_subparsers(dest='action',required=True)
    sub.add_parser('plan')
    b=sub.add_parser('build')
    b.add_argument('--model-path',type=Path,required=True)
    b.add_argument('--reuse-from',default=None)
    b.add_argument('--device',choices=('cuda','cpu'),default='cuda')
    b.add_argument('--batch-size',type=int,default=2)
    b.add_argument('--write-batch-size',type=int,default=128)
    b.add_argument('--max-length',type=int,default=768)
    v=sub.add_parser('verify')
    v.add_argument('--with-vectors',action='store_true')
    a=p.parse_args()
    if a.action=='plan':
        print(json.dumps(plan(a.root,a.version),ensure_ascii=False,indent=2));return
    if a.action=='verify':
        from validate_index_version_v1c2 import validate
        result=validate(a.root,a.version,with_vectors=a.with_vectors)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if result['errors']:raise SystemExit(2)
        return
    from build_bm25_version import build as build_bm25
    from build_dense_version import build as build_dense
    old=plan(a.root,a.version)
    if old['bm25']['db_exists'] != old['bm25']['manifest_exists']:
        raise RuntimeError('BM25 中断状态：索引文件与 manifest 不一致；请先人工检查')
    if old['dense']['dir_exists'] != old['dense']['manifest_exists']:
        raise RuntimeError('Dense 中断状态：目录与 manifest 不一致；请先人工检查')
    if not old['bm25']['db_exists']:
        print('开始创建 BM25 独立索引 ...',flush=True)
        build_bm25(a.root,a.version)
    if not old['dense']['dir_exists']:
        print('开始创建 Dense 独立索引 ...',flush=True)
        build_dense(a.root,a.version,a.model_path,reuse_from=a.reuse_from,
             device=a.device,batch_size=a.batch_size,max_length=a.max_length,
             write_batch_size=a.write_batch_size)
    from validate_index_version_v1c2 import validate
    r=validate(a.root,a.version,with_vectors=False)
    print(json.dumps(r,ensure_ascii=False,indent=2))
    if r['errors']:raise RuntimeError('索引未通过一致性验证；不会发布')
    print('技术一致性通过。仍为隔离试验版本，尚未发布。')

if __name__=='__main__':
    main()
