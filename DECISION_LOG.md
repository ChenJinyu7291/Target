# Decision log

Cross-module contracts, workflow choices, model boundaries and scientific-safety decisions are recorded here. Accepted decisions must not be changed silently in a feature branch.

## 2026-08-09 - Round-2 全项目审计修复与 Round-3 产品收口

- **Status:** accepted
- Round-2 四份审计（产品/科学/架构/安全）及修复清单见 `../review/`；修复已随 `2c4bc76` 合入 main：CLI fork/branch/session 子命令、Web 错误 detail 展示/创建防抖与二次确认/审批角色接线/合同能力条、文档一致性、首个 GitHub Actions CI 门禁、benchmark 13/29 刷新。
- 修复闭环实质复查按 finding 实际类别执行，纯 coverage-gap 补充不再被 missing-provenance 原始质症检查误拦截；restore fork 的 head-backed 恢复 attempt 记入活动 fork 点（基础项单条历史 attempt、活动 fork 点两条）。
- Streamable HTTP MCP 状态统一为：基础 transport 已完成（本地/可信网络绑定）；认证、多租户与生产级发布未完成。
- 对齐数据生成与 Planner/Reviewer 小模型训练（P3）继续延后，先以 RAG + few-shot + 确定性规则验证策略价值。
- Round-3 产品收口：Dockerfile 与 Singularity 默认数据目录与 `/data` 卷对齐（裸 `docker run -v target-data:/data` / `--bind ...:/data` 持久化生效）；README benchmark 表述明确 13 非 live + 3 live 及报告范围；Web fork 发起与会话消息 actor 从会话角色派生；PRD 标记 P0 已完成并明确下一阶段优先级；product_status.html 修正重复条目并刷新时间戳。
- 未决：长任务取消/暂停（P2-2）需要 runtime 协作取消与 Web/MCP/CLI 接口，本轮不实现，作为下一轮架构+产品联合项。

## 2026-08-10 - Round-3 科学线与质量线收口

- **Status:** accepted
- 遗传学车道：非回文 EAF 一致性校验（swapped 用 1-EAF 互补，阈值 0.15），context 匹配输出 matched_terms/match_rules 明细，coloc/harmonized 列映射支持可选 EAF。
- 决策语义：强反对 FACT/OBSERVED 方向性证据与已知安全阻断不再被总分隐藏，直接 NO_GO；Reviewer 因果语言扫描扩展到 uncertainty/effect，并扩充中英文因果词表。
- 修复控制面：子上下文拆分必须锚定证据自身上下文；数据集替换按 context match/元数据置信度/独立样本量择优。
- 外部快照可审计：Open Targets 与 ClinicalTrials 缓存信封化（retrieved_at + response_digest），证据 version_meta 记录时间戳/摘要。
- 组学护栏：疑似原始整数计数拒绝进入固定 limma，归一化不可自证时标记 normalization_unverified；GEO 元数据审查增加 barcode 识别、单元覆盖率与疾病同义词命中检查。
- 质量门：CI 收集必红修复（pandas 等可选依赖函数内懒加载）；新增 60 个测试覆盖候选门禁、resume 共享校验、NO_GO、快照元数据等；schema 重导出 54 个生成 + 1 个手写基准。
- 远程验收：pytest 559 collected / 557 passed / 2 skipped；benchmark 13/29 score 1.0；repo_policy_check OK。
- 未决不变：外部独立 scorer/专家盲测标签、认证与多租户、长任务取消/暂停（P2-2）留待下一轮。

## 2026-08-08 - PR 12 合并与产品发布路径收口

- **Status:** accepted
- PR #12（contextual disease-target relation benchmark，939d9b9）通过远程验收（4/4）与静态审查后，按用户"审查并合并队友 PR"的要求合入 main（ffe8801）。
- 该基准已同时存在于产品分支（p2.7 合并），PR #11 合入 main 后不会遗漏该资产；PR #11 与最新 main 经 merge-tree 验证无冲突。
- 门户脱敏修复（保留证据来源 URL，不再误伤 https://）通过远程全量 pytest（430 passed），已推入产品分支并随 PR #11 发布。
- PR #11（产品化核心，87 提交）保持 draft，由用户确认后合入 main；对齐数据训练（P3）按用户要求延后到最后阶段。




## 2026-08-08 - 自然语言问题录入（P2.9）

- **Status:** accepted
- 决策：产品入口接受自然语言研究问题，但只生成可审阅草案（draft），绝不自动创建或执行项目；创建前必须由人确认 `needs_review` 与 `review_notes`。
- 决策：字段优先级固定为 显式 hints > 策展疾病库（规范名/ontology）> LLM 提案 > 缺失；库默认上下文只作为 suggestion 出现在 review_notes，禁止自动注入，避免把 benchmark 上下文误当真实生物学上下文。
- 决策：LLM 只输出结构化 brief 并经 Pydantic 校验；不可用/非法时确定性回退，问题文本含凭据类 token 或无法建立疾病时直接拒绝（CLI 非零退出 / Web 422）。
- 决策：入口同时提供 CLI `ask` 与 Web `POST /api/questions`；`--create` 仅 reserve 不可变项目，与执行完全分离。
## 2026-08-08 - Gold 论文提名工具（P2.8）

- **Status:** accepted
- 决策：提名阶段与 curation 阶段严格分离。提名工具对候选语料做确定性、元数据级排序（期刊权重、查询桶、标题证据层信号、RAG 缺口疾病加分、基础生物学惩罚），无模型、无网络；提名只输出 advisory shortlist，绝不写入 curation 台账。
- 决策：提名输出采用 append-ready JSONL + 逐行 SHA-256 manifest，每条记录带自洽 digest；CLI `pattern nominate` 提供 `--corpus/--out/--limit/--min-score/--year-min`。
- 决策：RAG 覆盖率缺口疾病（UC、银屑病、SLE、ALS、黑色素瘤）在提名打分中加权，确保下一批人工 curation 优先补位，但论文最终 gold 判定仍以双人评审为准。
- 决策：对齐数据生成与 Planner/Reviewer 小模型训练（P3）继续按团队决定最后再做，提名与 gold 评审结果作为其未来数据来源。
## 2026-08-08 - 论文RAG命中接入机制证据图与盲测RAG覆盖率（P2.7）

- **Status:** accepted
- 决策：Paper-RAG 命中以 strategy_paper 节点和 paper_strategy_hint 边投影进机制证据图，全部标记 strategy_only/not_evidence、claim_class=INFERRED、weight=0、evidence_ids 为空；基因提及匹配为确定性 token 边界匹配，畸形行与未知基因直接跳过。
- 决策：RAG 命中绝不改变 lane_coverage、方向冲突、证据依赖、pattern_links 或排名；机制图新增 paper_strategy_hints 统计与明确 limitation，Web 工作台“机制证据图”面板新增该指标并标注“仅作策略提示、不是证据、不进入排序”。
- 决策：benchmark/pattern_ablation.py 新增 --rag 离线分析，报告各疾病 RAG 命中数、覆盖率与“RAG lane 与确定性计划 lane 对齐”指标；基准 runner 新增单元检查 paper_rag_graph_projection。
- 决策：队友提交的上下文关系基准（PR 12，145 例疾病-靶点-组织-细胞-阶段 goldset）经审查无敏感信息后并入产品分支，作为后续 Planner/RAG 适配器的评测资产；评审标签保持 scoped（上下文错配不等于生物负例），其 PR 在 main 上仍为 draft，由作者决定何时就绪。
## 2026-08-08 - 论文摘要 RAG 与 Planner few-shot 增强（P2.6）

- **Status:** accepted
- 决策：新增论文摘要级 RAG 层（src/target_agent/paper_rag.py），对近年 CNS 论文的公开摘要做有界分块并以 append-only JSONL + digest 存储；检索保持确定性词法评分（疾病/查询/数据可得性/年份/期刊），运行时无模型、无网络。
- 决策：只持久化摘要分块；Europe PMC 的 Methods/全文仅在模式抽取时驻留内存，绝不落盘，延续“不存全文”的既有边界。
- 决策：PlannerFewShotBuilder 同时提供模式 few-shot 与论文 RAG 证据；领域 Planner 与项目 ResearchPlanner 把 paper_evidence 注入 prompt，写入 ResearchPlan.paper_evidence 与 planner_backend 后缀，并以 planner_paper_evidence trace 持久化命中，Web 工作台展示“论文证据（RAG）”面板并标注“策略提示非证据”。
- 决策：对齐数据生成与 Planner/Reviewer 小模型训练（P3）继续按团队决定延后到最终阶段；RAG 与 few-shot 先验证策略价值，其命中记录作为未来对齐数据的可追溯来源。
- 验收：新增 paper_rag 合同/检索/few-shot/两端 Planner 集成测试；全套 pytest、benchmark 与 schema 导出须在远程验收环境通过后再合并。

## 2026-08-08 - 机制证据图与证据合成质量门（P2.5）

- **Status:** accepted
- 决策：新增确定性证据合成层，把持久化 Evidence Store 投影为“实体 + 证据层 + 模式链接”三层的机制证据图；Web 工作台新增机制证据图面板，与既有工作流 DAG 并存。
- 决策：论文模式 EvidenceLink 只作为跨层链接的“建议模板”；链接边固定 claim_class=INFERRED，且必须同时满足：基因在源/目标证据层都有 context_match ≥ 0.5 的证据、基因无未解决的方向冲突、两层证据相互独立（不共享 source lineage 或同一 tool run）。
- 决策：方向冲突与证据依赖按确定性规则生成 synthesis findings，拦截而非静默删除；安全性阻断以独立 safety_liability 边保留。
- 决策：GraphEdge 增加 attributes（携带 lane/stance/pattern 元数据），GraphNode 增加 lane 类型；schema 同步导出，旧图可兼容读取。
- 决策：对齐数据生成与 Planner/Reviewer 小模型训练（P3）继续按团队决定最后再做；本次不产生任何训练数据或模型。

## 2026-08-08 - Gold 标注、抽取工具链与模式消融回归（P2）

- **Status:** accepted
- 决策：Gold 标注使用 append-only 台账（`CurationStore`，gold/rejected + 理由 + 标注角色），同一 PMID 可追加新状态但历史记录不可改写；抽取只允许 candidate 记录，只读取 Europe PMC 公开元数据与摘要或 Methods 有界文本，绝不落盘全文。
- 决策：抽取结果必须通过 StrategyPattern 完整合同校验（lane 顺序、required/optional 子集、证据链接、停止规则、至少一条 observed_workflows），失败进入 append-only 抽取审计（`extractions.jsonl`）供复盘，不允许半成品入库。
- 决策：专家评审使用 `ReviewLedger` 追加式台账（life_science + engineering 双人），模式 JSONL 记录保持不可变；`pattern review` 通过 CLI 写入，`PatternStore.corpus_card` 汇总有效评审状态。
- 决策：垂直子工作流（LangGraphRuntime 域内 Planner）与项目级 Planner 统一从模式库构建 few-shot；提示明确标注“策略提示非证据”，命中以 `planner_pattern_hints` trace 持久化，便于审计。
- 决策：盲测回归先以离线覆盖率和确定性计划有效性为质量门（BM-12），`--llm` 真实 Step 对比仅作为可选内部回归；覆盖率不表述为生物学成功率。
- 决策：对齐数据生成与 Planner/Reviewer 小模型训练（P3）按用户要求延后到最终阶段，模式库作为其数据来源；训练前不自动发布模型。

## 2026-08-08 - 论文模式语料管线与策略可见性（P1）

- **Status:** accepted
- 决策：候选语料只存元数据（PMID/标题/期刊/年份/DOI/PMCID/查询桶），不存摘要与全文；期刊白名单与查询桶是确定性常量，过滤规则可测试；append-only 按 PMID 去重并逐条 SHA-256。
- 决策：ResearchPlan 新增 `evidence_strategy_patterns` 字段，Planner few-shot 命中随计划持久化，项目快照与 Web 工作台展示“论文模式 → 策略 → 执行”链路，并明确标注为策略提示而非证据。
- 决策：语料刷新通过 NCBI E-utilities（esearch + esummary）完成，邮箱/API Key 只从环境或命令行注入，不进入仓库与日志。
- 验收：新增语料管线测试（过滤/去重/分桶/上限/append-only/digest/状态绑定）与 Planner 持久化测试；全量 pytest、benchmark、schema 导出与仓库策略检查在远程验收环境执行通过。

## 2026-08-08 - 修复策略可执行闭包门禁（P0.4）

- **Status:** accepted
- 决策：把修复策略从文档化约定升级为可执行闭包断言。`verify_domain_repair_policy()` 校验 finding 类别字面量 = 可修复类别 ∪ 显式拒绝类别、动作映射封闭、overlay payload 白名单与成功标准非空，并在 `ResearchProjectStore.assert_integrity()` 每次执行时先行校验，防止新增修复模式静默绕过持久化门禁。
- 决策：`RepairRequest` 增加 `candidate_lane_recompute_required` 并强制绑定 SWITCH_DATASET_SAME_CONTEXT：数据集切换必须显式声明候选绑定证据通道按新候选集重算，其它动作禁止携带；域修复请求必须保持 `no_scope_change=True`。
- 决策：修复决议快照绑定（before == trigger snapshot、最新决议 after == 当前项目快照）与“RESOLVED 时不得存在 active blocking 评估”写入 store 完整性门禁；ToolResult `supersedes_tool_run_id` 链加入引用完整性检查（禁止孤儿引用、自指与环），旧结果保留在 append-only ledger 中仅供审计。
- 验收：新增 10 项测试（8 项策略闭包/合同/ledger + 2 项 store 篡改门禁），全量 pytest、benchmark、schema 导出与仓库策略检查在远程验收环境执行通过。

## 2026-08-08 - 类型化候选绑定与证据失效（P0.3）
- **Status:** accepted
- 决策：把“证据依赖候选集合”从隐式约定升级为类型化合同。PlanStep 声明 candidate_bound + evidence_lane；候选绑定步骤的 ToolResult 记录 step_id + candidate_universe_digest（含合同版本与排序候选全集），恢复时 digest 不匹配即重取，并以 supersedes_tool_run_id 建立取代链。
- 决策：Reviewer 与 Ranking 只消费未被取代的 active 结果与证据；旧结果保留在 append-only ledger 中，确保“候选集变化后文献/药物/安全/扰动证据按新候选集重算”可被机器断言，而不是只依赖整体子图重跑。
- 决策：项目修复层把“数据集切换后所有候选绑定证据通道按新候选集重算”写入 SWITCH_DATASET_SAME_CONTEXT 成功标准；SPLIT_CONTEXT_SAME_SCOPE 明确保持候选绑定不变。
- 决策：对齐数据训练（Planner/Reviewer SFT 与偏好对）按用户要求延后到最终阶段，不进入本轮改造。
- 验收：新增候选绑定合同/Planner 校验/运行时重取与取代链测试，全套测试在远程验收环境执行。
## 2026-08-08 - 类型化领域修复：上下文拆分与 overlay 可执行断言

- **Status:** accepted
- 决策：把“上下文错误”细化为两种确定性处置——子上下文仍在冻结 TaskSpec 内时走 SPLIT_CONTEXT_SAME_SCOPE（R2/checkpoint，重新绑定证据上下文，不删除），完全在范围外时走 EXCLUDE_EVIDENCE（隔离引用、保留源证据）。
- 子上下文门控为字符串级收窄关系（等于或包含于冻结值），是保守启发式；不接受更宽泛或跨维度值，宁可不修复也不猜。
- 决策：AUTONOMOUS 项目不提出任何 CHECKPOINT_REQUIRED 修复；之前“R2 提议 → WAITING_REVIEW → resume 校验失败”的组合视为缺陷，本次以策略层门控消除。
- 决策：overlay 不允许删除证据记录；EXCLUDE 仅从 active 引用移除并写 isolated_only/retained_in_source，SUPPLEMENT 必须带 reason，DOWNGRADE 固定目标 INFERRED 且拒绝 no-op/升级。
- 验收：新增 6 项策略/overlay 测试（原因与来源校验、自主性门控、同范围拆分提议与执行、升级/缺失/no-op 拒绝、冲突拆分检测、checkpointed 拆分端到端），全套测试需在远程验收环境通过。

## 2026-08-08 - WorkItemHead/ArtifactHead CAS 与确定性恢复

- **Status:** accepted
- WorkItemHead 是工作项当前已提交结果的权威指针（attempt + result digest + CAS version），result.json 只是镜像；执行顺序固定为“不可变结果快照 → attempt 行 → head CAS → 镜像”，中断恢复从 durable head 重建，不依赖 Trace 猜测。
- 产物注册同时写 ArtifactVersion 不可变版本行与 ArtifactHead（CAS active 指针）；旧版本保留且可读，Reviewer 只读取 active artifact 集。
- 每次评审提交写 ReviewTarget，snapshot digest 只包含评审项输入闭包（结果/评审/产物），下游报告完成后 digest 不漂移，恢复可幂等重建缺失 target。
- Worker lease 增加 heartbeat 续期与过期回收；孤儿/过期 lease（含 RUNNING attempt 行）恢复时回收并重试，中断 attempt 保留审计。
- 验收（远程验收环境）：新增 6 项测试覆盖 CAS 冲突、幂等重放、版本台账、heartbeat、镜像修复不重跑、过期 lease 回收、ReviewTarget 重建；全套测试通过。

## 2026-08-08 - Product-speed cache layers

- **Status:** accepted
- Analysis cache keys are task-context-free (dataset + recipe + source checksum + tool/contract versions); legacy task-context keys remain as a one-time migration fallback.
- Literature LLM rerank/extract results persist under corpus-snapshot + model + prompt-version keys; replay still requires exact source spans.
- Reviewer LLM findings cache uses a normalized payload (per-run ids become positional tokens); replay maps tokens back to current ids and re-validates. A single invalid finding is skipped instead of discarding the whole review round.
- Reviewer uses a dedicated 240s read timeout with one retry; the previous 90s x 3 budget wasted ~4 minutes before deterministic fallback.
- Acceptance (remote acceptance environment): fresh UC project warm stage-2 run is 55s; cold/warm/hot runs produce identical Top-10 rankings with completed_with_gaps, 0 blocking, 2 gaps.


## 2026-08-07 - Context-relation benchmark uses scoped contrastive labels

- Added a disease-disjoint relationship benchmark spanning disease, target,
  tissue, cell type and disease stage.
- Disease-target positives reuse the curated, evidence-graded anchors in
  `configs/disease_library.yaml`; they are explicitly bounded as ranking sanity
  anchors rather than cell-specific causal facts.
- Tissue/stage swaps are labelled as mismatches against the curated benchmark
  context, not as universal biological negatives. Cross-disease target swaps
  are excluded because pleiotropy makes those negatives unsafe without
  publication-level review.
- Context donors are restricted to the same split, and every case for one
  disease stays in a single split.


## 2026-08-01 - Versioned schemas are module boundaries

- **Status:** accepted
- Cross-module objects use the versioned JSON Schemas generated from the canonical Pydantic models.
- Breaking changes require a new contract version and an explicit one-way adapter.
- The goal is to prevent omics, perturbation, evidence and reporting modules from drifting into incompatible field conventions.

## 2026-08-03 - V2 auditable Agent boundaries

- **Status:** superseded in part by V2.1
- This repository is the only maintained implementation; handover assets remain read-only inputs.
- MCH/K562 is an isolated causal-modelling gold sample and must not be presented as disease-context causal evidence.
- Low-context DeltaFactor predictions are exploratory and excluded from formal ranking.
- Reports and the UI render structured Evidence Store values only.
- Experience promotion and LoRA training are offline, auditable and human-approved; automatic code, training or publishing mutation is prohibited.

## 2026-08-03 - V2.1 generic public-omics workflow

- **Status:** accepted
- The public contract is `2.1.0`, with an explicit one-way adapter from TaskSpec `2.0.0`.
- The default disease workflow discovers GEO and CELLxGENE data dynamically; UC snapshots and fixed perturbation tools are disabled compatibility plugins.
- LLMs select only tools exposed by the live typed registry. Matrix type, biological replication, metadata confidence and context remain deterministic gates.
- Cache identity binds source checksums, scientific recipe content, tool and contract versions, and biological context; per-run trace IDs are excluded.
- Infrastructure configuration and secrets are external to Git. The production web command uses Waitress; Flask development mode is explicit.
- Scientific workflow references are pinned to `scientific-agent-skills` v2.62.0 at commit `ad21a3868923628330734375dddbf7b86ea84222`.

## 2026-08-04 - Disease library as a first-class asset

- **Status:** accepted
- `configs/disease_library.yaml` holds 18 curated diseases (autoimmune, neurodegenerative, cancer, metabolic, respiratory); every ontology identifier was verified against live EBI OLS search before entry, and new identifiers must pass the same live check.
- Reference targets are evidence-graded (`approved_drug > gwas > mendelian > clinical_trial > mechanistic`) and serve as ranking sanity anchors, not as ground truth for scoring novelty.
- Task templates encode the project 50/20/15/15 composition (normal / missing_context / conflicting_evidence / trap) with machine-checkable `expectation` blocks consumed by the benchmark layer.
- The disease resolver merges library aliases at runtime; hard-coded legacy aliases win on conflict so existing behaviour never regresses.

## 2026-08-04 - Stable demo replay and live workbench share one backend

- **Status:** accepted
- The main workbench supports both validated stored-run replay and new live Agent runs; replay is never represented as live execution.
- The replay bundle is derived only from persisted status, Plan, Trace, ToolResult, EvidenceItem, ranking and TargetCard artifacts.
- Internal tool/event identifiers, absolute server paths and secrets are excluded from the public bundle.
- Frontend code performs presentation only and does not create new scientific scores, claims or database results.

## 2026-08-05 - V3 project control plane serves the vertical Target product

- **Status:** accepted
- Target remains a disease-driven drug-target-discovery Agent; the project/run/artifact model is internal reliability infrastructure, not a general-purpose scientific-workbench claim.
- The project contract is `3.0.0` and wraps, rather than replaces, the target-specific `TaskSpec 2.1.0`, evidence contracts, ranking rules, Reviewer gates and TargetCards.
- Accepted plans use allowlisted typed modules. Artifact snapshots are content-addressed; project events, assessments and decisions are append-only.
- The vertical project plan invokes the existing disease-target workflow as one bounded module instead of duplicating its literature, omics and evidence-fusion stages at project level.
- Phase-one HTTP project endpoints are shipped for embedding. MCP, external blind target-ranking evaluation, GWAS/eQTL ingestion and broad perturbation Oracles remain roadmap work and must not be represented as completed.

## 2026-08-05 - Reviewer repair is bounded to transient read-only failures

- **Status:** accepted
- Automatic repair may retry only allowlisted read-only connectors and must remain within both tool-call and review-round budgets.
- The append-only ToolResult ledger retains failed attempts. Review and terminal status use the latest effective attempt for each tool after repair.
- Matrix eligibility, replication, biological context, model scope, causal boundaries and safety blockers cannot be cleared by retry.
- If repair is unavailable or still fails, the system preserves the corresponding evidence gap and degraded terminal status.

## 2026-08-05 - V2.2 controlled human-genetics evidence boundary

- **Status:** accepted; narrows the V3 phase-one statement that no controlled human-genetics input existed. General eQTL ingestion and statistical fine-mapping/colocalization recomputation remain roadmap work.
- The target-discovery contract advances to `2.2.0`; homogeneous `2.0.0` and `2.1.0` payloads use explicit one-way adapters, while mixed contract trees are rejected.
- The current genetics lane audits pre-staged, checksum-bound GWAS summary statistics, SuSiE per-signal posterior credible sets and coloc results. It does not recompute fine-mapping or colocalization and does not execute arbitrary analysis code.
- A locus-to-gene result enters the strict human-genetics score only after study, phenotype, build, ancestry, LD, signal, regional variant manifest, allele harmonization, overlap, posterior, sensitivity and biological-context gates pass. GWAS-only loci remain unresolved; nearest-gene assignment is forbidden.
- Colocalization supports a shared association signal under the supplied model and priors. It remains `INFERRED`, does not establish the causal gene or variant and does not determine therapeutic direction.
- Open Targets aggregate genetic association is retained as non-formal database context; somatic-mutation evidence is represented separately. Neither can independently satisfy the strict genetics `GO` gate.
- For `gwas_locus_to_target`, the rankable candidate universe is restricted to checksum- and provenance-validated formal candidates emitted by the genetics extraction chain. Disease-level aggregate sources may annotate those candidates but may not expand the locus-specific universe.
- Non-terminal runs created under an older contract cannot resume in place under `2.2.0`; callers must start a derived current-contract run. Terminal legacy runs remain readable without mutation.
- Public bundles expose the stored run's source contract as `contract_version`/`source_contract_version` and the current renderer separately as `rendered_contract_version`.
- Genetics report provenance is additive across retries. Each selected stage records its exact ToolRun plus artifact checksums; earlier attempts remain available for audit.

## 2026-08-05 - Target is a vertical domain service, not a replacement workbench

- **Status:** accepted; supersedes only the MCP-roadmap portion of the earlier V3 phase-one decision.
- Architecture review of SciForge, OpenScience, OpenAI4S and Wisp showed that chat, general code execution, persistent kernels, remote compute and desktop workspace concerns belong to a mature host runtime.
- Target continues to own disease-target task contracts, evidence semantics, project state, allowlisted scientific execution, Reviewer gates, immutable artifacts and release decisions.
- `ResearchProjectService` is the product-facing application boundary. CLI, HTTP and MCP adapters must operate on the same durable store and may not create a parallel conversation-only state.
- The phase-one MCP surface uses the official Python SDK over local stdio and exposes project creation, bounded execution, status, event replay, checkpoint acceptance and verified text artifacts.
- MCP does not expose arbitrary shell or model-generated code execution and cannot waive frozen plans, evidence gates, artifact integrity or missing-context outcomes.
- Streamable HTTP MCP, registry publication, remote authentication policy and host-specific installation remain explicit roadmap work.

## 2026-08-05 - Child workflow activity is a projection, not a second evidence store

- **Status:** accepted
- The authoritative disease-target Trace, ToolResult, EvidenceItem, ReviewerFinding and Claim records remain in the child Evidence Store.
- The project store indexes a strict, append-only activity projection with its own cursor and an exact `child_run_id + source_trace_id` backlink.
- This is an additive `3.0.0` record and optional ledger: existing projects with no activity file remain readable as an empty activity stream; no persisted 3.0 object changes meaning.
- Only operational fields are projected: domain stage, tool name, status, coverage, context-match metadata and source IDs. Candidate genes, ranking values, evidence text and Reviewer prose are not copied.
- Projection is reconciled before resume and after execution. Projection failure may degrade the project work item but cannot alter the child scientific terminal status.
- Child `reviewer_repair` records a real bounded connector retry. It is not a project `DecisionEvent.REPLAN`; project-level evidence supplementation still requires a future immutable plan-revision contract.
