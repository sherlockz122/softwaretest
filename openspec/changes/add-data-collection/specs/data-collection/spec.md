## ADDED Requirements

### Requirement: 仓库克隆
系统 SHALL 支持从指定 URL 克隆 Git 仓库到本地存储。

#### Scenario: 克隆成功
- WHEN 用户提供有效的仓库 URL
- THEN 系统克隆仓库并记录仓库元数据

#### Scenario: 无效 URL
- WHEN 用户提供无法访问的仓库 URL
- THEN 系统返回明确错误信息且不创建仓库记录

### Requirement: 提交解析
系统 SHALL 解析仓库提交的哈希、作者、时间、消息与变更文件并持久化。

#### Scenario: 正常解析
- WHEN 系统扫描一个已克隆仓库的提交
- THEN 每个提交的元数据与文件变更被存入数据库

#### Scenario: 解析中断后恢复
- WHEN 解析过程中断后重新执行
- THEN 系统从上次进度继续，不重复导入已入库提交

### Requirement: 缺陷修复提交识别
系统 MUST 依据提交消息关键词与 Issue 关联规则识别缺陷修复提交。

#### Scenario: 关键词命中
- WHEN 提交消息包含 fix、bug、defect 等关键词
- THEN 该提交被标记为缺陷修复提交

#### Scenario: Issue 关联命中
- WHEN 提交消息包含与缺陷跟踪系统关联的 Issue ID
- THEN 该提交被标记为缺陷修复提交
