import pathlib
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from resident_runner import TimedRetriever
from bridge import run_question

class FakeRetriever:
    def __init__(self):
        self.hits=0
    def search(self, query, top_k):
        self.hits+=1
        return [query]
    def lookup_document(self, title):
        return [title]

class ResidentTests(unittest.TestCase):
    def test_retriever_preserves_methods_and_counts(self):
        r=FakeRetriever()
        t=TimedRetriever(r)
        self.assertEqual(t.search('a', 2), ['a'])
        self.assertEqual(t.lookup_document('title'), ['title'])
        self.assertEqual(t.calls, 2)
        self.assertGreaterEqual(t.seconds, 0)
        t.reset()
        self.assertEqual(t.calls, 0)
        self.assertEqual(t.raw.hits, 1)

    def test_no_false_lookup_on_retrievers_without_lookup(self):
        class Simple:
            def search(self, q, k):return []
        self.assertFalse(hasattr(TimedRetriever(Simple()), 'lookup_document'))

    def test_default_uses_resident_one_worker_and_avoids_subprocess(self):
        with patch('bridge.agent_entrypoint', return_value=pathlib.Path(__file__)), \
             patch('resident_runner.run_resident_question', return_value={'status':'partial'}) as fast, \
             patch('bridge.subprocess.run') as subprocess_run, \
             patch.dict('os.environ', {'MEDEVIDENCE_WEB_ENGINE':'resident'}):
            self.assertEqual(run_question('测试')['status'], 'partial')
            fast.assert_called_once_with('测试')
            subprocess_run.assert_not_called()

    def test_cli_mode_backward_compatible(self):
        from unittest.mock import MagicMock
        import json
        fake_json={'status':'partial','answer':'demo','evidence':[]}
        with patch('bridge.agent_entrypoint', return_value=pathlib.Path(__file__)), \
             patch('bridge.subprocess.run', return_value=MagicMock(returncode=0,stdout=json.dumps(fake_json))) as sp, \
             patch.dict('os.environ', {'MEDEVIDENCE_WEB_ENGINE':'cli'}):
            self.assertEqual(run_question('测试')['status'], 'partial')
            sp.assert_called_once()

if __name__=='__main__':unittest.main()
