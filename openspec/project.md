# 项目上下文

## 项目概述

即时软件缺陷预测系统（Just-in-Time Defect Prediction, JIT）：从真实开源仓库
（ActiveMQ、Lucene 等知名 Java 项目）挖掘历史提交数据，训练机器学习模型预测
"某次提交是否会引入缺陷"，对外提供在线预测服务与可视化前端，指导代码评审员
优先审查高风险提交，实现质量保障左移。

## 目标用户

- 代码评审员：按风险排序审查提交，优先处理高风险项
- 研发团队负责人：查看缺陷引入趋势，掌握代码库质量变化
- 算法工程师：训练、对比与切换预测模型

## 技术栈

- 后端：Python 3.11 + FastAPI + SQLAlchemy
- 数据：PyDriller + 原生 Git 命令解析仓库；MySQL 8 持久化
- 任务：Celery + Redis，支持幂等、心跳、重试、取消与断点恢复
- 工件：首期使用本地持久化卷，通过存储接口预留 MinIO 迁移能力
- 模型：scikit-learn、XGBoost、LightGBM、PyTorch；解释用 SHAP
- 前端：Vue3 + ECharts + Element Plus
- 部署：Docker Compose
- 工程：GitHub Actions CI；OpenSpec 规格驱动开发；当前基线见 `docs/README.md`

## 团队约定

- 分支规范：main（稳定，由维护者管理）/ wang（个人独立开发）/ feature/<us-id>-<slug>（可选短期功能分支）
- 当前修改尚未与团队同步，仅推送 origin/wang；不修改 main 或其他分支，不主动发起合入 main 的流程。
- 提交信息：type(scope): subject，如 feat(szz): 支持行号追踪
- 合并前至少一位非作者成员在 PR 中审阅，审阅记录保留在平台
- 所有需求变更必须先创建或修改 OpenSpec change，不得只改代码不更新规格
- 看板任务必须标注 Sprint 与对应 OpenSpec change/task
- 每个核心功能须有正向验收与至少一个异常/无效输入验证
- `softwarePrediction/` 为外部导入材料，不再作为开发依据；后续统一维护 `docs/`
