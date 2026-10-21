#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only FTS5 term coverage check. Term counts are NOT clinical relevance labels.

Run from MedEvidence project root:
python scripts/audit_corpus_fts_coverage.py --db data/processed/index_versions/v3_2_trial_clean/bm25.sqlite3
"""
import argparse
import sqlite3
from pathlib import Path

TESTS = {
    'Q06_COPD_steroids': ['"COPD" AND "corticosteroids"', '"COPD" AND "prednisone"', '"COPD" AND "exacerbation"'],
    'Q08_CAP_outpatient': ['"pneumonia" AND "outpatient"', '"pneumonia" AND "amoxicillin"', '"pneumonia" AND "doxycycline"'],
    'Q12_ACS_DAPT': ['"acute coronary syndrome" AND "DAPT"', '"ACS" AND "antiplatelet"'],
    'Q13_VTE_duration': ['"pulmonary embolism" AND "anticoagulation"', '"venous thromboembolism" AND "duration"'],
    'Q15_Hp_test_of_cure': ['"Helicobacter pylori"', '"Helicobacter pylori" AND "eradication"', '"Helicobacter pylori" AND "breath"', '"Helicobacter pylori" AND "stool"'],
}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True)
    p.add_argument('--table',default='fts_chunks')
    args=p.parse_args()
    db=Path(args.db).resolve()
    if not db.is_file():
        raise SystemExit(f'File not found: {db}')
    if not args.table.replace('_','').isalnum():
        raise SystemExit('Invalid FTS table name')
    con=sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)
    try:
        con.execute('PRAGMA query_only=ON')
        t=args.table
        print('DB:', db)
        print('FTS table:',t)
        print('FTS columns:',[col[1] for col in con.execute(f'PRAGMA table_info("{t}")').fetchall()])
        for label,patterns in TESTS.items():
            print('\n'+label)
            for expr in patterns:
                try:
                    count=con.execute(f'SELECT count(*) FROM "{t}" WHERE "{t}" MATCH ?', (expr,)).fetchone()[0]
                    print(f'  {expr} -> {count} chunk matches')
                except sqlite3.Error as e:
                    print(f'  {expr} -> query error: {e}')
        print('\nNOTE: Counts are term co-occurrence in the same indexed chunk, not verified clinical guidance.')
        print('NOTE: Zero may mean the phrase is split across chunks, tokenized differently, or absent from this index.')
    finally:
        con.close()

if __name__=='__main__':
    main()
