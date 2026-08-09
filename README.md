# TargetDiscovery Agent

A traceable, recoverable vertical Agent for disease-driven drug-target discovery. It connects genetics, disease-context omics, perturbation, mechanism, druggability and safety evidence into ranked candidates, TargetCards and falsifiable experiments. Internal rubrics and benchmarks are quality gates, not the product itself.

V3 adds an internal project-level reliability layer above the target-discovery runtime: typed work items, immutable artifacts, resumable execution, append-only decisions, digest-bound assessment and a narrowly scoped project repair loop. A typed transient failure in a replay-safe, side-effect-free module can produce an immutable repair request, rerun the affected subgraph, rerun project execution/integrity review and bind the release decision marker to a new snapshot. Typed domain findings trigger bounded derived-layer evidence repair: R0 claim downgrade and R1 evidence supplement run automatically, R2 evidence exclusion requires a checkpoint, and scope/truth/threshold changes are never auto-proposed; deterministic gates flag opposing effect directions and non-independent evidence. This infrastructure does not turn Target into a general-purpose scientific workbench. See [PRODUCT_V3.md](docs/PRODUCT_V3.md) for the product boundary, current capability and roadmap.

Product handoff: [next-stage PRD](PRD.md), [evidence-bounded completed capabilities](COMPLETED.md) and [offline status page](product_status.html).

## Target-discovery workflow (V2.2)

## What V2.2 does

```text
Disease -> GEO/CELLxGENE discovery -> metadata audit -> reviewed analysis recipe
        -> bulk/single-cell evidence -> pathways
        -> optional checksum-bound GWAS/SuSiE/coloc audit
        -> aggregate associations/literature/drugs/trials
        -> Reviewer -> ranking -> TargetCards -> traceable report
```

- Public contract `2.2.0` is defined by [contracts.py](src/target_agent/contracts.py); JSON Schemas are generated from Pydantic.
- The strict human-genetics lane accepts controlled GWAS summary statistics, precomputed SuSiE signal credible sets and precomputed coloc results only when checksums, study/build/ancestry links and a variant-level harmonization manifest pass deterministic gates. GWAS-only loci remain unresolved; nearest-gene mapping is forbidden.
- Open Targets genetic-association and somatic-mutation aggregates are kept distinct. Aggregate scores can add database context but cannot enter the 25-point strict human-genetics dimension or satisfy the `GO` gate.
- GEO discovery uses NCBI E-Utils and official GEO HTTPS/FTP resources.
- ClinicalTrials.gov API v2 adds gene-named trial-registry evidence (`clinical_trials_gov`); claims are emitted only when the intervention or title text explicitly names the gene, and stopped trials are downgraded to uncertain.
- The literature tool upgrades to full-text-aware RAG: open-access PMC full texts are section-parsed into a persistent shared FTS5 corpus with optional LLM reranking and bm25 fallback.
- Two execution engines ship and are parity-tested: the legacy hand-rolled state machine and the LangGraph `StateGraph` runtime (default; `--runtime legacy` opts out). Both write contract-compatible, parity-tested observable artifacts and share the same checkpoint/resume contract.
- A systematic benchmark lives in [benchmark/](benchmark/): 13 non-live internal contract/regression tasks plus 3 live-mode tasks（以 `benchmark/goldset_v2.jsonl` 为准）covering the main chain, robustness, determinism, recovery, contract gates and engine parity; `python benchmark/runner.py` must score 100% in fake+unit mode. `benchmark/results/benchmark_report.json` 已于 2026-08-09 远程重跑刷新为 13 tasks / 29 assertions（score 1.0）。This is not an external blind biological result.
- The Reviewer LoRA pipeline (data + training + heldout evaluation + remote GPU runbook) is under [training/](training/); local CPU smoke is verified, full training runs on the external GPU profile only. At runtime the trained adapter acts as an optional probe-based confirmation layer inside the Reviewer (configure `TARGET_AGENT_REVIEWER_LORA_BASE`/`TARGET_AGENT_REVIEWER_LORA_ADAPTER`): deterministic gates stay authoritative, adapter answers are category-cross-checked and silently discarded on any parse/category failure, and SFT categories are mapped onto the canonical finding taxonomy before a ReviewerFinding is emitted.
- The externally stored Reviewer adapter used in prior acceptance was trained on the earlier generic V2.1 failure taxonomy; model weights are not tracked in Git. V2.2 genetics gates are deterministic and authoritative; genetics-specific alignment examples require fresh scientific and engineering review and retraining before any model-alignment claim is upgraded.
- PyDESeq2 accepts non-negative integer counts only. Continuous expression requires the explicitly enabled fixed limma backend.
- Standard H5AD and 10x formal DE requires donor, cell type and condition metadata and runs donor-by-cell-type-by-condition pseudobulk.
- CELLxGENE Census is a separately diagnosed optional backend fixed to version `2025-11-08`; an unavailable platform wheel is reported as a capability gap.
- UC snapshots, measured T-cell perturbation and DeltaFactor remain disabled compatibility plugins, not the default workflow.
- MCH/K562 remains an isolated causal-modelling gold sample.

## Disease library

[configs/disease_library.yaml](configs/disease_library.yaml) is the curated disease library behind [diseases.py](src/target_agent/diseases.py):

- 18 diseases across autoimmune, neurodegenerative, cancer, metabolic and respiratory categories. Every `ontology_id` (MONDO/EFO) was verified against live EBI OLS search on 2026-08-04; new identifiers must pass the same live check before being added.
- Each entry carries evidence-graded reference targets (`approved_drug > gwas > mendelian > clinical_trial > mechanistic`) used as ranking sanity anchors, plus a default biological context (tissue / cell type / stage / desired phenotype).
- Four benchmark task templates follow the project 50/20/15/15 composition: `normal`, `missing_context` (blanks tissue/cell type), `conflicting_evidence` and `trap` (causal-overreach provocation). Expectations are metadata until the generated case contains an explicit executable assertion; a bucket label alone is not evidence that the behavior was detected or repaired.
- The disease resolver merges library aliases at runtime, so every id, English name, Chinese name and synonym resolves to the verified ontology identifier without touching the OLS network path.

```bash
target-agent diseases                                  # list the library
target-agent run-disease --disease uc,ra,ad            # batch-run library diseases
target-agent run-disease --disease uc --kind missing_context --summary-out batch.json
```

The same four buckets feed the benchmark: `benchmark/generate_disease_goldset.py` renders
`goldset_diseases.jsonl` (72 fake-mode entries) and `goldset_diseases_lora.jsonl`
(live matrix whose expectation-derived assertions require the Reviewer LoRA backend).
仓库门禁为 `python scripts/repo_policy_check.py` + `pytest`；首个 CI 配置见 [.github/workflows/ci.yml](.github/workflows/ci.yml)，由父任务统一评审（仓库当前不声称外部 CI 已在运行）。

The regression matrices do not measure biological ranking quality. A separate scorer-only blind
ranking protocol is documented in [benchmark/rubric.md](benchmark/rubric.md): Agent task, ranking
and terminal-status artifacts are digest-frozen before a Git-external private label file is opened,
then scored with
disease-macro nDCG/Recall/MRR and independent trap/safety gates. The scorer is implemented; an
external expert-adjudicated final label set, evaluator-controlled scorer and publishable blind
performance result are not yet available.

## Quickstart（产品路径）

完整部署（本机 pip / Docker Compose / HPC Singularity，含密钥与持久化约定）见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

1. 安装并检查环境：

```bash
python -m pip install -e ".[test,omics-bulk]"
target-agent doctor
```

2. 配置模型（复制 .env.example 为未跟踪的 .env，或注入进程环境变量）。默认使用 Step：

```bash
LLM_PROVIDER=step STEP_API_KEY=... target-agent llm-smoke-test
```

也可以接入任意 OpenAI 兼容端点：

```bash
LLM_PROVIDER=openai OPENAI_BASE_URL=https://.../v1 OPENAI_API_KEY=... OPENAI_MODEL=... target-agent llm-smoke-test
```

3. 用自然语言提出研究问题，生成可审阅的项目草案（推荐入口，不会自动执行）：

```bash
target-agent ask --question "In lung adenocarcinoma, which druggable targets are supported by public evidence?" --disease "lung adenocarcinoma"
# 审阅输出中的 review_notes / needs_review；确认后落为不可变项目：
target-agent ask --question "In lung adenocarcinoma, which druggable targets are supported by public evidence?" --disease "lung adenocarcinoma" --create --output ./my-project/project.yaml
target-agent project-run --input ./my-project/project.yaml
```

也可以直接用结构化字段初始化项目：

```bash
target-agent init --output ./my-project --disease "ulcerative colitis" --tissue colon
target-agent project-run --input ./my-project/project.yaml
```

4. 运行工作台，在浏览器中审批计划、查看结果、回退或导出；会话支持研究员/审阅者/管理员/只读查看四种角色，viewer 只读会话可提问但不能审批或补充输入：

```bash
target-agent serve --port 8888
```

5. 把整个项目导出为可移植、可校验的 zip，在另一台机器导入后继续：

```bash
target-agent project-export --project-id project-xxx --output project-xxx.target-project.zip
target-agent project-import --input project-xxx.target-project.zip
target-agent project-package-inspect --input project-xxx.target-project.zip
```

6. 生成只读分享审查页（单文件离线 HTML，无后端/网络；活项目或包均可渲染，页面带快照指纹并自动脱敏）：

```bash
target-agent share --project-id project-xxx --output project-xxx.html
target-agent share --input project-xxx.target-project.zip --output project-xxx.html
# 工作台运行栏“分享审查页”按钮即 GET /api/projects/<id>/share
```

7. 查看内置技能库（Best Practice 渐进披露，供 Planner 按需参考，不作为任务证据）：

```bash
target-agent skills list
target-agent skills search --lanes genetics
target-agent skills show --id experiment-planning
```

8. 管理持久分析内核（Python/R 会话，状态跨执行保留；仅供人工或注册工具使用，LLM 不自动执行代码）：

```bash
target-agent kernel start --language python
target-agent kernel status
target-agent kernel exec --kernel-id <id> --code "x = 5; __kernel_result__ = x * 7"
target-agent kernel stop --kernel-id <id>
target-agent kernel stop-all
```

## 双合同版本（TaskSpec 2.2.0 / ResearchProjectSpec 3.0.0）

仓库同时出现两个合同版本，分工不同：

- **科学输入合同 `TaskSpec 2.2.0`**（[contracts.py](src/target_agent/contracts.py)，`CONTRACT_VERSION=2.2.0`）：描述疾病靶点发现任务本身的输入——疾病、组织、细胞类型、阶段、表型、约束与数据偏好；JSON Schema 由 Pydantic 生成。
- **项目控制面合同 `ResearchProjectSpec 3.0.0`**（[research_contracts.py](src/target_agent/research_contracts.py)，`RESEARCH_CONTRACT_VERSION=3.0.0`）：描述不可变研究项目的控制面——goal、autonomy_mode、工作项/修复/回退预算（max_work_items / max_replans / max_forks）、workflow 模板绑定（SHA-256 冻结）与快照绑定决策。

Web 工作台能力条按“科学合同 2.2.0 · 项目合同 3.0.0”显示；创建项目时前者写入 `context.target_task_spec.contract_version`，后者写入项目 spec 顶层 `contract_version`。

## Executable workflow templates

The product is not one hard-coded pipeline. `workflows/*.yaml` are executable
contracts loaded by `WorkflowCatalog`: each template declares allowed typed
modules, required modules, dependencies, checkpoints and limits. The planner
plans inside the template, the runtime re-validates persisted plans against the
template on every resume, and a project freezes the template SHA-256 so a later
template change fails closed instead of silently altering an accepted project.

```bash
target-agent workflows list
target-agent workflows show --id disease_to_target
target-agent workflows show --id literature_review

# Disease-target template (default) and a generic literature-driven workflow:
target-agent init --output ./uc-project --disease "ulcerative colitis" --workflow disease_to_target
target-agent init --output ./lit-project --question "Evidence for IL-23 blockade in IBD?" --workflow literature_review --disease "IBD"
```

The Web workbench lists the catalog at `GET /api/workflows` and lets you pick a
workflow when creating a project. Adding a new workflow is a typed YAML file
plus registered modules; no planner rewrite is needed.

## Install

Python 3.11 is the acceptance runtime.

```bash
python -m pip install -e ".[test,omics-bulk,omics-single-cell]"
# Optional workbench integration through the official MCP Python SDK:
python -m pip install -e ".[mcp]"
# Optional only when the deployment platform supports its TileDB-SOMA wheel:
python -m pip install -e ".[omics-census]"
target-agent doctor
```

Copy `.env.example` to an untracked `.env`, or inject variables through the process environment. Process variables override dotenv values. Never commit a real key.

The skill catalog and paper-strategy hinting are configurable but work with defaults:

```bash
TARGET_AGENT_SKILL_CATALOG=skills
TARGET_AGENT_SKILL_HINT_TOP_K=3
TARGET_AGENT_PATTERN_STORE=paper_strategy/patterns.jsonl
TARGET_AGENT_PATTERN_FEW_SHOT_TOP_K=3
```

```bash
target-agent --env-file .env llm-smoke-test
target-agent run --input cases/main_demo/input.uc_demo.yaml
target-agent project-run --input cases/research_project.example.yaml
target-agent project-status --project-id project-alzheimer-example
target-agent project-repairs --project-id project-alzheimer-example
# For the example's checkpointed mode, accept the printed plan id and resume:
target-agent project-approve --project-id project-alzheimer-example --target-id PLAN_ID \
  --actor reviewer --rationale "Plan scope and evidence budget accepted" --resume
# A checkpointed repair requires the exact request snapshot digest:
target-agent project-repair-decision --project-id project-alzheimer-example \
  --repair-request-id REPAIR_ID --snapshot-digest SNAPSHOT_SHA256 --approve \
  --actor reviewer --rationale "Approve bounded same-input retry" --resume
# Fork/branch and session control plane (same service as Web/MCP):
target-agent project-branches --project-id project-alzheimer-example
target-agent project-fork-propose --project-id project-alzheimer-example \
  --target-work-item-id WORK_ITEM_ID --mode redo --actor reviewer \
  --rationale "Rerun with bounded inputs"
target-agent project-fork-decision --project-id project-alzheimer-example \
  --branch-id branch-xxx --approve --actor reviewer --rationale "Approved"
target-agent session create --project-id project-alzheimer-example --role reviewer
target-agent session list --project-id project-alzheimer-example
target-agent session post --project-id project-alzheimer-example \
  --session-id session-xxx --text "What is the status?" --ask-agent
target-agent session intervene --project-id project-alzheimer-example \
  --session-id session-xxx --action accept_checkpoint --target-id PLAN_ID \
  --actor reviewer --rationale "Approved"
target-agent serve --host 127.0.0.1 --port "$TARGET_AGENT_PORT"
```

`serve` uses Waitress. Add `--dev` only when the Flask development server is intentionally required.
The V3 HTTP surface adds `POST /api/projects`, `GET /api/projects/{project_id}`,
`GET /api/projects/{project_id}/events`, `GET /api/projects/{project_id}/activities`,
`GET /api/projects/{project_id}/repairs`,
`POST /api/projects/{project_id}/repairs/{repair_request_id}/decision`,
`POST /api/projects/{project_id}/decisions` and content-addressed artifact downloads. The activity
endpoint pages through a safe projection of the authoritative child Trace: domain stage, tool status,
coverage and source IDs are visible while candidates, evidence text and ranking values remain in the
checksum-bound scientific artifacts. `checkpointed` projects require plan and release acceptance;
`supervised` projects additionally require each work-item acceptance.
The research session surface adds `POST /api/projects/{project_id}/sessions`,
`GET /api/projects/{project_id}/sessions`, `GET /api/projects/{project_id}/sessions/{session_id}`
and `POST /api/projects/{project_id}/sessions/{session_id}/messages`. Sessions are
append-only conversation views over a durable project: messages carry content SHA-256,
and `ask_agent` returns a deterministic snapshot summary explicitly marked
`source_bound=false` — the session can never create or mutate scientific state.
Interventions close the loop from inside a session:
`POST /api/projects/{project_id}/sessions/{session_id}/interventions` accepts
`{action, rationale, actor, target_id}` with action in
`accept_checkpoint | decide_repair | decide_fork` (repair/fork decisions also
carry `approve`; repair additionally carries `snapshot_digest`). The decision is
persisted to the project ledger and, for approvals, the runtime resumes
automatically; the session records the user instruction and the decision result.
`propose_fork` supplements missing input without mutating the frozen
spec/plan: `{action:"propose_fork", target_id:"<work_item_id>", mode:"redo",
input_overrides:{...}}` creates an immutable fork branch that waits for the
same session's approve/reject decision before rerunning affected steps.

The optional MCP server exposes the same durable project service to Codex, SciForge,
OpenScience, Wisp and other MCP hosts over stdio or the official Streamable HTTP transport:

```bash
target-agent mcp-serve                                  # stdio (default)
target-agent mcp-serve --transport streamable-http --host 127.0.0.1 --port 8000 --path /mcp
# equivalent dedicated entry points:
target-agent-mcp
target-agent-mcp --transport streamable-http --port 8000
```

It provides typed operations to create a disease project, advance it to the next checkpoint,
inspect state/events/domain activities and repair records, approve or reject one exact repair snapshot,
accept a frozen checkpoint and read checksum-verified text artifacts. Session tools
(`target_create_session`, `target_list_sessions`, `target_read_session`,
`target_post_session_message`, `target_session_intervene`) let an external workbench drive
the conversation/approval loop with role-aware sessions (viewer sessions are read-only).
It does not expose arbitrary shell or model-generated code execution. Remote registry
publication and host-specific installation bundles remain future integration work.

## Demo workbench（真实演示路径）

The repository does not ship stored-replay demo data; `/api/demo/*` and `/api/runs/*` are legacy endpoints not used by the workbench. The honest presentation paths are:

- **Live project:** `target-agent serve --port 8888`, create or open a durable project, and walk through plan/session approvals, results, branches, artifacts and evidence graphs. Live runs may call Step and public databases.
- **Offline review page:** `target-agent share --project-id <id> --output <id>.html` (or `--input <package>.zip`) renders a single-file, offline, network-free HTML snapshot with a SHA-256 fingerprint and scrubbed secrets — suitable as a network-free fallback and for reviewers.

See [DEMO_GUIDE.md](docs/DEMO_GUIDE.md) for the presentation path, verification checklist and recovery tips.


### One-command start and OS-keyring secrets

```bash
target-agent up --port 8888          # doctor checks, then Waitress workbench
target-agent secrets status          # keyring backend + configured/not configured
target-agent secrets set STEP_API_KEY --value 'sk-...'   # or omit --value and paste on stdin
target-agent secrets delete STEP_API_KEY
```

Secret resolution is process environment > `.env` > OS keyring
(`pip install -e ".[secrets]"` enables the keyring backend). `doctor` reports
the backend and per-key configured state without printing values.
`tests/test_product_acceptance.py` is the product-level gate: it drives one
checkpointed project through the Web API, session approvals, completion,
session summaries, export, read-only checksum verification and import into a
second store — proving the control plane, session layer and share packages are
one closed product loop.## Deployment portability

The repository contains no SSH target, remote path, Conda environment, scheduler queue, node, core count, GPU selection or service tunnel. Those values belong in an external deployment profile. Missing resource fields must fail rather than be guessed. See [REMOTE_ACCEPTANCE.md](docs/REMOTE_ACCEPTANCE.md).

## Scientific boundaries

- `FACT`, `OBSERVED`, `PREDICTED` and `INFERRED` are never conflated.
- Search hits are not evidence until an exact source span or reproducible analysis output exists.
- Low-context predictions are excluded from formal ranking.
- Missing public omics data degrades to `completed_with_gaps`; genetics, literature and drug evidence continue.
- Reports and the UI render structured Evidence Store values only.
- File-based analysis inputs and generated artifacts are checksum-bound. API evidence instead carries source locators, retrieval/tool-run provenance and available source-version metadata; analysis caches also bind the recipe, tool version, biological context and contract version.
- Raw FASTQ/SRA, arbitrary GEO layouts, statistical fine-mapping/coloc recomputation, spatial analysis, patents and automatic code/training mutation are outside V2.2.

## Repository policy

Run all project tests and builds in the user-supplied remote execution profile. Large data, caches, models, `.env` files and deployment profiles stay outside Git.
