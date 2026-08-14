# Evaluation Protocol: Evidence Tiers and Mechanism Ablations

Mid-term review deliverable, revised 2026-08-14 after mentor feedback. This document
defines what the evidence-tier layer and the ablation harness do and — just as
important — what claims they do and do not support.

## 1. Evidence tiers are orchestration metadata, not a second weight system

The public scoring contract stays the six-dimensional `ScoreBreakdown`
(human_genetics 25 / disease_omics 20 / perturbation 20 / mechanism 15 /
druggability 10 / safety 10). `configs/evidence_tiers.yaml` adds a descriptive
layer on top of it:

- `role` (anchor / validation / context / translation) declares how a tier is used;
- `quality_rank` orders discovery auditing, not scoring;
- `ranking_dimension` only maps a tier onto an existing ScoreBreakdown dimension
  (`null` = the tier never scores directly).

Attribution: the layered view is **inspired by gsMAP's cross-scale integration of
GWAS with spatial/cellular context** (gsMAP = GWAS × spatial transcriptomics,
Nature 2025). gsMAP does not propose a general-purpose evidence ranking; the tier
structure here is our own engineering hypothesis and must be challenged by the
ablation protocol below — no weight or order is attributed to gsMAP.

"Genetics first" is an **audit order, never a hard serial gate**: when a disease or
subtype lacks GWAS/coloc data, the other channels proceed in parallel and the run
degrades to `completed_with_gaps` instead of stopping.

## 2. AblationConfig: explicit, immutable, evaluation-only

Ablations are injected as an immutable `AblationConfig`
(`src/target_agent/ablations.py`); the default empty config travels the exact
production code path (pinned by
`test_default_ablation_config_is_field_level_identical`). `TARGET_AGENT_ABLATIONS`
is honored only when `TARGET_AGENT_EVALUATION_MODE=1` is set — the benchmark
runner sets it — so no ablation, including the safety negative control, is
reachable from the production CLI.

The switches are **four different categories**, reported separately and never
compared against each other by total score:

| Category | Switches | Correct readout |
|---|---|---|
| evidence_input | `no_human_genetics`, `no_perturbation_layer` | score/coverage delta where the layer actually fires |
| scoring | `no_mechanism_bonus` | mechanism-dimension delta |
| safety_negative_control | `no_context_gate` | mismatched-evidence admission rate, safety-violation rate — **not** a ranking drop |
| model_component | `no_reviewer_llm`, `no_planner_llm` | deterministic-only vs LLM/LoRA-assisted behavior |

## 3. What a goldset score delta does NOT prove

An assertion-score delta on `goldset_v2` (e.g. 1.0 → 0.9259) shows only that some
assertions on this goldset no longer hold with the mechanism disabled. Because the
goldset partially asserts the mechanism itself, this is **descriptive, not evidence
of an independent contribution**. Note also that layers which never fire in a
goldset (e.g. strict genetics in the fake fixtures) show a zero delta there by
construction; the delta must be measured where the layer fires.

## 4. Independent-contribution claims: blind-ranking protocol

To answer "does each mechanism contribute independently of the base model", use
the scorer-only blind-ranking protocol (`benchmark/rubric.md`, BM-14 fixture):

1. Fix the case set, candidate sets and evaluation labels (digest-frozen before
   labels are opened).
2. Run each ablation arm on the identical frozen inputs.
3. Compare per arm: disease-macro **nDCG**, **Recall@K**, **MRR**, **GO/NO_GO
   blocker accuracy**, **trap/safety violation rate**, and **coverage /
   `completed_with_gaps` ratio**.

The base model is identical across arms, so any ranking-quality delta on frozen
candidates is attributable to the removed mechanism rather than to model
variation. An external expert-adjudicated label set is still pending (see
`benchmark/rubric.md`); until then all ablation numbers are development
measurements, not publishable blind results.

### Wired instrument: `benchmark/ablation_blind_ranking.py`

The protocol is connected to the ablation arms on the synthetic development
fixture (`blind_demo_labels.json`, `adjudication.status = synthetic_fixture`).
Each arm re-ranks identical frozen synthetic evidence with the real
`rank_targets` scorer under an evaluation-only `AblationConfig`; per-arm
artifacts are digest-frozen, then the BM-14 scorer computes disease-macro
nDCG@K / Recall@K / MRR@K and the non-compensating trap/safety gates. Latest
run (`benchmark/results_ablation_blind/`):

| Arm | Category | nDCG@K | Readout |
|---|---|---:|---|
| baseline | — | 0.9516 | all gates pass |
| no_human_genetics | evidence_input | 0.9061 | genetics-anchored grade-3 targets drop in rank |
| no_mechanism_bonus | scoring | 0.9464 | small ranking degradation |
| no_perturbation_layer | evidence_input | 0.9989 | **negative delta**: on this fixture perturbation scoring pulls a grade-2 target above a grade-3 one; reported, not hidden |
| no_context_gate | safety_negative_control | 0.9516 | ranking unchanged, but disease-mismatched evidence is ADMITTED — the correct readout for this arm |
| no_reviewer_llm / no_planner_llm | model_component | 0.9516 | these layers do not fire inside the in-process scorer fixture; zero delta means not-measured-here |

Synthetic labels only: these numbers measure mechanism behavior through ranking
metrics, never biological performance.

## 5. Model-component ablations ("is it just the base model?")

The ranking path is deterministic code, not base-model scoring. The
model-component arms separate planning/review capability from tool evidence:

- `no_planner_llm` — the planner falls back to the deterministic template plan;
  compares LLM-planned vs fixed-plan runs on identical tasks.
- `no_reviewer_llm` — the reviewer runs deterministic gates only (no LoRA, no
  hosted LLM); compares against the LoRA/LLM-assisted review.
- A base-model-only negative control (no tool evidence at all) is part of the
  blind-ranking protocol design and is run at benchmark level, not as a runtime
  switch, because removing tool evidence changes the task itself.

## 6. Reproducing

```bash
# descriptive goldset matrix (fake+unit, deterministic)
python benchmark/compare_ablations.py --goldset benchmark/goldset_v2.jsonl

# single arm
python benchmark/runner.py --goldset benchmark/goldset_v2.jsonl \
    --out runs_ablation/no_human_genetics --ablate no_human_genetics

# scorer-only blind-ranking development fixture
python benchmark/demo_blind_ranking.py

# ablation x blind-ranking matrix (synthetic fixture; per-arm nDCG/Recall/MRR/gates)
python benchmark/ablation_blind_ranking.py
```
