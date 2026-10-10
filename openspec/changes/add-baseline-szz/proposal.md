# Change: add-baseline-szz

## Why
Fix 候选与人工复核已完成，但缺少可追溯的缺陷引入行证据。独立 SZZ change 防止将采集任务、未接受同步数据和未来证据混为训练标签。

## What Changes
- A13 异步 SZZ 与 A14 分页证据，冻结已完成 Fix 轮次、接受 HEAD/私有快照、规则/解析/过滤版本、复核 revision 与观察截止时间。
- 唯一父提交的删除/替换行进行受监督 Git blame；直接父提交合法，根/merge/略过内容明确 Unknown。
- 固定输入、每10项原子证据/进度、有效租约恢复、取消/重试、容量保护、页面与验收。
- 输出带可用时间的候选 buggy 证据；无证据均为 Unknown。clean 成熟度/完整历史审计随不可变数据集阶段实施，不在本批推断负标签。

## Impact
0007_szz 增加三张表及仓库 SZZ 状态；复用认证、任务/outbox、原生 Git 监督和既有锁定依赖。仅 wang；无 main 合并、无远程 Issue 写入。工程预计0.5～3GiB在D，数据及构建峰值另计。
