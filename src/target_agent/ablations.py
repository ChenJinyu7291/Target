"""Mechanism ablation config for evaluation-only experiments (mid-term review).

An ablation disables one designed mechanism so the benchmark can measure what
changes; it never forks the code — the default (empty) config travels the exact
production path, and every runtime entry point (legacy, LangGraph, benchmark
runner) shares it.

Methodology guardrails (mentor feedback, 2026-08-14):

- Switches are NOT one comparable set. They belong to four categories and must
  be reported per category, never as one table of total scores:
    evidence_input          no_human_genetics, no_perturbation_layer
    scoring                 no_mechanism_bonus
    safety_negative_control no_context_gate
    model_component         no_reviewer_llm, no_planner_llm
- ``no_context_gate`` is a SAFETY NEGATIVE CONTROL: the expected readout is the
  mismatched-evidence admission rate and safety-violation rate, not a ranking
  drop. It must never be reachable from the production CLI.
- An assertion-score delta on the goldset alone does NOT establish a
  mechanism's independent contribution (the goldset partially checks the
  mechanism itself). Independent-contribution claims require the blind-ranking
  protocol (benchmark/rubric.md, BM-14 scorer) on fixed cases, candidates and
  labels: disease-macro nDCG, Recall@K, MRR, GO/NO_GO blocker accuracy,
  trap/safety violations, and completed_with_gaps coverage.
- Ablations are evaluation-only: TARGET_AGENT_ABLATIONS is honored only when
  TARGET_AGENT_EVALUATION_MODE=1 is set (the benchmark runner sets it); setting
  switches outside evaluation mode raises instead of silently applying.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

ENV_VAR = "TARGET_AGENT_ABLATIONS"
EVALUATION_MODE_VAR = "TARGET_AGENT_EVALUATION_MODE"

CATEGORIES: dict[str, tuple[str, ...]] = {
    "evidence_input": ("no_human_genetics", "no_perturbation_layer"),
    "scoring": ("no_mechanism_bonus",),
    "safety_negative_control": ("no_context_gate",),
    "model_component": ("no_reviewer_llm", "no_planner_llm"),
}

VALID = frozenset(name for names in CATEGORIES.values() for name in names)


def category_of(name: str) -> str:
    for category, names in CATEGORIES.items():
        if name in names:
            return category
    raise ValueError(f"unknown ablation switch: {name}")


def parse(value: str | None) -> frozenset[str]:
    """Parse a comma-separated ablation list; unknown names raise, never silently ignored."""
    names = frozenset(part.strip() for part in (value or "").split(",") if part.strip())
    unknown = names - VALID
    if unknown:
        raise ValueError(f"unknown ablation switches: {sorted(unknown)}; valid: {sorted(VALID)}")
    return names


@dataclass(frozen=True)
class AblationConfig:
    """Explicit, immutable ablation state injected into ranking/reviewer/planner.

    The default ``AblationConfig()`` is empty and therefore byte-identical in
    behavior to unmodified code; parity tests pin this. Membership tests
    (``"no_context_gate" in config``) keep call sites readable.
    """

    switches: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        unknown = frozenset(self.switches) - VALID
        if unknown:
            raise ValueError(f"unknown ablation switches: {sorted(unknown)}")

    def __contains__(self, name: str) -> bool:
        return name in self.switches

    def __bool__(self) -> bool:
        return bool(self.switches)

    @classmethod
    def coerce(cls, value: "AblationConfig | frozenset[str] | set[str] | None") -> "AblationConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        return cls(frozenset(value))

    def by_category(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {category: [] for category in CATEGORIES}
        for name in sorted(self.switches):
            grouped[category_of(name)].append(name)
        return {category: names for category, names in grouped.items() if names}


def from_env() -> AblationConfig:
    """Read process-wide ablations; honored only in evaluation mode."""
    raw = os.environ.get(ENV_VAR)
    if not raw:
        return AblationConfig()
    if os.environ.get(EVALUATION_MODE_VAR) != "1":
        raise ValueError(
            f"{ENV_VAR} is set but {EVALUATION_MODE_VAR}=1 is not: ablations are "
            "evaluation-only and must not be reachable from production runs."
        )
    return AblationConfig(parse(raw))
