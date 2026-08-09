"""Versioned public contracts for every TargetDiscovery Agent boundary."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


CONTRACT_VERSION = "2.2.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    contract_version: Literal["2.2.0"] = CONTRACT_VERSION


class ClaimClass(str, Enum):
    FACT = "FACT"
    OBSERVED = "OBSERVED"
    PREDICTED = "PREDICTED"
    UNVERIFIED = "UNVERIFIED"
    INFERRED = "INFERRED"


class Stance(str, Enum):
    SUPPORTS = "supports"
    REFUTES = "refutes"
    MIXED = "mixed"
    UNCERTAIN = "uncertain"


class ToolStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    OUT_OF_SCOPE = "out_of_scope"


class CoverageStatus(str, Enum):
    COVERED = "covered"
    PARTIAL = "partial"
    NOT_COVERED = "not_covered"
    UNKNOWN = "unknown"


class TerminalStatus(str, Enum):
    COMPLETED = "completed"
    COMPLETED_WITH_GAPS = "completed_with_gaps"
    NEEDS_INPUT = "needs_input"
    REFUSED = "refused"
    FAILED = "failed"


class TaskContext(ContractModel):
    disease: str | None = None
    disease_id: str | None = None
    disease_subtype: str | None = None
    organism: str = "Homo sapiens"
    population: str | None = None
    tissue: str | None = None
    cell_type: str | None = None
    disease_stage: str | None = None
    desired_phenotype: str | None = None
    assay: str | None = None
    perturbation_type: str | None = None
    genome_build: Literal["GRCh37", "GRCh38"] | None = None
    ancestry: str | None = None
    locus_id: str | None = None
    study_id: str | None = None


class OmicsInput(ContractModel):
    """A user-selected, local omics input. Arbitrary remote URLs are not executed."""

    uri: str
    data_kind: Literal["h5ad", "10x_mtx", "10x_h5"]
    metadata_uri: str | None = None
    cell_type_key: str = "cell_type"
    donor_key: str = "donor_id"
    condition_key: str = "condition"
    counts_layer: str = "counts"


class GeneticsAssetBase(ContractModel):
    """Reference to a pre-staged, checksum-bound genetics file.

    `relative_path` is resolved under the deployment's controlled input root;
    remote URLs and absolute paths are intentionally not executable inputs.
    """

    asset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_format: Literal["tsv", "csv", "tsv.gz"]
    genome_build: Literal["GRCh37", "GRCh38"]
    study_id: str = Field(min_length=1)
    phenotype: str = Field(min_length=1)
    phenotype_id: str | None = None
    ancestry: str = Field(min_length=1)
    sample_size: int = Field(gt=0)
    source_uri: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    public_data: bool = True

    @model_validator(mode="after")
    def safe_relative_path(self) -> "GeneticsAssetBase":
        from pathlib import PurePosixPath
        path = PurePosixPath(self.relative_path.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("genetics relative_path must stay inside the controlled input root")
        return self


class GwasColumnMap(ContractModel):
    chromosome: str
    position: str
    effect_allele: str
    other_allele: str
    effect: str
    standard_error: str
    p_value: str
    effect_allele_frequency: str | None = None
    variant_id: str | None = None
    locus_id: str | None = None


class GwasSummaryStatsInput(GeneticsAssetBase):
    kind: Literal["gwas_summary_statistics"] = "gwas_summary_statistics"
    effect_scale: Literal["beta", "log_odds", "odds_ratio"]
    columns: GwasColumnMap


class LDReferenceSpec(ContractModel):
    reference_id: str = Field(min_length=1)
    ancestry: str = Field(min_length=1)
    genome_build: Literal["GRCh37", "GRCh38"]
    source_uri: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sample_size: int = Field(gt=0)
    matched_to_study: bool = False


class FineMappingColumnMap(ContractModel):
    chromosome: str
    position: str
    effect_allele: str
    other_allele: str
    signal_posterior: str
    credible_set_id: str
    locus_id: str
    variant_id: str | None = None


class FineMappingResultInput(GeneticsAssetBase):
    kind: Literal["fine_mapping_result"] = "fine_mapping_result"
    method: Literal["susie"]
    method_version: str = Field(min_length=1)
    posterior_kind: Literal["signal_posterior"] = "signal_posterior"
    credible_level: float = Field(default=0.95, gt=0.5, lt=1.0)
    ld_reference: LDReferenceSpec | None = None
    columns: FineMappingColumnMap


class ColocResultColumnMap(ContractModel):
    gene: str
    locus_id: str
    signal_id: str
    chromosome: str
    position: str
    gwas_effect_allele: str
    gwas_other_allele: str
    eqtl_effect_allele: str
    eqtl_other_allele: str
    eqtl_beta: str
    pp0: str
    pp1: str
    pp2: str
    pp3: str
    pp4: str
    n_variants: str
    variant_id: str | None = None


class HarmonizedVariantColumnMap(ContractModel):
    gene: str
    locus_id: str
    signal_id: str
    chromosome: str
    position: str
    gwas_effect_allele: str
    gwas_other_allele: str
    eqtl_effect_allele: str
    eqtl_other_allele: str
    variant_id: str | None = None


class HarmonizedVariantManifest(ContractModel):
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_format: Literal["tsv", "csv", "tsv.gz"]
    columns: HarmonizedVariantColumnMap

    @model_validator(mode="after")
    def safe_relative_path(self) -> "HarmonizedVariantManifest":
        from pathlib import PurePosixPath
        path = PurePosixPath(self.relative_path.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("harmonized variant relative_path must stay inside the controlled input root")
        return self


class AnalysisEvidenceArtifact(ContractModel):
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: Literal["application/json", "text/tab-separated-values", "text/plain"]

    @model_validator(mode="after")
    def safe_relative_path(self) -> "AnalysisEvidenceArtifact":
        from pathlib import PurePosixPath
        path = PurePosixPath(self.relative_path.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("analysis evidence relative_path must stay inside the controlled input root")
        return self


class EqtlColocalizationResultInput(GeneticsAssetBase):
    kind: Literal["eqtl_colocalization_result"] = "eqtl_colocalization_result"
    method: Literal["coloc_abf", "coloc_susie"]
    method_version: str = Field(min_length=1)
    gwas_study_id: str = Field(min_length=1)
    eqtl_study_id: str = Field(min_length=1)
    eqtl_ancestry: str = Field(min_length=1)
    tissue: str = Field(min_length=1)
    cell_type: str | None = None
    molecular_trait: str = "gene_expression"
    minimum_variant_overlap_used: int = Field(ge=1)
    prior_p1: float = Field(gt=0, lt=1)
    prior_p2: float = Field(gt=0, lt=1)
    prior_p12: float = Field(gt=0, lt=1)
    sensitivity_analysis_passed: bool
    sample_overlap: Literal["none", "known", "unknown"]
    sample_overlap_adjustment: str | None = None
    columns: ColocResultColumnMap
    harmonized_variants: HarmonizedVariantManifest
    sensitivity_artifact: AnalysisEvidenceArtifact

    @model_validator(mode="after")
    def require_overlap_accounting(self) -> "EqtlColocalizationResultInput":
        if self.sample_overlap == "known" and not self.sample_overlap_adjustment:
            raise ValueError("known sample overlap requires a declared adjustment method")
        return self


GeneticsInput = Annotated[
    GwasSummaryStatsInput | FineMappingResultInput | EqtlColocalizationResultInput,
    Field(discriminator="kind"),
]


class GeneticsAnalysisConstraints(ContractModel):
    max_file_size_mb: int = Field(default=250, ge=1, le=2048)
    max_rows_per_asset: int = Field(default=250_000, ge=1_000, le=2_000_000)
    gwas_p_value_threshold: float = Field(default=5e-8, gt=0, le=1)
    credible_set_level: float = Field(default=0.95, gt=0.5, lt=1.0)
    credible_set_sum_tolerance: float = Field(default=0.05, gt=0, le=0.2)
    minimum_coloc_variant_overlap: int = Field(default=50, ge=10, le=100_000)
    minimum_coloc_pp4: float = Field(default=0.8, ge=0.5, le=1.0)
    require_coloc_sensitivity: bool = True
    reject_palindromic_without_frequency: bool = True


class DatasetSelectionConstraint(ContractModel):
    preferred_dataset_accessions: list[str] = Field(default_factory=list)
    excluded_dataset_accessions: list[str] = Field(default_factory=list)
    omics_modes: list[Literal["geo_bulk", "cellxgene", "local_single_cell"]] = Field(
        default_factory=lambda: ["geo_bulk", "cellxgene"]
    )
    max_geo_candidates: int = Field(default=10, ge=1, le=20)
    max_datasets_to_analyze: int = Field(default=2, ge=1, le=2)
    max_download_mb: int = Field(default=2048, ge=1, le=10240)
    max_cells: int = Field(default=100_000, ge=100, le=1_000_000)
    min_biological_replicates_per_group: int = Field(default=3, ge=3, le=20)
    min_metadata_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    allow_raw_sra: bool = False


class TaskConstraints(ContractModel):
    public_data_only: bool = True
    druggable_only: bool = False
    max_initial_candidates: int = Field(default=20, ge=1, le=20)
    max_ranked_targets: int = Field(default=10, ge=1, le=50)
    max_target_cards: int = Field(default=5, ge=1, le=10)
    max_review_rounds: int = Field(default=2, ge=0, le=2)
    max_tool_calls: int = Field(default=30, ge=1, le=30)
    dataset_selection: DatasetSelectionConstraint = Field(default_factory=DatasetSelectionConstraint)
    genetics: GeneticsAnalysisConstraints = Field(default_factory=GeneticsAnalysisConstraints)


class TaskSpec(ContractModel):
    task_id: str = Field(default_factory=lambda: new_id("task"))
    task_type: Literal["disease_to_target", "gwas_locus_to_target", "trait_mechanism"]
    question: str = Field(min_length=3)
    context: TaskContext
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    candidate_genes: list[str] = Field(default_factory=list)
    omics_inputs: list[OmicsInput] = Field(default_factory=list)
    genetics_inputs: list[GeneticsInput] = Field(default_factory=list)
    requested_outputs: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_scope(self) -> "TaskSpec":
        if self.task_type == "disease_to_target" and not self.context.disease:
            raise ValueError("disease_to_target requires context.disease")
        if self.task_type == "gwas_locus_to_target" and not self.context.disease:
            raise ValueError("gwas_locus_to_target requires context.disease")
        if self.task_type == "gwas_locus_to_target" and not self.genetics_inputs:
            raise ValueError("gwas_locus_to_target requires genetics_inputs")
        if self.task_type == "trait_mechanism" and not self.context.desired_phenotype:
            raise ValueError("trait_mechanism requires context.desired_phenotype")
        asset_ids = [asset.asset_id for asset in self.genetics_inputs]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("genetics asset_id values must be unique")
        if self.constraints.public_data_only and any(not asset.public_data for asset in self.genetics_inputs):
            raise ValueError("public_data_only tasks cannot use non-public genetics assets")
        if self.genetics_inputs:
            gwas_assets = [asset for asset in self.genetics_inputs if isinstance(asset, GwasSummaryStatsInput)]
            if not gwas_assets:
                raise ValueError("genetics_inputs requires at least one GWAS summary-statistics asset")
            gwas_studies = {asset.study_id for asset in gwas_assets}
            gwas_by_study = {asset.study_id: asset for asset in gwas_assets}
            if len(gwas_studies) != len(gwas_assets):
                raise ValueError("each supplied GWAS asset must have a unique study_id")
            if self.context.organism.casefold() not in {"homo sapiens", "human"}:
                raise ValueError("the genetics workflow currently supports Homo sapiens only")
            if self.context.study_id and self.context.study_id not in gwas_studies:
                raise ValueError("requested study_id must reference a supplied GWAS study")
            for asset in self.genetics_inputs:
                if self.context.genome_build and asset.genome_build != self.context.genome_build:
                    raise ValueError("genetics asset genome build must match the requested context")
                if self.context.ancestry and asset.ancestry.casefold() != self.context.ancestry.casefold():
                    raise ValueError("genetics asset ancestry must match the requested context")
                if self.context.disease_id and asset.phenotype_id != self.context.disease_id:
                    raise ValueError("genetics asset phenotype_id must match the requested disease_id")
                if not self.context.disease_id and self.context.disease and (
                    "".join(character for character in asset.phenotype.casefold() if character.isalnum())
                    != "".join(character for character in self.context.disease.casefold() if character.isalnum())
                ):
                    raise ValueError("genetics asset phenotype must match the requested disease")
                if isinstance(asset, FineMappingResultInput):
                    if asset.study_id not in gwas_studies:
                        raise ValueError("fine-mapping study_id must reference a supplied GWAS study")
                    gwas = gwas_by_study[asset.study_id]
                    if (
                        asset.genome_build != gwas.genome_build
                        or asset.ancestry.casefold() != gwas.ancestry.casefold()
                        or asset.sample_size != gwas.sample_size
                    ):
                        raise ValueError(
                            "fine-mapping build, ancestry and sample size must match its GWAS study"
                        )
                if isinstance(asset, EqtlColocalizationResultInput):
                    if asset.gwas_study_id not in gwas_studies:
                        raise ValueError("colocalization gwas_study_id must reference a supplied GWAS study")
                    gwas = gwas_by_study[asset.gwas_study_id]
                    if (
                        asset.genome_build != gwas.genome_build
                        or asset.ancestry.casefold() != gwas.ancestry.casefold()
                    ):
                        raise ValueError(
                            "colocalization build and ancestry must match its GWAS study"
                        )
                    if asset.study_id != asset.eqtl_study_id:
                        raise ValueError("colocalization study_id must identify the supplied eQTL study")
                    if asset.eqtl_ancestry.casefold() != asset.ancestry.casefold():
                        raise ValueError("GWAS and eQTL ancestry must match in the current formal workflow")
        return self


class PlanStep(ContractModel):
    step_id: str
    name: str
    tool: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    input_refs: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    degradation_conditions: list[str] = Field(default_factory=list)
    # A candidate-bound step consumes the current candidate universe; its
    # persisted result is only reusable for the same step_id + candidate digest.
    candidate_bound: bool = False
    evidence_lane: Literal["literature", "genetics", "drug_safety", "perturbation", "trials"] | None = None

    @model_validator(mode="after")
    def bind_evidence_lane(self) -> "PlanStep":
        if self.evidence_lane is not None and not self.candidate_bound:
            raise ValueError("evidence_lane requires candidate_bound=True")
        return self


class ExecutionPlan(ContractModel):
    plan_id: str = Field(default_factory=lambda: new_id("plan"))
    task_id: str
    planner_backend: str
    steps: list[PlanStep]
    fallback_used: bool = False
    created_at: str = Field(default_factory=utc_now)


class SourceLocator(ContractModel):
    uri: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    version: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    version_meta: dict[str, Any] | None = None
    section: str | None = None
    chunk_id: str | None = None
    start_char: int | None = Field(default=None, ge=0)
    end_char: int | None = Field(default=None, ge=0)


class EvidenceContext(ContractModel):
    organism: str | None = None
    population: str | None = None
    tissue: str | None = None
    cell_type: str | None = None
    disease: str | None = None
    disease_stage: str | None = None
    assay: str | None = None
    perturbation_type: str | None = None
    genome_build: Literal["GRCh37", "GRCh38"] | None = None
    ancestry: str | None = None
    locus_id: str | None = None
    study_id: str | None = None
    signal_id: str | None = None


class GeneticEvidencePayload(ContractModel):
    evidence_type: Literal[
        "gwas_association", "fine_mapping", "colocalization", "locus_to_gene",
        "open_targets_genetic_association",
    ]
    analysis_level: Literal[
        "association_only", "fine_mapped", "colocalization_supported", "database_aggregate",
    ]
    study_id: str
    molecular_study_id: str | None = None
    locus_id: str | None = None
    variant_id: str | None = None
    signal_id: str | None = None
    gene_symbol: str | None = None
    method: str | None = None
    method_version: str | None = None
    strength: float = Field(ge=0.0, le=1.0)
    formal_score_eligible: bool
    causal_status: Literal["not_established"] = "not_established"
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def enforce_formal_scoring_boundary(self) -> "GeneticEvidencePayload":
        required_level = {
            "gwas_association": "association_only",
            "fine_mapping": "fine_mapped",
            "colocalization": "colocalization_supported",
            "locus_to_gene": "colocalization_supported",
            "open_targets_genetic_association": "database_aggregate",
        }[self.evidence_type]
        if self.analysis_level != required_level:
            raise ValueError(
                f"{self.evidence_type} evidence requires analysis_level={required_level}"
            )
        if self.evidence_type == "open_targets_genetic_association":
            if self.formal_score_eligible:
                raise ValueError("Open Targets aggregate evidence cannot be formally scored as locus genetics")
        if self.analysis_level in {"association_only", "database_aggregate"} and self.formal_score_eligible:
            raise ValueError("association-only and database-aggregate evidence cannot enter formal genetics scoring")
        if self.formal_score_eligible and self.evidence_type not in {"colocalization", "locus_to_gene"}:
            raise ValueError("only audited colocalization or locus-to-gene evidence is formally score eligible")
        if self.formal_score_eligible and self.strength <= 0:
            raise ValueError("formal genetic evidence requires positive statistical support")
        if self.formal_score_eligible and (
            not self.gene_symbol or not self.locus_id or not self.signal_id
            or not self.molecular_study_id or not self.method or not self.method_version
            or self.analysis_level != "colocalization_supported"
        ):
            raise ValueError("formal genetic evidence requires an identified gene, locus, signal and audited method")
        return self


class EvidenceItem(ContractModel):
    evidence_id: str = Field(default_factory=lambda: new_id("ev"))
    tool_run_id: str = Field(min_length=1)
    gene_symbol: str | None = None
    claim_class: ClaimClass
    statement: str = Field(min_length=1)
    source: SourceLocator
    source_span: str = Field(min_length=1)
    context: EvidenceContext
    stance: Stance = Stance.UNCERTAIN
    effect_direction: Literal["increase", "decrease", "mixed", "unclear"] = "unclear"
    effect: dict[str, Any] = Field(default_factory=dict)
    uncertainty: str = Field(min_length=1)
    quality_flags: list[str] = Field(default_factory=list)
    context_match_score: float = Field(ge=0.0, le=1.0)
    # Tool-supplied match evidence (matched disease/tissue/cell/assay terms and
    # source fields). This is never the final score: formal ranking and the
    # reviewer recompute the score deterministically from this metadata.
    context_match: dict[str, Any] | None = None
    genetic_evidence: GeneticEvidencePayload | None = None

    @model_validator(mode="after")
    def enforce_genetic_context_consistency(self) -> "EvidenceItem":
        genetic = self.genetic_evidence
        if not genetic or not genetic.formal_score_eligible:
            return self
        if not self.gene_symbol or self.gene_symbol != genetic.gene_symbol:
            raise ValueError("formal genetic payload gene must match EvidenceItem.gene_symbol")
        if (
            self.context.study_id != genetic.study_id
            or self.context.locus_id != genetic.locus_id
            or self.context.signal_id != genetic.signal_id
        ):
            raise ValueError("formal genetic payload study/locus/signal must match EvidenceContext")
        if not self.context.genome_build or not self.context.ancestry:
            raise ValueError("formal genetic evidence requires genome build and ancestry context")
        if self.stance != Stance.SUPPORTS:
            raise ValueError("formal locus-to-gene evidence must have a supporting stance")
        if self.context_match_score < 0.5:
            raise ValueError("low-context genetic evidence cannot be marked formal score eligible")
        return self


class ToolCapability(ContractModel):
    supported_organisms: list[str] = Field(default_factory=list)
    supported_tissues: list[str] = Field(default_factory=list)
    supported_cell_types: list[str] = Field(default_factory=list)
    supported_perturbations: list[str] = Field(default_factory=list)
    training_scope: str | None = None
    validation_scope: str | None = None
    supported_genome_builds: list[Literal["GRCh37", "GRCh38"]] = Field(default_factory=list)
    supported_ancestries: list[str] = Field(default_factory=list)
    supported_methods: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class SkillRef(ContractModel):
    name: str
    upstream_version: str
    upstream_commit: str
    source_uri: str
    sha256: str | None = None
    adaptation_note: str | None = None


class ToolDescriptor(ContractModel):
    tool_id: str
    evidence_dimension: Literal[
        "scope", "dataset_discovery", "omics", "genetics", "literature",
        "perturbation", "pathway", "drug", "multi_evidence", "causal_gold",
    ]
    description: str
    input_types: list[str] = Field(default_factory=list)
    output_types: list[str] = Field(default_factory=list)
    critical: bool = False
    enabled: bool = True
    execution_policy: Literal["typed_wrapper", "fixed_script", "read_only_connector"] = "typed_wrapper"
    skills: list[SkillRef] = Field(default_factory=list)


class DatasetCandidate(ContractModel):
    accession: str
    source: Literal["GEO", "CELLxGENE"]
    title: str
    organism: str | None = None
    disease: str | None = None
    tissue: str | None = None
    cell_type: str | None = None
    assay: str | None = None
    sample_count: int | None = Field(default=None, ge=0)
    case_count: int | None = Field(default=None, ge=0)
    control_count: int | None = Field(default=None, ge=0)
    processed_files: list[str] = Field(default_factory=list)
    metadata_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    context_match_score: float = Field(default=0.0, ge=0.0, le=1.0)
    eligibility: Literal["eligible", "ineligible", "needs_confirmation"] = "needs_confirmation"
    exclusion_reasons: list[str] = Field(default_factory=list)
    source_uri: str
    source_version: str | None = None
    retrieved_at: str = Field(default_factory=utc_now)


class AnalysisRecipe(ContractModel):
    recipe_id: str = Field(default_factory=lambda: new_id("recipe"))
    accession: str
    data_kind: Literal[
        "bulk_counts", "bulk_continuous_expression", "single_cell_census",
        "single_cell_h5ad", "single_cell_10x",
    ]
    backend: Literal["pydeseq2", "limma", "scanpy_pseudobulk"]
    input_uri: str
    group_mapping: dict[str, str] = Field(default_factory=dict)
    design: str
    contrast: list[str]
    qc_thresholds: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    skill_refs: list[SkillRef] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    degradation_conditions: list[str] = Field(default_factory=list)


class OmicsResult(ContractModel):
    accession: str
    backend: str
    status: ToolStatus
    coverage_status: CoverageStatus
    qc_summary: dict[str, Any] = Field(default_factory=dict)
    candidate_genes: list[str] = Field(default_factory=list)
    differential_result_artifact: ArtifactRef | None = None
    pathway_result_artifact: ArtifactRef | None = None
    tested_gene_background: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ArtifactRef(ContractModel):
    name: str
    uri: str
    sha256: str | None = None
    media_type: str | None = None


class ToolResult(ContractModel):
    tool_run_id: str = Field(default_factory=lambda: new_id("tool"))
    tool_name: str
    tool_version: str
    status: ToolStatus
    coverage_status: CoverageStatus
    context_match_score: float = Field(ge=0.0, le=1.0)
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    candidate_genes: list[str] = Field(default_factory=list)
    capability: ToolCapability
    data_version: str | None = None
    code_version: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    error: str | None = None
    cached: bool = False
    # Set when a candidate-bound evidence step is re-executed because the
    # candidate universe changed; consumers must treat the superseded result
    # and its evidence as inactive for review, ranking and reporting.
    supersedes_tool_run_id: str | None = Field(default=None, pattern=r"^tool-[a-f0-9]{12}$")
    started_at: str = Field(default_factory=utc_now)
    elapsed_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_status(self) -> "ToolResult":
        if self.coverage_status == CoverageStatus.NOT_COVERED and self.status == ToolStatus.SUCCESS:
            raise ValueError("not_covered cannot be reported as success")
        if self.status == ToolStatus.FAILED and not self.error:
            raise ValueError("failed tool result requires error")
        return self


class Claim(ContractModel):
    claim_id: str = Field(default_factory=lambda: new_id("claim"))
    claim_class: ClaimClass
    statement: str
    evidence_ids: list[str] = Field(min_length=1)
    synthesis_rationale: str | None = None


class ReviewerFinding(ContractModel):
    finding_id: str = Field(default_factory=lambda: new_id("finding"))
    severity: Literal["blocking", "major", "minor"]
    category: Literal[
        "missing_provenance", "context_mismatch", "causal_overreach",
        "conflicting_evidence", "numeric_error", "coverage_gap", "tool_failure",
        "dataset_ineligibility",
        "allele_harmonization", "genome_build_mismatch", "ancestry_mismatch",
        "fine_mapping_invalid", "colocalization_invalid", "gene_mapping_overreach",
        "duplicate_genetic_study",
    ]
    message: str
    related_ids: list[str] = Field(default_factory=list)
    required_action: str
    resolved: bool = False


class GraphNode(ContractModel):
    node_id: str
    node_type: Literal["gene", "variant", "locus", "program", "trait", "disease", "cell_state", "drug", "lane", "strategy_paper"]
    label: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(ContractModel):
    source: str
    target: str
    relation: str
    evidence_ids: list[str] = Field(default_factory=list)
    claim_class: ClaimClass
    weight: float | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class CausalGraph(ContractModel):
    graph_id: str = Field(default_factory=lambda: new_id("graph"))
    graph_kind: Literal["causal_model", "mechanistic_evidence"]
    context: EvidenceContext
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    model_statistics: dict[str, Any] = Field(default_factory=dict)
    source_artifacts: list[ArtifactRef] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ExperimentOutcome(ContractModel):
    outcome: Literal["positive", "negative", "contradictory"]
    observation: str
    conclusion: str
    next_action: str


class ExperimentPlan(ContractModel):
    hypothesis: str
    model_system: str
    intervention: str
    direction: Literal["activate", "inhibit", "knockout", "overexpress", "compare"]
    controls: list[str]
    primary_endpoints: list[str]
    secondary_endpoints: list[str]
    replication_and_power: str
    outcomes: list[ExperimentOutcome]
    highest_information_next_experiment: str
    stop_conditions: list[str]


class ScoreBreakdown(ContractModel):
    human_genetics: float = Field(ge=0, le=25)
    disease_omics: float = Field(ge=0, le=20)
    perturbation: float = Field(ge=0, le=20)
    mechanism: float = Field(ge=0, le=15)
    druggability: float = Field(ge=0, le=10)
    safety_translation: float = Field(ge=0, le=10)
    total: float = Field(ge=0, le=100)


class TargetGeneticEvidenceSummary(ContractModel):
    evidence_id: str
    study_id: str
    molecular_study_id: str
    locus_id: str
    signal_id: str
    method: str
    method_version: str
    strength: float = Field(ge=0.0, le=1.0)
    genome_build: Literal["GRCh37", "GRCh38"]
    ancestry: str
    tissue: str | None = None
    interpretation: Literal["shared_association_signal_not_causality"] = "shared_association_signal_not_causality"


class TargetCard(ContractModel):
    target_card_id: str = Field(default_factory=lambda: new_id("card"))
    gene_symbol: str
    rank: int = Field(ge=1)
    decision: Literal["GO", "CONDITIONAL_GO", "NO_GO", "INSUFFICIENT_EVIDENCE"]
    scores: ScoreBreakdown
    evidence_ids: list[str]
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    opposing_evidence_ids: list[str] = Field(default_factory=list)
    safety_blockers: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    matched_drugs: list[dict[str, Any]] = Field(default_factory=list)
    genetic_evidence_summary: list[TargetGeneticEvidenceSummary] = Field(default_factory=list)
    experiment_plan: ExperimentPlan
    limitations: list[str] = Field(default_factory=list)


class TraceEvent(ContractModel):
    event_id: str = Field(default_factory=lambda: new_id("trace"))
    run_id: str
    task_id: str
    event_type: Literal[
        "state_transition", "plan", "tool_call", "tool_result", "review",
        "replan", "checkpoint", "ranking", "report", "degradation", "refusal",
        "evidence_superseded", "planner_pattern_hints", "planner_paper_evidence",
    ]
    state: str
    detail: dict[str, Any] = Field(default_factory=dict)
    related_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)


class CaseRecord(ContractModel):
    case_id: str = Field(default_factory=lambda: new_id("case"))
    run_id: str
    task_spec: TaskSpec
    plan: ExecutionPlan
    tool_run_ids: list[str]
    finding_ids: list[str]
    revision_history: list[dict[str, Any]]
    final_status: TerminalStatus
    final_claim_ids: list[str]
    scientific_review: Literal["pending", "approved", "rejected"] = "pending"
    promotion_eligible: bool = False
    created_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def prevent_unsafe_promotion(self) -> "CaseRecord":
        if self.promotion_eligible and self.scientific_review != "approved":
            raise ValueError("only scientifically approved cases are promotion eligible")
        return self
