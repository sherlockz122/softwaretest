# Change: add-feature-dataset

## Why

系统已经有了可审计的 SZZ 候选证据，也有了 Kamei14 的计算内核、持久化任务和 A15 / A16 页面。

但还缺两样东西：成熟度判定，以及不可变的时间数据集。

US-06～US-09 要求先固定三件事：公式、历史覆盖范围、信息的可用时间。
不先固定它们，就会出现最严重的那类错误——把「缺失的历史」和「事后才知道的标签」填成 0。

## What Changes

- 交付 US-06 的计算内核和 US-07 的质量统计前置，包含真实 Git 的逐项手算验证，
  以及时间、覆盖、重命名的边界验收。
- 纠正 NUC 的歧义：它统计的是「相关文件上次修改提交的去重数量」，不是全部历史修改次数。
- 接入固定快照、可靠任务、持久化和 A15 / A16 页面。
- 交付多轮完整观察的成熟度判定、双时间数据集内核，以及只读的真实样本审计。
- 更新 `docs/03`、`docs/05`～`docs/07`、开发指导、规划、文档记录与阶段报告。

## Capabilities

### New Capabilities

- `feature-dataset`：版本化的特征内核、覆盖审计，以及后续的时间数据集约束。

### Modified Capabilities

没有。这次改动不改变任何已有能力的行为。

## Impact

- 成熟度这一部分只用标准库和现有的冻结解析器、特征、SZZ、Fix 输入，
  不新增依赖、迁移或服务；本机只读审计不改写来源。
- `feature_version`、公式摘要和规范化输入摘要共同隔离未来的规则变更。
- 关联追踪：User Story US-06～US-09；验收项 A15、A16、A17～A19；
  测试 UNIT-FEAT-01～14、INT-FEAT、DATA-FEAT、UI-FEAT、DATA-SPLIT、DATA-LEAK、UNIT-SAMPLE。
