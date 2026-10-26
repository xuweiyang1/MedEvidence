"""Offline tests for evidence discovery by a real, user-named publication."""
from __future__ import annotations
import sqlite3
import unittest
from agent.retrieval_tools import LocalQdrantRetriever, extract_explicit_title, _focus_terms

TITLE = 'Real-world effectiveness of dupilumab in COPD: a German multicenter analysis'
PREFIX = 'Real-world effectiveness of dupilumab in COPD'
CID = 'PMC13640210:parent:body:s0010:child:0010'


def make_retriever():
    db = sqlite3.connect(':memory:')
    db.execute('''CREATE TABLE chunks (
        id INTEGER PRIMARY KEY, chunk_id TEXT, document_id TEXT,
        title TEXT, section_path TEXT, body_text TEXT)''')
    samples = [
        ('PMC13640210:parent:body:s0001:child:0001', 'Introduction',
         'The effectiveness of dupilumab for patients with COPD was evaluated.'),
        (CID, 'Discussion',
         'Several limitations should be acknowledged. First, this was a retrospective '
         'real-world analysis without a control group, limiting causal inference.'),
        ('PMC13640210:parent:body:s0010:child:0009', 'Discussion',
         'We have observed encouraging signals. A treatment result was described.'),
        ('PMC13699000:parent:body:s0001:child:0001', 'Results',
         'Prednisone in a totally unrelated lung disease was discussed.'),
    ]
    for id, sec, txt in samples:
        title = TITLE if id.startswith('PMC13640210') else 'Different cohort study'
        db.execute('INSERT INTO chunks(chunk_id,document_id,title,section_path,body_text) VALUES(?,?,?,?,?)',
                   (id,id.split(':')[0],title,sec,txt))
    r = LocalQdrantRetriever.__new__(LocalQdrantRetriever)
    r.fts = db
    r.chunks_table = 'chunks'
    return r, db


class TestNamedTitleV026(unittest.TestCase):
    def test_extract_chinese_named_title(self):
        question = '根据Real-world effectiveness of dupilumab in COPD这篇研究，作者指出了哪些研究局 限性？'
        self.assertEqual(extract_explicit_title(question), PREFIX)

    def test_non_title_question_not_rewritten(self):
        self.assertEqual(extract_explicit_title('COPD systemic corticosteroids 5 days'), '')

    def test_hyphen_punctuation_normalized(self):
        r, db = make_retriever()
        try:
            docs = r.lookup_document('Real world effectiveness of dupilumab in COPD', 2, '局 限性')
            self.assertEqual(len(docs), 2)
            self.assertEqual(docs[0].id, CID)
            self.assertEqual(docs[0].pmcid, 'PMC13640210')
            self.assertIn('Several limitations', docs[0].text)
        finally:
            db.close()

    def test_title_search_does_not_call_dense_or_bm25_or_create_ids(self):
        r, db = make_retriever()
        try:
            r._dense = lambda *x: self.fail('unnecessary dense')
            r._sparse = lambda *x: self.fail('unnecessary broad BM25')
            got = r.search('根据' + PREFIX + '这篇研究的局限性是什么', 2)
            self.assertEqual(got[0].id, CID)
            self.assertEqual(got[0].source, 'sqlite_title_lookup')
        finally:
            db.close()

    def test_unknown_or_ambiguous_not_invented(self):
        r, db = make_retriever()
        try:
            self.assertEqual(r.lookup_document('Unknown long title for non existing study in COPD', 4), [])
            self.assertEqual(r.lookup_document('COPD', 4), [])
        finally:
            db.close()

    def test_index_read_only(self):
        r, db = make_retriever()
        try:
            n = db.execute('select count(*) from chunks').fetchone()[0]
            r.lookup_document(PREFIX, 6, focus='limitations')
            self.assertEqual(db.execute('select count(*) from chunks').fetchone()[0], n)
        finally:
            db.close()

    def test_interword_space_in_chinese_focus_normalized(self):
        self.assertIn('limitations', _focus_terms('研究局 限性'))

if __name__ == '__main__':
    unittest.main()
