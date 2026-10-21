#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence V1c.1 snapshot common code. Python 3.11 stdlib only.

Canonical wire format: UTF-8, LF, sorted object keys, compact JSON, rows sorted by
stable object IDs. Source text/codepoints are NEVER silently normalized, because
V3.1's source offsets and hashes depend on exact text. Unicode policy is explicitly
PRESERVE_SOURCE_CODEPOINTS_V1; NFC may be introduced in a later *new* snapshot
version with changed embedding fingerprints and regenerated vectors.
"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path

SNAPSHOT_FILES = ('documents.jsonl', 'chunks.jsonl', 'hashes.jsonl')
OPTIONAL_FILES = ('parent_units.jsonl', 'evidence_relations.jsonl',
                  'media_registry.jsonl', 'excluded_retrieval_chunks.jsonl')
VERSION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$')


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda: f.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def jsonline(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False) + '\n'


def rows(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f'缺少文件: {path}')
    with path.open('r', encoding='utf-8-sig', newline=None) as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f'{path}:{n} JSON 格式错误: {e}') from e
            if not isinstance(obj, dict):
                raise ValueError(f'{path}:{n} 必须是 JSON 对象')
            yield obj


def text_hash(value: str) -> str:
    # EXACT string hash; dense_input applies the old pick(...).strip() below.
    return sha_bytes(value.encode('utf-8'))


def dense_input(raw: str) -> str:
    # medevidence_dense_v1.normalize_chunk uses pick(), which calls .strip()
    # for every string. Neither Unicode codepoints nor interior newlines change.
    return raw.strip()


def stable_digest(lines: list[str]) -> str:
    return sha_bytes(''.join(lines).encode('utf-8'))


def require_id(obj: dict, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f'{key} 为空或首尾含空白: {value!r}')
    return value


def read_identity_table(path: Path, key: str) -> dict[str, dict]:
    result = {}
    for obj in rows(path):
        oid = require_id(obj, key)
        if oid in result:
            raise ValueError(f'{path} 重复 {key}={oid}')
        result[oid] = obj
    return result


def hash_id_set(ids) -> str:
    return stable_digest([jsonline({'id': key}) for key in sorted(ids)])


def hashes_for_chunks(chunks: dict[str, dict], *, field='text') -> list[dict]:
    output = []
    for cid in sorted(chunks):
        ch = chunks[cid]
        raw = ch.get(field)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f'Chunk 缺少非空 {field}: {cid}')
        output.append({'chunk_id': cid, 'document_id': require_id(ch, 'document_id'),
                       'content_hash': text_hash(raw), 'embedding_input_hash': text_hash(dense_input(raw)),
                       'input_field': field})
    return output


def validate_source(docs: dict, chunks: dict, parents: dict | None = None):
    if not docs or not chunks:
        raise ValueError('文献或 Chunk 为空，不生成快照')
    orphan = sorted({v['document_id'] for v in chunks.values()} - set(docs))
    if orphan:
        raise ValueError(f'Chunk 引用不存在的文献: {orphan[:3]}')
    without = sorted(set(docs) - {v['document_id'] for v in chunks.values()})
    if without:
        raise ValueError(f'有文献没有检索 Chunk: {without[:3]}')
    if parents is not None:
        wrong = [(c['chunk_id'], c.get('parent_id')) for c in chunks.values()
                 if not c.get('parent_id') or c.get('parent_id') not in parents]
        if wrong:
            raise ValueError(f'Chunk 引用不存在的 Parent: {wrong[:3]}')
    return {'documents': len(docs), 'chunks': len(chunks),
            'chunk_id_set_sha256': hash_id_set(chunks),
            'document_id_set_sha256': hash_id_set(docs)}


def load_snapshot(snapshot: Path):
    manifest = json.loads((snapshot / 'manifest.json').read_text(encoding='utf-8'))
    actual_files = {}
    for name, digest in manifest.get('file_sha256', {}).items():
        target = snapshot / name
        if not target.is_file() or file_hash(target) != digest:
            raise ValueError(f'快照文件缺失或哈希不匹配: {name}')
        actual_files[name] = digest
    if not all(name in actual_files for name in SNAPSHOT_FILES):
        raise ValueError('Manifest 缺少必要文件的 SHA256')
    docs = read_identity_table(snapshot / 'documents.jsonl', 'document_id')
    chunks = read_identity_table(snapshot / 'chunks.jsonl', 'chunk_id')
    parents = (read_identity_table(snapshot / 'parent_units.jsonl', 'parent_id')
               if (snapshot / 'parent_units.jsonl').is_file() else None)
    stats = validate_source(docs, chunks, parents)
    if stats['documents'] != manifest['paper_count'] or stats['chunks'] != manifest['chunk_count']:
        raise ValueError('实际数量与 Manifest 不一致')
    if (stats['chunk_id_set_sha256'] != manifest['chunk_id_set_sha256']
            or stats['document_id_set_sha256'] != manifest['document_id_set_sha256']):
        raise ValueError('集合哈希与 Manifest 不一致')
    hashes = read_identity_table(snapshot / 'hashes.jsonl', 'chunk_id')
    if set(hashes) != set(chunks):
        raise ValueError('hashes.jsonl 与 chunks.jsonl 的 Chunk ID 集合不同')
    field = manifest['embedding']['input_field']
    for cid, item in chunks.items():
        rec = hashes[cid]
        if rec.get('document_id') != item['document_id'] or rec.get('input_field') != field:
            raise ValueError(f'哈希登记字段与 Chunk 不同: {cid}')
        if rec.get('content_hash') != text_hash(item.get('text', '')):
            raise ValueError(f'正文哈希不匹配: {cid}')
        if rec.get('embedding_input_hash') != text_hash(dense_input(item.get(field, ''))):
            raise ValueError(f'Embedding 输入哈希不匹配: {cid}')
    return manifest, docs, chunks, hashes, stats
