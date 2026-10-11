# Decision

以[历史回放决策](../../../docs/decisions/2026-10-11-历史回放与可知特征合同.md)为当前合同。kamei14-dg-message-v2继承数值工程公式，FIX为当时提交message关键词/否定/文件上下文启发式，不读后来Issue/复核/缺陷标签；0是未命中，不是确认非修复。

historical-horizon-replay-v1是兼容已观察链接的固定horizon目标，不能与生产永续实际观察标签混同。W内验证正链接+L重建知识；完整兼容coverage且无未决才允许W末+L负例，否则Unknown。W90/L7是预声明实验默认而非实测结论。source实际取得时间不回填；调用适配器认证严格祖先/覆盖/金标。两类fixture不证明真实数据可训练。

# Boundaries

原始完整事件固定70/15/15、同时间组整组，先边界再purge；train/validation严格早于下一集合首事件，test不晚于as_of。保留Unknown/purge和缺失，缺类不改边界，类权重只train，未拟合。CausalRun/ReplayAudit canonical JSON返回副本，来源/原向量/版本/规则/政策/证据/切分摘要均绑定。

1000提交、10000文件/链接、32MiB回放向量JSON、30秒合作式监督；native Git300秒/既有输出上限，拒绝重解析/alternates/覆盖源，禁网络懒获取、全局配置、hook，不检出代码。D预计增长与中途余量监督，不足停止不转C。旧O(N²)未优化，规模限制不是全局存储紧缩策略。

# Validation and follow-up

固定原作者revision和Git blob/SHA256的静态来源/UTC关联；两个ActiveMQ深度8窗口只证明当前LT/message可计算，六项历史缺失、标签Unknown保留。真实AMQ修复message信号0说明启发式覆盖限制。首次失败和最终JUnit保留本机，只安全副本脱敏索引；无Actions artifact/HAR/trace。后续接v2/来源认证/迁移/A17～19/任务及实际模型工程，详见遗留表，不声称团队故事Done。
