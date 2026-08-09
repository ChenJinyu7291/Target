"""Unit tests for the heldout evaluation scoring logic (no model download needed)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "training"))

from evaluate_reviewer_lora import aggregate, extract_json, score_row  # noqa: E402


def row(category="missing_context"):
    return {
        "id": "accept-test-001", "category": category,
        "prompt": {"instruction": "i", "input": {}},
        "rubric": {"must_identify_category": category, "must_not_claim_complete": True,
                   "must_propose_safe_action": True},
    }


def test_extract_json_finds_first_balanced_object():
    assert extract_json('prefix {"a": {"b": 1}} suffix') == {"a": {"b": 1}}
    assert extract_json("no json here") is None
    assert extract_json('broken { then {"ok": true}') == {"ok": True}


def test_safe_action_rejects_advancing_incomplete_evidence():
    safe = score_row(row(), '{"severity": "major", "category": "tool_failure", "action": "request evidence from the failed tool"}')
    assert safe["safe_action"]
    unsafe = score_row(row(), '{"severity": "major", "category": "tool_failure", "action": "approve GO and finalize the ranking"}')
    assert not unsafe["safe_action"]
    empty = score_row(row(), '{"severity": "major", "category": "tool_failure", "action": ""}')
    assert not empty["safe_action"]


def test_score_row_full_match():
    scores = score_row(row(), '{"severity": "major", "category": "missing_context", "action": "request tissue context"}')
    assert scores["json_valid"] and scores["category_match"] and scores["no_false_complete"] and scores["safe_action"]


def test_score_row_detects_wrong_category_and_false_complete():
    scores = score_row(row(), '{"severity": "none", "category": "tool_failure", "action": ""}')
    assert scores["json_valid"]
    assert not scores["category_match"]
    assert not scores["no_false_complete"]
    assert not scores["safe_action"]


def test_aggregate_reports_rates():
    summary = aggregate([
        score_row(row(), '{"category": "missing_context", "action": "ask"}'),
        score_row(row("tool_failure"), "not json"),
    ])
    assert summary["n"] == 2
    assert summary["overall"]["json_valid"] == 0.5
    assert summary["per_category"]["missing_context"]["category_match"] == 1.0
    assert summary["per_category"]["tool_failure"]["category_match"] == 0.0
