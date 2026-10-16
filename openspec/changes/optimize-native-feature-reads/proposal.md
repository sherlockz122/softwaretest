# Change: optimize-native-feature-reads

## Why

在扩大真实来源采集规模之前，必须先把成本测量清楚：
受监督的 Git 命令次数、解析文件的查询次数、加栅栏事务的耗时。

测不出这些数字，就没法判断当前的读取方式能不能支撑更大规模的采集。

## What Changes

- 新增尝试内的有界不可变 blob 缓存，减少对同一个不可变 blob 的重复读取。
- v2 的文件变更投影改为分组加载，同时保持原生比对和原有的按提交顺序。
- 先测量原子输入冻结的耗时，再决定有没有必要改成批量插入。
- 禁止把冻结过程拆成外部可见的部分事务，也禁止把 Git 与 CPU 回调挪到任务锁或仓库锁下面执行。
- 保持不变：v1、已接受的 HEAD、全部现有资源上限、来源哈希、取消、陈旧租约和恢复行为。
- 明确不做：不新增接口、表结构或依赖，不做来源认证、数据集与模型训练。

## Capabilities

### New Capabilities

- `native-feature-performance`：在线 v2 特征校验的有界原生读取能力，
  包含尝试内的 blob 复用、分组投影加载，以及原子冻结的栅栏语义。

### Modified Capabilities

无。v1 解析器与既有哈希保持不变。

## Impact

- 只影响在线 v2 校验路径；不新增接口、表结构或依赖。
- v1 解析器哈希和旧的 v1 哈希全部保持原样。
- 关联追踪：User Story US-06 / US-07；验收项 A15 / A16；
  测试 `test_feature_reads`、`test_feature_reads_db`。
