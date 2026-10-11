## ADDED Requirements

### Requirement: Pinned attributed public inputs
系统 SHALL 仅获取预声明公开来源，记录实际下载时间、固定版本、大小及发布校验和与SHA256，保留许可归属，不执行作者复现代码。

#### Scenario: Corrupted source
- **WHEN** 输入字节与固定发布大小或校验和不符
- **THEN** 拒绝使用，不发布成功清单或自动覆盖已有输入

### Requirement: Separate research data contract
系统 SHALL 保留公开数据原始特征与标签口径，将研究发布标签与生产观测合同分离。

#### Scenario: Incompatible canonical features
- **WHEN** 数据缺LT、使用author_date或FIX/公式语义未核验
- **THEN** 标记生产合同不兼容，不填0、不改生产版本、不宣称合格产品训练输入

#### Scenario: Missing retrospective evidence
- **WHEN** 修复日期缺失、非法或早于引入事件，或缺负标签观察覆盖
- **THEN** 输出来源质量阻碍，不编造历史label_available_at，不把外部clean转成生产确认标签

### Requirement: Read-only deterministic feasibility audit
系统 SHALL 验证原始CSV、按项目与事件组稳定切分，保留异常计数和各分区两类分布，只输出离线可行性汇总。

#### Scenario: Same-time population and class deficit
- **WHEN** 同时刻有多个提交或某分区只有一类
- **THEN** 同时刻不跨集合，缺类明确报告，不根据purge或成绩重选边界

#### Scenario: Invalid or excessive input
- **WHEN** 重复键、异源字段冲突、非有限数值或资源限制出现
- **THEN** 明确拒绝或标记不合格，不修改输入，不留下声称可训练的半成品
