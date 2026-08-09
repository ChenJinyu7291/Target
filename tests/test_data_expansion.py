"""Data-expansion tools: ClinicalTrials.gov connector and RAG v2.2 upgrades."""
from __future__ import annotations

import json
from pathlib import Path

from target_agent.contracts import TaskContext, TaskSpec
from target_agent.tools.base import ToolContext
from target_agent.tools.clinicaltrials import ClinicalTrialsGovTool
from target_agent.tools.literature import EuropePMCRAGTool, parse_fulltext_sections, stable_chunks


class FakeResponse:
    def __init__(self, payload=None, text: str | None = None, status: int = 200):
        self._payload = payload
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")
        self.status_code = status

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"http {self.status_code}")


def ctx(tmp_path: Path, genes=None, disease="ulcerative colitis", run_name="run"):
    return ToolContext(
        task=TaskSpec(task_type="disease_to_target", question="test",
                      context=TaskContext(disease=disease)),
        run_dir=tmp_path / run_name, cache_dir=tmp_path / "cache",
        candidate_genes=genes or ["IL2", "CD27"],
    )


# ---------------------------------------------------------------- clinical trials
TRIALS_PAYLOAD = {
    "studies": [
        {"protocolSection": {
            "identificationModule": {"nctId": "NCT00000001", "briefTitle": "Anti-IL2 therapy in ulcerative colitis"},
            "statusModule": {"overallStatus": "RECRUITING", "lastUpdateSubmitDate": "2026-01-01"},
            "designModule": {"phases": ["PHASE2"]},
            "armsInterventionsModule": {"interventions": [
                {"type": "BIOLOGICAL", "name": "Anti-IL2 monoclonal antibody", "description": "Targets IL2 pathway"}]},
        }},
        {"protocolSection": {
            "identificationModule": {"nctId": "NCT00000002", "briefTitle": "Unrelated dietary study"},
            "statusModule": {"overallStatus": "TERMINATED", "whyStopped": "Futility"},
            "designModule": {"phases": ["PHASE3"]},
            "armsInterventionsModule": {"interventions": [
                {"type": "OTHER", "name": "Placebo diet", "description": "No molecular target"}]},
        }},
    ]
}


class TrialsSession:
    def get(self, url, params=None, timeout=None):
        return FakeResponse(payload=TRIALS_PAYLOAD)


def test_clinical_trials_gene_named_only(tmp_path):
    tool = ClinicalTrialsGovTool(session=TrialsSession())
    out = tool.run(ctx(tmp_path, genes=["IL2"]))
    assert out.result.status.value == "success"
    assert out.result.outputs["studies_seen"] == 2
    # 只有干预/标题显式命名 IL2 的记录能成为证据
    assert len(out.evidence) == 1
    ev = out.evidence[0]
    assert ev.gene_symbol == "IL2" and ev.claim_class.value == "INFERRED"
    assert ev.source.uri == "https://clinicaltrials.gov/study/NCT00000001"
    assert "Phase 2" in ev.statement


def test_clinical_trials_stopped_flagged_and_cache_only_fails(tmp_path):
    payload = {"studies": [{"protocolSection": {
        "identificationModule": {"nctId": "NCT00000003", "briefTitle": "CD27 agonist trial"},
        "statusModule": {"overallStatus": "WITHDRAWN", "whyStopped": "Business decision"},
        "designModule": {"phases": ["PHASE1"]},
        "armsInterventionsModule": {"interventions": [
            {"type": "BIOLOGICAL", "name": "CD27 agonist", "description": "agonist of CD27"}]},
    }}]}

    class S:
        def get(self, url, params=None, timeout=None):
            return FakeResponse(payload=payload)

    out = ClinicalTrialsGovTool(session=S()).run(ctx(tmp_path, genes=["CD27"]))
    assert len(out.evidence) == 1
    assert out.evidence[0].stance.value == "uncertain"
    assert any(flag == "trial_stopped" for flag in out.evidence[0].quality_flags)

    class Broken:
        def get(self, url, params=None, timeout=None):
            import requests
            raise requests.ConnectionError("offline")

    context = ctx(tmp_path, genes=["IL2"], run_name="run_broken")
    context.settings = context.settings.model_copy(update={"cache_only": True})
    failed = ClinicalTrialsGovTool(session=Broken()).run(context)
    assert failed.result.status.value == "failed"
    assert failed.evidence == []


# ---------------------------------------------------------------- RAG v2.2
FULLTEXT_XML = """<?xml version="1.0"?>
<article><body>
<sec><title>Results</title><p>{body}</p></sec>
</body></article>"""

SENTENCE = ("IL2 blockade reduced mucosal inflammation in patients with ulcerative colitis "
            "in the randomized cohort. ")


def _fulltext_body() -> str:
    filler = "The cohort was followed for twelve weeks with endoscopic scoring. "
    return SENTENCE + filler * 12


class RAGSession:
    def get(self, url, params=None, timeout=None):
        if "fullTextXML" in url:
            return FakeResponse(text=FULLTEXT_XML.format(body=_fulltext_body()))
        payload = {"resultList": {"result": [{
            "pmid": "12345678", "pmcid": "PMC9999999", "isOpenAccess": "Y",
            "title": "IL2 in ulcerative colitis", "firstPublicationDate": "2024-01-01",
            "abstractText": "We studied IL2 and CD27 in ulcerative colitis. " + "Background text. " * 20,
        }]}}
        return FakeResponse(payload=payload)


def test_fulltext_enrichment_and_shared_corpus(tmp_path):
    tool = EuropePMCRAGTool(session=RAGSession(), llm=None)
    first = tool.run(ctx(tmp_path, run_name="run_a"))
    assert first.result.outputs["fulltext_articles"] == 1
    assert first.result.outputs["rerank_backend"] == "bm25_only"
    assert first.result.outputs["search_hits_are_evidence"] is False
    fulltext_ev = [e for e in first.evidence if e.source.section.startswith("fulltext:")]
    assert fulltext_ev, "expected at least one full-text-section claim"
    assert all(e.source_span for e in first.evidence)
    shared = tmp_path / "cache" / "literature_corpus.sqlite"
    assert shared.exists()
    size_first = first.result.outputs["shared_corpus_chunks"]
    # 第二次运行(不同 run_dir): 共享语料继续增长或持平, 且召回优先本次来源
    second = tool.run(ctx(tmp_path, run_name="run_b"))
    assert second.result.outputs["shared_corpus_chunks"] >= size_first


def test_fulltext_parser_and_chunk_sections():
    sections = parse_fulltext_sections(FULLTEXT_XML.format(body=_fulltext_body()))
    assert sections and sections[0]["section"] == "Results"
    assert "IL2 blockade" in sections[0]["text"]
    chunks = stable_chunks("PMID1", "word " * 800, section="fulltext:Results")
    assert all(c["section"] == "fulltext:Results" for c in chunks)
    assert parse_fulltext_sections("<broken") == []

# ---------------------------------------------------------------- literature LLM stage cache
class FakeStageLLM:
    model = "step-test"

    def __init__(self):
        self.calls = 0

    def json_completion(self, system, user):
        self.calls += 1
        payload = json.loads(user)
        if "Rank text chunks" in system:
            return {"ranked_chunk_ids": [c["chunk_id"] for c in reversed(payload["chunks"])]}
        text0 = payload["chunks"][0]["text"]
        return {"claims": [
            {"gene": "IL2", "chunk_id": payload["chunks"][0]["chunk_id"],
             "exact_quote": text0[:40], "stance": "supports",
             "statement": "IL2 blockade reduced mucosal inflammation in UC."},
            {"gene": "FAKE", "chunk_id": payload["chunks"][0]["chunk_id"],
             "exact_quote": "not present in source", "stance": "supports",
             "statement": "bogus"},
        ]}


def test_literature_llm_stage_cache_reuses_rerank_and_extract(tmp_path):
    chunks = [
        {"chunk_id": f"c{i}", "source_id": "s1", "section": "abstract",
         "text": f"IL2 blockade reduced mucosal inflammation in ulcerative colitis cohort {i}. " * 3}
        for i in range(5)
    ]
    llm = FakeStageLLM()
    tool = EuropePMCRAGTool(session=RAGSession(), llm=llm)
    cache_dir = tmp_path / "cache"
    ordered1, backend1, cached1 = tool._llm_rerank("ulcerative colitis", ["IL2"], chunks, cache_dir)
    assert backend1 == "step_rerank" and cached1 is False
    assert [c["chunk_id"] for c in ordered1] == ["c4", "c3", "c2", "c1", "c0"]
    ordered2, backend2, cached2 = tool._llm_rerank("ulcerative colitis", ["IL2"], chunks, cache_dir)
    assert backend2 == "step_rerank_cached" and cached2 is True
    assert ordered1 == ordered2
    claims1, extract_cached1 = tool._llm_extract("ulcerative colitis", ["IL2"], ordered1, cache_dir)
    assert extract_cached1 is False
    assert [c["gene"] for c in claims1] == ["IL2"]
    claims2, extract_cached2 = tool._llm_extract("ulcerative colitis", ["IL2"], ordered1, cache_dir)
    assert extract_cached2 is True and claims1 == claims2
    # rerank and extract each hit the model exactly once; repeats replay the cache
    assert llm.calls == 2
