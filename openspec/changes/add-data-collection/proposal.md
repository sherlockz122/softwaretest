# Change: add-data-collection

## 关联基线

- User Stories：US-01～US-04
- 基线：`DG-BL-2026-09-26`
- 受影响文档：`docs/02-系统架构与详细设计.md`、`docs/04-API与数据库规格.md`、`docs/07-需求设计测试追踪矩阵.md`

## Why

JIT 缺陷预测需要真实仓库的提交历史数据，当前系统没有任何数据入口，
模型训练无从谈起。本 change 建立数据采集基线，为后续 SZZ 标注与特征工程提供输入。

## What Changes

- 新增公开 HTTPS Git 仓库接入、URL/DNS/重定向安全校验与元数据管理
- 新增异步克隆、提交解析与持久化能力（哈希、作者、时间、消息、父提交、文件变更）
- 新增幂等任务、心跳、批次 checkpoint、失败与重试状态
- 新增可审计的缺陷修复证据识别：缺陷关键词与已确认的 bug Issue；普通 Issue ID 不自动视为 Fix
- 新增数据采集模块的单元、集成、安全与异常测试

## 影响范围

- 新增数据库表：repository、author_identity、git_commit、file_change、defect_evidence；async_task/task_outbox 复用已验收框架
- 新增后端模块：仓库接入、数据采集、Fix 证据；扩展已有任务服务的业务 payload
- 不包含 SZZ 标注、特征提取与模型训练（后续 change 实现）


## 分批实施

第六阶段先交付安全 bare 克隆、repository 与 A07～A09/仓库页面；解析与 checkpoint、增量同步、Fix 分批实施。作者表与解析一起创建。SZZ 不加入本 change。实施状态见 tasks，第六阶段完成不代表整个 change 或课程故事完成。
