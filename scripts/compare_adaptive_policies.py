"""Offline policy replay: no DashScope, embedding, Qdrant or model dependency.

The input consists of per-round snapshots from one fixed retrieval pool, NOT
live model calls. The simulator reports proxy metrics only. It is not a claim
of biomedical answer quality or a fair final benchmark on its own.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.adaptive_policy import decide_evidence_action
from agent.models import Coverage, Evidence


def demo_cases():
    return [
        {"case_id": "SYNTHETIC_COMPLETE", "aspects": ["benefit", "limits"],
         "rounds": [
             {"evidence": [{"id": "E1", "pmcid": "SYN_A", "text": "Synthetic study describes a benefit and its limitations without patient data."}],
              "covered": {"benefit": ["E1"], "limits": ["E1"]}, "gaps": [], "conflicts": [], "new_chunks": 1},
             {"evidence": [{"id": "E1", "pmcid": "SYN_A", "text": "Synthetic study describes a benefit and its limitations without patient data."},
                           {"id": "E2", "pmcid": "SYN_B", "text": "Another synthetic document for evaluating policy efficiency."}],
              "covered": {"benefit": ["E1"], "limits": ["E1"]}, "gaps": [], "conflicts": [], "new_chunks": 1}]},
        {"case_id": "SYNTHETIC_GAP", "aspects": ["benefit", "safety"],
         "rounds": [
             {"evidence": [{"id": "E1", "pmcid": "SYN_A", "text": "Synthetic paper addresses benefits without addressing safety directly."}],
              "covered": {"benefit": ["E1"]}, "gaps": ["safety"], "conflicts": [], "new_chunks": 1},
             {"evidence": [{"id": "E1", "pmcid": "SYN_A", "text": "Synthetic paper addresses benefits without addressing safety directly."},
                           {"id": "E2", "pmcid": "SYN_B", "text": "A separate synthetic source provides details about the safety dimension."}],
              "covered": {"benefit": ["E1"], "safety": ["E2"]}, "gaps": [], "conflicts": [], "new_chunks": 1}]},
        {"case_id": "SYNTHETIC_MISSING", "aspects": ["benefit", "safety"],
         "rounds": [
             {"evidence": [{"id": "E1", "pmcid": "SYN_A", "text": "Synthetic source contains benefit evidence but not the safety outcome."}],
              "covered": {"benefit": ["E1"]}, "gaps": ["safety"], "conflicts": [], "new_chunks": 1},
             {"evidence": [{"id": "E1", "pmcid": "SYN_A", "text": "Synthetic source contains benefit evidence but not the safety outcome."}],
              "covered": {"benefit": ["E1"]}, "gaps": ["safety"], "conflicts": [], "new_chunks": 0}]}
    ]


def _make_decision(case, round_index, max_searches):
    snap = case['rounds'][round_index]
    ev = [Evidence(**x) for x in snap['evidence']]
    cov = Coverage(covered=snap.get('covered', {}), gaps=snap.get('gaps', []),
                   conflicts=snap.get('conflicts', []))
    # Count consecutive zero-novelty retrieval rounds through this snapshot.
    streak = 0
    for earlier in reversed(case['rounds'][:round_index + 1]):
        if earlier.get('new_chunks', 0) != 0:
            break
        streak += 1
    return decide_evidence_action(
        aspects=case['aspects'], coverage=cov, evidence=ev,
        searches_used=round_index + 1, max_searches=max_searches,
        new_chunks_last_search=snap.get('new_chunks', 0), no_novelty_streak=streak)


def replay(case, mode):
    rounds = case.get('rounds', [])
    if not rounds:
        raise ValueError('case.rounds must not be empty')
    if mode == 'single_rag':
        idx = 0
        decision = _make_decision(case, idx, len(rounds))
    elif mode == 'fixed_two_round':
        idx = min(1, len(rounds) - 1)
        decision = _make_decision(case, idx, len(rounds))
    elif mode == 'adaptive':
        idx = 0
        budget = len(rounds)
        for j in range(budget):
            idx = j
            decision = _make_decision(case, j, budget)
            if decision.action != 'search_again':
                break
    else:
        raise ValueError('unknown mode')
    # A fixed baseline stops at its fixed retrieval count; its final evidence
    # cannot be marked 'answered' if it did not meet the same hard gates.
    terminal_action = decision.action
    if terminal_action == 'search_again':
        terminal_action = 'partial' if decision.aspect_coverage > 0 else 'abstain'
    return {
        'case_id': case['case_id'], 'mode': mode, 'retrieval_rounds': idx + 1,
        'terminal_action': terminal_action, 'aspect_coverage_proxy': decision.aspect_coverage,
        'sufficiency_proxy': decision.sufficiency_proxy,
        'conflict_penalty': decision.conflict_penalty,
    }


def validate_case(case):
    if not isinstance(case, dict) or not isinstance(case.get('case_id'), str):
        raise ValueError('case_id missing')
    if not isinstance(case.get('aspects'), list) or not all(isinstance(a, str) for a in case['aspects']):
        raise ValueError('aspects must be list[str]')
    if not isinstance(case.get('rounds'), list) or not case['rounds']:
        raise ValueError('rounds must be a non-empty list')
    for rnd in case['rounds']:
        if not isinstance(rnd, dict) or not isinstance(rnd.get('evidence'), list):
            raise ValueError('each round needs evidence list')
        if not isinstance(rnd.get('covered'), dict):
            raise ValueError('each round needs covered map')


def run(cases):
    results = []
    for case in cases:
        validate_case(case)
        for mode in ('single_rag', 'fixed_two_round', 'adaptive'):
            results.append(replay(case, mode))
    return {'schema': 'MedEvidence_PolicyReplay_V0_3',
            'data_type': 'synthetic_demo_or_user_supplied_frozen_snapshots',
            'metrics_are_proxies': True,
            'not_clinical_accuracy_or_true_agent_benchmark': True,
            'results': results}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--demo', action='store_true', help='three synthetic cases, no API, no index')
    p.add_argument('--input', help='JSONL with one case per line')
    p.add_argument('--output', help='write JSON output (optional)')
    args = p.parse_args()
    if not args.demo and not args.input:
        p.error('choose --demo or --input')
    cases = demo_cases() if args.demo else [json.loads(line) for line in Path(args.input).read_text(encoding='utf-8').splitlines() if line.strip()]
    result = run(cases)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
