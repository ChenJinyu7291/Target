# Target 已完成能力与证据边界

> 状态日期：2026-08-08  
> 本文只记录仓库中已经实现且有检查路径的能力；未完成项不会写成产品能力。具体远程验收事实见 [docs/VALIDATION_REPORT.md](docs/VALIDATION_REPORT.md)。

## 1. 已完成的科学主链

### 1.1 通用任务与上下文合同

- `TaskSpec` 记录疾病、亚型、组织、细胞类型、阶段、物种、目标表型和数据集限制；
- 任务支持疾病到靶点与受控遗传学/机制输入；
- 缺少上下文时保留缺口，不从自然语言中静默虚构；
- 关键科学对象以 Pydantic 为单一源并导出 JSON Schema。

### 1.2 动态公开组学流程

- 疾病标准化、GEO 动态检索、候选数据集评分与元数据资格审查；
- 整数 count 矩阵走 PyDESeq2，非整数/连续矩阵会被拒绝；
- 单细胞正式差异证据要求 donor、condition、cell type，并采用 donor-level pseudobulk；
- 支持 QC、差异结果、通路富集、候选基因提取和来源校验和；
- 没有合格数据时，其他证据链继续，终态降级为 `completed_with_gaps`。

### 1.3 遗传学与多证据融合

- 受控 GWAS summary-statistics 候选宇宙冻结、变异校验与 provenance；
- 审查用户提供的 SuSiE credible set 与 coloc 输出；
- 遗传学、组学、扰动、机制、可成药性和安全性分维度记录；
- 上下文匹配系数、预测扰动上限、反方证据和安全阻断项独立保留；
- 分数仅作为优先级，不表述为临床成功概率。

当前边界：尚未在主链中重算 fine-mapping/coloc；Open Targets 聚合证据不替代正式遗传统计；上下文错配的 K562/DeltaFactor 结果不得进入疾病正式得分。

### 1.4 研究交付物

- 候选排序、重点 TargetCard、证据缺口、Go/Conditional Go/Insufficient Evidence；
- 可证伪实验计划，明确干预、模型、对照、终点和不同结果对应结论；
- Evidence Store、Trace、工具版本/参数与报告回链；
- 报告从结构化存储生成，不能新造后端不存在的数字。

### 1.5 产品级缓存与热重跑

- 分析缓存 key 与任务上下文解耦（数据集 + recipe + 源校验和 + 工具/合同版本）；旧含上下文的 key 保留一次性迁移回退，同一数据集/recipe 可在不同任务间复用数值结果，证据仍按当前任务上下文生成；
- Europe PMC 检索/全文持久缓存；LLM 重排与跨度抽取结果按“语料快照 + 模型 + 提示版本”缓存，回放时仍执行原文 span 校验；
- Reviewer 使用归一化载荷缓存：运行内 ID 归一为位置 token，缓存命中后映射回当前 ID 并重新校验；单条非法 finding 只被跳过，不再丢弃整轮有效结果；
- Reviewer 专属超时 240s、重试 1 次，避免 90s×3 无效等待后落入确定性回退；
- 远程验收（UC）：冷启动约 28 分钟 → 工具层全缓存后阶段二 55s；冷、暖、热三次独立运行的 Top10 排序完全一致，终态均为 completed_with_gaps、0 blocking、2 gaps。

### 1.6 机制证据图与证据合成（P2.5）

- 证据合成将持久化 Evidence Store 投影为机制证据图：disease / locus / variant / gene / cell_state（组织或细胞类型）/ drug 实体节点，外加 genetics / omics / perturbation / drug / literature / safety 证据层节点；
- 跨层模式链接：只有当基因在两条独立证据层都有达标证据（context_match ≥ 0.5）且匹配论文模式 EvidenceLink 时才生成 pattern_evidence_link 边，claim_class 固定为 INFERRED，并携带 pattern_id、link_type、decision_rule 与 why_this_link，明确标注“策略假设而非当前疾病证据”；
- 确定性质量门：方向冲突基因（同上下文 increase+decrease）拦截其全部模式链接；两条层共享 source lineage 或同一 tool run 时视为非独立证据，模式链接被拦截并写入 evidence_dependence finding；低上下文证据不参与模式链接但保留在图上供审计；
- 图语义边界：边权重仅为上下文匹配系数（排序用途），不表示因果或临床成功概率；安全性阻断以独立 safety_liability 边保留，不被加权平均隐藏；
- 接口：GET /api/projects/<id>/mechanism-graph 返回 graph + synthesis_findings + reviewer_findings + lane_coverage + pattern_links；Web 工作台证据图区改为“工作流 DAG / 机制证据图”双面板，支持模式链接、预测/推断边、证据层节点筛选，前端不创造任何后端不存在的数字；
- 合同：GraphNode 新增 lane 类型，GraphEdge 新增 attributes，schemas/causal_graph.schema.json 同步更新（兼容读取旧图）。

### 1.7 论文摘要 RAG 与 Planner few-shot 增强（P2.6）

- 新增 src/target_agent/paper_rag.py：把近年 CNS 论文的公开摘要切成有界分块（PaperChunk，默认 700 字符、90 字符重叠），每块带 PMID/DOI/PMCID、期刊、年份、证据层标签与 SHA-256 digest；存储为 append-only JSONL（paper_strategy/rag/chunks.jsonl）并生成 MANIFEST；
- 检索为确定性词法评分：疾病词、查询词、数据可得性证据层、论文新旧、期刊权重，无 embedding、无网络依赖；PaperRagStore.search 返回 PaperChunkHit（分数 + 命中原因）；
- PlannerFewShotBuilder 新增 build_paper_evidence：模式命中与论文证据同时注入 Planner prompt；两端 Planner（领域 Planner 与项目 ResearchPlanner）都会把 paper_evidence 放进请求、持久化进 ResearchPlan、并把命中数写入 planner_backend（+paper-rag:N）；
- LangGraph 运行时会以 planner_paper_evidence trace 记录 chunk_id/PMID，领域活动投影允许该事件类型；Web 工作台新增“论文证据（RAG）”面板，展示 chunk、得分、证据层与命中原因，并明确标注“策略提示非证据”；
- 只持久化摘要分块：Methods/全文仅在抽取时驻留内存，绝不落盘；对齐数据生成与 Planner/Reviewer 小模型训练仍按团队决定延后到最终阶段。
- 远程已刷新种子语料：paper_strategy/rag/chunks.jsonl 共 155 个分块 / 59 篇 2025-2026 年 Nature/Science/Cell 系列论文（MANIFEST.json 校验和入库），可通过 pattern rag refresh 继续扩充；顺带修复 Europe PMC Methods 截断引用未加 self. 的存量缺陷。

## 2. 已完成的可靠性控制面

### 2.1 持久项目模型

- 项目、目标、计划、work item、result、artifact、assessment、decision 和 event 为类型化记录；
- 项目 spec 与基础 plan 冻结；事件、评审和决定追加记录；
- artifact 内容寻址并校验 SHA-256；
- 项目目录是恢复单元，跨进程执行锁避免同一项目被并发运行；
- 终态、依赖失败、预算和缺口具有显式状态。

### 2.2 项目执行/完整性评审触发的有界修复

已实现的真实边界是：

```text
typed transient failure
-> digest-bound independent FAIL assessment
-> immutable RepairRequest
-> deterministic policy eligibility
-> append-only ResearchPlanRevision overlay
-> same-input affected-subgraph rerun
-> re-review + RepairResolution
-> new release snapshot digest
```

自动修复必须同时满足：模块 side-effect-free、replay-safe、声明 `same_input_retry`、输入 digest 不变且仍在预算内。checkpointed/supervised 模式要求对精确 trigger snapshot 批准；过期 digest 被拒绝。旧 result/assessment/artifact 不删除；当前 runtime 的 finalize 与 release digest 使用逻辑 active item 集，API ledger 仍返回全部历史记录并额外给出 active item IDs 与 active artifact IDs；WorkItemHead/ArtifactHead 是 active 视图的权威指针（见 2.5）。

已实现同上下文数据集切换修复：Reviewer 对首选数据集给出 blocking FAIL 时，确定性策略生成类型化 SWITCH_DATASET_SAME_CONTEXT 指令（不改变冻结 TaskSpec，仅替换 preferred/excluded accession），重建受影响子图并强制重新评审，具备端到端验收测试。

已实现类型化领域 finding 驱动的完整修复策略（R0–R3）：

- R0 声明降级（causal_overreach）：Reviewer 判定某条派生 Claim 因果越界时，自动生成 DOWNGRADE_CLAIM overlay，将 claim_class 降为 INFERRED 并写入 `causal_interpretation_removed`，无需人工审批；
- R1 同范围补证（coverage_gap）：证据引用缺失但证据集内存在候选时，自动生成 SUPPLEMENT_EVIDENCE overlay，只追加已存在的证据引用，不新造证据；
- R2 证据排除（context_mismatch）：上下文不匹配或冲突证据必须经 checkpoint 审批后生成 EXCLUDE_EVIDENCE overlay，仅从引用层移除、不删除来源证据；
- R3 越界拒绝：`unsupported_claim`、真值/阈值/范围改动等类别不在 FINDING_TO_ACTION 白名单内，策略层永不提议，只能由人类决策处理；
- overlay 全部由确定性 `DomainOverlayModule` 在派生层执行：保留源结果、只写 `domain_overlay.json` 审计文件，并将已解决的 finding 标记为 `finding_status=resolved`，Reviewer 不再重复触发；
- 多个 finding 形成链式 overlay 时，早期 repair 的 Resolution 会跟随最终 active 链升级为 RESOLVED，仓库完整性校验按链末 active 项复核，避免“修好了但永远显示 unresolved”。

- 确定性证据门禁（来自子运行 claims/evidence，而不是 LLM 文本）：
  - 方向一致性：同一基因同时出现 increase 与 decrease 证据时，生成 blocking `conflicting_evidence` finding，映射到 R2 证据排除（需 checkpoint 审批）；
  - 证据独立性：一条 Claim 的多条证据共享同一 source/dataset/study/tool-run 谱系时，生成 blocking `evidence_dependence` finding，映射到 R0 声明降级（自动降为 INFERRED），防止把同一研究的重复引用当作独立支持；
  - 与子运行 Reviewer finding 按 finding_id 去重合并，确定性门禁不覆盖人工/LLM finding。

- 尚未完成：从自由 Reviewer 文本直接生成修复（当前只接受类型化 finding）；步骤级回退与跨模块候选依赖超集链的完整自动修复（上下文拆分见 2.6，候选绑定证据失效见 2.7）。

### 2.3 Review 与发布

- 结构完整性评审和领域 Reviewer 分离；
- finding 可分 blocking/major/minor，结果绑定 target digest；
- 修复后必须重新 Review；
- release decision marker 绑定当前 active project snapshot SHA-256，而不是只绑定 plan ID；
- 输入或修复导致快照变化时，旧 marker 不再适用；当前尚无独立 `ReleaseRecord`、专家签名或认证发布包。

### 2.4 持久 Python/R 分析内核（借鉴 OpenAI4S/Wisp）

- 显式创建/停止的持久会话，Python（默认）与 R（Rscript + jsonlite，可选）双后端，状态跨执行保留；
- 换行 JSON 协议，stdout/stderr/traceback 截断标记，超时终止会话并保留 FAILED 原因；空闲会话按策略回收；
- 本地守护进程自动拉起（日志在 cache/kernel-daemon.log）；CLI 提供 kernel start/exec/status/stop/stop-all/serve；
- Web API：GET/POST /api/kernels、GET/DELETE /api/kernels/<id>、POST /api/kernels/<id>/exec；工作台第 08 节内核控制台（启动/运行/停止，能力栏显示内核开/关）；
- 明确边界：LLM 不自动执行代码，仅人工或注册工具使用；doctor 区分必需依赖与可选分析后端；
- 9 项内核测试：状态持久、错误恢复、超时杀死、输出截断、默认目录自动创建、禁用、R 未装、空闲回收、Web API 生命周期。

### 2.5 工作尝试、产物版本与 CAS 头

- WorkAttempt 永久不可变（append-only，结果快照按 digest 绑定）；WorkItemHead 以 CAS 更新并记录 attempt/result digest，重放同一提交是幂等 no-op；
- 每条工作项执行顺序固定为：不可变结果快照 -> attempt 行 -> head CAS -> result.json 镜像；中断恢复以 head 为权威，不通过 Trace 猜测业务状态；
- 产物注册同时写入 artifact_versions.jsonl（不可变版本行）与 artifact_heads.jsonl（CAS active 指针），旧版本内容寻址保留、可审计可读取；
- Reviewer 只接收当前 active artifact 集；每次评审提交生成 ReviewTarget，绑定评审时刻的输入闭包 snapshot digest、result digest 与 active artifact 逻辑 ID；
- worker lease 支持 heartbeat 续期与过期回收；孤儿/过期 lease（含 RUNNING attempt 行）恢复时被回收并重试，中断的 attempt 行保留为审计记录；
- 中断边界验收：attempt/head/review 任一边界模拟中断后恢复，已完成步骤不重复执行、旧版本不丢失，缺失的 ReviewTarget 由恢复逻辑幂等重建。

### 2.6 类型化领域修复：同范围上下文拆分与 overlay 断言

- DomainFinding.category 与 FINDING_TO_ACTION 对齐为同一封闭集合（新增 gene_mapping_overreach、evidence_dependence、missing_provenance、context_split_needed），合同层不再允许策略映射之外的 finding 类别进入修复流程；
- 新增 SPLIT_CONTEXT_SAME_SCOPE 修复（R2、checkpoint 审批）：当冲突证据可确定性映射到冻结 TaskSpec 内的不同子上下文时，overlay 将每条证据重新绑定到其子上下文（context 合并 + context_split_by/reason 审计标记），证据保持 active，不删除任何一侧；子上下文必须等于或收窄冻结值，更宽泛值被拒绝；
- 冲突检测器优先尝试上下文拆分：同一基因 opposing-direction 证据带不同 tissue/cell_type 子上下文时输出 context_split_needed（替代直接排除）；无可用子上下文时仍走 conflicting_evidence 到 EXCLUDE；
- overlay 可执行断言：EXCLUDE 只能隔离已存在于派生证据集的证据（missing row 拒绝应用，isolated_only/retained_in_source 写入审计），SUPPLEMENT 必须携带非空 reason 且只能引用已存在证据，DOWNGRADE 只允许降至 INFERRED、拒绝任何升级与已 INFERRED 的 no-op；
- 自主性契约：AUTONOMOUS 项目不再提出 checkpoint 必须审批的 R2 修复（dataset switch / exclusion / context split），避免“提出后暂停、恢复时校验失败”的死锁；这类 finding 保留为文档化阻塞缺口并以 completed_with_gaps 收尾，CHECKPOINTED/SUPERVISED 项目行为不变。

### 2.7 类型化候选绑定与证据失效（P0.3）

- `PlanStep` 声明 `candidate_bound` 与 `evidence_lane`（literature / genetics / drug_safety / perturbation / trials），evidence_lane 必须与 candidate_bound 同时出现，Planner 拒绝没有候选产生依赖的证据步骤；
- 每个候选绑定步骤的 ToolResult 写入 `_candidate_bound` 元数据（step_id + candidate_universe_digest + evidence_lane），digest 包含合同版本与排序后的候选基因全集；
- 恢复时以 ToolResult ledger 重建候选全集，比对每个已完成候选绑定步骤的 digest；不一致则从 completed_steps 移除并重取，同时写入 `replan/resume` 与 `evidence_superseded` trace，新结果通过 `supersedes_tool_run_id` 显式取代旧结果；
- Reviewer/Ranking 只消费未被取代的 active ToolResult 及其 EvidenceItem，旧证据保留在 append-only ledger 中但不再参与评分与报告；
- 项目修复策略明确要求数据集切换后所有候选绑定证据通道按新候选集重算，上下文拆分时保持候选绑定不变。

### 2.8 修复策略可执行闭包门禁（P0.4）

- `verify_domain_repair_policy()` 作为唯一可执行策略闭包断言：DomainFinding.category 字面量必须等于“可修复类别 ∪ 显式拒绝类别”，FINDING_TO_ACTION 键必须与可修复类别一致且每个动作都存在于 DOMAIN_REPAIR_POLICY，overlay 动作必须与 payload 白名单一一对应且每个动作都有非空成功标准；`ResearchProjectStore.assert_integrity()` 每次持久化完整性检查都先执行该断言，任何新增修复模式都无法静默绕过；
- `RepairRequest` 新增 `candidate_lane_recompute_required` 合同字段并绑定到 SWITCH_DATASET_SAME_CONTEXT：数据集切换修复必须声明候选绑定证据通道按新候选集重算，其它动作禁止携带该标记；store 层同时校验域修复请求 `no_scope_change` 恒为 True；
- 快照绑定：修复决议的 `before_snapshot_digest` 必须等于触发请求的 `trigger_snapshot_digest`，最新决议的 `after_snapshot_digest` 必须等于当前项目快照；RESOLVED 决议不允许存在任何未消除的 active blocking 评估；
- ToolResult 超链成为引用完整性的一部分：`supersedes_tool_run_id` 必须指向 append-only ledger 中真实存在的旧结果、不允许自指或成环，旧结果与旧证据保留在 ledger 中仅供审计，Reviewer/Ranking 只消费 active 结果。
## 3. 已完成的产品接口

- CLI：创建、运行、状态、事件、活动、checkpoint、repair queue 和 repair decision；
- HTTP：项目创建/运行/状态、事件、领域活动、artifact、repair queue 与 snapshot-bound 决策；
- HTTP 工作台：`/api/projects` 项目列表、`/api/projects/<id>/resume` 手动继续、checkpoint 审批（plan/release）、repair/fork 审批、redo/restore 回退与 branches 查询；
- Web 单页工作台（UTF-8 中文）：新建项目、计划/结果/分支/事件/产物渲染，所有数字只来自后端 API；
- MCP fork 工具：`target_propose_fork`、`target_decide_fork`、`target_get_branches`；
- 产品外壳：`target-agent init` 脚手架（project.yaml + README + .env.example）、`project-export`/`project-import` 可移植项目包（MANIFEST + SHA-256 校验、拒绝覆盖、拒绝密钥文件）、`project-package-inspect` 与 Web `GET /api/projects/<id>/export`；
- 模型供应商抽象：`LLM_PROVIDER=step|openai`，openai 模式走任意 OpenAI 兼容 Chat Completions 端点；
- 科研工作区：`GET /api/projects/<id>/graph` 证据图（工作项依赖 + 产物溯源 DAG）、`GET /api/projects/<id>/files` 项目文件树、`GET /api/projects/<id>/files/preview` 文本预览（路径越界/密钥文件/超大文件拒绝），Web 工作台新增证据图与文件预览面板；
- 技能库：skills/ 下 6 个 SKILL.md（文献证据抽取、bulk RNA 受控分析、单细胞 pseudobulk、遗传审计、TargetCard Review、可证伪实验设计），SkillCatalog 扫描并以 SHA-256 校验、确定性检索；CLI `skills list/search/show`、Web `GET /api/skills` 与 `GET /api/skills/<id>`、`/api/capabilities.skills`；Planner 只接收 id/name/description/evidence_lanes 渐进提示，完整正文按需加载且不作为任务证据；
- 持久分析内核：CLI kernel 子命令、Web /api/kernels 生命周期接口与工作台内核控制台（能力栏显示内核开关）；
- stdio MCP：十一项类型化工具，全部调用同一 `ResearchProjectService`；
- Web 与 MCP 不建立第二套状态语义；
- 决策已持久化但后台队列已满时，接口区分“决定已接受”和“是否成功排队”。

## 4. 已完成的 benchmark 与对齐资产

- 内部 fake/unit benchmark 覆盖合同、恢复、拒绝、确定性和引擎一致性；
- 参考 blind-ranking scorer 能在载入私有标签前冻结任务、排名和状态 digest，并计算 disease-macro 指标及 trap/safety gate；
- Planner/Reviewer SFT、偏好和 held-out 资产具有角色字段与 promotion gate；现有 120 条 SFT 是 6 个模板各 20 个索引变体，科学与工程角色由同一负责人完成，不是独立双人审核；
- benchmark runner 已读取权威 `reviewer_findings.jsonl`，并支持显式 `finding_category` 断言；
- 论文策略 P0/P1：`paper_strategy.py` 的 ObservedWorkflow/StrategyPattern/BestPracticePattern 合同、append-only 确定性 PatternStore 与 Planner few-shot；种子库 10 条 discovery patterns 与 checksum manifest；
- 论文语料管线（P1）：`paper_corpus.py` 通过 NCBI E-utilities 按 4 个查询桶 × 期刊白名单检索近 5 年 CNS/高影响力论文，esummary 批量取元数据，确定性过滤（期刊/年份/标题排除 review 与 methods-only），append-only `CorpusStore` 按 PMID 去重并逐条 SHA-256；CLI `pattern corpus refresh|status` 与 `scripts/build_paper_corpus.py`；
- 证据策略可见性（P1）：ResearchPlan 持久化 `evidence_strategy_patterns`（few-shot 命中），项目快照与 Web 工作台展示“论文模式 → 策略 → 执行”链路，前端明确标注“策略提示非证据”；
- P3 对齐数据生成与 Planner/Reviewer LoRA 训练按团队决定延后至最后阶段，以论文策略沉淀为数据来源。

重要限定：历史 18 疾病 `conflicting_evidence` 桶的 54/54 仅验证终态、provenance 和报告存在；当时 `expectation.reviewer_categories` 未作为可执行断言，因此不能证明冲突识别或修复。公共疾病库也不能作为最终盲测集。当前没有外部 evaluator 控制、独立专家标注的生物学性能结果。

## 5. 已验证但不应夸大的事实

- 动态 AD、LUAD、UC 流程曾在远程环境运行；无合格 UC 组学时可靠降级；
- Step 结构化 Planner、Waitress 服务、stdio MCP、Schema 导出与仓库策略均有远程验收记录；
- Reviewer LoRA 在模板一致的 30 条 held-out 集上通过合同测试，但这不代表开放世界 Reviewer 水平；
- 当前项目修复的远程完整验收已经覆盖自动相同输入重跑、checkpointed 精确快照批准、逻辑 active item 集、release decision marker 重绑定、HTTP/MCP 和 benchmark ledger 修正；最终精确提交的验收结果以验证报告最后一节为准；
- 远程 HTTP 端到端冒烟通过：创建项目 → 审批计划 → 真实工具执行（GEO 检索、组学分析、Europe PMC、Open Targets、ClinicalTrials）→ 终态 `completed_with_gaps` → 事件/产物/项目列表完整；
- 2026-08-08 实时产品链路验证（Step 3.7 Flash + 冷缓存真实运行）：`init` → `project-run` → 计划 checkpoint → `project-approve --resume` → GEO 动态检索（10 候选、资格审核、拒绝理由可追溯）→ GSE197698/GSE206171 差异/通路/QC 分析 → 遗传/试验/文献 RAG → 10 个排名靶点 + 5 张 TargetCard + 11 条 Reviewer finding → 项目评审终态 `completed_with_gaps`（0 blocking）→ `project-export`（66 文件/1.67 MB zip）→ `project-package-inspect` → 全新目录 `project-import` 后状态与产物一致，重复导入拒绝覆盖；
- 全量回归 289 passed / 2 skipped（含 9 项持久内核测试与 6 项领域 finding 修复策略测试：自动 R0/R1、checkpointed R2、越界拒绝、overlay revision、链式 resolution、方向一致性与证据独立性门禁）；远程内核守护进程冒烟通过：start → exec → status → stop。

## 6. 仍未完成

- WorkItemHead/ArtifactVersion/ReviewTarget 的完整 active-head 模型；
- 所有中断边界恢复（WorkerLease 与 WorkAttempt 台账已实现，尚缺 heartbeat 与跨进程 CAS 全场景）；
- 从自由 Reviewer 文本直接生成修复（当前只接受类型化 finding）；上下文拆分与科学依赖失效的自动修复；
- 真实 fine-mapping/coloc 重算和广泛适用的扰动 Oracle；
- 大规模、多疾病、独立专家审核的 alignment 数据；
- 外部隐藏疾病盲测与独立专家仲裁；
- 生产级多用户认证与租户隔离（Streamable HTTP transport 基础版已完成，见 §12；认证/多租户未完成）；
- 自动湿实验、临床决策或自修改系统。

后续优先级与验收标准见 [PRD.md](PRD.md)，可视化状态页见 [product_status.html](product_status.html)。


## 7. 论文策略模式：Gold 标注、抽取、评审与消融回归（P2，2026-08-08）

### 7.1 已完成

- **Gold 标注台账**：`CurationStore` append-only，记录 gold/rejected、理由、标注角色与 digest；CLI `target-agent pattern curate`。
- **抽取工具链**：`pattern_extraction.py` 提供 Europe PMC 元数据 + 摘要/Methods 有界文本（`EuropePmcMetaFetcher`），LLM 结构化输出必须通过 StrategyPattern 完整合同校验并含至少一条 observed_workflows；每次尝试写入 append-only 审计 `extractions.jsonl`（状态、来源材料级别、提示版本、错误），全程不落盘全文；CLI `target-agent pattern extract`。
- **专家评审台账**：`ReviewLedger` 追加式记录 life_science/engineering 双人评审，`PatternStore.corpus_card` 汇总有效评审状态；模式记录保持不可变；CLI `target-agent pattern review`。
- **垂直子工作流注入**：LangGraphRuntime 域内 Planner 自动从配置模式库构建 few-shot 提示，命中以 `planner_pattern_hints` trace 持久化；项目级 Planner 保持原有注入。
- **消融回归**：`benchmark/pattern_ablation.py` 离线度量 18 个公开疾病（normal 桶）的模式覆盖率与确定性计划有效性，支持 `--llm` 真实 Step 对比；benchmark 新增 BM-12 单元检查。当前基线：10 条种子模式命中 14/18 疾病（77.8%）、计划有效性 18/18；SLE/银屑病/ALS/黑色素瘤暂无命中，是 Gold 标注的优先补位方向。检索打分已过滤“disease/cell/type”等泛化词并要求实质性匹配，避免一个微胶质模式命中全部疾病。
- **配置**：settings 新增 `TARGET_AGENT_PATTERN_CURATION`、`TARGET_AGENT_PATTERN_REVIEW_LEDGER`、`TARGET_AGENT_PATTERN_EXTRACTION_AUDIT`。

### 7.2 已确认边界

- 当前种子模式库（10 条）覆盖率低，属于预期状态；覆盖率提升依赖人工 Gold 标注与批量抽取，不做虚高门槛。
- 抽取采用摘要或 Methods 有界文本，不保证覆盖论文全部细节；`source_material` 与提示版本留痕。
- 对齐数据生成与 Planner/Reviewer 小模型训练按团队决定延后，模式库作为其未来数据来源。
## 8. 论文RAG入图与盲测RAG覆盖率（P2.7，2026-08-08）

### 8.1 已完成
- 机制证据图新增 strategy_paper 节点与 paper_strategy_hint 边：RAG 命中经确定性基因提及匹配投影入图，全部标记 strategy_only/not_evidence、claim_class=INFERRED、weight=0、evidence_ids 为空；新增 paper_strategy_hints 统计与明确 limitation。
- 隔离性保证：RAG 命中不改变 lane_coverage、方向冲突、证据依赖、pattern_links 或排名；新增 4 项测试覆盖节点投影、隔离性、畸形行跳过与 build_mechanistic_graph 兼容。
- 工作台“机制证据图”面板新增“论文RAG策略提示”指标，并标注“仅作策略提示、不是证据、不进入排序”。
- benchmark/pattern_ablation.py 新增 --rag 离线分析：报告各疾病 RAG 命中数、覆盖率、平均命中数与“RAG lane 与计划 lane 对齐”指标；benchmark runner 新增单元检查 paper_rag_graph_projection。
- 队友 PR 12（context-relation benchmark，145 例疾病-靶点-组织-细胞-阶段 goldset）审查后并入产品分支，作为上下文关系评测资产；评审标签保持 scoped。

### 8.2 边界
- RAG 命中是策略提示而非证据，不进入排名与 TargetCard。
- RAG 覆盖率是对当前 155 chunks / 59 篇种子语料的度量；扩充 gold 语料后需重跑 --rag 消融。
- 对齐数据生成与 Planner/Reviewer LoRA（P3）仍按团队决定最后再做。
## 9. Gold 论文提名工具（P2.8，2026-08-08）

### 9.1 已完成
- 确定性提名：`src/target_agent/gold_nomination.py` 对候选语料（仅元数据）按期刊权重、查询桶、标题证据层信号（genetics/perturbation/single_cell/mechanism/target_drug）、RAG 覆盖率缺口疾病加分（UC/银屑病/SLE/ALS/黑色素瘤）与基础生物学扣分排序，无模型、无网络、完全可复现。
- 提名仅作建议：不写入 curation 台账；论文须经 life_science + engineering 双人 `pattern curate` 后才成为 gold。
- 输出：`paper_strategy/nominations.jsonl`（append-ready JSONL，每条带自洽 digest）+ `nominations_MANIFEST.json`（逐行 SHA-256）；CLI `target-agent pattern nominate --corpus --out --limit --min-score --year-min`。
- 配置：settings 新增 `TARGET_AGENT_PATTERN_NOMINATION`。
- 测试：`tests/test_gold_nomination.py` 覆盖确定性排序、资格过滤（status/年份/最低分/limit）、缺口疾病加分、基础生物学惩罚、写入/读取/篡改校验与真实语料提名数量区间。

### 9.2 边界
- 提名分数是排序优先级，不代表论文科学质量，也不自动进入模式库或 RAG 语料。
- 标题信号只做初步召回，理由供人审核对；最终 gold 判定依据全文/摘要的完整评审。
- RAG 缺口疾病（UC/银屑病）为下一批 curation 的优先补位方向，但不保证提名中必有对应论文。
## 10. 自然语言问题录入（P2.9，2026-08-08）

### 10.1 已完成
- `src/target_agent/question_intake.py`：把自由文本研究问题转成“可审阅的项目草案”，不保留、不执行任何东西。
- 提取门控：显式 hints > 策展疾病库匹配（规范名 + MONDO/EFO ontology）> LLM 提案 > 缺失；缺失字段保持缺失，不虚构组织/细胞类型/阶段/表型。
- 库上下文只作为 `library_context_suggestion` 提示，绝不注入项目；疾病不在库中、字段缺失/低置信、LLM 不可用都会标记 `needs_review` 并给出 review_notes。
- 凭据拦截：问题文本含 `sk-...` 类 token 时直接拒绝；无疾病可建立时返回 `QuestionNeedsInput`（Web 422 / CLI 退出）。
- 入口：CLI `target-agent ask --question ... [--disease/--tissue/--phenotype/--create/--output]`；Web `POST /api/questions` 返回草案，工作台“新建项目”区新增自然语言提问 + “AI 解读并填入表单”。
- 测试：`tests/test_question_intake.py` 覆盖库疾病识别、无 LLM 回退、hints 优先、LLM 字段采纳与问题改写、LLM 故障回退、缺疾病拒绝、凭据拒绝、Web 端点与 CLI 打印。

### 10.2 边界
- 草案不是最终研究目标：用户必须审阅 review_notes，创建后才成为不可变项目。
- LLM 只做结构化建议；疾病规范化和上下文门控由确定性代码决定。
- `--create` 只 reserve 项目，不执行任何研究步骤。


## 11. 可执行工作流模板（P2.10，2026-08-08，产品化重构第一刀）

### 11.1 已完成
- `WorkflowTemplate` / `WorkflowModuleSpec` 契约（`src/target_agent/workflow_catalog.py`）：模板声明允许模块、必需模块、依赖 DAG、human checkpoints、`max_work_items`；严格 schema（未知字段拒绝）、模块唯一、无环、至少一个必需模块。
- `WorkflowCatalog`：加载 `workflows/*.yaml`，逐模板 SHA-256 摘要；目录缺失、YAML 损坏或模板非法时 fail closed。
- Planner 模板驱动（`research_planner.py`）：确定性计划只执行模板必需模块；LLM 只能在模板 allowlist 内增删可选模块；必需模块保护字段、依赖与 review/report 门禁保持与旧版一致；模板变更（SHA-256 不匹配）直接拒绝。
- Runtime 双保险（`research_runtime.py`）：每次加载/恢复计划都通过 `WorkflowCatalog.validate_plan_modules` 重新校验项目冻结模板；模板 id + SHA-256 绑定，模板文件变更后旧项目拒绝执行。
- 服务层（`research_service.py`）：`workflow_templates()` 公开无密钥模板摘要；`build_disease_project(workflow_template=...)` 与 `build_generic_project(workflow="literature_review")` 创建时绑定模板与摘要。
- 产品面：CLI `target-agent workflows list|show`、`init --workflow`；Web `GET /api/workflows`、工作台“研究工作流”下拉框；`TARGET_AGENT_WORKFLOW_CATALOG` 配置。
- 两个开箱模板：`workflows/disease_to_target.yaml`（疾病 → 靶点证据包）与 `workflows/literature_review.yaml`（文献 → 假设 → 独立评审 → 报告）。
- 测试：`tests/test_workflow_catalog.py` 覆盖模板契约、DAG 校验、allowlist、max_work_items、Planner 模板路径、Runtime fail-closed、服务构建器与摘要绑定。

### 11.2 边界
- 可选模块（如 disease_to_target 模板中的 literature_search / hypothesis_generation）只允许 LLM 在模板内增加，确定性回退不执行它们。
- 模板是产品契约；新增工作流 = 模板 YAML + 已注册模块，不需要改 Planner/Runtime 主链。
- 对齐数据生成与 Planner/Reviewer 小模型训练（P3）仍按团队决定最后阶段执行。

## 12. Streamable HTTP MCP（P2.11，2026-08-08）

### 12.1 已完成
- `target-agent-mcp` 与 `target-agent mcp-serve` 支持 `--transport stdio|streamable-http`、`--host/--port/--path`；同一 `ResearchProjectService` 薄适配器无第二套逻辑。
- `mcp_server._serve()` 统一路由两种传输，新增 `tests/test_mcp_http.py` 覆盖参数路由与 server 可运行性。
- README 与产品重构文档同步更新；HTTP MCP 让 Target 可嵌入 OpenScience/Wisp/SciForge 等宿主工作台。

### 12.2 边界
- HTTP MCP 仍是本地/可信网络绑定（默认 127.0.0.1）；认证与多租户属于后续平台化增量。
- 不暴露任意 shell/模型生成代码执行，工具面与 stdio 完全一致。

## 13. 研究会话层（P2.12，2026-08-08）

### 13.1 已完成
- `ResearchSession` / `SessionMessage` 契约与 `ResearchSessionStore`：项目目录内 append-only JSONL 会话账本；每条消息带内容 SHA-256，读取时校验，篡改即报错。
- `ResearchSessionService`：创建/列出会话、追加消息、`ask_agent` 返回当前项目快照的确定性摘要；摘要明确 `source_bound=false`，是只读视图，永不创建或修改科学状态。
- Web API：`POST/GET /api/projects/<id>/sessions`、`GET /api/projects/<id>/sessions/<id>`、`POST /api/projects/<id>/sessions/<id>/messages`；项目不存在返回 404，空文本/非法 ID 返回 400。
- 测试：`tests/test_research_session.py` 覆盖账本往返、篡改检测、服务层错误路径、确定性摘要与 Web 四端点；远程全套 401 passed / 0 failed / 2 skipped。

- 会话式干预（P2.13）：`POST /api/projects/<id>/sessions/<session_id>/interventions` 用显式、确定性的动作路由（`accept_checkpoint` / `decide_repair` / `decide_fork`）执行审批，自然语言只作为 rationale 写入决策；决策仍由 ResearchProjectService 写入项目账本，会话仅追加记录用户指令与结果视图；批准类动作自动排队恢复执行。
- Web 工作台新增“研究会话”面板：选择项目自动创建会话、消息气泡展示、发送/询问 Agent、按 next_actions 一键“批准/拒绝检查点、修复、回退”并记录到会话；前端资产测试同步扩展。
- 会话补充输入闭环（P2.14）：干预动作新增 `propose_fork`，会话内提交按工作项的 JSON `input_overrides` → 走不可变 fork + redo + 人工批准 + 自动重跑；spec/plan 仍不可原地修改。Web 工作台在 `needs_input` 状态显示“补充输入并重跑”面板，解析 JSON 后经干预端点发起回退。
### 13.2 边界
- 会话是“视图”，计划、结果、证据、决策与发布仍以项目账本为唯一真相；Agent 回答永不写入科学状态。
- 自然语言批准/拒绝已通过干预端点接入 checkpoint 流程；会话内“补充输入”（为检查点补字段后继续）尚未接入，仍通过现有 decisions/repairs/fork API 操作。
- 多角色会话（研究者/审稿人/管理员）与 MCP 会话工具属于下一增量。

## 14. 部署与密钥管理（P2.15，2026-08-08）

### 14.1 已完成
- 单命令启动：`target-agent up --port <port>` 先跑 `doctor`（必需依赖缺失直接拒绝启动），打印启动摘要（LLM 配置、keyring 后端、目录可写），再以 Waitress 正式启动工作台；`serve` 与 `up` 共用同一 `_start_workbench` 路径。
- OS keyring（可选，Wisp 式）：`target-agent secrets status|set|delete`；`pip install -e ".[secrets]"` 后密钥可存系统钥匙串。解析优先级固定为 进程环境 > .env > OS keyring；keyring 后端不可用时 failure-soft，不影响 .env 部署。`doctor` 输出 keyring 后端与各密钥 configured/not configured，不打印值。
- 只读分享包校验（OpenAI4S 式）：`project-package-inspect` 现在不导入、不落盘，逐文件校验 MANIFEST 的 SHA-256；篡改包直接报 checksum mismatch。导出本身已拒绝 secret-like 文件，导入仍先校验后提交。
- 测试：`tests/test_secret_store.py`（伪造 keyring 后端，覆盖读写删、无后端降级、Settings 填充优先级、doctor 不泄露值）、`tests/test_cli_product.py`（up 先检查后启动、缺依赖拒绝、secrets CLI）、`tests/test_project_package.py`（inspect 篡改检测）；远程全套 419 passed / 0 failed / 2 skipped。

### 14.2 边界
- keyring 是可选依赖（extra `secrets`），无 keyring 时 .env/进程环境照常工作。
- 单命令启动只做配置与依赖检查，不自动安装依赖、不申请端口之外的资源；多用户认证与租户隔离仍未实现。

## 15. 产品旅程总门禁（P2.16 验收，2026-08-08）

- 新增 `tests/test_product_acceptance.py`：以产品界面（Web API + 会话 + 干预 + 项目包）完整走一遍“问题提出 → 计划检查点 → 会话批准 → 执行 → 发布检查点 → 会话批准 → 完成 → 会话摘要 → 导出/只读校验/导入”，并校验报告产物内容与导入项目完整性。
- 该测试覆盖 checkpoined 模式全流程，证明工作流控制面、会话层、项目包三块产品能力是同一个闭环，而不是各自独立的 demo 零件。
- 远程全套 420 passed / 0 failed / 2 skipped。

## 16. 多角色会话与 MCP 会话工具（P2.17，2026-08-08）

- 会话增加角色字段（researcher / reviewer / admin / viewer，默认 researcher）；viewer 会话只读，可提问与读取，但干预端点直接拒绝（400）。
- Web `POST /api/projects/<id>/sessions` 接受 `role`。
- MCP 新增 5 个会话工具：`target_create_session`、`target_list_sessions`、`target_read_session`、`target_post_session_message`、`target_session_intervene`；与 Web/CLI 共用同一个 `ResearchSessionService`，外部工作台（SciForge/OpenScience/Wisp 等）可以直接驱动会话与审批闭环。
- 测试：viewer 门禁、角色往返/校验、Web 角色透传、MCP 真实项目会话全流程（建会话 → 列表 → 提问 → 读消息 → propose_fork 干预）；远程全套 424 passed / 0 failed / 2 skipped。

## 17. 只读分享门户与工作台角色 UI（P2.18，2026-08-08）

### 17.1 已完成
- 新增 `src/target_agent/share_portal.py`：把项目账本安全投影渲染成**单文件离线 HTML 审查页**（无后端、无网络、无外部脚本/样式）。
  - 展示：数据边界说明、研究问题与上下文、执行计划（含修订与回退分支）、工作项结果、评估记录、事件时间线（最近 200 条）、决策记录、产物清单与报告/简报预览、审查边界与证据缺口、待处理修复请求、评审目标、待办动作。
  - 页面内嵌规范 JSON（`PORTAL_DATA`），带**快照指纹**（SHA-256）：同一账本状态的两次渲染指纹一致，可直接比对。
  - 脱敏：密钥类字段、绝对路径、邮箱、IP、SSH 公钥与 `key=value` 凭据在渲染前统一 scrubbed；工具运行内部 ID 与会话原始消息不进入页面。
- 两种来源：`render_share_portal_for_project(projects_dir, project_id)` 直接渲染活项目；`render_share_portal_from_package(archive)` 只读校验 zip 包（MANIFEST + 逐文件 SHA-256）后解压到临时目录渲染，不导入、不落盘、不改动任何项目。
- 产品面：
  - CLI：`target-agent share --project-id X --output X.html [--max-preview-bytes N]` 或 `--input package.zip --output X.html`。
  - Web：`GET /api/projects/<id>/share` 直接返回 HTML；工作台运行栏新增“分享审查页”按钮。
- 工作台角色 UI：新建会话时可选角色（研究员/审阅者/管理员/只读查看）；会话卡片显示角色标签；viewer 会话隐藏审批、修复、补充输入按钮并显示只读提示（后端干预端点本就 400 拒绝，前端只是不误导）。
- 测试：`tests/test_share_portal.py` 覆盖离线单文件属性（中文、无外部资源、无敏感标识、指纹 64 位）、内嵌 JSON 可解析、报告预览、包渲染与活项目渲染指纹一致、Web `/share` 路由、viewer 只读门禁与前端门禁资产断言、payload 脱敏；远程全套 **429 passed / 0 failed / 2 skipped**。

### 17.2 边界
- 分享页是“某一时刻的审查视图”，不是实时控制面；权威来源仍是项目账本与导出项目包。
- 产物预览默认只含 `project_brief` 与 `research_report`（各 ≤64KB，可配）；其余产物只列元数据与校验和，内容需导出项目包获取。
- 分享页不包含会话原始消息；多用户认证与配额仍属于后续平台化增量（P2.19 按需）。

## 18. 容器化部署（P2.19 前半，2026-08-08）

### 18.1 已完成
- 新增 `Dockerfile`（python:3.11-slim，`TARGET_EXTRAS` 可扩展 omics 后端；健康检查、`/data` 持久卷、`target-agent serve --host 0.0.0.0 --port 8888` 入口）与 `.dockerignore`（排除 .env/.git/缓存/大数据）。
- 新增 `docker-compose.yml`：单命令 `docker compose up -d --build`；Compose 自动读取仓库根 `.env` 做变量替换（密钥不写入镜像层）；端口 `TARGET_PORT` 可覆盖；命名卷 `target-data:/data`；健康检查对齐 `/healthz`。
- 新增 `singularity/target.def`：HPC 无 Docker daemon 时用 Singularity 构建同一套代码；国内集群无法直连 Docker Hub，默认使用匿名可达镜像 `docker.1panel.live/library/python`（可换回 `python:3.11-slim`）。
- 新增 `docs/DEPLOYMENT.md`：本机 pip / Docker Compose / HPC Singularity 三种部署、密钥与数据约定、部署验收命令与边界。
- 新增 `scripts/check_deployment_assets.py`：静态部署资产门禁（Dockerfile/Compose/Singularity/runbook 结构完整性），无 Docker 也能先卡住缺文件或契约漂移。
- 远程真实验证（PBS + Singularity，计算节点）：资产检查 32/32；`singularity build` 成功（target.sif ≈79MB）；容器内 `target-agent doctor`、`export-schemas`、`serve + /healthz + /api/capabilities` 全部 OK。

### 18.2 边界
- Docker/Compose 资产已静态校验，但集群无 Docker daemon，未在本环境执行 `docker compose up`；同一代码路径已在 Singularity 容器内完成运行验证。
- 多用户认证、配额与租户隔离仍未实施（P2.19 后半，真实多用户部署需要时再做）。

### 18.3 Live product journey (2026-08-08, verified on remote GPU node)

- Real end-to-end journey through the durable product layer with Step 3.7 Flash:
  healthz -> project create -> Step planner (step:step-3.7-flash + pattern-fewshot + paper-rag + skills)
  -> human plan approval -> execute literature workflow (project_brief, literature_search over Europe PMC,
  hypothesis_generation, independent_review, research_report) -> release gate approval -> completed.
- Session ask returned a source-bound summary; export package checksums validated; import into a second
  store kept status=completed and passed integrity checks.
- Live finding fixed: Step 3.7 Flash returned assumptions as a string, failing the strict pydantic
  contract and degrading the journey to needs_input. The hypothesis-generation prompt now declares
  assumptions as an array of strings; re-verified live with 15 real Europe PMC records (5/5 hypotheses
  validate). Safe rejection behavior is unchanged: no invalid hypothesis is accepted.

### 18.4 Live Alzheimer target-discovery journey (2026-08-08, verified on remote GPU node)

- Real Step 3.7 Flash planning (step:step-3.7-flash + pattern-fewshot + paper-rag + skills) and a real
  child target-discovery chain: disease resolver -> GEO search/audit -> bulk omics -> pathway ->
  omics candidates -> genetics/trials -> literature RAG -> review -> ranking -> report.
- Output: 5 ranked candidates (APP/PSEN1/PSEN2 CONDITIONAL_GO, APOE/CR1 INSUFFICIENT_EVIDENCE),
  5 TargetCards, 12KB traceable report, 58 evidence items, 19 Reviewer findings; project terminal
  state completed_with_gaps with release snapshot. Export -> checksum verify -> import preserved
  status and integrity.
- Second live finding fixed: LangGraph runtime emitted planner_pattern_hints and
  planner_paper_evidence trace events whenever the Step planner used pattern few-shot or paper RAG,
  but TraceEvent.event_type did not allow them, crashing the child runtime with ValidationError at
  intake. Both types are now part of the contract, with a regression test over the hinted-planner
  path. Full remote suite: 430 passed / 0 failed / 2 skipped; repo policy OK; deployment assets 32/32.

## 19. 2026-08-09 全项目审计与修复轮

- 四份审计报告已写入 `../review/`：产品（[audit_product.md](../review/audit_product.md)）、科学（[audit_scientific.md](../review/audit_scientific.md)）、架构（[audit_architecture.md](../review/audit_architecture.md)）、安全（[audit_security.md](../review/audit_security.md)）；对应修复任务见 [task_impl_product.md](../review/task_impl_product.md)、[task_impl_architecture.md](../review/task_impl_architecture.md)、[task_impl_security.md](../review/task_impl_security.md) 与科学审计任务 [task_audit_scientific.md](../review/task_audit_scientific.md)。
- 产品修复轮（impl_product）覆盖：CLI 补齐 fork/branch/session 子命令；Web 工作台错误 detail 展示、创建防抖与二次确认、审批角色接线、合同版本能力条；文档一致性（README / DEMO_GUIDE / COMPLETED / PRODUCT_REFACTOR / PRODUCT_RECONSTRUCTION / product_status.html）；首个 GitHub Actions CI 门禁（`.github/workflows/ci.yml`，由父任务统一评审）；benchmark 过期报告标注与远程重跑命令（`../review/benchmark_rerun.pbs`）。
- 本仓库 Streamable HTTP MCP 状态统一为：**基础 transport 已完成（本地/可信网络绑定）**；认证、多租户与生产级发布未完成。

### 19.1 2026-08-09 第三轮全量检验与修复封口
- 第一轮全量 pytest（494 tests）发现 25 失败，经修复与第二轮失败集重跑后仅剩 2 项；第三轮完成两项最终修复：
  - 修复闭环实质复查（`_finding_recheck_substance`）按 finding 记录的实际类别执行检查：纯 coverage-gap 补充不再被 missing-provenance 的原始质症检查错误拦截，而真正的 provenance 缺失 finding 仍保持实质检查；
  - restore fork 的 head-backed 恢复 attempt 记载到活动 fork 点下，统一了旧测试与新恢复设计的记账期望（基础项保持单条历史 attempt，活动 fork 点为 2 条：redo attempt + restore attempt）；
- 全量远程 pytest 重跑：**492 passed / 0 failed / 2 skipped**（2026-08-09 远程 round3 全套，共 494 tests，含摘要行）；repo policy 门禁 OK；
- benchmark 重跑刷新：13 tasks / 29 assertions / score 1.0（BM-01~BM-13 全 PASS），`benchmark/results/benchmark_report.{json,md}` 已同步；
- 修复 benchmark 脚本将 pytest/policy 日志写入仓库内 `review/` 的问题：日志输出改为 WORKDIR 同级 `review/`（仓库外），避免远程执行资产（绝对路径/主机名）进入仓库并触发策略门禁；
- 已按团队指示推送 `main`（`2c4bc76`）；后续合并/发布保留团队评审。

## 20. 2026-08-09 Round-3 产品收口

- Dockerfile 与 Singularity 默认数据目录与持久化挂载点 `/data` 对齐（projects、runs、cache、input 四个子目录）：裸 `docker run -v target-data:/data` 与 `singularity ... --bind ...:/data` 不再把数据落在容器层（原 P2-1 修复补完）。
- README benchmark 表述精确化：`goldset_v2.jsonl` = 13 非 live 任务（BM-01~BM-13）+ 3 可选 live 任务（BM-L1~BM-L3）；已刷新报告为 13 tasks / 29 assertions / score 1.0（非 live），live 需显式 `--live`。
- Web 工作台角色接线补完：fork 发起与会话消息 actor 从当前会话角色派生，不再硬编码 scientist/researcher；只读会话不能发起回退。
- PRD 状态刷新：P0.1–P0.4 标记为已完成（2026-08-09 验收），下一阶段优先级更新为 P1.2 对齐数据（延后）→ P1.3 盲测 → P1.4 专家 release → P2 产品交付。
- product_status.html 修正重复条目并刷新时间戳；DECISION_LOG 补充 2026-08-09 记录。
- 静态门禁复验：`repo_policy_check` OK；部署资产 32/32；`node --check app.js` 通过。全量 pytest 仍按约定在远程 profile 执行。

## 21. 2026-08-09/10 Round-3 科学线与质量线收口（远程验收）

- 遗传学车道增强：非回文 EAF 一致性校验（direct/swapped 用 1-EAF 互补，阈值 0.15，不一致进入 rejection reasons）；遗传 context 匹配明细（20+ 组织与 20+ 细胞类型同义词规范化，输出 matched_terms/match_rules）；coloc/harmonized 列映射增加可选 EAF 字段（向后兼容）。
- 排名与 Reviewer：强反对证据（FACT/OBSERVED 且方向明确）与已知安全性阻断 -> NO_GO；Reviewer 因果语言检测扩展到 uncertainty/effect 载荷并扩充中英文因果词表；Reviewer 载荷补充 data/code version、source_span、quality_flags、effect 与 genetic 摘要。
- 修复控制面：自由子上下文拆分必须锚定证据自身声明的上下文；同上下文数据集替换按 context match、元数据置信度与独立样本量择优，不再盲目选择第一个合格项。
- 外部数据快照可审计性：Open Targets 与 ClinicalTrials 缓存改为信封格式（schema_v1 + retrieved_at + response_digest + payload），兼容旧缓存；证据 version_meta 与 outputs 记录时间戳/摘要。
- 连续表达矩阵保护：非负、整数且 max>1000 的矩阵判定为疑似原始/未取对数计数并拒绝进入固定 limma；无法自证归一化时打 normalization_unverified 标记。
- GEOMetadataAudit：TCGA/TARGET/GTEX/GDC barcode 识别、生物单元覆盖率与 fallback 明细、疾病同义词命中检查（GEO 候选疾病不匹配即拒）。
- 对齐与基准：LoRA heldout safe_action 语义收紧（空动作/approve/finalize/GO 一律不安全）；benchmark 新增 reference-gene 覆盖断言与证据级因果扫描；盲测 GO 白名单计数修正；split 级 context-relation 指标。
- 质量门：CI 收集必红修复（pandas 等可选重依赖全部改为函数内懒加载）；新增 60 个直接覆盖（候选门禁、resume 共享校验、NO_GO 语义、快照元数据、连续表达保护等）。
- Schema 全量重导出：54 个生成 schema + 1 个手写 context-relation 基准 schema，导出与仓库一致。
- 远程验收（gpu03 / agenttest / 35 核）：全量 pytest 559 collected / 557 passed / 2 skipped；benchmark 13 tasks / 29 assertions / score 1.0；repo_policy_check OK。
