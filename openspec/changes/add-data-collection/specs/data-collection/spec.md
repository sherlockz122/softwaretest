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

#### Scenario: 独立快照增量同步
- WHEN 初次解析完成后 Member 请求同步默认分支
- THEN 系统创建任务、outbox、审计和独立同步窗口，固定基线并安全克隆到新目录
- AND 新 HEAD 的增量按新可达集合排除旧 HEAD 可达集合，不以时间戳过滤
- AND 最终批次、checkpoint、正式 HEAD 与成功终态在有效租约检查下同事务提交

#### Scenario: 同步中断和回执丢失
- WHEN 同步取消、资源不足、Worker 中断或快照发布回执丢失
- THEN 已提交批次、旧基线和已发布候选快照保留，successor 继续相同窗口
- AND 旧执行令牌不得推进状态，同键请求重放原任务，不重新选择远程 HEAD

#### Scenario: 同步无新提交和历史覆盖度
- WHEN 新旧 HEAD 相同且默认分支未改变
- THEN 同步窗口以零个新提交完成，不重复入库
- AND 初次最近 N 窗口仍标记为有限历史，不因同步变为完整历史

#### Scenario: 默认分支变化或历史消失
- WHEN 已有默认分支改变、旧 HEAD 对象不在新快照或非空历史变空
- THEN 同步进入 requires_review 并保留候选用于复核
- AND 不导入分叉提交、不推进正式 HEAD、不自动解除复核或删除旧快照

#### Scenario: 固定初次解析窗口
- WHEN Member 启动已克隆仓库的初次解析并选择全部历史或最近 N 个提交
- THEN 系统固定 HEAD、窗口、规则版本和执行计划摘要
- AND 按 committer time、SHA 稳定排序，记录作者身份哈希及原始时间偏移
- AND 同键重放同一任务，另一窗口请求不改写已建立窗口

#### Scenario: 取消或资源不足后继续
- WHEN 解析取消、超时、输出超限或磁盘无法容纳预计增长与余量
- THEN 系统保留此前提交批次和已发布 Git 数据，记录稳定错误或取消状态
- AND successor 从同一 checkpoint 继续，过期 execution_token 不得写入
- AND 最终批次、checkpoint、进度与成功终态在同一事务提交

#### Scenario: 查询已入库数据
- WHEN 已认证用户查询提交或文件列表
- THEN 系统返回有界稳定分页的已提交记录及略过内容状态
- AND 不返回原始作者邮箱、存储路径、执行令牌或原始命令输出

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

#### Scenario: 固定接受快照与规则重算
- WHEN Member运行Fix识别或改变版本化政策重算
- THEN 系统固定已接受HEAD、解析及规则版本/摘要、覆盖度和政策，按可达已解析集合生成独立轮次
- AND 不纳入未接受同步批次、不覆盖旧证据和复核，不将有限覆盖或无证据认定clean

#### Scenario: Issue观测不确定与资源边界
- WHEN Issue确认失败、限流、返回PR、不安全连接、重定向或超出查询/响应预算
- THEN 系统保存受限的low线索且不伪造high证据
- AND 公开HTTPS接口验证全部地址及证书、固定peer、不发送凭据或执行远程写入

#### Scenario: Fix批次与外部观测恢复
- WHEN 已提交批次后取消/空间不足/Worker强杀或Issue观测/最终事务回执丢失
- THEN 已提交判定/复核及首次冻结观测保留，successor使用同根任务和计划继续
- AND 有效token/status/lease/latest/root下批次与checkpoint同事务，旧执行不能写入

#### Scenario: 人工复核并发与审计
- WHEN Member提交确认、拒绝或恢复待复核及理由
- THEN 系统检查expected_revision并同事务保存状态/revision/actor/理由/审计，保留原规则证据
- AND 相同已接受决策可安全重放，其它冲突409，Viewer拒绝写入，不自动生成SZZ标签

#### Scenario: Fix查询与页面恢复
- WHEN 已认证用户查看Fix轮次和证据或遇到查询/写回执错误
- THEN 系统提供有界稳定分页、覆盖度和复核历史，页面刷新关联查询、同键跟踪实际任务并隔离迟到响应
- AND 公开响应不含私有路径、token、作者邮箱或Issue原始载荷
