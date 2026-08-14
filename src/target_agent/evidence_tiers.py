"""Evidence-tier orchestration and cross-layer concordance.

Encodes the mid-term review policy (configs/evidence_tiers.yaml): discovery anchors on
the highest-quality, largest-cohort tiers (human genetics, bulk disease omics); every
downstream tier cross-validates or refines context; no single layer gates entry (a
missing genetics layer degrades to completed_with_gaps, never stops the run), and
single-cell data refines context instead of anchoring discovery because its patient
cohorts are not comparable in scale to population-level data.

Attribution: the layered view is inspired by gsMAP's cross-scale integration of GWAS
with spatial/cellular context (GWAS x spatial transcriptomics, Nature 2025); gsMAP does
not propose a general tier ranking, and the tier structure here is our own engineering
hypothesis to be challenged by ablation, not borrowed weights.

The matrix produced here is descriptive: it reports, per gene, which independent tiers
support / oppose / miss, so layered evidence is visibly complementary rather than fused
into an opaque order. Ranking scores stay owned by ranking.py and the six-dimensional
ScoreBreakdown contract; these tiers add no second weight system.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .contracts import ClaimClass, EvidenceItem, Stance, ToolResult

CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "evidence_tiers.yaml"

TIER_HUMAN_GENETICS = "human_genetics"
TIER_OMICS_BULK = "disease_omics_bulk"
TIER_PERTURB_MEASURED = "perturbation_measured"
TIER_SINGLE_CELL = "single_cell_context"
TIER_PERTURB_PREDICTED = "perturbation_predicted"
TIER_LITERATURE = "literature_mechanism"
TIER_CLINICAL = "clinical_translation"

SINGLE_CELL_TOOLS = {"single_cell_analysis", "cellxgene_census"}
CLINICAL_TOOLS = {"clinicaltrials", "clinical_trials"}
LITERATURE_HOSTS = ("europepmc", "europe_pmc", "pubmed", "pmc")


@dataclass(frozen=True)
class Tier:
    id: str
    title: str
    quality_rank: int
    role: str
    ranking_dimension: str | None
    note: str = ""


@dataclass(frozen=True)
class EvidenceTierPolicy:
    tiers: tuple[Tier, ...]
    orchestration: dict[str, Any] = field(default_factory=dict)

    @property
    def tier_ids(self) -> list[str]:
        return [tier.id for tier in self.tiers]

    def orchestration_order(self) -> list[str]:
        """Tier ids in discovery order (highest-quality first)."""
        return [tier.id for tier in sorted(self.tiers, key=lambda t: t.quality_rank)]

    def tier(self, tier_id: str) -> Tier | None:
        return next((tier for tier in self.tiers if tier.id == tier_id), None)


@lru_cache(maxsize=4)
def load_tiers(path: str | None = None) -> EvidenceTierPolicy:
    config = Path(path) if path else CONFIG_PATH
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    tiers = tuple(
        Tier(
            id=row["id"], title=row["title"], quality_rank=int(row["quality_rank"]),
            role=row["role"], ranking_dimension=row.get("ranking_dimension"),
            note=str(row.get("note", "")).strip(),
        )
        for row in raw["tiers"]
    )
    return EvidenceTierPolicy(tiers=tiers, orchestration=raw.get("orchestration") or {})


def tier_for_item(item: EvidenceItem, tool_names: dict[str, str] | None = None) -> str | None:
    """Classify one evidence item into a tier; None when the item carries no tier signal."""
    effect = item.effect or {}
    tool_name = (tool_names or {}).get(item.tool_run_id, "")
    if tool_name in SINGLE_CELL_TOOLS:
        return TIER_SINGLE_CELL
    if "safety" in effect or tool_name in CLINICAL_TOOLS:
        return TIER_CLINICAL
    if item.genetic_evidence is not None or "genetic_score" in effect:
        return TIER_HUMAN_GENETICS
    if "legacy_disease_strength_0_60" in effect or "omics_strength" in effect:
        return TIER_OMICS_BULK
    if "disease_alignment" in effect:
        return TIER_PERTURB_MEASURED if item.claim_class == ClaimClass.OBSERVED else TIER_PERTURB_PREDICTED
    uri = (item.source.uri or "").lower()
    if any(host in uri for host in LITERATURE_HOSTS):
        return TIER_LITERATURE
    if item.claim_class == ClaimClass.PREDICTED:
        return TIER_PERTURB_PREDICTED
    return None


def concordance_matrix(
    genes: list[str],
    evidence: list[EvidenceItem],
    results: list[ToolResult] | None = None,
    policy: EvidenceTierPolicy | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Per gene x tier coverage: supports / opposes / missing plus evidence ids.

    An item opposes when its stance is REFUTES or MIXED; otherwise it supports.
    """
    policy = policy or load_tiers()
    tool_names = {result.tool_run_id: result.tool_name for result in (results or [])}
    matrix: dict[str, dict[str, dict[str, Any]]] = {}
    for gene in genes:
        row: dict[str, dict[str, Any]] = {}
        for tier in policy.tiers:
            row[tier.id] = {"status": "missing", "evidence_ids": []}
        for item in evidence:
            if item.gene_symbol != gene:
                continue
            tier_id = tier_for_item(item, tool_names)
            if tier_id is None or tier_id not in row:
                continue
            cell = row[tier_id]
            cell["evidence_ids"].append(item.evidence_id)
            if item.stance in {Stance.REFUTES, Stance.MIXED}:
                cell["status"] = "opposes"
            elif cell["status"] == "missing":
                cell["status"] = "supports"
        matrix[gene] = row
    return matrix


def concordance_score_row(row: dict[str, dict[str, Any]], policy: EvidenceTierPolicy | None = None) -> dict[str, int]:
    """Compact per-gene tally used by reports and tests."""
    policy = policy or load_tiers()
    anchor = sum(1 for tier in policy.tiers if tier.role == "anchor" and row[tier.id]["status"] == "supports")
    validation = sum(1 for tier in policy.tiers if tier.role == "validation" and row[tier.id]["status"] == "supports")
    opposed = sum(1 for tier in policy.tiers if row[tier.id]["status"] == "opposes")
    return {"anchor_tiers": anchor, "validation_tiers": validation, "opposed_tiers": opposed}


def format_report_section(
    matrix: dict[str, dict[str, dict[str, Any]]],
    genes: list[str],
    policy: EvidenceTierPolicy | None = None,
    max_genes: int = 5,
) -> list[str]:
    """Markdown lines for the disease report's evidence-tier concordance section."""
    policy = policy or load_tiers()
    ordered = policy.orchestration_order()
    short = [policy.tier(t).title.split("（")[0].split("(")[0].split(",")[0] for t in ordered]
    lines = [
        "", "## 证据分层一致性", "",
        "发现从最高质量、最大队列的证据层起步（人类遗传 → 群体组学），下游各层用于交叉验证与上下文细化；"
        "单层证据（含单细胞）不单独决定发现顺序，预测级证据不替代实测层。",
        "",
        "| 靶点 | " + " | ".join(short) + " | 锚定/验证/冲突 |",
        "|---|" + "---:|" * (len(ordered) + 1),
    ]
    marks = {"supports": "✓", "opposes": "✗", "missing": "—"}
    for gene in genes[:max_genes]:
        row = matrix.get(gene, {})
        cells = [marks.get(row.get(t, {}).get("status", "missing"), "—") for t in ordered]
        tally = concordance_score_row(row, policy) if row else {"anchor_tiers": 0, "validation_tiers": 0, "opposed_tiers": 0}
        lines.append(
            f"| {gene} | " + " | ".join(cells)
            + f" | {tally['anchor_tiers']}/{tally['validation_tiers']}/{tally['opposed_tiers']} |"
        )
    return lines
