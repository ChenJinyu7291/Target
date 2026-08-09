# Target 产品化重构方案（Product Refactor）

> 状态：2026-08-08 定稿 v1
> 目标：把 Target 从“能跑通 demo 的科研 Agent”重构为“能用的垂直科研产品”：一个问题从提出推进到做完，全程可追溯、可恢复、可导出、可复核，并具备数据对齐与可靠性门禁。

## 1. 参考项目画像

| 项目 | 核心定位 | 值得吸收的产品能力 | Target 现状对照 |
| --- | --- | --- | --- |
| SciForge | 科研 Agent 的人类控制面与证据层 | 干预面板、Evidence/Project DAG、跨会话长期工作区、人类审批节点 | 已有 checkpoint/repair/fork 审批；缺证据图可视化与长期工作区文件浏览 |
| OpenScience | 本地科研工作台（单二进制） | 开箱即用、模型供应商路由、290+ Skills 渐进加载、浏览器工作区（文件树/编辑器/终端）、最终 blind reviewer 门、OpenAPI SDK | 有模型（仅 Step）、工具注册表、Web 工作台雏形；缺供应商抽象、Skills 目录化、导出包 |
| OpenAI4S | JSON 编排 + 持久 Python/R 内核双平面 | Action Ledger、plan/review 状态机、持久内核、版本化产物、会话分享包、doctor/诊断 | 有 Ledger/计划/评审/产物；缺持久内核与会话分享包 |
| Wisp Science | local-first 科研工作台 | 本地/SSH/GPU 执行上下文、OS keyring 密钥、MCP 数据库连接器、Skills 渐进披露 | 有远程执行 profile 与 OS keyring；MCP stdio + Streamable HTTP 基础（认证/多租户未完成） |

## 2. 目标架构（产品化后的 Target）

```text
用户入口：CLI（target-agent） / Web 工作台 / HTTP API / MCP（stdio + Streamable HTTP 基础）
        ↓
产品控制面：项目生命周期（init → plan → approve → execute → review → release）
        ├─ 任意步骤回退（redo / restore / fork 审批）
        ├─ 修复队列（repair request → deterministic policy → rerun → re-review）
        └─ 项目包（export / import：可移植、可复现、无密钥）
        ↓
科学引擎：Planner（Step/OpenAI 兼容供应商 + 论文策略 few-shot）
        → Router → 科学工具（GEO/组学/遗传/文献/药物/安全，注册表白名单）
        → Evidence Store → Reviewer → 排名/TargetCard/实验方案 → 报告
        ↓
对齐与可靠性：Pattern 库（P0/P1）→ 对齐数据（P3 最后阶段）
        → blind benchmark → 专家 release gate
```

### 产品化五条主线

1. **开箱可用**：target-agent init 一键生成研究项目；doctor 诊断；serve 起工作台；README 快速开始。
2. **自带模型**：供应商抽象（默认 Step，支持任意 OpenAI 兼容端点），不再绑定单一厂商。
3. **项目即产物**：项目目录可整体导出为带校验清单的 zip，导入后可继续运行、复核或移交。
4. **工作台可操作**：项目列表、计划/结果/分支/事件/产物浏览、审批与回退、一键导出。
5. **可靠性门禁**：终态诚实降级、evidence 回链、Reviewer 阻断、快照绑定 release；对齐数据训练（P3）最后阶段做。

## 3. 分阶段计划

### P0：产品外壳（已完成）

- [x] Web 项目工作台（列表、resume、审批、回退 UI）
- [x] 模型供应商抽象（LLM_PROVIDER=step|openai-compatible）
- [x] target-agent init：脚手架项目 + 说明
- [x] target-agent project-export / project-import：可移植项目包（manifest + SHA-256）
- [x] Web GET /api/projects/<id>/export 与导出按钮
- [x] README 快速开始 + 验收：263 回归通过，导出/导入往返一致

### P1：科研工作区

- [x] 证据图 / 项目 DAG 可视化（借鉴 SciForge）：GET /api/projects/<id>/graph + 工作台 SVG DAG
- [x] Skills 目录化与渐进加载（借鉴 OpenScience/Wisp）：skills/*/SKILL.md + SkillCatalog（SHA-256）+ CLI/Web 查询 + Planner skill_hints 渐进披露
- [x] 项目文件树与报告/产物在线预览：GET /api/projects/<id>/files + files/preview + 工作台面板
- [x] P1 首轮验收：274 passed / 2 skipped（证据图、技能库、文件预览、同上下文数据集切换修复）
- [x] P1 二轮验收：283 passed / 2 skipped（持久内核 + 工作台内核控制台）
- [x] P1 三轮验收：288 passed / 2 skipped（领域 finding 驱动的 R0–R3 修复策略：自动降级/补证、checkpointed 排除、越界拒绝与链式 overlay resolution）
- [x] P1 四轮验收：289 passed / 2 skipped（新增方向一致性与证据独立性确定性门禁）
- [x] 持久 Python/R 执行内核（借鉴 OpenAI4S/Wisp，作为可选用后端）：KernelManager + 守护进程 + CLI/Web 控制台 + 9 项内核测试
- [x] 同上下文数据集切换：SWITCH_DATASET_SAME_CONTEXT 类型化指令（不改变冻结 TaskSpec）+ 端到端验收
- [x] 领域 finding 驱动的补证 / 排除 / 降级全策略（R0–R3）：类型化 finding → DOWNGRADE/SUPPLEMENT/EXCLUDE overlay 链 → 已解决标记 → 链式 Resolution 升级 → 仓库完整性复核
- [x] 确定性证据门禁：方向一致性（同基因反向证据 → `conflicting_evidence` → R2 排除）与证据独立性（同谱系重复引用 → `evidence_dependence` → R0 降级），与子运行 Reviewer finding 去重合并

### P2：平台化

- 认证、多租户、资源配额与观测
- 连接器 SDK（Streamable HTTP MCP 基础已完成；认证/多租户/配额未完成）
- 会话/项目分享包（借鉴 OpenAI4S read-only share）
- OS keyring 密钥管理（借鉴 Wisp）

### P3：对齐数据训练（延后至最后阶段）

- 基于论文策略 P0/P1 与真实 CaseRecord 生成 Planner SFT、Reviewer 偏好对（P0/P1 沉淀已就绪，训练延后）
- 混合方法：模型学习“为什么这篇论文选这个顺序”
- Reviewer/Planner 小模型 LoRA 最小实验
- 盲测靶点排名 + 专家审核验证提升

## 4. 明确不做（产品边界）

- 不允许 LLM 任意执行 Shell/Python/R（保持白名单工具与受控 wrapper）
- 不做自动湿实验、临床决策、自动训练发布
- 不把内部 benchmark 写成外部生物学发现性能
- 不复制 OpenScience 的通用全科研范围；Target 保持“疾病 → 靶点 → 证据包”垂直纵深
