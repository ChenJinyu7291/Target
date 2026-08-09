# TargetDiscovery Agent 部署手册（P2.19）

本文档把 Target 从“源码可跑”推进到“可部署的服务”：本机 pip、Docker Compose、HPC Singularity 三种方式，并说明持久化、密钥、健康检查与验证路径。

## 1. 方式一：本机 pip（开发/单人使用）

```bash
python -m pip install -e ".[test,mcp]"
target-agent doctor          # 检查依赖与能力，不打印密钥
cp .env.example .env         # 按需填写 STEP_API_KEY 等（.env 不入 Git）
target-agent up --port 8888  # 单命令：先检查再启动
```

- 工作台：http://localhost:8888
- 健康检查：http://localhost:8888/healthz
- 数据目录默认在仓库下 `projects/`、`runs/`、`cache/`，可用环境变量覆盖（见 `.env.example`）。

## 2. 方式二：Docker Compose（服务化/团队共用一台服务器）

前置：Docker ≥ 24 与 Compose v2。

```bash
# 1. 配置密钥（Compose 自动读取仓库根目录 .env 做变量替换）
cp .env.example .env
# 编辑 .env：STEP_API_KEY、STEP_BASE_URL、STEP_MODEL、NCBI_EMAIL 等

# 2. 构建并启动
docker compose up -d --build
docker compose ps             # 健康状态变为 healthy 后可用

# 3. 使用
curl http://localhost:8888/healthz
curl http://localhost:8888/api/capabilities
```

- 端口可用 `TARGET_PORT` 覆盖（默认 8888）；Compose 默认只绑定 `127.0.0.1`，远程访问前请先阅读“安全部署清单”。
- 持久化：命名卷 `target-data` 挂载到容器 `/data`，项目/运行/缓存/输入分别落在 `/data/projects|runs|cache|input`，重建容器不丢数据。
- 停止/升级：
  ```bash
  docker compose down         # 停止（卷保留）
  docker compose pull && docker compose up -d   # 升级（镜像 tag 变化时）
  ```
- 额外科学后端（可选）：把 compose 中 `TARGET_EXTRAS` 改为 `mcp,omics-bulk`（或 `omics-single-cell`）后重新构建；镜像会变大，但差异/通路分析开箱可用。
- 密钥优先级与本地一致：进程环境 > 容器环境变量 > `.env`；密钥绝不写入镜像层（构建不 COPY .env）。

## 3. 方式三：HPC Singularity（集群/GPU 节点）

HPC 通常没有 Docker daemon，但可用 Singularity/Apptainer 构建并运行同一套代码：

```bash
# 1. 在仓库根目录构建（需要 --fakeroot；集群无用户命名空间时请向管理员申请）
singularity build --fakeroot target.sif singularity/target.def
# 国内集群无法直连 Docker Hub 时，定义文件默认使用匿名可达的镜像库（docker.1panel.live）；若该镜像不可用，可换成 dockerproxy.net 等，或把 From: 改回 python:3.11-slim。

# 2. 冒烟
singularity run target.sif doctor
singularity exec target.sif target-agent export-schemas --output /tmp/schemas

# 3. 启动工作台（绑定数据目录到 /data，端口由部署 profile 指定）
mkdir -p $HOME/target-data/{projects,runs,cache,input}
singularity instance start --bind $HOME/target-data:/data target.sif target-agent
singularity exec instance://target-agent target-agent serve --host 0.0.0.0 --port 8888
```

- 环境变量通过 `SINGULARITYENV_STEP_API_KEY=...` 注入（密钥仍在容器外）。
- PBS 作业里把 `singularity run target.sif ...` 作为作业命令即可在计算节点执行；数据目录建议放在共享文件系统。

> ⚠️ 安全提示：容器内 CMD 仍监听 `0.0.0.0`（Docker 端口发布需要）；Compose 默认只绑定到宿主机 `127.0.0.1`。任何远程暴露（公网、跨网络共享）必须先设置 `TARGET_AGENT_WEB_TOKEN`，或将服务置于带认证与 TLS 的反向代理之后。

## 4. 数据与密钥约定

| 项 | 约定 |
| --- | --- |
| 项目账本 | `projects/`（Docker 中 `/data/projects`），项目即目录，可整体导出 zip |
| 运行缓存 | `cache/`，可按分析缓存 key 复用，不入 Git |
| 密钥 | 仅 `.env` / 进程环境 / OS keyring，绝不进 Git、Trace、日志或报告 |
| 校验 | `target-agent project-package-inspect` 只读校验包；分享页带快照指纹 |

## 5. 部署验收

每次部署后至少执行：

```bash
target-agent doctor                        # 依赖与能力就绪
curl -fsS http://localhost:8888/healthz    # 服务与后台健康
curl -fsS http://localhost:8888/api/capabilities
target-agent project-package-inspect --input 任意已导出包   # 包完整性
```

Docker 环境健康检查失败时先看 `docker compose logs target-agent`；常见原因是端口被占用、`.env` 中密钥格式错误或 /data 权限不足。

## 6. 安全部署清单

远程或多人使用前，逐项确认：

- 绑定：本机与 Compose 默认只监听 `127.0.0.1`；不要把 8888 直接发布到公网或非受信网络。
- 令牌：任何远程暴露都必须设置 `TARGET_AGENT_WEB_TOKEN`；启用后除 `/healthz` 外所有 `/api/*` 都需要 `Authorization: Bearer <token>`。
- 反向代理：若经反向代理提供访问，代理必须负责认证与 TLS，且不要把 `/api/*` 透传给未认证客户端。
- 网络隔离：Web 端口与 kernel daemon 端口（默认 8765）只对受信主机开放；kernel daemon 仅监听本机回环。
- Kernel 令牌：多用户主机建议设置 `TARGET_AGENT_KERNEL_TOKEN`；daemon 请求需携带 `X-Kernel-Token` 或 `Authorization: Bearer <token>`。

## 7. 边界

- 当前为单租户部署；多用户认证、配额与租户隔离按“真实多用户部署需要时”再实施（P2.19 后半）。
- 容器默认不含 R/limma 后端与训练依赖；需要时用 `TARGET_EXTRAS` 或单独镜像扩展。