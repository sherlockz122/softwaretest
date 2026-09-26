## ADDED Requirements

### Requirement: 仓库克隆
系统 SHALL 支持从通过安全校验的公开 HTTPS URL 异步克隆 Git 仓库到隔离的本地持久化存储。

#### Scenario: 克隆成功
- WHEN 用户提供有效的仓库 URL
- THEN 系统创建唯一仓库记录和异步任务，返回 repository_id 与 task_id
- AND Worker 克隆仓库并记录规范化 URL、默认分支、head SHA 与终态

#### Scenario: 不安全 URL
- WHEN URL 使用非允许协议、内嵌凭据、异常端口，或 DNS/重定向指向 loopback、私网、链路本地或保留地址
- THEN 系统在建立不安全连接前拒绝请求
- AND 返回稳定、安全化的错误，不创建仓库与任务记录

#### Scenario: 重复创建请求
- WHEN 同一作用域使用相同 Idempotency-Key 重复提交相同仓库
- THEN 系统返回首次请求创建的资源
- AND 不重复克隆或创建重复仓库记录

#### Scenario: 克隆失败
- WHEN 合法仓库因不可达、超时、大小限制或 Git 错误而克隆失败
- THEN 任务进入 failed 并保存稳定错误码与安全化摘要
- AND 不暴露凭据、本机路径、命令行或依赖版本

### Requirement: 提交解析
系统 SHALL 以确定性顺序解析提交的哈希、作者身份、author/committer time、消息、父提交及文件变更并分批持久化。

#### Scenario: 正常解析
- WHEN 系统扫描一个已克隆仓库的提交
- THEN 每个提交的元数据与文件变更被存入数据库
- AND `(repository_id, sha)` 唯一约束防止重复提交
- AND checkpoint 只在对应批次成功提交后推进

#### Scenario: 解析中断后恢复
- WHEN 解析过程中断后重新执行
- THEN 系统从上次进度继续，不重复导入已入库提交

#### Scenario: 异常文件变更
- WHEN 提交包含空消息、重命名、删除、二进制或超大 diff
- THEN 系统按照版本化策略记录或跳过受限内容
- AND 单个异常文件不得导致已提交批次损坏

#### Scenario: 默认分支历史被改写
- WHEN 增量同步发现已记录 head 不再是新 head 的祖先
- THEN 仓库进入 requires_review
- AND 系统不得静默删除或重写已有历史

### Requirement: 缺陷修复提交识别
系统 MUST 依据可审计的证据规则识别候选缺陷修复提交，并保存证据类型、值、置信等级、规则版本和人工复核状态。

#### Scenario: 关键词命中
- WHEN 提交消息命中带单词边界的缺陷修复关键词且不属于排除语境
- THEN 系统保存 medium 等级的关键词证据
- AND 是否纳入 Fix 集合由当前规则版本决定

#### Scenario: 已确认的 Bug Issue
- WHEN 提交关联的 Issue 被确认具有 bug 类型或标签，且存在修复、关闭或等价关系
- THEN 系统保存 high 等级的 Issue 证据并将提交纳入候选 Fix 集合

#### Scenario: 仅出现普通 Issue ID
- WHEN 提交消息只包含普通 Issue ID，且无法确认该 Issue 是 bug
- THEN 系统不得自动将提交标记为 Fix
- AND 可以保存 low 等级线索供人工复核

#### Scenario: 误报语境
- WHEN 关键词出现在文档修正、拼写修正、否定语境或回滚中
- THEN 规则按照已声明排除条件处理并保留判定依据
