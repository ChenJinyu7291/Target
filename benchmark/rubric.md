# Agent Benchmark Rubric (v2)

Systematic evaluation of the Target Discovery Agent across five dimensions.
Every benchmark task declares machine-checkable assertions; each assertion scores 1 point.
A task passes when **all** of its assertions pass. Category and overall scores are the
fraction of passed assertions (not passed tasks), so partial regressions stay visible.

## Dimensions

| Dimension | What it measures | Example assertions |
|---|---|---|
| Main-chain correctness | End-to-end pipeline produces the expected scientific output on the reference UC case | terminal status, IL12B ranked, 10 ranked targets, 5 target cards |
| Traceability | Every claim carries provenance; referential integrity holds; reports/cards exist | `evidence_provenance`, `file_exists`, trace topology |
| Robustness & degradation | Budget exhaustion, out-of-scope inputs, and refusals degrade gracefully instead of fabricating | `trace_contains degradation`, `terminal_status completed_with_gaps / needs_input` |
| Determinism & recovery | Same input -> same ranking; terminal resume is idempotent; mid-pipeline resume finishes | `deterministic`, `resume_idempotent`, `resume_completes` |
| Contract & migration discipline | Contract version gate, planner whitelist, legacy<->LangGraph parity | `unit:contract_version_gate`, `unit:planner_whitelist`, `parity` |

## Modes

- `fake`: deterministic fake registry; runs in CI, no network, no API key.
- `unit`: in-process check that does not need a pipeline run.
- `live`: real external APIs (Europe PMC, Open Targets, GEO, ClinicalTrials.gov, Step LLM).
  Skipped unless `--live` is passed; results are recorded separately because they are
  not byte-reproducible.

## Pass bar

- Overall fake+unit score must be **100%** for a release; any regression blocks merge.
- Live tasks are informational: a live failure triggers manual triage, not an automatic block.

## Disease-library goldsets (generated)

`benchmark/generate_disease_goldset.py` renders two goldsets from `configs/disease_library.yaml`
(18 diseases x 4 task buckets; `--check` fails CI when the committed files are stale):

- `goldset_diseases.jsonl` (fake mode): 72 entries; must score **100%** in CI alongside the main goldset.
- `goldset_diseases_lora.jsonl` (live mode): the same matrix with expectation-derived assertions that
  require the Reviewer LoRA backend (`finding_message_contains`); run with `--live` on the external
  GPU profile only.

The four buckets encode the project task composition (normal 50 / missing-context 20 /
conflicting-evidence 15 / trap 15). Expectation blocks map to assertions as follows:
`terminal_status_in` -> `terminal_status_in`; `must_not_claim_causal` -> `no_causal_claims`
(no FACT/OBSERVED claim may contain causal language — the trap bucket's provocation must be
absorbed, not obeyed); guaranteed reviewer probes (currently `missing_context`) ->
`finding_message_contains` in the LoRA live goldset only.

## Blind target-ranking protocol

The regression suites above are not evidence that the Agent discovers or ranks biologically
correct targets. The separate scorer in `benchmark/blind_ranking.py` defines that product-level
evaluation boundary:

1. An external evaluator supplies opaque discovery tasks without target labels. The 18 diseases
   and `reference_targets` in the public disease library are train/dev sanity data and are not
   eligible as final blind cases.
2. The Agent runs without a private-label file mounted. Completed `task_spec.json`,
   `ranked_targets.json` and terminal `status.json` artifacts are frozen into a public manifest
   with individual and combined SHA-256 digests.
3. Only after all runs are closed does the scoring environment read the external label file. The
   in-repository scorer is a reference implementation, not a trusted isolation boundary: official
   final scoring must use evaluator-controlled code/container outside the participant repository.

Private labels stay outside Git and contain graded target relevance, trap targets, safety-blocker
expectations and adjudication metadata. `expert_adjudicated` labels require at least two blinded
reviewers and frozen source-snapshot identifiers. Public TaskSpecs are recursively rejected if they
contain gold, labels, reference targets, relevance, trap targets or safety expectations.

The suite owner must provide all release thresholds explicitly; there are no permissive defaults.
Final suites require expert-adjudicated labels unless marked as a synthetic development fixture.
The primary metric is disease-macro nDCG@K with gain `2^relevance - 1`. Recall@K and MRR@K use
`relevance >= 2`; unlisted genes have relevance 0. Trap-case rate, safety-blocker recall and
unsafe-`GO` rate are reported separately and enforced as non-compensating release gates. Empty or
duplicate rankings, label leakage, missing artifacts and TaskSpec digest mismatch are structural
failures. Public reports contain aggregate metrics only; per-case audit data stays organizer-only.
These values are ranking/reliability measurements, never clinical success probabilities.

```bash
# Agent execution happens first and has no label argument.
target-agent run --input PRIVATE_TASK.yaml --run-id OPAQUE_RUN_ID

# Freeze run identity and task digest before labels are opened.
python benchmark/blind_ranking.py freeze \
  --benchmark-id EXTERNAL_SUITE --split-id final \
  --runs runs --case OPAQUE_CASE=OPAQUE_RUN_ID=DISEASE_GROUP \
  --policy /suite-owned/release_policy.json \
  --out artifacts/blind_manifest.json

# Run in the scorer environment after prediction collection is closed.
python benchmark/blind_ranking.py score \
  --manifest artifacts/blind_manifest.json \
  --labels /scorer-only/blind_labels.json --runs runs \
  --out artifacts/blind_score
```

No external expert-adjudicated label set or blind biological performance result is distributed in
this repository yet. Until that exists, only the protocol and scorer may be claimed as implemented.

## Synthetic development fixture (BM-14)

`benchmark/demo_blind_ranking.py` + `benchmark/blind_demo_labels.json` exercise the complete
freeze -> score pipeline with clearly synthetic labels (`adjudication.status = synthetic_fixture`).
It builds three opaque runs, freezes task/ranking/status digests, loads the fixture labels only
after freezing, and scores disease-macro nDCG@K / Recall@K / MRR@K plus trap-case rate,
safety-blocker recall and unsafe-GO rate as non-compensating gates. BM-14 runs it in fake/unit
mode as a release gate. This is a protocol and tooling check, not an external blind biological
result; final suites still require expert-adjudicated labels held outside the repository.
