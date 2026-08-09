"""P1-2: literature FACT semantics and independent-pillar accounting.

Co-mention statements and database-aggregate/registry evidence must never
satisfy the independent literature pillar; directional, design-anchored,
source-versioned FACT claims may.
"""
from __future__ import annotations

from target_agent.contracts import (
    ClaimClass, EvidenceContext, EvidenceItem, SourceLocator, Stance, TaskContext,
)
from target_agent.ranking import rank_targets
from target_agent.tools.literature import _claim_class_for


def _item(
    *,
    claim_class: ClaimClass,
    statement: str,
    version: str | None = "2024-01-01",
    uri: str = "https://europepmc.org/article/MED/1",
    effect_direction: str = "increase",
    stance: Stance = Stance.SUPPORTS,
) -> EvidenceItem:
    return EvidenceItem(
        tool_run_id="tool-1",
        gene_symbol="GENE1",
        claim_class=claim_class,
        statement=statement,
        source=SourceLocator(uri=uri, source_id="src-1", version=version),
        source_span="source span",
        context=EvidenceContext(disease="ulcerative colitis", assay="literature extraction"),
        stance=stance,
        effect_direction=effect_direction,
        uncertainty="test uncertainty",
        quality_flags=[],
        context_match_score=0.9,
        context_match={
            "matched_disease": ["ulcerative colitis"],
            "matched_assay": ["literature extraction"],
            "context_score_origin": "tool_estimate",
        },
    )


def test_co_mention_statement_is_unverified_not_fact():
    claim = {
        "statement": "The source explicitly co-mentions GENE1 and ulcerative colitis; direction requires scientific review.",
        "exact_quote": "GENE1 and ulcerative colitis co-mention.",
    }
    assert _claim_class_for(claim) == ClaimClass.UNVERIFIED


def test_direction_with_design_anchor_keeps_fact():
    claim = {
        "statement": "In a randomized clinical trial cohort, knockout increased disease severity.",
        "exact_quote": "knockout increased disease severity.",
    }
    assert _claim_class_for(claim) == ClaimClass.FACT


def test_directionless_or_designless_llm_claim_is_inferred():
    directionless = {"statement": "GENE1 is associated with ulcerative colitis.", "exact_quote": "associated with"}
    assert _claim_class_for(directionless) == ClaimClass.INFERRED
    designless = {"statement": "GENE1 increases ulcerative colitis.", "exact_quote": "increases"}
    assert _claim_class_for(designless) == ClaimClass.INFERRED


def test_co_mention_item_is_not_an_independent_literature_pillar():
    item = _item(
        claim_class=ClaimClass.UNVERIFIED,
        statement="The source explicitly co-mentions GENE1 and ulcerative colitis; direction requires scientific review.",
    )
    ranked = rank_targets(
        ["GENE1"], [item], [],
        task_context=TaskContext(disease="ulcerative colitis"),
    )
    row = ranked[0]
    assert row.scores.mechanism == 0.0
    assert "No span-validated literature claim for this target." in row.evidence_gaps
    assert row.decision == "INSUFFICIENT_EVIDENCE"


def test_downgraded_aggregate_evidence_is_not_an_independent_literature_pillar():
    # Open Targets / ClinicalTrials.gov evidence is now INFERRED (P1-2).
    item = _item(
        claim_class=ClaimClass.INFERRED,
        statement="Open Targets reports a human-genetic association score for GENE1 and ulcerative colitis.",
        version="live-or-cache",
    )
    ranked = rank_targets(
        ["GENE1"], [item], [],
        task_context=TaskContext(disease="ulcerative colitis"),
    )
    row = ranked[0]
    assert row.scores.mechanism == 0.0
    assert "No span-validated literature claim for this target." in row.evidence_gaps


def test_directional_fact_with_verifiable_source_version_counts():
    item = _item(
        claim_class=ClaimClass.FACT,
        statement="In a randomized clinical trial cohort, knockout increased disease severity.",
        version="2024-01-01",
    )
    ranked = rank_targets(
        ["GENE1"], [item], [],
        task_context=TaskContext(disease="ulcerative colitis"),
    )
    row = ranked[0]
    assert row.scores.mechanism == 4.0
    assert "No span-validated literature claim for this target." not in row.evidence_gaps


def test_fact_without_verifiable_source_version_does_not_count():
    item = _item(
        claim_class=ClaimClass.FACT,
        statement="In a randomized clinical trial cohort, knockout increased disease severity.",
        version=None,
    )
    ranked = rank_targets(
        ["GENE1"], [item], [],
        task_context=TaskContext(disease="ulcerative colitis"),
    )
    row = ranked[0]
    assert row.scores.mechanism == 0.0
    assert "No span-validated literature claim for this target." in row.evidence_gaps