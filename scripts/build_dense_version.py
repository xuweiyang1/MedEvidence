"""Pinned-model BGE-M3 build into NEW local Qdrant directory; reuse only V1c.2-verified vectors.

No network download. No --reset. Legacy Qdrant is deliberately ineligible for reuse.
"""
from __future__ import annotations
import argparse
import json
import math
import os
import shutil
import uuid
from pathlib import Path
from snapshot_core_v1c1 import dense_input
from v1c2_common import (snapshot, paths, dense_files, point_uuid, model_identity, atomic_json)


def load_model(path: Path, device: str):
    os.environ['USE_TF'] = '0'
    os.environ['USE_TORCH'] = '1'
    import torch
    if device == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('未检测到可用 CUDA，请检查 PyTorch')
    from FlagEmbedding import BGEM3FlagModel
    return BGEM3FlagModel(str(path), use_fp16=(device == 'cuda'), devices=['cuda:0' if device == 'cuda' else 'cpu'])


def encode(model, texts, batch_size: int, max_length: int):
    values = model.encode(texts, batch_size=batch_size, max_length=max_length,
                          return_dense=True, return_sparse=False, return_colbert_vecs=False)['dense_vecs']
    if len(values) != len(texts):
        raise ValueError('BGE-M3 返回向量数不一致')
    out = []
    for vector in values:
        v = vector.tolist()
        if len(v) != 1024 or not all(math.isfinite(x) for x in v):
            raise ValueError('BGE-M3 向量维度或数值异常')
        out.append(v)
    return out


def payload(ch, hashes, cid, fp, version):
    return {'chunk_id':cid,'document_id':ch['document_id'],
            'content_hash':hashes[cid]['content_hash'],
            'embedding_input_hash':hashes[cid]['embedding_input_hash'],
            'embedding_fingerprint':fp,'corpus_version':version}


def build(root: Path, version: str, model_path: Path, *, reuse_from: str | None = None,
          device='cuda', batch_size=2, write_batch_size=128, max_length=768, model_loader=load_model,
          encoder=encode):
    from qdrant_client import QdrantClient, models
    if batch_size < 1 or write_batch_size < 1:
        raise ValueError('batch sizes must be positive')
    folder, idx, m, docs, chunks, hashes = snapshot(root, version)
    dest, report_file = dense_files(idx)
    if dest.exists() or report_file.exists():
        raise FileExistsError('新版本 Dense 目录或 Manifest 已存在，拒绝覆盖；不要使用 --reset')
    model_path = model_path.expanduser().resolve()
    identity, fp = model_identity(model_path, m, device=device, max_length=max_length)
    collection = 'bge_m3_' + version
    prev = None
    prev_collection = ''
    reused_ids = set()
    idx.mkdir(parents=True, exist_ok=True)
    temp_dir = idx / ('.qdrant.build_' + uuid.uuid4().hex[:12])
    # Verify reusable old index BEFORE opening new collection.
    if reuse_from:
        if reuse_from == version:
            raise ValueError('不能从相同版本复用向量')
        old_folder, old_idx, old_m, old_docs, old_chunks, old_hashes = snapshot(root, reuse_from)
        old_db, old_report = dense_files(old_idx)
        if not old_report.is_file() or not old_db.is_dir():
            raise ValueError('旧版本不是 V1c.2 建成的索引，拒绝复用旧 Legacy 向量')
        old_metadata = json.loads(old_report.read_text('utf-8'))
        if old_metadata.get('embedding_fingerprint') != fp or old_metadata.get('model_identity') != identity:
            raise ValueError('旧版与新版模型文件/切分/环境/参数指纹不一致，禁止复用')
        from validate_index_version_v1c2 import validate_dense
        old_errors, _ = validate_dense(root, reuse_from, with_vectors=True)
        if old_errors:
            raise ValueError('旧版本 Dense 验证未通过，禁止复用: ' + '; '.join(old_errors[:3]))
        prev_collection = old_metadata['collection']
        prev = old_db

    new_client = None
    old_client = None
    success = False
    inserted = 0
    reused = 0
    encoded = 0
    try:
        new_client = QdrantClient(path=str(temp_dir))
        new_client.create_collection(collection_name=collection,
           vectors_config=models.VectorParams(size=1024,distance=models.Distance.COSINE))
        pending = []
        def write():
            nonlocal inserted
            if pending:
                new_client.upsert(collection_name=collection,points=pending[:],wait=True)
                inserted += len(pending)
                pending.clear()
        if prev:
            old_client = QdrantClient(path=str(prev))
            offset = None
            while True:
                points, offset = old_client.scroll(collection_name=prev_collection,limit=write_batch_size,
                        offset=offset,with_payload=True,with_vectors=True)
                for point in points:
                    p = point.payload or {}
                    cid = p.get('chunk_id')
                    if cid not in chunks:
                        continue  # deleted chunk
                    expected = payload(chunks[cid],hashes,cid,fp,version)
                    if any(p.get(k) != expected[k] for k in ('chunk_id','document_id','content_hash',
                                        'embedding_input_hash','embedding_fingerprint')):
                        continue  # changed text / ID -> re-encode
                    if str(point.id) != point_uuid(cid):
                        raise ValueError('旧索引 UUID 无法验证: ' + cid)
                    vector = point.vector
                    if not isinstance(vector, (list,tuple)) or len(vector) != 1024:
                        raise ValueError('旧向量格式异常: ' + cid)
                    if not all(math.isfinite(float(x)) for x in vector):
                        raise ValueError('旧向量包含非有限数: ' + cid)
                    pending.append(models.PointStruct(id=point_uuid(cid),vector=vector,payload=expected))
                    reused_ids.add(cid)
                    reused += 1
                    if len(pending) >= write_batch_size:
                        write()
                if offset is None: break
            write()
            old_client.close(); old_client=None
        need = [cid for cid in sorted(chunks) if cid not in reused_ids]
        del reused_ids  # avoid accidental retention during GPU operation
        if need:
            model = model_loader(model_path, device)
            for start in range(0,len(need),write_batch_size):
                ids = need[start:start+write_batch_size]
                texts = [dense_input(chunks[cid]['text']) for cid in ids]
                vecs = encoder(model,texts,batch_size,max_length)
                for cid, vec in zip(ids,vecs):
                    pending.append(models.PointStruct(id=point_uuid(cid),vector=vec,
                                  payload=payload(chunks[cid],hashes,cid,fp,version)))
                encoded += len(ids)
                write()
                print(f'BGE-M3 进度 {reused+encoded}/{len(chunks)}（复用 {reused}，新编码 {encoded}）',flush=True)
        total = new_client.count(collection_name=collection,exact=True).count
        if total != len(chunks) or total != inserted or reused+encoded != total:
            raise RuntimeError(f'Qdrant 数量错误: count={total}, inserted={inserted}, expected={len(chunks)}')
        new_client.close();new_client=None
        if dest.exists():
            raise FileExistsError(dest)
        os.rename(temp_dir,dest)
        atomic_json(report_file, {
            'schema':'MedEvidence_Dense_IndexManifest_V1c2','status':'BUILT_UNPUBLISHED',
            'corpus_version':version,'collection':collection,
            'chunk_count':len(chunks),'chunks_sha256':m['chunks_sha256'],
            'documents_sha256':m['documents_sha256'],
            'chunk_id_set_sha256':m['chunk_id_set_sha256'],
            'embedding_fingerprint':fp,'model_identity':identity,
            'reused_chunks':reused,'encoded_chunks':encoded,'reused_from':reuse_from or None,
            'verification_note':'Payload/ID verified; validator cannot independently prove model numerical correctness.'})
        success=True
        return dest, {'reused':reused,'encoded':encoded,'collection':collection,'fingerprint':fp}
    finally:
        if old_client is not None: old_client.close()
        if new_client is not None: new_client.close()
        if not success and temp_dir.exists():
            shutil.rmtree(temp_dir,ignore_errors=True)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    ap.add_argument('--version',required=True)
    ap.add_argument('--model-path',type=Path,required=True,help='HF 缓存 snapshots/<完整40位commit> 的目录')
    ap.add_argument('--reuse-from',default=None,help='已严格验证的 V1c.2 基线版本')
    ap.add_argument('--device',choices=('cuda','cpu'),default='cuda')
    ap.add_argument('--batch-size',type=int,default=2)
    ap.add_argument('--max-length',type=int,default=768)
    ap.add_argument('--write-batch-size',type=int,default=128)
    a=ap.parse_args()
    p, info = build(a.root,a.version,a.model_path,reuse_from=a.reuse_from,
         device=a.device,batch_size=a.batch_size,max_length=a.max_length,write_batch_size=a.write_batch_size)
    print('独立 Dense 索引:',p,'\n构建统计:',json.dumps(info,ensure_ascii=False))

if __name__=='__main__':
    main()
