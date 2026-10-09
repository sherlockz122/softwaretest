# Fix证据与验收

2026-10-10第九阶段在个人wang实现US-04/A12，其迁移为`0006_fix_evidence`；当前第十阶段head为`0007_szz`。Fix生成版本化候选证据及人工复核记录，已接入独立[SZZ追溯](SZZ追溯与验收.md)；成熟标签/不可变数据集、Kamei与训练仍待后续。未发现证据不等于clean。

## 运行与接口

先按[工程启动](工程启动与验收.md)启动MySQL/Redis、升级迁移，再启动完整服务。已解析的仓库详情提供“运行 Fix 识别”，Member/Admin可运行及复核，Viewer只读。与解析/同步/Fix/SZZ活动任务互斥；失败/取消可以在当前任务重试，新分析保留旧轮记录。历史分叉requires_review时仅分析仍被接受的旧HEAD，不会解除同步暂停。

| A12关联操作 | 契约 |
|---|---|
| POST `/repositories/{id}/fix-detection` | Idempotency-Key；body `{rule_version:"fix-evidence-v1",include_medium:true}`，均有默认值；版本固定、布尔严格、未知字段拒绝，202 task_id/status |
| GET `/repositories/{id}/fix-runs` | 所有已认证角色；created_at desc/root_task_id asc，有界分页 |
| GET `/repositories/{id}/fix-runs/{run_id}/evidence` | sha asc；提交判定、证据、规则候选/复核后候选、内容覆盖和复核状态；运行中只读已提交批次 |
| PATCH `/repositories/{id}/fix-evidence/{assessment_id}/review` | Member/Admin；`status=confirmed/rejected/unreviewed`、`expected_revision>=0`、1～300字符且非空白note；200判定，冲突409 |

分页默认20、最大100。仓库/轮次/判定ID绑定检查，不能用其它仓库ID访问判定。公开DTO不返回storage_key、token、作者邮箱、Issue原始正文或HTTP头。页面用文本转义，不渲染提交/理由HTML。

同用户/仓库/键及同政策重放原任务；同键不同include_medium返回409。丢失写响应继续查询新任务；不确定复核回执可用原revision/同用户/同状态/同理由重放已完成的下一revision。其它并发决策返回409且页面刷新，必须明确再次确认。状态、理由、revision、actor及审计同事务，最近20次审计理由可查询，完整审计留库。规则候选及原证据不被复核改写；confirmed纳入、rejected移除、unreviewed恢复规则候选。复核操作不产生SZZ标签。

## 固定快照与恢复

fix_run记录根任务、接受HEAD/私有快照引用、解析版本、history_coverage、规则版本/摘要、include_medium、plan_hash、total/processed/last_sha。计划是接受HEAD可达SHA与已解析同版本SHA的交集，不能直接用A11持久化并集；full覆盖缺失应有解析记录则拒绝，不伪造完整结果。recent_window保持有限历史，即使同步后也不提升覆盖度。

每10提交将fix_assessment、defect_evidence、checkpoint、任务进度在有效token/status/lease/latest/root检查下同事务提交；最终批次含成功/审计。既有ParseExecutor监督Git可达集合，证据读取已解析元数据/文件状态，不重复生成diff。真实Worker强杀后successor继续同一轮，同SHA唯一，不覆盖已经人工复核的批次；未知最终提交回执保留成功结果。临时目录仅本次UUID，原bare和历史轮次不删除。

外部Issue观测按(run,number)首次发布后冻结；网络在事务外，发布重新检查有效租约。即使其发布回执丢失或Issue标签后来改变，重试使用已保存观测。再次获取外部变化必须新建分析轮次，不悄悄覆盖旧证据。规则代码摘要不匹配时旧轮拒绝恢复，规则变更需注册新版本和新轮，不能复用v1名称改变已发布规则。

## v1规则与Issue适配

- 带英文单词边界、大小写不敏感：fix/fixes/fixed/fixing、bugfix、hotfix，以及resolve/correct与近邻bug/defect/crash/error组合，产生medium。prefix/suffix/fixture、普通bug提及不匹配。默认纳入medium，可在新轮关闭；不是无条件真值。
- Revert/this reverts commit、否定修复/关闭关系、typo/spelling、仅文档路径（同时检查旧/新路径）或无文件的文档语境，保留low排除依据，不自动纳入。message_encoding不可用；merge、binary、编码/大小略过通过content_complete明确保留，不能当作零统计或clean。
- 每消息最多20个不同Issue引用，去重；只适配本仓库`#n`或`owner/repo#n`，直接close/closes/closed、fix/fixes/fixed、resolve/resolves/resolved关系且观测确认为bug才产生high。普通提及、其它仓库、PR、未确认为bug、不可用观测仅low。超出范围及其它关联表达不自动推断；后续扩展需新版本。
- 生产适配器`github-public-issue-v1`仅匿名读取固定`api.github.com/repos/{owner}/{repo}/issues/{number}`，精确大小写无关bug标签确认；PR键排除。自定义bug标签/类型、私有仓库和其它托管平台适配留待明确配置/版本，不能由客户端传`confirmed_bug=true`伪造。
- 每次连接重新校验全部A/AAAA、固定公开数值IP/peer、原主机TLS；无认证/Cookie、代理环境、重定向或TLS绕过。最多1MiBJSON/2MiB隧道传输、连接3秒和读取4秒预算；每轮最多256次不同Issue查询，之后保存request_budget低线索。403/429、404、TLS/网络/格式失败保留受限观测，不伪造high。

协议依据：[GitHub Issues REST](https://docs.github.com/en/rest/issues/issues#get-an-issue)及[关闭关键词](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue)。只调用GET，不在远程仓库创建Issue或评论。

## 验收、容量及下一轮

`tests/test_fix_rules.py`参数化正反例/语境/等级/关系；`tests/test_issues.py`实际TLS API及证书/地址/重定向/限流/体积；`tests/test_fix.py`实际Git/MySQL并发、批次回滚/恢复、观测冻结、复核审计、版本化重算、接受HEAD/有限历史/空库、旧数据升迁和整路径降级；`tests/test_fix_runtime.py`实际Worker SIGKILL及已复核批次恢复。页面单元测试及独立新库/卷浏览器验证实际公开GitHub链路、同键重放、409复核冲突、新轮/旧结果、Viewer/mobile及查询恢复。

受理、运行/批次复用512MiB预计增长加2GiB紧急余量；本批工程规划0.3～1GiB，数据/镜像峰值另计。可控新增在D，容量不足提前停止并告知，不转C，不自动prune旧数据。结果在`runtime/acceptance/stage9`脱敏索引，不保留HAR/trace/storageState、不上传Actions artifact，最后核对推送确切SHA的CI。

第十阶段独立baseline SZZ已冻结Fix轮次、接受HEAD/覆盖度、复核revision和Issue observed_at，并实现父策略、内容排除、行级候选证据与Unknown，详见[SZZ指导](SZZ追溯与验收.md)。下一轮Kamei14/不可变时间数据集须补齐成熟度和观察窗口。事后Issue和人工复核不能泄漏到此前特征/候选预测；外部观测失败和20引用上限不能被用来宣称无缺陷。requires_review恢复/重新基线、受控快照归档仍需独立审计设计。扩大规模先探测CPU/内存/时间和空间峰值。
