# Context

US-06～09 依赖已接受 HEAD，而 A11 是已入库记录并集。首批采用纯函数输入契约，集成必须由后续服务验证 HEAD/私有 storage_key/完整窗口，不能直接传 A11 并集。

# Goals / Non-Goals

当前交付可手算、无副作用、可复现的十四维内核与质量统计。任务、持久化、鉴权和页面下一切片；数据集及模型在其后。不以 engineering fixture 声称真实精度。

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

同时限制 event_time 与 label_available_at；观察窗口 W 预声明，clean 必须有完整观察覆盖、无冲突、无未知未复核证据。现阶段 SZZ 候选不直接作为确定标签。历史证据今天才生成时，历史 train 截止可能没有可用样本，必须明示不可构建/不足类别，禁止回填时间。冻结接受快照/特征轮次/SZZ/Fix/revision/identity/公式/label版本，train-only 填充/缩放/采样，validation 阈值，test 最终评估。

# Test Strategy

真实 Git 经 NativeReader 的两文件多作者夹具逐项手算十四维；重命名、删除再新增、根/零 churn/merge/binary/编码/超限；缺父/full/recent/未接受分支、同时间 SHA/倒置时间、晚到 Fix、输入冻结、摘要顺序/版本变化、质量缺失与分位数；全量既有后端/新卷浏览器/静态/规格/契约回归及确切 SHA CI。本机 runtime/acceptance/stage11 保留脱敏证据。
