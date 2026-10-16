## ADDED Requirements

### Requirement: 冻结的 SZZ 输入
系统 SHALL 把每一次 SZZ 运行绑定到已完成的 Fix 轮次、已接受的快照、版本化政策、
复核版本号和观测截止时间上。

#### Scenario: 后续复核发生变化
- **WHEN** Fix 判定在 SZZ 创建之后发生变化
- **THEN** 既有的冻结输入保持不变，需要另建一轮新的 SZZ

#### Scenario: 历史截止时间
- **WHEN** 证据在请求的截止时间之后才可用
- **THEN** 结果是 Unknown，而不是一个历史 buggy 标签

### Requirement: 可审计的基线追溯
系统 SHALL 在唯一父提交中用有界的方式逐行追溯（Git blame）被删除或被替换的代码行，并保留来源证据与合格性。

#### Scenario: 直接父提交与重命名
- **WHEN** 逐行追溯指向直接父提交，或者沿着整文件重命名继续追溯
- **THEN** 该来源被保留下来，并带上原始行号和路径证据

#### Scenario: 内容或历史不可用
- **WHEN** 该修复是 merge 提交或根提交，或者内容与历史不可用、不完整
- **THEN** 系统记录显式的 Unknown，绝不推断成 clean

### Requirement: 可恢复的原子执行
系统 SHALL 以租约栅栏和进度原子发布有界的结果批次，并在恢复过程中保留已完成的批次。

#### Scenario: Worker 被终止
- **WHEN** Worker 在一个已经提交的部分批次之后被终止
- **THEN** 后继任务按冻结输入继续，使用唯一的关联，并拒绝旧令牌

### Requirement: 带授权的 SZZ 接口
系统 SHALL 为 Member 和 Admin 提供启动入口与已认证的分页查询，并保证幂等与安全的返回内容。

#### Scenario: Viewer 尝试启动
- **WHEN** Viewer 尝试启动 SZZ
- **THEN** 启动被拒绝，也不创建任何 SZZ 状态

#### Scenario: 项目盘空间不足
- **WHEN** D 盘无法支撑预计增长和紧急余量
- **THEN** 工作停止并保留进度，不转移到 C 盘
