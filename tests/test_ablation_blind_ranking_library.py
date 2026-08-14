"""Disease-library-scale ablation x blind-ranking tests (synthetic fixture only)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmark"))

from ablation_blind_ranking_library import (  # noqa: E402
    build_labels,
    load_library,
    run_arm,
)
from target_agent.blind_benchmark import BlindLabelSet  # noqa: E402


@pytest.fixture(scope="module")
def diseases():
    return load_library()


@pytest.fixture(scope="module")
def labels(diseases):
    return BlindLabelSet.model_validate(build_labels(diseases))


def test_labels_cover_all_18_diseases(diseases, labels):
    assert len(diseases) == 18
    assert {case.case_id for case in labels.cases} == {f"lib-{d['id']}" for d in diseases}
    assert labels.adjudication.status == "synthetic_fixture"


def test_baseline_passes_all_gates(tmp_path, diseases, labels):
    row = run_arm("baseline", diseases, labels, tmp_path)
    assert row["gates_passed"] is True
    assert row["disease_macro_recall_at_k"] >= 0.9
    assert row["mismatched_evidence_admitted"] is False


def test_no_human_genetics_moves_ndcg(tmp_path, diseases, labels):
    baseline = run_arm("baseline", diseases, labels, tmp_path / "base")
    ablated = run_arm("no_human_genetics", diseases, labels, tmp_path / "ablated")
    assert ablated["disease_macro_ndcg_at_k"] != baseline["disease_macro_ndcg_at_k"]


def test_no_context_gate_admits_mismatched(tmp_path, diseases, labels):
    row = run_arm("no_context_gate", diseases, labels, tmp_path)
    assert row["mismatched_evidence_admitted"] is True
