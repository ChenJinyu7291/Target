"""Transparent six-dimensional ranking; total score is never a success probability."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ablations import AblationConfig
from .contracts import (
    ClaimClass, EvidenceItem, ReviewerFinding, ScoreBreakdown, Stance,
    TargetGeneticEvidenceSummary, TaskContext, TerminalStatus, ToolResult,
)
from .context_score import evidence_context_score


WEIGHTS = {
    "human_genetics": 25.0,
    "disease_omics": 20.0,
    "perturbation": 20.0,
    "mechanism": 15.0,
    "druggability": 10.0,
    "safety_translation": 10.0,
}


@dataclass
class RankedTarget:
    gene: str
    scores: ScoreBreakdown
    evidence_ids: list[str]
    supporting_ids: list[str]
    opposing_ids: list[str]
    safety_blockers: list[str]
    evidence_gaps: list[str]
    matched_drugs: list[dict[str, Any]]
    genetic_evidence_summary: list[TargetGeneticEvidenceSummary]
    decision: str
    context_score_origin: str = "recomputed"
    context_score_notes: list[str] = field(default_factory=list)


def _clamp(value: float, high: float) -> float:
    return round(max(0.0, min(high, value)), 4)


def _phase_value(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").lower()
    for digit in ("4", "3", "2", "1"):
        if digit in text:
            return float(digit)
    for token, score in (("iv", 4), ("iii", 3), ("ii", 2), ("i", 1), ("approved", 4)):
        if token in text:
            return float(score)
    return 0.0



def _directional_supported_literature(item: EvidenceItem) -> bool:
    """FACT/stronger literature counts only when it is directional, non-co-mention,
    and its source version/checksum is verifiable (P1-2/P1-4)."""
    if item.claim_class not in {ClaimClass.FACT, ClaimClass.OBSERVED}:
        return False
    if "europepmc" not in item.source.uri.lower():
        return False
    if "provenance_missing" in item.quality_flags:
        return False
    if "co-mentions" in item.statement.lower():
        return False
    if item.effect_direction not in {"increase", "decrease"} or item.stance != Stance.SUPPORTS:
        return False
    if not item.source.version:
        return False
    if item.source.version.startswith("shared-corpus"):
        return bool(item.source.sha256 or (item.source.version_meta or {}).get("corpus_snapshot_sha256"))
    return True


def rank_targets(
    candidates: list[str], evidence: list[EvidenceItem], results: list[ToolResult],
    findings: list[ReviewerFinding] | None = None,
    *,
    minimum_coloc_pp4: float = 0.8,
    task_context: TaskContext | None = None,
    terminal_status: TerminalStatus | None = None,
    ablations: AblationConfig | frozenset[str] | None = None,
) -> list[RankedTarget]:
    """Transparent six-dimensional ranking.

    `ablations` (see ablations.py) removes individual mechanisms for evaluation;
    the default (empty AblationConfig) travels the exact production path.
    """
    ablations = AblationConfig.coerce(ablations)
    results_by_id = {result.tool_run_id: result for result in results}

    def tool_lineage(tool_run_id: str) -> set[str]:
        lineage: set[str] = set()
        pending = [tool_run_id]
        while pending:
            current = pending.pop()
            if current in lineage:
                continue
            lineage.add(current)
            result = results_by_id.get(current)
            if result is None:
                continue
            pending.extend(
                value
                for key, value in result.inputs.items()
                if key.endswith("_tool_run_id") and isinstance(value, str) and value
            )
        return lineage

    by_gene: dict[str, list[EvidenceItem]] = {gene: [] for gene in candidates}
    for item in evidence:
        if item.gene_symbol in by_gene:
            by_gene[item.gene_symbol].append(item)
    ot_by_gene: dict[str, dict[str, Any]] = {}
    for result in results:
        if result.tool_name == "open_targets":
            ot_by_gene.update({row["gene"]: row for row in result.outputs.get("associations", [])})

    ranked = []
    for gene in candidates:
        items = by_gene[gene]
        scored = [(evidence_context_score(item, task_context), item) for item in items]
        context_by_id = {item.evidence_id: score for score, item in scored}
        formal = items if "no_context_gate" in ablations else [item for score, item in scored if score.score >= 0.5]
        genetics = 0.0
        omics = 0.0
        perturb = 0.0
        mechanism = 0.0
        druggability = 0.0
        safety = 0.0
        gaps = []
        blockers = []
        matched_drugs = ot_by_gene.get(gene, {}).get("known_drugs", [])
        tractability = ot_by_gene.get(gene, {}).get("tractability", [])
        has_strict_genetics = False
        genetic_summaries: list[TargetGeneticEvidenceSummary] = []
        independent_genetic_context: dict[tuple[str, str], float] = {}

        for item in formal:
            context = context_by_id[item.evidence_id].score
            genetic = item.genetic_evidence
            if (
                "no_human_genetics" not in ablations
                and genetic is not None
                and genetic.formal_score_eligible
                and genetic.analysis_level == "colocalization_supported"
                and genetic.evidence_type in {"colocalization", "locus_to_gene"}
                and genetic.evidence_type != "open_targets_genetic_association"
                and genetic.strength >= minimum_coloc_pp4
                and item.stance == Stance.SUPPORTS
                and item.gene_symbol == genetic.gene_symbol
                and item.context.study_id == genetic.study_id
                and item.context.locus_id == genetic.locus_id
                and item.context.signal_id == genetic.signal_id
                and bool(item.context.genome_build and item.context.ancestry)
            ):
                has_strict_genetics = True
                independent_key = (genetic.study_id, genetic.locus_id or "")
                independent_genetic_context[independent_key] = max(
                    independent_genetic_context.get(independent_key, 0.0), context,
                )
                genetic_summaries.append(TargetGeneticEvidenceSummary(
                    evidence_id=item.evidence_id, study_id=genetic.study_id,
                    molecular_study_id=genetic.molecular_study_id or "",
                    locus_id=genetic.locus_id or "", signal_id=genetic.signal_id or "",
                    method=genetic.method or "", method_version=genetic.method_version or "",
                    strength=genetic.strength, genome_build=item.context.genome_build,
                    ancestry=item.context.ancestry or "", tissue=item.context.tissue,
                ))
            if "legacy_disease_strength_0_60" in item.effect:
                normalized = float(item.effect["legacy_disease_strength_0_60"]) / 60.0
                omics = max(omics, WEIGHTS["disease_omics"] * normalized * context)
            if "omics_strength" in item.effect:
                omics = max(omics, WEIGHTS["disease_omics"] * float(item.effect["omics_strength"]) * context)
            if "no_perturbation_layer" not in ablations:
                if item.claim_class == ClaimClass.OBSERVED and "disease_alignment" in item.effect:
                    alignment = abs(float(item.effect["disease_alignment"]))
                    perturb = max(perturb, (8.0 + min(12.0, alignment / 0.1 * 12.0)) * context)
                if item.claim_class == ClaimClass.PREDICTED:
                    perturb = max(perturb, min(WEIGHTS["perturbation"] / 2.0, 10.0 * context))

        if independent_genetic_context:
            independent_loci = len(independent_genetic_context)
            evidence_tier = 0.5 if independent_loci == 1 else 0.75 if independent_loci == 2 else 1.0
            context_multiplier = sum(independent_genetic_context.values()) / independent_loci
            genetics = WEIGHTS["human_genetics"] * evidence_tier * context_multiplier

        has_omics = any("legacy_disease_strength_0_60" in item.effect or "omics_strength" in item.effect for item in formal)
        has_observed_perturb = any(item.claim_class == ClaimClass.OBSERVED and "disease_alignment" in item.effect for item in formal)
        has_literature = any(_directional_supported_literature(item) for item in formal)
        if "no_mechanism_bonus" not in ablations:
            if has_omics and has_observed_perturb:
                mechanism += 8.0
            if has_literature:
                mechanism += 4.0
            if genetics > 0 and has_omics:
                mechanism += 3.0
            mechanism = min(WEIGHTS["mechanism"], mechanism)
        if matched_drugs:
            max_phase = max(_phase_value(drug.get("phase")) for drug in matched_drugs)
            druggability = min(10.0, 4.0 + max_phase * 1.5)
        elif any(item.get("value") for item in tractability):
            druggability = 4.0
        if not any("safety" in item.effect for item in formal):
            gaps.append("No matched, source-grounded safety evidence was retrieved.")
        if genetics == 0:
            gaps.append("No qualifying human-genetic evidence in the current store.")
        if not has_observed_perturb:
            gaps.append("No matched-context measured perturbation evidence for this target.")
        if not has_literature:
            gaps.append("No span-validated literature claim for this target.")
        if not matched_drugs:
            gaps.append("No known drug was returned in the current Open Targets result.")
        opposing = [item.evidence_id for item in items if item.stance in {Stance.REFUTES, Stance.MIXED}]
        supporting = [item.evidence_id for item in items if item.stance == Stance.SUPPORTS]
        if opposing:
            blockers.append("Opposing or mixed evidence is retained and requires context-specific review.")
        safety_events = [str(item.effect["safety"].get("event") or item.effect["safety"].get("eventId"))
                         for item in items if "safety" in item.effect]
        blockers.extend(f"Open Targets safety liability: {event}" for event in safety_events)
        target_related_ids = {
            related_id
            for item in items
            for related_id in ({item.evidence_id} | tool_lineage(item.tool_run_id))
        }
        unresolved_target_findings = [
            finding for finding in (findings or [])
            if not finding.resolved
            and finding.severity in {"blocking", "major"}
            and target_related_ids.intersection(finding.related_ids)
        ]
        if unresolved_target_findings:
            categories = sorted({finding.category for finding in unresolved_target_findings})
            blockers.append(
                "Unresolved Reviewer finding(s) affect this target: " + ", ".join(categories) + "."
            )
            gaps.append("Resolve the linked blocking/major Reviewer findings before an unconditional GO.")
        global_block = terminal_status is not None and terminal_status != TerminalStatus.COMPLETED
        if global_block:
            blockers.append(
                "Terminal status is not COMPLETED; unconditional GO is globally forbidden while unresolved gaps remain."
            )
            gaps.append("Terminal status carries unresolved gaps; resolve findings before an unconditional GO.")
        strong_opposing = any(
            item.stance == Stance.REFUTES
            and item.claim_class in {ClaimClass.FACT, ClaimClass.OBSERVED}
            and item.effect_direction != "unclear"
            for item in items
        )
        if strong_opposing:
            blockers.append(
                "Strong opposing evidence (FACT/OBSERVED, directional) is retained; "
                "NO_GO unless context-specific review resolves it."
            )
            gaps.append("Resolve strong opposing evidence before any conditional investment.")
        if safety_events:
            blockers.append(
                "Known safety liabilities are retained; NO_GO unless a context-specific "
                "risk assessment overrides them."
            )

        scores = ScoreBreakdown(
            human_genetics=_clamp(genetics, 25), disease_omics=_clamp(omics, 20),
            perturbation=_clamp(perturb, 20), mechanism=_clamp(mechanism, 15),
            druggability=_clamp(druggability, 10), safety_translation=_clamp(safety, 10),
            total=_clamp(genetics + omics + perturb + mechanism + druggability + safety, 100),
        )
        independent = sum([has_strict_genetics, has_omics, has_observed_perturb, has_literature, bool(matched_drugs)])
        gate = has_strict_genetics or (has_observed_perturb and "no_perturbation_layer" not in ablations)
        if safety_events or strong_opposing:
            decision = "NO_GO"
        elif blockers:
            decision = "CONDITIONAL_GO" if independent >= 2 else "INSUFFICIENT_EVIDENCE"
        elif independent >= 2 and gate:
            decision = "GO"
        elif independent >= 2:
            decision = "CONDITIONAL_GO"
        else:
            decision = "INSUFFICIENT_EVIDENCE"
        ranked.append(RankedTarget(
            gene=gene, scores=scores, evidence_ids=[item.evidence_id for item in items],
            supporting_ids=supporting, opposing_ids=opposing, safety_blockers=blockers,
            evidence_gaps=gaps, matched_drugs=matched_drugs, decision=decision,
            genetic_evidence_summary=genetic_summaries,
            context_score_origin=(
                "recomputed" if task_context is not None else "recomputed_against_evidence_context"
            ),
            context_score_notes=sorted({note for score, _ in scored for note in score.notes}),
        ))
    ranked.sort(key=lambda row: (-row.scores.total, row.gene))
    return ranked
