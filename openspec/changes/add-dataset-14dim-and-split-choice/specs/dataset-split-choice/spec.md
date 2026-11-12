# Capability: dataset-split-choice

## ADDED Requirements

### Requirement: 用户自选训练/验证/测试比例

创建研究数据集 SHALL 由用户给出训练/验证/测试三个比例，MUST 满足：三者总和为 1、训练与测试比例大于 0、验证比例大于等于 0。接口层 SHALL 校验并拒绝不合法的组合；前端 SHALL 提供三个数字输入与总和校验，不合法时禁用提交并提示。

#### Scenario: 合法比例创建数据集

- **WHEN** 用户提交总和为 1、训练与测试大于 0 的比例
- **THEN** 按给定比例切分数据集，成员分区归属与比例一致

#### Scenario: 不合法比例被拒绝

- **WHEN** 比例总和不为 1、训练或测试为 0、或出现负值
- **THEN** 前端禁用提交并提示；接口返回参数无效错误，不创建任务

### Requirement: 验证集可选

`validation_ratio` 允许为 0，表示不切验证分区，数据集只分训练与测试两份。MUST 满足：成员不含 validation 分区；摘要分区只有 train/test；CSV 工件只落这两份；请求不存在的分区（如 validation）SHALL 返回 `DATA_PARTITION_ABSENT` 错误而不是空文件。

#### Scenario: 两分区数据集构建

- **WHEN** 以验证比例 0 创建数据集
- **THEN** 全部成员分属 train 或 test，摘要与工件都不出现 validation

#### Scenario: 下载不存在的分区报错

- **WHEN** 对两分区数据集请求下载 validation 分区
- **THEN** 返回 404 DATA_PARTITION_ABSENT，不生成文件

### Requirement: 无验证分区时训练在训练区内校准定门槛

对两分区数据集训练时，训练链路 SHALL 在训练分区内按事件时间切前后两段：前段拟合校准器、后段定门槛，流程与有验证分区时同构。报告 MUST 标注 `calibration_partition="train"`、门槛指标键为 `train_threshold`，并在 limitations 中说明「训练区内校准、属样本内」，MUST NOT 以验证集名义呈现。此类模型的风险列表默认分区 SHALL 为测试分区。

#### Scenario: 无验证分区训练成功且如实标注

- **WHEN** 对只有 train/test 的数据集发起训练
- **THEN** 训练成功，报告 calibration_partition 为 train，指标含 train_threshold 与 test，limitations 说明样本内校准；风险列表默认只查测试分区

#### Scenario: 有验证分区时行为不变

- **WHEN** 数据集含验证分区
- **THEN** 校准仍用验证分区，报告键仍为 validation_threshold，不出现训练区内回退的 limitation
