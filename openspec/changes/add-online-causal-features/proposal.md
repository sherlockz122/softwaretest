# Change: add-online-causal-features

## Why

独立的 v2 内核已经完成，但它还得接进在线链路才能真正用起来。
与此同时，完整的负例来源认证仍未完成。

如果把「接入在线」和「数据集就绪」打包进同一个 change，就会夸大现有证据——
让一个来源还没验证过的标签体系，看起来像是已经可以训练了。

所以这次只做接入，不宣称数据可用。

## What Changes

- 在现有的特征接口、持久化任务和质量页上增加显式的 v2 选择，
  同时保留 v1 作为接口默认值和既有轮次。
- 用已接受的原生 Git 记录认证冻结的特征输入；出现漂移就拒绝，并保留最小化的消息哈希。
- 标签覆盖度和产品就绪状态保持为 false；数据集 A17～A19 和完整的负例来源认证推迟到后续。

## Capabilities

### New Capabilities

- `online-causal-features`：v1 与 v2 并存的在线特征版本能力，
  包含原生 Git 认证绑定、冻结输入的可恢复执行，以及特征覆盖度与标签知识的分离表达。

### Modified Capabilities

无。v1 的既有行为保持不变。

## Impact

- 涉及已有的特征服务、A15 / A16 接口与 OpenAPI、前端页面。
- 数据库表结构不变。
- 不新增依赖，不做模型训练。
- 关联追踪：User Story US-06、US-07；验收项 A15 / A16；
  测试 `test_feature_v2`、v1 与 v2 的真实 Worker 强杀用例、
  FeaturePanel 单元测试与独立新卷的端到端测试。
