"""Offline tests: no Qwen requests or Qdrant access."""
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from general_qa import answer_general, GeneralQAError
from question_router import run_web_question
from reporting import export_question_report
from history_store import HistoryService

AGENT_ANSWER = {
    'answer': '有研究支持的回答', 'status': 'answered',
    'evidence': [{'id':'E1', 'title':'研究', 'pmcid':'PMC1234'}],
    'claims': [{'text':'结论', 'citation_ids':['E1']}],
    'rounds':2, 'coverage_ratio':1.0,
}
GEN_ANSWER = {
    'answer': '模型知识回答', 'status':'model_knowledge', 'answer_origin':'model_knowledge',
    'evidence':[], 'claims':[], 'limitations':['模型知识未核验'],
}

class DualRouteTests(unittest.TestCase):
    def test_general_quick_never_opens_qdrant(self):
        def forbidden(_): raise AssertionError('Agent should not run')
        r = run_web_question('高血压?', mode='general', agent_runner=forbidden, general_runner=lambda q: GEN_ANSWER)
        self.assertEqual(r['status'], 'model_knowledge')
        self.assertEqual(r['evidence'], [])

    def test_strict_only_agent_and_no_fallback(self):
        def forbidden(_): raise AssertionError('General should not run')
        r = run_web_question('高血压?', mode='strict', agent_runner=lambda q: AGENT_ANSWER, general_runner=forbidden)
        self.assertEqual(r['status'], 'answered')
        self.assertEqual(r['answer_origin'], 'evidence_agent')

    def test_strict_retains_rejection(self):
        r = run_web_question('Q', mode='strict', agent_runner=lambda q: {'status':'insufficient_evidence','evidence':[], 'answer':'拒答'},
                             general_runner=lambda q: self.fail('must not fallback'))
        self.assertEqual(r['status'], 'insufficient_evidence')

    def test_general_evidence_first_verified(self):
        r = run_web_question('Q', mode='general', prefer_evidence=True, agent_runner=lambda q: AGENT_ANSWER,
                             general_runner=lambda q: self.fail('should use cited Agent answer'))
        self.assertEqual(r['answer_origin'], 'evidence_agent')

    def test_general_fallback_after_rejection_keeps_no_citations(self):
        r = run_web_question('Q', mode='general', prefer_evidence=True,
                             agent_runner=lambda q: {'status':'insufficient_evidence','evidence':[{'id':'E1'}]},
                             general_runner=lambda q: GEN_ANSWER)
        self.assertEqual(r['status'], 'model_knowledge')
        self.assertFalse(r['evidence'])
        self.assertEqual(r['local_attempt']['source_count'], 1)

    def test_general_fallback_after_local_error(self):
        from bridge import AgentFrontendError
        def fail(_): raise AgentFrontendError('locked')
        r = run_web_question('Q', mode='general', prefer_evidence=True,
                             agent_runner=fail, general_runner=lambda q: GEN_ANSWER)
        self.assertEqual(r['local_attempt']['outcome'], 'error')

    def test_agent_error_status_is_reported_as_error(self):
        agent = lambda q: {'status': 'error', 'answer': 'failed', 'evidence': [],
                           'claims': [], 'limitations': ['network']}
        r = run_web_question('Q', mode='general', prefer_evidence=True,
                             agent_runner=agent, general_runner=lambda q: GEN_ANSWER)
        self.assertEqual(r['local_attempt']['outcome'], 'error')
        self.assertIn('运行失败', r['limitations'][0])

    def test_general_export_does_not_claim_evidence(self):
        text = export_question_report('高血压', GEN_ANSWER)
        self.assertIn('未经本地医学文献', text)
        self.assertNotIn('检索次数', text)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError): run_web_question('Q', mode='bad')

class GeneralAPITests(unittest.TestCase):
    def test_parses_realistic_qwen_response_without_invented_evidence(self):
        raw = io.BytesIO(json.dumps({'choices':[{'message':{'content':'降压治疗包括生活方式干预。'}, 'finish_reason':'stop'}]}).encode())
        cfg = SimpleNamespace(api_key='FAKE_NOT_A_REAL_KEY', model='qwen-plus', api_base='https://example.com')
        with patch('general_qa._load_config',return_value=cfg), patch('general_qa.urllib.request.urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value = raw
            out = answer_general('高血压')
        self.assertEqual(out['answer'], '降压治疗包括生活方式干预。')
        self.assertEqual(out['evidence'], [])
        self.assertIsNone(out['coverage_ratio'])
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent['model'], 'qwen-plus')
        self.assertIn('未核验', sent['messages'][0]['content'])

    def test_http_401_safe(self):
        import urllib.error
        cfg = SimpleNamespace(api_key='FAKE_SECRET', model='qwen-plus', api_base='https://example.com')
        def boom(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, 'bad', None, io.BytesIO(b'FAKE_SECRET'))
        with patch('general_qa._load_config',return_value=cfg), patch('general_qa.urllib.request.urlopen',side_effect=boom):
            with self.assertRaises(GeneralQAError) as cm:
                answer_general('Q')
        self.assertNotIn('FAKE_SECRET',str(cm.exception))

class MigrationTests(unittest.TestCase):
    def test_new_records_keep_mode_and_evidence_preference(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'history.sqlite3'
            seen = []
            def runner(question, *, mode='general', prefer_evidence=False):
                seen.append((question, mode, prefer_evidence))
                return dict(GEN_ANSWER)
            s = HistoryService(p, runner=runner)
            try:
                cid = s.enqueue('New question', mode='general', prefer_evidence=True)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    rows = s.get_questions(cid)
                    if rows and rows[0]['status'] == 'done':
                        break
                    time.sleep(.01)
                self.assertEqual(rows[0]['mode'], 'general')
                self.assertEqual(rows[0]['prefer_evidence'], 1)
                self.assertEqual(seen, [('New question', 'general', True)])
            finally:
                s.shutdown()

    def test_old_data_remains_strict(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'existing.sqlite3'
            # sqlite3.connect() as a context manager only commits; on Windows a
            # still-open handle makes TemporaryDirectory cleanup fail. Close it.
            cx = sqlite3.connect(p)
            try:
                cx.executescript('''
                  CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT,created_at TEXT,updated_at TEXT);
                  CREATE TABLE questions(id TEXT PRIMARY KEY,conversation_id TEXT,question TEXT,status TEXT,
                    result_json TEXT,error TEXT,created_at TEXT,completed_at TEXT);
                  INSERT INTO conversations VALUES ('c1','Old','t','t');
                  INSERT INTO questions VALUES ('j1','c1','Old question','done',NULL,NULL,'t','t');
                ''')
                cx.commit()
            finally:
                cx.close()
            runner = lambda q, **kw: {'answer':'ok','status':'model_knowledge','evidence':[]}
            s=HistoryService(p,runner=runner)
            try:
                old=s.get_questions('c1')[0]
                self.assertEqual(old['mode'],'strict')
                self.assertEqual(old['question'],'Old question')
                self.assertEqual(old['status'],'done')
                self.assertEqual(old['prefer_evidence'],0)
            finally:s.shutdown()

if __name__=='__main__': unittest.main()
