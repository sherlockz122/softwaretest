# Change: bootstrap-application

## Why

项目已具备文档基线与独立 Python 3.11 环境，但尚无可运行应用。仓库采集需要共享认证、数据库迁移、可靠任务和前端入口。本 change 先建立这些基础能力，避免在采集代码中重复实现或绕过安全与可靠性约束。

## What Changes

- 初始化 Python 3.11/FastAPI、Celery、Vue 3 工程与锁文件，提供 Compose 和可重复启动说明。
- 实现预置账号、登录/刷新/退出及 Viewer/Member/Admin 最小权限。
- 实现 MySQL 任务、租约、重试、审计和事务 outbox，提供受限诊断任务验证真实 Worker 链路。
- 实现登录与任务页面，并提供正常、边界、故障注入和 E2E 验收。
- 提供项目存储预算、D 盘缓存/临时目录、启动前余量检查；框架阶段不安装全量模型依赖。

## Capabilities

### New Capabilities

- `application-foundation`：可运行工程、环境与存储、最小权限和可靠任务纵向链路。

### Modified Capabilities

- 无已有归档能力修改；`add-data-collection` 使用本 change 提供的共享任务服务。

## Impact

- User Stories：US-01/02 的前置能力、US-12/21 的共享任务基础、US-22 最小认证；NFR-01/02/04/05。
- 受影响基线：`docs/02`、`docs/04`、`docs/05`、`docs/06`、`docs/07`，以及开发环境与存储说明。
- 首批迁移表：`user`、`auth_session`、`async_task`、`task_outbox`、`operation_log`。
- 范围不包含真实 Git 克隆、Fix/SZZ、特征、训练、完整用户管理或大仓库采集；这些仍在后续 change。
- 当前仅在 `wang` 个人分支推进；不发起合入 main 的流程。
