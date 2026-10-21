#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从现有不可改动的 V3.x 语料导入成统一的 V1c.1 规范快照（不解析原始 PDF）。

每个快照只有一个 chunks.jsonl 真相源；不更新原语料和 BM25/Qdrant。
不同源格式的预处理仍由现有 V1a/V1b 完成，此脚本只冻结其产物。
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from snapshot_core_v1c1 import (VERSION_RE, OPTIONAL_FILES, file_hash, sha_bytes, hashes_for_chunks,
    jsonline, read_identity_table, rows, validate_source)


def write_sorted(path: Path, items: dict):
    with path.open('w', encoding='utf-8', newline='\n') as f:
        for key in sorted(items):
            f.write(jsonline(items[key]))


def copy_optional(source: Path, tmp: Path):
    copied = []
    for name in OPTIONAL_FILES:
        p = source / name
        if not p.is_file():
            continue
        # Source offsets and relationships must be preserved. Keep auxiliary files
        # byte-for-byte and record their SHA; the canonical indexed files are sorted.
        shutil.copyfile(p, tmp / name)
        copied.append(name)
    return copied


def create(root: Path, source_version: str, version: str, parent_version: str,
           parser_version: str, chunker_version: str, model: str, model_revision: str,
           max_length: int, input_field: str):
    source = root / 'data/processed/knowledge' / source_version
    destination = root / 'data/processed/corpus_snapshots' / version
    if destination.exists():
        raise FileExistsError(f'快照已存在，禁止覆盖: {destination}')
    if not source.is_dir():
        raise FileNotFoundError(source)
    source_docs = source / 'documents.jsonl'
    source_chunks = source / 'retrieval_chunks.jsonl'
    docs = read_identity_table(source_docs, 'document_id')
    chunks = read_identity_table(source_chunks, 'chunk_id')
    parents = (read_identity_table(source / 'parent_units.jsonl', 'parent_id')
               if (source / 'parent_units.jsonl').is_file() else None)
    stats = validate_source(docs, chunks, parents)
    if input_field != 'text':
        raise ValueError('V1c.1 旧版导入只接受 input_field=text；切换其他字段必须构建新向量版本')
    records = hashes_for_chunks(chunks, field=input_field)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name('.' + version + '.tmp_' + uuid.uuid4().hex[:8])
    tmp.mkdir(exist_ok=False)
    try:
        write_sorted(tmp / 'documents.jsonl', docs)
        write_sorted(tmp / 'chunks.jsonl', chunks)
        with (tmp / 'hashes.jsonl').open('w', encoding='utf-8', newline='\n') as f:
            for record in records:
                f.write(jsonline(record))
        extras = copy_optional(source, tmp)
        if (source / 'ingestion_report.json').is_file():
            shutil.copyfile(source / 'ingestion_report.json', tmp / 'source_ingestion_report.json')
            extras.append('source_ingestion_report.json')
        file_sha = {name: file_hash(tmp / name) for name in
                    ('documents.jsonl', 'chunks.jsonl', 'hashes.jsonl', *extras)}
        source_file_sha = {
            'documents_jsonl': file_hash(source_docs),
            'retrieval_chunks_jsonl': file_hash(source_chunks),
        }
        source_snapshot_digest = sha_bytes(json.dumps(source_file_sha, sort_keys=True,
            ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        embedding_params = {
            'model': model, 'model_revision': model_revision,
            'max_length': max_length, 'input_field': input_field,
            'parser_version': parser_version, 'chunker_version': chunker_version,
            'normalization_version': 'PRESERVE_EXACT_SOURCE_TEXT_V1',
            'dimension': 1024, 'distance': 'Cosine',
            'input_transform': 'legacy_pick_strip_v1',
            'return_dense': True, 'return_sparse': False,
            'return_colbert_vecs': False,
            'normalization': 'legacy_model_default_unverified',
            'precision': 'FP16_CUDA_LEGACY',
        }
        embedding_fingerprint = sha_bytes(json.dumps(embedding_params, sort_keys=True,
            ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        manifest = {
            'schema_version': 'MedEvidence_CorpusSnapshot_V1c1',
            'corpus_version': version, 'parent_version': parent_version,
            'source_version': source_version,
            'created_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'status': 'CANDIDATE_NOT_PUBLISHED',
            'paper_count': stats['documents'], 'chunk_count': stats['chunks'],
            'document_id_set_sha256': stats['document_id_set_sha256'],
            'chunk_id_set_sha256': stats['chunk_id_set_sha256'],
            'documents_sha256': file_sha['documents.jsonl'],
            'chunks_sha256': file_sha['chunks.jsonl'],
            'file_sha256': file_sha,
            'source_snapshot_sha256': source_snapshot_digest,
            'source_file_sha256': source_file_sha,
            'parser_version': parser_version,
            'chunker_version': chunker_version,
            'normalization_version': 'PRESERVE_EXACT_SOURCE_TEXT_V1',
            'serialization': {'encoding': 'UTF-8', 'file_newline': 'LF',
                              'json_object_keys': 'sorted', 'records': 'sorted_by_ID',
                              'unicode': 'preserve_original_codepoints',
                              'empty_optional_metadata': 'preserve_original_values'},
            'embedding': {**embedding_params, 'embedding_fingerprint': embedding_fingerprint,
                          'fingerprint_status': 'LEGACY_UNVERIFIED' if model_revision == 'UNKNOWN_LEGACY' else 'DECLARED_NOT_VERIFIED'},
            'pipeline_status': 'SNAPSHOT_ONLY_NO_INDEX_WRITES',
            'source_warning': '继承原 V1b/V1b1 解析质量及许可状态；本脚本不进行临床证据审核。',
            'stable_id_warning': ('现有 USER_PDF 的 IMPORT_SHA 前缀 ID 与上传文件内容哈希绑定；'
                                  '修改 PDF 可能导致 document_id 改变。未来需要独立身份注册表。'),
        }
        (tmp / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False,
                          sort_keys=True, indent=2) + '\n', encoding='utf-8', newline='\n')
        os.rename(tmp, destination)  # refuse replacement via pre-existing target check
        return destination, manifest
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument('--source-version', default='v3_2_trial_clean')
    ap.add_argument('--version', default='v3_2_trial_clean')
    ap.add_argument('--parent-version', default='v3_2_trial')
    ap.add_argument('--parser-version', default='legacy_V1b_PDF_JATS')
    ap.add_argument('--chunker-version', default='hierarchical_v3_1_plus_v1b1_filter')
    ap.add_argument('--model', default='BAAI/bge-m3')
    ap.add_argument('--model-revision', default='UNKNOWN_LEGACY')
    ap.add_argument('--max-length', type=int, default=768)
    ap.add_argument('--input-field', default='text')
    args = ap.parse_args()
    for v in (args.source_version, args.version, args.parent_version):
        if not VERSION_RE.fullmatch(v):
            ap.error(f'版本名格式不正确: {v}')
    if args.max_length <= 0:
        ap.error('--max-length 必须大于0')
    target, m = create(args.root.resolve(), args.source_version, args.version,
                       args.parent_version, args.parser_version, args.chunker_version,
                       args.model, args.model_revision, args.max_length, args.input_field)
    print(f'只读源版本: {args.source_version}\n新快照: {target}')
    print(f'文献数: {m["paper_count"]}; Chunk 数: {m["chunk_count"]}')
    print(f'规范 Chunk SHA256: {m["chunks_sha256"]}\n状态: {m["status"]}')
    print('原有数据/BM25/Qdrant 均未修改；尚未发布。')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
