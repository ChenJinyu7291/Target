"""Deterministic, tool-independent context scoring.

Tools submit *match evidence* (matched disease/tissue/cell/assay terms and
source fields) and may keep their old ``context_match_score`` field only as a
self-reported estimate (``context_score_origin="tool_estimate"``). Formal
ranking and the reviewer always recompute the score with this module; a
missing disease match hard-caps the total below the 0.5 formal gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .contracts import EvidenceContext, TaskContext


DISEASE_WEIGHT = 0.40
TISSUE_WEIGHT = 0.20
CELL_WEIGHT = 0.15
ASSAY_WEIGHT = 0.25
# Dimensions the task did not constrain are neutral; a matched dimension is
# 1.0 and a present-but-unmatched dimension is 0.0.
NEUTRAL = 1.0
# A disease miss must never let other dimensions carry the total across the
# 0.5 formal ranking gate.
DISEASE_MISS_CAP = 0.40


@dataclass
class ContextScore:
    score: float
    disease: float
    tissue: float
    cell: float
    assay: float
    hits: dict[str, list[str]]
    notes: list[str]
    origin: str = "recomputed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "disease": self.disease,
            "tissue": self.tissue,
            "cell": self.cell,
            "assay": self.assay,
            "hits": self.hits,
            "notes": self.notes,
            "origin": self.origin,
        }


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _dimension(task_value: str | None, evidence_values: list[str]) -> tuple[float, list[str]]:
    """Score one context dimension; returns (score, hit terms)."""
    if not task_value:
        return NEUTRAL, []
    needle = _norm(task_value)
    hits = [
        value for value in evidence_values
        if value and needle and (needle in _norm(value) or _norm(value) in needle)
    ]
    return (1.0, hits) if hits else (0.0, [])


def _meta_list(meta: dict[str, Any] | None, key: str) -> list[str]:
    if not meta:
        return []
    value = meta.get(key)
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str) and value:
        return [value]
    return []


def recompute_context_score(
    task_context: TaskContext | None,
    evidence_context: EvidenceContext | None = None,
    meta: dict[str, Any] | None = None,
) -> ContextScore:
    """Deterministically rescore evidence context against the task context.

    ``meta`` (``matched_disease``/``matched_tissue``/``matched_cell``/
    ``matched_assay``/``source_fields``) is match evidence only; it never
    carries the final score. When no task context is available the evidence's
    own declared context is used as the reference so pre-task-context callers
    still get a deterministic recomputation instead of trusting the
    self-reported number.
    """
    reference = task_context or evidence_context
    if reference is None:
        return ContextScore(
            0.0, 0.0, 0.0, 0.0, 0.0, {},
            ["no task or evidence context; conservative score"], "recomputed",
        )
    evidence_values: dict[str, list[str]] = {
        "disease": _meta_list(meta, "matched_disease"),
        "tissue": _meta_list(meta, "matched_tissue"),
        "cell": _meta_list(meta, "matched_cell"),
        "assay": _meta_list(meta, "matched_assay"),
    }
    if evidence_context is not None:
        for key, value in (
            ("disease", evidence_context.disease),
            ("tissue", evidence_context.tissue),
            ("cell", evidence_context.cell_type),
            ("assay", evidence_context.assay),
        ):
            if value:
                evidence_values[key].append(value)
    disease, disease_hits = _dimension(reference.disease, evidence_values["disease"])
    tissue, tissue_hits = _dimension(reference.tissue, evidence_values["tissue"])
    cell, cell_hits = _dimension(reference.cell_type, evidence_values["cell"])
    assay, assay_hits = _dimension(reference.assay, evidence_values["assay"])
    total = (
        DISEASE_WEIGHT * disease
        + TISSUE_WEIGHT * tissue
        + CELL_WEIGHT * cell
        + ASSAY_WEIGHT * assay
    )
    notes: list[str] = []
    if reference.disease and disease == 0.0:
        notes.append("disease dimension not matched; hard cap below the formal gate")
        total = min(total, DISEASE_MISS_CAP)
    hits = {
        "disease": disease_hits,
        "tissue": tissue_hits,
        "cell": cell_hits,
        "assay": assay_hits,
    }
    return ContextScore(
        score=round(total, 4),
        disease=round(disease, 4),
        tissue=round(tissue, 4),
        cell=round(cell, 4),
        assay=round(assay, 4),
        hits=hits,
        notes=notes,
        origin="recomputed",
    )


def evidence_context_score(item: Any, task_context: TaskContext | None) -> ContextScore:
    """Rescore one EvidenceItem without importing it at module import time."""
    return recompute_context_score(task_context, item.context, item.context_match)
