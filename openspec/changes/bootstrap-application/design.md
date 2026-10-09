# Design: bootstrap-application

## Context

采用 DG-BL-2026-09-26 的模块化单体和 MySQL 8/Celery/Redis/Vue 3。用户已确认最小认证前置及事务 outbox。2026-10-09 用户释放 D 盘空间后可用约 85.06GiB，明确要求质量优先、取消严格存储预算。旧 4GiB 上限不再生效；常规建议保留 20GiB 并仅提示，低于 2GiB 时保护写入。依赖和实验按验证需要配置，不无节制浪费。

## Goals / Non-Goals

目标：从登录页面创建受限诊断任务，经数据库和真实 Worker 完成，刷新后状态仍可查询；新环境按说明可复现。

本轮不实现仓库算法或安装 PyTorch/CUDA、XGBoost、LightGBM、SHAP。诊断任务只接受时长，不能执行用户代码、URL、文件路径或 shell 命令。

## Decisions

### 1 工程和依赖

沿用 `apps/api`、`apps/worker`、`apps/web` 与按需创建的 `packages/domain`、`packages/persistence`。Python 单一根配置和 uv 锁，前端独立 npm 锁。API/Worker/投递器/协调器复用同一 Python 包；原生开发只容器化 MySQL/Redis，全容器 Compose 为演示路径。端口默认绑定 127.0.0.1，MySQL/Redis 不复用本机已有实例。

Windows 补充：API/Web 可原生开发；Celery 官方不支持原生 Windows，因此需要 Worker 时使用共享应用镜像的 Linux 容器。第二批先提供 `packages/platform` 配置和连接基础；domain/persistence 业务包及 migration 在第 2～3 组引入，不创建虚假的业务表。

Python 使用项目 `.venv`；启动命令显式绑定项目解释器。密钥从未提交 `.env` 注入。依赖精确版本在实施任务中解析并锁定，不在本文凭空指定。内置诊断 API 仅 development/test 开启，后续生产配置关闭。

### 2 最小权限

细则以 `docs/04` 第 10 节为准。预置账号由显式初始化配置创建；seed 缺少用户名/密码时拒绝，服务缺少签名密钥时拒绝启动，常规启动不自动 seed。密码使用 Argon2id。短期 access JWT 包含 session ID，所有受保护请求检查活动用户和未撤销会话，不仅验证签名。refresh 为随机不透明令牌，仅哈希入库，通过 HttpOnly cookie 传输并轮转。cookie 端点校验 CSRF 与 Origin；默认不公开注册、不保存可用默认密码。

### 3 数据库与可靠投递

MySQL 事务中创建任务、outbox 和关键操作审计，Redis 不可用不回滚已确认的任务创建；持久 outbox 负责补投。投递为至少一次。DB 是状态唯一来源，前端不从 Celery result backend 推断终态。

投递器通过锁/租约领取事件；Worker 原子领取 queued 任务并取得 execution_token。任何进度、业务结果、终态写回都在检查有效执行令牌和租约的事务内进行。重复消息不能启动第二个执行，旧 Worker 不得在租约失效后写回。

心跳每 10 秒、执行租约 45 秒、协调扫描每 15 秒。lease 到期由协调器条件更新为 failed/TASK_LEASE_EXPIRED，再根据重试策略创建 successor。queued 最长等待默认 10 分钟；发布已成功但 Redis 丢失消息仍以可查询失败收敛。任务自动恢复链最多 3 个 successor，手动重试必须作用于当前失败/取消节点并留审计。

outbox 最多 8 次投递，延迟 `min(5*2^(n-1),60)` 秒。未知投递结果允许再次发送；只有原任务仍 queued 才可因超限更新为失败。已 running 的任务不因 outbox 写回中断被改判失败。

### 4 状态、重试与取消

queued 可到 running/cancelled/failed；running 可到 succeeded/failed/cancel_requested；cancel_requested 可到 cancelled/failed。succeeded/failed/cancelled 都是不可变终态；retry 创建新 UUID 任务而非复活原记录。

领取、取消与完成通过锁或带状态/令牌条件的更新裁决：完成先提交则后来的取消返回冲突；cancel_requested 先提交则 Worker 完成路径不能再写 succeeded，应在检查点写 cancelled。stale 是健康标记而非第七个状态。

### 5 存储

可移植默认将 runtime/data/artifacts 和缓存配置在项目所在盘；本机全部在 D 盘。项目命令包装器只为当前命令设置 TEMP/TMP、pip/uv/npm 缓存，退出后恢复，不修改系统变量。Compose 的 MySQL/Redis 具名卷落在已配置的 D 盘 Docker 数据磁盘，volume 的挂载与内容在实际实现时验收。

开发先保持单 Worker，依据 CPU/内存和故障验收调整并发。取消小仓库/1000 提交的全局限制，采集阶段先验证正确性再扩展多个仓库与历史窗口；实际克隆字节、运行时间与可用空间分别测量。保留日志轮转和可核查数据清单，禁止自动全局 prune 或删除其他软件缓存。

## Risks / Trade-offs

- outbox/租约增加实现复杂度，因此先用诊断任务做故障注入，再移植采集流程。
- 当前容量足以按既定中小规模路线推进八模型；深度模型和大仓库仍须评估 CPU/内存/显存与实际增长。
- 应用余量检查不能严格约束 Docker 的全局镜像写入或其他程序占用；每次 pull/build 前还需显式测量。
- 本轮 artifacts complete 仅表示规格文件齐全；未通过业务 E2E 时 tasks 不得勾选实施完成或归档。

## Migration Plan

按实施批次拆分迁移：第三阶段 `0001_auth` 创建 user/auth_session/operation_log，随后显式 seed；第四阶段 `0002_tasks` 增加 async_task/task_outbox。使用随机命名专用开发库及独立新配置/新卷验证升级、空库回滚和非空拒绝回滚。MySQL DDL 隐式提交，跨两个版本回退至 base 时先检查全部受影响表，再删除任何任务表，避免中途拒绝造成部分删表。已有本机平台标记表及认证数据保留。API 启动和 readiness 校验精确版本 `0002_tasks`，缺失/不兼容版本给出安全提示。bootstrap 用户名/密码仅初始化命令必填，常规服务启动不要求或自动创建账号。实际验收证据见第三、第四阶段报告。

## Verification

第五阶段页面采用 Vue Router 的 /login、/tasks、/tasks/:id 和 /health，refresh 通过单个在途 Promise 合并。access/CSRF 保持内存；刷新页面重新登录后回到任务地址。注销使页面会话版本递增，迟到响应不能恢复会话。列表/详情请求使用 AbortController 与查询版本，页面离开停止轮询。创建/重试不确定结果保留原幂等键；输入变化才建立新操作。按钮显式禁用并展示原因，数据库状态为唯一事实来源。

CI 仅触发 wang push、目标 wang 的 PR 及手动运行，使用新随机配置的专用 Compose 环境。保留 JUnit、摘要、OpenAPI 和安全截图；认证 trace/HAR/storageState 禁用。采集入口容量检查由 add-data-collection 自己接入；框架归档范围包含现有 pull/build 和验收入口，不能为尚未实现采集制造空入口。

PLATFORM-ENV、SEC-AUTH、SEC-RBAC、INT-MIGRATION、INT-OUTBOX、REL-LEASE、REL-CANCEL、E2E-BOOT、STORAGE-GUARD 与 tasks 一一对应。验收必须实际连接 MySQL/Redis/Celery，不能以 eager mode 替代 Worker 故障验证。前端覆盖刷新、加载、空态、认证失效、任务失败和重复点击。
