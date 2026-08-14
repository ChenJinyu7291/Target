# Ablation × Blind-Ranking Matrix — disease library (synthetic fixture)

18 diseases from configs/disease_library.yaml; synthetic labels derived from curated reference_targets (train/dev sanity data per rubric; not an external blind result). `no_context_gate` readout is the mismatched-evidence admission flag; model-component arms do not fire in this in-process scorer fixture.

| Arm | Category | nDCG@K | Δ nDCG | Recall@K | MRR@K | Trap | Safety recall | Unsafe GO | Gates | Mismatch admitted |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| baseline | baseline | 0.999643 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | False |
| no_context_gate | safety_negative_control | 0.999394 | 0.000249 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | True |
| no_human_genetics | evidence_input | 0.94768 | 0.051963 | 1.0 | 0.916667 | 0.0 | 1.0 | 0.0 | PASS | False |
| no_mechanism_bonus | scoring | 0.987513 | 0.01213 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | False |
| no_perturbation_layer | evidence_input | 0.999643 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | False |
| no_planner_llm | model_component | 0.999643 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | False |
| no_reviewer_llm | model_component | 0.999643 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | PASS | False |
| all | all_combined | 0.940015 | 0.059628 | 1.0 | 0.916667 | 0.0 | 1.0 | 0.0 | PASS | True |
