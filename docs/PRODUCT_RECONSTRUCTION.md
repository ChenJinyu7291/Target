# Target 产品化重构（Product Reconstruction）

> 状态：进行中（2026-08-08 启动）  
> 目标：把 Target 从“能跑通 demo 的科研 Agent”重构为**能用的科研工作流产品**：用户给一个问题，系统把它从提出推进到做完——规划、执行真实工具、审查、修复、报告与发布，全程可追溯、可恢复、可导出，并且数据对齐与可靠性不是宣传词而是门禁。

## 1. 参照项目画像与差距

| 项目 | 核心定位 | 值得吸收的产品能力 | Target 现状 | 差距 |
| --- | --- | --- | --- | --- |
| SciForge | 科研 Agent 的人类控制面与证据层 | 关键节点干预面板、Evidence/Project DAG、长期工作区、发布审批 | 已有 checkpoint/repair/fork 审批、证据图、项目控制面 | 缺会话式干预与多角色长期工作区 |
| OpenScience | 本地科研工作台 | 开箱即用、模型供应商路由、290+ Skills 渐进加载、浏览器工作区、blind reviewer 门 | 有 Step/OpenAI 兼容路由、SkillCatalog、Web 工作台 | 缺聊天式会话、文件编辑/终端工作区 |
| OpenAI4S | JSON 编排 + 持久内核双平面 | Action Ledger、plan/review 状态机、持久内核、版本化产物、分享包、doctor | 已有 Ledger/评审/产物/内核/doctor/只读分享包 | Streamable HTTP MCP 基础已完成；缺认证/多租户 |
| Wisp Science | local-first 桌面科研工作台 | 本地/SSH/GPU 执行上下文、OS keyring、MCP 数据库连接器、Skills 渐进披露 | 已有远程 profile、内核执行与 OS keyring | MCP stdio + Streamable HTTP 基础（认证/多租户未完成） |

## 2. 重构后的目标架构

```text
用户入口：CLI（target-agent）/ Web 工作台 / HTTP API / MCP
        ↓
产品控制面：项目生命周期（init → plan → approve → execute → review → release）
        ├─ 可执行工作流模板（workflows/*.yaml：模块、依赖、checkpoint、限制、SHA-256 冻结）
        ├─ 任意步骤回退（redo / restore / fork 审批）
        ├─ 修复队列（repair request → deterministic policy → rerun → re-review）
        └─ 项目包（export / import：可移植、可复现、无密钥）
        ↓
科学引擎：Planner（Step/OpenAI 兼容 + 论文策略 few-shot）
        → Router → 科学工具（GEO/组学/遗传/文献/药物/安全，注册表白名单）
        → Evidence Store → Reviewer → 排名/TargetCard/实验方案 → 报告
        ↓
对齐与可靠性：Pattern 库 → 对齐数据（P3 最后阶段）→ blind benchmark → 专家 release gate
```

## 3. 已完成的增量

### P2.10 可执行工作流模板（本阶段第一刀）

- `WorkflowTemplate` / `WorkflowModuleSpec` 契约：模板声明允许的模块、必需模块、依赖 DAG、checkpoint、`max_work_items`，严格 schema（未知字段拒绝）。
- `WorkflowCatalog`：加载 `workflows/*.yaml`，逐模板 SHA-256，目录缺失/模板损坏时 fail closed。
- Planner 模板驱动：`deterministic()` 只执行模板必需模块；LLM 只能在模板 allowlist 内增删可选模块；必需模块的保护字段、依赖与 review/report 门禁与旧版一致。
- Runtime 双保险：每次加载/恢复计划都重新校验项目冻结模板（id + SHA-256），模板变更直接拒绝项目。
- 两个开箱模板：`disease_to_target`（疾病 → 靶点证据包）与 `literature_review`（文献 → 假设 → 评审 → 报告），证明平台可承载非靶点科研问题。
- 产品面：`target-agent workflows list|show`、`init --workflow`、Web `GET /api/workflows`、工作台“研究工作流”下拉框（Web 端创建项目时绑定模板与摘要）。
- 测试：`tests/test_workflow_catalog.py` 覆盖模板契约、DAG、allowlist、planner 模板路径、运行时 fail-closed、服务构建器与摘要绑定。

### P2.11 Streamable HTTP MCP（已完成，2026-08-08）

- `target-agent-mcp --transport streamable-http --host/--port/--path` 与 `target-agent mcp-serve --transport ...`：同一薄适配器同时支持 stdio 与官方 Streamable HTTP 传输，服务面不变（create/run/inspect/approve/repair/fork/artifact）。
- 新增 `_serve()` 帮助函数与 `tests/test_mcp_http.py`（stdio 与 streamable-http 参数路由、server 可运行性）。
- 意义：Target 现在可作为 HTTP MCP 服务嵌入 OpenScience/Wisp/SciForge 等科研工作台，不需要宿主理解内部存储。
- 边界：基础 transport 已完成（默认绑定 127.0.0.1，本地/可信网络）；认证、多租户与生产级发布未完成。


### P2.12 研究会话层（已完成，2026-08-08）

- 项目目录内 append-only JSONL 会话账本：`SessionMessage` 带内容 SHA-256，读取时校验篡改；会话不是系统真相，计划/证据/决策仍在项目账本。
- `ResearchSessionService`：创建/列表/追加消息/`ask_agent` 确定性快照摘要，明确 `source_bound=false`，不产生也不修改科学状态。
- Web API 四端点（create/list/read/post）；测试覆盖账本往返、篡改、404/400 与摘要确定性。
- 会话式干预（P2.13）：`POST /api/projects/<id>/sessions/<session_id>/interventions` 显式路由 `accept_checkpoint` / `decide_repair` / `decide_fork`；自然语言作为决策 rationale，决策写入项目账本，会话只记录指令与结果视图；批准类动作自动排队恢复。Web 工作台新增会话面板（消息、询问 Agent、一键批准/拒绝），前端不猜测动作类型。
- 会话补充输入闭环（P2.14）：`propose_fork` 干预动作接受 `input_overrides`（按工作项 JSON），走不可变 fork + redo + 人工批准 + 自动重跑；工作台 `needs_input` 状态提供“补充输入并重跑”面板。
- 产品旅程总门禁（P2.16）：`tests/test_product_acceptance.py` 用产品界面走通“建项目 → 会话批准计划 → 执行 → 会话批准发布 → 完成 → 会话摘要 → 导出/只读校验/导入”全闭环，防止各层各自能跑但串不成产品。
- 多角色会话与 MCP 会话工具（P2.17）：会话带 researcher/reviewer/admin/viewer 角色，viewer 只读；MCP 新增 5 个会话工具，外部工作台可驱动会话与审批闭环。
### P2.18 只读分享门户与角色 UI（已完成，2026-08-08）

- 新增 `share_portal.py`：把项目账本安全投影渲染成单文件离线 HTML 审查页（无后端/网络/外部资源），展示问题、计划、结果、评估、事件、决策、产物、缺口与待办；内嵌规范 JSON 并带 SHA-256 快照指纹，同一状态的两次渲染可比对。
- 两种来源：活项目（`target-agent share --project-id`）或只读校验后的 zip 包（`--input`，不导入不落盘）；Web `GET /api/projects/<id>/share` 与工作台“分享审查页”按钮。
- 渲染前统一脱敏：密钥字段、绝对路径、邮箱、IP、SSH 公钥、key=value 凭据；会话原始消息与工具运行内部 ID 不进页面。
- 工作台角色 UI：新建会话可选 researcher/reviewer/admin/viewer，viewer 隐藏审批/修复/补充输入按钮（后端 400 门禁不变）。
- 测试：`tests/test_share_portal.py` 覆盖离线单文件、包/活项目指纹一致、Web 路由、viewer 只读与脱敏；远程全套 429 passed / 0 failed / 2 skipped。

### P2.19 容器化部署（前半已完成，2026-08-08）

- 新增 Dockerfile / .dockerignore / docker-compose.yml / singularity/target.def / docs/DEPLOYMENT.md / scripts/check_deployment_assets.py。
- 远程验证（PBS + Singularity，计算节点）：资产检查 32/32；singularity build 成功；容器内 doctor、schema 导出、serve + /healthz + /api/capabilities 全部 OK。
- 边界：Docker Compose 在本集群无 daemon 未实跑（同一代码路径已在 Singularity 容器验证）；多用户认证/配额/租户仍按真实需要再实施。

## 4. 下一步增量（按价值排序）

1. **多用户认证与配额（P2.19 后半，按需）**：认证、多租户、资源配额，仅在真实多用户部署需要时做。
2. **对齐数据（P3，最后）**：以已评审 CaseRecord 与论文策略模式为来源生成 Planner SFT / Reviewer 偏好对，训练 Reviewer/Planner 小模型并做 blind benchmark 消融。

## 5. 完成标准（本目标）

- [ ] 任一科研问题可经 CLI/Web/API 从提出推进到可审查报告，关键节点可人工干预、任意步骤可回退。
- [ ] 新增科研工作流只需添加模板 YAML + 注册模块，不改 Planner/Runtime 主链。
- [ ] 所有结论回链来源、数据版本、工具运行与参数；FACT/OBSERVED/PREDICTED/INFERRED 不混写。
- [ ] 模板/计划/产物/评审/发布均绑定摘要，变更后旧批准失效；无覆盖就标记缺口。
- [ ] 工作台可会话式使用；HTTP MCP 可嵌入第三方工作台。
- [ ] 对齐数据训练按 P3 最后执行，且有真实 CaseRecord 来源与双人复核门禁。