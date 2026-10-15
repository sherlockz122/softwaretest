# 项目上下文

2026-10-15 第二十三阶段当前：真实Git语义输入与独立CPU冻结CodeBERT先导已实施，785条同人口12次拟合/安全回读与数值复现完成，融合未达到预声明增益门槛；测试4正例、严重base截断和权重许可元数据缺口明确保留。完整验收按[本批报告](../docs/reports/2026-10-15-第二十三阶段冻结语义先导报告.md)登记。第二十二阶段LR/RF应用已验收；本轮没有语义API/Worker产品闭环、候选发布或全P1完成。后续准入见[先导指南](../docs/development/冻结语义先导与验收.md)。

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

## 第二十一阶段当前实施边界

v1/v2及旧迁移不改，schema0010_dataset_numbers。公开CSV离线资产/review保留；应用研究dataset/任务/RBAC/A17～19/页面/无损12数值导出已实现。真实106674结构校验不等于金标/历史/候选资格，旧false和2条未决不回写。下一批LR/RF注册/任务/评估工件，再真实语义/冻结预训练融合P1与候选发布，质量线持续有限核验。仅wang，不依赖隔离材料。

## 第二十二阶段研究模型增量

0011_models、LR/RF model.train、A20/A21/A23及页面已实施，A22研究用途拒绝产品发布；双seed/复现/可靠恢复与正常备份隔离恢复、完整回归和最终SHA分别验收。下一组真实语义/冻结编码器与融合P1，旧资格不回填。当前指南见docs/development/LR与RF研究训练及验收.md。
