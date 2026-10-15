## ADDED Requirements
### Requirement: 可追溯变更上下文
系统 SHALL 在新版本中用真实old/new blob计算hunk与行范围，在固定整行token预算内选择角色文本并报告所有遗漏，保持原分区和旧工件。
#### Scenario: 正常有界上下文
- **WHEN** train/validation成员和原生对象可验证
- **THEN** 输出源行索引、hunk代表率、全文及选中token、策略/源码摘要，不把选定片段称为完整提交
#### Scenario: 漂移或不适用分区
- **WHEN** blob/hash/成员/tokenizer漂移或调用要求消费旧test
- **THEN** 明确拒绝而不覆盖旧版本或自动换人口
### Requirement: 未暴露人口封存
系统 SHALL 以固定时间窗口和label-blind SHA规则封存旧数据集之外的未来候选，不生成或消费其标签/预测。
#### Scenario: 未来候选
- **WHEN** 单父原始commit、双时钟、原成员排除和数量上限可验证
- **THEN** 保存不可变身份/时钟及未认证条件，不据已有原生Git对象自动提升holdout资格
#### Scenario: 无合格候选
- **WHEN** 窗口无成员或来源/时间不符
- **THEN** 保存失败并保持准入关闭，不回用已暴露test
### Requirement: 可证伪的准入与复核
系统 SHALL 区分工程完成、数据/许可/人工证据和模型效用，提供来源复核材料而不伪造独立审阅。
#### Scenario: 证据不足
- **WHEN** 校准支持、许可、污染、人工审阅或产品链路条件尚未完成
- **THEN** 报告具体缺口/工作量/完成条件，保持candidate_release和完整产品P1为false
#### Scenario: 双人材料交付
- **WHEN** 导出旧unresolved的假设与原生证据
- **THEN** 保留修订摘要、两份空白独立表和仲裁规则，不修改旧review状态、标签或历史取得时间
