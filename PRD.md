# Target 产品化后续 PRD

> 文档状态：下一阶段输入；当前基线（V2.2 科学主链 + V3 项目控制面 3.1.0 + Round-4 控制面/盲测协议/对齐重训）已于 2026-08-10 通过远程验收（571 passed / 2 skipped；benchmark 14 tasks / 30 assertions / score 1.0；repo policy OK；GitHub Actions CI 绿）。后续开发按团队决定执行。
> 产品对象：面向疾病靶点发现的可追溯生命科学科研 Agent，而非通用聊天助手、独立评测平台或湿实验自动化系统。

## 1. 产品目标

Target 要把一个疾病靶点问题从“提出”推进到“可审查的研究决策包”：理解疾病与上下文，制定计划，检索并分析遗传学、组学、扰动、文献、药物和安全性证据，形成候选排序、TargetCard、可证伪实验方案，并在证据不足、冲突或越界时可靠降级。

北极星结果不是“给出一个基因名”，而是：

- 结论能回链到数据、来源片段、工具运行、参数、版本和不可变产物；
- `FACT / OBSERVED / PREDICTED / INFERRED` 不混写；
- 反方证据、安全阻断项和证据缺口不会被总分隐藏；
- Reviewer 的阻断项可以触发受政策约束的修复、重算与复核；
- 最终发布绑定精确证据快照，并能被专家批准或拒绝；
- 能通过 HTTP/MCP 嵌入现有科研工作台。

## 2. 目标用户与核心任务

### 2.1 目标用户

- 转化医学、生物信息学和药物研发研究者；
- 需要做靶点尽调、疾病机制分析或实验优先级判断的项目团队；
- 希望把 Target 作为能力服务嵌入内部平台的研发团队。

### 2.2 首要场景

1. 从疾病和上下文出发发现候选靶点；
2. 从 GWAS/精细定位/共定位输入解析候选基因；
3. 从公开 bulk 和单细胞组学数据发现并验证靶点；空间组学作为后续独立工作流，不在当前近期承诺中；
4. 对已有候选靶点做系统尽调；
5. 为候选靶点匹配药物并设计验证实验。

第一阶段仍以“疾病到靶点”为主路径。药物组合、专利全景、临床决策和自动湿实验不进入近期范围。

## 3. 产品原则

1. **科学合同优先**：结构化合同是系统真相，报告只能从权威记录生成。
2. **控制面与科学面分离**：项目、计划、执行、审批、恢复、修复与发布由可靠性控制面负责；领域工具负责科学计算。
3. **追加而非覆盖**：旧计划、结果、评审和产物保留；新结果通过 revision/supersession 成为当前有效视图。
4. **默认拒绝的修复策略**：LLM 可以建议，确定性策略决定能否执行；自由文本不得直接变成工具调用。
5. **快照绑定**：审批、评审、修复和发布均绑定精确 digest；输入或产物变化后旧批准失效。
6. **真实性优先于完整外观**：不覆盖就标记 `not_covered`，不确定就降级，不能用流畅文本补齐证据。

## 4. 已有基线

当前仓库已经具备：动态公开组学发现与受控分析、受限遗传学输入审查、多证据融合、TargetCard 与实验计划、Evidence Store/Trace、确定性 Reviewer、项目级持久化控制面、HTTP/stdio MCP，以及第一种项目级修复：对经项目执行/完整性评审确认的 transient、side-effect-free、replay-safe 模块执行相同输入的有界子图重跑，并重新评审和生成绑定新快照的 release decision marker。类型化领域 finding 已能触发受政策约束的自动补证：R0 派生层修正与 R1 同范围补证自动执行，R2 证据排除需 checkpoint，R3 问题/真值/阈值变更禁止自动；LangGraph 与 legacy 双引擎已实现 parity，论文模式 few-shot 与摘要 RAG 已接入两端 Planner，对齐数据资产（120 SFT / 60 preference / 30 held-out）与 Reviewer LoRA 管线已就绪（训练延后）。

详见 [COMPLETED.md](COMPLETED.md)。这一基线不等于已形成开放世界产品：外部盲测、跨疾病专家数据、通用科学修复、并发多用户服务和生产级权限仍未完成。

## 5. 目标架构

```text
Research Project / Frozen Goal
  -> Versioned Plan + typed Work Items
  -> Capability Registry + bounded execution
  -> Evidence Store + immutable Artifact Versions
  -> Scientific Reviewer + structural Reviewer
  -> typed Repair Directive + deterministic Repair Policy
  -> affected-subgraph recomputation + re-review
  -> Target ranking / TargetCard / Experiment Plan
  -> snapshot-bound Expert & Release Gate
  -> HTTP API / MCP / Workbench
```

参考项目带来的设计取舍：

- [SciForge](https://github.com/AGI4Sci/SciForge)：快照绑定决定、项目更新与独立 release gate；
- [OpenScience](https://github.com/synthetic-sciences/openscience)：Reviewer 审查不可变 artifact snapshot，而不是变化中的工作区；
- [OpenAI4S](https://github.com/PKU-YuanGroup/OpenAI4S)：计划恢复、CAS 与 settled-step 语义；
- [Wisp Science](https://github.com/xuzhougeng/wisp-science)：权威 workflow/attempt/delivery 状态与 capability-scoped MCP。

Target 的差异化不是复制通用工作台，而是在这些控制面原则上叠加靶点发现专用证据合同与科学门控。

## 6. 后续需求与优先级

### P0：把当前可靠性闭环做完整（已完成，2026-08-09 验收）

> 状态：P0.1–P0.4 已实现并通过 2026-08-09 全量 pytest（492 passed / 2 skipped）与 benchmark（13/13 tasks、29/29 assertions）；2026-08-10 Round-4 全量 pytest 571 passed / 2 skipped、benchmark 14/14 tasks、30/30 assertions；以下条目保留为设计与验收记录，详见 [COMPLETED.md](COMPLETED.md)。

#### P0.1 工作尝试与不可变产物版本

- 新增 `WorkAttempt`、`WorkItemHead`、`ArtifactVersion`、`ReviewTarget`；
- attempt 永久不可变，当前 head 以 CAS 更新；
- Reviewer 只读取当前 active artifact set，历史记录仍可审计；
- 增加 worker lease、CAS claim、heartbeat、幂等键和 orphan recovery；
- worker 中断后从确定状态恢复，不通过 Trace 猜测业务状态。

验收：在 request、revision、attempt、head、review、report 任一边界模拟中断，恢复后既不重复执行已完成步骤，也不丢失旧版本。

#### P0.2 类型化领域修复

从当前 `same_input_retry` 扩展为判别联合合同：

- 同上下文候选数据集切换；
- 补充/排除证据；
- provenance 恢复；无来源证据只能从 active evidence set 隔离/失效，并以 supersession 保留原记录和原因；
- 来源 Evidence/Claim 永不修改；Agent 综合 Claim 可以由确定性策略新增降级 revision 并 supersede 旧推断，但不得自动升级；
- 上下文拆分；
- 重算依赖项；
- 请求输入、保留缺口、停止/拒绝。

风险分级：R0 派生层修正可自动，R1 同范围只读动作通过门控后可自动，R2 方法或上下文改变必须 checkpoint，R3 改变问题、真值或阈值禁止自动执行。

验收：Reviewer finding 必须形成 `proposal -> policy decision -> execution -> new snapshot -> verification -> resolution` 完整链；LLM 不能直接设置 `resolved=true`。

#### P0.3 数据集替换与依赖失效

当首选组学数据不合格时，只允许选择通过确定性资格审查、且与冻结 TaskSpec 同上下文的候选。至少失效并重算：

```text
recipe -> omics analysis -> enrichment -> candidate extraction
-> evidence fusion -> ranking -> cards -> experiments -> report
```

候选集合变化时，文献、药物、安全性和扰动证据也要重取。工具尝试必须以“步骤实例 + subject key”标识，不能只按 tool name 取最新值。

#### P0.4 Benchmark 真实性修复

- 把 `finding_category`、repair action/outcome、snapshot binding、supersession、下游重算、冲突保留、claim class、禁止措辞和 no-scope-change 变成可执行断言；
- 旧 conflicting-evidence 桶只视为终态/溯源/报告检查，不再宣称验证冲突识别；
- 建立错误注入状态迁移集，而不是只检查最终文件存在。

### P1：形成科学壁垒

#### P1.1 多证据图深化

- 支持真正统计重算的 fine-mapping、coloc 与敏感性分析；
- 引入规范化 eQTL/sQTL、细胞类型与疾病阶段上下文；
- 建立统一 Evidence Graph，保留 stance、effect direction、context 与冲突集合；
- 扩充上下文匹配的扰动 Oracle；预测扰动仍受正式评分上限约束。

#### P1.2 Alignment 数据闭环

- 从真实项目沉淀 Planner、Extractor、Reviewer、Repair 和 Release 决策；
- 覆盖阴性、冲突、缺失上下文、OOD、工具失败、数字错误和正确拒绝；
- 科学与工程角色分离审核；只有通过复核的 CaseRecord 才能晋升训练集；
- 建立数据卡、来源许可、去污染与版本管理。
- **状态（2026-08-10）**：120 行 SFT 双审齐全；Qwen3-8B Reviewer LoRA 重训完成（数据基线 6de64c9，promotion_eligible=true）；heldout 30 行 adapter 全维度 1.0 vs base category_match 0.0；权重不入 Git，仅作可选 Reviewer 后端。局限：heldout 为模板一致合同场景，非开放世界质量。

#### P1.3 外部盲测靶点排名 benchmark

- evaluator 控制 scorer、冻结数据快照与隐藏疾病；
- 专家给出分级相关性、trap、安全阻断和可验证理由；
- 指标使用 disease-macro nDCG/Recall/MRR，加不可补偿的 trap/safety gate；
- 公共输出仅含聚合结果，防止标签泄露。
- **状态（2026-08-10）**：可执行盲测协议与合成夹具 BM-14 已合入（freeze→score、摘要 SHA-256 冻结、trap/safety 非补偿门禁，CI 可自动验证）；外部独立专家标签与主办方控制 scorer 仍待外部提供，无法由仓库内部完成。

验收：至少一个完全未进入开发库的疾病批次，由独立专家完成盲评和分歧仲裁。

#### P1.4 专家审核和 release

- 专家看到精确 Evidence/Artifact snapshot、未解决 blocker 和建议实验；
- 审批具有项目/步骤/一次性作用域与过期时间；
- 计划、输入、排名、TargetCard 或报告变化后旧审批自动失效；
- 区分 `executed / reviewed / scientifically_ready / released / expert_approved`。

### P2：产品交付与生态

- Streamable HTTP MCP、认证、租户隔离、审计与权限作用域；
- 多租户任务队列、资源配额和生产可观测性；
- 连接器 SDK 与能力清单，外部工具不能绕过合同；
- 科研工作台中的项目视图、修复队列、证据图、差异对比和交付下载；
- 公开部署文档、兼容性矩阵、迁移策略和稳定 API 版本。
- **已实现（2026-08-10）**：长任务暂停/取消/恢复控制面（HTTP/CLI/MCP/Web），取消为终态且不生成报告；SSE 事件流与 /healthz、/api/capabilities 已具备。仍待办：认证、租户隔离、生产级多用户配额。

## 7. 核心验收案例

1. GEO 首选数据样本不足，第二个同上下文数据合格：自动切换、旧拒绝保留、下游完整重算。
2. 第二个数据来自不同组织：不得自动替换，返回 `needs_input` 或 `completed_with_gaps`。
3. Europe PMC 首次 503、第二次成功：相同输入 digest、两次 attempt 可追溯、发布只读 active 结果。
4. TPM 输入 DESeq2：不得重试或改阈值，必须换受支持方法/数据或保留缺口。
5. 观察性证据写成“驱动疾病”：降级 Agent Claim，但不篡改原始证据。
6. 匹配上下文中存在相反方向证据：两方均保留，形成 blocker，不能无条件 GO。
7. EvidenceItem 缺 tool run/source span：从正式结论移除或阻断发布。
8. 修复 proposal 之后证据快照变化：旧批准返回 stale conflict。
9. repair/tool/replan 预算耗尽：结束为带缺口终态，不循环。
10. 仅凭 README 和部署 profile，非作者能恢复项目并重现 release digest。

## 8. 非目标

- 自动修改代码、自动训练并发布新模型；
- 允许 LLM 任意执行 shell/Python/R；
- 把预测扰动或相关性包装为疾病因果证据；
- 自动做临床治疗决策或替代专家；
- 以内部 benchmark 满分宣称生物学发现准确率；
- 在没有独立验证的情况下自动控制湿实验。

## 9. 建议顺序与状态

1. 冻结 attempt/head/artifact-version 合同 —— 已完成（P0.1）；
2. 实现同上下文数据集替换及依赖失效 —— 已完成（P0.3）；
3. 把领域 finding 转换为类型化 repair directive —— 已完成（P0.2）；
4. 建立状态迁移 benchmark —— 已完成（P0.4，BM-01~BM-13 与错误注入回归）；
5. 当前状态：P1.2 Alignment 数据闭环已完成（2026-08-10 重训 + heldout 验收）；P1.3 盲测协议/夹具（BM-14）已完成，外部专家盲评标签待主办方提供；P1.4 专家 release gate 与 P2 认证/多租户仍待办。

本 PRD 是当前基线之后的开发输入，不代表其中路线已实现或已承诺发布日期。
