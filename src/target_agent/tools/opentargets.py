"""Open Targets association, tractability and known-drug connector."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import requests

from ..contracts import (
    ClaimClass, CoverageStatus, EvidenceContext, EvidenceItem, GeneticEvidencePayload,
    SourceLocator, Stance, ToolCapability, ToolDescriptor, ToolResult, ToolStatus, new_id,
)
from .base import ScientificTool, ToolContext, ToolExecution


ENDPOINT = "https://api.platform.opentargets.org/api/v4/graphql"
ASSOCIATION_QUERY = """
query DiseaseEvidence($diseaseId: String!) {
  disease(efoId: $diseaseId) {
    id
    name
    associatedTargets(page: {index: 0, size: 100}) {
      rows {
        score
        datatypeScores { id score }
        target { id approvedSymbol approvedName biotype }
      }
    }
  }
}
"""

SEARCH_QUERY = """
query ResolveDisease($queryString: String!) {
  search(queryString: $queryString, entityNames: ["disease"], page: {index: 0, size: 10}) {
    hits { id name entity }
  }
}
"""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _read_open_targets_cache(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("schema") == "open_targets_snapshot_v1" and isinstance(raw.get("payload"), dict):
        return raw["payload"], {
            "retrieved_at": raw.get("retrieved_at"), "response_digest": raw.get("response_digest"),
        }
    return raw, None


def _snapshot_meta(meta: dict[str, Any] | None, cached: bool) -> dict[str, Any]:
    merged: dict[str, Any] = {"data_version": "OpenTargets:live-or-cache", "cached": cached}
    if meta and meta.get("retrieved_at"):
        merged["retrieved_at"] = meta["retrieved_at"]
    if meta and meta.get("response_digest"):
        merged["response_digest"] = meta["response_digest"]
    return merged

class OpenTargetsTool(ScientificTool):
    name = "open_targets"
    version = "2.2.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="multi_evidence",
        description=(
            "Resolve diseases and retrieve Open Targets aggregate associations, tractability, safety and known drugs; "
            "aggregate scores are not locus-level genetic evidence."
        ),
        input_types=["TaskSpec", "candidate_genes"], output_types=["EvidenceItem[]", "candidate_genes"],
        execution_policy="read_only_connector",
    )

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    @staticmethod
    def _disease_id(context: ToolContext) -> str | None:
        if context.task.context.disease_id:
            return context.task.context.disease_id
        resolver = next((item for item in reversed(context.prior_results) if item.tool_name == "disease_resolver"), None)
        return resolver.outputs.get("disease_id") if resolver else None

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(ENDPOINT, json={"query": query, "variables": variables}, timeout=45)
        if response.status_code >= 400:
            raise ValueError(f"Open Targets HTTP {response.status_code}: {response.text[:400]}")
        payload = response.json()
        if payload.get("errors"):
            raise ValueError(f"Open Targets GraphQL error: {json.dumps(payload['errors'])[:400]}")
        return payload

    def _resolve_disease(self, requested_id: str | None, disease_name: str) -> tuple[str, dict[str, Any]]:
        if requested_id:
            payload = self._post(ASSOCIATION_QUERY, {"diseaseId": requested_id})
            if payload.get("data", {}).get("disease"):
                return requested_id, payload
        search = self._post(SEARCH_QUERY, {"queryString": disease_name})
        hits = search.get("data", {}).get("search", {}).get("hits", [])
        exact = next((hit for hit in hits if hit.get("name", "").casefold() == disease_name.casefold()), None)
        selected = exact or (hits[0] if hits else None)
        if not selected:
            raise ValueError("Open Targets disease name could not be resolved")
        resolved = str(selected["id"])
        payload = self._post(ASSOCIATION_QUERY, {"diseaseId": resolved})
        if not payload.get("data", {}).get("disease"):
            raise ValueError("Open Targets resolved disease returned no payload")
        return resolved, payload

    def _retrieve(
        self, disease_id: str | None, disease_name: str, candidate_genes: list[str],
        cache_path: Path, cache_only: bool = False,
    ) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
        if cache_only:
            if not cache_path.exists():
                raise FileNotFoundError("Open Targets cache is missing in cache-only mode")
            payload, meta = _read_open_targets_cache(cache_path)
            return payload, True, meta
        try:
            resolved_id, payload = self._resolve_disease(disease_id, disease_name)
            disease = payload["data"]["disease"]
            association_rows = (disease.get("associatedTargets") or {}).get("rows", [])
            top_genetic = sorted(
                association_rows,
                key=lambda row: max(
                    (float(entry.get("score") or 0) for entry in row.get("datatypeScores") or [] if entry.get("id") == "genetic_association"),
                    default=0.0,
                ),
                reverse=True,
            )[:10]
            selected_symbols = set(candidate_genes) | {
                row.get("target", {}).get("approvedSymbol") for row in top_genetic
            }
            target_ids = [
                row["target"]["id"]
                for row in association_rows
                if row.get("target", {}).get("approvedSymbol") in selected_symbols
            ]
            clinical_warning = None
            try:
                clinical = self._retrieve_clinical_candidates(target_ids) if target_ids else {}
            except (requests.RequestException, ValueError) as exc:
                clinical = {}
                clinical_warning = f"clinical candidate subquery unavailable: {str(exc)[:300]}"
            payload["resolved_disease_id"] = resolved_id
            payload["target_clinical_candidates"] = clinical
            payload["selected_genetic_symbols"] = sorted(symbol for symbol in selected_symbols if symbol)
            payload["clinical_warning"] = clinical_warning
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "schema": "open_targets_snapshot_v1", "retrieved_at": _utc_now(),
                "response_digest": _payload_digest(payload), "payload": payload,
            }
            cache_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            return payload, False, {
                "retrieved_at": snapshot["retrieved_at"], "response_digest": snapshot["response_digest"],
            }
        except (requests.RequestException, ValueError):
            if cache_path.exists():
                payload, meta = _read_open_targets_cache(cache_path)
                return payload, True, meta
            raise

    def _retrieve_clinical_candidates(self, target_ids: list[str]) -> dict[str, Any]:
        aliases = []
        for index, target_id in enumerate(target_ids):
            aliases.append(
                f't{index}: target(ensemblId: "{target_id}") {{ id approvedSymbol '
                "tractability { label modality value } "
                "safetyLiabilities { event eventId datasource url literature } "
                "drugAndClinicalCandidates { rows { id maxClinicalStage drug { id name } "
                "diseases { diseaseFromSource disease { id name } } } } }"
            )
        query = "query CandidateClinicalEvidence { " + " ".join(aliases) + " }"
        payload = self._post(query, {})
        return payload.get("data", {})

    def run(self, context: ToolContext) -> ToolExecution:
        started = time.perf_counter()
        run_id = new_id("tool")
        disease_id = self._disease_id(context)
        capability = ToolCapability(
            supported_organisms=["Homo sapiens"], supported_tissues=["database-wide"],
            supported_cell_types=["database-wide"], training_scope="not applicable",
            validation_scope="Open Targets Platform disease-target associations and known drugs",
        )
        resolver = next((item for item in reversed(context.prior_results) if item.tool_name == "disease_resolver"), None)
        disease_name = (resolver.outputs.get("normalized_disease") if resolver else None) or context.task.context.disease or ""
        cache_key = hashlib.sha256(json.dumps({
            "tool_version": self.version,
            "disease": disease_id or disease_name,
            "candidate_genes": sorted(context.candidate_genes),
        }, sort_keys=True).encode()).hexdigest()[:20]
        cache_path = context.cache_dir / "open_targets" / f"{cache_key}.json"
        try:
            payload, cached, snapshot_meta = self._retrieve(
                disease_id, disease_name,
                context.candidate_genes, cache_path, context.settings.cache_only,
            )
        except (requests.RequestException, ValueError, OSError) as exc:
            return ToolExecution(result=ToolResult(
                tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
                status=ToolStatus.FAILED, coverage_status=CoverageStatus.UNKNOWN, context_match_score=0.0,
                inputs={"disease_id": disease_id, "disease": disease_name}, outputs={}, capability=capability,
                error=f"Open Targets retrieval failed: {str(exc)[:500]}",
                limitations=["Genetic, druggability and known-drug dimensions remain missing until the API/cache is available."],
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            ), evidence=[])
        snapshot_meta = snapshot_meta or {}
        disease = payload["data"]["disease"]
        resolved_disease_id = payload.get("resolved_disease_id", disease["id"])
        candidate_set = set(context.candidate_genes) | set(payload.get("selected_genetic_symbols", []))
        associations = []
        drugs_by_target: dict[str, list[dict[str, Any]]] = {}
        profiles_by_target: dict[str, dict[str, Any]] = {}
        for target_payload in payload.get("target_clinical_candidates", {}).values():
            if not target_payload:
                continue
            target_id = target_payload.get("id")
            profiles_by_target[target_id] = {
                "tractability": target_payload.get("tractability") or [],
                "safety_liabilities": target_payload.get("safetyLiabilities") or [],
            }
            for row in (target_payload.get("drugAndClinicalCandidates") or {}).get("rows", []):
                disease_items = row.get("diseases") or []
                disease_ids = [
                    (item.get("disease") or {}).get("id")
                    for item in disease_items
                    if (item.get("disease") or {}).get("id")
                ]
                if resolved_disease_id not in disease_ids:
                    continue
                drug = row.get("drug") or {}
                drugs_by_target.setdefault(target_id, []).append({
                    "targetId": target_id, "drugId": drug.get("id"), "prefName": drug.get("name"),
                    "phase": row.get("maxClinicalStage"), "status": "Open Targets clinical candidate",
                    "disease_ids": disease_ids,
                    "chembl_url": f"https://www.ebi.ac.uk/chembl/explore/compound/{drug.get('id')}" if drug.get("id") else None,
                })
        evidence = []
        for row in (disease.get("associatedTargets") or {}).get("rows", []):
            target = row.get("target") or {}
            gene = target.get("approvedSymbol")
            if gene not in candidate_set:
                continue
            datatype = {entry.get("id"): float(entry.get("score") or 0) for entry in row.get("datatypeScores") or []}
            genetic_association = datatype.get("genetic_association", 0)
            somatic_mutation = datatype.get("somatic_mutation", 0)
            known_drugs = drugs_by_target.get(target.get("id"), [])
            profile = profiles_by_target.get(target.get("id"), {})
            normalized = {
                "gene": gene, "target_id": target.get("id"), "association_score": float(row.get("score") or 0),
                "genetic_association_score": genetic_association,
                "genetic_score": genetic_association,
                "somatic_mutation_score": somatic_mutation,
                "datatype_scores": datatype, "known_drugs": known_drugs,
                "tractability": profile.get("tractability", []),
                "safety_liabilities": profile.get("safety_liabilities", []),
            }
            associations.append(normalized)
            if genetic_association > 0:
                span = f"disease={resolved_disease_id}|target={target.get('id')}|genetic_association_score={genetic_association}"
                evidence.append(EvidenceItem(
                    tool_run_id=run_id, gene_symbol=gene, claim_class=ClaimClass.INFERRED,
                    statement=f"Open Targets reports a human-genetic association score of {genetic_association:.3g} for {gene} and {disease['name']}.",
                    source=SourceLocator(uri=f"https://platform.opentargets.org/disease/{resolved_disease_id}/associations", source_id=resolved_disease_id, version="live-or-cache", section="associatedTargets", chunk_id=f"ot-genetics-{gene}", version_meta={**_snapshot_meta(snapshot_meta, cached), "section": "associatedTargets"}),
                    source_span=span,
                    context=EvidenceContext(organism="Homo sapiens", disease=disease["name"], assay="Open Targets evidence aggregation"),
                    stance=Stance.SUPPORTS,
                    effect={"open_targets_genetic_association_score": genetic_association},
                    uncertainty=(
                        "This is an aggregate database evidence score, not locus-level fine-mapping or "
                        "colocalization evidence and not a treatment success probability."
                    ),
                    quality_flags=["database_aggregate_score", "not_formal_human_genetics"],
                    context_match_score=0.9,
                    context_match={
                        "matched_disease": [disease["name"]], "matched_tissue": [],
                        "matched_cell": [], "matched_assay": ["Open Targets evidence aggregation"],
                        "source_fields": ["associatedTargets"], "context_score_origin": "tool_estimate",
                        "tool_estimate": 0.9,
                    },
                    genetic_evidence=GeneticEvidencePayload(
                        evidence_type="open_targets_genetic_association",
                        analysis_level="database_aggregate",
                        study_id=f"OpenTargets:{resolved_disease_id}",
                        gene_symbol=gene,
                        strength=genetic_association,
                        formal_score_eligible=False,
                        assumptions=["Underlying studies and loci are not independently audited in this aggregate."],
                    ),
                ))
            if somatic_mutation > 0:
                evidence.append(EvidenceItem(
                    tool_run_id=run_id, gene_symbol=gene, claim_class=ClaimClass.INFERRED,
                    statement=(
                        f"Open Targets reports a somatic-mutation evidence score of "
                        f"{somatic_mutation:.3g} for {gene} and {disease['name']}."
                    ),
                    source=SourceLocator(
                        uri=f"https://platform.opentargets.org/disease/{resolved_disease_id}/associations",
                        source_id=resolved_disease_id, version="live-or-cache",
                        section="associatedTargets", chunk_id=f"ot-somatic-{gene}",
                        version_meta={**_snapshot_meta(snapshot_meta, cached), "section": "associatedTargets"},
                    ),
                    source_span=(
                        f"disease={resolved_disease_id}|target={target.get('id')}|"
                        f"somatic_mutation_score={somatic_mutation}"
                    ),
                    context=EvidenceContext(
                        organism="Homo sapiens", disease=disease["name"],
                        assay="Open Targets evidence aggregation",
                    ),
                    stance=Stance.SUPPORTS, effect={"somatic_mutation_score": somatic_mutation},
                    uncertainty=(
                        "Somatic-mutation evidence is distinct from inherited human-genetic support and "
                        "does not enter the strict human-genetics score."
                    ),
                    quality_flags=["database_aggregate_score", "somatic_not_germline_genetics"],
                    context_match_score=0.9,
                    context_match={
                        "matched_disease": [disease["name"]], "matched_tissue": [],
                        "matched_cell": [], "matched_assay": ["Open Targets evidence aggregation"],
                        "source_fields": ["associatedTargets"], "context_score_origin": "tool_estimate",
                        "tool_estimate": 0.9,
                    },
                ))
            for drug in known_drugs[:5]:
                span = f"target={target.get('id')}|drug={drug.get('drugId')}|clinical_stage={drug.get('phase')}|status={drug.get('status')}"
                evidence.append(EvidenceItem(
                    tool_run_id=run_id, gene_symbol=gene, claim_class=ClaimClass.INFERRED,
                    statement=f"Open Targets links {drug.get('prefName')} ({drug.get('drugId')}) to {gene}; reported clinical stage {drug.get('phase')}.",
                    source=SourceLocator(uri=f"https://platform.opentargets.org/target/{target.get('id')}", source_id=str(drug.get("drugId")), version="live-or-cache", section="knownDrugs", chunk_id=f"ot-drug-{gene}-{drug.get('drugId')}", version_meta={**_snapshot_meta(snapshot_meta, cached), "section": "knownDrugs"}),
                    source_span=span,
                    context=EvidenceContext(organism="Homo sapiens", disease=disease["name"], assay="Open Targets known drugs"),
                    stance=Stance.SUPPORTS, effect={"drug": drug},
                    uncertainty="Drug-target linkage does not establish efficacy in the requested disease context.",
                    quality_flags=["drug_link_not_disease_efficacy"], context_match_score=0.8,
                    context_match={
                        "matched_disease": [disease["name"]], "matched_tissue": [],
                        "matched_cell": [], "matched_assay": ["Open Targets known drugs"],
                        "source_fields": ["knownDrugs"], "context_score_origin": "tool_estimate",
                        "tool_estimate": 0.8,
                    },
                ))
            for liability in profile.get("safety_liabilities", [])[:3]:
                event = liability.get("event") or liability.get("eventId") or "unspecified safety liability"
                span = (
                    f"target={target.get('id')}|event={event}|datasource={liability.get('datasource')}|"
                    f"literature={liability.get('literature')}"
                )
                evidence.append(EvidenceItem(
                    tool_run_id=run_id, gene_symbol=gene, claim_class=ClaimClass.INFERRED,
                    statement=f"Open Targets records a safety liability for {gene}: {event}.",
                    source=SourceLocator(
                        uri=liability.get("url") or f"https://platform.opentargets.org/target/{target.get('id')}",
                        source_id=str(liability.get("eventId") or target.get("id")), version="live-or-cache",
                        section="safetyLiabilities", chunk_id=f"ot-safety-{gene}-{liability.get('eventId') or 'event'}",
                        version_meta={**_snapshot_meta(snapshot_meta, cached), "section": "safetyLiabilities"},
                    ),
                    source_span=span,
                    context=EvidenceContext(organism="Homo sapiens", disease=disease["name"], assay="Open Targets safety liability"),
                    stance=Stance.REFUTES, effect={"safety": liability},
                    uncertainty="A recorded liability is a risk signal; relevance depends on modality, exposure and tissue.",
                    quality_flags=["safety_blocker_retained"], context_match_score=0.8,
                    context_match={
                        "matched_disease": [disease["name"]], "matched_tissue": [],
                        "matched_cell": [], "matched_assay": ["Open Targets safety liability"],
                        "source_fields": ["safetyLiabilities"], "context_score_origin": "tool_estimate",
                        "tool_estimate": 0.8,
                    },
                ))
        aggregate_covered = bool(associations)
        inherited_covered = any(row["genetic_association_score"] > 0 for row in associations)
        result = ToolResult(
            tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
            status=ToolStatus.SUCCESS if aggregate_covered else ToolStatus.PARTIAL,
            coverage_status=CoverageStatus.COVERED if aggregate_covered else CoverageStatus.NOT_COVERED,
            context_match_score=0.9 if aggregate_covered else 0.0,
            inputs={"disease_id": disease_id, "genes": context.candidate_genes},
            outputs={"covered": aggregate_covered,
                     "aggregate_association_covered": aggregate_covered,
                     "inherited_genetic_association_covered": inherited_covered,
                     "locus_level_genetics_covered": False,
                     "disease": {"id": disease["id"], "name": disease["name"]},
                     "requested_disease_id": disease_id, "resolved_disease_id": resolved_disease_id,
                     "associations": associations,
                     "top_genetic_candidates": [
                         {"gene": row["gene"], "target_id": row["target_id"],
                          "genetic_association_score": row["genetic_association_score"],
                          "genetic_score": row["genetic_association_score"]}
                         for row in sorted(
                             associations, key=lambda row: row["genetic_association_score"], reverse=True,
                         ) if row["genetic_association_score"] > 0
                     ][:10],
                     "context_match": {
                         "matched_disease": [disease["name"]], "matched_tissue": [],
                         "matched_cell": [], "matched_assay": ["Open Targets evidence aggregation"],
                         "source_fields": ["associatedTargets", "knownDrugs", "safetyLiabilities"],
                     },
                     "context_score_origin": "tool_estimate",
                     "snapshot_meta": snapshot_meta
                     },
            candidate_genes=[
                row["gene"] for row in sorted(
                    associations, key=lambda row: row["genetic_association_score"], reverse=True,
                ) if row["genetic_association_score"] > 0
            ][:10],
            capability=capability, data_version="OpenTargets:live-or-cache", code_version="2.2.0",
            parameters={"graphql_endpoint": ENDPOINT}, evidence_ids=[item.evidence_id for item in evidence],
            warnings=([payload["clinical_warning"]] if payload.get("clinical_warning") else [])
                     + ([] if associations else ["candidate_genes_not_in_top_100_associations"])
                     + ([] if inherited_covered else ["no_inherited_genetic_association_for_selected_targets"]),
            limitations=[
                "Only the first 100 disease associations and selected candidate target profiles are retrieved for the MVP.",
                "Open Targets coverage is aggregate multi-evidence coverage, never locus-level genetics coverage.",
                "Open Targets is queried live when no cache exists, so results can drift between runs; retrieval timestamp and response digest are recorded for audit.",
            ],
            cached=cached, elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return ToolExecution(result=result, evidence=evidence)
