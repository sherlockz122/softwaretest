# DefectGuard 即时软件缺陷预测系统

DefectGuard 是面向代码评审场景的提交级即时软件缺陷预测系统。系统从 Git 仓库构建可追溯的缺陷标签、变更特征和模型数据集，并对历史提交及尚未合入主干的候选变更提供风险概率、解释和评审排序。

## 文档入口

项目开发的唯一文档基线位于 [`docs/`](docs/README.md)。

- `docs/` 中标记为“当前基线”的文档是需求、设计、测试和协作的唯一依据。
- `softwarePrediction/` 是外部导入的原始材料，只用于合并来源追溯，不直接指导开发。
- `openspec/` 记录按变更推进的可验收规格。OpenSpec 与 `docs/` 冲突时，应先修正规格，再进行实现。

## 当前状态

当前实现工程、认证、可靠任务、登录/任务页面、安全克隆（A07～A09）、初次提交解析/checkpoint（A11与文件列表）及第八阶段增量同步（A10）。同步采用独立不可变快照，取消/故障恢复沿用同一窗口，最终批次才推进HEAD；强推或默认分支变化进入requires_review，保留旧数据。Fix、SZZ、模型和分叉复核恢复流程待后续。框架 [当前规格](openspec/specs/application-foundation/spec.md) 已归档，采集change保持活动。见 [增量同步与验收](docs/development/增量同步与验收.md)、[提交解析与验收](docs/development/提交解析与验收.md)、[整体规划](docs/development/后续整体规划与空间预算.md) 和 [文档更新记录](docs/development/文档更新记录.md)。

## 当前分支范围

`wang` 是当前个人独立开发分支。本分支的文档和 OpenSpec 修改尚未与团队其他成员同步，仅作为本分支开发依据，不代表团队已批准或其他分支已采用。开发提交仅推送到 `origin/wang`；不修改或同步覆盖 `main` 及其他分支。合入 `main` 由项目维护者另行决定，不属于当前个人开发任务。

开发准备评审和实施顺序见 [开发准备评审与启动建议](docs/reviews/2026-09-26-开发准备评审与启动建议.md)。

## Python 开发环境

项目使用 Python 3.11 和根目录 `.venv`，版本要求记录在 `.python-version`。虚拟环境不进入 Git；系统默认 Python 可以继续使用其他版本。激活、退出及编辑器解释器选择见 [Python 环境使用](docs/development/Python环境使用.md)。

按 [存储安排与命令使用](docs/development/存储预算与命令使用.md) 执行：D 盘扩容后取消旧严格预算，常规容量检查改为提示，保留磁盘极低余量时的写入保护；缓存与临时文件继续位于 D 盘 runtime。
