"""Deterministic best-practice reviewer; LLM output cannot waive these gates."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .contracts import (
    ClaimClass, CoverageStatus, EvidenceItem, ReviewerFinding, TaskSpec,
    ToolResult, ToolStatus,
)
from .context_score import evidence_context_score, recompute_context_score
from .llm import LLMUnavailable, StepClient


LLM_REVIEW_PROMPT_VERSION = 1
# The reviewer payload is much larger than planner prompts; Step routinely
# needs 60-120s on it. Give it a dedicated budget and fail over to the
# deterministic gates after one retry instead of burning 3x90s.
REVIEWER_READ_TIMEOUT_SECONDS = 240
REVIEWER_MAX_RETRIES = 1


class Reviewer:
    def __init__(self, client: StepClient | None = None, settings=None,
                 cache_dir: Path | None = None):
        self.client = client
        self.last_backend = "deterministic"
        self.cache_dir = cache_dir
        self._lora = None
        self._lora_failed = False
        if (
            settings is not None
            and getattr(settings, "reviewer_lora_base", None)
            and getattr(settings, "reviewer_lora_adapter", None)
        ):
            from .reviewer_lora import LoRAReviewerBackend
            self._lora = LoRAReviewerBackend(settings.reviewer_lora_base, settings.reviewer_lora_adapter)

    def review(self, task: TaskSpec, results: list[ToolResult], evidence: list[EvidenceItem]) -> list[ReviewerFinding]:
        findings: list[ReviewerFinding] = []
        tool_ids = {result.tool_run_id for result in results}
        evidence_ids = {item.evidence_id for item in evidence}
        for item in evidence:
            if item.tool_run_id not in tool_ids or not item.source.uri or not item.source_span:
                findings.append(ReviewerFinding(
                    severity="blocking", category="missing_provenance",
                    message=f"Evidence {item.evidence_id} lacks a valid tool/source/span chain.",
                    related_ids=[item.evidence_id, item.tool_run_id], required_action="Repair or remove the evidence before reporting.",
                ))
            recomputed_context = evidence_context_score(item, task.context)
            if recomputed_context.score < 0.5:
                findings.append(ReviewerFinding(
                    severity="major", category="context_mismatch",
                    message=(
                        f"Evidence {item.evidence_id} has recomputed context match "
                        f"{recomputed_context.score:.2f} (self-reported estimate "
                        f"{item.context_match_score:.2f}) and cannot enter formal ranking."
                    ),
                    related_ids=[item.evidence_id], required_action="Exclude from formal score and retain only as exploratory context.",
                ))
            causal_words = (
                "causes", "causal evidence", "drives disease", "proves", "is causal",
                "causal target", "is causative", "confers risk", "causal role",
                "cause of", "causal effect", "致病", "因果靶点", "证明因果",
                "导致", "驱动疾病", "引起", "因果证据",
            )
            scanned_text = " | ".join(filter(None, (
                item.statement, item.uncertainty,
                json.dumps(item.effect, ensure_ascii=False) if item.effect else "",
            ))).lower()
            if any(word in scanned_text for word in causal_words):
                findings.append(ReviewerFinding(
                    severity="major", category="causal_overreach",
                    message=f"Evidence {item.evidence_id} uses causal language beyond its evidence class.",
                    related_ids=[item.evidence_id], required_action="Downgrade language or add a valid causal design.",
                ))
            genetic = item.genetic_evidence
            if genetic is not None:
                if genetic.formal_score_eligible and (
                    genetic.analysis_level in {"association_only", "database_aggregate"}
                    or genetic.evidence_type == "open_targets_genetic_association"
                ):
                    findings.append(ReviewerFinding(
                        severity="major", category="gene_mapping_overreach",
                        message=f"Evidence {item.evidence_id} marks aggregate or association-only genetics as formal score eligible.",
                        related_ids=[item.evidence_id],
                        required_action="Remove formal-score eligibility until audited fine-mapping or colocalization supports the gene mapping.",
                    ))
                if genetic.formal_score_eligible and (
                    not item.context.genome_build or not genetic.locus_id
                ):
                    findings.append(ReviewerFinding(
                        severity="blocking", category="missing_provenance",
                        message=f"Formal genetic evidence {item.evidence_id} lacks genome build or locus provenance.",
                        related_ids=[item.evidence_id],
                        required_action="Provide a normalized genome build and stable locus identifier before scoring.",
                    ))
                if genetic.formal_score_eligible and item.effect_direction != "unclear" and (
                    "aligned_eqtl_beta" not in item.effect
                    or "harmonization=" not in item.source_span
                ):
                    findings.append(ReviewerFinding(
                        severity="major", category="allele_harmonization",
                        message=f"Genetic evidence {item.evidence_id} reports direction without an auditable allele-harmonization result.",
                        related_ids=[item.evidence_id],
                        required_action="Set direction to unclear or attach the aligned effect and harmonization trace.",
                    ))
        for result in results:
            missing = set(result.evidence_ids) - evidence_ids
            if missing:
                findings.append(ReviewerFinding(
                    severity="blocking", category="missing_provenance",
                    message=f"Tool {result.tool_run_id} references missing EvidenceItems.",
                    related_ids=[result.tool_run_id, *sorted(missing)], required_action="Restore the missing EvidenceItems.",
                ))
            if result.status == ToolStatus.FAILED:
                findings.append(ReviewerFinding(
                    severity="major", category="tool_failure",
                    message=f"Tool {result.tool_name} failed: {result.error}", related_ids=[result.tool_run_id],
                    required_action="Retry within budget or mark the corresponding evidence dimension as missing.",
                ))
            if result.coverage_status == CoverageStatus.NOT_COVERED:
                severity = "blocking" if task.task_type == "trait_mechanism" and result.tool_name == "mch_causal_gold" else "major"
                findings.append(ReviewerFinding(
                    severity=severity, category="coverage_gap",
                    message=f"Tool {result.tool_name} does not cover the requested context.", related_ids=[result.tool_run_id],
                    required_action="Request matching input/data; do not describe this step as complete.",
                ))
            elif result.coverage_status == CoverageStatus.PARTIAL:
                severity = "minor" if result.outputs.get("formal_score_eligible") is False else "major"
                findings.append(ReviewerFinding(
                    severity=severity, category="coverage_gap",
                    message=f"Tool {result.tool_name} has partial coverage.", related_ids=[result.tool_run_id],
                    required_action="Expose uncovered genes/context as an evidence gap.",
                ))
            tool_context_meta = result.outputs.get("context_match")
            if isinstance(tool_context_meta, dict) and tool_context_meta:
                recomputed_tool_context = recompute_context_score(task.context, None, tool_context_meta)
                effective_tool_context = recomputed_tool_context.score
            else:
                # No match evidence was submitted: keep the legacy coverage gate
                # but never let a missing-metadata result claim a higher score.
                effective_tool_context = result.context_match_score
            if effective_tool_context < 0.5:
                severity = "minor" if result.outputs.get("formal_score_eligible") is False else "major"
                findings.append(ReviewerFinding(
                    severity=severity, category="context_mismatch",
                    message=(
                        f"Tool {result.tool_name} context match is {effective_tool_context:.2f} "
                        f"(self-reported estimate {result.context_match_score:.2f})."
                    ),
                    related_ids=[result.tool_run_id], required_action="Exclude low-match outputs from formal ranking.",
                ))
            if result.tool_name == "disease_resolver" and result.outputs.get("identifier_source") == "unresolved":
                findings.append(ReviewerFinding(
                    severity="blocking", category="tool_failure",
                    message=(
                        "Disease identifier resolution failed; the normalized disease label "
                        "is an unverified fallback and must not be treated as resolved context."
                    ),
                    related_ids=[result.tool_run_id],
                    required_action="Resolve the disease identifier or request a canonical disease ID before downstream evidence is trusted.",
                ))
            for numeric_error in result.outputs.get("numeric_validation_errors", []):
                findings.append(ReviewerFinding(
                    severity="blocking", category="numeric_error",
                    message=f"Tool {result.tool_name} emitted an invalid numeric value: {numeric_error}",
                    related_ids=[result.tool_run_id],
                    required_action="Remove or recompute the invalid numeric output before reporting.",
                ))
            if result.tool_name == "genetics_input_audit":
                assets = result.outputs.get("assets", [])
                builds = {row.get("genome_build") for row in assets if row.get("genome_build")}
                ancestries = {str(row.get("ancestry")).casefold() for row in assets if row.get("ancestry")}
                if len(builds) > 1:
                    findings.append(ReviewerFinding(
                        severity="blocking", category="genome_build_mismatch",
                        message="Genetics assets use multiple genome builds within one analysis.",
                        related_ids=[result.tool_run_id],
                        required_action="Normalize all assets to one declared build with an auditable conversion trace.",
                    ))
                if len(ancestries) > 1:
                    findings.append(ReviewerFinding(
                        severity="major", category="ancestry_mismatch",
                        message="Genetics assets declare different ancestries.",
                        related_ids=[result.tool_run_id],
                        required_action="Justify cross-ancestry integration or use ancestry-matched GWAS, eQTL and LD inputs.",
                    ))
                for failed in result.outputs.get("failed_assets", []):
                    error = str(failed.get("error") or "")
                    if any(token in error for token in ("allele", "palindromic", "orientation")):
                        findings.append(ReviewerFinding(
                            severity="blocking", category="allele_harmonization",
                            message=f"Genetics asset {failed.get('asset_id', 'unknown')} failed allele QC: {error}.",
                            related_ids=[result.tool_run_id],
                            required_action="Repair allele encoding or exclude the asset before genetic scoring.",
                        ))
            if result.tool_name == "fine_mapping_audit":
                for credible_set in result.outputs.get("credible_sets", []):
                    reasons = credible_set.get("rejection_reasons", [])
                    if reasons:
                        findings.append(ReviewerFinding(
                            severity="blocking", category="fine_mapping_invalid",
                            message=(
                                f"Fine-mapping set {credible_set.get('credible_set_id', 'unknown')} "
                                f"is not score eligible: {', '.join(reasons)}."
                            ),
                            related_ids=[result.tool_run_id],
                            required_action=(
                                "Correct LD/build/ancestry/signal-posterior provenance or retain the set as non-formal evidence."
                            ),
                        ))
            if result.tool_name == "eqtl_colocalization_audit":
                for coloc in result.outputs.get("colocalizations", []):
                    reasons = set(coloc.get("rejection_reasons", []))
                    if not reasons:
                        continue
                    if reasons & {
                        "allele_set_mismatch", "palindromic_ambiguous_without_frequency",
                        "palindromic_ambiguous_without_informative_frequency",
                        "orientation_unresolved", "regional_allele_harmonization_failed",
                    }:
                        category, severity = "allele_harmonization", "blocking"
                    elif "eqtl_context_mismatch" in reasons:
                        category, severity = "context_mismatch", "major"
                    elif "gwas_eqtl_ancestry_mismatch" in reasons:
                        category, severity = "ancestry_mismatch", "blocking"
                    else:
                        category, severity = "colocalization_invalid", "major"
                    findings.append(ReviewerFinding(
                        severity=severity, category=category,
                        message=(
                            f"Colocalization result for {coloc.get('gene', 'unknown')} at "
                            f"{coloc.get('locus_id', 'unknown')} is not score eligible: {', '.join(sorted(reasons))}."
                        ),
                        related_ids=[result.tool_run_id],
                        required_action="Resolve the stated QC/context issue or retain the result as non-formal evidence.",
                    ))
            if result.tool_name == "geo_metadata_audit":
                selected = result.outputs.get("selected_datasets", [])
                for rejected in result.outputs.get("rejected_datasets", []):
                    candidate = rejected.get("candidate", {})
                    reasons = candidate.get("exclusion_reasons", [])
                    findings.append(ReviewerFinding(
                        severity="minor" if selected else "major",
                        category="dataset_ineligibility",
                        message=(
                            f"GEO dataset {candidate.get('accession', 'unknown')} was rejected: "
                            + ", ".join(reasons or ["unspecified eligibility failure"])
                        ),
                        related_ids=[result.tool_run_id],
                        required_action="Select the next eligible dataset or retain the omics dimension as a documented gap.",
                    ))

        directions: dict[str, set[str]] = defaultdict(set)
        ids_by_gene: dict[str, list[str]] = defaultdict(list)
        for item in evidence:
            if item.gene_symbol and item.effect_direction in {"increase", "decrease"} and item.stance.value in {"supports", "refutes", "mixed"}:
                directions[item.gene_symbol].add(item.effect_direction)
                ids_by_gene[item.gene_symbol].append(item.evidence_id)
        for gene, values in directions.items():
            if len(values) > 1:
                findings.append(ReviewerFinding(
                    severity="major", category="conflicting_evidence",
                    message=f"{gene} has opposing effect directions across contexts.",
                    related_ids=ids_by_gene[gene], required_action="Keep both directions and resolve by tissue, cell, assay and perturbation context.",
                ))
        seen_genetics: dict[tuple[str, str, str, str], str] = {}
        for item in evidence:
            genetic = item.genetic_evidence
            if genetic is None or not genetic.formal_score_eligible:
                continue
            key = (
                item.gene_symbol or genetic.gene_symbol or "",
                genetic.study_id, genetic.locus_id or "", genetic.signal_id or "",
            )
            if key in seen_genetics:
                findings.append(ReviewerFinding(
                    severity="major", category="duplicate_genetic_study",
                    message=f"Formal genetic evidence duplicates study/locus/type for {key[0] or 'an unresolved gene'}.",
                    related_ids=[seen_genetics[key], item.evidence_id],
                    required_action="Deduplicate the study/locus signal before scoring.",
                ))
            else:
                seen_genetics[key] = item.evidence_id
        findings.extend(self._lora_findings(task, results, evidence))
        findings.extend(self._llm_findings(task, results, evidence))
        return self._deduplicate(findings)

    def _lora_findings(
        self, task: TaskSpec, results: list[ToolResult], evidence: list[EvidenceItem]
    ) -> list[ReviewerFinding]:
        """Local LoRA confirmation layer; preferred over the hosted Step path when configured."""
        if self._lora is None:
            return []
        try:
            findings = self._lora.findings(task, results, evidence)
            self.last_backend = self._lora.name
            return findings
        except Exception:  # adapter failure must never block deterministic review
            self._lora_failed = True
            self.last_backend = "deterministic:lora_unavailable"
            return []

    def _llm_findings(
        self, task: TaskSpec, results: list[ToolResult], evidence: list[EvidenceItem]
    ) -> list[ReviewerFinding]:
        if not self.client or (self._lora is not None and not self._lora_failed):
            if self._lora is None:
                self.last_backend = "deterministic"
            return []
        allowed_ids = {result.tool_run_id for result in results} | {item.evidence_id for item in evidence}
        allowed_ids = {result.tool_run_id for result in results} | {item.evidence_id for item in evidence}
        tool_rows = [
            {
                "tool_run_id": result.tool_run_id, "tool_name": result.tool_name,
                "status": result.status.value, "coverage_status": result.coverage_status.value,
                "context_match_score": result.context_match_score,
                "data_version": result.data_version, "code_version": result.code_version,
                "warnings": result.warnings, "limitations": result.limitations,
                "outputs": {
                    key: value for key, value in result.outputs.items()
                    if key in {"selection_trace", "formal_score_eligible", "analysis_stage", "numeric_validation_errors"}
                },
            }
            for result in results
        ]
        evidence_rows = [
            {
                "evidence_id": item.evidence_id, "gene": item.gene_symbol,
                "claim_class": item.claim_class.value, "statement": item.statement,
                "context": item.context.model_dump(mode="json"),
                "context_match_score": item.context_match_score,
                "source_span": (item.source_span or "")[:400],
                "quality_flags": list(item.quality_flags or []),
                "effect": {
                    key: str(value)[:120] for key, value in (item.effect or {}).items()
                },
                "genetic": (
                    {
                        "formal_score_eligible": item.genetic_evidence.formal_score_eligible,
                        "evidence_type": item.genetic_evidence.evidence_type,
                        "analysis_level": item.genetic_evidence.analysis_level,
                        "strength": item.genetic_evidence.strength,
                    }
                    if item.genetic_evidence is not None else None
                ),
            }
            for item in evidence[:100]
        ]
        task_payload = task.model_dump(mode="json")
        # Per-run trace handles must not change the review contract: normalize
        # them to stable positional tokens so an identical scientific payload
        # replays from cache across runs. The LLM always sees the normalized
        # payload; replayed findings are mapped back to current ids and
        # re-validated against the current provenance set.
        normalized_payload: dict[str, Any] = {
            "task": {key: value for key, value in task_payload.items()
                     if key not in {"task_id", "created_at"}},
            "tool_results": [
                {**row, "tool_run_id": f"tool:{index}"} for index, row in enumerate(tool_rows)
            ],
            "evidence": [
                {**row, "evidence_id": f"ev:{index}"} for index, row in enumerate(evidence_rows)
            ],
        }
        system = (
            "You are a life-science best-practice reviewer. Add findings only; never waive deterministic gates. "
            "Return JSON with a findings array. Each item must contain severity (blocking, major, or minor), "
            "category, message, related_ids, and required_action. Flag metadata ambiguity, causal overreach, "
            "context mismatch, conflicting evidence, and missing validation. Use only supplied IDs."
        )
        cache_key = hashlib.sha256(json.dumps({
            "prompt_version": LLM_REVIEW_PROMPT_VERSION,
            "model": self.client.model if self.client else "none",
            "system": system,
            "payload": normalized_payload,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        cache_path = (self.cache_dir / "reviewer_llm" / f"{cache_key}.json") if self.cache_dir else None
        raw_findings: Any = None
        cached = False
        if cache_path is not None and cache_path.is_file():
            try:
                cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
                candidate = cached_payload.get("findings")
                if isinstance(candidate, list):
                    raw_findings = candidate
                    cached = True
            except (OSError, ValueError):
                cached = False
        if raw_findings is None:
            try:
                raw = self.client.json_completion(
                    system, json.dumps(normalized_payload, ensure_ascii=False),
                    read_timeout_seconds=REVIEWER_READ_TIMEOUT_SECONDS,
                    max_retries=REVIEWER_MAX_RETRIES,
                )
                raw_findings = raw.get("findings") or []
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps({"findings": raw_findings}, ensure_ascii=False), encoding="utf-8")
            except (LLMUnavailable, ValidationError, ValueError, TypeError, json.JSONDecodeError):
                self.last_backend = "deterministic:step_unavailable_or_invalid"
                return []
        id_map = {f"tool:{index}": row["tool_run_id"] for index, row in enumerate(tool_rows)}
        id_map.update({f"ev:{index}": row["evidence_id"] for index, row in enumerate(evidence_rows)})
        # One malformed LLM finding must not discard the valid ones: skip
        # invalid rows instead of aborting the whole review round.
        reviewed: list[ReviewerFinding] = []
        for item in list(raw_findings)[:10]:
            if not isinstance(item, dict):
                continue
            mapped = dict(item)
            if isinstance(mapped.get("related_ids"), list):
                mapped["related_ids"] = [id_map.get(str(value), str(value)) for value in mapped["related_ids"]]
            try:
                finding = ReviewerFinding.model_validate(mapped)
            except (ValidationError, ValueError, TypeError):
                continue
            if set(finding.related_ids).issubset(allowed_ids):
                reviewed.append(finding)
        self.last_backend = f"step:{self.client.model}" if not cached else f"step:{self.client.model}:cached"
        return reviewed

    @staticmethod
    def _deduplicate(findings: list[ReviewerFinding]) -> list[ReviewerFinding]:
        unique = {}
        for finding in findings:
            key = (finding.severity, finding.category, finding.message, tuple(finding.related_ids))
            unique[key] = finding
        return list(unique.values())
