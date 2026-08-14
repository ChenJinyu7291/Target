# Ablation × Blind-Ranking Matrix (synthetic development fixture)

Synthetic labels only; this measures mechanism behavior through ranking metrics,
not biological performance. `no_context_gate` is a safety negative control — read
its mismatched-evidence admission flag, not its ranking delta. Model-component arms
do not fire in this in-process scorer fixture (zero delta = not measured here).

| Arm | Category | nDCG@K | Δ nDCG | Recall@K | MRR@K | Trap | Safety recall | Unsafe GO | Gates | Mismatch admitted |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| baseline | baseline | 0.951556 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | False |
| no_context_gate | safety_negative_control | 0.951556 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | True |
| no_human_genetics | evidence_input | 0.906091 | 0.045465 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | False |
| no_mechanism_bonus | scoring | 0.946362 | 0.005194 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | False |
| no_perturbation_layer | evidence_input | 0.998906 | -0.04735 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | False |
| no_planner_llm | model_component | 0.951556 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | False |
| no_reviewer_llm | model_component | 0.951556 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | False |
| all | all_combined | 0.907596 | 0.04396 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | True |
