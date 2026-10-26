"""Persistent browser chat log and non-blocking background job queue.

The user's existing V0.3 Agent CLI reads Qdrant in embedded/local mode.  We
intentionally run ONE background Agent subprocess at a time to avoid unsafe
concurrent access to that database.  Multiple questions can be enqueued.
"""
from __future__ import annotations

import inspect
import json
import sqlite3
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from bridge import AgentFrontendError
from general_qa import GeneralQAError
from question_router import run_web_question

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "history.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HistoryService:
    """Thread-safe through short per-operation SQLite connections (WAL)."""

    def __init__(self, db_path: Path = DEFAULT_DB, runner: Callable[..., dict] = run_web_question):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.runner = runner
        # Preserve compatibility with existing one-argument mock runners.
        params = inspect.signature(runner).parameters
        self._runner_supports_options = ('mode' in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()))
        self._mutex = RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="medevidence-agent")
        self._init_db()
        # A Streamlit restart can interrupt an active subprocess. Recover
        # unfinished jobs; do not lose the questions after process exit.
        with self._connect() as cx:
            cx.execute("UPDATE questions SET status='queued' WHERE status='running'")
            pending = [r['id'] for r in cx.execute(
                "SELECT id FROM questions WHERE status='queued' ORDER BY rowid")]
        for job_id in pending:
            self._pool.submit(self._run, job_id)

    @contextmanager
    def _connect(self):
        cx = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            cx.row_factory = sqlite3.Row
            cx.execute('PRAGMA foreign_keys=ON')
            cx.execute('PRAGMA busy_timeout=30000')
            with cx:
                yield cx
        finally:
            cx.close()

    def _init_db(self) -> None:
        with self._connect() as cx:
            cx.execute('PRAGMA journal_mode=WAL')
            cx.execute('''CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            cx.execute('''CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                question TEXT NOT NULL, status TEXT NOT NULL,
                result_json TEXT, error TEXT,
                created_at TEXT NOT NULL, completed_at TEXT,
                mode TEXT NOT NULL DEFAULT 'strict',
                prefer_evidence INTEGER NOT NULL DEFAULT 0)''')
            # In-place migration: previous questions all used strict Agent mode.
            cols = {r['name'] for r in cx.execute('PRAGMA table_info(questions)')}
            if 'mode' not in cols:
                cx.execute("ALTER TABLE questions ADD COLUMN mode TEXT NOT NULL DEFAULT 'strict'")
            if 'prefer_evidence' not in cols:
                cx.execute("ALTER TABLE questions ADD COLUMN prefer_evidence INTEGER NOT NULL DEFAULT 0")
            cx.execute('CREATE INDEX IF NOT EXISTS questions_by_chat ON questions(conversation_id,created_at,id)')
            cx.execute('CREATE INDEX IF NOT EXISTS questions_by_state ON questions(status)')

    def list_conversations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as cx:
            return [dict(r) for r in cx.execute(
                'SELECT id,title,created_at,updated_at FROM conversations ORDER BY updated_at DESC,rowid DESC LIMIT ?',
                (max(1, min(limit, 1000)),))]

    def get_conversation(self, cid: str) -> dict[str, Any] | None:
        with self._connect() as cx:
            row = cx.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone()
            return dict(row) if row else None

    def enqueue(self, question: str, conversation_id: str | None = None,
                *, mode: str = 'general', prefer_evidence: bool = False) -> str:
        question = question.strip()
        if mode not in ('general', 'strict'):
            raise ValueError('问答模式无效。')
        if mode == 'strict':
            prefer_evidence = False
        if not question or len(question) > 1800:
            raise ValueError('问题不能为空，且不能超过 1800 字。')
        with self._mutex:
            now = _now()
            cid = conversation_id or uuid4().hex
            job_id = uuid4().hex
            with self._connect() as cx:
                cx.execute('BEGIN IMMEDIATE')
                existing = cx.execute('SELECT 1 FROM conversations WHERE id=?', (cid,)).fetchone()
                if not existing:
                    if conversation_id:
                        raise ValueError('选中的历史对话不存在。')
                    cx.execute('INSERT INTO conversations VALUES(?,?,?,?)',
                               (cid, question[:35].replace('\n', ' '), now, now))
                cx.execute('''INSERT INTO questions(
                    id,conversation_id,question,status,created_at,mode,prefer_evidence)
                    VALUES(?,?,?,?,?,?,?)''',
                    (job_id, cid, question, 'queued', now, mode, int(prefer_evidence)))
                cx.execute('UPDATE conversations SET updated_at=? WHERE id=?', (now, cid))
            self._pool.submit(self._run, job_id)
        return cid

    def _run(self, job_id: str) -> None:
        with self._connect() as cx:
            changed = cx.execute(
                "UPDATE questions SET status='running' WHERE id=? AND status='queued'", (job_id,))
            if changed.rowcount != 1:
                return
            row = cx.execute('SELECT question,mode,prefer_evidence FROM questions WHERE id=?',
                             (job_id,)).fetchone()
        if not row:
            return
        try:
            if self._runner_supports_options:
                result = self.runner(row['question'], mode=row['mode'],
                                     prefer_evidence=bool(row['prefer_evidence']))
            else:
                result = self.runner(row['question'])
            serialized = json.dumps(result, ensure_ascii=False)
            with self._connect() as cx:
                cx.execute('''UPDATE questions SET status='done',result_json=?,error=NULL,
                              completed_at=? WHERE id=?''', (serialized, _now(), job_id))
        except (AgentFrontendError, GeneralQAError) as exc:
            with self._connect() as cx:
                cx.execute('''UPDATE questions SET status='failed',error=?,completed_at=? WHERE id=?''',
                           (str(exc), _now(), job_id))
        except Exception as exc:
            # Don't store full exceptions: they can contain paths, user text or secrets.
            with self._connect() as cx:
                cx.execute('''UPDATE questions SET status='failed',error=?,completed_at=? WHERE id=?''',
                           (f'Agent 运行异常（{type(exc).__name__}），请检查本地启动窗口。', _now(), job_id))

    def get_questions(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as cx:
            rows = cx.execute('''SELECT id,conversation_id,question,status,result_json,error,created_at,completed_at,
                                        mode,prefer_evidence
                                 FROM questions WHERE conversation_id=? ORDER BY rowid''',
                              (conversation_id,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d['result_json']:
                try:
                    d['result'] = json.loads(d.pop('result_json'))
                except ValueError:
                    d.pop('result_json', None)
                    d['status'] = 'failed'
                    d['error'] = '已保存结果无法解析。'
            else:
                d.pop('result_json', None)
            result.append(d)
        return result

    def counts(self) -> dict[str, int]:
        with self._connect() as cx:
            rows = cx.execute('''SELECT status,COUNT(*) AS n FROM questions
                                 WHERE status IN ('queued','running') GROUP BY status''')
            status_counts = {r['status']: r['n'] for r in rows}
        return {'queued': status_counts.get('queued', 0), 'running': status_counts.get('running', 0)}

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
