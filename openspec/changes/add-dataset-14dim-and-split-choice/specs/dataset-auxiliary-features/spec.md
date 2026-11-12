# Capability: dataset-auxiliary-features

## ADDED Requirements

### Requirement: 成员行携带十四维辅助特征

研究数据集的每个成员行 SHALL 在 12 个数值特征之外携带两个辅助特征：`fix`（布尔，是否为修复提交）与 `lt`（改动前文件总行数）。`fix` MUST 来自上游源已发布的修复元数据，不得自行推断；存储的上游源没有 lt 时，`lt` MUST 为 null 且 MUST 带 `lt_missing_reason` 说明缺失原因，不得用 0 或估算值填充。

成员一致性校验（重试续跑时）MUST 把 fix/lt 纳入比较；数据集摘要 SHALL 记录 `auxiliary_features` 与 `lt_source`，lt 缺失时 MUST 在摘要 limitations 中说明。

#### Scenario: 修复标志与缺失的 lt

- **WHEN** 构建一个研究数据集，上游源带修复元数据但不含 lt 列
- **THEN** 每个成员行的 fix 为布尔值，lt 为 null 且 lt_missing_reason 说明上游未提供；摘要记录 auxiliary_features=["fix","lt"] 与相应 limitation

#### Scenario: 重试续跑时辅助特征参与一致性校验

- **WHEN** 构建中断后续跑，已入库成员的 fix/lt 与重算结果不一致
- **THEN** 任务以数据漂移错误失败，不得发布不一致的数据集

### Requirement: 辅助特征随工件与接口暴露

每个分区的 CSV 工件 SHALL 在 12 个数值特征列之后追加 `fix,lt` 两列；fix MUST 写为布尔字面量，lt 缺失时该格留空。数据集详情接口的成员行 SHALL 返回 `fix`、`lt`、`lt_missing_reason` 三个字段。

#### Scenario: CSV 工件带辅助列

- **WHEN** 下载任一已构建分区的 CSV
- **THEN** 表头以 `...,fix,lt` 结尾，每行的 fix 为 True/False，lt 缺失的行该格为空

#### Scenario: 详情接口返回辅助字段

- **WHEN** 查询数据集成员
- **THEN** 每个成员行带 fix、lt 与 lt_missing_reason 字段，旧数据集缺字段时按缺失处理不报错

### Requirement: 辅助特征只展示不参与模型计算

fix/lt SHALL 只出现在展示与下载链路，MUST NOT 进入任何训练特征列表、特征合同或模型请求。数据集详情页 SHALL 展示这两列（fix 显示「是修复/非修复」、lt 缺失显示「未提供」并悬停可见原因），并 MUST 用文案说明「修复标志只展示、不参与模型计算」。

#### Scenario: 训练特征不被辅助特征污染

- **WHEN** 打开模型训练页或发起训练
- **THEN** 可选特征仍是 12 个数值指标，fix/lt 不出现；训练报告的特征合同不含 fix/lt

#### Scenario: 详情页人话展示与缺失兜底

- **WHEN** 查看数据集详情表的 fix/lt 列
- **THEN** fix 显示「是修复/非修复」，lt 缺失显示「未提供」且悬停可见缺失原因；页面有「修复标志只展示、不参与模型计算」的提示
