## Decisions
仅支持published_binary_12_v1，消费已发布manifest与固化成员/CSV；不调用当前dataset构建规则重算旧版。创建冻结数据集摘要、十二列、目标、插件/参数/种子/代码及依赖；Worker重验完整DB成员/分区和工件。无损数字文本及CSV逐项精确核对，不放宽容差。

model_run根ID对应首次任务；重试保留同根/最新任务栅栏，独立attempt目录。model_artifact记录固定名称/大小/hash，无用户URI。原子事务登记完整工件/成功，不发布半成品。A22产品发布检查当前用途并拒绝，无研究模型自动上线。

scikit-learn1.7.2，LR标准化train-only、RF原始数值；默认无重采样/类别权重。validation前半同时间组拟合sigmoid校准，后半同组选择F1阈值（固定0.05～0.95步长0.01，平局取较高阈值）；不足两类/组时拒绝而不借用test。训练/校准/阈值子集及规则固定，test只终评。基准为train先验常数；指标包括两类、PR-AUC定义为average precision、ROC-AUC/MCC/F1/Brier/可靠性、PofB20/IFA/Popt（并列概率按event、SHA、canonical顺序，完整提交跨过20%边界，零effort无穷密度置前的最优/最差参考；面积梯形积分，分母0返回null）。测试连续时间块bootstrap固定seed/100次，仅报告描述性条件区间，不用于择优。

训练用独立本地子进程，父每0.5秒监督取消/租约/900秒/2GiB RSS/物理盘余量2.5GiB；每2秒容量监测、阶段检查点和租约续期；进程线程上限1，RF n_jobs1/最大256树/深度32，LR最大1000轮。子进程父身份消失即退出；fit无法中途接续，故障重试从固定输入重新训练，禁止伪称恢复模型内部状态。工件skops不加载外来pickle，不自动信任未知类型；序列化后重载预测精确核对，JSON报告及逐行概率保留。

schema0011降级对整个请求目标路径预检：模型运行/工件/训练任务以及将受影响旧表/引用必须为空，任何DDL前拒绝。0001～0010不改。模型目录Worker可写/API只读、dataset只读，现有初始化器不得递归更改正常卷。
## Verification
手算指标/阈值、test扰动不影响模型/校准/阈值、两类/同组/精度及schema漂移、skops拒绝未知类型；真实MySQL幂等/RBAC/取消/过期令牌/原子回滚/降级，真实Worker终止和重试，106674固定版LR/RF双seed复现及资源实测、独立新卷浏览器/备份恢复摘要；旧完整回归/锁/安全/strict/合同/精确SHA CI。

## 验收中合同纠正
初始v1跨仓并列将canonical排在SHA前，与原03规范不符；二样本反例确认后改research-lr-rf-v2的event/SHA/canonical顺序，补独立oracle。保留旧模型/失败及中断回归，参数/校准/阈值不变，新版本重新训练和验收，不依据测试结果优化排序。
