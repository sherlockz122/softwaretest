# Baseline SZZ design

## Immutable input and time
仅接受与仓库当前接受HEAD/快照相同、已完整结束的Fix轮次；启动事务锁仓库，再锁该轮全部判定，冻结 rule_candidate/review_status/revision、created/reviewed时间及全部证据摘要。Fix复核在启动之后不改旧输入，新轮才能反映改变。as_of默认数据库UTC，显式时间必须含时区且不在未来。available_at取Fix创建/判定创建/有效复核/Issue观测及Fix事件时间最大值；晚于as_of的输入Unknown，不用当前状态倒推历史。高/中规则候选和人工确认分别保留来源，不声称人工证实全部规则样本。

## Git and filtering
baseline-szz-v1 固定唯一父，merge/root Unknown；native-v1 文件/删除行元数据与私有Git对象核对。只对解析完整的普通文本文件执行父版本 `git blame --root --line-porcelain`，默认whole-file rename跟踪，不使用-w/-M/-C或任意外部工具。空白仅过滤blank，不忽略Python缩进/字符串空白。Python全文件tokenize仅过滤独立COMMENT token行；Java词法状态区分行/块注释与字符、字符串、text block，Unicode预处理转义或未闭合构造Unknown；其它语言本批只过滤blank，明确限制。Python语法/tokenize失败Unknown，不启用可能吞代码的通用正则。文档路径、submodule/symlink、binary/编码/大小/超量/歧义状态保留Unknown或明确excluded。新增文件无旧行，删除文件追溯旧路径；代码行仅保存SHA256，无作者邮箱/原始blame。

## Evidence and labels
保留fix SHA、父SHA、旧路径/行、blob/行摘要、来源SHA/原始行与Git路径表示。来源必须在接受HEAD可达且同解析版本的集合内，并验证为父的祖先；时钟倒退导致来源事件晚于Fix事件时Unknown。只有该行满足输入可见性、来源覆盖及时间条件才提供candidate_buggy，label_available_at随证据保存；它是算法候选，不等同人工真值。无旧代码行/未知/非候选不自动生成clean；negative成熟度、观察窗口W及冲突/完整覆盖审核留数据集change。

## Execution and resource
三表szz_run/szz_item/szz_link。item启动时保存冻结输入，result仅在批次提交；每10项唯一链接/结果/checkpoint/进度同事务，最终批次含成功审计。旧token/租约/latest/root/version/hash拒绝；失败回执不覆盖成功。SZZ与同步/Fix互斥，复核可并行且旧输入不变。复用既有blob/diff/index/输出/deadline保护；每fix最多10000待追溯行，超过保留Unknown并保留其它文件已完成证据，不能伪造完整覆盖。启动/每批检查512MiB增长+2GiB紧急余量；D不足停止不转C。API每页最多100；公开DTO不返回快照路径、token、邮箱或源码。证据仅本机脱敏留存。

## Validation
真实Git golden、MySQL与迁移、并发/权限/幂等/冻结/时间/有限历史/空库、故障/真实Worker强杀及新卷浏览器，具体状态见tasks。基于 [Git blame](https://git-scm.com/docs/git-blame) 与 [Python tokenize](https://docs.python.org/3.11/library/tokenize.html)。

Java词法边界依据 [JLS](https://docs.oracle.com/javase/specs/jls/se21/html/jls-3.html)；非编译器语义验证，不将歧义输入当clean。
