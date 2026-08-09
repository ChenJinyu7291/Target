

"""Round-3 science-side reliability checks.

Covers NO_GO decision semantics, expanded Reviewer causal-language detection,
evidence-anchored context splitting, dataset-switch selection, split-level
context-relation metrics, and executable benchmark assertions.
"""
from __future__ import annotations

import json

import pytest

from benchmark.evaluate_context_relations import evaluate
from benchmark.generate_context_relation_goldset import build_entries
from benchmark.runner import check_assertion
from target_agent.contracts import (
    ClaimClass, EvidenceContext, EvidenceItem, SourceLocator, Stance,
    TaskContext, TaskSpec,
)
from target_agent.ranking import rank_targets
from target_agent.research_repair import (
    _replacement_dataset_score, _subcontext_refines_frozen_scope,
)
from target_agent.reviewer import Reviewer
from target_agent.tools.genetics import _context_match_detail, _harmonize_alleles
from tests.test_research_repair import _project


def _item(
    *,
    gene: str = "GENE1",
    claim_class: ClaimClass = ClaimClass.FACT,
    stance: Stance = Stance.SUPPORTS,
    direction: str = "increase",
    effect: dict | None = None,
    statement: str = "In a randomized trial cohort, knockout increased severity.",
) -> EvidenceItem:
    return EvidenceItem(
        tool_run_id="tool-1",
        gene_symbol=gene,
        claim_class=claim_class,
        statement=statement,
        source=SourceLocator(uri="https://europepmc.org/article/MED/1", source_id="src-1", version="2024-01-01"),
        source_span="source span",
        context=EvidenceContext(disease="ulcerative colitis", tissue="colon"),
        stance=stance,
        effect_direction=direction,
        effect=effect or {"omics_strength": 0.5},
        uncertainty="test uncertainty",
        quality_flags=[],
        context_match_score=0.9,
        context_match={
            "matched_disease": ["ulcerative colitis"], "matched_tissue": ["colon"],
            "context_score_origin": "tool_estimate",
        },
    )


def _task() -> TaskSpec:
    return TaskSpec(
        task_type="disease_to_target", question="Prioritize targets for ulcerative colitis",
        context=TaskContext(disease="ulcerative colitis", tissue="colon"),
    )


def test_safety_liability_forces_no_go():
    task = TaskContext(disease="ulcerative colitis", tissue="colon")
    items = [
        _item(),
        _item(stance=Stance.REFUTES, direction="unclear",
              effect={"safety": {"event": "hepatotoxicity"}}),
    ]
    ranked = rank_targets(["GENE1"], items, [], task_context=task)
    assert ranked[0].decision == "NO_GO"
    assert any("safety" in blocker.casefold() for blocker in ranked[0].safety_blockers)


def test_strong_opposing_evidence_forces_no_go():
    task = TaskContext(disease="ulcerative colitis", tissue="colon")
    items = [
        _item(),
        _item(claim_class=ClaimClass.OBSERVED, stance=Stance.REFUTES, direction="decrease"),
    ]
    ranked = rank_targets(["GENE1"], items, [], task_context=task)
    assert ranked[0].decision == "NO_GO"


def test_no_go_absent_without_safety_or_strong_opposing():
    task = TaskContext(disease="ulcerative colitis", tissue="colon")
    ranked = rank_targets(["GENE1"], [_item()], [], task_context=task)
    assert ranked[0].decision != "NO_GO"


def test_reviewer_flags_expanded_causal_language():
    reviewer = Reviewer(client=None)
    item = _item(statement="This gene drives disease and confers risk in the cohort.")
    findings = reviewer.review(_task(), [], [item])
    assert any(f.category == "causal_overreach" for f in findings)


def test_subcontext_refinement_requires_evidence_anchor():
    project = _project()
    assert _subcontext_refines_frozen_scope(project, "cell_type", "T cell") is False
    assert _subcontext_refines_frozen_scope(project, "cell_type", "T cell", evidence_value="T cell") is True
    assert _subcontext_refines_frozen_scope(project, "cell_type", "T cell", evidence_value="macrophage") is False
    assert _subcontext_refines_frozen_scope(project, "tissue", "colonic mucosa", evidence_value="anything") is True
    assert _subcontext_refines_frozen_scope(project, "tissue", "brain") is False


def test_replacement_dataset_prefers_context_match_over_first_row():
    rows = [
        {"accession": "GSE1", "status": "eligible", "context_match_score": 0.6,
         "metadata_confidence": 0.99, "case_count": 3, "control_count": 3, "sample_count": 6},
        {"accession": "GSE2", "status": "eligible", "context_match_score": 0.95,
         "metadata_confidence": 0.85, "case_count": 4, "control_count": 4, "sample_count": 8},
    ]
    assert _replacement_dataset_score(rows[1]) > _replacement_dataset_score(rows[0])


def test_context_relation_report_has_split_level_metrics():
    gold = build_entries()
    predictions = [{
        "id": entry["id"],
        "label": entry["gold"]["label"],
        "actions": entry["gold"]["required_actions"],
        "claims": [],
    } for entry in gold]
    report = evaluate(gold, predictions)
    assert set(report["by_split"]) == {"train", "validation", "test"}
    assert sum(report["split_counts"].values()) == len(gold)
    assert all(round(value, 6) == 1.0 for value in report["by_split"]["train"].values())


def test_runner_min_reference_genes_assertion(tmp_path):
    ctx = {"run_dir": tmp_path, "evidence_items": [
        {"evidence_id": "e1", "gene_symbol": "GENE1", "claim_class": "FACT", "statement": "x"},
        {"evidence_id": "e2", "gene_symbol": "GENE2", "claim_class": "OBSERVED", "statement": "y"},
    ]}
    assert check_assertion({
        "type": "min_reference_genes_in_evidence",
        "reference_genes": ["GENE1"], "min_count": 1,
    }, ctx) is None
    failure = check_assertion({
        "type": "min_reference_genes_in_evidence",
        "reference_genes": ["GENE1", "GENE3"], "min_count": 2,
    }, ctx)
    assert failure is not None


# ---------------- P3-2 / P3-9 added coverage ----------------
def _gwas_row(eaf=None):
    return {"effect_allele": "A", "other_allele": "G", "effect_allele_frequency": eaf}


def _coloc_row(eqtl_eaf=None, swapped=False):
    return {
        "gwas_effect_allele": "A", "gwas_other_allele": "G",
        "eqtl_effect_allele": ("G" if swapped else "A"),
        "eqtl_other_allele": ("A" if swapped else "G"),
        "eqtl_effect_allele_frequency": eqtl_eaf,
    }


def test_harmonize_alleles_rejects_inconsistent_eaf_for_non_palindromic():
    status, sign = _harmonize_alleles(_gwas_row(0.05), _coloc_row(0.7), False)
    assert status == "eaf_inconsistent" and sign is None
    status, sign = _harmonize_alleles(_gwas_row(0.7), _coloc_row(0.7), False)
    assert (status, sign) == ("direct", 1)
    status, sign = _harmonize_alleles(_gwas_row(0.3), _coloc_row(0.7, swapped=True), False)
    assert (status, sign) == ("swapped", -1)
    # EAF absent => legacy behaviour, orientation still resolved
    status, sign = _harmonize_alleles(_gwas_row(None), _coloc_row(None, swapped=True), False)
    assert (status, sign) == ("swapped", -1)


def test_harmonize_alleles_rejects_inconsistent_eaf_for_palindromic_with_informative_frequency():
    gwas = {"effect_allele": "A", "other_allele": "T", "effect_allele_frequency": 0.2}
    coloc = {
        "gwas_effect_allele": "A", "gwas_other_allele": "T",
        "eqtl_effect_allele": "A", "eqtl_other_allele": "T",
        "eqtl_effect_allele_frequency": 0.85,
    }
    status, _ = _harmonize_alleles(gwas, coloc, True)
    assert status == "eaf_inconsistent"
    coloc["eqtl_effect_allele_frequency"] = 0.2
    status, sign = _harmonize_alleles(gwas, coloc, True)
    assert (status, sign) == ("direct", 1)


def test_context_match_detail_reports_synonyms_rules_and_rejects_mismatch():
    from types import SimpleNamespace

    context = SimpleNamespace(
        task=SimpleNamespace(context=TaskContext(disease="uc", tissue="pulmonary", cell_type="t cell"))
    )
    asset = SimpleNamespace(tissue="lung", cell_type="CD8+ T cell")
    score, detail = _context_match_detail(context, asset)
    assert score == 1.0
    assert detail["matched_terms"] and detail["match_rules"]

    context2 = SimpleNamespace(
        task=SimpleNamespace(context=TaskContext(disease="uc", tissue="lung", cell_type="macrophage"))
    )
    asset2 = SimpleNamespace(tissue="brain", cell_type="t cell")
    score2, detail2 = _context_match_detail(context2, asset2)
    assert score2 < 0.5
    assert any("no_match" in rule for rule in detail2["match_rules"])


def test_runner_no_causal_claims_scans_evidence_statements(tmp_path):
    (tmp_path / "claims.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "evidence_items.jsonl").write_text(
        json.dumps({
            "evidence_id": "e1", "gene_symbol": "GENE1", "claim_class": "FACT",
            "statement": "this gene causes disease",
        }) + "\n",
        encoding="utf-8",
    )
    ctx = {"run_dir": tmp_path, "evidence_items": []}
    failure = check_assertion({"type": "no_causal_claims"}, ctx)
    assert failure is not None
    assert "evidence" in failure
