#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only V1c.1 snapshot / BM25 / Qdrant parity validator.

Legacy Qdrant has no per-point full content hash, embedding fingerprint or pinned
model revision. Even if all IDs and truncated previews match, legacy vectors CANNOT
be certified as full-content / full-embedding-equivalent. No activation or writes.
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from snapshot_core_v1c1 import VERSION_RE, dense_input, file_hash, load_snapshot, rows, text_hash


def issue(code, message, examples=None):
    out = {'code': code, 'message': message}
    if examples:
        out['examples'] = examples[:5]
    return out


def validate_structure(snapshot: Path):
    manifest, docs, chunks, hashes, stats = load_snapshot(snapshot)
    errors = []
    warnings = []
    parent_path = snapshot / 'parent_units.jsonl'
    if parent_path.is_file():
        parents = {p['parent_id']: p for p in rows(parent_path)}
        for cid, ch in chunks.items():
            if ch.get('parent_id') not in parents:
                errors.append(issue('MISSING_PARENT', cid))
            elif parents[ch['parent_id']].get('document_id') != ch['document_id']:
                errors.append(issue('PARENT_DOCUMENT_MISMATCH', cid))
            for field in ('previous_chunk_id', 'next_chunk_id'):
                sibling = ch.get(field)
                if sibling and (sibling not in chunks or chunks[sibling].get('parent_id') != ch['parent_id']):
                    errors.append(issue('INVALID_SIBLING', f'{cid} {field}={sibling}'))
        known = set(docs) | set(parents) | set(chunks)
        media_file = snapshot / 'media_registry.jsonl'
        if media_file.is_file():
            known.update(x['media_id'] for x in rows(media_file))
        edges = snapshot / 'evidence_relations.jsonl'
        if edges.is_file():
            for edge in rows(edges):
                if edge.get('src') not in known or edge.get('dst') not in known:
                    errors.append(issue('DANGLING_EDGE', str(edge)[:220]))
    else:
        warnings.append(issue('NO_PARENT_FILE', '缺少 Parent 结构；不能验证 Chunk → Parent 追溯'))
    return manifest, docs, chunks, hashes, stats, errors, warnings


def validate_bm25(path: Path, version: str, chunks: dict, hashes: dict):
    errors, warnings = [], []
    if not path.is_file():
        return [issue('BM25_NOT_FOUND', str(path))], warnings, {}
    conn = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)
    try:
        integrity = conn.execute('PRAGMA quick_check').fetchone()[0]
        if integrity != 'ok':
            errors.append(issue('BM25_SQLITE_CORRUPT', integrity))
        metadata = dict(conn.execute('SELECT key,value FROM index_meta'))
        if metadata.get('corpus_version') != version:
            errors.append(issue('BM25_VERSION_MISMATCH', f'{metadata.get("corpus_version")} != {version}'))
        indexed = {}
        for cid, did, txt in conn.execute('SELECT chunk_id, document_id, body_text FROM chunks'):
            if cid in indexed:
                errors.append(issue('BM25_DUPLICATE_ID', cid))
            indexed[cid] = (did, text_hash(txt))
        expected_ids = set(chunks)
        found = set(indexed)
        if expected_ids != found:
            errors.append(issue('BM25_ID_MISMATCH', f'缺少={len(expected_ids-found)} 多出={len(found-expected_ids)}',
                                sorted(expected_ids-found)[:3] + sorted(found-expected_ids)[:3]))
        bad_content = [cid for cid in sorted(expected_ids & found)
                       if indexed[cid] != (chunks[cid]['document_id'], hashes[cid]['content_hash'])]
        if bad_content:
            errors.append(issue('BM25_CONTENT_MISMATCH', f'{len(bad_content)} 条正文或 document_id 不一致', bad_content))
        fts_count = conn.execute('SELECT COUNT(*) FROM fts_chunks').fetchone()[0]
        if fts_count != len(chunks):
            errors.append(issue('BM25_FTS_COUNT', f'{fts_count} != {len(chunks)}'))
        if metadata.get('chunk_count') != str(len(chunks)):
            errors.append(issue('BM25_METADATA_COUNT', f'{metadata.get("chunk_count")} != {len(chunks)}'))
        # Legacy index_meta does not include source content digest, but exact
        # body_text hashes per row prove the current BM25 corpus mapping.
        return errors, warnings, {'chunk_count': len(indexed), 'fts_count': fts_count,
                                  'corpus_version': metadata.get('corpus_version'),
                                  'full_content_verified': not errors}
    finally:
        conn.close()


def validate_dense(index_dir: Path, collection: str, manifest: dict, chunks: dict, hashes: dict,
                   *, batch_size=512):
    errors, warnings = [], []
    legacy_manifest_file = index_dir / 'medevidence_dense_manifest_v1.json'
    if not legacy_manifest_file.is_file():
        return [issue('DENSE_BUILD_MANIFEST_MISSING', str(legacy_manifest_file))], warnings, {}
    legacy = json.loads(legacy_manifest_file.read_text(encoding='utf-8'))
    expected_raw = manifest['source_file_sha256']
    if legacy.get('corpus_sha256') != expected_raw.get('retrieval_chunks_jsonl'):
        errors.append(issue('DENSE_SOURCE_CORPUS_HASH', 'Dense 建库输入文件哈希与快照来源不一致'))
    if legacy.get('docs_sha256') != expected_raw.get('documents_jsonl'):
        errors.append(issue('DENSE_SOURCE_DOCUMENT_HASH', 'Dense 论文元数据文件哈希与快照来源不一致'))
    if legacy.get('model') != manifest['embedding']['model']:
        errors.append(issue('DENSE_MODEL_MISMATCH', str(legacy.get('model'))))
    if legacy.get('max_length') != manifest['embedding']['max_length']:
        errors.append(issue('DENSE_MAX_LENGTH_MISMATCH', str(legacy.get('max_length'))))
    if legacy.get('dimension') != manifest['embedding']['dimension']:
        errors.append(issue('DENSE_DIMENSION_MISMATCH', str(legacy.get('dimension'))))
    if legacy.get('collection') != collection:
        errors.append(issue('DENSE_COLLECTION_MISMATCH', str(legacy.get('collection'))))
    try:
        from qdrant_client import QdrantClient
    except ImportError as e:
        return errors + [issue('QDRANT_CLIENT_MISSING', '需在 xwy 环境安装已有 qdrant-client: ' + str(e))], warnings, {}
    if not index_dir.is_dir():
        return errors + [issue('DENSE_INDEX_NOT_FOUND', str(index_dir))], warnings, {}
    # Important: Qdrant local mode keeps an exclusive lock on the folder.
    client = QdrantClient(path=str(index_dir))
    try:
        if not client.collection_exists(collection):
            errors.append(issue('COLLECTION_NOT_FOUND', collection))
            return errors, warnings, {}
        actual_count = client.count(collection_name=collection, exact=True).count
        if actual_count != len(chunks):
            errors.append(issue('DENSE_COUNT_MISMATCH', f'{actual_count} != {len(chunks)}'))
        seen = set()
        wrong_doc = []
        wrong_preview = []
        point_hash_missing = 0
        point_hash_wrong = []
        fp_missing = 0
        fp_wrong = []
        wrong_point_uuid = []
        offset = None
        while True:
            points, offset = client.scroll(collection_name=collection, limit=batch_size,
                                           offset=offset, with_payload=True, with_vectors=False)
            for point in points:
                p = point.payload or {}
                cid = p.get('chunk_id')
                if not isinstance(cid, str) or not cid:
                    errors.append(issue('DENSE_EMPTY_CHUNK_ID', str(point.id)))
                    continue
                if cid in seen:
                    errors.append(issue('DENSE_DUPLICATE_CHUNK_ID', cid))
                seen.add(cid)
                expected_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, 'MedEvidence:v3_1:' + cid))
                if str(point.id).lower() != expected_uuid:
                    wrong_point_uuid.append(cid)
                if cid not in chunks:
                    continue
                if p.get('document_id') != chunks[cid]['document_id']:
                    wrong_doc.append(cid)
                # LEGACY preview only; proves at most first 1600 chars.
                if p.get('text_preview') != dense_input(chunks[cid]['text'])[:1600]:
                    wrong_preview.append(cid)
                if p.get('content_hash') is None:
                    point_hash_missing += 1
                elif p['content_hash'] != hashes[cid]['content_hash']:
                    point_hash_wrong.append(cid)
                if not p.get('embedding_fingerprint'):
                    fp_missing += 1
                elif p['embedding_fingerprint'] != manifest['embedding']['embedding_fingerprint']:
                    fp_wrong.append(cid)
            if offset is None:
                break
        expected = set(chunks)
        if seen != expected:
            errors.append(issue('DENSE_ID_MISMATCH', f'缺少={len(expected-seen)}, 多出={len(seen-expected)}',
                                sorted(expected-seen)[:3] + sorted(seen-expected)[:3]))
        if wrong_point_uuid:
            errors.append(issue('DENSE_POINT_UUID_MISMATCH', str(len(wrong_point_uuid)), wrong_point_uuid))
        if fp_wrong:
            errors.append(issue('DENSE_EMBEDDING_FINGERPRINT_MISMATCH', str(len(fp_wrong)), fp_wrong))
        if wrong_doc:
            errors.append(issue('DENSE_DOCUMENT_ID_MISMATCH', str(len(wrong_doc)), wrong_doc))
        if wrong_preview:
            errors.append(issue('DENSE_PREVIEW_MISMATCH', str(len(wrong_preview)), wrong_preview))
        if point_hash_wrong:
            errors.append(issue('DENSE_CONTENT_HASH_MISMATCH', str(len(point_hash_wrong)), point_hash_wrong))
        if point_hash_missing:
            warnings.append(issue('LEGACY_DENSE_NO_FULL_CONTENT_HASH',
                f'{point_hash_missing} 个向量点缺少完整 content_hash；只能核对旧构建输入文件哈希与正文前1600字符'))
        if fp_missing:
            warnings.append(issue('LEGACY_DENSE_NO_FINGERPRINT',
                f'{fp_missing} 个向量点缺少 embedding_fingerprint；禁止未经重新验证就复用到其他模型配置'))
        if not legacy.get('model_revision'):
            warnings.append(issue('LEGACY_DENSE_REVISION_UNPINNED', '旧版 Dense Manifest 未锁定 model_revision'))
        if not legacy.get('corpus_version'):
            warnings.append(issue('LEGACY_DENSE_VERSION_UNSTAMPED', '旧版 Dense Manifest 未明确记录 corpus_version；通过 source SHA 与 collection 名间接核对'))
        elif legacy.get('corpus_version') != manifest['corpus_version']:
            errors.append(issue('DENSE_VERSION_MISMATCH', str(legacy.get('corpus_version'))))
        # NOTE: This script does not retrieve all vectors and recompute embeddings;
        # matching payload/fingerprint cannot prove every float was computed correctly.
        return errors, warnings, {'chunk_count': actual_count, 'scanned_points': len(seen),
                                  'collection': collection, 'source_manifest_hash_matched':
                                  legacy.get('corpus_sha256') == expected_raw.get('retrieval_chunks_jsonl'),
                                  'full_point_hashes_present': point_hash_missing == 0,
                                  'fingerprints_present': fp_missing == 0,
                                  'revision_pinned': bool(legacy.get('model_revision'))}
    finally:
        close = getattr(client, 'close', None)
        if callable(close):
            close()


def report(args):
    root = args.root.resolve()
    snapshot = root / 'data/processed/corpus_snapshots' / args.version
    manifest, docs, chunks, hashes, stats, errors, warnings = validate_structure(snapshot)
    sections = {'snapshot': {'documents': len(docs), 'chunks': len(chunks),
                             'sha256': manifest['chunks_sha256'],
                             'chunk_id_set_sha256': stats['chunk_id_set_sha256']}}
    if manifest.get('corpus_version') != args.version:
        errors.append(issue('SNAPSHOT_VERSION_MISMATCH', str(manifest.get('corpus_version'))))
    if args.with_indexes:
        bm25 = root / 'data/processed/index' / args.version / 'bm25_v1b.sqlite3'
        dense = root / 'data/processed/index' / args.version / 'qdrant_bge_m3_dense'
        e, w, detail = validate_bm25(bm25, args.version, chunks, hashes)
        errors.extend(e); warnings.extend(w); sections['bm25'] = detail
        e, w, detail = validate_dense(dense, args.collection, manifest, chunks, hashes)
        errors.extend(e); warnings.extend(w); sections['dense'] = detail
    # V1c.1 intentionally cannot authorize publication. V1c.2 must also
    # independently attest per-vector hashes, matching fingerprints and revision.
    release_ready = False
    status = 'FAIL' if errors else 'PASS_BUT_NOT_RELEASE_READY'
    output = {'schema': 'MedEvidence_Validation_V1c1',
              'created_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'corpus_version': args.version, 'status': status,
              'release_ready': bool(release_ready), 'sections': sections,
              'errors': errors, 'warnings': warnings,
              'notes': ('旧 Dense 不含完整 Chunk 哈希和模型 revision；'
                        '即便 ID 一致也不能声称向量逐条可验证。V1c.1 不执行发布。')}
    return output


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument('--version', default='v3_2_trial_clean')
    ap.add_argument('--with-indexes', action='store_true', help='同时检查已有 BM25 / Dense（需关闭占用 Qdrant 的程序）')
    ap.add_argument('--collection', default='bge_m3_dense_v3_2_trial_clean')
    ap.add_argument('--output', default='reports/v1c1_validation.json')
    args = ap.parse_args()
    try:
        if not VERSION_RE.fullmatch(args.version):
            raise ValueError('版本名只能包含英文字母、数字、_、-')
        result = report(args)
        dest = Path(args.output)
        if not dest.is_absolute():
            dest = args.root.resolve() / dest
        snapshot = args.root.resolve() / 'data/processed/corpus_snapshots' / args.version
        if snapshot.resolve() in dest.resolve().parents:
            raise ValueError('验证报告不得写到不可变快照内部')
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        print(f'快照版本: {result["corpus_version"]}\n状态: {result["status"]}')
        print(f'论文={result["sections"]["snapshot"]["documents"]}, '
              f'Chunks={result["sections"]["snapshot"]["chunks"]}')
        print(f'错误={len(result["errors"])}, 风险提示={len(result["warnings"])}')
        for item in (result['errors'] + result['warnings'])[:15]:
            print(f'  [{item["code"]}] {item["message"]}')
        print(f'报告: {dest}\n可发布: {result["release_ready"]}')
        if result['errors']:
            sys.exit(2)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError) as e:
        print(f'ERROR: {type(e).__name__}: {e}', file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
