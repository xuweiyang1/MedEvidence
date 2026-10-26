import json
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import (_extract_agent_json, configuration_status, pmc_url, pubmed_url,
                    render_markdown_report, run_question, AgentFrontendError)

FIXTURE = {
    'run_id': 'demo-1', 'answer': '有引文的示例回复', 'status': 'partial',
    'evidence': [{'id': 'E1', 'title': '研究一', 'pmcid': 'PMC1234', 'section': 'results'}],
    'claims': [{'text': '示例', 'citation_ids': ['E1']}],
    'rounds': 2, 'coverage_ratio': 0.5, 'limitations': ['需更多证据'],
}

class BridgeTests(unittest.TestCase):
    def test_json_with_progress_and_warning(self):
        message = 'Fetching 30 files: done\nUserWarning: something\n' + json.dumps(FIXTURE, ensure_ascii=False, indent=2)
        self.assertEqual(_extract_agent_json(message)['answer'], '有引文的示例回复')

    def test_trailing_noise(self):
        message = json.dumps(FIXTURE, ensure_ascii=False, indent=2) + '\nprogress finished\n'
        self.assertEqual(_extract_agent_json(message)['run_id'], 'demo-1')

    def test_invalid_output(self):
        with self.assertRaises(AgentFrontendError):
            _extract_agent_json('some unexpected output')

    def test_links_safe(self):
        self.assertEqual(pmc_url('PMC1234'), 'https://pmc.ncbi.nlm.nih.gov/articles/PMC1234/')
        self.assertIsNone(pmc_url('javascript:alert(1)'))

    def test_report_no_trace(self):
        d = dict(FIXTURE, trace_path='C:/secrets/api_key')
        text = render_markdown_report('问题', d)
        self.assertIn('PMC1234', text)
        self.assertNotIn('api_key', text)

    def test_subprocess_wired_real_entrypoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'scripts').mkdir()
            (root / 'scripts' / 'run_agent.py').write_text('# placeholder\n')
            with patch('bridge.subprocess.run') as fake:
                fake.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(FIXTURE), stderr='')
                result = run_question('糖尿病?', root=root)
                self.assertEqual(result['status'], 'partial')
                args, kwargs = fake.call_args
                self.assertIn('--json', args[0])
                self.assertIn('--question', args[0])
                self.assertEqual(kwargs['cwd'], str(root))

    def test_real_subprocess_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'scripts').mkdir()
            (root / 'scripts' / 'run_agent.py').write_text(
                'import json\n'
                'print("progress 100%")\n'
                'print(json.dumps(' + repr(FIXTURE) + ', ensure_ascii=False, indent=2))\n',
                encoding='utf-8',
            )
            result = run_question('测试中文问题', root=root, timeout=10)
            self.assertEqual(result['run_id'], 'demo-1')

    def test_no_shell(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'scripts').mkdir()
            (root / 'scripts' / 'run_agent.py').write_text('# test\n')
            with patch('bridge.subprocess.run') as fake:
                fake.return_value = SimpleNamespace(returncode=2, stdout='', stderr='API_KEY_DO_NOT_LEAK')
                with self.assertRaises(AgentFrontendError) as ctx:
                    run_question('demo', root=root)
                self.assertNotIn('API_KEY_DO_NOT_LEAK', str(ctx.exception))

    def test_error_report_marks_failure(self):
        report = render_markdown_report("问题", {
            "status": "error", "answer": "运行未完成", "reason_code": "agent_runner_error",
            "failure_stage": "agent_runner", "limitations": ["网络错误"],
        })
        self.assertIn("运行故障", report)
        self.assertIn("agent_runner_error", report)
        self.assertIn("agent_runner", report)

    def test_pubmed_url_validation(self):
        self.assertEqual(pubmed_url("12345678"), "https://pubmed.ncbi.nlm.nih.gov/12345678/")
        self.assertIsNone(pubmed_url("PMC1234567"))
        self.assertIsNone(pubmed_url(""))

    def test_online_report_marks_abstract(self):
        report = render_markdown_report("问题", {
            "status": "partial", "answer": "x", "rounds": 1, "coverage_ratio": 0.5,
            "evidence": [{"id": "E1", "title": "Online", "pmid": "12345678",
                          "source": "pubmed", "evidence_level": "abstract"}],
            "limitations": [],
        })
        self.assertIn("（摘要）", report)
        self.assertIn("pubmed.ncbi.nlm.nih.gov", report)
        self.assertIn("在线摘要证据：1 条", report)

    def test_online_flag_is_read_from_env_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".env.agent").write_text(
                "MEDEVIDENCE_ONLINE_ENABLED=1\nMEDEVIDENCE_MODEL=qwen-plus\n", encoding="utf-8")
            status = configuration_status(root)
            self.assertTrue(status["online_enabled"])

if __name__ == '__main__':
    unittest.main()
