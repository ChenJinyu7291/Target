"""P1-5: non-COMPLETED terminal status globally forbids GO and marks cards."""
from __future__ import annotations

from target_agent.cards import build_cards
from target_agent.contracts import (
    ClaimClass, CoverageStatus, EvidenceContext, EvidenceItem, SourceLocator,
    Stance, TaskContext, TaskSpec, TerminalStatus, ToolCapability, ToolResult,
    ToolStatus,
)
from target_agent.ranking import rank_targets


def _literature_item() -> EvidenceItem:
    return EvidenceItem(
        tool_run_id="tool-lit",
        gene_symbol="GENE1",
        claim_class=ClaimClass.FACT,
        statement="In a randomized clinical trial cohort, knockout increased disease severity.",
        source=SourceLocator(uri="https://europepmc.org/article/MED/1", source_id="src-1", version="2024-01-01"),
        source_span="source span",
        context=EvidenceContext(disease="ulcerative colitis", assay="literature extraction"),
        stance=Stance.SUPPORTS,
        effect_direction="increase",
        uncertainty="test uncertainty",
        quality_flags=[],
        context_match_score=0.9,
        context_match={
            "matched_disease": ["ulcerative colitis"],
            "matched_assay": ["literature extraction"],
            "context_score_origin": "tool_estimate",
        },
    )


def _perturbation_item() -> EvidenceItem:
    return EvidenceItem(
        tool_run_id="tool-perturb",
        gene_symbol="GENE1",
        claim_class=ClaimClass.OBSERVED,
        statement="Observed perturbation in a functional assay aligns with ulcerative colitis.",
        source=SourceLocator(uri="https://europepmc.org/article/MED/2", source_id="src-2", version="2024-01-01"),
        source_span="observed perturbation",
        context=EvidenceContext(disease="ulcerative colitis", assay="functional assay"),
        stance=Stance.SUPPORTS,
        effect_direction="increase",
        effect={"disease_alignment": 0.5},
        uncertainty="test uncertainty",
        quality_flags=[],
        context_match_score=0.9,
        context_match={
            "matched_disease": ["ulcerative colitis"],
            "matched_assay": ["functional assay"],
            "context_score_origin": "tool_estimate",
        },
    )


def _open_targets_result() -> ToolResult:
    return ToolResult(
        tool_name="open_targets",
        tool_version="1.0.0",
        status=ToolStatus.SUCCESS,
        coverage_status=CoverageStatus.COVERED,
        context_match_score=0.9,
        inputs={},
        outputs={
            "associations": [
                {"gene": "GENE1", "known_drugs": [{"drugId": "D1", "phase": 3}]},
            ],
        },
        capability=ToolCapability(validation_scope="test"),
    )


def _ranked(terminal_status: TerminalStatus | None):
    return rank_targets(
        ["GENE1"],
        [_literature_item(), _perturbation_item()],
        [_open_targets_result()],
        task_context=TaskContext(disease="ulcerative colitis"),
        terminal_status=terminal_status,
    )


def test_non_completed_terminal_forbids_go_in_ranking():
    ok = _ranked(TerminalStatus.COMPLETED)
    assert ok[0].decision == "GO"

    gapped = _ranked(TerminalStatus.COMPLETED_WITH_GAPS)
    assert gapped[0].decision == "CONDITIONAL_GO"
    assert any("Terminal status is not COMPLETED" in blocker for blocker in gapped[0].safety_blockers)
    assert any("Terminal status carries unresolved gaps" in gap for gap in gapped[0].evidence_gaps)

    failed = _ranked(TerminalStatus.FAILED)
    assert failed[0].decision == "CONDITIONAL_GO"


def test_cards_downgrade_go_and_show_gaps_marker_on_non_completed_terminal():
    task = TaskSpec(
        task_type="disease_to_target", question="Find traceable UC targets",
        context=TaskContext(disease="ulcerative colitis"),
    )
    ranked = _ranked(TerminalStatus.COMPLETED)
    ok_cards = build_cards(task, ranked, terminal_status=TerminalStatus.COMPLETED)
    assert ok_cards[0].decision == "GO"
    assert not any("GAPS:" in limitation for limitation in ok_cards[0].limitations)
    assert any("Context score source:" in limitation for limitation in ok_cards[0].limitations)

    gap_cards = build_cards(task, ranked, terminal_status=TerminalStatus.COMPLETED_WITH_GAPS)
    assert gap_cards[0].decision == "CONDITIONAL_GO"
    assert any("GAPS:" in limitation for limitation in gap_cards[0].limitations)