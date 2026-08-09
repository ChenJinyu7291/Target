"""ClinicalTrials.gov API v2 connector with conservative intervention-target mapping.

A registry record is druggability-relevant FACT evidence only when the gene
symbol is explicitly named by the intervention, title or keyword text; mere
study-gene co-retrieval is never emitted as evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from ..contracts import (
    ClaimClass, CoverageStatus, EvidenceContext, EvidenceItem, SourceLocator,
    Stance, ToolCapability, ToolDescriptor, ToolResult, ToolStatus, new_id,
)
from .base import ScientificTool, ToolContext, ToolExecution

STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"
STOPPED_STATUSES = {"TERMINATED", "WITHDRAWN", "SUSPENDED"}


def _phase_label(phases: list[str] | None) -> str:
    if not phases:
        return "phase not specified"
    order = {"EARLY_PHASE1": 0, "PHASE1": 1, "PHASE2": 2, "PHASE3": 3, "PHASE4": 4}
    best = max((order.get(p, -1) for p in phases), default=-1)
    return {0: "Early Phase 1", 1: "Phase 1", 2: "Phase 2", 3: "Phase 3", 4: "Phase 4"}.get(best, "phase not specified")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _read_clinical_trials_cache(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("schema") == "clinical_trials_snapshot_v1" and isinstance(raw.get("payload"), dict):
        return raw["payload"], {
            "retrieved_at": raw.get("retrieved_at"), "response_digest": raw.get("response_digest"),
        }
    return raw, None


class ClinicalTrialsGovTool(ScientificTool):
    name = "clinical_trials_gov"
    version = "1.0.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="drug",
        description="Retrieve ClinicalTrials.gov v2 records; emit claims only when the intervention text explicitly names the gene.",
        input_types=["TaskSpec", "candidate_genes"], output_types=["EvidenceItem[]"],
        execution_policy="read_only_connector",
    )

    def __init__(self, session: requests.Session | None = None, max_genes: int = 10, page_size: int = 10):
        self.session = session or requests.Session()
        self.max_genes = max_genes
        self.page_size = page_size

    def _fetch(self, disease: str, gene: str, cache_path: Path, cache_only: bool) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
        if cache_only:
            if not cache_path.exists():
                raise FileNotFoundError("ClinicalTrials.gov cache is missing in cache-only mode")
            payload, meta = _read_clinical_trials_cache(cache_path)
            return payload, True, meta
        try:
            response = self.session.get(
                STUDIES_URL,
                params={"query.cond": disease, "query.term": gene,
                        "pageSize": self.page_size, "format": "json"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "schema": "clinical_trials_snapshot_v1", "retrieved_at": _utc_now(),
                "response_digest": _payload_digest(payload), "payload": payload,
            }
            cache_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            return payload, False, {
                "retrieved_at": snapshot["retrieved_at"], "response_digest": snapshot["response_digest"],
            }
        except (requests.RequestException, ValueError):
            if cache_path.exists():
                payload, meta = _read_clinical_trials_cache(cache_path)
                return payload, True, meta
            raise

    @staticmethod
    def _gene_named(gene: str, *texts: str) -> bool:
        return any(re.search(rf"\b{re.escape(gene)}\b", text or "", re.I) for text in texts)

    def run(self, context: ToolContext) -> ToolExecution:
        started = time.perf_counter()
        run_id = new_id("tool")
        resolver = next((item for item in reversed(context.prior_results) if item.tool_name == "disease_resolver"), None)
        disease = (resolver.outputs.get("normalized_disease") if resolver else None) or context.task.context.disease or ""
        genes = [g for g in context.candidate_genes[: self.max_genes] if g]
        capability = ToolCapability(
            supported_organisms=["Homo sapiens (registry scope)"],
            supported_tissues=["trial-dependent"], supported_cell_types=["trial-dependent"],
            training_scope="not applicable",
            validation_scope="ClinicalTrials.gov API v2 registry records returned live or from cache",
        )
        inputs = {"disease": disease, "genes": genes}
        evidence: list[EvidenceItem] = []
        queried = 0
        studies_seen = 0
        cached_any = False
        try:
            for gene in genes:
                cache_key = hashlib.sha256(f"{disease}|{gene}".encode()).hexdigest()[:16]
                cache_path = context.cache_dir / "clinical_trials" / f"{cache_key}.json"
                payload, cached, snapshot_meta = self._fetch(disease, gene, cache_path, context.settings.cache_only)
                cached_any = cached_any or cached
                queried += 1
                for study in payload.get("studies", []):
                    studies_seen += 1
                    proto = study.get("protocolSection", {})
                    ident = proto.get("identificationModule", {})
                    status_mod = proto.get("statusModule", {})
                    design = proto.get("designModule", {})
                    arms = proto.get("armsInterventionsModule", {})
                    nct = ident.get("nctId", "")
                    title = ident.get("briefTitle", "")
                    overall = status_mod.get("overallStatus", "")
                    interventions = arms.get("interventions", []) or []
                    named = [
                        iv for iv in interventions
                        if self._gene_named(gene, iv.get("name", ""), iv.get("description", ""))
                    ]
                    if not named and not self._gene_named(gene, title):
                        continue  # 共检索命中 ≠ 证据
                    phase = _phase_label(design.get("phases"))
                    stopped = overall in STOPPED_STATUSES
                    iv_names = ", ".join(iv.get("name", "") for iv in named) or title
                    flags = ["registry_record"]
                    if stopped:
                        flags.append("trial_stopped")
                        if status_mod.get("whyStopped"):
                            flags.append(f"stopped_reason:{status_mod['whyStopped'][:80]}")
                    span = f"{nct}|{overall}|{phase}|{iv_names}"
                    evidence.append(EvidenceItem(
                        tool_run_id=run_id, gene_symbol=gene, claim_class=ClaimClass.INFERRED,
                        statement=(f"Clinical trial {nct} ({phase}, {overall}) evaluates "
                                   f"{iv_names} in {disease}; the record explicitly names {gene}."),
                        source=SourceLocator(
                            uri=f"https://clinicaltrials.gov/study/{nct}", source_id=nct,
                            version=status_mod.get("lastUpdateSubmitDate", ""),
                            section="registry_record", chunk_id=f"ctgov-{nct}-{gene}",
                            start_char=0, end_char=len(span),
                            version_meta={
                                "data_version": "ClinicalTrials.gov:live-or-cache",
                                "cached": cached_any,
                                "retrieved_at": (snapshot_meta or {}).get("retrieved_at"),
                                "response_digest": (snapshot_meta or {}).get("response_digest"),
                                "last_update_submit_date": status_mod.get("lastUpdateSubmitDate", ""),
                            },
                        ),
                        source_span=span,
                        context=EvidenceContext(disease=disease, assay="clinical trial registry"),
                        stance=Stance.UNCERTAIN if stopped else Stance.SUPPORTS,
                        effect_direction="unclear", effect={"phase": phase, "status": overall},
                        uncertainty="Registry records show trial existence and phase, not efficacy; intervention-target mapping is name-based.",
                        quality_flags=flags, context_match_score=0.8,
                        context_match={
                            "matched_disease": [disease] if disease else [],
                            "matched_tissue": [], "matched_cell": [],
                            "matched_assay": ["clinical trial registry"],
                            "source_fields": ["registry_record"], "context_score_origin": "tool_estimate",
                            "tool_estimate": 0.8,
                        },
                    ))
        except (requests.RequestException, ValueError, OSError) as exc:
            return ToolExecution(result=ToolResult(
                tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
                status=ToolStatus.FAILED, coverage_status=CoverageStatus.UNKNOWN,
                context_match_score=0.0, inputs=inputs, outputs={},
                capability=capability, code_version="1.0.0",
                error=f"ClinicalTrials.gov retrieval failed: {exc.__class__.__name__}",
                limitations=["No trial claim was emitted because no cached or live registry record was available."],
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            ), evidence=[])
        coverage = CoverageStatus.COVERED if evidence else CoverageStatus.PARTIAL
        result = ToolResult(
            tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
            status=ToolStatus.SUCCESS if evidence else ToolStatus.PARTIAL,
            coverage_status=coverage, context_match_score=0.8 if evidence else 0.4,
            inputs=inputs,
            outputs={"genes_queried": queried, "studies_seen": studies_seen,
                     "gene_named_claims": len(evidence),
                     "retrieval_hits_are_evidence": False,
                     "context_match": {
                         "matched_disease": [disease] if disease else [],
                         "matched_tissue": [], "matched_cell": [],
                         "matched_assay": ["clinical trial registry"],
                         "source_fields": ["registry_record"],
                     },
                     "context_score_origin": "tool_estimate",
                     "snapshot_meta": snapshot_meta or {}},
            capability=capability, data_version="ClinicalTrials.gov:live-or-cache", code_version="1.0.0",
            parameters={"api": "v2", "page_size": self.page_size},
            artifacts=[], evidence_ids=[item.evidence_id for item in evidence],
            warnings=[] if evidence else ["no_gene_named_trial_claims"],
            limitations=[
                "Intervention-target mapping is name-based and conservative; efficacy requires trial results, not registry records.",
                "ClinicalTrials.gov is queried live when no cache exists, so records can drift between runs; retrieval timestamp and response digest are recorded for audit.",
            ],
            cached=cached_any, elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return ToolExecution(result=result, evidence=evidence)
