## ADDED Requirements

### Requirement: 可重复启动的应用基础
系统 SHALL 使用 Python 3.11、锁定依赖和版本化迁移，提供 Web、API、MySQL、Redis 与真实 Celery Worker 的运行入口。

#### Scenario: 新环境启动
- WHEN 按启动文档配置独立开发环境并执行迁移
- THEN 页面可访问 API，组件健康状态可检查
- AND 未配置数据库或未执行正确迁移时返回可行动错误

#### Scenario: 配置缺失
- WHEN 必要密码、签名密钥或依赖配置缺失
- THEN 系统拒绝不安全启动
- AND 不自动补充可用默认密码或把秘密写入日志

### Requirement: 项目存储和磁盘余量
系统 MUST 将可控缓存、临时文件和运行数据配置到项目所在盘，并在可能增加显著占用的命令或任务前检查容量。

#### Scenario: 本机低存储开发
- WHEN 本机项目位于 D 盘且剩余容量满足保留 6GiB 和显式估计增量
- THEN 仅在 D 盘指定目录进行缓存和临时写入
- AND 使用小样本与基础依赖，不自动安装完整深度学习栈

#### Scenario: 磁盘不足
- WHEN 目标盘余量低于配置保留量加预计增量
- THEN 下载、构建或采集在新增工作开始前拒绝执行并给出容量原因
- AND 不转移到 C 盘、不自动删除用户文件或其他项目数据

### Requirement: 登录会话和基础角色
系统 SHALL 提供预置账号登录、refresh 轮转、退出、当前用户及角色校验，使用 `docs/04` 第 10 节的首批契约。

#### Scenario: 正常登录与刷新
- WHEN 活跃用户提供正确凭据并在会话有效期内刷新
- THEN 返回 access token 并轮转 HttpOnly refresh cookie
- AND 旧 refresh 值不可重复成功使用

#### Scenario: 错误凭据和越权
- WHEN 凭据错误、用户禁用、会话撤销或角色不足
- THEN 返回稳定的 401 或 403 错误
- AND 登录错误不区分未知用户与错误密码，日志不含敏感令牌

#### Scenario: 退出与跨站请求
- WHEN 用户退出或 cookie 端点缺少正确 CSRF 校验
- THEN 退出使会话失效，失效会话的 access token 被拒绝
- AND 无效 CSRF/Origin 的刷新或退出请求返回 403

### Requirement: 幂等创建受限诊断任务
系统 SHALL 在 development/test 提供 Member 可访问的诊断任务入口，以真实 Worker 验证共享基础设施。

#### Scenario: 同请求重复提交
- WHEN 相同操作者、作用域和 Idempotency-Key 提交相同有效输入
- THEN 返回同一 task_id，数据库仅有一个任务和一个初始 outbox 事件
- AND 前端重复点击不会创建第二个执行

#### Scenario: 冲突与输入边界
- WHEN 相同 key 对应不同输入，或时长超出 0～30 秒，或请求任意路径/命令字段
- THEN 分别返回幂等冲突 409 或输入错误 422
- AND production 配置不暴露诊断路由

### Requirement: 数据库事务 outbox
系统 MUST 在同一事务保存任务与 outbox，采用至少一次投递和执行幂等，避免入队失败使任务永久悬挂。

#### Scenario: Redis 暂时不可用
- WHEN 任务事务提交后 Redis 不可用
- THEN 创建响应仍指向已持久化 queued 任务，投递器按有限退避重试
- AND 超限或等待超时形成明确失败而不是无限 queued

#### Scenario: 投递写回中断
- WHEN 消息已发送但 outbox 已发送标记写回失败，随后重复发送
- THEN Worker 的原子领取确保至多一个有效执行
- AND 后到的重复消息不覆盖任务进度或终态

### Requirement: 租约、恢复与 successor
系统 MUST 用心跳、执行令牌和数据库条件更新管理任务，保留原终态并使重试可追溯。

#### Scenario: Worker 中断与过期写回
- WHEN Worker 停止心跳且租约失效
- THEN 协调器将原执行标记 failed，并按上限创建 retry_of 关联的 successor
- AND 旧 Worker 恢复后无法凭过期令牌写回结果

#### Scenario: 重试并发与上限
- WHEN 多个请求重试同一终态任务或自动恢复已达到上限
- THEN 原任务最多有一个直接 successor
- AND 自动恢复超限留为明确失败，手动请求使用稳定错误说明当前节点状态

### Requirement: 取消与完成的一致终态
系统 SHALL 在队列和执行检查点接受协作取消，处理与成功写回的竞态。

#### Scenario: 队列取消
- WHEN queued 任务收到有权用户的取消请求
- THEN 任务进入 cancelled，后到消息不能领取

#### Scenario: 执行取消与完成竞态
- WHEN running 任务同时收到取消请求和完成写回
- THEN 条件更新确定唯一结果；cancel_requested 先提交时不能再写 succeeded
- AND 接受取消请求返回 202，终态冲突返回 409，实际取消后状态为 cancelled

### Requirement: 任务界面与验收证据
系统 SHALL 提供登录及任务创建/查询界面，展示真实数据库状态和可行动错误，并保存框架 E2E 证据。

#### Scenario: 页面刷新和任务失败
- WHEN 页面刷新、认证失效或后台任务失败
- THEN 可重新读取持久任务，认证失效按统一流程处理
- AND 显示阶段、心跳、进度、安全化错误以及 request/task ID

#### Scenario: 框架验收
- WHEN 正常与故障场景完成实际运行
- THEN E2E-BOOT 证明页面到真实 Worker 的完整链路
- AND 只有实现及验收证据齐备才勾选实施任务和归档 change
