"""Shared, fail-closed V1c.2 index definitions. Pure Python 3.11."""
from __future__ import annotations
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from snapshot_core_v1c1 import load_snapshot, file_hash

REV_RE = re.compile(r"[0-9a-f]{40}")


def paths(root: Path, version: str):
    from snapshot_core_v1c1 import VERSION_RE
    if not VERSION_RE.fullmatch(version):
        raise ValueError('Invalid version')
    root = root.resolve()
    return (root / 'data/processed/corpus_snapshots' / version,
            root / 'data/processed/index_versions' / version)


def canonical_sha(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
         separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def atomic_json(path: Path, value: dict):
    if path.exists():
        raise FileExistsError(f'拒绝覆盖: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('x', encoding='utf-8', newline='\n') as f:
            json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')
        if path.exists():
            raise FileExistsError(path)
        os.rename(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def snapshot(root: Path, version: str):
    folder, index = paths(root, version)
    manifest, docs, chunks, hashes, stats = load_snapshot(folder)
    if manifest['corpus_version'] != version:
        raise ValueError('快照版本名与 manifest 不符')
    if manifest.get('status') != 'CANDIDATE_NOT_PUBLISHED':
        raise ValueError('只允许读取已验证的候选快照')
    if manifest['embedding']['input_field'] != 'text':
        raise ValueError('当前仅支持与 V1c.1 对齐的 text 字段')
    return folder, index, manifest, docs, chunks, hashes


def model_identity(model_path: Path, manifest: dict, *, device: str, max_length: int):
    """Model must be a *local, pinned* HF snapshots/<40hex> directory.

    File hashes bind even locally edited safetensors/tokenizers. Network is not used.
    """
    p = model_path.expanduser().resolve()
    if p.parent.name != 'snapshots' or not REV_RE.fullmatch(p.name):
        raise ValueError('模型路径必须是 Hugging Face 本地 snapshots/<40位commit> 目录，不能使用 main 或无版本路径')
    if not p.is_dir():
        raise FileNotFoundError(p)
    if p.parent.parent.name != 'models--BAAI--bge-m3':
        raise ValueError('模型快照路径不属于 models--BAAI--bge-m3；拒绝误用其他模型权重')
    files = sorted(q for q in p.rglob('*') if q.is_file() and (q.suffix.lower() in ('.safetensors', '.bin')
                   or q.name in ('config.json','tokenizer.json','tokenizer_config.json',
                                 'special_tokens_map.json','sentencepiece.bpe.model','sentencepiece.model','vocab.txt','modules.json')))
    if not any(q.suffix == '.safetensors' or q.suffix == '.bin' for q in files):
        raise ValueError('模型目录缺少权重文件')
    if not any(q.name == 'config.json' for q in files):
        raise ValueError('模型目录缺少 config.json')
    import importlib.metadata as md
    versions = {}
    for pkg in ('FlagEmbedding','transformers','torch','tokenizers'):
        try:
            versions[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            versions[pkg] = 'NOT_INSTALLED'
    if versions['FlagEmbedding'] == 'NOT_INSTALLED':
        raise RuntimeError('当前环境没有 FlagEmbedding')
    if max_length <= 0 or max_length > 8192:
        raise ValueError('max_length 范围必须为 1..8192')
    if max_length != manifest['embedding']['max_length']:
        raise ValueError('max_length 与不可变快照声明不一致；请使用新快照，而不是静默修改建库参数')
    conf = {
        'model':'BAAI/bge-m3', 'revision':p.name, 'dimension':1024,
        'distance':'Cosine','input_field':'text','input_transform':'legacy_pick_strip_v1',
        'max_length':max_length,'model_files_sha256':{str(q.relative_to(p)).replace('\\','/'):file_hash(q) for q in files},
        'precision':'fp16' if device == 'cuda' else 'fp32',
        'device_type': device, 'versions':versions,
        'parser_version':manifest['parser_version'],'chunker_version':manifest['chunker_version'],
        'normalization_version':manifest['normalization_version'],
        'return_dense':True,'return_sparse':False,'return_colbert_vecs':False,
        'embedding_normalization':'BGEM3FlagModel_default',
    }
    return conf, canonical_sha(conf)


def point_uuid(cid):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'MedEvidence:v1c2:' + cid))


def bm25_files(index_dir):
    return index_dir / 'bm25.sqlite3', index_dir / 'bm25_manifest.json'


def dense_files(index_dir):
    return index_dir / 'qdrant', index_dir / 'dense_manifest.json'
