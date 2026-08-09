"""Paper RAG chunk store, retrieval and planner few-shot tests (no network)."""
from __future__ import annotations

import json

import pytest

from target_agent.paper_rag import (
    PaperChunk, PaperRagStore, build_chunks, chunk_text, fetch_chunks,
)
from target_agent.paper_strategy import PlannerFewShotBuilder
from target_agent.pattern_extraction import PaperMeta


def _meta(pmid: str = "12345678", abstract: str | None = None) -> PaperMeta:
    return PaperMeta(
        pmid=pmid,
        title="GWAS and single-cell dissection of colitis",
        journal="Nature",
        year=2024,
        doi="10.1/test",
        pmcid="PMC123",
        abstract=abstract or (
            "GWAS identified risk loci; single-cell RNA-seq mapped cell types; "
            "CRISPR screen validated targets; drug assessment followed."
        ),
        source_text="ignored for rag tests",
        source_material="abstract",
    )


def _chunk(overrides: dict | None = None) -> PaperChunk:
    data = {
        "chunk_id": "chunk-12345678-abstract-000",
        "pmid": "12345678",
        "title": "GWAS and single-cell dissection of colitis",
        "journal": "Nature",
        "year": 2024,
        "doi": "10.1/test",
        "pmcid": "PMC123",
        "source_material": "abstract",
        "chunk_index": 0,
        "text": "GWAS identified risk loci; single-cell RNA-seq mapped cell types; "
                "CRISPR screen validated targets; drug assessment followed.",
        "lane_tags": ["genetics", "single_cell", "perturbation"],
        "disease_tags": ["colitis"],
    }
    if overrides:
        data.update(overrides)
    return PaperChunk.model_validate(data)


def test_chunk_text_respects_size_and_produces_overlap():
    text = " ".join(f"sentence number {i}" for i in range(30))
    chunks = chunk_text(text, chunk_size=200, overlap=20)
    assert len(chunks) >= 2
    assert all(chunks)
    assert all(len(chunk) <= 210 for chunk in chunks)
    assert chunk_text("", chunk_size=200, overlap=20) == []
    with pytest.raises(ValueError):
        chunk_text(text, chunk_size=50, overlap=20)


def test_store_deduplicates_and_manifest(tmp_path):
    store = PaperRagStore(tmp_path / "chunks.jsonl")
    chunk = _chunk()
    assert store.add(chunk)
    assert not store.add(chunk)
    assert store.get(chunk.chunk_id) is not None
    manifest = store.write_manifest()
    assert manifest["chunks"] == 1
    assert manifest["records"][0]["chunk_id"] == chunk.chunk_id
    with pytest.raises(ValueError):
        store.add(_chunk({"digest": "0" * 64}))
    assert store.corpus_card()["papers"] == 1


def test_search_scores_disease_query_and_lanes(tmp_path):
    store = PaperRagStore(tmp_path / "chunks.jsonl")
    store.add(_chunk())
    store.add(_chunk({
        "chunk_id": "chunk-12345678-abstract-001",
        "text": "A chemistry methods note without disease signal.",
        "lane_tags": [],
        "disease_tags": [],
    }))
    hits = store.search(
        disease="ulcerative colitis",
        lanes_available=["genetics", "single_cell", "perturbation"],
    )
    assert hits
    assert hits[0].chunk.chunk_id == "chunk-12345678-abstract-000"
    assert hits[0].score > 0
    assert store.search(disease="zebra") == []


def test_search_penalizes_unavailable_lanes(tmp_path):
    store = PaperRagStore(tmp_path / "chunks.jsonl")
    store.add(_chunk())
    hits = store.search(disease="colitis", lanes_available=["literature"])
    # chunk lanes (genetics/single_cell/perturbation) unavailable => score can
    # still be positive but the reason records the penalty; retrieval remains
    # deterministic rather than empty, so planners can explain the gap.
    assert hits
    assert any("unavailable" in reason for reason in hits[0].matched_reason)


def test_build_chunks_uses_abstract_and_lane_tags():
    meta = _meta(abstract=(
        "GWAS and eQTL anchored the locus; CRISPR screen confirmed the gene; "
        "drug target assessment followed."
    ))
    chunks = build_chunks(meta, context_tags=["gwas_target", "perturbation_screen"])
    assert chunks
    assert chunks[0].pmid == meta.pmid
    assert chunks[0].source_material == "abstract"
    assert "genetics" in chunks[0].lane_tags
    assert "perturbation" in chunks[0].lane_tags
    assert chunks[0].digest == chunks[0].compute_digest()


def test_fetch_chunks_uses_injected_fetcher():
    class FakeFetcher:
        def fetch(self, pmid: str) -> PaperMeta | None:
            return _meta(pmid=pmid)

    chunks = fetch_chunks("12345678", FakeFetcher())
    assert chunks
    assert chunks[0].pmid == "12345678"

    class EmptyFetcher:
        def fetch(self, pmid: str) -> None:
            return None

    assert fetch_chunks("99999999", EmptyFetcher()) == []


def test_few_shot_builder_paper_evidence(tmp_path):
    store = PaperRagStore(tmp_path / "chunks.jsonl")
    store.add(_chunk())
    builder = PlannerFewShotBuilder(
        None, paper_rag=store, paper_top_k=2,
    )
    rows = builder.build_paper_evidence(
        disease="ulcerative colitis",
        data_availability={"genetics": True, "single_cell": True, "perturbation": True},
    )
    assert rows
    assert rows[0]["kind"] == "paper_rag"
    assert rows[0]["strategy_hint_not_evidence"] is True
    assert rows[0]["pmid"] == "12345678"
    assert len(rows[0]["snippet"]) <= 500
    assert builder.build_paper_evidence(disease="") == []
    assert PlannerFewShotBuilder(None).build_paper_evidence(disease="colitis") == []


def test_research_plan_persists_paper_evidence(tmp_path):
    from target_agent.research_contracts import ResearchGoal, ResearchProjectSpec
    from target_agent.research_modules import ModuleDescriptor, ResearchModuleRegistry
    from target_agent.research_planner import ResearchPlanner

    class StubModule:
        def __init__(self, name: str):
            self.descriptor = ModuleDescriptor(
                name=name, description=f"Typed test capability for {name}",
                input_types=("object",), output_types=("object",),
                execution_policy="typed_local",
            )

        def execute(self, context):  # pragma: no cover - planner test
            raise AssertionError("module execution is outside planner scope")

    modules = ResearchModuleRegistry([StubModule(name) for name in (
        "project_brief", "literature_search", "hypothesis_generation",
        "independent_review", "research_report", "target_discovery",
    )])
    rag = PaperRagStore(tmp_path / "chunks.jsonl")
    rag.add(_chunk())

    class FakeClient:
        model = "step-test"

        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def json_completion(self, system: str, user: str):
            self.calls.append((system, user))
            template = json.loads(user)["required_template"]
            return {"items": template["items"], "rationale": "RAG few-shot."}

    client = FakeClient()
    planner = ResearchPlanner(
        modules, client, pattern_store=None, paper_rag=rag, paper_top_k=2,
    )
    spec = ResearchProjectSpec(
        project_id="project-test",
        title="Colitis target discovery",
        domain="disease_target_discovery",
        goal=ResearchGoal(
            question="Find targets for colitis",
            success_criteria=["Evidence-bearing conclusions are source traceable."],
            deliverables=["A reviewed research report."],
        ),
        context={
            "target_task_spec": {
                "task_type": "disease_to_target",
                "context": {
                    "disease": "ulcerative colitis",
                    "gwas_summary_stats": "fixtures/gwas.tsv",
                    "preferred_dataset_accessions": ["GSE1"],
                },
            }
        },
        max_work_items=12,
    )
    plan = planner.create_plan(spec)
    assert plan.paper_evidence
    assert plan.paper_evidence[0]["kind"] == "paper_rag"
    assert "+paper-rag:1" in plan.planner_backend
    sent = json.loads(client.calls[0][1])
    assert sent["paper_evidence"][0]["pmid"] == "12345678"
    assert sent["paper_evidence"][0]["strategy_hint_not_evidence"] is True


def test_domain_planner_injects_paper_evidence(tmp_path):
    from target_agent.contracts import TaskSpec
    from target_agent.planner import Planner
    from target_agent.tools.base import ToolRegistry

    from fakes import FakeGenericOmics, FakeLiterature, FakeOpenTargets

    rag = PaperRagStore(tmp_path / "chunks.jsonl")
    rag.add(_chunk())

    class FakeClient:
        model = "step-test"
        last_request_meta: dict = {}

        def __init__(self):
            self.last_user = ""

        def json_completion(self, system: str, user: str) -> dict:
            self.last_user = user
            return json.loads(user)["required_template"]

    client = FakeClient()
    registry = ToolRegistry([FakeGenericOmics(), FakeOpenTargets(), FakeLiterature()])
    planner = Planner(client, registry, pattern_store=None, paper_rag=rag, paper_top_k=2)
    task = TaskSpec(
        task_type="disease_to_target",
        question="Discover traceable targets for ulcerative colitis",
        context={"disease": "ulcerative colitis"},
    )
    plan = planner.create_plan(task)
    assert planner.last_paper_evidence
    assert "+paper-rag:1" in plan.planner_backend
    sent = json.loads(client.last_user)
    assert sent["paper_evidence"][0]["strategy_hint_not_evidence"] is True
    assert "paper_evidence" in sent

    plain = Planner(client, registry, pattern_store=None)
    plain.create_plan(task)
    assert plain.last_paper_evidence == []

def test_rag_cache_hits_and_invalidates_on_external_change(tmp_path):
    store = PaperRagStore(tmp_path / "chunks.jsonl")
    assert store.add(_chunk()) is True
    # Force a cold cache so the counting wrapper observes the first load.
    store._rows = None
    store._row_stat = None
    calls: list[int] = []
    original_load = store._load

    def counting_load():
        calls.append(1)
        return original_load()

    store._load = counting_load  # type: ignore[method-assign]

    # Repeated reads hit the process-local cache instead of re-reading JSONL.
    assert store.search(disease="colitis")
    assert store.get("chunk-12345678-abstract-000") is not None
    assert store.corpus_card()["chunks"] == 1
    assert len(calls) == 1

    # An external writer changes size/mtime, so the next read reloads once.
    extra = _chunk({
        "chunk_id": "chunk-12345678-abstract-001",
        "text": "A chemistry methods note without disease signal.",
        "lane_tags": [],
        "disease_tags": [],
    })
    with store.path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(extra.model_dump_json() + "\n")
    assert store.get("chunk-12345678-abstract-001") is not None
    assert store.corpus_card()["chunks"] == 2
    assert len(calls) == 2
