import unittest
from agent.adaptive_policy import PolicyConfig, decide_evidence_action
from agent.models import Evidence, Coverage


def e(i='E1', text='Direct primary evidence reported by the original study with outcomes included.', pmcid='PMC123'):
    return Evidence(id=i, text=text, title='Mock article', pmcid=pmcid)


class TestAdaptivePolicy(unittest.TestCase):
    def act(self, coverage=None, ev=None, asp=None, used=1, limit=3, new=1, stale=0):
        return decide_evidence_action(aspects=asp or ['effect', 'safety'],
            coverage=coverage, evidence=ev if ev is not None else [e()],
            searches_used=used, max_searches=limit, new_chunks_last_search=new,
            no_novelty_streak=stale)

    def test_full_grounded_answer(self):
        d = self.act(Coverage(covered={'effect':['E1'],'safety':['E1']}, ratio=1.0), [e()])
        self.assertEqual(d.action, 'answer')
        self.assertEqual(d.aspect_coverage, 1.0)

    def test_fake_ids_do_not_earn_coverage(self):
        d = self.act(Coverage(covered={'effect':['E666'],'safety':['E777']}, ratio=1.0))
        self.assertEqual(d.action, 'search_again')
        self.assertEqual(d.aspect_coverage, 0.0)

    def test_one_gap_forces_search(self):
        d = self.act(Coverage(covered={'effect':['E1']}, ratio=1.0))
        self.assertEqual(d.action, 'search_again')
        self.assertIn('safety', d.unresolved_aspects)

    def test_all_gaps_exhausted_abstains(self):
        d = self.act(Coverage(), ev=[], used=3)
        self.assertEqual(d.action, 'abstain')

    def test_some_evidence_exhausted_partial(self):
        d = self.act(Coverage(covered={'effect':['E1']}), used=3)
        self.assertEqual(d.action, 'partial')

    def test_contradictory_coverage_gap_does_not_full_answer(self):
        d = self.act(Coverage(covered={'effect':['E1'],'safety':['E1']}, gaps=['safety']))
        self.assertEqual(d.action, 'search_again')

    def test_conflict_forces_search(self):
        d = self.act(Coverage(covered={'effect':['E1'],'safety':['E1']}, conflicts=['disagreement']))
        self.assertEqual(d.action, 'search_again')

    def test_weak_text_cannot_yield_full_answer(self):
        d = self.act(Coverage(covered={'effect':['E1'],'safety':['E1']}), ev=[e(text='ok')])
        self.assertNotEqual(d.action, 'answer')

    def test_saturation_early_stop(self):
        d = self.act(Coverage(covered={'effect':['E1']}), used=2, stale=2, new=0)
        self.assertEqual(d.action, 'partial')
        self.assertEqual(d.reason_code, 'retrieval_saturated')

    def test_no_future_inputs(self):
        inp = dict(coverage=Coverage(covered={'effect':['E1']}), ev=[e()], used=1)
        self.assertEqual(self.act(**inp).action, 'search_again')

    def test_sensitive_gap_text_not_leaked_by_default(self):
        d = self.act(Coverage(covered={'effect':['E1']}))
        self.assertEqual(d.public()['unresolved_aspects'], 1)
        self.assertNotIn('safety', str(d.public()))

    def test_source_counts_not_evidence_quality(self):
        d = self.act(Coverage(covered={'effect':['E1'],'safety':['E1']}),ev=[e(), e('E2',pmcid='PMC123')])
        self.assertEqual(d.independent_source_count, 1)

    def test_no_coverage_not_answer(self):
        self.assertEqual(self.act(None).action, 'search_again')

if __name__ == '__main__':
    unittest.main()
