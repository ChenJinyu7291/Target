"""Direct unit tests for shared candidate-universe policies.

The genetics candidate policy is correctness-critical: it decides which
locus-to-gene candidates may enter formal scoring. These tests exercise
candidate_policy.py directly instead of only through runtime fixtures.
"""
from __future__ import annotations

import pytest

from target_agent.candidate_policy import (
    formal_gwas_candidates,
    initial_candidate_genes,
    merge_candidates_for_task,
)
from target_agent.contracts import (
    ClaimClass,
    CoverageStatus,
    EvidenceContext,
    EvidenceItem,
    GeneticEvidencePayload,
    SourceLocator,
    Stance,
    TaskContext,
    TaskSpec,
    ToolCapability,
    ToolResult,
    ToolStatus,
)

MIN_PP4 = 0.8


def _disease_task(candidate_genes: list[str] | None = None) -> TaskSpec:
    return TaskSpec(
        task_id="task-candidate-policy",
        task_type="disease_to_target",
        question="Prioritize candidate targets for lung adenocarcinoma",
        context=TaskContext(disease="lung adenocarcinoma", tissue="lung"),
        candidate_genes=candidate_genes or [],
    )


def _result(
    tool_name: str,
    *,
    tool_run_id: str,
    inputs: dict | None = None,
    outputs: dict | None = None,
    candidate_genes: list[str] | None = None,
    evidence_ids: list[str] | None = None,
    status: ToolStatus = ToolStatus.SUCCESS,
    coverage: CoverageStatus = CoverageStatus.COVERED,
) -> ToolResult:
    return ToolResult(
        tool_run_id=tool_run_id,
        tool_name=tool_name,
        tool_version="test",
        status=status,
        coverage_status=coverage,
        context_match_score=1.0,
        inputs=inputs or {},
        outputs=outputs or {},
        candidate_genes=candidate_genes or [],
        capability=ToolCapability(validation_scope="candidate policy test"),
        evidence_ids=evidence_ids or [],
    )


def _formal_evidence(
    *,
    evidence_id: str,
    gene: str,
    tool_run_id: str,
    pp4: float = 0.9,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        tool_run_id=tool_run_id,
        gene_symbol=gene,
        claim_class=ClaimClass.INFERRED,
        statement="A shared-signal analysis supports a locus-to-gene hypothesis.",
        source=SourceLocator(
            uri="https://example.org/coloc-fixture",
            source_id="GWAS-1|EQTL-1",
            chunk_id=f"L1-CS1-{gene}",
        ),
        source_span=f"locus=L1|signal=CS1|gene={gene}|PP4={pp4}",
        context=EvidenceContext(
            disease="lung adenocarcinoma",
            tissue="lung",
            genome_build="GRCh38",
            ancestry="EUR",
            study_id="GWAS-1",
            locus_id="L1",
            signal_id="CS1",
        ),
        stance=Stance.SUPPORTS,
        effect_direction="unclear",
        uncertainty="Synthetic test evidence; a shared signal does not establish causality.",
        context_match_score=1.0,
        genetic_evidence=GeneticEvidencePayload(
            evidence_type="colocalization",
            analysis_level="colocalization_supported",
            study_id="GWAS-1",
            molecular_study_id="EQTL-1",
            locus_id="L1",
            signal_id="CS1",
            gene_symbol=gene,
            method="coloc_susie",
            method_version="fixture",
            strength=pp4,
            formal_score_eligible=True,
        ),
    )


def _chain(
    genes: tuple[str, ...] = ("IL6",),
    *,
    pp4: float = 0.9,
    evidence_pp4: float | None = None,
) -> tuple[list[ToolResult], list[EvidenceItem]]:
    audit = _result(
        "genetics_input_audit",
        tool_run_id="tool-audit",
        outputs={
            "covered": True,
            "assets": [
                {
                    "kind": "gwas_summary_statistics",
                    "study_id": "GWAS-1",
                    "genome_build": "GRCh38",
                    "ancestry": "EUR",
                }
            ],
        },
    )
    fine = _result(
        "fine_mapping_audit",
        tool_run_id="tool-fine",
        outputs={
            "covered": True,
            "credible_sets": [
                {
                    "study_id": "GWAS-1",
                    "locus_id": "L1",
                    "credible_set_id": "CS1",
                    "formal_score_eligible": True,
                }
            ],
        },
    )
    coloc = _result(
        "eqtl_colocalization_audit",
        tool_run_id="tool-coloc",
        inputs={
            "genetics_input_audit_tool_run_id": audit.tool_run_id,
            "fine_mapping_tool_run_id": fine.tool_run_id,
        },
        outputs={
            "covered": True,
            "colocalizations": [
                {
                    "gene": gene,
                    "study_id": "GWAS-1",
                    "gwas_study_id": "GWAS-1",
                    "locus_id": "L1",
                    "signal_id": "CS1",
                    "pp4": pp4,
                    "formal_score_eligible": True,
                }
                for gene in genes
            ],
        },
    )
    extraction = _result(
        "genetics_candidate_extraction",
        tool_run_id="tool-extraction",
        inputs={
            "genetics_input_audit_tool_run_id": audit.tool_run_id,
            "fine_mapping_tool_run_id": fine.tool_run_id,
            "colocalization_tool_run_id": coloc.tool_run_id,
        },
        outputs={"covered": True, "candidate_genes": list(genes)},
        candidate_genes=list(genes),
        evidence_ids=[f"ev-{gene}" for gene in genes],
    )
    evidence = [
        _formal_evidence(
            evidence_id=f"ev-{gene}",
            gene=gene,
            tool_run_id=extraction.tool_run_id,
            pp4=evidence_pp4 if evidence_pp4 is not None else pp4,
        )
        for gene in genes
    ]
    return [audit, fine, coloc, extraction], evidence


def test_initial_candidate_genes_returns_task_candidates_for_non_gwas():
    task = _disease_task(candidate_genes=["TP53", "IL6"])
    assert initial_candidate_genes(task) == ["TP53", "IL6"]


def test_merge_candidates_for_task_routes_non_gwas_to_default_merge():
    task = _disease_task()
    result = _result(
        "genetics_candidate_extraction",
        tool_run_id="tool-extraction",
        candidate_genes=["IL6"],
    )

    def default_merge(current, incoming, limit):
        return (current + list(incoming.candidate_genes))[:limit]

    merged = merge_candidates_for_task(
        task, ["TP53"], result, 10, default_merge, [], [], MIN_PP4,
    )
    assert merged == ["TP53", "IL6"]


def test_formal_gwas_accepts_formally_mapped_genes():
    results, evidence = _chain(genes=("IL6", "TP53"))
    assert formal_gwas_candidates(results, evidence, MIN_PP4, 20) == ["IL6", "TP53"]


def test_formal_gwas_accepts_partial_status_when_covered_true():
    results, evidence = _chain()
    results[-1] = results[-1].model_copy(
        update={"status": ToolStatus.PARTIAL, "coverage_status": CoverageStatus.PARTIAL}
    )
    assert formal_gwas_candidates(results, evidence, MIN_PP4, 20) == ["IL6"]


def test_formal_gwas_respects_candidate_limit_and_ordering():
    results, evidence = _chain(genes=("IL6", "TP53"))
    assert formal_gwas_candidates(results, evidence, MIN_PP4, 1) == ["IL6"]


def test_formal_gwas_rejects_strength_below_threshold():
    results, evidence = _chain(pp4=0.7)
    assert formal_gwas_candidates(results, evidence, MIN_PP4, 20) == []


def test_formal_gwas_rejects_coloc_row_not_matching_evidence_strength():
    results, evidence = _chain(pp4=0.9, evidence_pp4=0.85)
    assert formal_gwas_candidates(results, evidence, MIN_PP4, 20) == []


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda results, evidence: (results[:-1], evidence), id="missing_extraction"),
        pytest.param(
            lambda results, evidence: (
                [results[-1].model_copy(update={"status": ToolStatus.FAILED, "error": "boom"})],
                evidence,
            ),
            id="extraction_failed",
        ),
        pytest.param(
            lambda results, evidence: (
                [results[-1].model_copy(update={"status": ToolStatus.PARTIAL, "coverage_status": CoverageStatus.NOT_COVERED})],
                evidence,
            ),
            id="extraction_not_covered",
        ),
        pytest.param(
            lambda results, evidence: (
                [
                    results[-1].model_copy(
                        update={
                            "outputs": {**results[-1].outputs, "covered": False},
                            "status": ToolStatus.PARTIAL,
                            "coverage_status": CoverageStatus.PARTIAL,
                        }
                    )
                ],
                evidence,
            ),
            id="extraction_covered_false",
        ),
        pytest.param(
            lambda results, evidence: (
                [
                    results[-1].model_copy(
                        update={"inputs": {key: value for key, value in results[-1].inputs.items() if key != "genetics_input_audit_tool_run_id"}}
                    )
                ],
                evidence,
            ),
            id="missing_lineage_input",
        ),
        pytest.param(
            lambda results, evidence: (
                [
                    results[0].model_copy(update={"tool_name": "other_tool"}),
                    *results[1:],
                ],
                evidence,
            ),
            id="referenced_tool_mismatch",
        ),
        pytest.param(
            lambda results, evidence: (
                [
                    results[0].model_copy(update={"status": ToolStatus.FAILED, "error": "boom"}),
                    *results[1:],
                ],
                evidence,
            ),
            id="referenced_audit_failed",
        ),
        pytest.param(
            lambda results, evidence: (
                [
                    results[0].model_copy(update={"outputs": {**results[0].outputs, "assets": [{"kind": "other"}]}}),
                    *results[1:],
                ],
                evidence,
            ),
            id="no_gwas_asset",
        ),
        pytest.param(
            lambda results, evidence: (
                [
                    *results[:1],
                    results[1].model_copy(update={"outputs": {**results[1].outputs, "credible_sets": []}}),
                    *results[2:],
                ],
                evidence,
            ),
            id="empty_credible_sets",
        ),
        pytest.param(
            lambda results, evidence: (
                [
                    *results[:2],
                    results[2].model_copy(update={"outputs": {**results[2].outputs, "colocalizations": []}}),
                    *results[3:],
                ],
                evidence,
            ),
            id="empty_colocalizations",
        ),
        pytest.param(
            lambda results, evidence: (
                [
                    *results[:3],
                    results[3].model_copy(update={"outputs": {**results[3].outputs, "candidate_genes": []}, "candidate_genes": []}),
                ],
                evidence,
            ),
            id="no_output_candidates",
        ),
        pytest.param(
            lambda results, evidence: (results, []), id="evidence_missing_from_store",
        ),
        pytest.param(
            lambda results, evidence: (
                results,
                [evidence[0].model_copy(update={"tool_run_id": "tool-other"})],
            ),
            id="evidence_tool_run_mismatch",
        ),
        pytest.param(
            lambda results, evidence: (
                results,
                [evidence[0].model_copy(update={"stance": Stance.REFUTES})],
            ),
            id="evidence_refuting_stance",
        ),
        pytest.param(
            lambda results, evidence: (
                results,
                [evidence[0].model_copy(update={"context_match_score": 0.3})],
            ),
            id="evidence_low_context_match",
        ),
    ],
)
def test_formal_gwas_rejects_incomplete_or_tampered_lineage(mutate):
    results, evidence = _chain()
    mutated_results, mutated_evidence = mutate(results, evidence)
    assert formal_gwas_candidates(mutated_results, mutated_evidence, MIN_PP4, 20) == []