## ADDED Requirements

### Requirement: Versioned causal feature kernel
系统 SHALL 从版本化冻结解析记录计算十四个原始特征，绑定接受 HEAD、覆盖、身份及输入/规则摘要，不读取未来证据或将缺失填零。

#### Scenario: Hand calculated Git history
- **WHEN** 两文件多作者真实 Git 历史通过现有解析器输入
- **THEN** 十四维与手算一致，重算内容摘要一致且输入无变化

#### Scenario: Recent history or unsupported file
- **WHEN** 输入仅最近窗口，或文件为 binary/encoding/超限，或 AGE 首次/零 churn
- **THEN** 相关特征为 null 并有稳定原因，完整可用历史中的真实零保持零

#### Scenario: Invalid snapshot
- **WHEN** full 缺父、重复 SHA、非 HEAD 可达提交、循环或不支持版本进入计算
- **THEN** 拒绝输入并无任何部分写入

### Requirement: Explicit lineage and information availability
系统 SHALL 只使用严格祖先及事件/SHA 前序，沿单父重命名追溯文件，隔离删除再新增，并限制 FIX 证据可用时间。

#### Scenario: Rename and re-add
- **WHEN** 文件改名后修改，或删除后以相同路径新增
- **THEN** 改名继承历史，再新增终止旧历史，NUC 去重上次修改 SHA

#### Scenario: Equal time and late Fix
- **WHEN** 祖先时间与当前相同但 SHA 不在前序，或 FIX available_at 晚于当前 event_time
- **THEN** 历史相关特征标明时间倒置，FIX 为 null；非祖先分支不影响历史

#### Scenario: Candidate purpose
- **WHEN** 显式使用 candidate 模式
- **THEN** FIX 使用声明的零默认并保留原因，模式进入摘要

### Requirement: Missing-aware quality report
系统 SHALL 提供原始分位数、范围和缺失原因，不拟合填充、不构造监督标签。

#### Scenario: Mixed or empty vectors
- **WHEN** 全部或部分值缺失，或输入为空
- **THEN** 统计只使用有限有效值，空分位数为 null，缺失数量/原因可追溯

#### Scenario: Invalid report population
- **WHEN** 重复提交或混合输入摘要进入同一质量报告
- **THEN** 拒绝生成混合轮次报告
