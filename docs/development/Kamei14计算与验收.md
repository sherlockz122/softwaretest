# Kamei14 计算与验收

2026-10-11 第十一阶段交付计算内核，第十二阶段接入 `feature_service.py`、`0008_features`、可靠任务/A15/A16与仓库质量页面。数值版本仍 `kamei14-dg-v1`，租约回调/tree缓存改变源码摘要，新轮次冻结当前摘要。数据集/模型仍待后续。

## 公式决策

参考 [Kamei 等作者发布的论文](https://posl.ait.kyushu-u.ac.jp/~kamei/publications/Kamei_TSE2013.pdf) 第 3 节：NUC 使用相关文件上次修改提交的去重数。原基线“历史变更次数”改为这一明确口径，不是累计所有历史修改数。论文 AGE 为算术平均，项目保留 churn 加权，REXP 使用连续年数，因此命名为项目工程版本，不声明严格论文复现。

| 特征 | 本版本口径 |
|---|---|
| NS/ND/NF | 每文件目的路径（删除用旧路径）计数，重命名计一文件；NS 第一层目录，ND 父目录，根为 `.` |
| Entropy | churn=`LA_i+LD_i`，`-Σ p_i log2(p_i) / log2(NF)`；NF≤1 或总 churn=0 时 0 |
| LA/LD/LT | 当前完整文件新增/删除/变更前 LOC 原始求和，不按 NF 归一化、不做 log/标准化 |
| FIX | 冻结 bool 信号且 available_at≤当前 committer_time；缺失/晚到为 null；candidate 模式固定 0 并说明默认 |
| NDEV | 相关文件当前生命周期的历史作者 identity_key 去重 |
| AGE | `Σ(churn_i × 距上次修改天数_i)/Σ churn_i`；有正 churn 的首次文件/总 churn=0 时 null |
| NUC | 相关文件上次修改 SHA 的去重数量；新增/复制无历史贡献 |
| EXP | 当前作者此前唯一祖先提交数 |
| REXP | 作者此前祖先每项 `1/(1+elapsed_seconds/(86400×365.25))` 求和 |
| SEXP | 作者此前涉及当前子系统的提交去重数，一次改多个子系统仍计一次 |

历史来自严格祖先且 `(committer_time,SHA)` 小于当前提交；不混入接受 HEAD 的其它分支。祖先时间倒置（含同时间 SHA 在后）时六个历史量 null。保守的 DAG 口径区别于只按仓库全局时间扫描。

## 输入和缺失

`Snapshot` 要求 HEAD 在输入内、无重复/环/非可达提交，full 缺父拒绝。recent_window 六个历史特征统一 `history_incomplete`；完整根提交 EXP/REXP/NDEV/NUC/SEXP 的真实零保留。

输入/输出使用冻结 tuple/dataclass；时间采用与数据库相同的 naive UTC，拒绝带时区对象。identity_key 只允许已有 SHA256，原始邮箱、姓名、message、diff 不进入特征输出。`freeze_record` 要求显式 Git modes，缺字段拒绝；`freeze_native_record` 通过受监督 NativeReader 的只读、有输出/时间上限的 ls-tree 校验 modes/blob，再冻结；不运行仓库代码、hook、checkout 或网络。

当前 binary/encoding/limit/submodule/类型变更/非普通文件 mode/merge 等产生 `current_content_unsupported`，相关维度 null。无法对应实际 Git blob 的路径拒绝适配。祖先解析不完整/merge 时 NDEV/AGE/NUC/SEXP 为 `ancestor_content_unsupported`，EXP/REXP 仍独立按身份/事件计算。此策略扩大缺失范围，避免声称完整历史。

单父文件历史向后追溯 rename；新增/复制停止旧历史，删除再新增产生新生命周期。Git 路径按 `/` 与大小写解析，合法反斜杠是文件名字符，不按 Windows 目录规则转换。每项缺失有原因；`FIX=0` 候选默认带 `candidate_fix_default`，不当作观察到的确认事实。

## 调用与重算

```python
from packages.mining.feature_inputs import freeze_native_record
from packages.mining.features import Snapshot, compute, quality

# reader 为已有受监督 NativeReader；plan 必须来自已接受 HEAD。
inputs = tuple(freeze_native_record(reader, reader.record(sha)) for sha in plan)
snapshot = Snapshot(repository_id, accepted_head, inputs, history_coverage="full")
vectors = compute(snapshot)
report = quality(vectors)
```

集成调用方须验证数据库已接受 HEAD、storage_key、parser/coverage/Fix revision。本纯函数不能证明调用方给的是接受快照，**不得直接用 A11 的已入库并集**。权限、幂等、租约、恢复与持久化已由第十二阶段 FeatureService 接入。

`source_digest` 绑定 repo_id/HEAD/覆盖/解析/身份/feature version/规范化输入/模式；commit 输入顺序不影响摘要。`RULE_HASH` 对内核/适配代码统一 UTF-8 换行，避免 Windows/Linux 不一致。同输入重算内容一致，输入或规则变更需新轮次；后续唯一性不能只用 `(commit_id,feature_version)` 而忽略输入版本。

`as_dict` 返回副本。quality 只接收同轮次无重复向量，输出 min/p25/median/p75/p95/max、缺失数与原因，分位数为排序线性插值。无有效值为 null，不填充、标准化、生成标签分布或监督标签；异常下钻和质量页面已由 A16/FeaturePanel 接入。

## 验收与探针

```powershell
& .\scripts\Invoke-ProjectCommand.ps1 -FilePath "$PWD\.venv\Scripts\python.exe" -ExpectedGrowthGiB 0.2 -ArgumentList @('-m','pytest','tests/test_features.py','-q')
& .\scripts\Invoke-ProjectCommand.ps1 -FilePath "$PWD\.venv\Scripts\python.exe" -ExpectedGrowthGiB 0.3 -ArgumentList @('-m','scripts.probe_features','--counts','200','1000','--output','runtime/acceptance/stage11/probe.json')
```

真实 Git 两目录/两文件/两作者夹具最后一提交：NS=2、ND=2、NF=2、Entropy≈0.9709505945、LA=3、LD=2、LT=5、FIX=1、NDEV=2、AGE=2.4 天、NUC=2、EXP=1、REXP=`1/(1+3/365.25)`、SEXP=1，逐项手算断言。FIX 使用声明的历史 fixture 时间，不作为真实证据回填的先例。还包含真实 Git symlink mode/blob 验证，不要求 Windows 创建 symlink 权限。

合成线性探针 200 提交约 0.276 秒/0.868 MiB，1000 提交约 4.870 秒/4.311 MiB（含 tracemalloc 开销，Python 分配峰值，非 RSS）。这不覆盖原生 Git/MySQL 或真实精度。内核祖先扫描为 O(N²) 级基准；大仓库接入前做缓存/批次/超时/取消恢复与 modes/tree 缓存探针，保持同等因果规则，不将小样本当无限规模保证。

## 第十一阶段交给本批的注意点（已接入，历史记录）

1. 固定接受 HEAD/private storage/parser/identity/coverage 和 Fix/review 可用时间，冻结 source_digest/RULE_HASH。任务复用 execution_token/lease，禁止事后 FIX 泄漏。
2. A15/A16 及分页数值/缺失/异常下钻；数据库原子批次、旧 token、并发、取消、kill/retry、失败恢复和实际权限/新卷浏览器验收。
3. 成熟度/数据集再拆一批：预声明 W、全观察覆盖/冲突/unknown，SZZ 候选不直接转确定 buggy，无证据不自动 clean。
4. event_time 和 label_available_at 同时审计；今天才获得证据不回填历史。缺成熟两类训练样本明确拒绝/不足数据；train 拟合填充/缩放/采样，validation 定阈值，test 最后评估。
5. 可控制新增使用 D，开工测量与 ExpectedGrowthGiB；不足先停告知、不转 C；保留本机证据，仅 wang，核对确切 SHA CI。


## 第十二阶段 API 与使用

| 端点 | 契约 |
|---|---|
| A15 POST `/repositories/{id}/feature-runs` | Member/Admin、Idempotency-Key；未知字段拒绝。feature_version固定kamei14-dg-v1，purpose默认historical或candidate，fix_run_id可null或与当前HEAD/private快照一致的完整Fix UUID；202任务编号 |
| GET `/repositories/{id}/feature-runs` | 认证分页历史，created_at降序/root稳定排序，page≥1，page_size 1～100 |
| A16 GET `/repositories/{id}/feature-quality?run_id=UUID` | 认证读取本仓库轮次、完整质量/分页结果；feature可十四维之一，missing=true只缺失，false只有效按数值降序，ordinal稳定；不传feature按ordinal |

仓库页面完成解析后可选择历史/候选用途提取；自动选择当前HEAD已完成Fix，无Fix则历史FIX缺失。任务链接复用取消/重试；页面显示完整或未完成、接受HEAD/用途/覆盖/摘要，质量表可按缺失或较大数值下钻，不以较大值自动认定缺陷。Viewer只读，旧仓库晚到数据丢弃，响应未知使用原幂等键。

## 冻结、恢复与边界

创建事务冻结Fix review revision/status/evidence digest与available_at（生成/判定/复核/Issue观察最大值，UTC转换）。未复核且内容不完整没有否定信号。Worker从固定bare HEAD验证可达集合，排除A11未接受并集，full缺解析拒绝；recent六项历史null。modes/blob验证后一次事务冻结最小输入；非法UTF8路径无法与原始Git树匹配时整轮稳定失败，不以替换路径猜测普通文件。

每10结果/进度同事务，最后结果/总体quality/成功状态原子。重试沿用root输入及plan/source摘要，不重新读取当前Fix复核。旧execution_token、过期租约、latest/root/HEAD/storage/源码/解析/身份不符均拒绝。完整报告只在最终成功时发布；中途缺失质量表示未完成，不是有效零。

tree LRU上限64项和8MiB原始树字节，单Git输出仍受32MiB限制；Python对象实际占用高于原始字节。祖先/文件回溯和逐提交转换调用既有监督tick，检查D容量、租约/取消与总时间；不改变数字公式。O(N²)仍存在，资源探针不构成任意大仓库承诺。真实400提交Worker强杀/恢复与完整本机记录见第十二阶段报告。实际规模超时须重估CPU/内存/任务时间，不能为省磁盘降低数据质量。

无新依赖；项目3.11、系统3.13不变。预计工程0.5～2GiB及构建/独立验收3～4GiB峰值均用D。空间不足先停止告知，不转C；runtime/acceptance/stage12保留脱敏证据，仅wang发布与确切SHA CI。


下一批先实现成熟度与不可变时间数据集，设计预声明W和完整观察覆盖/Unknown决策；用合格两类样本验证可行性再训练。资源优化须在保持规则/源摘要与旧轮次不变的条件下验证，可补充按提交顺序读取提升tree缓存命中和更大真实仓库探针，不降低缺失/覆盖约束。


启动与Fix最终批次发布并发时，轮次/判定/证据使用当前locking read保持一致；不把幂等查询建立的旧RR快照用于读取新证据。双事务时序用例覆盖Feature和SZZ冻结的available_at，避免发布后证据遗漏。总体质量在事务外完成并接受监督，最后原子提交检查完整数量/摘要及有效租约。
