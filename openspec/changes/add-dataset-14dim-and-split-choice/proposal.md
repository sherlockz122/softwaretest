# Change: add-dataset-14dim-and-split-choice

## Why

研究数据集的成员行目前只带 12 个数值特征（Kamei 口径的 NS、ND、NF、Entropy、LA、LD、LT、FIX、NDEV、AGE、NUC、EXP、REXP、SEXP 十四维里，FIX 与 LT 缺失）。用户要求「每次提交要有单独的十四维特征基本数据」：每个样本行除了 12 个数值指标，还应带上修复标志（fix）与改动前文件总行数（lt）。

同时，数据集的训练/验证/测试比例固定为 70/15/15，验证集不可省略。用户要求「训练集、验证集、测试集比例可以让用户自己选择」「验证集不是必须」。因此切分比例改为创建时由用户填写，验证比例允许填 0（不切验证分区），训练链路要在没有验证分区时仍能完成校准与定门槛，并如实标注口径。

顺带把两处界面口径对齐：「代码行数」改为「样本数」；模型训练页与历史风险列表的职责边界文案明确化。

## What Changes

- 每个数据集成员行携带完整十四维特征：12 项数值指标 + 修复标志 `fix`（布尔，来自上游已发布的修复元数据）+ 改动前文件总行数 `lt`。存储的上游源里没有 lt，按「未提供」处理并带缺失原因 `upstream_source_omits_lt`，不补算。
- `fix`/`lt` 是辅助特征：随成员行展示、进 CSV 下载、随详情接口返回，但**不进训练特征**（防标签泄漏）。
- 创建数据集时训练/验证/测试比例由用户填写（前端三个数字输入，总和必须为 100）；`validation_ratio` 允许为 0，表示只切训练/测试两个分区。
- 无验证分区时：CSV 工件只落训练/测试两份，下载不存在的分区返回 `DATA_PARTITION_ABSENT`；训练在训练区内按时间切前后两段做校准与定门槛，报告标注 `calibration_partition="train"` 与 limitation，不冒充验证集；风险列表默认只展示测试分区。
- 前端口径：所有「代码行数」改为「样本数」；数据集详情、模型训练页补充分区样本数与「验证集不是必须」的说明；训练与历史风险列表的职责边界文案明确化。

## Capabilities

### New Capabilities

- `dataset-auxiliary-features`：数据集成员的每个样本携带十四维特征（12 数值 + fix + lt），随二维表展示与 CSV 下载，只展示不参与模型计算。
- `dataset-split-choice`：用户自选训练/验证/测试比例，验证集可选；无验证分区时训练链路在训练区内完成校准与定门槛并如实标注。

### Modified Capabilities

- `dataset-2d-table`（成员表在 12 个数值特征之外新增 fix/lt 两列，缺省展示）。
- `dataset-detail`（成员行与 CSV 工件新增 fix/lt 字段；下载不存在的分区返回 `DATA_PARTITION_ABSENT`）。
- `model-training`（训练请求 `validation_ratio` 下限由「大于 0」放宽为「大于等于 0」；训练支持无验证分区的校准回退）。

## Impact

**数据库**

无迁移。`fix`/`lt` 存在数据集成员的 `source_json` 里（既有 JSON 列，随行返回），不新增列。已冻结的旧数据集不受影响，行内没有 fix/lt 的展示为「未提供」。

**后端**

- `packages/mining/research_dataset.py`：VERSION 升为 `published-binary-12-v2`；成员行带 `fix`（布尔）与 `lt`（None + 缺失原因）；切分逻辑支持两分区；元数据带 `auxiliary_features`/`lt_source`/limitations。
- `packages/mining/dataset_service.py`：批次、详情、CSV 工件、成员一致性校验全部带上 fix/lt；工件跳过空分区；下载缺分区 404。
- `apps/api/datasets.py`：`validation_ratio` 下限放宽为 0。
- `packages/modeling/train.py` 与 `packages/modeling/service.py`、`packages/modeling/data.py`：无验证分区时的校准回退、风险列表默认分区、审计按实际分区过滤。
- `packages/semantic/study.py`：适配训练函数新返回结构。

**前端**

- 数据集列表：三个比例输入（验证比例可为 0），样本数口径。
- 数据集详情：十四维特征表（fix/lt 两列人话展示），提示「修复标志只展示、不参与模型计算」。
- 模型训练页：分区样本数与「验证区不是必须的」说明，训练与历史风险列表的职责边界文案。
- 模型详情/风险面板：无验证区时「训练区后段定门槛（样本内）」的展示与提示。

**不在本次范围内**

不补算 lt（上游源未存，补算需联网克隆仓库，成本与不确定性均不可控）；fix 不进任何训练特征；不改候选预测链路的十四维口径；不新增数据库迁移。

**关联追踪**

受影响文档：`docs/04-API与数据库规格.md`（数据集构建请求与详情、CSV 工件、训练报告键）、`docs/08-界面与交互规格.md`（数据集列表/详情、模型训练页）、`docs/07-需求设计测试追踪矩阵.md` 新增 US-29（十四维行级数据）与 US-30（比例自选与验证集可选）、PRD「数据集」需求域与 MVP 定义。
