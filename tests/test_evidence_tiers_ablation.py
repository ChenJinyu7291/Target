"""Evidence-tier policy and mechanism-ablation regression tests (mid-term review)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from target_agent import ablations  # noqa: E402
from target_agent.contracts import (  # noqa: E402
    ClaimClass, EvidenceContext, EvidenceItem, SourceLocator, Stance, TaskContext,
)
from target_agent.evidence_tiers import (  # noqa: E402
    TIER_HUMAN_GENETICS, TIER_LITERATURE, TIER_OMICS_BULK, TIER_PERTURB_MEASURED,
    TIER_PERTURB_PREDICTED, TIER_SINGLE_CELL, concordance_matrix, concordance_score_row,
    format_report_section, load_tiers, tier_for_item,
)
from target_agent.ranking import rank_targets  # noqa: E402


def make_item(gene="IL12B", claim=ClaimClass.OBSERVED, effect=None, uri="https://tool.example/x",
              stance=Stance.SUPPORTS, context_score=0.9, tool_run_id="tr-1", context=None):
    return EvidenceItem(
        tool_run_id=tool_run_id, gene_symbol=gene, claim_class=claim,
        statement=f"{gene} test statement", source=SourceLocator(uri=uri, source_id="s1"),
        source_span="span", context=context or {}, stance=stance, effect=effect or {},
        uncertainty="test", context_match_score=context_score,
    )


# ------------------------------------------------------------------ tier policy
def test_policy_loads_and_orders_quality_first():
    policy = load_tiers()
    assert len(policy.tiers) == 7
    order = policy.orchestration_order()
    assert order[0] == TIER_HUMAN_GENETICS  # population-scale anchor starts discovery
    assert order.index(TIER_SINGLE_CELL) > order.index(TIER_PERTURB_MEASURED)
    anchors = [t.id for t in policy.tiers if t.role == "anchor"]
    assert set(anchors) == {TIER_HUMAN_GENETICS, TIER_OMICS_BULK}
    single_cell = policy.tier(TIER_SINGLE_CELL)
    assert single_cell.role == "context"  # small cohorts: refines, never anchors
    assert single_cell.ranking_dimension is None


def test_tier_classification():
    assert tier_for_item(make_item(effect={"genetic_score": 0.8})) == TIER_HUMAN_GENETICS
    assert tier_for_item(make_item(effect={"omics_strength": 0.7})) == TIER_OMICS_BULK
    assert tier_for_item(make_item(effect={"disease_alignment": 0.2})) == TIER_PERTURB_MEASURED
    assert tier_for_item(make_item(claim=ClaimClass.PREDICTED, effect={"disease_alignment": 0.2})) == TIER_PERTURB_PREDICTED
    assert tier_for_item(make_item(claim=ClaimClass.FACT, uri="https://europepmc.org/a")) == TIER_LITERATURE
    assert tier_for_item(make_item(tool_run_id="tr-sc"), {"tr-sc": "single_cell_analysis"}) == TIER_SINGLE_CELL
    assert tier_for_item(make_item(effect={})) is None


def test_concordance_matrix_supports_opposes_missing():
    evidence = [
        make_item(effect={"genetic_score": 0.8}),
        make_item(effect={"disease_alignment": 0.2}, stance=Stance.REFUTES),
    ]
    matrix = concordance_matrix(["IL12B"], evidence)
    row = matrix["IL12B"]
    assert row[TIER_HUMAN_GENETICS]["status"] == "supports"
    assert row[TIER_PERTURB_MEASURED]["status"] == "opposes"
    assert row[TIER_SINGLE_CELL]["status"] == "missing"
    tally = concordance_score_row(row)
    assert tally == {"anchor_tiers": 1, "validation_tiers": 0, "opposed_tiers": 1}
    section = "\n".join(format_report_section(matrix, ["IL12B"]))
    assert "证据分层一致性" in section and "IL12B" in section


# ------------------------------------------------------------------ ablation parsing
def test_ablation_parse_rejects_unknown():
    assert ablations.parse("no_mechanism_bonus, no_context_gate") == frozenset(
        {"no_mechanism_bonus", "no_context_gate"})
    with pytest.raises(ValueError):
        ablations.parse("not_a_real_switch")
    assert ablations.parse("") == frozenset()


# ------------------------------------------------------------------ ranking ablations
def ranked_gene(ablations_set=None, context_score=0.9):
    evidence = [
        make_item(effect={"genetic_score": 0.8}, context_score=context_score),
        make_item(effect={"omics_strength": 0.7}, context_score=context_score),
        make_item(effect={"disease_alignment": 0.2}, context_score=context_score),
        make_item(claim=ClaimClass.FACT, uri="https://europepmc.org/a", context_score=context_score),
    ]
    ranked = rank_targets(["IL12B"], evidence, [], ablations=ablations_set)
    return ranked[0]


def test_default_ranking_unchanged_without_ablations():
    full = ranked_gene()
    explicit = ranked_gene(frozenset())
    assert full.scores == explicit.scores
    assert full.scores.mechanism > 0  # concordance bonus active by default


def test_no_mechanism_bonus_zeroes_only_mechanism():
    full, ablated = ranked_gene(), ranked_gene(frozenset({"no_mechanism_bonus"}))
    assert ablated.scores.mechanism == 0
    assert ablated.scores.human_genetics == full.scores.human_genetics
    assert ablated.scores.total < full.scores.total


def test_no_perturbation_layer_zeroes_perturbation():
    ablated = ranked_gene(frozenset({"no_perturbation_layer"}))
    assert ablated.scores.perturbation == 0


def test_no_human_genetics_zeroes_genetics():
    # Strict path only: a colocalization-supported locus with a full harmonization
    # context scores the genetics dimension; the ablation removes exactly that.
    from target_agent.contracts import EvidenceContext, GeneticEvidencePayload

    context = EvidenceContext(
        disease="ulcerative colitis", tissue="colon", genome_build="GRCh38", ancestry="EUR",
        study_id="GWAS-1", locus_id="L1", signal_id="CS1",
    )
    strict_item = EvidenceItem(
        tool_run_id="tr-g", gene_symbol="IL12B", claim_class=ClaimClass.OBSERVED,
        statement="IL12B coloc statement", source=SourceLocator(uri="https://tool.example/g", source_id="g1"),
        source_span="locus=L1|signal=CS1|gene=IL12B|PP4=0.9", context=context,
        stance=Stance.SUPPORTS, effect={}, uncertainty="test", context_match_score=1.0,
        genetic_evidence=GeneticEvidencePayload(
            evidence_type="colocalization", analysis_level="colocalization_supported",
            study_id="GWAS-1", molecular_study_id="EQTL-1", locus_id="L1", signal_id="CS1",
            gene_symbol="IL12B", method="coloc_susie", method_version="fixture",
            strength=0.9, formal_score_eligible=True,
        ),
    )
    full = rank_targets(["IL12B"], [strict_item], [])[0]
    ablated = rank_targets(["IL12B"], [strict_item], [], ablations=frozenset({"no_human_genetics"}))[0]
    assert full.scores.human_genetics > 0
    assert ablated.scores.human_genetics == 0


def test_no_context_gate_admits_low_context_evidence():
    # New deterministic scorer: a disease mismatch hard-caps the recomputed
    # context score at 0.40, below the 0.5 formal gate. context_match_score is
    # only a self-reported estimate and no longer decides admission.
    mismatched = TaskContext(disease="alzheimer disease")
    context = EvidenceContext(disease="ulcerative colitis")

    def ranked_with(ablation_set):
        evidence = [
            make_item(effect={"genetic_score": 0.8}, context=context),
            make_item(effect={"omics_strength": 0.7}, context=context),
        ]
        return rank_targets(["IL12B"], evidence, [], task_context=mismatched,
                            ablations=ablation_set)[0]

    gated = ranked_with(None)
    ungated = ranked_with(frozenset({"no_context_gate"}))
    assert gated.scores.total == 0  # everything filtered by the context gate
    assert ungated.scores.total > 0  # ablation admits it (measuring the gate's contribution)


# ------------------------------------------------------------------ reviewer ablation
def test_no_reviewer_llm_skips_llm_layers(monkeypatch):
    from target_agent.reviewer import Reviewer
    from target_agent.contracts import TaskSpec

    monkeypatch.setenv("TARGET_AGENT_EVALUATION_MODE", "1")
    monkeypatch.setenv("TARGET_AGENT_ABLATIONS", "no_reviewer_llm")
    reviewer = Reviewer(None)
    reviewer._lora_findings = lambda *a, **k: (_ for _ in ()).throw(AssertionError("LoRA layer ran"))
    reviewer._llm_findings = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Step layer ran"))
    task = TaskSpec(task_type="disease_to_target", question="reviewer ablation check", context={"disease": "ulcerative colitis"})
    findings = reviewer.review(task, [], [])
    assert findings == []
    assert reviewer.last_backend == "deterministic:ablated_no_reviewer_llm"


# ------------------------------------------------------------------ evaluation-mode guard
def test_ablations_outside_evaluation_mode_raise(monkeypatch):
    monkeypatch.delenv("TARGET_AGENT_EVALUATION_MODE", raising=False)
    monkeypatch.setenv("TARGET_AGENT_ABLATIONS", "no_context_gate")
    with pytest.raises(ValueError, match="evaluation-only"):
        ablations.from_env()
    monkeypatch.delenv("TARGET_AGENT_ABLATIONS")
    assert ablations.from_env() == ablations.AblationConfig()


def test_ablation_config_categories_and_validation():
    config = ablations.AblationConfig(ablations.parse("no_context_gate,no_planner_llm"))
    assert "no_context_gate" in config and "no_planner_llm" in config
    grouped = config.by_category()
    assert grouped == {
        "safety_negative_control": ["no_context_gate"],
        "model_component": ["no_planner_llm"],
    }
    with pytest.raises(ValueError):
        ablations.AblationConfig(frozenset({"not_a_real_switch"}))


# ------------------------------------------------------------------ planner ablation
def test_no_planner_llm_uses_deterministic_plan(monkeypatch):
    from target_agent.contracts import TaskSpec
    from target_agent.planner import Planner

    monkeypatch.setenv("TARGET_AGENT_EVALUATION_MODE", "1")
    monkeypatch.setenv("TARGET_AGENT_ABLATIONS", "no_planner_llm")

    class ExplodingClient:
        model = "should-never-run"

        def json_completion(self, *a, **k):
            raise AssertionError("Planner LLM ran under no_planner_llm")

    task = TaskSpec(task_type="disease_to_target", question="planner ablation check",
                    context={"disease": "ulcerative colitis"})
    plan = Planner(ExplodingClient(), registry=None).create_plan(task)
    assert plan.fallback_used
    assert "ablated no_planner_llm" in plan.planner_backend


# ------------------------------------------------------------------ parity with the default path
def test_default_ablation_config_is_field_level_identical():
    import dataclasses

    evidence = [
        make_item(effect={"genetic_score": 0.8}),
        make_item(effect={"omics_strength": 0.7}),
        make_item(effect={"disease_alignment": 0.2}),
        make_item(claim=ClaimClass.FACT, uri="https://europepmc.org/a"),
    ]
    baseline = rank_targets(["IL12B"], evidence, [])[0]
    explicit_default = rank_targets(["IL12B"], evidence, [],
                                    ablations=ablations.AblationConfig())[0]
    assert dataclasses.asdict(baseline) == dataclasses.asdict(explicit_default)
