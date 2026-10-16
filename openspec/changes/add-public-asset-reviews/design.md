# Context

遵循2026-10-14公开数据与语义路线。首批只ApacheJIT显式CSV适配，其他JSONL源/语言/类型导入待后续；派生JSONL是可重载原始记录与12数值视图，非已创建训练数据集。

# Decisions

资产ID绑定来源清单、adapter/rule版本、原始及派生文件摘要；同盘临时目录完整写入/校验再发布，已有资产重载而不覆盖或更新原取得时间。存储根限runtime内，路径拒绝链接/重解析；总字节、行/字段、合作式时限、取消及D容量受控。清单含源事件口径/实际取得、原cohort/canonical仓库、未知类型/语言验证与能力不足。未来重新适配另ID，旧资产保留。

review是独立不可变链：审计报告自哈希/来源与资产绑定、包成员原摘要、revision与前序hash、实际复核时间/证据状态，未审或未决不自动变clean/buggy/类型或gold。证据文件仅runtime内有界JSON、引用按hash绑定；原生探针用无网络/替换/alternate隔离Git环境，缺对象及浅历史返回unknown；不把merge-base退出1在不完整历史下当无祖先。

本批本机CLI不具有应用用户身份，记录operator只是本机复核声明，不冒充已实现RBAC/授权认证。review的verified provenance指绑定通过，不认证陈述真值。source report readiness不回写，研究数据集与A17～19下一批接入。

# Risks / Trade-offs

取得来源真实性依赖固定校验与来源清单，文件哈希不是数字签名；原始代码材料只封存不执行。磁盘资产增加约原始+JSONL和临时副本，先预算1GiB/实测再处理。合作式旧public audit调用有独立60秒上限，取消可能延迟至该调用返回；不承诺硬实时。定向85条不是总体噪声率。

# Validation

专项验证合法原始重载/派生/分页、输入/清单/输出漂移/不覆盖、并发/失败清理、链接/越界、行/字段/期限/取消/容量、review源/成员/前序/revision/UTC/证据绑定及独立副本。真实106674发布CSV与原报告绑定，existing AMQ有限抽检；完整已有后端/故障、前端/独立新卷、存储保护、最终SHA CI与其他分支核对。
