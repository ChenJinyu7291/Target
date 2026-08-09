"""Generate benchmark goldsets from the curated disease library.

Two artifacts are produced deterministically (byte-stable across runs):

- goldset_diseases.jsonl      fake-mode entries for every disease x template bucket.
                              CI-safe; the runner must score 1.0 on this file.
- goldset_diseases_lora.jsonl live-mode entries whose expectation-derived assertions
                              (e.g. finding_message_contains for missing_context)
                              require the Reviewer LoRA backend to be configured
                              (TARGET_AGENT_REVIEWER_LORA_BASE/ADAPTER). Run with
                              `python benchmark/runner.py --goldset benchmark/goldset_diseases_lora.jsonl --live`
                              on the external GPU profile only.

Expectation -> assertion mapping
--------------------------------
- expectation.terminal_status_in          -> terminal_status_in assertion (both files)
- expectation.must_not_claim_causal       -> no_causal_claims assertion (both files)
- expectation.reviewer_categories         -> finding_message_contains assertions,
                                             live/LoRA file only, and only for
                                             categories with a guaranteed probe trigger
                                             (currently: missing_context). Other
                                             categories are kept as informational
                                             metadata under "expectation".

Usage: python benchmark/generate_disease_goldset.py [--check]
  --check regenerates in memory and fails if the committed files are stale.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from target_agent.diseases import TEMPLATE_KINDS, load_library  # noqa: E402

OUT_FAKE = ROOT / "benchmark" / "goldset_diseases.jsonl"
OUT_LORA = ROOT / "benchmark" / "goldset_diseases_lora.jsonl"

BUCKET_PREFIX = {"normal": "N", "missing_context": "MC", "conflicting_evidence": "CE", "trap": "T"}
GUARANTEED_PROBE_CATEGORIES = {"missing_context"}

BASE_ASSERTIONS = [
    {"type": "evidence_provenance"},
    {"type": "file_exists", "path": "report.md"},
]


def expectation_assertions(expectation: dict, live: bool) -> list[dict]:
    assertions: list[dict] = []
    statuses = expectation.get("terminal_status_in")
    if statuses:
        assertions.append({"type": "terminal_status_in", "values": list(statuses)})
    if expectation.get("must_not_claim_causal"):
        assertions.append({"type": "no_causal_claims"})
    min_reference = expectation.get("min_reference_genes_in_evidence")
    reference_genes = expectation.get("reference_genes")
    if min_reference and reference_genes:
        assertions.append({
            "type": "min_reference_genes_in_evidence",
            "reference_genes": list(reference_genes),
            "min_count": int(min_reference),
        })
    if live:
        for category in expectation.get("reviewer_categories", []):
            if category in GUARANTEED_PROBE_CATEGORIES:
                assertions.append({"type": "finding_message_contains", "substring": category})
    return assertions


def build_entries(live: bool) -> list[dict]:
    library = load_library()
    entries: list[dict] = []
    for disease in library.diseases:
        for kind in TEMPLATE_KINDS:
            template = library.task_templates[kind]
            task = disease.to_task_spec(kind=kind, template=template)
            task_payload = task.model_dump(mode="json", exclude={"task_id", "created_at"})
            assertions = list(expectation_assertions(template.expectation, live))
            if live:
                assertions.extend(BASE_ASSERTIONS)
                if kind == "normal":
                    assertions.append({"type": "ranking_min_length", "value": 1})
            else:
                assertions.extend(BASE_ASSERTIONS)
            prefix = BUCKET_PREFIX[kind]
            entries.append({
                "id": f"BM-DL-{prefix}-{disease.id}",
                "title": f"{disease.name} ({kind})",
                "category": f"disease_library_{kind}",
                "mode": "live" if live else "fake",
                "runtime": "langgraph",
                **({} if live else {"registry": "fake"}),
                "task": task_payload,
                "expectation": template.expectation,
                "assertions": assertions,
            })
    return entries


def render(entries: list[dict]) -> str:
    return "".join(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n" for entry in entries)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if committed goldsets are stale")
    args = parser.parse_args()
    outputs = {OUT_FAKE: render(build_entries(live=False)), OUT_LORA: render(build_entries(live=True))}
    if args.check:
        stale = [str(path) for path, content in outputs.items()
                 if not path.exists() or path.read_text(encoding="utf-8") != content]
        if stale:
            print(f"stale goldsets: {stale}; run python benchmark/generate_disease_goldset.py")
            return 1
        print("goldsets are up to date")
        return 0
    for path, content in outputs.items():
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path} ({content.count(chr(10))} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
