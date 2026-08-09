# V2.1 historical baseline and V2.2 acceptance report

This file records portable acceptance facts only. Infrastructure identifiers are retained in the external execution profile and private job logs.

The V2.1 checks and evidence below are a historical baseline; they do not by themselves validate the current V2.2 branch. Current-commit V2.2 evidence is recorded in a separate section after remote acceptance.

## Required checks

- [x] Contract and generated-schema consistency.
- [x] Explicit 2.0-to-2.1 TaskSpec migration and mixed-version rejection.
- [x] Generic Planner uses the live typed registry.
- [x] `.env` auto-loading and process-environment precedence.
- [x] Step structured planning without fallback, including request metadata.
- [x] Dynamic GEO discovery and metadata eligibility gates.
- [x] PyDESeq2 rejection of continuous/non-integer expression.
- [x] Single-cell donor/pseudobulk metadata gates and synthetic execution.
- [x] Evidence-to-ToolRun-to-Trace referential integrity.
- [x] Missing omics data returns `completed_with_gaps` while other evidence chains continue.
- [x] Formal Waitress launch plus live health and capabilities endpoints.
- [x] Repository contains no secret or infrastructure-specific value.
- [x] ClinicalTrials.gov evidence tool: gene-name-gated claims, stopped-trial downgrade, cache-only degradation.
- [x] Literature RAG upgrade: PMC full-text sections in a persistent shared FTS5 corpus; LLM reranking falls back to bm25.
- [x] LangGraph runtime parity: identical observable output, budget degradation, terminal and mid-pipeline resume vs the legacy engine.
- [x] Internal contract/regression benchmark: 14 tasks, fake+unit modes score 100% (27/27 assertions). This is not an external blind biological result.
- [x] Reviewer LoRA: data gates enforced, CPU smoke train/save/load/evaluate verified, remote GPU runbook documented.

## Latest portable evidence

- 33 remote-node tests passed, including unfinished-run identity recovery, stable checksum-bound cache keys, one bounded structured-output repair, request-ID capture, generic Planner routing, non-integer DESeq rejection and donor-level pseudobulk execution.
- Repository policy scan passed and all 16 generated JSON Schemas contain contract `2.1.0` only.
- Alignment factory produced 120 SFT rows, 60 preference pairs and 30 held-out acceptance rows with role-based review gates. The 120 SFT rows represent six reviewed templates with 20 indexed variants each; they are not 120 independent target-research cases.
- Live cold-search probes discovered disease-specific GEO candidates for Alzheimer disease and lung adenocarcinoma without an accession in TaskSpec.
- GSE318560 completed an Alzheimer-disease PyDESeq2 run; GSE104854 completed the independent lung-adenocarcinoma gold-input PyDESeq2 run.
- Alzheimer disease selected GSE248417/GSE318560 dynamically: cold run 47 seconds; cached runs 20, 7 and 5 seconds with identical status and rankings.
- Lung adenocarcinoma selected GSE310170 dynamically: cold run 79 seconds; cached runs 19, 8 and 8 seconds with identical status and rankings.
- UC selected GSE177044 dynamically but had no eligible automatic bulk result, so it correctly returned `completed_with_gaps`; cached runs took 7, 7 and 6 seconds with identical status and rankings.
- A scheduled-node Waitress process passed live `/healthz` and `/api/capabilities` checks using the externally supplied loopback bind and service port; the process was stopped after acceptance.
- A real Step 3.7 Flash smoke test produced a valid 14-step plan without fallback in 23.0 seconds; the provider request ID was captured without exposing the API key.
- Historical workstation diagnostic after V2.1 (not a release acceptance): 45 tests passed and 2 skipped; `python benchmark/runner.py` scored 27/27 assertions across 11 executed tasks (3 live tasks recorded separately); a live ClinicalTrials.gov probe returned 12 gene-named evidence items for EGFR/KRAS in lung adenocarcinoma; the LangGraph engine matched the legacy engine on ranking, evidence, findings, tool results and trace topology; the Reviewer LoRA CPU smoke (Qwen3-0.6B, 1 step) wrote a loadable adapter and the heldout evaluator parsed every generation as valid JSON.
- Reviewer LoRA full training completed on the external GPU profile (single H100, Qwen3-8B, 100 steps, ~2.5 min, train_loss 0.57). Heldout acceptance (30 rows, per training/RUNBOOK.md gates): base model json_valid 0.933 / category_match 0.0 / fully_correct 0.0; adapter json_valid 1.0 / category_match 1.0 / fully_correct 1.0. The adapter beat the base model on this 30-row template-consistent contract heldout set; this does not establish open-world Reviewer quality. Role-gate review was recorded by one project owner acting in both scientific and engineering roles, so it is not a two-person independent review or expert panel. The promotion run trained without `--allow-pending-review`, so the manifest records `promotion_eligible: true`. Model weights and evaluation outputs were external, Git-excluded acceptance artifacts under `models/reviewer-lora/`.
- Disease library (2026-08-04): 18 curated diseases in configs/disease_library.yaml; every ontology identifier (MONDO/EFO) passed a live EBI OLS exact-label verification on 2026-08-04. Reference targets are evidence-graded and machine-checked by tests (unique ids, CURIE format, >=2 graded targets per entry). Four task templates encode the 50/20/15/15 composition (normal / missing_context / conflicting_evidence / trap) with machine-checkable expectations. `target-agent diseases`, `target-agent run-disease` and `/api/diseases` expose the library; the disease resolver merges library aliases at runtime. Local regression: 59 passed, 2 skipped.
- Reviewer LoRA live end-to-end (2026-08-04, external GPU profile): `target-agent run-disease --disease uc` with the trained adapter configured completed the full live pipeline (12 tool calls, terminal `completed_with_gaps`; archived at runs_archive/run-lora-e2e3-uc/). Trace records `reviewer_backend: lora:reviewer-lora` and the adapter emitted a confirmed finding (`major/context_mismatch`, "LoRA reviewer confirmed out_of_distribution"). Two earlier attempts exposed real defects that are now fixed and regression-tested: GPU OOM degradation (all node GPUs occupied by other tenants; reviewer ran on CPU) and a category-mapping bug where confirmed SFT-only categories (missing_context/out_of_distribution/correct_refusal) crashed ReviewerFinding construction and silently degraded to `deterministic:lora_unavailable`; SFT categories are now mapped onto the canonical taxonomy via FINDING_CATEGORY_MAP. The graceful degradation itself behaved as designed: both degraded runs still completed with the deterministic reviewer.
- Disease-library benchmark goldsets (2026-08-04): benchmark/generate_disease_goldset.py renders 72 fake entries (18 diseases x 4 buckets) scoring 234/234 assertions = 1.0 across all four buckets, and 72 live LoRA entries; the main goldset still scores 27/27 = 1.0. These cases validate contract behavior, refusal and recovery of known reference anchors; they are not a blind target-discovery accuracy result.
- Live disease-library matrix (2026-08-04, external GPU profile, LoRA reviewer configured; artifacts in runs_archive/): Alzheimer disease `completed_with_gaps` (top-3 PSEN1/APP/PSEN2, reviewer_backend lora:reviewer-lora); UC missing_context bucket `completed_with_gaps` with the adapter confirming `missing_context` (canonical coverage_gap) — the exact assertion encoded in goldset_diseases_lora.jsonl; UC trap bucket `completed_with_gaps` with ZERO causal FACT/OBSERVED claims despite the causal-provocation question (top-3 unchanged IL12B/IL23R/IL10); first LUAD attempt hit transient GEO/Open Targets network failures and degraded to `completed_with_gaps` with the adapter confirming `tool_failure` (evidence marked missing, nothing fabricated); the LUAD rerun completed the full pipeline with 10 ranked targets (top-3 EGFR/TP63/NRG1, EGFR being a library approved-drug reference anchor) and `reviewer_backend: lora:reviewer-lora`.
- Full live disease-library matrix (2026-08-04/05, external profile, 4-way sharded runner with `--shared-cache` and per-entry crash isolation, LoRA reviewer on CPU): all 72 entries (18 diseases x 4 buckets) executed live against `benchmark/goldset_diseases_lora.jsonl` with final score 72/72 tasks and 270/270 assertions = 1.0, zero crashes. Per-bucket assertion scores were normal 72/72, missing-context 72/72, conflicting-evidence 54/54 and trap 72/72. Important qualification: the historical conflicting-evidence cases asserted only terminal status, evidence provenance and report existence; their `expectation.reviewer_categories` field was metadata and was not executed by that runner. Therefore 54/54 does **not** prove that conflicts were detected, preserved or repaired. The runner now supports explicit `finding_category` assertions for future executable cases. Normal-bucket outputs recovered known reference anchors across the disease library, but this remains a contract/reliability matrix with known anchors, not external blind biological validation. Artifacts were retained outside Git under the acceptance run archive.

## Current environment-specific gaps

- The validated node exposes Scanpy 1.11.5 and AnnData 0.12.16. Its operating-system ABI has no compatible TileDB-SOMA wheel required by `cellxgene-census==1.17.*`, so Census is correctly reported unavailable rather than silently downgraded.
- The optional fixed limma backend is not declared ready because neither Rscript nor the limma package is present.
- The historical Reviewer adapter was promotion-eligible under the then-current two-role gate with no training override. Both roles were completed by one owner, so this is not independent dual-person review. If team policy requires two reviewers, record separate role identities outside public artifacts, re-run `training/mark_review.py`, and retrain per `training/RUNBOOK.md` §3.

The checkboxes above record the historical V2.1 remote-node baseline, not current V2.2 acceptance.

## V3 project-control-plane acceptance (2026-08-05)

- Full remote pytest suite passed; two pre-existing model-dependent cases were skipped by their declared capability gates.
- The internal V2.1 benchmark remained at 11/11 tasks and 27/27 assertions after adding the project control plane.
- Bounded Reviewer repair was exercised against a transient Europe PMC connector failure: the runtime retried the allowlisted read-only tool, appended both attempts to the evidence ledger, emitted replan/re-review trace events and removed the recovered `tool_failure` from the final findings. Cache-only execution correctly performed no retry.
- Reviewer repair remains deliberately narrow: it cannot execute arbitrary code or mutate scientific methods, and it stops at the configured review-round and tool-call budgets.
- A real Step 3.7 Flash request produced the protected four-item vertical plan whose labels included project brief, target discovery, independent review and report. This validates plan structure only; it does not prove model- or expert-independent scientific review.
- The checkpointed smoke project stopped at `needs_input` before any scientific module ran and recorded the exact plan identifier requiring human acceptance.
- Canonical Pydantic export produced 11 new research-project JSON Schemas under `schemas/`.
- Repository policy scan returned `REPO_POLICY=OK` after generated runtime outputs were kept under excluded artifact storage.
- Acceptance covered atomic project reservation, cross-process execution locking, terminal-resume integrity checks, protected planner contracts, input/output alignment gates, dependency failure, attempt budget, content-addressed artifact versions, fail-closed target deliverables and plan/release checkpoints.

## Internal blind-ranking scorer protocol acceptance — no external biological result (2026-08-05)

- The full remote test suite passed after adding the scorer contracts; two pre-existing model-dependent cases remained skipped by their declared capability gates.
- Existing regression quality gates remained at 11/11 tasks and 27/27 assertions, and repository policy returned `REPO_POLICY=OK`.
- The reference scorer freezes `task_spec.json`, `ranked_targets.json` and terminal `status.json` with individual and combined SHA-256 digests before private labels are loaded.
- Tests cover graded nDCG/Recall/MRR, non-compensating trap and safety gates, candidate-label leakage, post-freeze ranking tampering, duplicate predictions, empty rankings, malformed-case isolation, explicit suite thresholds and expert-label schema/gate logic using development fixtures.
- Public score output is aggregate-only; per-case label signals are available only through an explicit organizer-audit path.
- Two canonical JSON Schemas were exported for the public manifest and private-label contracts.
- This acceptance validates the protocol and participant-side reference implementation only. It is not an external blind biological result; evaluator-controlled code, hidden diseases, expert labels and submission controls are still required.

## V2.2 controlled-genetics source acceptance (2026-08-05)

- Source commit `920ed28` was archived and executed only in the external scheduled-node acceptance environment. The witness reported Python 3.11.13, the configured isolated Conda environment, the requested node and 35 allocated CPU slots; infrastructure identifiers remain outside Git.
- The focused genetics/contract/runtime/Web suite passed 105/105 tests.
- The full suite passed 202 tests with two optional model-backed Reviewer tests skipped by their declared capability gate. The only emitted warnings came from the existing synthetic single-cell/PyDESeq2 fixtures.
- The internal fake/unit benchmark passed 11/11 tasks and 27/27 assertions. It validates contracts, recovery, deterministic behavior and refusal boundaries; it is not an external blind biological result.
- Canonical export produced 38 JSON Schemas. `TaskSpec` and the target-discovery contracts use `2.2.0`; the genetics evidence schema and `multi_evidence` tool descriptor were explicitly witnessed.
- The repository policy scan returned `REPO_POLICY=OK` after benchmark reports were changed to store portable path labels rather than deployment-specific absolute paths.
- Acceptance covered strict GWAS candidate-universe freezing, checksum/provenance lineage, valid fine-mapping plus colocalization gates, PP4/context/stance thresholds, upstream Reviewer blockers, tool-budget degradation, terminal-status witness checks, duplicate-ID rejection and Legacy/LangGraph parity.
- The controlled lane audits supplied, pre-staged GWAS, SuSiE and coloc outputs. It does not recompute fine-mapping/colocalization, establish causal genes or therapeutic direction, validate an external hidden benchmark, provide an independent expert panel, provide remote/multi-user MCP deployment, or constitute a large open-world alignment-data moat.

## Durable product service and stdio MCP acceptance (2026-08-05)

- The project-facing application boundary is now `ResearchProjectService`. The HTTP workbench and optional MCP adapter read and mutate the same immutable project specification, frozen plan, ordered events, decisions, assessments and content-addressed artifacts.
- Disease-project intake preserves omitted tissue, cell type, stage and phenotype as missing. It does not infer biological scope from prose before the scientific workflow can assess or request it.
- The official MCP Python SDK `2.0.0` was installed through the optional `mcp` extra. Its in-memory client exercised project creation and execution against the real durable service.
- A separate stdio subprocess smoke completed protocol initialization, discovered all eight Target tools and called `target_capabilities`; the witness was `TARGET_MCP_STDIO=OK`.
- The focused research-service/runtime/Web suite passed. The complete suite passed 207 tests with two optional model-backed Reviewer tests skipped and three existing synthetic omics warnings.
- `pip check` reported no broken requirements, canonical Schema export remained successful and the repository policy returned `REPO_POLICY=OK`.
- This acceptance proves the local stdio product adapter and shared durable semantics. It does not prove Streamable HTTP MCP, registry publication, multi-user authorization, external-host compatibility matrices, external blind target-ranking quality or independent expert review.

## Observable child workflow and domain-activity acceptance (2026-08-05)

- Package `0.7.0` adds a separate project domain-activity ledger and paged HTTP/MCP access. Each activity links to the exact child TraceEvent and the content-addressed `target_discovery_trace` artifact.
- The projection whitelist exposes stage, tool status, coverage and source IDs; tests verify that candidate genes, highlighted targets, provider request metadata and other scientific payloads are absent.
- `reviewer_repair` tool calls and replans are classified as reliability-review activities. Pre-run and post-run reconciliation is idempotent, and observer failure does not change the authoritative child terminal status.
- The focused projection/store/runtime/service/Web recovery suite passed 36 tests. The complete suite passed 218 tests with two optional model-backed Reviewer tests skipped and three existing synthetic omics warnings.
- Canonical export produced 41 JSON Schemas, including typed project snapshot and activity-page responses. The stdio protocol smoke discovered nine Target tools and returned `TARGET_MCP_STDIO=OK`; `pip check` and repository policy both passed.
- This increment makes the existing bounded child Reviewer repair observable. It does not implement mutable project plans, general project-level replan, arbitrary evidence supplementation or external biological validation.

## Constrained project repair and product handoff acceptance (2026-08-05)

- Package `0.8.0` adds a deliberately narrow project-level execution repair path. A typed transient failure must be bound to a project execution/integrity FAIL assessment, identical effective input digest and a module declaring `side_effect_free`, `replay_safe` and `same_input_retry` before policy may create a repair request.
- Repair requests, plan-revision overlays and repair resolutions are immutable records. The original result, assessment and artifact history remains available; runtime finalize and release-digest computation use the logical active item set.
- Autonomous mode reruns the affected successor subgraph within budget. Checkpointed/supervised modes require approval of the exact trigger snapshot; stale approval is rejected. Every applied repair reruns assessment and changes the release decision marker target.
- The full remote suite collected 229 tests and completed with 227 passed and two optional model-backed tests skipped by their declared capability gates. The only warnings came from the existing synthetic single-cell/PyDESeq2 fixtures.
- The internal fake/unit benchmark remained at 11/11 tasks and 27/27 assertions. The runner now reads the authoritative `reviewer_findings.jsonl` ledger and supports an explicit `finding_category` assertion.
- Canonical export produced 45 JSON Schemas. The stdio MCP smoke discovered 11 Target tools and returned `TARGET_MCP_STDIO=OK`; `pip check` and repository policy returned clean/`REPO_POLICY=OK`.
- Historical disease-matrix correction: its conflicting-evidence bucket asserted terminal status, provenance and report existence only. The recorded 54/54 score did not verify conflict detection, preservation or repair and must not be used as that claim.
- This increment does **not** implement science-finding-driven evidence repair, dataset switching, method changes, general DAG replanning, WorkAttempt/Head, a certified release package, external blind biological validation or independent expert release.


## Paper RAG and few-shot Planner acceptance (2026-08-08, P2.6)

- New paper-level RAG layer (src/target_agent/paper_rag.py): bounded abstract
  chunks with per-chunk SHA-256 digest, append-only JSONL store and a
  MANIFEST. Retrieval is deterministic lexical scoring (disease tokens, query
  tokens, available evidence lanes, recency, journal premium); no embedding
  model and no network at query time. Methods/full text is never persisted.
- PlannerFewShotBuilder.build_paper_evidence() now injects top chunks into the
  domain Planner and the project ResearchPlanner; ResearchPlan persists
  paper_evidence, planner_backend records +paper-rag:N, and the LangGraph
  runtime traces planner_paper_evidence. The Web workbench shows the hits in a
  dedicated panel labelled "strategy hint, not evidence".
- Seed corpus refreshed on the remote workspace: 155 chunks from 59 recent
  (2025-2026) Nature/Science/Cell-family papers; MANIFEST is committed so
  chunk checksums are reproducible. The full candidate pool can be expanded
  with target-agent pattern rag refresh or scripts/build_paper_rag.py.
- Full remote acceptance passed: 360 tests collected with no failures (two
  pre-existing optional model-backed tests skipped by their capability gates),
  internal benchmark 12/12 tasks with 28/28 assertions, canonical schema
  export regenerated all schemas, and repository policy returned
  REPO_POLICY=OK; the acceptance script printed TARGET_P02_ACCEPTANCE=OK.
- Alignment-data generation and Planner/Reviewer small-model training remain
  deferred (P3) per the team decision; RAG hits and pattern library are the
  future alignment-data source.
## Paper RAG in mechanism graph and RAG ablation acceptance (2026-08-08, P2.7)

- Mechanism graph now projects paper-RAG hits as strategy_paper nodes and paper_strategy_hint edges: claim_class=INFERRED, weight=0, empty evidence_ids, attributes strategy_only/not_evidence. Gene mention matching is deterministic (token-boundary) and malformed rows / unknown genes are skipped. RAG hits never alter lane_coverage, direction conflicts, dependence findings, pattern_links or ranking. GraphNode.node_type was extended with strategy_paper (additive literal).
- Web workbench mechanism panel shows a paper_strategy_hints metric and an explicit note that RAG hits are strategy hints, not evidence and not ranked.
- benchmark/pattern_ablation.py gained --rag/--paper-top-k; offline report now includes RAG hit counts, coverage, average hits and lane-alignment per disease. Benchmark goldset gained BM-13 (paper_rag_graph_projection unit check).
- Final remote acceptance (external GPU node, pinned conda environment, 35 cores): internal benchmark 13/13 tasks, 29/29 assertions, score 1.0; repository policy REPO_POLICY=OK; canonical schema export regenerated; TARGET_P02_ACCEPTANCE=OK.
- RAG coverage ablation on the current 155-chunk / 59-paper seed corpus: 16/18 diseases with at least one hit (88.9%), 2.5 average hits per disease, 15/18 diseases with RAG lanes aligned to the deterministic plan. UC and psoriasis currently have no hits; they are the priority targets for the next gold-paper curation batch.
- Teammate context-relation benchmark (PR 12; 145 cases, disease-disjoint splits, scoped contrastive labels) reviewed for sensitive content and merged into the product branch as an evaluation asset. Its main-branch PR remains draft until the author marks it ready.
## Gold-paper nomination acceptance (2026-08-08, P2.8)

- New deterministic nomination layer (src/target_agent/gold_nomination.py):
  ranks candidate-corpus metadata by journal premium, query bucket, title
  lane signals (genetics / perturbation / single_cell / mechanism /
  target_drug), RAG gap-disease bonus (UC, psoriasis, SLE, ALS, melanoma)
  and a basic-biology-only penalty. No model, no network, fully
  reproducible; a nomination never writes to the curation ledger.
- CLI `target-agent pattern nominate --corpus --out --limit --min-score
  --year-min`; output `paper_strategy/nominations.jsonl` (40 advisory
  rows, each with a self-consistent digest) plus per-line SHA-256
  `nominations_MANIFEST.json`. Gold status still requires dual-role
  `target-agent pattern curate` confirmation.
- Pattern CLI dispatch fixed: curate/review/nominate previously crashed
  on unconditional `args.store` access; a regression test now runs
  `pattern nominate` end to end.
- Final remote acceptance (external GPU node, pinned conda environment,
  35 cores): full pytest 374 collected, 372 passed, 2 pre-existing
  capability-gated skips, no failures; internal benchmark 13/13 tasks,
  29/29 assertions, score 1.0; canonical schema export regenerated;
  repository policy REPO_POLICY=OK; nomination witness
  NOMINATION_WITNESS=OK; TARGET_P28_ACCEPTANCE=OK.
- Nomination gap audit on the current 200-record corpus: 105 eligible
  records; only two gap-disease hits exist in the pool (melanoma PMID
  42556334 / 41606121). UC, psoriasis, SLE and ALS papers are absent
  from the candidate corpus, which explains the P2.7 RAG zero-hit rows;
  the next corpus refresh must add targeted queries for these diseases
  before re-nominating.
- Alignment-data generation and Planner/Reviewer small-model training
  remain deferred (P3) per the team decision.
## Natural-language question intake acceptance (2026-08-08, P2.9)

- New product entry point (src/target_agent/question_intake.py): a free-form
  research question becomes a reviewable draft ResearchProjectSpec; nothing
  is reserved or executed until a human creates the project.
- Field precedence is deterministic: explicit hints > curated disease
  library (canonical name + MONDO/EFO ontology id) > LLM proposal > missing.
  Missing context stays missing; the library benchmark context is reported
  as a suggestion in review_notes and never injected.
- Safety gates: credential-like tokens and unresolvable diseases are
  rejected (CLI non-zero exit / Web 422); LLM failures fall back to the
  deterministic path with an explicit llm_unavailable note.
- Surface: CLI `target-agent ask` (--create reserves only, --output writes
  the draft YAML) and Web `POST /api/questions`; the workbench "new project"
  panel now accepts a natural-language question and decodes it into the
  reviewable form.
- Final remote acceptance (external GPU node, pinned conda environment,
  35 cores): full pytest 384 collected, 382 passed, 2 pre-existing
  capability-gated skips, no failures; internal benchmark 13/13 tasks,
  29/29 assertions, score 1.0; live Step ask resolved lung adenocarcinoma
  to MONDO:0005061 with matched=true and created=false; deterministic
  ask witness OK; repository policy REPO_POLICY=OK;
  TARGET_P29_ACCEPTANCE=OK.

## Round-4 acceptance (2026-08-10)

- Full remote pytest: 571 passed / 2 skipped / 3 warnings (before and after the alignment-environment upgrade).
- Internal non-live benchmark: 14 tasks / 30 assertions / score 1.0 (BM-01..BM-14, fake+unit modes); this is not an external blind biological result.
- Schema export: 55 generated schemas + 1 hand-maintained `context_relation_case.schema.json` = 56 consistent.
- `scripts/repo_policy_check.py`: REPO_POLICY=OK; GitHub Actions CI on main `f4e2ece`: success.
- User control plane (pause/cancel/resume): HTTP/CLI/MCP/Web, safe-boundary consumption, cancel is terminal and produces no report; round-4 focused tests 14 passed.
- Reviewer LoRA retrain on the current dual-reviewed 120-row SFT set (data baseline commit `6de64c9`): adapter heldout 30 rows = 1.0 on all four rubric dimensions and fully_correct; base = 0.0 category_match/safe_action/fully_correct; `training_manifest.json` promotion_eligible=true; weights are not tracked in Git. This is a template-consistent contract heldout and does not establish open-world Reviewer quality.
