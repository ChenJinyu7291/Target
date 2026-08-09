# Reviewer LoRA — Remote GPU Runbook

The structured Reviewer backend is aligned with LoRA on review-gated data.
Training is **not** run on the local machine: per project policy, all GPU work
happens on the remote training host supplied through the external deployment
profile. This repository intentionally stores no hostnames, credentials or
infra identifiers — fill the slots below from your deployment archive.

## 1. Slots to fill (from the external archive, never commit)

| Slot | Example shape | Where it is used |
|---|---|---|
| `<REMOTE_HOST>` | `user@host` | ssh/scp target |
| `<REMOTE_WORKDIR>` | `/data/target-agent` | remote checkout |
| `<REMOTE_PYTHON>` | `/opt/conda/envs/agent/bin/python` | remote interpreter with CUDA torch |
| `<GPU_VISIBLE>` | `0` | CUDA device selection |

## 2. What gets trained and why

- Base model: `Qwen/Qwen3-8B` (bf16, LoRA r=16, alpha=32, dropout=0.05,
  target modules q/k/v/o — see `training/reviewer_lora.py`).
- Data: `alignment_data/reviewer_sft.jsonl` (120 rows, 6 review categories).
- The adapter is an **optional structured Reviewer backend**; it never controls
  the full Agent and is only promotion-eligible when every training row has
  dual (life-science + engineering) approval (`reviewer_sft.jsonl` review fields).
- `alignment_data/acceptance_heldout.jsonl` (30 rows) is evaluation-only and
  must never enter the training set.

## 3. Remote execution

```bash
# from the repo root, locally
git archive --format=tar.gz -o /tmp/target-agent.tar.gz HEAD   # or copy the working tree
scp /tmp/target-agent.tar.gz <REMOTE_HOST>:<REMOTE_WORKDIR>/
ssh <REMOTE_HOST> 'cd <REMOTE_WORKDIR> && tar xzf target-agent.tar.gz'

# on the remote host
ssh <REMOTE_HOST>
cd <REMOTE_WORKDIR>
<REMOTE_PYTHON> -m pip install -e ".[train]"
export CUDA_VISIBLE_DEVICES=<GPU_VISIBLE>
# mainland-China hosts may need: export HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1

# full training (100 steps ≈ 30-60 min on one 24 GB GPU)
<REMOTE_PYTHON> training/reviewer_lora.py \
    --model Qwen/Qwen3-8B --output models/reviewer-lora --max-steps 100
# NOTE: without --allow-pending-review the script refuses rows lacking dual approval.

# heldout acceptance evaluation: adapter vs base
<REMOTE_PYTHON> training/evaluate_reviewer_lora.py \
    --model Qwen/Qwen3-8B --output models/reviewer-lora/eval_base.json
<REMOTE_PYTHON> training/evaluate_reviewer_lora.py \
    --model Qwen/Qwen3-8B --adapter models/reviewer-lora \
    --output models/reviewer-lora/eval_adapter.json

# bring artifacts back
tar czf /tmp/reviewer-lora.tar.gz models/reviewer-lora
# locally: scp <REMOTE_HOST>:/tmp/reviewer-lora.tar.gz . && tar xzf reviewer-lora.tar.gz
```

## 4. Acceptance gates before the adapter may back the Reviewer

1. `eval_adapter.json` beats `eval_base.json` on `overall.category_match` and
   `fully_correct`; `json_valid` must be ≥ 0.95 on the 30-row heldout set.
2. No heldout row was used for training (manifest counts: sft 120 / heldout 30).
3. `training_manifest.json` has `"promotion_eligible": true` (i.e. training ran
   without `--allow-pending-review`).
4. The adapter is registered only as the optional Reviewer backend; runtime
   behavior without it must stay green (`python -m pytest tests/ -q`).

## 5. Local smoke (CPU, verified)

A 1-step CPU smoke of the full train -> save -> load -> evaluate path was run with
`Qwen/Qwen3-0.6B` (~155 s/step, loss 4.907, adapter + `training_manifest.json`
written, heldout eval produced `json_valid = 1.0`). Command:

```bash
python training/reviewer_lora.py --model models/qwen3-0.6b \
    --output models/reviewer-lora-smoke --max-steps 1 \
    --gradient-accumulation 1 --allow-pending-review
python training/evaluate_reviewer_lora.py --model models/qwen3-0.6b \
    --adapter models/reviewer-lora-smoke --limit 3 \
    --output models/reviewer-lora-smoke/eval_report.json
```

Smoke adapters are never promotion-eligible and must not be committed
(`models/` is excluded from version control).

## 6. Completed full run (2026-08-04)

A full 100-step run was executed on the external GPU profile (1 x H100 80 GB,
Qwen3-8B, bf16, ~2.5 min, train_loss 0.57). Heldout acceptance: adapter 1.0 on
all four rubric dimensions (30/30 fully correct) vs base 0.0 fully_correct.

A first development run used `--allow-pending-review` (`promotion_eligible:
false`). The owner then signed the dual-review worksheet
(`alignment_data/REVIEW_WORKSHEET.md`), approvals were written back with
`training/mark_review.py` (120/120 rows, audit trail in `review_audit`), and
the final promotion run trained WITHOUT the override:
`training_manifest.json` records `review_override: false,
promotion_eligible: true`. If team policy requires a second independent
reviewer, co-sign the worksheet, re-run mark_review.py with both names, and
retrain per §3.

## 7. Round-4 retrain (2026-08-10)

- Data baseline: `alignment_data/reviewer_sft.jsonl` after commit `6de64c9` (120 rows, dual review complete).
- Environment: single A100-PCIE-40GB (CUDA 12.4, torch 2.6.0+cu124); 100 steps, grad accum 8; train_loss 0.593; wall time ~149.6 s.
- Heldout evaluation (30 rows, `training/evaluate_reviewer_lora.py`):
  - base: json_valid 0.9333 / category_match 0.0 / no_false_complete 1.0 / safe_action 0.0 / fully_correct 0.0
  - adapter: json_valid 1.0 / category_match 1.0 / no_false_complete 1.0 / safe_action 1.0 / fully_correct 1.0
- `training_manifest.json`: `review_override: false`, `promotion_eligible: true`.
- Evidence copies (not in Git): `models/reviewer-lora/{eval_base.json, eval_adapter.json, training_manifest.json, adapter_config.json, training_r4_report.md}`.
- Limits are the same as in section 5: a template-consistent heldout, not open-world Reviewer quality; the dual review is not an independent two-person expert panel.
