# Mechanism-Ablation Report (descriptive)

- Gold set: goldset_v2.jsonl (fake+unit, deterministic)
- All arms score 1.0 on this goldset: the strict genetics layer never fires in the fake fixtures and the LLM planner/reviewer layers are not exercised in fake mode, so every delta here is zero BY CONSTRUCTION. This confirms the guardrail that goldset assertion deltas are the wrong instrument for independent-contribution claims - use the blind-ranking protocol (benchmark/rubric.md, BM-14) with nDCG/Recall@K/MRR/blocker/safety metrics, and measure each evidence-input arm on a goldset where the layer actually fires.

| Ablation set | Category | Disabled mechanism(s) | Score | Δ vs baseline |
|---|---|---|---:|---:|
| baseline | baseline | none | 1.0 | 0.0 |
| no_context_gate | safety_negative_control | ['no_context_gate'] | 1.0 | 0.0 |
| no_human_genetics | evidence_input | ['no_human_genetics'] | 1.0 | 0.0 |
| no_mechanism_bonus | scoring | ['no_mechanism_bonus'] | 1.0 | 0.0 |
| no_perturbation_layer | evidence_input | ['no_perturbation_layer'] | 1.0 | 0.0 |
| no_planner_llm | model_component | ['no_planner_llm'] | 1.0 | 0.0 |
| no_reviewer_llm | model_component | ['no_reviewer_llm'] | 1.0 | 0.0 |
| all | all_combined | ['no_context_gate', 'no_human_genetics', 'no_mechanism_bonus', 'no_perturbation_layer', 'no_planner_llm', 'no_reviewer_llm'] | 1.0 | 0.0 |
