# Design: add-dataset-14dim-and-split-choice

## Context

十四维 Kamei 口径（NS、ND、NF、Entropy、LA、LD、LT、FIX、NDEV、AGE、NUC、EXP、REXP、SEXP）里，数据集成员行只实现了 12 个数值特征（LT 在既定口径中被 LA/LD 取代，FIX 是标签来源）。用户要求每个样本行展示完整十四维，因此把 FIX（修复标志）与 LT（改动前文件总行数）作为行级辅助字段补上。

数据集的 70/15/15 固定切分在课程项目的对照组场景里过于僵硬：想跑「只分训练/测试两份」的实验没有入口。需要把比例交给用户，并让「没有验证集」成为一等公民——训练链路不能因为没有验证分区就拒绝工作。

## Goals / Non-Goals

**Goals:**

- 每个数据集样本行带 `fix`（布尔）与 `lt`（缺失时「未提供」+ 原因），展示、下载、接口三处一致。
- 用户自选训练/验证/测试比例，验证比例可为 0；两分区数据集全链路可用（构建→工件→下载→训练→评估→风险列表）。
- 无验证分区时训练校准与定门槛在训练区内完成，报告如实标注、不冒充验证集。
- 界面口径「代码行数」→「样本数」，训练与历史风险列表职责边界文案明确。

**Non-Goals:**

- 不补算 lt；不新增数据库迁移；fix 不进训练特征。
- 不做比例的前端「预设方案」下拉（70/15/15 等），数字输入已足够，预设留到有需求再说。
- 不改候选预测链路的十四维口径与语义研究链路的产品定位。

## Decisions

### 决策 1：fix/lt 存数据集成员的 source_json，不建数据库列

技术选型：fix/lt 作为成员行的辅助字段写进 `dataset_member.source_json`（既有 JSON 列），随 `numeric12` 一起返回；CSV 工件按「前 4 个身份列 + numeric12 + fix,lt」落盘。

备选路径：① 为 fix/lt 新增数据库列——需要迁移 + 降级保护 + 旧行回填，只为两个展示字段不值；② 单独建一张 `dataset_auxiliary` 表——多一次 join，且成员一致性校验要跨表。均不采用：两字段体积小、只读、生命周期与成员行完全一致，JSON 内嵌零迁移成本。

### 决策 2：lt 上游缺失时存「未提供 + 原因」，不补算

技术选型：存储的上游源（apachejit_total.csv 归一化产物）没有 lt 列，成员行 `lt=None`、`lt_missing_reason="upstream_source_omits_lt"`，元数据记 `lt_source="missing_in_stored_upstream_source"` 并列入 limitations。前端「未提供」列悬停展示原因。

备选路径：① 从上游 git 仓库补算 lt（改动前文件总行数）——需联网逐个克隆仓库、逐提交 diff 父版本，构建时长与网络不确定性不可控，且上游口径本身就不含 lt，补算结果无法与上游对齐；② 干脆不放 lt 列——违背用户「完整的十四维基本数据」的明确要求；③ 把 lt 填 0——伪造数据，绝对不可。不采用。

### 决策 3：无验证分区时，训练在训练区内按时间切前后两段做校准与定门槛

技术选型：`fit_model()` 检测到 validation.csv 缺失时，把训练分区按 event_epoch 切成前段（拟合校准器）与后段（定门槛）两段，流程与有验证分区时完全同构（同一个 sigmoid-logit 校准器），只是 `calibration_partition="train"`；报告键用 `train_threshold` 而不是 `validation_threshold`，limitations 加 `no_validation_partition_train_internal_calibration`；风险列表默认分区对这类模型是 `("test",)`。

备选路径：① 拒绝无验证集训练——直接违背「验证集不是必须」的需求；② 门槛固定 0.5 不校准——校准器是模型概率对齐的核心组件，跳过会导致阈值失真；③ 用测试分区定门槛——泄漏，绝对不可。不采用。

### 决策 4：fix/lt 不进训练特征，前端用独立的辅助特征术语表承载

技术选型：`terms.js` 新增独立的 `datasetAuxiliaryFeatures`/`datasetAuxiliaryNames`/`datasetAuxiliaryHints`，与 `datasetFeatureNames`（12 个训练特征）分开导出；训练页的 `featureList = Object.keys(datasetFeatureNames)` 因此永远不会把 fix/lt 送进训练。

备选路径：① 把 fix/lt 加进 `datasetFeatureNames` 再在训练页过滤——训练页的 featureList 直接取 Object.keys，过滤点分散、漏一处就把标签泄漏进训练；② 后端在训练入口硬编码排除——防泄漏不该只靠一处黑名单。不采用。

### 决策 5：验证集可选用 `validation_ratio=0` 表达，不加独立开关

技术选型：`validation_ratio` 字段下限放宽为 `ge=0`，0 即「不切验证分区」；策略校验要求三个比例总和为 1、train/test 必须大于 0。前端验证比例输入框 min=0，附「填 0 表示不切验证集」提示。

备选路径：① 新增 `has_validation: boolean` 开关——与比例数值二义（0.2 的验证比例 + false 算什么？），校验分支翻倍；② 允许 train 或 test 为 0——训练没训练集、评估没测试集都是无意义状态，保留禁止。不采用。

## Risks / Trade-offs

| 风险 / 权衡 | 说明 |
|---|---|
| 训练区内校准是样本内口径 | 报告显式标注 `calibration_partition=train` + limitation，前端文案「属样本内」，不冒充验证集；风险列表默认只看测试。 |
| fix 的泄漏风险 | fix 是「修复提交」的标志，与标签同源；只在展示/下载层出现，训练 featureList 与特征合同（report.features）里永不出现，由前端测试与训练契约测试双重锁定。 |
| 旧数据集没有 fix/lt | 已冻结数据集的成员行缺这两个键，前端按「未提供」渲染（null 兜底），接口 `source_json.get` 兜底，不要求重新构建。 |
| 比例总和校验 | 前端（总和必须为 100、train/test 不得为 0）与后端（policy_valid 同一规则）双处校验，前端先拦、后端兜底 422。 |

## Validation

- 单元：两分区切分（time_ordered 与 repository_holdout）、fix/lt 成员字段与元数据、策略接受 0 验证拒绝负值、无验证分区训练的校准回退（calibration_partition、train_threshold、risk_items 默认分区）。
- 集成：真实 MySQL 上构建两分区数据集（工件跳过空分区、下载 validation 404、成员辅助字段）、无验证分区训练全链路。
- 前端：比例输入与总和校验、十四维特征表（fix/lt 展示与缺失提示）、分区样本数、无验证区的训练区后段文案。
- 契约检查与追踪矩阵更新后整体跑一遍。
