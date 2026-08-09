"""Typed, auditable ingestion of user-supplied GWAS/fine-mapping/coloc results.

The first release audits precomputed statistical results. It never chooses a
nearest gene and never describes association or posterior support as causal.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, TextIO

from ..contracts import (
    ArtifactRef, ClaimClass, CoverageStatus, EqtlColocalizationResultInput,
    EvidenceContext, EvidenceItem, FineMappingResultInput, GeneticEvidencePayload,
    GeneticsAssetBase, GwasSummaryStatsInput, SourceLocator, Stance, ToolCapability,
    ToolDescriptor, ToolResult, ToolStatus, new_id,
)
from .base import ScientificTool, ToolContext, ToolExecution


_GENE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.-]{0,31}$")
_VALID_CHROMOSOMES = {str(index) for index in range(1, 23)} | {"X", "Y", "MT"}
_PALINDROMIC = {frozenset({"A", "T"}), frozenset({"C", "G"})}


class GeneticsInputError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(run_dir: Path, path: Path, media_type: str) -> ArtifactRef:
    return ArtifactRef(
        name=path.name, uri=path.relative_to(run_dir).as_posix(), sha256=_sha256(path), media_type=media_type,
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_run_artifact(run_dir: Path, uri: str, expected_sha256: str | None) -> list[dict[str, Any]]:
    root = run_dir.resolve()
    path = (root / uri).resolve()
    if path == root or root not in path.parents:
        raise GeneticsInputError("normalized_artifact_path_escape")
    if not path.is_file():
        raise GeneticsInputError("normalized_artifact_missing")
    if not expected_sha256 or _sha256(path) != expected_sha256:
        raise GeneticsInputError("normalized_artifact_checksum_mismatch")
    return _read_jsonl(path)


def _input_root(context: ToolContext) -> Path:
    return context.settings.input_root.expanduser().resolve()


def _controlled_path(
    context: ToolContext, relative_path: str, expected_sha256: str, *, label: str = "asset",
) -> Path:
    root = _input_root(context)
    path = (root / relative_path).resolve()
    if path != root and root not in path.parents:
        raise GeneticsInputError(f"{label}_path_escape")
    if not path.is_file():
        raise GeneticsInputError(f"{label}_missing")
    if path.stat().st_size > context.task.constraints.genetics.max_file_size_mb * 1024 * 1024:
        raise GeneticsInputError(f"{label}_size_budget_exceeded")
    if _sha256(path) != expected_sha256:
        raise GeneticsInputError(f"{label}_checksum_mismatch")
    return path


def _asset_path(context: ToolContext, asset: GeneticsAssetBase) -> Path:
    return _controlled_path(context, asset.relative_path, asset.sha256)


def _open_table(path: Path, file_format: str) -> TextIO:
    if file_format == "tsv.gz":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def _float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise GeneticsInputError(f"invalid_{field}") from exc
    if not math.isfinite(number):
        raise GeneticsInputError(f"nonfinite_{field}")
    return number


def _integer(value: Any, field: str) -> int:
    number = _float(value, field)
    if not number.is_integer():
        raise GeneticsInputError(f"invalid_{field}")
    return int(number)


def _two_sided_log10_p(z_score: float) -> float:
    z = abs(z_score)
    if z <= 8:
        return math.log10(max(math.erfc(z / math.sqrt(2.0)), 1e-320))
    log_p = math.log(2.0) - (z * z / 2.0) - math.log(z) - 0.5 * math.log(2.0 * math.pi)
    return log_p / math.log(10.0)


def _variant(chromosome: Any, position: Any, effect: Any, other: Any, build: str) -> dict[str, Any]:
    chrom = str(chromosome).strip().upper().removeprefix("CHR")
    if chrom == "M":
        chrom = "MT"
    if chrom not in _VALID_CHROMOSOMES:
        raise GeneticsInputError("invalid_chromosome")
    pos = _integer(position, "position")
    if pos <= 0:
        raise GeneticsInputError("invalid_position")
    ea, oa = str(effect).strip().upper(), str(other).strip().upper()
    if not ea or not oa or ea == oa or any(token in ea + oa for token in (",", ";", "/")):
        raise GeneticsInputError("unsupported_multiallelic_or_missing_allele")
    if any(base not in {"A", "C", "G", "T"} for base in ea + oa):
        raise GeneticsInputError("unsupported_non_snv_allele")
    return {
        "chromosome": chrom, "position": pos, "effect_allele": ea, "other_allele": oa,
        "position_key": f"{build}:{chrom}:{pos}",
        "variant_key": f"{build}:{chrom}:{pos}:{oa}:{ea}",
    }


def _column(row: dict[str, str], name: str | None, field: str, required: bool = True) -> str | None:
    if not name:
        if required:
            raise GeneticsInputError(f"missing_column_mapping_{field}")
        return None
    value = row.get(name)
    if value is None or not str(value).strip():
        if required:
            raise GeneticsInputError(f"missing_{field}")
        return None
    return str(value).strip()


def _normalize_gwas(row: dict[str, str], asset: GwasSummaryStatsInput) -> dict[str, Any]:
    columns = asset.columns
    variant = _variant(
        _column(row, columns.chromosome, "chromosome"), _column(row, columns.position, "position"),
        _column(row, columns.effect_allele, "effect_allele"),
        _column(row, columns.other_allele, "other_allele"), asset.genome_build,
    )
    effect = _float(_column(row, columns.effect, "effect"), "effect")
    if asset.effect_scale == "odds_ratio":
        if effect <= 0:
            raise GeneticsInputError("invalid_odds_ratio")
        effect = math.log(effect)
    se = _float(_column(row, columns.standard_error, "standard_error"), "standard_error")
    p_value = _float(_column(row, columns.p_value, "p_value"), "p_value")
    if se <= 0 or not 0 < p_value <= 1:
        raise GeneticsInputError("invalid_standard_error_or_p_value")
    if abs(math.log10(p_value) - _two_sided_log10_p(effect / se)) > 4.0:
        raise GeneticsInputError("inconsistent_effect_standard_error_and_p_value")
    eaf_raw = _column(row, columns.effect_allele_frequency, "effect_allele_frequency", required=False)
    eaf = _float(eaf_raw, "effect_allele_frequency") if eaf_raw is not None else None
    if eaf is not None and not 0 <= eaf <= 1:
        raise GeneticsInputError("invalid_effect_allele_frequency")
    return {
        **variant, "variant_id": _column(row, columns.variant_id, "variant_id", required=False),
        "locus_id": _column(row, columns.locus_id, "locus_id", required=False),
        "beta": effect, "standard_error": se, "p_value": p_value,
        "effect_allele_frequency": eaf,
    }


def _normalize_fine_mapping(row: dict[str, str], asset: FineMappingResultInput) -> dict[str, Any]:
    columns = asset.columns
    variant = _variant(
        _column(row, columns.chromosome, "chromosome"), _column(row, columns.position, "position"),
        _column(row, columns.effect_allele, "effect_allele"),
        _column(row, columns.other_allele, "other_allele"), asset.genome_build,
    )
    posterior = _float(_column(row, columns.signal_posterior, "signal_posterior"), "signal_posterior")
    if not 0 <= posterior <= 1:
        raise GeneticsInputError("invalid_signal_posterior")
    return {
        **variant, "variant_id": _column(row, columns.variant_id, "variant_id", required=False),
        "locus_id": _column(row, columns.locus_id, "locus_id"),
        "credible_set_id": _column(row, columns.credible_set_id, "credible_set_id"),
        "signal_posterior": posterior,
    }


def _normalize_coloc(row: dict[str, str], asset: EqtlColocalizationResultInput) -> dict[str, Any]:
    columns = asset.columns
    gwas_variant = _variant(
        _column(row, columns.chromosome, "chromosome"), _column(row, columns.position, "position"),
        _column(row, columns.gwas_effect_allele, "gwas_effect_allele"),
        _column(row, columns.gwas_other_allele, "gwas_other_allele"), asset.genome_build,
    )
    eqtl_ea = _column(row, columns.eqtl_effect_allele, "eqtl_effect_allele")
    eqtl_oa = _column(row, columns.eqtl_other_allele, "eqtl_other_allele")
    eqtl_variant = _variant(
        gwas_variant["chromosome"], gwas_variant["position"], eqtl_ea, eqtl_oa, asset.genome_build,
    )
    gene = _column(row, columns.gene, "gene") or ""
    if not _GENE_RE.fullmatch(gene):
        raise GeneticsInputError("invalid_gene_symbol")
    probabilities = {key: _float(_column(row, getattr(columns, key), key), key) for key in ("pp0", "pp1", "pp2", "pp3", "pp4")}
    if any(not 0 <= value <= 1 for value in probabilities.values()) or abs(sum(probabilities.values()) - 1) > 0.02:
        raise GeneticsInputError("invalid_coloc_posterior_vector")
    n_variants = _integer(_column(row, columns.n_variants, "n_variants"), "n_variants")
    if n_variants <= 0:
        raise GeneticsInputError("invalid_n_variants")
    return {
        **gwas_variant, "variant_id": _column(row, columns.variant_id, "variant_id", required=False),
        "locus_id": _column(row, columns.locus_id, "locus_id"),
        "signal_id": _column(row, columns.signal_id, "signal_id"), "gene": gene.upper(),
        "gwas_effect_allele": gwas_variant["effect_allele"],
        "gwas_other_allele": gwas_variant["other_allele"],
        "eqtl_effect_allele": eqtl_variant["effect_allele"],
        "eqtl_other_allele": eqtl_variant["other_allele"],
        "eqtl_beta": _float(_column(row, columns.eqtl_beta, "eqtl_beta"), "eqtl_beta"),
        "gwas_effect_allele_frequency": _float(gwas_eaf, "gwas_effect_allele_frequency") if (gwas_eaf := _column(row, columns.gwas_effect_allele_frequency, "gwas_effect_allele_frequency", required=False)) is not None else None,
        "eqtl_effect_allele_frequency": _float(eqtl_eaf, "eqtl_effect_allele_frequency") if (eqtl_eaf := _column(row, columns.eqtl_effect_allele_frequency, "eqtl_effect_allele_frequency", required=False)) is not None else None,
        **probabilities, "n_variants": n_variants,
    }


def _normalize_harmonized_variant(
    row: dict[str, str], asset: EqtlColocalizationResultInput,
) -> dict[str, Any]:
    columns = asset.harmonized_variants.columns
    gwas_variant = _variant(
        _column(row, columns.chromosome, "chromosome"), _column(row, columns.position, "position"),
        _column(row, columns.gwas_effect_allele, "gwas_effect_allele"),
        _column(row, columns.gwas_other_allele, "gwas_other_allele"), asset.genome_build,
    )
    eqtl_variant = _variant(
        gwas_variant["chromosome"], gwas_variant["position"],
        _column(row, columns.eqtl_effect_allele, "eqtl_effect_allele"),
        _column(row, columns.eqtl_other_allele, "eqtl_other_allele"), asset.genome_build,
    )
    gene = _column(row, columns.gene, "gene") or ""
    if not _GENE_RE.fullmatch(gene):
        raise GeneticsInputError("invalid_gene_symbol")
    return {
        "position_key": gwas_variant["position_key"],
        "variant_key": gwas_variant["variant_key"],
        "variant_id": _column(row, columns.variant_id, "variant_id", required=False),
        "locus_id": _column(row, columns.locus_id, "locus_id"),
        "signal_id": _column(row, columns.signal_id, "signal_id"),
        "gene": gene.upper(),
        "gwas_effect_allele": gwas_variant["effect_allele"],
        "gwas_other_allele": gwas_variant["other_allele"],
        "eqtl_effect_allele": eqtl_variant["effect_allele"],
        "eqtl_other_allele": eqtl_variant["other_allele"],
        "gwas_effect_allele_frequency": _float(gwas_eaf, "gwas_effect_allele_frequency") if (gwas_eaf := _column(row, columns.gwas_effect_allele_frequency, "gwas_effect_allele_frequency", required=False)) is not None else None,
        "eqtl_effect_allele_frequency": _float(eqtl_eaf, "eqtl_effect_allele_frequency") if (eqtl_eaf := _column(row, columns.eqtl_effect_allele_frequency, "eqtl_effect_allele_frequency", required=False)) is not None else None,
    }


def _row_identity(asset: GeneticsAssetBase, row: dict[str, Any]) -> tuple[Any, ...]:
    allele_set = tuple(sorted((row["effect_allele"], row["other_allele"])))
    if isinstance(asset, FineMappingResultInput):
        return row["locus_id"], row["credible_set_id"], row["position_key"], allele_set
    if isinstance(asset, EqtlColocalizationResultInput):
        return row["locus_id"], row["signal_id"], row["gene"], row["position_key"], allele_set
    return row["position_key"], allele_set


def _normalized_rows(path: Path, asset: GeneticsAssetBase, max_rows: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    delimiter = "," if asset.file_format == "csv" else "\t"
    rows: list[dict[str, Any]] = []
    errors: dict[str, int] = defaultdict(int)
    seen: set[tuple[Any, ...]] = set()
    with _open_table(path, asset.file_format) as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if not reader.fieldnames:
            raise GeneticsInputError("empty_or_headerless_file")
        for index, row in enumerate(reader, start=1):
            if index > max_rows:
                raise GeneticsInputError("row_budget_exceeded")
            try:
                if isinstance(asset, GwasSummaryStatsInput):
                    normalized = _normalize_gwas(row, asset)
                elif isinstance(asset, FineMappingResultInput):
                    normalized = _normalize_fine_mapping(row, asset)
                else:
                    normalized = _normalize_coloc(row, asset)
                key = _row_identity(asset, normalized)
                if key in seen:
                    raise GeneticsInputError("duplicate_variant")
                seen.add(key)
                normalized.update({
                    "asset_id": asset.asset_id, "row_number": index,
                    "study_id": asset.study_id, "genome_build": asset.genome_build,
                    "ancestry": asset.ancestry,
                })
                rows.append(normalized)
            except GeneticsInputError as exc:
                errors[str(exc)] += 1
    total = len(rows) + sum(errors.values())
    if total == 0:
        raise GeneticsInputError("empty_data_file")
    invalid_fraction = sum(errors.values()) / total
    if not rows or invalid_fraction > 0.05:
        raise GeneticsInputError(f"row_qc_failed:{dict(sorted(errors.items()))}")
    return rows, {
        "rows_total": total, "rows_valid": len(rows), "rows_rejected": sum(errors.values()),
        "invalid_fraction": round(invalid_fraction, 6), "rejection_reasons": dict(sorted(errors.items())),
    }


def _normalized_manifest_rows(
    path: Path, asset: EqtlColocalizationResultInput, max_rows: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    delimiter = "," if asset.harmonized_variants.file_format == "csv" else "\t"
    rows: list[dict[str, Any]] = []
    errors: dict[str, int] = defaultdict(int)
    seen: set[tuple[Any, ...]] = set()
    with _open_table(path, asset.harmonized_variants.file_format) as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if not reader.fieldnames:
            raise GeneticsInputError("empty_or_headerless_variant_manifest")
        for index, source in enumerate(reader, start=1):
            if index > max_rows:
                raise GeneticsInputError("variant_manifest_row_budget_exceeded")
            try:
                row = _normalize_harmonized_variant(source, asset)
                key = (
                    row["locus_id"], row["signal_id"], row["gene"], row["position_key"],
                    tuple(sorted((row["gwas_effect_allele"], row["gwas_other_allele"]))),
                    tuple(sorted((row["eqtl_effect_allele"], row["eqtl_other_allele"]))),
                )
                if key in seen:
                    raise GeneticsInputError("duplicate_harmonized_variant")
                seen.add(key)
                row.update({"asset_id": asset.asset_id, "row_number": index})
                rows.append(row)
            except GeneticsInputError as exc:
                errors[str(exc)] += 1
    total = len(rows) + sum(errors.values())
    if total == 0:
        raise GeneticsInputError("empty_variant_manifest")
    invalid_fraction = sum(errors.values()) / total
    if not rows or invalid_fraction > 0.05:
        raise GeneticsInputError(f"variant_manifest_qc_failed:{dict(sorted(errors.items()))}")
    return rows, {
        "rows_total": total, "rows_valid": len(rows), "rows_rejected": sum(errors.values()),
        "invalid_fraction": round(invalid_fraction, 6), "rejection_reasons": dict(sorted(errors.items())),
    }


def _latest(context: ToolContext, tool_name: str) -> ToolResult | None:
    return next((result for result in reversed(context.prior_results) if result.tool_name == tool_name), None)


def _asset_records(context: ToolContext, kind: str | None = None) -> list[dict[str, Any]]:
    audit = _latest(context, "genetics_input_audit")
    if not audit:
        return []
    records = []
    for asset in audit.outputs.get("assets", []):
        if kind and asset.get("kind") != kind:
            continue
        records.extend(_read_run_artifact(
            context.run_dir, asset["normalized_artifact"], asset.get("normalized_sha256"),
        ))
    return records


def _manifest_records(context: ToolContext) -> list[dict[str, Any]]:
    audit = _latest(context, "genetics_input_audit")
    if not audit:
        return []
    records = []
    for asset in audit.outputs.get("assets", []):
        artifact = asset.get("harmonized_variant_artifact")
        if artifact:
            records.extend(_read_run_artifact(
                context.run_dir, artifact, asset.get("harmonized_variant_sha256"),
            ))
    return records


def _capability(methods: list[str]) -> ToolCapability:
    return ToolCapability(
        supported_organisms=["Homo sapiens"], supported_genome_builds=["GRCh37", "GRCh38"],
        supported_ancestries=["declared by input asset"], supported_methods=methods,
        validation_scope="Pre-staged, checksum-bound summary statistics and precomputed results",
        assumptions=["No nearest-gene assignment", "Association and posterior support do not establish causality"],
    )


class GeneticsInputAuditTool(ScientificTool):
    name = "genetics_input_audit"
    version = "2.2.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="genetics",
        description="Validate checksum-bound GWAS, fine-mapping and colocalization assets and normalize variants.",
        input_types=["TaskSpec.genetics_inputs"], output_types=["GeneticsAssetAudit[]", "ArtifactRef[]"],
        execution_policy="fixed_script", critical=True,
    )

    def run(self, context: ToolContext) -> ToolExecution:
        started, run_id = time.perf_counter(), new_id("tool")
        if not context.task.genetics_inputs:
            return ToolExecution(result=ToolResult(
                tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
                status=ToolStatus.OUT_OF_SCOPE, coverage_status=CoverageStatus.NOT_COVERED,
                context_match_score=0.0, inputs={}, outputs={"covered": False},
                capability=_capability([]), limitations=["No genetics_inputs were supplied."],
            ), evidence=[])
        asset_outputs, artifacts, failures, partial_assets = [], [], [], []
        for asset in context.task.genetics_inputs:
            asset_artifacts: list[ArtifactRef] = []
            try:
                path = _asset_path(context, asset)
                rows, qc = _normalized_rows(
                    path, asset, context.task.constraints.genetics.max_rows_per_asset,
                )
                if qc["rows_rejected"]:
                    if not isinstance(asset, GwasSummaryStatsInput):
                        raise GeneticsInputError("statistical_result_contains_rejected_rows")
                    partial_assets.append(asset.asset_id)
                if context.task.context.locus_id and not any(
                    row.get("locus_id") == context.task.context.locus_id for row in rows
                ):
                    raise GeneticsInputError("requested_locus_missing_from_asset")
                normalized = context.run_dir / "genetics" / f"{asset.asset_id}.normalized.jsonl"
                _write_jsonl(normalized, rows)
                ref = _artifact(context.run_dir, normalized, "application/x-ndjson")
                asset_artifacts.append(ref)
                asset_output = {
                    "asset_id": asset.asset_id, "kind": asset.kind, "study_id": asset.study_id,
                    "genome_build": asset.genome_build, "ancestry": asset.ancestry,
                    "sample_size": asset.sample_size, "source_version": asset.source_version,
                    "input_sha256": asset.sha256, "normalized_artifact": ref.uri,
                    "normalized_sha256": ref.sha256, "qc": qc,
                }
                if isinstance(asset, EqtlColocalizationResultInput):
                    manifest = asset.harmonized_variants
                    manifest_path = _controlled_path(
                        context, manifest.relative_path, manifest.sha256,
                        label="harmonized_variant_manifest",
                    )
                    manifest_rows, manifest_qc = _normalized_manifest_rows(
                        manifest_path, asset, context.task.constraints.genetics.max_rows_per_asset,
                    )
                    if manifest_qc["rows_rejected"]:
                        raise GeneticsInputError("harmonized_variant_manifest_contains_rejected_rows")
                    normalized_manifest = (
                        context.run_dir / "genetics" / f"{asset.asset_id}.harmonized_variants.jsonl"
                    )
                    _write_jsonl(normalized_manifest, manifest_rows)
                    manifest_ref = _artifact(context.run_dir, normalized_manifest, "application/x-ndjson")
                    asset_artifacts.append(manifest_ref)
                    sensitivity = asset.sensitivity_artifact
                    sensitivity_path = _controlled_path(
                        context, sensitivity.relative_path, sensitivity.sha256,
                        label="sensitivity_artifact",
                    )
                    sensitivity_copy = (
                        context.run_dir / "genetics" / f"{asset.asset_id}.sensitivity{''.join(sensitivity_path.suffixes)}"
                    )
                    sensitivity_copy.parent.mkdir(parents=True, exist_ok=True)
                    sensitivity_copy.write_bytes(sensitivity_path.read_bytes())
                    sensitivity_ref = _artifact(
                        context.run_dir, sensitivity_copy, sensitivity.media_type,
                    )
                    asset_artifacts.append(sensitivity_ref)
                    asset_output.update({
                        "gwas_study_id": asset.gwas_study_id,
                        "eqtl_study_id": asset.eqtl_study_id,
                        "eqtl_ancestry": asset.eqtl_ancestry,
                        "harmonized_variant_input_sha256": manifest.sha256,
                        "harmonized_variant_artifact": manifest_ref.uri,
                        "harmonized_variant_sha256": manifest_ref.sha256,
                        "harmonized_variant_qc": manifest_qc,
                        "sensitivity_input_sha256": sensitivity.sha256,
                        "sensitivity_artifact": sensitivity_ref.uri,
                        "sensitivity_artifact_sha256": sensitivity_ref.sha256,
                    })
                asset_outputs.append(asset_output)
                artifacts.extend(asset_artifacts)
            except (OSError, GeneticsInputError, UnicodeError, csv.Error) as exc:
                failures.append({"asset_id": asset.asset_id, "kind": asset.kind, "error": str(exc)[:500]})
        if not asset_outputs:
            status, coverage, error = ToolStatus.FAILED, CoverageStatus.NOT_COVERED, "all genetics assets failed deterministic input QC"
        elif failures or partial_assets:
            status, coverage, error = ToolStatus.PARTIAL, CoverageStatus.PARTIAL, None
        else:
            status, coverage, error = ToolStatus.SUCCESS, CoverageStatus.COVERED, None
        result = ToolResult(
            tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
            status=status, coverage_status=coverage, context_match_score=1.0 if asset_outputs else 0.0,
            inputs={"asset_ids": [asset.asset_id for asset in context.task.genetics_inputs]},
            outputs={
                "covered": bool(asset_outputs), "assets": asset_outputs,
                "failed_assets": failures, "partial_assets": partial_assets,
            },
            capability=_capability([]), artifacts=artifacts, error=error,
            limitations=[
                "Only tabular SNVs and precomputed fine-mapping/colocalization results are supported.",
                "Colocalization requires a checksum-bound, variant-level harmonization manifest.",
            ],
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return ToolExecution(result=result, evidence=[])


class FineMappingAuditTool(ScientificTool):
    name = "fine_mapping_audit"
    version = "2.2.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="genetics",
        description="Audit precomputed SuSiE signal posteriors, credible-set coverage, LD provenance and GWAS overlap.",
        input_types=["GeneticsAssetAudit[]"], output_types=["CredibleSet[]"],
        execution_policy="fixed_script",
    )

    def run(self, context: ToolContext) -> ToolExecution:
        started, run_id = time.perf_counter(), new_id("tool")
        fine_assets = [asset for asset in context.task.genetics_inputs if isinstance(asset, FineMappingResultInput)]
        if not fine_assets:
            return ToolExecution(result=ToolResult(
                tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
                status=ToolStatus.OUT_OF_SCOPE, coverage_status=CoverageStatus.NOT_COVERED,
                context_match_score=0.0, inputs={}, outputs={"covered": False, "credible_sets": []},
                capability=_capability(["susie:signal_posterior"]),
                limitations=["No fine_mapping_result asset was supplied."],
            ), evidence=[])
        rows = _asset_records(context, "fine_mapping_result")
        gwas_variants: dict[str, set[tuple[str, tuple[str, ...]]]] = defaultdict(set)
        for row in _asset_records(context, "gwas_summary_statistics"):
            gwas_variants[row["study_id"]].add((
                row["position_key"], tuple(sorted((row["effect_allele"], row["other_allele"]))),
            ))
        by_asset = {asset.asset_id: asset for asset in fine_assets}
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[(row["asset_id"], row["locus_id"], row["credible_set_id"])].append(row)
        credible_sets = []
        tolerance = context.task.constraints.genetics.credible_set_sum_tolerance
        for (asset_id, locus_id, cs_id), members in sorted(grouped.items()):
            asset = by_asset[asset_id]
            ordered = sorted(members, key=lambda row: (-row["signal_posterior"], row["variant_key"]))
            posterior_sum = sum(row["signal_posterior"] for row in ordered)
            reasons = []
            ld = asset.ld_reference
            if not ld or not ld.matched_to_study:
                reasons.append("missing_or_unmatched_ld_reference")
            elif ld.genome_build != asset.genome_build or ld.ancestry.casefold() != asset.ancestry.casefold():
                reasons.append("ld_build_or_ancestry_mismatch")
            if asset.credible_level < context.task.constraints.genetics.credible_set_level:
                reasons.append("credible_set_level_below_task_minimum")
            if not asset.credible_level - tolerance <= posterior_sum <= 1.0 + tolerance:
                reasons.append("credible_set_signal_posterior_sum_out_of_tolerance")
            expected = gwas_variants.get(asset.study_id, set())
            if not expected:
                reasons.append("matching_gwas_study_missing")
            elif any((
                row["position_key"], tuple(sorted((row["effect_allele"], row["other_allele"])))
            ) not in expected for row in ordered):
                reasons.append("credible_set_variant_missing_from_gwas")
            cumulative, selected = 0.0, []
            for row in ordered:
                selected.append(row)
                cumulative += row["signal_posterior"]
                if cumulative >= asset.credible_level:
                    break
            credible_sets.append({
                "asset_id": asset_id, "locus_id": locus_id, "credible_set_id": cs_id,
                "study_id": asset.study_id,
                "method": asset.method, "method_version": asset.method_version,
                "posterior_kind": asset.posterior_kind,
                "requested_coverage": asset.credible_level,
                "signal_posterior_sum": round(posterior_sum, 8),
                "cumulative_signal_posterior": round(cumulative, 8),
                "ld_reference": {
                    "reference_id": ld.reference_id if ld else None,
                    "version": ld.version if ld else None,
                    "sha256": ld.sha256 if ld else None,
                    "sample_size": ld.sample_size if ld else None,
                },
                "variants": [{
                    "variant_key": row["variant_key"], "position_key": row["position_key"],
                    "signal_posterior": row["signal_posterior"],
                } for row in selected],
                "formal_score_eligible": not reasons, "rejection_reasons": reasons,
                "causal_status": "not_established",
            })
        output_path = context.run_dir / "genetics" / "credible_sets.json"
        _write_json(output_path, credible_sets)
        valid = [row for row in credible_sets if row["formal_score_eligible"]]
        status = ToolStatus.SUCCESS if valid and len(valid) == len(credible_sets) else ToolStatus.PARTIAL
        coverage = CoverageStatus.COVERED if status == ToolStatus.SUCCESS else CoverageStatus.PARTIAL
        result = ToolResult(
            tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
            status=status, coverage_status=coverage, context_match_score=1.0 if valid else 0.0,
            inputs={"asset_ids": [asset.asset_id for asset in fine_assets]},
            outputs={"covered": bool(valid), "credible_sets": credible_sets},
            capability=_capability(["susie:signal_posterior"]),
            artifacts=[_artifact(context.run_dir, output_path, "application/json")],
            limitations=[
                "Signal posterior and credible sets are model/LD dependent and are not biological causal probabilities.",
                "This release audits precomputed SuSiE signal posteriors; it does not treat marginal PIP as signal coverage.",
            ],
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return ToolExecution(result=result, evidence=[])


_TISSUE_SYNONYMS = {
    "lung": {"lung", "pulmonary", "bronchus", "bronchial", "alveolar", "alveolus", "lung tissue", "lung parenchyma", "pneumocyte"},
    "blood": {"blood", "whole blood", "peripheral blood", "peripheral blood mononuclear cell", "pbmc", "blood-derived"},
    "colon": {"colon", "colonic", "colonic mucosa", "large intestine", "colorectal", "intestinal", "gut", "bowel"},
    "brain": {"brain", "cerebral", "cortex", "cortical", "hippocampus", "hippocampal", "cerebellum", "cerebellar", "frontal", "prefrontal", "cerebrospinal"},
    "liver": {"liver", "hepatic", "hepatocyte", "hepatocellular"},
    "kidney": {"kidney", "renal", "nephron", "glomerular"},
    "heart": {"heart", "cardiac", "myocardial", "myocardium"},
    "skin": {"skin", "dermal", "epidermal", "cutaneous"},
    "muscle": {"muscle", "skeletal muscle", "myocyte", "smooth muscle"},
    "breast": {"breast", "mammary"},
    "prostate": {"prostate", "prostatic"},
    "pancreas": {"pancreas", "pancreatic"},
    "spleen": {"spleen", "splenic"},
    "bone_marrow": {"bone marrow", "marrow"},
    "lymph_node": {"lymph node", "lymph node tissue", "lymphatic"},
    "adipose": {"adipose", "adipocyte", "fat"},
    "testis": {"testis", "testicular"},
    "ovary": {"ovary", "ovarian"},
}

_CELL_SYNONYMS = {
    "t cell": {"t cell", "t-cell", "t cells", "t lymphocyte", "t lymphocytes", "cd4", "cd8", "cd4+ t cell", "cd8+ t cell", "regulatory t cell", "treg", "cytotoxic t cell", "helper t cell"},
    "b cell": {"b cell", "b-cell", "b cells", "b lymphocyte", "b lymphocytes", "plasma cell", "plasma b cell"},
    "nk cell": {"nk cell", "nk cells", "natural killer cell", "natural killer cells"},
    "macrophage": {"macrophage", "macrophages", "monocyte-derived macrophage", "m1 macrophage", "m2 macrophage", "kupffer cell", "alveolar macrophage", "microglia"},
    "monocyte": {"monocyte", "monocytes", "cd14", "cd14+ monocyte"},
    "dendritic cell": {"dendritic cell", "dendritic cells", "plasmacytoid dendritic cell", "cdc"},
    "neutrophil": {"neutrophil", "neutrophils"},
    "fibroblast": {"fibroblast", "fibroblasts", "fibroblast-like", "stromal", "stroma", "caf"},
    "endothelial cell": {"endothelial", "endothelium", "endothelial cell", "endothelial cells"},
    "epithelial cell": {"epithelial", "epithelium", "epithelial cell", "epithelial cells", "enterocyte", "enterocytes", "alveolar type ii", "alveolar type ii cell", "at2", "alveolar type i", "alveolar type i cell"},
    "neuron": {"neuron", "neurons", "neuronal", "excitatory neuron", "inhibitory neuron", "glutamatergic neuron", "gabaergic neuron"},
    "astrocyte": {"astrocyte", "astrocytes"},
    "oligodendrocyte": {"oligodendrocyte", "oligodendrocytes"},
    "hepatocyte": {"hepatocyte", "hepatocytes"},
    "smooth muscle cell": {"smooth muscle", "smooth muscle cell", "smc"},
    "skeletal muscle cell": {"skeletal muscle", "skeletal muscle cell", "myocyte", "myocytes"},
    "cardiac muscle cell": {"cardiac muscle", "cardiomyocyte", "cardiomyocytes", "cardiac myocyte"},
    "adipocyte": {"adipocyte", "adipocytes", "fat cell"},
    "stem cell": {"stem cell", "stem cells", "hematopoietic stem cell", "hsc", "ipsc", "ips cell"},
}


def _context_token_canonical(term: str, synonym_map: dict[str, set[str]]) -> tuple[str | None, str]:
    tokens = set(re.findall(r"[a-z0-9]+", term.casefold().replace("-", " ")))
    for canonical, aliases in synonym_map.items():
        canonical_tokens = set(re.findall(r"[a-z0-9]+", canonical.replace("-", " ")))
        if canonical_tokens and canonical_tokens <= tokens:
            return canonical, "canonical"
        for alias in aliases:
            alias_tokens = set(re.findall(r"[a-z0-9]+", alias.replace("-", " ")))
            if alias_tokens and alias_tokens <= tokens:
                return canonical, "synonym"
    return None, "unknown"


def _term_match(requested: str, observed: str, synonym_map: dict[str, set[str]]) -> tuple[float, list[str], str]:
    if requested == observed:
        return 1.0, [requested], "exact"
    requested_canon, _ = _context_token_canonical(requested, synonym_map)
    observed_canon, _ = _context_token_canonical(observed, synonym_map)
    if requested_canon and requested_canon == observed_canon:
        return 1.0, [observed], "synonym"
    requested_tokens = set(re.findall(r"[a-z0-9]+", requested))
    observed_tokens = set(re.findall(r"[a-z0-9]+", observed))
    overlap = requested_tokens & observed_tokens
    if overlap:
        return 0.85, sorted(overlap), "token_overlap"
    if requested in observed or observed in requested:
        return 0.6, [], "substring"
    return 0.3, [], "no_match"


def _context_match_detail(context: ToolContext, asset: EqtlColocalizationResultInput) -> tuple[float, dict[str, Any]]:
    requested_tissue = (context.task.context.tissue or "").casefold()
    observed_tissue = asset.tissue.casefold()
    matched: list[str] = []
    rules: list[str] = []
    if not requested_tissue:
        score = 0.3
        rules.append("tissue:no_requested")
    else:
        tissue_score, tissue_terms, tissue_rule = _term_match(requested_tissue, observed_tissue, _TISSUE_SYNONYMS)
        score = tissue_score
        matched.extend(tissue_terms)
        rules.append(f"tissue:{tissue_rule}")
    if context.task.context.cell_type:
        requested_cell = context.task.context.cell_type.casefold()
        if not asset.cell_type:
            score = min(score, 0.3)
            rules.append("cell_type:no_asset")
        else:
            cell_score, cell_terms, cell_rule = _term_match(requested_cell, asset.cell_type.casefold(), _CELL_SYNONYMS)
            score = min(score, cell_score)
            matched.extend(cell_terms)
            rules.append(f"cell_type:{cell_rule}")
    return round(score, 3), {"matched_terms": list(dict.fromkeys(matched)), "match_rules": rules}


def _context_match(context: ToolContext, asset: EqtlColocalizationResultInput) -> float:
    score, _ = _context_match_detail(context, asset)
    return score


def _harmonize_alleles(
    gwas: dict[str, Any], coloc: dict[str, Any], reject_palindromic: bool,
) -> tuple[str, int | None]:
    gwas_pair = frozenset({gwas["effect_allele"], gwas["other_allele"]})
    coloc_gwas_pair = frozenset({coloc["gwas_effect_allele"], coloc["gwas_other_allele"]})
    eqtl_pair = frozenset({coloc["eqtl_effect_allele"], coloc["eqtl_other_allele"]})
    if gwas_pair != coloc_gwas_pair or gwas_pair != eqtl_pair:
        return "allele_set_mismatch", None
    frequency = gwas.get("effect_allele_frequency")
    if reject_palindromic and gwas_pair in _PALINDROMIC and (
        frequency is None or 0.4 <= float(frequency) <= 0.6
    ):
        return "palindromic_ambiguous_without_informative_frequency", None
    if (gwas["effect_allele"], gwas["other_allele"]) == (coloc["eqtl_effect_allele"], coloc["eqtl_other_allele"]):
        sign = 1
    elif (gwas["effect_allele"], gwas["other_allele"]) == (coloc["eqtl_other_allele"], coloc["eqtl_effect_allele"]):
        sign = -1
    else:
        return "orientation_unresolved", None
    gwas_eaf = gwas.get("effect_allele_frequency")
    eqtl_eaf = coloc.get("eqtl_effect_allele_frequency")
    if gwas_eaf is not None and eqtl_eaf is not None:
        expected = float(eqtl_eaf) if sign == 1 else 1.0 - float(eqtl_eaf)
        if abs(float(gwas_eaf) - expected) > 0.15:
            return "eaf_inconsistent", None
    return ("direct" if sign == 1 else "swapped"), sign


def _harmonize(
    gwas: dict[str, Any], coloc: dict[str, Any], reject_palindromic: bool,
) -> tuple[str, float | None]:
    status, sign = _harmonize_alleles(gwas, coloc, reject_palindromic)
    return status, (float(coloc["eqtl_beta"]) * sign if sign is not None else None)


class EqtlColocalizationAuditTool(ScientificTool):
    name = "eqtl_colocalization_audit"
    version = "2.2.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="genetics",
        description="Audit precomputed regional colocalization, variant overlap, alleles and tissue context.",
        input_types=["GeneticsAssetAudit[]", "CredibleSet[]"], output_types=["ColocalizationResult[]"],
        execution_policy="fixed_script",
    )

    def run(self, context: ToolContext) -> ToolExecution:
        started, run_id = time.perf_counter(), new_id("tool")
        coloc_assets = [asset for asset in context.task.genetics_inputs if isinstance(asset, EqtlColocalizationResultInput)]
        if not coloc_assets:
            return ToolExecution(result=ToolResult(
                tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
                status=ToolStatus.OUT_OF_SCOPE, coverage_status=CoverageStatus.NOT_COVERED,
                context_match_score=0.0, inputs={}, outputs={"covered": False, "colocalizations": []},
                capability=_capability(["coloc_abf", "coloc_susie"]),
                limitations=["No eqtl_colocalization_result asset was supplied."],
            ), evidence=[])
        coloc_rows = _asset_records(context, "eqtl_colocalization_result")
        gwas_by_variant = {
            (
                row["study_id"], row["position_key"],
                tuple(sorted((row["effect_allele"], row["other_allele"]))),
            ): row
            for row in _asset_records(context, "gwas_summary_statistics")
        }
        manifest_by_signal: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for manifest_row in _manifest_records(context):
            manifest_by_signal[(
                manifest_row["asset_id"], manifest_row["locus_id"],
                manifest_row["signal_id"], manifest_row["gene"],
            )].append(manifest_row)
        fine = _latest(context, "fine_mapping_audit")
        fine_by_signal = {
            (row["study_id"], row["locus_id"], row["credible_set_id"]): {
                item["position_key"] for item in row["variants"]
            }
            for row in (fine.outputs.get("credible_sets", []) if fine else []) if row.get("formal_score_eligible")
        }
        assets = {asset.asset_id: asset for asset in coloc_assets}
        audited = []
        for row in coloc_rows:
            asset = assets[row["asset_id"]]
            reasons = []
            gwas = gwas_by_variant.get((
                asset.gwas_study_id, row["position_key"],
                tuple(sorted((row["gwas_effect_allele"], row["gwas_other_allele"]))),
            ))
            if not gwas:
                reasons.append("lead_variant_not_present_in_harmonized_gwas")
                harmonization, aligned_beta = "not_tested", None
            elif gwas["p_value"] > context.task.constraints.genetics.gwas_p_value_threshold:
                reasons.append("gwas_variant_not_genome_wide_significant")
                harmonization, aligned_beta = _harmonize(
                    gwas, row, context.task.constraints.genetics.reject_palindromic_without_frequency,
                )
            else:
                harmonization, aligned_beta = _harmonize(
                    gwas, row, context.task.constraints.genetics.reject_palindromic_without_frequency,
                )
            if aligned_beta is None:
                reasons.append(harmonization)
            manifest_rows = manifest_by_signal.get((
                row["asset_id"], row["locus_id"], row["signal_id"], row["gene"],
            ), [])
            regional_failures: dict[str, int] = defaultdict(int)
            regional_valid = 0
            for manifest_row in manifest_rows:
                regional_gwas = gwas_by_variant.get((
                    asset.gwas_study_id, manifest_row["position_key"],
                    tuple(sorted((
                        manifest_row["gwas_effect_allele"], manifest_row["gwas_other_allele"],
                    ))),
                ))
                if not regional_gwas:
                    regional_failures["variant_missing_from_matching_gwas"] += 1
                    continue
                status, _ = _harmonize_alleles(
                    regional_gwas, manifest_row,
                    context.task.constraints.genetics.reject_palindromic_without_frequency,
                )
                if status not in {"direct", "swapped"}:
                    regional_failures[status] += 1
                    continue
                regional_valid += 1
            if not manifest_rows:
                reasons.append("missing_harmonized_variant_manifest")
            elif regional_failures:
                reasons.append("regional_allele_harmonization_failed")
            if row["n_variants"] != len(manifest_rows):
                reasons.append("reported_variant_overlap_mismatch")
            if regional_valid < max(
                asset.minimum_variant_overlap_used,
                context.task.constraints.genetics.minimum_coloc_variant_overlap,
            ):
                reasons.append("insufficient_variant_overlap")
            if row["pp4"] < context.task.constraints.genetics.minimum_coloc_pp4 or row["pp4"] <= row["pp3"]:
                reasons.append("shared_signal_not_supported")
            if context.task.constraints.genetics.require_coloc_sensitivity and not asset.sensitivity_analysis_passed:
                reasons.append("coloc_sensitivity_not_passed")
            if asset.sample_overlap == "unknown":
                reasons.append("sample_overlap_unresolved")
            context_score, context_detail = _context_match_detail(context, asset)
            if context_score < 0.5:
                reasons.append("eqtl_context_mismatch")
            if asset.eqtl_ancestry.casefold() != asset.ancestry.casefold():
                reasons.append("gwas_eqtl_ancestry_mismatch")
            fine_key = (asset.gwas_study_id, row["locus_id"], row["signal_id"])
            if fine_key not in fine_by_signal:
                reasons.append("signal_missing_valid_credible_set")
            elif fine_key in fine_by_signal and row["position_key"] not in fine_by_signal[fine_key]:
                reasons.append("coloc_variant_outside_valid_credible_set")
            audited.append({
                **row, "method": asset.method, "method_version": asset.method_version,
                "tissue": asset.tissue, "cell_type": asset.cell_type, "ancestry": asset.ancestry,
                "genome_build": asset.genome_build, "study_id": asset.gwas_study_id,
                "gwas_study_id": asset.gwas_study_id, "eqtl_study_id": asset.eqtl_study_id,
                "sample_overlap": asset.sample_overlap,
                "sample_overlap_adjustment": asset.sample_overlap_adjustment,
                "priors": {"p1": asset.prior_p1, "p2": asset.prior_p2, "p12": asset.prior_p12},
                "sensitivity_analysis_passed": asset.sensitivity_analysis_passed,
                "sensitivity_artifact_sha256": asset.sensitivity_artifact.sha256,
                "harmonization": harmonization, "aligned_eqtl_beta": aligned_beta,
                "regional_variant_count": len(manifest_rows),
                "regional_variants_harmonized": regional_valid,
                "regional_harmonization_failures": dict(sorted(regional_failures.items())),
                "harmonized_variant_manifest_sha256": asset.harmonized_variants.sha256,
                "context_match_score": context_score, "context_match_detail": context_detail, "formal_score_eligible": not reasons,
                "rejection_reasons": list(dict.fromkeys(reasons)), "causal_status": "not_established",
                "assumptions": [
                    "colocalization posterior depends on supplied priors and regional variant coverage",
                    "shared association signal does not establish the causal gene or variant",
                ],
            })
        output_path = context.run_dir / "genetics" / "colocalization_audit.json"
        _write_json(output_path, audited)
        valid = [row for row in audited if row["formal_score_eligible"]]
        status = ToolStatus.SUCCESS if valid and len(valid) == len(audited) else ToolStatus.PARTIAL
        result = ToolResult(
            tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
            status=status, coverage_status=CoverageStatus.COVERED if status == ToolStatus.SUCCESS else CoverageStatus.PARTIAL,
            context_match_score=max((row["context_match_score"] for row in audited), default=0.0),
            inputs={
                "asset_ids": [asset.asset_id for asset in coloc_assets],
                "genetics_input_audit_tool_run_id": (
                    _latest(context, "genetics_input_audit").tool_run_id
                    if _latest(context, "genetics_input_audit") else None
                ),
                "fine_mapping_tool_run_id": fine.tool_run_id if fine else None,
            },
            outputs={"covered": bool(valid), "colocalizations": audited},
            capability=_capability(["coloc_abf", "coloc_susie"]),
            artifacts=[_artifact(context.run_dir, output_path, "application/json")],
            limitations=[
                "The tool audits precomputed colocalization; it does not recompute the statistical model.",
                "Formal eligibility requires a checksum-bound variant-level regional harmonization manifest.",
            ],
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return ToolExecution(result=result, evidence=[])


class GeneticsCandidateExtractionTool(ScientificTool):
    name = "genetics_candidate_extraction"
    version = "2.2.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="genetics",
        description="Create traceable locus-to-gene hypotheses only from QC-passing statistical evidence.",
        input_types=["ColocalizationResult[]", "CredibleSet[]"],
        output_types=["EvidenceItem[]", "candidate_genes", "locus_to_gene_graph"],
        execution_policy="typed_wrapper", critical=True,
    )

    def run(self, context: ToolContext) -> ToolExecution:
        started, run_id = time.perf_counter(), new_id("tool")
        evidence: list[EvidenceItem] = []
        unresolved_loci = []
        gwas_assets = {
            asset.asset_id: asset for asset in context.task.genetics_inputs if isinstance(asset, GwasSummaryStatsInput)
        }
        for row in sorted(_asset_records(context, "gwas_summary_statistics"), key=lambda item: item["p_value"]):
            if row["p_value"] > context.task.constraints.genetics.gwas_p_value_threshold:
                continue
            asset = gwas_assets[row["asset_id"]]
            unresolved_loci.append(row["locus_id"] or row["position_key"])
            evidence.append(EvidenceItem(
                tool_run_id=run_id, claim_class=ClaimClass.OBSERVED,
                statement=(
                    f"GWAS asset {asset.asset_id} reports an association at {row['position_key']} "
                    f"(P={row['p_value']:.3g}); no gene is assigned by this association alone."
                ),
                source=SourceLocator(
                    uri=f"asset://{asset.asset_id}", source_id=asset.study_id, version=asset.source_version,
                    section="gwas_summary_statistics", chunk_id=f"{asset.asset_id}-row-{row['row_number']}",
                ),
                source_span=(
                    f"position_key={row['position_key']}|EA={row['effect_allele']}|OA={row['other_allele']}|"
                    f"beta={row['beta']}|se={row['standard_error']}|p={row['p_value']}"
                ),
                context=EvidenceContext(
                    organism="Homo sapiens", disease=context.task.context.disease,
                    population=asset.ancestry, ancestry=asset.ancestry, genome_build=asset.genome_build,
                    locus_id=row["locus_id"] or row["position_key"], study_id=asset.study_id, assay="GWAS",
                ),
                stance=Stance.SUPPORTS, effect_direction="unclear",
                effect={
                    "effect_allele": row["effect_allele"], "beta": row["beta"],
                    "standard_error": row["standard_error"], "p_value": row["p_value"],
                },
                uncertainty="A GWAS association does not identify a causal gene or variant.",
                quality_flags=["unresolved_locus", "association_not_causality"], context_match_score=1.0,
                genetic_evidence=GeneticEvidencePayload(
                    evidence_type="gwas_association", analysis_level="association_only",
                    study_id=asset.study_id, locus_id=row["locus_id"] or row["position_key"],
                    variant_id=row["variant_id"] or row["position_key"], strength=0.0,
                    formal_score_eligible=False,
                    assumptions=["No locus-to-gene assignment is made from proximity alone."],
                ),
            ))
            if len(unresolved_loci) >= 20:
                break

        coloc = _latest(context, "eqtl_colocalization_audit")
        coloc_assets = {
            asset.asset_id: asset for asset in context.task.genetics_inputs
            if isinstance(asset, EqtlColocalizationResultInput)
        }
        candidates, links = [], []
        for row in (coloc.outputs.get("colocalizations", []) if coloc else []):
            if not row.get("formal_score_eligible"):
                continue
            gene = row["gene"].upper()
            asset = coloc_assets[row["asset_id"]]
            candidates.append(gene)
            source_span = (
                f"locus={row['locus_id']}|signal={row['signal_id']}|variant={row['position_key']}|gene={gene}|"
                f"PP3={row['pp3']}|PP4={row['pp4']}|regional_variants={row['regional_variant_count']}|"
                f"harmonized_variants={row['regional_variants_harmonized']}|harmonization={row['harmonization']}|"
                f"manifest_sha256={row['harmonized_variant_manifest_sha256']}|"
                f"gwas_study={row['gwas_study_id']}|eqtl_study={row['eqtl_study_id']}|"
                f"priors={row['priors']}|sensitivity_sha256={row['sensitivity_artifact_sha256']}"
            )
            evidence.append(EvidenceItem(
                tool_run_id=run_id, gene_symbol=gene, claim_class=ClaimClass.INFERRED,
                statement=(
                    f"A QC-passing {row['method']} result supports a shared association signal for "
                    f"{gene} at {row['locus_id']}; this is a locus-to-gene hypothesis, not a causal conclusion."
                ),
                source=SourceLocator(
                    uri=f"asset://{row['asset_id']}",
                    source_id=f"{row['gwas_study_id']}|{row['eqtl_study_id']}",
                    version=asset.source_version,
                    section="colocalization_result", chunk_id=f"{row['asset_id']}-row-{row['row_number']}",
                ),
                source_span=source_span,
                context=EvidenceContext(
                    organism="Homo sapiens", disease=context.task.context.disease,
                    tissue=row["tissue"], cell_type=row.get("cell_type"), population=row["ancestry"],
                    ancestry=row["ancestry"], genome_build=row["genome_build"], locus_id=row["locus_id"],
                    study_id=row["study_id"], signal_id=row["signal_id"], assay=row["method"],
                ),
                stance=Stance.SUPPORTS, effect_direction="unclear",
                effect={"aligned_eqtl_beta": row["aligned_eqtl_beta"], "pp3": row["pp3"], "pp4": row["pp4"]},
                uncertainty=(
                    "PP4 is a posterior under the supplied model and priors; shared signal support does not "
                    "establish the causal gene, mechanism, therapeutic direction or clinical success."
                ),
                quality_flags=[
                    "colocalization_supported", "statistical_hypothesis_not_causality",
                    "direction_not_therapeutic_direction",
                ],
                context_match_score=row["context_match_score"],
                genetic_evidence=GeneticEvidencePayload(
                    evidence_type="locus_to_gene", analysis_level="colocalization_supported",
                    study_id=row["study_id"], molecular_study_id=row["eqtl_study_id"],
                    locus_id=row["locus_id"], signal_id=row["signal_id"],
                    variant_id=row["variant_id"] or row["position_key"], gene_symbol=gene,
                    method=row["method"], method_version=row["method_version"],
                    strength=row["pp4"], formal_score_eligible=True, assumptions=row["assumptions"],
                ),
            ))
            links.append({
                "locus_id": row["locus_id"], "signal_id": row["signal_id"],
                "variant_id": row["variant_id"] or row["position_key"],
                "gene": gene, "analysis_level": "colocalization_supported", "pp4": row["pp4"],
                "context_match_score": row["context_match_score"], "causal_status": "not_established",
            })
        candidates = list(dict.fromkeys(candidates))
        mapped_loci = {row["locus_id"] for row in links}
        unresolved_loci = [locus for locus in dict.fromkeys(unresolved_loci) if locus not in mapped_loci]
        graph = {
            "nodes": [
                *[{"id": f"locus:{row['locus_id']}", "type": "locus", "label": row["locus_id"]} for row in links],
                *[{"id": f"gene:{row['gene']}", "type": "gene", "label": row["gene"]} for row in links],
            ],
            "edges": [{
                "source": f"locus:{row['locus_id']}", "target": f"gene:{row['gene']}",
                "type": "shared_signal_support", "causal_status": "not_established",
            } for row in links],
        }
        graph_path = context.run_dir / "genetics" / "locus_to_gene_evidence_graph.json"
        _write_json(graph_path, graph)
        if candidates:
            status = ToolStatus.PARTIAL if unresolved_loci else ToolStatus.SUCCESS
            coverage = CoverageStatus.PARTIAL if unresolved_loci else CoverageStatus.COVERED
            match = max(
                item.context_match_score for item in evidence if item.gene_symbol
            )
        else:
            status, coverage, match = ToolStatus.PARTIAL, CoverageStatus.PARTIAL, 0.0
        result = ToolResult(
            tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
            status=status, coverage_status=coverage, context_match_score=match,
            inputs={
                "colocalization_tool_run_id": coloc.tool_run_id if coloc else None,
                "fine_mapping_tool_run_id": (
                    _latest(context, "fine_mapping_audit").tool_run_id
                    if _latest(context, "fine_mapping_audit") else None
                ),
                "genetics_input_audit_tool_run_id": (
                    _latest(context, "genetics_input_audit").tool_run_id
                    if _latest(context, "genetics_input_audit") else None
                ),
            },
            outputs={
                "covered": bool(candidates), "candidate_genes": candidates,
                "locus_to_gene_links": links, "unresolved_gwas_loci": list(dict.fromkeys(unresolved_loci)),
                "causal_status": "not_established",
            },
            candidate_genes=candidates, capability=_capability(["typed_locus_to_gene_integration"]),
            artifacts=[_artifact(context.run_dir, graph_path, "application/json")],
            evidence_ids=[item.evidence_id for item in evidence],
            limitations=[
                "No nearest-gene assignment is performed.",
                "Precomputed colocalization is audited but not recomputed in this release.",
            ],
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return ToolExecution(result=result, evidence=evidence)
