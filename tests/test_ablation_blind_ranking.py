"""Ablation x blind-ranking matrix tests (synthetic development fixture only)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmark"))

import pytest  # noqa: E402

from ablation_blind_ranking import run_arm, LABELS_PATH  # noqa: E402
from target_agent.blind_benchmark import BlindLabelSet  # noqa: E402


@pytest.fixture(scope="module")
def labels():
    return BlindLabelSet.model_validate_json(LABELS_PATH.read_text(encoding="utf-8"))


def test_baseline_passes_all_gates(tmp_path, labels):
    row = run_arm("baseline", tmp_path, labels)
    assert row["gates_passed"] is True
    assert row["disease_macro_ndcg_at_k"] >= 0.9
    assert row["mismatched_evidence_admitted"] is False


def test_no_human_genetics_drops_ndcg(tmp_path, labels):
    baseline = run_arm("baseline", tmp_path / "base", labels)
    ablated = run_arm("no_human_genetics", tmp_path / "ablated", labels)
    assert ablated["disease_macro_ndcg_at_k"] < baseline["disease_macro_ndcg_at_k"]
    # recall is unchanged: the genetics-anchored gene is still ranked, just lower
    assert ablated["disease_macro_recall_at_k"] == baseline["disease_macro_recall_at_k"]


def test_no_context_gate_admits_mismatched_evidence(tmp_path, labels):
    # safety negative control: the readout is admission of disease-mismatched
    # evidence, not a ranking-metric delta
    row = run_arm("no_context_gate", tmp_path, labels)
    assert row["mismatched_evidence_admitted"] is True


def test_model_component_arms_do_not_fire_here(tmp_path, labels):
    # the in-process scorer fixture never calls planner/reviewer LLM layers;
    # a zero delta is reported as not-measured-here, not as no contribution
    baseline = run_arm("baseline", tmp_path / "base", labels)
    for arm in ("no_reviewer_llm", "no_planner_llm"):
        row = run_arm(arm, tmp_path / arm, labels)
        assert row["disease_macro_ndcg_at_k"] == baseline["disease_macro_ndcg_at_k"]


def test_manifest_digests_are_frozen_per_arm(tmp_path, labels):
    row = run_arm("baseline", tmp_path, labels)
    assert len(row["manifest_sha256"]) == 64
