# DefectGuard 即时软件缺陷预测系统

DefectGuard 是面向代码评审场景的提交级即时软件缺陷预测系统。系统从 Git 仓库构建可追溯的缺陷标签、变更特征和模型数据集，并对历史提交及尚未合入主干的候选变更提供风险概率、解释和评审排序。

## 文档入口

项目开发的唯一文档基线位于 [`docs/`](docs/README.md)。

- `docs/` 中标记为“当前基线”的文档是需求、设计、测试和协作的唯一依据。
- `softwarePrediction/` 是外部导入的原始材料，只用于合并来源追溯，不直接指导开发。
- `openspec/` 记录按变更推进的可验收规格。OpenSpec 与 `docs/` 冲突时，应先修正规格，再进行实现。

## 当前状态

当前实现工程、认证、可靠任务、仓库安全克隆（A07～A09）、初次提交解析（A11）、增量同步（A10）及第九阶段Fix证据（A12）。Fix绑定接受快照/规则，保留Issue观测和每轮结果，支持异步恢复及可审计人工复核；普通Issue编号不自动当Fix，未发现证据不等于clean。同步强推/分支变化仍暂停待复核。SZZ、模型、分叉复核恢复及受控归档待后续；框架 [当前规格](openspec/specs/application-foundation/spec.md) 已归档，采集change保持活动。见 [Fix证据与验收](docs/development/Fix证据与验收.md)、[整体规划](docs/development/后续整体规划与空间预算.md) 和 [文档更新记录](docs/development/文档更新记录.md)。

## 当前分支范围

`wang` 是当前个人独立开发分支。本分支的文档和 OpenSpec 修改尚未与团队其他成员同步，仅作为本分支开发依据，不代表团队已批准或其他分支已采用。开发提交仅推送到 `origin/wang`；不修改或同步覆盖 `main` 及其他分支。合入 `main` 由项目维护者另行决定，不属于当前个人开发任务。

开发准备评审和实施顺序见 [开发准备评审与启动建议](docs/reviews/2026-09-26-开发准备评审与启动建议.md)。

## Python 开发环境

项目使用 Python 3.11 和根目录 `.venv`，版本要求记录在 `.python-version`。虚拟环境不进入 Git；系统默认 Python 可以继续使用其他版本。激活、退出及编辑器解释器选择见 [Python 环境使用](docs/development/Python环境使用.md)。

按 [存储安排与命令使用](docs/development/存储预算与命令使用.md) 执行：D 盘扩容后取消旧严格预算，常规容量检查改为提示，保留磁盘极低余量时的写入保护；缓存与临时文件继续位于 D 盘 runtime。
