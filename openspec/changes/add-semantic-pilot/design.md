# 合同

固定第二十二阶段dataset037a87d0-609e-4822-b2b4-78a49fe73e55/已发布三分区，不重算旧规则或改标签。先导apache/activemq，train按SHA取512、val179/test138全保留；选择不看标签/模型结果，实际文本失败明确拒绝和报告缺失人口。只接受完整单父Git对象、纯UTF8文本、有界文件/patch/base；Java及配置/其它文本逐文件记录语言线索，未知不认证，记录原始对象摘要、作者时钟、首次取得时间及许可证，不读取fix或未来提交。

新不可变输入schema分message/added/deleted/base角色，记录逐文件语言线索、空角色、来源和完整性；token128，每角色至多2个均匀首尾窗口，保留总token/被覆盖token/截断。各角色attention-mask去特殊token均值，再按覆盖token加权窗口均值；拼接四角色向量及mask，空角色零向量。缓存绑定输入、源/权重/tokenizer文件、策略/代码/依赖/CPU线程与batch，摘要失败不重用。

独立tools/semantic环境，CPU-only、4线程、batch8；原始官方权重核验LFS SHA256、隔离weights_only张量读取后转换safetensors，未知对象/缺额外权重拒绝，运行只读本地safetensors且无remote-code。子进程受父监督取消/期限/RSS6GiB/实际D盘2.5GiB，失败保留证据而不发布半成品。

同一eligible原分区比较LR/RF数值、冻结语义LR、数值+语义LR；train-only标准化，RF原值，无降维/重采样，validation同时间组前半校准/后半F1阈值，test仅终评。固定42/17及42重复；PR-AUC/MCC/F1/Brier/可靠性/effort/时间块区间，两类支持、按语言线索/时间/缺失及重复摘要。added/deleted交换与message屏蔽仅预声明诊断，不据test选模型或改变输入。小测试正例和预训练可能污染显式保留，不声称泛化或产品效用。

源/训练输入、编码器/缓存和模型三层独立manifest，校验后原子目录登记；损坏拒绝，失败不覆盖旧版本。旧数据/FeatureRun false、83pending/2unresolved、NFR05原始容器字节确定性及其他治理问题保留，另有具体验收安排。

主展示固定seed42，17为预声明稳定性复验。研究收益门槛在读新test预测前固定：融合对数值LR的AP绝对增量至少0.02，MCC/PofB20不降、Brier不升，两个seed均须满足；任一不符只报告负/不确定结果，不按test换算法/人群。测试支持极小即使达到数值门槛也不构成显著性、目标域效用或候选发布证明。
