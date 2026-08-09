import json

import pytest
from pydantic import ValidationError

from target_agent.blind_benchmark import (
    BlindBenchmarkManifest,
    BlindLabelSet,
    bundle_sha256,
    evaluate_benchmark,
    file_sha256,
    public_report,
)


POLICY = {
    "min_ndcg_at_k": 0.9,
    "min_recall_at_k": 1.0,
    "min_mrr_at_k": 1.0,
    "max_trap_case_rate": 0.0,
    "min_safety_blocker_recall": 1.0,
    "max_unsafe_go_rate": 0.0,
}


def _write_run(root, run_id="run-blind-1", disease_id="MONDO_0000001", task=None, ranking=None):
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    task_payload = task or {
        "contract_version": "2.2.0",
        "task_type": "disease_to_target",
        "question": "rank targets for the held-out disease",
        "context": {"contract_version": "2.2.0", "disease": "held-out disease",
                    "disease_id": disease_id},
        "constraints": {"contract_version": "2.2.0"},
        "candidate_genes": [],
    }
    default_ranking = [
        {"gene": "A", "decision": "GO", "safety_blockers": []},
        {"gene": "B", "decision": "CONDITIONAL_GO", "safety_blockers": ["known toxicity"]},
        {"gene": "TRAP", "decision": "INSUFFICIENT_EVIDENCE", "safety_blockers": []},
    ]
    (run_dir / "task_spec.json").write_text(json.dumps(task_payload, sort_keys=True), encoding="utf-8")
    (run_dir / "ranked_targets.json").write_text(
        json.dumps(default_ranking if ranking is None else ranking), encoding="utf-8"
    )
    (run_dir / "status.json").write_text(
        json.dumps({"terminal_status": "completed_with_gaps"}), encoding="utf-8"
    )
    task_digest = file_sha256(run_dir / "task_spec.json")
    ranking_digest = file_sha256(run_dir / "ranked_targets.json")
    status_digest = file_sha256(run_dir / "status.json")
    return {
        "case_id": f"case-{run_id}", "disease_group_id": disease_id, "run_id": run_id,
        "task_sha256": task_digest, "ranking_sha256": ranking_digest,
        "status_sha256": status_digest,
        "bundle_sha256": bundle_sha256(task_digest, ranking_digest, status_digest),
    }


def _label(case_id, trap=True):
    return {
        "case_id": case_id,
        "judgment_pool_id": "pool-sha256-fixture",
        "unjudged_policy": "treat_as_nonrelevant",
        "relevance": [
            {"gene": "A", "grade": 3, "source_ids": ["source-a"]},
            {"gene": "B", "grade": 2, "source_ids": ["source-b"]},
            {"gene": "LOW", "grade": 1, "source_ids": ["source-low"]},
        ],
        "trap_targets": ([{"gene": "TRAP", "expected_behavior": "do_not_go"}] if trap else []),
        "safety_expectations": [{
            "gene": "B", "allowed_decisions": ["CONDITIONAL_GO"],
            "required_blocker_terms": ["toxicity"], "source_ids": ["safety-b"],
        }],
    }


def _inputs(cases, labels=None, policy=None, require_expert=False):
    manifest = BlindBenchmarkManifest.model_validate({
        "benchmark_id": "fixture", "split_id": "heldout", "k": 2,
        "require_expert_adjudication": require_expert,
        "thresholds": policy or POLICY,
        "cases": cases,
    })
    labels = BlindLabelSet.model_validate({
        "benchmark_id": "fixture", "split_id": "heldout",
        "adjudication": {"status": "synthetic_fixture", "reviewer_count": 0,
                         "reviewers_blinded": True, "evidence_cutoff": "2026-07-31",
                         "source_snapshot_ids": []},
        "cases": labels or [_label(case["case_id"]) for case in cases],
    })
    return manifest, labels


def test_blind_metrics_and_non_compensating_safety_gate(tmp_path):
    case = _write_run(tmp_path)
    manifest, labels = _inputs([case])
    report = evaluate_benchmark(manifest, labels, tmp_path)
    assert report["passed"]
    assert report["summary"]["disease_macro_ndcg_at_k"] == 1.0
    assert report["summary"]["disease_macro_recall_at_k"] == 1.0
    assert report["summary"]["disease_macro_mrr_at_k"] == 1.0
    assert report["summary"]["trap_case_rate"] == 0.0  # trap retained as non-GO is correct
    assert report["summary"]["disease_macro_safety_blocker_recall"] == 1.0
    assert "cases" not in public_report(report)


def test_trap_go_and_unsafe_go_fail_even_with_metric_thresholds_disabled(tmp_path):
    case = _write_run(tmp_path, ranking=[
        {"gene": "TRAP", "decision": "GO", "safety_blockers": []},
        {"gene": "B", "decision": "GO", "safety_blockers": []},
    ])
    policy = {**POLICY, "min_ndcg_at_k": 0.0, "min_recall_at_k": 0.0, "min_mrr_at_k": 0.0}
    manifest, labels = _inputs([case], policy=policy)
    report = evaluate_benchmark(manifest, labels, tmp_path)
    assert not report["gates"]["trap_case_rate"]
    assert not report["gates"]["safety_blocker_recall"]
    assert not report["gates"]["unsafe_go_rate"]


def test_frozen_ranking_tamper_and_candidate_leakage_fail_isolation(tmp_path):
    case = _write_run(tmp_path)
    manifest, labels = _inputs([case])
    (tmp_path / case["run_id"] / "ranked_targets.json").write_text("[]", encoding="utf-8")
    report = evaluate_benchmark(manifest, labels, tmp_path)
    assert report["cases"][0]["error_code"] == "gold_isolation_failure"

    leaked = _write_run(tmp_path, run_id="run-leak", task={
        "contract_version": "2.2.0", "task_type": "disease_to_target", "question": "leaked",
        "context": {"disease": "held-out", "disease_id": "MONDO_0000001"},
        "candidate_genes": ["A"],
    })
    manifest, labels = _inputs([leaked])
    report = evaluate_benchmark(manifest, labels, tmp_path)
    assert report["cases"][0]["error_code"] == "gold_isolation_failure"


def test_duplicate_or_malformed_case_does_not_abort_other_cases(tmp_path):
    bad = _write_run(tmp_path, run_id="run-bad", ranking=[
        {"gene": "A", "decision": "GO", "safety_blockers": []},
        {"gene": " a ", "decision": "GO", "safety_blockers": []},
    ])
    good = _write_run(tmp_path, run_id="run-good", disease_id="MONDO_0000002")
    manifest, labels = _inputs([bad, good])
    report = evaluate_benchmark(manifest, labels, tmp_path)
    assert len(report["cases"]) == 2
    assert report["cases"][0]["error_code"] == "duplicate_predictions"
    assert report["cases"][1]["valid"]
    assert not report["gates"]["structural_integrity"]


def test_empty_ranking_is_valid_but_fails_candidate_gate(tmp_path):
    case = _write_run(tmp_path, ranking=[])
    policy = {**POLICY, "min_ndcg_at_k": 0.0, "min_recall_at_k": 0.0,
              "min_mrr_at_k": 0.0, "min_safety_blocker_recall": 0.0,
              "max_unsafe_go_rate": 1.0}
    manifest, labels = _inputs([case], policy=policy)
    report = evaluate_benchmark(manifest, labels, tmp_path)
    row = report["cases"][0]
    assert row["valid"] and row["error_code"] == "no_candidates"
    assert row["ndcg_at_k"] == row["recall_at_k"] == row["mrr_at_k"] == 0.0
    assert not report["gates"]["has_candidates"]


def test_manifest_requires_explicit_policy_and_expert_gate_is_real(tmp_path):
    case = _write_run(tmp_path)
    payload = {"benchmark_id": "fixture", "split_id": "heldout", "cases": [case]}
    with pytest.raises(ValidationError):
        BlindBenchmarkManifest.model_validate(payload)
    manifest, labels = _inputs([case], require_expert=True)
    report = evaluate_benchmark(manifest, labels, tmp_path)
    assert not report["gates"]["expert_adjudication"]


def test_expert_labels_require_two_blinded_reviewers_and_snapshots():
    with pytest.raises(ValueError, match="at least two"):
        BlindLabelSet.model_validate({
            "benchmark_id": "fixture", "split_id": "heldout",
            "adjudication": {"status": "expert_adjudicated", "reviewer_count": 1,
                             "reviewers_blinded": True, "evidence_cutoff": "2026-07-31",
                             "source_snapshot_ids": ["snapshot-1"]},
            "cases": [_label("case-1")],
        })


def test_go_without_safety_expectation_is_not_counted_as_unsafe(tmp_path):
    case = _write_run(tmp_path, ranking=[
        {"gene": "A", "decision": "GO", "safety_blockers": []},
    ])
    labels = [_label(case["case_id"], trap=False)]
    labels[0]["safety_expectations"] = []  # gold has no safety expectation for gene A
    manifest, label_set = _inputs([case], labels=labels)
    report = evaluate_benchmark(manifest, label_set, tmp_path)
    assert report["cases"][0]["unsafe_go_rate"] is None
    assert report["gates"]["unsafe_go_rate"]


def test_go_not_allowed_by_gold_label_is_counted_as_unsafe(tmp_path):
    case = _write_run(tmp_path, ranking=[
        {"gene": "A", "decision": "GO", "safety_blockers": []},
    ])
    labels = [_label(case["case_id"], trap=False)]
    labels[0]["safety_expectations"] = [{
        "gene": "A", "allowed_decisions": ["CONDITIONAL_GO", "NO_GO"],
        "required_blocker_terms": ["toxicity"], "source_ids": ["safety-a"],
    }]
    manifest, label_set = _inputs([case], labels=labels)
    report = evaluate_benchmark(manifest, label_set, tmp_path)
    assert report["summary"]["disease_macro_unsafe_go_rate"] == 1.0
    assert not report["gates"]["unsafe_go_rate"]
