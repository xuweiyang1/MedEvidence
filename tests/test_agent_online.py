"""Offline tests for abstract-level online literature retrieval."""
from __future__ import annotations
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

from agent.models import Evidence
from agent.online_literature import (
    OnlineHit,
    OnlineLiteratureError,
    OnlineLiteratureService,
    PubMedClient,
    _clean,
)
from agent.sdk_runtime import SDKState, find_existing_evidence

XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>11111111</PMID>
      <Article>
        <ArticleTitle>Alpha <i>beta</i> trial</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Alpha <i>beta</i> reduces events.</AbstractText>
          <AbstractText Label="RESULTS">ARR 3.2% versus 5.1%.</AbstractText>
        </Abstract>
        <Journal><JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal>
        <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="doi">10.1000/alpha</ArticleId>
        <ArticleId IdType="pmc">PMC1234567</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>22222222</PMID>
      <Article>
        <ArticleTitle>Retracted paper</ArticleTitle>
        <Abstract><AbstractText>Retracted abstract text.</AbstractText></Abstract>
        <PublicationTypeList><PublicationType>Retracted Publication</PublicationType></PublicationTypeList>
        <Journal><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>
      </Article>
      <CommentsCorrectionsList>
        <CommentsCorrections RefType="RetractionIn">notice</CommentsCorrections>
      </CommentsCorrectionsList>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>33333333</PMID>
      <Article>
        <ArticleTitle>No abstract</ArticleTitle>
        <Journal><JournalIssue><PubDate><Year>2019</Year></PubDate></JournalIssue></Journal>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


class FakeHttp:
    def __init__(self, search_payload: dict, fetch_payload: bytes):
        self.search_payload = json.dumps(search_payload).encode("utf-8")
        self.fetch_payload = fetch_payload

    def get(self, url: str) -> bytes:
        return self.search_payload if "esearch" in url else self.fetch_payload


class FakeClient:
    def __init__(self, hits: list[OnlineHit]):
        self.hits = hits
        self.calls = 0

    def search(self, query: str, limit: int, publication_types: str = "") -> list[OnlineHit]:
        self.calls += 1
        return self.hits[:limit]


def hit(**kwargs) -> OnlineHit:
    base = dict(source="pubmed", source_id="1", title="T", abstract="A")
    base.update(kwargs)
    return OnlineHit(**base)


class TestPubMedParsing(unittest.TestCase):
    def test_parses_abstract_metadata_and_retraction(self):
        client = PubMedClient(FakeHttp({"esearchresult": {"idlist": ["11111111", "22222222", "33333333"]}}, XML))
        hits = client.search("alpha beta", 3)
        self.assertEqual(len(hits), 2)  # third record has no abstract
        first = hits[0]
        self.assertEqual(first.pmid, "11111111")
        self.assertEqual(first.doi, "10.1000/alpha")
        self.assertEqual(first.pmcid, "PMC1234567")
        self.assertEqual(first.year, "2024")
        self.assertIn("Alpha beta reduces events", first.abstract)
        self.assertIn("ARR 3.2%", first.abstract)
        self.assertFalse(first.is_retracted)
        self.assertIn("pubmed.ncbi.nlm.nih.gov", first.url)
        self.assertTrue(hits[1].is_retracted)

    def test_html_is_cleaned(self):
        self.assertEqual(_clean("A <i>B</i>&nbsp;C"), "A B C")

    def test_guideline_filter_is_added_to_pubmed_term(self):
        class CaptureHttp(FakeHttp):
            def __init__(self, *args):
                super().__init__(*args)
                self.urls = []

            def get(self, url: str) -> bytes:
                self.urls.append(url)
                return super().get(url)

        http = CaptureHttp({"esearchresult": {"idlist": []}}, b"")
        PubMedClient(http).search("heart failure treatment", 2, "guideline[pt] OR review[pt]")
        self.assertIn("guideline", urllib.parse.unquote(http.urls[0]))

    def test_empty_search(self):
        client = PubMedClient(FakeHttp({"esearchresult": {"idlist": []}}, b""))
        self.assertEqual(client.search("nothing", 3), [])


class TestService(unittest.TestCase):
    def test_cache_and_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            service = OnlineLiteratureService(cache_dir=Path(td), sources="pubmed")
            client = FakeClient([
                hit(source_id="1", doi="10.1/x", title="Same paper"),
                hit(source_id="2", doi="10.1/X", title="Same paper duplicate"),
                hit(source_id="3", doi="10.1/y", title="Other"),
            ])
            service.clients["pubmed"] = client
            hits, meta = service.search("query", 5)
            self.assertEqual(len(hits), 2)
            self.assertFalse(meta["cached"])
            self.assertEqual(client.calls, 1)
            hits2, meta2 = service.search("query", 5)
            self.assertEqual(len(hits2), 2)
            self.assertTrue(meta2["cached"])
            self.assertEqual(client.calls, 1)

    def test_all_sources_failing_raises(self):
        class Broken:
            def search(self, query, limit, publication_types=""):
                raise OnlineLiteratureError("HTTP 503")
        with tempfile.TemporaryDirectory() as td:
            service = OnlineLiteratureService(cache_dir=Path(td), sources="pubmed")
            service.clients["pubmed"] = Broken()
            with self.assertRaises(OnlineLiteratureError):
                service.search("query", 2)

    def test_empty_hits_are_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            service = OnlineLiteratureService(cache_dir=Path(td), sources="pubmed")
            service.clients["pubmed"] = FakeClient([])
            hits, meta = service.search("query", 2)
            self.assertEqual(hits, [])
            self.assertFalse(meta["cached"])


class TestDedup(unittest.TestCase):
    def test_local_full_text_wins_over_online_abstract(self):
        state = SDKState(config=SimpleNamespace(), retriever=None, evaluator=None)
        state.evidence["E1"] = Evidence("E1", "local full text", title="Paper", pmcid="PMC123")
        self.assertEqual(find_existing_evidence(state, hit(pmcid="pmc123")), "E1")
        self.assertEqual(find_existing_evidence(state, hit(pmcid="PMC999", doi="10.1/new")), None)


if __name__ == "__main__":
    unittest.main()
