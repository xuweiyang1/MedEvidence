"""Strict V1c.2 parity validation. No index or active-version writes.

This proves stored IDs, payload hashes, and model configuration provenance;
not independent numerical reproducibility of every BGE-M3 vector, medical quality, or licenses.
"""
from __future__ import annotations
import argparse
import json
import math
import sqlite3
from pathlib import Path
from snapshot_core_v1c1 import text_hash, file_hash
from v1c2_common import snapshot, bm25_files, dense_files, point_uuid, canonical_sha, REV_RE


def validate_bm25(root:Path, version:str):
    folder,index,m,docs,chunks,hashes = snapshot(root,version)
    db,mf=bm25_files(index)
    errs=[]
    if not db.is_file() or not mf.is_file():
        return ['BM25_FILE_OR_MANIFEST_MISSING'],{}
    rec=json.loads(mf.read_text('utf-8'))
    required = {'corpus_version':version,'chunk_count':len(chunks),'chunks_sha256':m['chunks_sha256'],
                'documents_sha256':m['documents_sha256'],'chunk_id_set_sha256':m['chunk_id_set_sha256']}
    errs += [f'BM25_MANIFEST_{key}' for key,value in required.items() if rec.get(key)!=value]
    if file_hash(db) != rec.get('db_sha256'):
        errs.append('BM25_DATABASE_FILE_HASH_CHANGED')
    uri=db.resolve().as_uri()+'?mode=ro'
    con=sqlite3.connect(uri,uri=True)
    try:
        if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            errs.append('BM25_DATABASE_INTEGRITY')
        meta=dict(con.execute('SELECT key,value FROM index_meta'))
        for k,val in {'corpus_version':version,'chunk_count':str(len(chunks)),
                      'chunks_sha256':m['chunks_sha256'],'documents_sha256':m['documents_sha256'],
                      'chunk_id_set_sha256':m['chunk_id_set_sha256']}.items():
            if meta.get(k)!=val: errs.append('BM25_INDEX_META_'+k)
        seen={}
        for cid,did,txt in con.execute('SELECT chunk_id,document_id,body_text FROM chunks'):
            if cid in seen: errs.append('BM25_DUPLICATE_ID_'+cid)
            seen[cid]=(did,text_hash(txt))
        if set(seen)!=set(chunks): errs.append('BM25_ID_SET_MISMATCH')
        bad=[cid for cid in set(seen)&set(chunks) if seen[cid] != (chunks[cid]['document_id'],hashes[cid]['content_hash'])]
        if bad: errs.append(f'BM25_CONTENT_HASH_MISMATCH_{len(bad)}_first_{bad[0]}')
        if con.execute('SELECT count(*) FROM fts_chunks').fetchone()[0]!=len(chunks):
            errs.append('BM25_FTS_COUNT_MISMATCH')
        return errs, {'checked_ids':len(seen),'fts_rows':con.execute('SELECT count(*) FROM fts_chunks').fetchone()[0]}
    finally:
        con.close()


def validate_dense(root:Path, version:str, *, with_vectors=False):
    from qdrant_client import QdrantClient
    folder,index,m,docs,chunks,hashes=snapshot(root,version)
    qdir,mf=dense_files(index)
    errs=[]
    if not qdir.is_dir() or not mf.is_file():
        return ['DENSE_DIR_OR_MANIFEST_MISSING'],{}
    rec=json.loads(mf.read_text('utf-8'))
    required = {'corpus_version':version,'chunk_count':len(chunks),'chunks_sha256':m['chunks_sha256'],
                'documents_sha256':m['documents_sha256'],'chunk_id_set_sha256':m['chunk_id_set_sha256']}
    errs += ['DENSE_MANIFEST_'+key for key,val in required.items() if rec.get(key)!=val]
    identity=rec.get('model_identity',{})
    fingerprint=rec.get('embedding_fingerprint')
    if not identity or canonical_sha(identity)!=fingerprint:
        errs.append('DENSE_FINGERPRINT_INVALID')
    if not REV_RE.fullmatch(identity.get('revision','')):
        errs.append('DENSE_REVISION_NOT_PINNED')
    if identity.get('input_field')!=m['embedding']['input_field']:
        errs.append('DENSE_INPUT_FIELD_MISMATCH')
    if identity.get('max_length')!=m['embedding']['max_length']:
        errs.append('DENSE_MAX_LENGTH_MISMATCH')
    if identity.get('model')!=m['embedding']['model']:
        errs.append('DENSE_MODEL_MISMATCH')
    if identity.get('dimension')!=1024 or identity.get('distance')!='Cosine':
        errs.append('DENSE_VECTOR_CONFIGURATION_MISMATCH')
    if identity.get('input_transform')!=m['embedding']['input_transform']:
        errs.append('DENSE_INPUT_TRANSFORM_MISMATCH')
    if identity.get('parser_version')!=m['parser_version'] or identity.get('chunker_version')!=m['chunker_version']:
        errs.append('DENSE_CHUNKER_MISMATCH')
    if identity.get('normalization_version')!=m['normalization_version']:
        errs.append('DENSE_NORMALIZATION_VERSION_MISMATCH')
    collection=rec.get('collection')
    if collection != 'bge_m3_'+version:
        errs.append('DENSE_COLLECTION_NAME_MISMATCH')
    seen=set()
    client=QdrantClient(path=str(qdir))
    try:
        if not client.collection_exists(collection):
            return errs+['DENSE_COLLECTION_MISSING'],{}
        count=client.count(collection_name=collection,exact=True).count
        if count!=len(chunks): errs.append('DENSE_POINT_COUNT_MISMATCH')
        offset=None
        problems=[]
        while True:
            pts,offset=client.scroll(collection_name=collection,offset=offset,limit=256,
                 with_payload=True,with_vectors=with_vectors)
            for p in pts:
                data=p.payload or {}
                cid=data.get('chunk_id')
                if cid not in chunks or cid in seen:
                    problems.append('missing_or_duplicate_chunk_id_'+str(cid));continue
                seen.add(cid)
                expected={'document_id':chunks[cid]['document_id'],
                   'content_hash':hashes[cid]['content_hash'],
                   'embedding_input_hash':hashes[cid]['embedding_input_hash'],
                   'embedding_fingerprint':fingerprint,'corpus_version':version}
                if str(p.id)!=point_uuid(cid): problems.append('uuid_'+cid)
                if any(data.get(k)!=v for k,v in expected.items()):
                    problems.append('payload_'+cid)
                if with_vectors:
                    vec=p.vector
                    if not isinstance(vec,(list,tuple)) or len(vec)!=1024 or not all(math.isfinite(float(v)) for v in vec):
                        problems.append('vector_'+cid)
            if offset is None:break
        if seen!=set(chunks):errs.append('DENSE_ID_SET_MISMATCH')
        if problems:errs.append('DENSE_POINT_MISMATCH_COUNT_'+str(len(problems))+'_sample_'+','.join(problems[:3]))
        return errs,{'scanned_points':len(seen),'collection':collection,'with_vectors':bool(with_vectors)}
    finally:
        client.close()


def validate(root:Path,version:str,*,with_vectors=False):
    folder,index,m,docs,chunks,hashes=snapshot(root,version)
    eb,db=validate_bm25(root,version)
    ed,dd=validate_dense(root,version,with_vectors=with_vectors)
    errors=eb+ed
    return {'schema':'MedEvidence_IndexValidation_V1c2','version':version,
            'snapshot_chunks_sha256':m['chunks_sha256'],'paper_count':len(docs),
            'chunk_count':len(chunks), 'errors':errors,'bm25':db,'dense':dd,
            'technical_index_parity':not errors,
            'medical_evidence_approved':False,'license_approved':False,
            'can_publish':False,
            'note':'技术一致性通过也不自动发布；不证明向量数值或临床证据正确。'}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    ap.add_argument('--version',required=True)
    ap.add_argument('--with-vectors',action='store_true',help='额外检查所有向量的维度和有限数值')
    a=ap.parse_args()
    result=validate(a.root,a.version,with_vectors=a.with_vectors)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result['errors']:
        raise SystemExit(2)

if __name__=='__main__':
    main()
