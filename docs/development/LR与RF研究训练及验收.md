# LR与RF研究训练及验收

第二十二阶段，当前实施与验收入口；最终结果及准确提交以本批报告/CI证据为准。沿用[用途分离数据集](用途分离数据集与验收.md)、[最新规划](后续整体规划与空间预算.md)及自有01～08，不依赖外来原型。

## 输入与拟合合同

只接受已发布`published_binary_12_v1`研究数据集ID、lr/rf、允许参数和seed。固定原数据集manifest、成员/分区摘要、十二列及弱监督目标；Worker核验DB全部成员与三份CSV精确一致、无额外成员、来源摘要和元数据不变。只消费固化版本，不调用当前dataset规则重算旧版。非就绪、漂移、任意路径、多余字段或不兼容用途拒绝；不补LT/FIX/类型、旧false不回写。

训练区仅拟合StandardScaler+LR或原数值RF；当前不重采样、不使用类别权重，完整有限十二列不接受缺失或负数。LR默认C=1/max_iter=500，范围C[0.001,1000]/轮数[50,1000]；RF默认100树/深度16/叶最少2，范围10～256树/深度2～32/叶最少1～100，n_jobs固定1。seed默认42，整数0～2147483647，bool不是合法整数参数。

validation按唯一event_epoch组的前后半划分校准与阈值区，不拆同时间组；两部分各须两类。校准为固定裁剪[1e-6,1-1e-6]后的logit与sigmoid logistic（C=1e6/200轮）；阈值按后半F1在0.05～0.95步长0.01选择，平局取更高阈值。不足拒绝，不借test；不在完整validation上再拟合校准器或模型。测试只终评，不据它选择参数/seed/模型；双seed为预声明稳定性研究，不择优。

## 运行与工件

0011_models新增model_run/model_artifact，模型JSON均无损规范文本。v2修正跨仓并列SHA排序；当前research-lr-rf-v3另以规范JSON字符串保存任务请求，避免MySQL JSON浮点末位变化，并在训练及注册时核对根运行参数。超大整数先检查范围再检查有限性，API返回422。初始v1/v2模型、工件和失败证据保留而不改写。code_sha明确是模型源文件及uv.lock规范内容的组合SHA256，sources逐文件可核验；阶段报告另绑定实际Git交付SHA，不将未提交源码冒称属于旧Git版本。依赖记录scikit-learn/numpy/scipy/skops/psutil/joblib/threadpoolctl实际版本。

model.train复用事务outbox、用户当前角色、幂等、最新尝试、令牌和租约栅栏；相同有效请求含默认参数重放同根。重试保留失败原任务和同根，独立attempt目录；fit中断后重新训练，不冒称恢复模型内部迭代。完整report/模型/概率CSV与manifest校验后，最后事务一次注册三项工件和成功，事务失败不能出现可用模型。

监督子进程每0.5秒检查，每2秒续租/检查实际模型输出挂载余量；整轮900秒、子进程树RSS2GiB、输出物理盘最后保护2.5GiB，宿主ExpectedGrowthGiB仍需提前预算。训练单线程，父退出后子按父PID及创建时间检查退出；取消、内存/时限/空间失败杀死未完成子进程。Worker和DB健康、磁盘充足不能代替训练资源探针。

model.skops包含模型/处理器/校准器/阈值/列顺序；序列化后未知类型必须为空才以trusted=[]重载，并逐项核对全分区概率。只处理本任务生成的工件，不自动加载外来pickle或不明类型。设计依据：[scikit-learn工件文档](https://scikit-learn.org/1.7/model_persistence.html)、[概率校准](https://scikit-learn.org/1.7/modules/calibration.html)。

API只读models卷、Worker可写，model-init只初始化该根，不递归改正常内容；DG_MODEL_DATA_DIR是部署配置，不是请求参数。数据集和原资产保持各自挂载与资格。

## API及界面

| 编号 | 已实施合同 | 资格 |
|---|---|---|
| A20 | POST /api/v1/model-runs，Idempotency-Key，dataset_id/plugin/params/seed，202含model_run_id/task_id | Member/Admin |
| A21 | GET /api/v1/model-runs/{id}，状态/冻结输入/完整报告/工件清单 | Viewer及以上 |
| A21工件 | 同路径artifact=model.skops/report.json/probabilities.csv，核验manifest及登记hash后下载 | 已完成注册，只读受控名称 |
| A22 | POST /api/v1/model-runs/{id}/publish | Member/Viewer拒绝；Admin仍因当前研究用途不兼容返回MODEL_PURPOSE_INCOMPATIBLE，不产生产品发布 |
| A23 | GET /api/v1/models分页研究运行及LR/RF插件，registered区分工件是否完整，production_models=[] | Viewer及以上 |

/models选择已发布数据集、算法、有限参数/种子；数据集选择和运行列表分别分页。/model-runs/:id展示验证阈值区/测试/常数基线，两类/校准/成本指标、下载与固定审计；任务页管理取消/重试，刷新后仍需重新登录且允许回跳。研究注册不代表产品发布；界面无无效产品发布按钮，360px/表格聚焦键盘滚动及展开报告需要实测。

## 指标解释

Precision/Recall/F1、ROC-AUC、PR-AUC=average precision、MCC、混淆矩阵、Brier和十箱可靠性；无预测正类时precision采用zero_division=0，单类的AUC/MCC为null及原因。测试常数基准为train的buggy先验，阈值0.5，不借测试分布。

effort=LA+LD；概率降序、event升序、SHA升序，最后以canonical身份打破相同SHA并列。PofB20包含首次跨过20%成本的完整提交，不按提交行部分计数；IFA为首个buggy前clean数；Popt以buggy/effort密度最优/最差参考梯形面积归一化，零成本buggy最优置前，参考面积相同时null。全部零成本或无buggy返回null和原因。

测试按至多20个连续唯一时间组块重采样100次，固定seed，给PR-AUC/MCC/Brier/PofB20的2.5%～97.5%描述性条件区间与有效次数。不是金标置信度或跨仓库泛化证明。验证阈值区因参与选择，其指标不是无偏终评。未来跨语言/语义/候选另按独立资格和同口径验证。

## 验收与接续

本批复现按完整规范学习参数/树结构、校准器、阈值及逐条概率验证：同seed的数值状态摘要和probabilities.csv精确一致。skops归档含序列化实例元数据，report保留各次真实耗时，其原始文件hash各自登记且已经过完整性/重载验算；没有宣称跨run归档和报告原始字节相同。原始序列化容器的字节确定性保留为工件格式治理限制，NFR-05整体验收不借本批标Done。

手算指标/时间同组/参数资源边界、test扰动不影响拟合/校准/阈值，真实MySQL/RBAC/幂等/成员漂移/取消/故障重试/旧令牌/原子回滚、Worker SIGKILL、新卷LR/RF浏览器与模型数据备份恢复。真实固定106674两模型各42/17种子，加42重复精确概率复现；报告原样负结果，不依据测试表现搜索参数。

保留初始失败、完整回归、模型报告、实际资源、源码镜像和最终确切SHA CI证据到runtime/acceptance/stage22；正常卷不清空。研究链完成后按规划推进真实文本/冻结编码器与融合P1，有限来源质量线继续；生产/历史/候选、八模型、类型定位、多语言和治理仍待验收。
