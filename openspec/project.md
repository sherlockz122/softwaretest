# 项目上下文

## 项目概述

即时软件缺陷预测系统（Just-in-Time Defect Prediction, JIT）：从真实开源仓库
及审计过的公开数据构建用途分离的训练集，训练机器学习模型预测
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
- 用户指定本项目当前有效规格、最新规划与已验收wang实现为后续开发/团队同步标准，不迁就外来旧材料；仅推送origin/wang，main合并维护和其它分支不是本流程职责。
- 提交信息：type(scope): subject，如 feat(szz): 支持行号追踪
- 合并前至少一位非作者成员在 PR 中审阅，审阅记录保留在平台
- 所有需求变更必须先创建或修改 OpenSpec change，不得只改代码不更新规格
- 看板任务必须标注 Sprint 与对应 OpenSpec change/task
- 每个核心功能须有正向验收与至少一个异常/无效输入验证
- 原始设计/课程、外来原型和旧工具已隔离到archive/external，退出默认检索/构建；开发只依赖docs当前有效规格、决策/指南和一致的OpenSpec，有用结论见自己的吸收清单。

## 第二十阶段当前实施边界

v1/v2特征及schema0008_features保留；首条公开CSV离线资产保存/重载和85起点review链已实现，2条未决不自动晋升训练资格。第二十一阶段待用户开工：用途分离dataset迁移/任务/RBAC/A17～19/页面及研究12数值导出；LR/RF工程随后，再真实语义/冻结预训练融合P1与候选发布。质量认证持续有限核验，旧false不回写。当前开发与验收不依赖隔离来源。
