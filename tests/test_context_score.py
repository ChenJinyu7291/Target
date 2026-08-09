"""P1-1: tool-independent deterministic context scoring.

Formal ranking must recompute the context score from match metadata; a
hard-coded tool estimate alone can never pass the 0.5 formal gate.
"""
from __future__ import annotations

from target_agent.context_score import DISEASE_MISS_CAP, recompute_context_score
from target_agent.contracts import (
    ClaimClass, EvidenceContext, EvidenceItem, SourceLocator, Stance, TaskContext,
)
from target_agent.ranking import rank_targets


def _evidence(
    *,
    disease: str = "ulcerative colitis",
    context_match: dict | None = None,
    context_match_score: float = 0.9,
    claim_class: ClaimClass = ClaimClass.FACT,
    statement: str = "In a randomized clinical trial cohort, knockout increased disease severity.",
) -> EvidenceItem:
    return EvidenceItem(
        tool_run_id="tool-1",
        gene_symbol="GENE1",
        claim_class=claim_class,
        statement=statement,
        source=SourceLocator(uri="https://europepmc.org/article/MED/1", source_id="src-1", version="2024-01-01"),
        source_span="source span",
        context=EvidenceContext(disease=disease, assay="literature extraction"),
        stance=Stance.SUPPORTS,
        effect_direction="increase",
        uncertainty="test uncertainty",
        quality_flags=[],
        context_match_score=context_match_score,
        context_match=context_match,
    )


def test_disease_miss_hard_caps_total_below_formal_gate():
    task = TaskContext(disease="Crohn disease", tissue="colon", cell_type="T cell", assay="RNA-seq")
    meta = {
        "matched_disease": ["ulcerative colitis"],
        "matched_tissue": ["colon"],
        "matched_cell": ["T cell"],
        "matched_assay": ["RNA-seq"],
    }
    scored = recompute_context_score(task, None, meta)
    assert scored.disease == 0.0
    assert scored.score <= DISEASE_MISS_CAP < 0.5
    assert "hard cap below the formal gate" in " ".join(scored.notes)


def test_dimension_hits_are_reported_deterministically():
    task = TaskContext(disease="ulcerative colitis", tissue="colon", cell_type="T cell", assay="RNA-seq")
    meta = {
        "matched_disease": ["ulcerative colitis"],
        "matched_tissue": ["colon"],
        "matched_cell": ["T cell"],
        "matched_assay": ["RNA-seq"],
    }
    first = recompute_context_score(task, None, meta)
    second = recompute_context_score(task, None, meta)
    assert first.score == 1.0
    assert first == second
    assert first.origin == "recomputed"
    assert first.hits["disease"] == ["ulcerative colitis"]
    assert first.hits["tissue"] == ["colon"]
    assert first.hits["cell"] == ["T cell"]
    assert first.hits["assay"] == ["RNA-seq"]


def test_hardcoded_high_score_alone_does_not_pass_rank_targets_formal_filter():
    task_context = TaskContext(disease="Crohn disease", tissue="colon", cell_type="T cell", assay="RNA-seq")
    item = _evidence(
        disease="ulcerative colitis",
        context_match_score=0.9,
        context_match={
            "matched_disease": ["ulcerative colitis"],
            "matched_assay": ["literature extraction"],
            "context_score_origin": "tool_estimate",
            "tool_estimate": 0.9,
        },
    )
    ranked = rank_targets(["GENE1"], [item], [], task_context=task_context)
    row = ranked[0]
    assert row.scores.total == 0.0
    assert row.decision == "INSUFFICIENT_EVIDENCE"
    assert row.context_score_origin == "recomputed"
    assert "hard cap below the formal gate" in " ".join(row.context_score_notes)


def test_missing_match_metadata_is_conservative_not_tool_score():
    task_context = TaskContext(disease="Crohn disease")
    item = _evidence(disease="ulcerative colitis", context_match_score=0.95, context_match=None)
    ranked = rank_targets(["GENE1"], [item], [], task_context=task_context)
    row = ranked[0]
    assert row.scores.total == 0.0
    assert row.decision == "INSUFFICIENT_EVIDENCE"