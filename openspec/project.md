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
- 数据：PyDriller 解析 Git 仓库；MySQL 持久化
- 模型：scikit-learn、XGBoost、LightGBM、PyTorch；解释用 SHAP
- 前端：Vue3 + ECharts + Element Plus
- 工程：GitHub Actions CI；OpenSpec 规格驱动开发

## 团队约定

- 分支规范：main（稳定）/ develop（集成）/ feature/<change名>
- 提交信息：type(scope): subject，如 feat(szz): 支持行号追踪
- 合并前至少一位非作者成员在 PR 中审阅，审阅记录保留在平台
- 所有需求变更必须先创建或修改 OpenSpec change，不得只改代码不更新规格
- 看板任务必须标注 Sprint 与对应 OpenSpec change/task
- 每个核心功能须有正向验收与至少一个异常/无效输入验证
