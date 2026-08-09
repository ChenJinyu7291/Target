"""Round-4: blind target-ranking protocol end-to-end coverage."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmark.demo_blind_ranking import DEMO_CASES, THRESHOLDS, _freeze_manifest, _write_artifacts, run_demo
from target_agent.blind_benchmark import BlindLabelSet, evaluate_benchmark

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "benchmark" / "blind_demo_labels.json"


def test_demo_passes_and_stays_synthetic(tmp_path):
    public = run_demo(tmp_path / "out", LABELS)
    assert public["passed"] is True
    assert public["summary"]["expert_adjudicated"] is False
    assert public["summary"]["structurally_valid_cases"] == public["summary"]["cases"] == 3
    assert public["gates"]["trap_case_rate"] is True
    assert public["gates"]["safety_blocker_recall"] is True
    assert public["gates"]["unsafe_go_rate"] is True
    assert (tmp_path / "out" / "blind_manifest.json").is_file()
    assert (tmp_path / "out" / "blind_ranking_report.md").is_file()


def test_labels_fixture_is_synthetic():
    labels = BlindLabelSet.model_validate_json(LABELS.read_text(encoding="utf-8"))
    assert labels.adjudication.status == "synthetic_fixture"
    assert labels.benchmark_id == "target-dev-synthetic"
    assert len(labels.cases) == 3


def test_freeze_rejects_missing_artifacts(tmp_path):
    runs = tmp_path / "runs"
    _write_artifacts(runs)
    (runs / DEMO_CASES["opaque-001"][1] / "status.json").unlink()
    with pytest.raises(OSError):
        _freeze_manifest(runs)


def test_case_mismatch_raises(tmp_path):
    runs = tmp_path / "runs"
    _write_artifacts(runs)
    manifest = _freeze_manifest(runs)
    labels = BlindLabelSet.model_validate_json(LABELS.read_text(encoding="utf-8"))
    wrong = labels.model_copy(deep=True)
    cases = [
        case.model_copy(update={"case_id": "other-case"}) if index == 0 else case
        for index, case in enumerate(wrong.cases)
    ]
    wrong = wrong.model_copy(update={"cases": cases})
    with pytest.raises(ValueError, match="exactly the same case IDs"):
        evaluate_benchmark(manifest, wrong, runs)


def test_tampered_ranking_fails_structural_gate(tmp_path):
    runs = tmp_path / "runs"
    _write_artifacts(runs)
    manifest = _freeze_manifest(runs)
    labels = BlindLabelSet.model_validate_json(LABELS.read_text(encoding="utf-8"))
    ranking_path = runs / DEMO_CASES["opaque-002"][1] / "ranked_targets.json"
    payload = json.loads(ranking_path.read_text(encoding="utf-8"))
    payload[0]["decision"] = "GO"
    ranking_path.write_text(json.dumps(payload), encoding="utf-8")
    report = evaluate_benchmark(manifest, labels, runs)
    assert report["gates"]["structural_integrity"] is False
    assert report["passed"] is False


def test_public_report_is_aggregate_only(tmp_path):
    public = run_demo(tmp_path / "out", LABELS)
    assert "cases" not in public
    assert "ranked_targets" not in public
    assert all(key in public for key in ("summary", "gates", "passed"))


def test_cli_freeze_score_end_to_end(tmp_path):
    runs = tmp_path / "runs"
    _write_artifacts(runs)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(THRESHOLDS), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    out = tmp_path / "score"
    cases = []
    for case_id, (group_id, run_id, _name, _ranking) in DEMO_CASES.items():
        cases.append(f"{case_id}={run_id}={group_id}")
    freeze = subprocess.run(
        [sys.executable, str(ROOT / "benchmark" / "blind_ranking.py"), "freeze",
         "--benchmark-id", "target-dev-synthetic", "--split-id", "demo",
         "--runs", str(runs), "--case", *cases, "--policy", str(policy),
         "--allow-nonexpert-fixture", "--out", str(manifest)],
        capture_output=True, text=True, cwd=ROOT, timeout=120,
    )
    assert freeze.returncode == 0, freeze.stderr
    assert manifest.is_file()
    score = subprocess.run(
        [sys.executable, str(ROOT / "benchmark" / "blind_ranking.py"), "score",
         "--manifest", str(manifest), "--labels", str(LABELS),
         "--runs", str(runs), "--out", str(out), "--audit-out", str(tmp_path / "audit.json")],
        capture_output=True, text=True, cwd=ROOT, timeout=120,
    )
    assert score.returncode == 0, score.stderr
    report = json.loads((out / "blind_ranking_report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert "labels_sha256" in report