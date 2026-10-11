# Context

US-06～09 依赖已接受 HEAD，而 A11 是已入库记录并集。首批采用纯函数输入契约，集成必须由后续服务验证 HEAD/私有 storage_key/完整窗口，不能直接传 A11 并集。

# Goals / Non-Goals

第十一/十二阶段已交付十四维内核、任务、持久化、鉴权与页面。第十三阶段交付成熟度/不可变时间数据集内核及真实来源只读审计；A17～19/迁移/页面下一切片，训练在合格样本后。不以 engineering fixture 声称真实精度。

# Decisions

1. 输入为冻结的 Commit/File/FixSignal；身份仅保留现有 identity_key，不带姓名、邮箱、message、diff。Snapshot 校验解析/身份版本、SHA、重复、DAG 环、HEAD 可达性。full 缺父拒绝；recent_window 允许缺父但六个历史特征 null，不伪造零。摘要绑定 repo_id、HEAD、覆盖、算法/源代码摘要及实际输入；提交输入顺序不影响摘要，文件序号沿用解析器冻结顺序。
2. 历史范围使用 DAG 严格祖先与 `(committer_time, SHA)` 前序的交集，同时间 SHA 明确；非祖先分支不能污染当前提交历史。遇到时间倒置祖先，六项历史特征 null 并记原因。EXP/REXP 用唯一祖先提交；SEXP 用作者相关子系统提交去重。首层目录为子系统，根目录为 `.`；目录使用 Git POSIX 路径，不按宿主 Windows 规则折叠大小写。
3. 普通提交文件 `new_path or old_path` 计 NF/ND/NS，重命名只计一次目的路径。LA/LD/LT 原始求和；不除以 NF、不 log、不缩放。Entropy 按 churn 分布除 log2(NF)，全零 churn 为零。任何当前文件非 parsed（含 binary/encoding/limit/submodule）、当前 merge 或无效内容，依赖特征 null，绝不将略过文件零贡献生成有效向量。
4. NDEV 是相关文件祖先开发者并集；NUC 是各文件上次修改 SHA 并集数。AGE 采用本项目 churn 加权天数，首次文件或零 churn 无定义则 null；REXP 使用连续 elapsed_days/365.25。与论文算术 AGE/年份桶不完全相同，明确命名 `kamei14-dg-v1`，不声称逐字论文复现。重命名沿单父链回溯；新增终止旧同名历史，删除再新增不混用。存在 merge 祖先或无法完整解析的祖先时文件历史 null；EXP/REXP 独立可算，SEXP 在祖先文件不完整时 null。
5. 历史 FIX 仅接受与当前记录绑定、available_at <= event_time 的版本化冻结信号，晚到或缺失 null。candidate 模式统一 FIX=0，明确原因/default，摘要绑定模式。不读取 SZZ 标签，人工复核不能回填历史 FIX。
6. 输出为冻结 tuple；as_dict 返回独立副本。每项 null 带原因，quality 提供缺失数量/原因、分位数/min/max，绝不填充或构造标签分布。同输入重算摘要、结果完全相同；代码摘要按 UTF-8 统一换行避免 Windows/Linux 差异。
7. 现有解析表尚无 Git modes，不能从 parsed 状态推断普通文件。只读适配用受监督 NativeReader 的 bounded ls-tree 验证 mode/blob 后显式投影；symlink/gitlink/type-change 为 unknown。缺字段或无法匹配原始 blob 拒绝。下一批服务实现 tree/mode 缓存与固定私有快照校验，不能直接把数据库 file_change 扔入内核。

# Risks / Trade-offs

祖先集合/逐提交扫描首批正确性优先，非大仓库性能承诺；记录线性资源探针与分支因果用例，后续任务集成前做规模/内存探针，缓存不得改变因果/路径边界。纯函数接收者不能证明调用方是真正接受 HEAD；真正权限、快照 fencing、空间/超时/恢复由后续服务承担。

# Failure Recovery / Security

纯函数无数据库/网络/仓库写入，校验失败产生稳定 FeatureInputError code，不留下半成品；输入不可变、输出复制、无原始身份或 diff。中断只丢当前内存计算，重算确定。后续任务复用现有 execution_token/lease/checkpoint 原子批次，不能新建平行任务框架。

# Dataset Follow-up

## 第十二阶段纵向决策

`0008_features` 新增 feature_run/item，唯一键包含根轮次与 SHA/ordinal。启动事务冻结接受 HEAD/storage、初次解析覆盖、用途及可选同 HEAD 已完成 Fix 的 review/证据摘要；available_at 取轮次生成/判定/复核/Issue 观察时间最大值，未复核且内容不完整不能形成否定 FIX。无需 Fix 也可提取，历史 FIX 缺失。

Worker 在固定私有 bare 仓库运行有监督的可达计划，交集过滤 A11 并集；full 缺解析拒绝。初次计划验证 mode/blob 后原子冻结最小 CommitInput，重试从冻结输入重算并校验摘要，不重新取当前复核。源摘要另绑定 storage_key/Fix 根轮次/Fix冻结摘要；公开响应不带私有 storage 或原始身份。tree LRU 至多64项且累计原始树字节8MiB（Python对象占用另计），祖先/文件回溯周期调用租约/取消/空间/总时限检查，数值公式保持一致，源码摘要变化产生新轮次。

每10结果与checkpoint/进度同事务；最终批次同时发布完整质量和成功状态。部分轮次没有总体质量。复用现有协调器自动续跑与人工retry；parse/sync/Fix/SZZ/feature 互斥，旧token/latest/root/HEAD/storage/源码/解析/身份变化拒绝。数据库迁移整个降级路径先检查数据后DDL。

A15 POST `/repositories/{id}/feature-runs` 支持 feature_version/purpose/fix_run_id；认证分页 GET 同路径，A16 GET `/repositories/{id}/feature-quality?run_id=...` 提供完整统计、分页结果及 feature/missing 下钻（有效值按数值降序，ordinal稳定次序）。页面显式历史/候选用途、覆盖/源摘要/缺失、运行中/失败状态，复用当前任务链接取消/retry；不增加标签或预处理。

保留 O(N²) 特征内核复杂度边界，第十二阶段以真实400提交 Worker 故障恢复与合成规模探针验证；不以夹具声称大仓库无限性能，超时需根据真实资源重新估算。第3组交付成熟度/数据集内核，第4组纵向接入；模型仍依赖合格真实数据，未交付不得提前勾选。

同时限制 event_time 与 label_available_at；观察窗口 W 预声明，clean 必须有完整观察覆盖、无冲突、无未知未复核证据。现阶段 SZZ 候选不直接作为确定标签。历史证据今天才生成时，历史 train 截止可能没有可用样本，必须明示不可构建/不足类别，禁止回填时间。冻结接受快照/特征轮次/SZZ/Fix/revision/identity/公式/label版本，train-only 填充/缩放/采样，validation 阈值，test 最终评估。

# Test Strategy

## 第十三阶段拆分及时间决策

单个今天完成的SZZ不足以证明历史train截止时已知道clean；内核支持多轮完整观察，历史HEAD必须为接受HEAD的祖先且其输入恰为该完整祖先集合。覆盖结束取绑定私有快照的实际clone/sync started_at（保守下界），published_at取实际成功任务finished_at及证据可用时间最大值；无可核对来源不得宣称完整覆盖。未复核candidate/不完整解析/Issue不可用/过时复核使相关祖先Unknown，确认且完整合法追溯才产生buggy。

默认W=90日是预声明工程政策，不是从测试集优化所得；允许显式正整数配置并纳入label版本/内容哈希。clean可用时间不得早于W届满与完整观察发布；buggy不得早于Fix复核/证据及SZZ发布。未来的已知修复可以纠正新轮次，旧数据集不改写。

仅完整历史的historical特征可进入训练审计；按event_time分组后70/15/15确定边界，同时间SHA排序不跨分区。再以label_available_at严格早于下一分区首事件purge，test不晚于snapshot。不根据purge结果反向移动边界。每区须两类；train任一十四维全缺失明确不可训练，不以填0藏缺失。冻结原始向量/原因、源与规则/配置/切分/purge摘要；类权重N/(2*N_class)仅train。填充/缩放/采样工件在模型阶段仅train拟合，本批不拟合。

只读MySQL适配和CLI是本机审计入口，不新增并行任务框架/线上接口/迁移。读取完整冻结轮次与共享locking read，校验解析/身份/公式/SZZ源码和输入集合/摘要，保守核对当前Fix复核。工程时间线夹具可验证成功路径，但不能冒称已有真实成熟两类数据。真实已存轮次若不满足，输出不足原因，A17～19在下一批接入。本批沿用全量/浏览器/严格规格/报告/最终SHA CI工作流。

真实 Git 经 NativeReader 的两文件多作者夹具逐项手算十四维；重命名、删除再新增、根/零 churn/merge/binary/编码/超限；缺父/full/recent/未接受分支、同时间 SHA/倒置时间、晚到 Fix、输入冻结、摘要顺序/版本变化、质量缺失与分位数；全量既有后端/新卷浏览器/静态/规格/契约回归及确切 SHA CI。本机 runtime/acceptance/stage11 保留脱敏证据。


## 第十二阶段并发发布边界

MySQL REPEATABLE READ 下，创建事务的幂等查询可能先建立旧快照，随后等待仓库锁到 Fix 最终发布。FixRun/Assessment 的 locking read 会看到当前已提交轮次，Evidence 必须同样使用当前共享 locking read，不能混入旧一致性快照。特征和SZZ同时修正；受控双事务时序用例验证晚发布证据摘要/available_at完整冻结，不改变已有轮次或追溯算法。
