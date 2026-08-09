"""P1-4: shared-corpus provenance must be recoverable and verifiable.

Chunks recalled from the shared corpus without an authoritative source row may
not be dressed up with a guessed Europe PMC URI; provenance_missing items are
excluded from the formal literature pillar.
"""
from __future__ import annotations

from target_agent.contracts import (
    ClaimClass, EvidenceContext, EvidenceItem, SourceLocator, Stance, TaskContext,
)
from target_agent.ranking import rank_targets
from target_agent.tools.literature import EuropePMCRAGTool, stable_chunks


def _item(
    *,
    uri: str = "https://europepmc.org/article/MED/1",
    version: str = "2024-01-01",
    sha256: str | None = None,
    version_meta: dict | None = None,
    quality_flags: list[str] | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        tool_run_id="tool-1",
        gene_symbol="GENE1",
        claim_class=ClaimClass.FACT,
        statement="In a randomized clinical trial cohort, knockout increased disease severity.",
        source=SourceLocator(
            uri=uri, source_id="src-1", version=version,
            sha256=sha256, version_meta=version_meta,
        ),
        source_span="source span",
        context=EvidenceContext(disease="ulcerative colitis", assay="literature extraction"),
        stance=Stance.SUPPORTS,
        effect_direction="increase",
        uncertainty="test uncertainty",
        quality_flags=quality_flags or [],
        context_match_score=0.9,
        context_match={
            "matched_disease": ["ulcerative colitis"],
            "matched_assay": ["literature extraction"],
            "context_score_origin": "tool_estimate",
        },
    )


def test_shared_corpus_recall_carries_source_and_snapshot_provenance(tmp_path):
    db = tmp_path / "corpus.sqlite"
    chunks = stable_chunks("PMID123", "GENE1 associated with disease text " * 4)
    size = EuropePMCRAGTool()._update_shared_corpus(
        db, chunks,
        {"PMID123": {"uri": "https://europepmc.org/article/MED/123", "version": "2024-01-01"}},
    )
    assert size == len(chunks)
    recalled = EuropePMCRAGTool()._recall(db, ["GENE1"], limit=5)
    assert recalled
    assert all(row["uri"] == "https://europepmc.org/article/MED/123" for row in recalled)
    assert all(row["version"] == "2024-01-01" for row in recalled)
    assert all(row["ingested_at"] for row in recalled)
    assert all(len(row["corpus_snapshot_sha256"]) == 64 for row in recalled)


def test_shared_corpus_recall_without_source_meta_leaves_uri_empty(tmp_path):
    db = tmp_path / "corpus.sqlite"
    chunks = stable_chunks("OLD1", "GENE1 associated with disease text " * 4)
    EuropePMCRAGTool()._update_shared_corpus(db, chunks, {})
    recalled = EuropePMCRAGTool()._recall(db, ["GENE1"], limit=5)
    assert recalled
    assert recalled[0]["uri"] == ""
    assert recalled[0]["corpus_snapshot_sha256"]


def test_provenance_missing_shared_corpus_item_is_excluded_from_literature_pillar():
    item = _item(
        uri="shared-corpus://OLD1",
        version="shared-corpus",
        quality_flags=["provenance_missing"],
    )
    ranked = rank_targets(
        ["GENE1"], [item], [],
        task_context=TaskContext(disease="ulcerative colitis"),
    )
    row = ranked[0]
    assert row.scores.mechanism == 0.0
    assert "No span-validated literature claim for this target." in row.evidence_gaps


def test_shared_corpus_item_with_snapshot_digest_counts_when_uri_is_authoritative():
    snapshot = "a" * 64
    item = _item(
        uri="https://europepmc.org/article/MED/1",
        version="shared-corpus:aaaaaaaaaaaa",
        sha256=snapshot,
        version_meta={"corpus_snapshot_sha256": snapshot, "ingested_at": "2026-01-01T00:00:00Z"},
    )
    ranked = rank_targets(
        ["GENE1"], [item], [],
        task_context=TaskContext(disease="ulcerative colitis"),
    )
    row = ranked[0]
    assert row.scores.mechanism == 4.0
    assert "No span-validated literature claim for this target." not in row.evidence_gaps