# Tasks

## 1. 规格与依据

- [x] 1.1 提案、规格、设计四件套入库，追踪矩阵登记 US-11 与 US-08 行对应的新增测试
  - 证据：本目录 proposal.md/specs/design.md；docs/07 第二十五阶段条目；docs/04、docs/08 第二十五阶段章节。
- [x] 1.2 术语表补 12 个数据集特征的人话命名、形式命名与取值口径说明
  - 证据：apps/web/src/terms.js 的 datasetFeatureNames/datasetFeatureHints/featureForms/featureFormHints；前端 55/55 通过。

## 2. 训练特征选择（后端）

- [x] 2.1 `policy.feature_plan()`：特征子集与形式的校验归一化，缺省按插件补全，错误码 `MODEL_FEATURE_INVALID`
  - 证据：packages/modeling/policy.py；tests/test_model_kernel.py 28/28 含 feature_plan 归一化与 8 组拒绝用例。
- [x] 2.2 训练请求体新增 features/forms 两个可选字段，创建时归一化并入幂等回放比较
  - 证据：apps/api/models.py、packages/modeling/service.py create/root_request；tests/test_model_application.py::test_feature_choice_contract_and_replay 通过（重放相等、不同特征 409）。
- [x] 2.3 训练子进程按所选特征与形式构造矩阵：log1p 逐值变换；standardize 只在训练分区拟合，零方差按 1 处理
  - 证据：packages/modeling/train.py feature_matrix；test_model_kernel.py::test_feature_matrix_transforms_use_train_stats_only（手算对照、零方差 scale=1）。
- [x] 2.4 逻辑回归移除内置 StandardScaler 管线，缺省配置数值与旧行为一致
  - 证据：test_model_kernel.py::test_feature_matrix_default_matches_legacy_pipeline（全标准化 == StandardScaler().fit_transform 各分区）。
- [x] 2.5 工件与报告记录特征、形式与标准化统计，root_request 兼容旧请求
  - 证据：train.py bundle/report 字段；test_feature_choice_contract_and_replay 断言 report.features/forms/standardization/request；真实栈 run d7a74705 报告 `{"la":"log1p","age":"standardize"}`、standardization `{"age":{"mean":28.5,"scale":16.163229875244614}}`。
- [x] 2.6 错误文案入库（api.js），policy.VERSION 升为 v4
  - 证据：apps/web/src/api.js MODEL_FEATURE_INVALID；packages/modeling/policy.py VERSION="research-lr-rf-v4"。

## 3. 数据集二维表（后端+前端）

- [x] 3.1 数据集成员查询新增可选分区过滤与标签过滤参数，缺省不过滤，分页按过滤后集合计算
  - 证据：packages/mining/dataset_service.py detail(filter_partition/filter_label)+member_total；tests/test_dataset_application.py::test_detail_member_filters_and_counts 通过（40/28/20 与筛选一致性）。
- [x] 3.2 数据集详情页「数据样例」升级为二维表：仓库/提交/分区/标签 + 12 特征列，人话表头、悬停代码、数值格式化
  - 证据：apps/web/src/views/DatasetPage.vue；DatasetsPage.test.js::ready dataset renders a 2D feature table with toggles and filters 通过（人话表头、213/0.42 格式化）。
- [x] 3.3 特征列显隐开关与分区/标签过滤控件，窄屏横向滚动保持可用
  - 证据：同上测试覆盖列显隐往返与 filter_partition/filter_label 查询参数；.table-scroll 横向滚动沿用既有样式。

## 4. 训练页与模型详情（前端）

- [x] 4.1 训练页特征勾选与形式选择，默认全选+插件缺省形式，切换插件重置形式、保留勾选
  - 证据：apps/web/src/views/ModelsPage.vue；ModelsPage.test.js::feature choice defaults all checked, plugin switch resets forms only 通过。
- [x] 4.2 全不勾选时禁提交并给提示；提交体带 features/forms
  - 证据：ModelsPage.test.js::empty feature choice disables submit and subset travels in body 通过（禁用+提示+body 断言）。
- [x] 4.3 模型详情页展示训练契约（所选特征与形式，人话命名）
  - 证据：apps/web/src/views/ModelPage.vue contract 段；ModelsPage.test.js::model detail shows the training feature contract 通过。

## 5. 测试

- [x] 5.1 后端单元：feature_plan 边界与归一化；变换矩阵手算对照（log1p/z-score/零方差/缺省一致性）
  - 证据：tests/test_model_kernel.py 28/28（2026-10-30）。
- [x] 5.2 后端集成：真实 MySQL 带特征选择训练成功、产物与报告契约字段、幂等回放与冲突
  - 证据：tests/test_model_application.py 16/16（2026-10-30，真实 MySQL）。
- [x] 5.3 前端：数据集页二维表/显隐/过滤；训练页默认态/禁提交/切换插件；模型详情契约
  - 证据：apps/web vitest 55/55（2026-10-30）。
- [x] 5.4 全量验证电池：契约检查、后端测试、前端测试、迁移一致性
  - 证据：scripts/check_contracts.py PASS（含更新后的 bootstrap-openapi.json）；tests/ 全量后端电池见 5.2 与 2026-10-30 全量运行结果；前端 55/55；迁移一致性由 test_dataset_application.py::test_empty_migration_and_whole_downgrade_guard（alembic check）通过。

## 6. 验收与文档

- [x] 6.1 真实栈上跑一次带特征选择的训练并截图留证据，勾选本清单
  - 证据：tests/test_model_runtime.py::test_feature_choice_training_on_live_stack 在 wang 栈（MySQL/Redis/Worker 全 health）通过（2026-10-30，18.7s）。带特征选择 run d7a74705（lr，features=["la","age"]，forms={"la":"log1p","age":"standardize"}，standardization 训练分区统计）与缺省 run 5a028f95（rf，12 特征全 raw）均 succeeded，MySQL 查询留档；空特征选择 422 MODEL_FEATURE_INVALID 未产生任务。
- [x] 6.2 更新 API 规格、交互规格与追踪矩阵，跑通 OpenSpec 结构与契约检查
  - 证据：docs/04、docs/08、docs/07 第二十五阶段条目；docs/01 §8.1；check_contracts PASS（608 本地链接、36 API ID、81 change 文档风格）。
