import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from history_store import HistoryService

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'history.sqlite3'
        self.calls = []
        self.service = HistoryService(self.db, runner=self.runner)

    def tearDown(self):
        self.service.shutdown()
        self.temp.cleanup()

    def runner(self, question):
        self.calls.append(question)
        time.sleep(0.035)
        return {'answer': 'answer ' + question, 'status': 'answered', 'evidence': []}

    def _until_done(self, cid, count, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.service.get_questions(cid)
            if len(rows) == count and all(r['status'] == 'done' for r in rows):
                return rows
            time.sleep(.02)
        self.fail(f'jobs did not finish: {self.service.get_questions(cid)}')

    def test_persisted_history_and_reopen(self):
        cid = self.service.enqueue('HFrEF treatment')
        rows = self._until_done(cid,1)
        self.assertEqual(rows[0]['result']['answer'], 'answer HFrEF treatment')
        self.assertEqual(self.service.list_conversations()[0]['id'], cid)
        independent = HistoryService(self.db, runner=self.runner)
        try:
            self.assertEqual(independent.get_questions(cid)[0]['status'], 'done')
        finally:
            independent.shutdown()

    def test_many_queued_and_fifo_worker(self):
        cid = self.service.enqueue('Q1')
        self.service.enqueue('Q2', cid)
        self.service.enqueue('Q3', cid)
        rows = self._until_done(cid,3)
        self.assertEqual(self.calls, ['Q1','Q2','Q3'])
        self.assertEqual([r['question'] for r in rows], ['Q1','Q2','Q3'])
        self.assertEqual(self.service.counts(), {'queued':0, 'running':0})

    def test_other_conversations(self):
        c1 = self.service.enqueue('test 1')
        c2 = self.service.enqueue('test 2')
        self.assertNotEqual(c1,c2)
        self._until_done(c1,1)
        self._until_done(c2,1)
        self.assertEqual(len(self.service.list_conversations()),2)
        self.assertEqual(len(self.service.get_questions(c1)),1)

    def test_input_validation(self):
        with self.assertRaises(ValueError): self.service.enqueue(' ')
        with self.assertRaises(ValueError): self.service.enqueue('x'*1801)
        with self.assertRaises(ValueError): self.service.enqueue('Hi', 'invalid-id')

    def test_failed_jobs_stored(self):
        self.service.shutdown()
        def bad(_): raise ValueError('secret should never be stored')
        self.service = HistoryService(self.db, runner=bad)
        cid = self.service.enqueue('Q failed')
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            rows = self.service.get_questions(cid)
            if rows and rows[0]['status']=='failed': break
            time.sleep(.02)
        self.assertEqual(rows[0]['status'],'failed')
        self.assertNotIn('secret',rows[0]['error'])

if __name__=='__main__': unittest.main()
