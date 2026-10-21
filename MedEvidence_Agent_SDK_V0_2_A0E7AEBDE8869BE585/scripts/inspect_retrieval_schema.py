"""Read-only diagnostics for MedEvidence v3.2. Never prints document text or API keys.

Usage from the standalone Agent folder:
    python scripts/inspect_retrieval_schema.py
    python scripts/inspect_retrieval_schema.py --project-root E:/.../MedEvidence
"""
from __future__ import annotations
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT))

from agent.config import Config, resolve_project_path
from agent.project_locator import find_project_root

ID_FIELD = re.compile(r"(^id$|_id$|^pmcid$|^doi$|^paper_id$|^chunk_index$|^chunk_idx$|^ordinal$|^rowid$)", re.I)
CANDIDATE_FILE = re.compile(r"(chunk|snapshot|corpus|content|document|paper|metadata|passage)", re.I)
CANDIDATE_EXT = {'.jsonl', '.json', '.parquet', '.sqlite', '.sqlite3', '.db', '.tsv', '.csv', '.arrow', '.feather'}


def summarize_value(value: Any, depth: int = 0) -> Any:
    if isinstance(value, dict):
        if depth >= 2:
            return {'type': 'dict', 'key_count': len(value), 'keys': sorted(map(str, value.keys()))[:40]}
        return {'type': 'dict', 'fields': {str(k): summarize_value(v, depth+1) for k,v in list(value.items())[:45]}}
    if isinstance(value, list):
        return {'type': 'list', 'length': len(value),
                'first_type': type(value[0]).__name__ if value else 'empty'}
    if isinstance(value, str):
        return {'type': 'str', 'length': len(value)}
    return {'type': type(value).__name__}


def keys_only_payload(payload: dict) -> dict:
    """Return shapes and carefully selected ID fields, never arbitrary text values."""
    return {
        'fields': {str(k): summarize_value(v) for k,v in list(payload.items())[:80]},
        'id_field_examples': {str(k): str(v)[:120] for k,v in payload.items()
                              if ID_FIELD.search(str(k)) and isinstance(v, (str,int))},
    }


def describe_sqlite(db_path: Path) -> dict:
    con = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        rows = con.execute("SELECT name,type,sql FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        tables = []
        for name, kind, ddl in rows:
            # Skip FTS5 shadow tables; they are not the user-controlled source schema.
            if name.endswith(('_data','_idx','_docsize','_config')):
                continue
            if not (len(tables) < 40):
                break
            quoted = '"' + name.replace('"', '""') + '"'
            col_rows = con.execute(f'PRAGMA table_xinfo({quoted})').fetchall()
            col_names = [x[1] for x in col_rows]
            obj = {'name':name, 'type':kind,'columns':col_names[:60],
                   'fts5': 'using fts5' in (ddl or '').lower()}
            if obj['fts5']:
                # DDL may contain useful content='...' mapping; it does not contain user rows.
                obj['fts5_sql'] = (ddl or '')[:500]
            if kind == 'table' and (obj['fts5'] or 'chunk' in name.lower() or 'document' in name.lower()):
                try:
                    cur=con.execute(f'SELECT rowid,* FROM {quoted} LIMIT 1')
                    sample=cur.fetchone()
                    if sample:
                        names=[col[0] for col in cur.description]
                        obj['sample_field_shapes'] = {n: summarize_value(v) for n,v in zip(names, sample)}
                        obj['sample_id_fields'] = {n: str(v)[:120] for n,v in zip(names, sample)
                                                   if ID_FIELD.search(n) and isinstance(v,(str,int))}
                except sqlite3.Error as exc:
                    obj['sample_error'] = f'{type(exc).__name__}: {str(exc)[:180]}'
            tables.append(obj)
        return {'path':str(db_path),'tables':tables}
    finally:
        con.close()


def snapshot_candidates(root: Path, qdrant_path: Path, max_files=35) -> list[dict]:
    """Search limited known locations for chunk sources; do not open documents."""
    import os
    bases = [qdrant_path.parent, root / 'data' / 'processed' / 'index_versions',
             root / 'data' / 'processed' / 'index', root / 'data' / 'processed']
    found: dict[str,dict] = {}
    for base in bases:
        if not base.is_dir():
            continue
        for parent, dirs, filenames in os.walk(base):
            rel = Path(parent).relative_to(base)
            if len(rel.parts) >= 3:
                dirs[:] = []
            else:
                dirs[:] = [d for d in dirs if d not in {'qdrant','__pycache__','.git','cache'} and not d.startswith('qdrant_')]
            for filename in filenames:
                p = Path(parent) / filename
                if p.suffix.lower() not in CANDIDATE_EXT or not CANDIDATE_FILE.search(filename):
                    continue
                if p == qdrant_path:
                    continue
                key = str(p).lower()
                if key not in found:
                    try:
                        found[key] = {'path':str(p.relative_to(root)), 'size_mb':round(p.stat().st_size/1048576,2)}
                    except (OSError,ValueError):
                        found[key] = {'path':str(p)}
                if len(found) >= max_files: return list(found.values())
    return list(found.values())


def diagnose(cfg: Config) -> dict:
    # Read-only: scroll metadata without embeddings, without model loading or API requests.
    result = {'project_root':str(cfg.root),'collection':cfg.collection, 'network_or_model_calls': False,
              'exposes_fulltext':False, 'api_key_logged':False}
    qdir = resolve_project_path(cfg.root, cfg.qdrant_path)
    result['qdrant_path'] = str(qdir)
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(path=str(qdir))
        try:
            samples,_ = client.scroll(collection_name=cfg.collection,limit=3,with_payload=True,with_vectors=False)
            result['qdrant_payload_samples'] = [keys_only_payload(dict(p.payload or {})) for p in samples]
            result['sample_point_count'] = len(samples)
        finally:
            client.close()
    except Exception as exc:
        result['qdrant_error'] = f'{type(exc).__name__}: {str(exc)[:260]}'
    if cfg.bm25_db:
        bpath = resolve_project_path(cfg.root,cfg.bm25_db)
        try:
            result['sqlite'] = describe_sqlite(bpath)
        except Exception as exc:
            result['sqlite_error'] = f'{type(exc).__name__}: {str(exc)[:260]}'
    result['chunk_file_candidates'] = snapshot_candidates(cfg.root,qdir)
    result['note'] = ('只展示 payload 字段名/值类型/字符串长度、必要的来源 ID、SQLite 表结构和候选文件名。'
                      '不会显示文献正文、API Key，也不会改动任何索引；请把输出发回来以便精准连接 Chunk 正文。')
    return result


def main() -> int:
    parser=argparse.ArgumentParser(description='MedEvidence only-schema diagnostics; no LLM or embeddings')
    parser.add_argument('--project-root')
    args=parser.parse_args()
    root=find_project_root(CODE_ROOT,args.project_root)
    cfg=Config.from_env(root,code_root=CODE_ROOT)
    print(json.dumps(diagnose(cfg),ensure_ascii=False,indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
