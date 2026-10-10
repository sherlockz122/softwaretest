# Baseline SZZ 追溯与验收

2026-10-10第十阶段在个人wang交付A13/A14及独立add-baseline-szz change。数据库head为`0007_szz`。版本`baseline-szz-v1`包含唯一父策略、blank/Python/Java词法过滤、默认Git whole-file rename及时间/覆盖检查；代码SHA256另行冻结，更改算法需要新版本和新轮。

## 使用与接口

按[工程启动](工程启动与验收.md)升级迁移并启动服务。仓库完成当前接受HEAD的Fix识别后，Member/Admin可点击“运行 SZZ 追溯”；Viewer只读。同步/Fix/SZZ活动任务互斥，取消/重试通过当前任务页面。requires_review时只能分析旧接受快照，不解除同步暂停。新轮保留旧轮输入、结果与行证据。

| 操作 | 契约 |
|---|---|
| A13 POST `/repositories/{id}/szz-runs` | Idempotency-Key；`fix_run_id` UUID必填；`algorithm_version`默认且仅支持baseline-szz-v1；`as_of`可省略/null，默认数据库UTC；显式时间需时区，未来拒绝422；未知字段拒绝；202 task_id/status |
| GET `/repositories/{id}/szz-runs` | 认证用户分页查询历史，created_at desc/root asc |
| A14 GET `/szz-runs/{id}/links` | 认证用户分页行证据，fix SHA/ordinal稳定排序 |
| GET `/szz-runs/{id}/results` | 分页冻结输入及各提交结果/文件状态、链接数；运行中展示已提交批次和待处理输入 |

默认page_size20，最大100。DTO不返回storage_key/token/邮箱/源码/原始blame；路径作为文本转义显示。相同用户/仓库/键及相同请求政策重放，输入不同409。默认截止时间在首次受理时固定，相同键重放不重新取now。页面丢失响应后查询受理轮次，并隔离切换仓库后的迟到响应；历史、提交结果及行证据分别分页。

## 冻结与时间

`szz_run`固定Fix根轮次、接受HEAD/私有快照、解析版本/历史覆盖、算法版本/摘要、Git版本、as_of、source_digest及组合label_version；`szz_item`在启动事务锁定Fix判定后冻结候选、规则、复核status/revision、证据摘要、confidence和available_at。仅接受已经结束且与当前接受HEAD/快照相同的Fix轮；不从未接受的同步候选或A11持久化并集生成标签。

available_at取Fix轮/判定创建、复核、全部Issue观测和Fix committer time的最大UTC时间。晚于as_of则Unknown，不能使用今天的复核状态倒推旧时点；本批不重建历史旧复核，若需历史视角，使用当时保存的不可变轮次。之后复核不会影响已创建SZZ，新轮才反映变化。label_version摘要绑定算法、规则、解析、覆盖、cutoff、Fix根轮次/HEAD/medium政策和冻结输入；算法摘要同时覆盖追溯引擎与冻结/发布服务代码，按统一换行读取，便于Windows/Linux核对；label_available_at随每条证据保存。

行级结果`candidate_buggy`是算法候选，保留reviewed/high/medium来源，不等于所有样本均人工确认。来源需在接受HEAD可达/同版本已解析集合中且是Fix父的祖先；直接父合法。来源committer time晚于Fix（Git时钟倒退）为Unknown。recent_window之外来源保留行证据及outside_parsed_history，但不可作为可监督正样本。事后观测只能在其available_at之后使用，后续数据集同时限制event_time和label_available_at，特征不能使用标签证据。

**本批不生成clean/负标签**。缺少候选、未追溯代码、根/merge、失败或受限覆盖均不能用作0；标签年龄/观察窗口W、完整覆盖、冲突和抽检审核将在不可变数据集change设计。现有规格中的clean定义是后续目标，不是仅凭本轮缺证据即可运行的实现。

## 追溯策略与界限

- 唯一父，root/merge明确Unknown；解析不可用沿用明确状态。只读私有bare，不改对象、分支、refs或工作树，不重新联网。
- 删除/替换行取同版本已解析diff行号，读取父版blob，核对ls-tree路径/blob/普通文件mode。新增文件无旧行；删除文件用旧路径；doc/docs、README和.md/.rst/.adoc排除；symlink/submodule/binary/编码/体积/歧义状态保持Unknown。
- 空白只过滤blank，不使用`-w`或忽略缩进/字符串空白。Python全文件tokenize过滤独立COMMENT token行，字符串/多行字符串及其内部空白行保留，词法失败Unknown。Java词法状态区分//、/* */、字符/字符串/text block，保留text block内部空白行，仅过滤整行注释；未闭合构造或Unicode预处理转义为Unknown，不能把不确定语义当注释删掉。Python/Java支持LF/CRLF，孤立CR导致词法与Git行号可能不一致时保持Unknown。其它语言本版只过滤blank，不宣称通用语言语义过滤；扩展需新版本。
- `git blame --root --line-porcelain --no-textconv PARENT -- OLD_PATH`保留默认whole-file rename，不启用-M/-C或跨文件移动推断；原始路径用Git quoted表示保存。逐行核对blame行内容哈希，不持久化作者信息或代码正文。
- 每Fix最多10000待追溯行、每次blame输出8MiB，加既有blob/diff/index/deadline监督。超限文件Unknown，其它已完成证据保留，结果明确partial；额度是运行保护，不是可随意扩大后宣称完整的数据窗口。扩大规模先测CPU/内存/时间与空间峰值。

策略依据：[Git blame](https://git-scm.com/docs/git-blame)、[Python tokenize](https://docs.python.org/3.11/library/tokenize.html)、[Java词法规范](https://docs.oracle.com/javase/specs/jls/se21/html/jls-3.html)。这些工程过滤不构成RA-SZZ，也未完成真实项目精度评估。

## 恢复、验收与后续

每10提交item结果/szz_link/checkpoint/进度原子，最终批次含成功审计；UNIQUE(item,ordinal)和固定输入/计划防重复。旧token/租约/status/latest/root/版本/快照拒绝。未知完成回执保留已成功结果；真实SIGKILL后successor沿用冻结输入，期间Fix复核也不改旧结果。读取事务中的取消收尾在独立心跳事务提交，避免读取中断回滚取消终态。

0007为冻结DDL，空库base/head往返、0006旧Fix/提交数据保留、受影响整路径非空降级在第一条DDL前拒绝。启动/每批复用512MiB增长+2GiB紧急余量；新增预计0.5～3GiB在D，实际仓库/构建峰值另计，不足停止告知、不转C、不prune业务卷。JUnit、故障日志、截图、容量/分支/确切SHA CI和SHA256清单仅保存在`runtime/acceptance/stage10`，脱敏，不上传Actions artifact。

下一批：Kamei14 golden/时间与身份/有限历史缺失规则、观察窗口及冲突/完整覆盖审核、不可变时间数据集，先验收防泄漏再接LR/RF。Java Unicode预处理、其它语言过滤、跨文件移动、历史分叉批准/重新基线和快照归档仍须明确版本/审计设计。不得因为D空间足够直接扩大未验证的数据或将Unknown置0。见[整体规划](后续整体规划与空间预算.md)。
