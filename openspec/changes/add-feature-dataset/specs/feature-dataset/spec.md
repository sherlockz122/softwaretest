## ADDED Requirements

### Requirement: Mature observations and immutable time dataset kernel
系统 SHALL 按预声明观察窗口及已发布的完整观察轮次审核标签，保留十四维原始值与缺失；SHALL 不把今天获得的证据回填到历史截止，也不将候选或有限覆盖转为clean。

#### Scenario: Confirmed positive and complete negative
- **WHEN** 同一接受DAG的完整轮次包含已人工确认、完整追溯的合法祖先证据，或完整观察覆盖提交年龄W且无未决证据
- **THEN** 产生版本化buggy/clean与实际label_available_at；未复核候选、冲突、解析略过、覆盖/年龄不足保留unknown原因

#### Scenario: Observation freshness and historical availability
- **WHEN** SZZ启动晚于实际clone/sync观察时点，或证据/轮次发布晚于历史切分截止
- **THEN** 覆盖只截至实际接受快照的观察时点，历史分区purge晚到标签；不以SZZ运行时间补足未观察的窗口

#### Scenario: Deterministic two-time partition
- **WHEN** 按事件时间构建train/validation/test
- **THEN** 同时刻提交保持同分区、SHA稳定排序；train标签必须早于validation首事件、validation标签早于test首事件，test标签不晚于快照；内容及政策/源摘要形成稳定哈希

#### Scenario: Insufficient data and train-only contract
- **WHEN** purge后任一分区缺少两类、train某维全缺失，或时间组不足
- **THEN** 审计列明数量/原因并拒绝构建训练就绪数据集；不移边界寻找好成绩、不伪造样本、不拟合全体数据，类权重只用train

#### Scenario: Read-only actual feasibility
- **WHEN** 本机审计已有特征/SZZ/Fix轮次
- **THEN** 绑定接受HEAD、完整历史、版本/摘要及当前复核，读取冻结输入而非A11并集，不修改源数据、不补写旧观测；尚不发布A17～19或模型

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

### Requirement: Frozen accepted feature run
系统 SHALL 复用可靠任务、租约和 execution_token，从固定私有接受 HEAD 筛选解析提交，冻结 Fix 复核与输入摘要，按原子批次发布向量并保留恢复点。

#### Scenario: Unaccepted database union
- **WHEN** A11 并集含未接受同步记录或 full 历史有解析缺口
- **THEN** 未接受提交排除；完整历史缺口拒绝，有限窗口保持明确缺失

#### Scenario: Concurrent creation and later review
- **WHEN** 相同幂等请求并发到达，或接受后 Fix 复核发生变化
- **THEN** 同政策只创建一轮，不同政策冲突；旧轮次继续使用冻结复核和证据时间

#### Scenario: Interrupted worker and stale token
- **WHEN** Worker 在已提交批次后中断，或计算中取消或空间不足
- **THEN** 未提交批次回滚，旧令牌禁止写入，重试沿用原轮次和冻结输入并且不重复结果

### Requirement: Authenticated quality workflow
系统 SHALL 提供 A15/A16 与分页轮次历史，Member/Admin 可发起，Viewer 只读，并允许按特征缺失或有效值降序下钻。

#### Scenario: Partial population
- **WHEN** 轮次尚未完成
- **THEN** 页面显示待处理及已提交批次，总体质量报告保持未发布

#### Scenario: Uncertain acknowledgement and changed repository
- **WHEN** 发起响应丢失，或旧仓库查询晚到
- **THEN** 重放相同幂等键并跟踪已接受任务，旧数据不覆盖新仓库界面

#### Scenario: Safe migration
- **WHEN** 从旧 SZZ schema 升级或对有数据的迁移路径降级
- **THEN** 升级保留旧业务数据，任何受影响数据在第一次 DDL 前阻止降级
