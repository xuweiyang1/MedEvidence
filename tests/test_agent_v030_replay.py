import unittest
from scripts.compare_adaptive_policies import demo_cases, run, replay


class TestReplay(unittest.TestCase):
    def test_compares_same_input_three_arms(self):
        r = run(demo_cases())
        self.assertEqual(len(r['results']), 9)
        self.assertTrue(r['metrics_are_proxies'])

    def test_stops_on_complete_first_round(self):
        case = demo_cases()[0]
        self.assertEqual(replay(case, 'adaptive')['retrieval_rounds'], 1)
        self.assertEqual(replay(case, 'fixed_two_round')['retrieval_rounds'], 2)

    def test_retrieves_second_round_for_gap(self):
        case = demo_cases()[1]
        self.assertEqual(replay(case, 'adaptive')['retrieval_rounds'], 2)
        self.assertEqual(replay(case, 'adaptive')['terminal_action'], 'answer')
        self.assertNotEqual(replay(case, 'single_rag')['terminal_action'], 'answer')

    def test_honest_partial_after_unresolved_gap(self):
        case = demo_cases()[2]
        self.assertEqual(replay(case, 'adaptive')['terminal_action'], 'partial')

    def test_no_hidden_future_leakage(self):
        case = demo_cases()[1]
        a = replay({**case, 'rounds': case['rounds'][:1]}, 'adaptive')
        self.assertEqual(a['retrieval_rounds'], 1)
        self.assertNotEqual(a['terminal_action'], 'answer')

if __name__ == '__main__':
    unittest.main()
