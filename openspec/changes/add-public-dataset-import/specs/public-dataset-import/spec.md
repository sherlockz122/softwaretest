# Capability: public-dataset-import

## ADDED Requirements

### Requirement: 导入 CSV/JSONL 缺陷数据集

系统 SHALL 接受用户提交的外部 CSV/JSONL 缺陷数据集（文件字节上传或服务器本地路径，本地路径仅管理员），走与内置资产相同的归一化、密封与审计管线。导入请求 MUST 携带显式列映射：canonical_repository、commit_id、事件时间、label 四个身份列为必填，12 个数值特征名（la、ld、nf、nd、ns、ent、ndev、age、nuc、aexp、arexp、asexp）在 v1 全部必填，fix 列可选；MUST NOT 自动推断列语义。

#### Scenario: 声明列映射导入文件

- **WHEN** 用户提交文件与合法列映射（含许可声明）
- **THEN** 系统归一化文件、生成清单与内容哈希并密封，返回资产编号与样本摘要

#### Scenario: 身份列缺失或映射非法被拒绝

- **WHEN** 列映射缺少任一身份列、映射到未知特征名或重复映射同一列
- **THEN** 返回 `DATASET_IMPORT_COLUMNS_INVALID`，不产生资产

#### Scenario: 同文件同映射幂等

- **WHEN** 以相同文件、相同列映射与相同许可声明重复提交
- **THEN** 返回同一资产编号，不重复导入；更换列映射或许可声明则视为新请求

### Requirement: 数值特征列齐全，fix 列可选

导入 MUST 要求四个身份列与全部 12 个数值特征列均有映射；缺失任一数值特征列 MUST 被拒绝并在错误信息中列出缺列清单。fix 列 SHALL 可选：未映射时成员行 fix 为「未提供」并带原因，摘要 limitations MUST 记录。

#### Scenario: 特征列不全被拒绝并列出缺口

- **WHEN** 文件的列映射缺少任一数值特征列
- **THEN** 返回 `DATASET_IMPORT_COLUMNS_INVALID`，错误信息列出缺失的特征列清单，不产生资产

#### Scenario: fix 列未映射仍可导入

- **WHEN** 文件只提供身份列与 12 个数值特征列，不映射 fix 列
- **THEN** 导入成功，成员行 fix 按「未提供」展示并带缺失原因，摘要 limitations 记录

### Requirement: 来源可追溯

数据集摘要 SHALL 记录 `source_kind`（`builtin_asset` / `user_import`）与导入来源描述（许可声明、列映射摘要、fix 映射情况）；数据集详情页 SHALL 展示来源与映射。导入文件的漂移防护 MUST 与内置资产一致：文件改动后重跑构建以漂移错误失败。

#### Scenario: 详情页展示来源

- **WHEN** 查看导入数据集的详情
- **THEN** 页面显示来源类型、许可声明与列映射摘要

#### Scenario: 导入文件被改动后拒绝

- **WHEN** 密封后的导入文件内容被修改，再以原哈希发起构建
- **THEN** 构建以源漂移错误失败，不产生不一致数据
