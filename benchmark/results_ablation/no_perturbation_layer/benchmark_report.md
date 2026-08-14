# Agent Benchmark Report

- Gold set: `goldset_v2.jsonl` (live mode: False)
- Ablations: no_perturbation_layer
- Tasks: 14/14 passed
- Assertions: 30/30 passed (score 1.0)

| Task | Category | Result | Failed assertions |
|---|---|---|---|
| BM-01 UC main chain completes with expected ranking | main_chain | PASS | - |
| BM-02 Legacy engine produces identical observable output | migration | PASS | - |
| BM-03 Repeated runs are deterministic | determinism | PASS | - |
| BM-04 Terminal resume is idempotent | determinism | PASS | - |
| BM-05 Mid-pipeline crash resumes to completion | determinism | PASS | - |
| BM-06 Tool-call budget degrades gracefully | robustness | PASS | - |
| BM-07 MCH causal gold validates the exact trait | main_chain | PASS | - |
| BM-08 Out-of-scope trait is refused, not fabricated | robustness | PASS | - |
| BM-09 Contract version gate rejects 2.0 tasks | contract | PASS | - |
| BM-10 Planner fallback only uses whitelisted registered tools | contract | PASS | - |
| BM-11 Exported JSON Schemas are valid Draft 2020-12 | contract | PASS | - |
| BM-12 Offline pattern-few-shot ablation keeps deterministic plans valid and reports coverage | contract | PASS | - |
| BM-13 Paper-RAG hits project as strategy-only graph nodes without leaking into evidence | contract | PASS | - |
| BM-L1 Live: Alzheimer disease full pipeline | live | SKIPPED (live) | - |
| BM-L2 Live: lung adenocarcinoma with GEO omics | live | SKIPPED (live) | - |
| BM-L3 Live: clinical trials evidence retrieved for LUAD oncogenes | live | SKIPPED (live) | - |
| BM-14 Blind target-ranking protocol synthetic demo (freeze + score + non-compensating gates) | contract | PASS | - |

## Category scores

| Category | Assertions | Passed | Score |
|---|---|---|---|
| main_chain | 14 | 14 | 1.0 |
| migration | 3 | 3 | 1.0 |
| determinism | 3 | 3 | 1.0 |
| robustness | 4 | 4 | 1.0 |
| contract | 6 | 6 | 1.0 |
