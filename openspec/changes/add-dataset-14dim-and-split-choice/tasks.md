# Tasks

每条任务含「验证环节」与「具体验证步骤」，完成验证后才勾选。

## 1. 规格与依据

- [x] 1.1 提案、规格、设计四件套入库（design 含技术选型与备选路径，tasks 含验证环节与验证步骤）
  - 验证环节：文档结构检查。
  - 具体验证步骤：`openspec validate add-dataset-14dim-and-split-choice --strict` 通过。
  - 证据：本目录 proposal.md/specs/design.md；openspec 校验通过。
- [ ] 1.2 追踪矩阵登记 US-29（十四维行级数据）与 US-30（比例自选、验证集可选），docs/04、docs/08 同步
  - 验证环节：文档一致性检查。
  - 具体验证步骤：docs/07 出现 US-29/US-30 行与第二十六阶段条目；docs/04 的构建请求含 validation_ratio=0 语义；docs/08 的三比例输入与 fix/lt 列说明。
  - 证据：待补（docs 更新后勾选）。

## 2. 十四维行级数据（后端）

- [x] 2.1 成员行带 fix（布尔，来自上游修复元数据）与 lt（None + `upstream_source_omits_lt`），元数据带 auxiliary_features/lt_source/limitations
  - 验证环节：数据集内核单元测试。
  - 具体验证步骤：`cd /d/softwaretest && PYTHONPATH=/d/softwaretest .venv/Scripts/python.exe -X utf8 -m pytest tests/test_research_dataset.py -q` 全绿，含 fix/lt 与缺失原因的断言。
  - 证据：tests/test_research_dataset.py::test_fix_and_lt_auxiliary_features_per_member；49/49 通过。
- [x] 2.2 批次/详情/CSV 工件/成员一致性校验带 fix/lt；工件跳过空分区；下载缺分区返回 DATA_PARTITION_ABSENT
  - 验证环节：数据集内核单元测试 + 应用测试。
  - 具体验证步骤：test_research_dataset.py 全绿；test_dataset_application.py（真实 MySQL，Docker 起栈）断言 CSV 头 `...,fix,lt`、两分区成员无 validation、下载 validation 抛 DATA_PARTITION_ABSENT。
  - 证据：内核 49/49 通过；应用测试待 Docker 起栈后补跑再勾。
- [x] 2.3 API `validation_ratio` 下限放宽为 ge=0，契约重新导出
  - 验证环节：契约检查。
  - 具体验证步骤：`scripts/check_contracts.py --export` 重新生成 bootstrap-openapi.json 后 `scripts/check_contracts.py` PASS。
  - 证据：apps/api/datasets.py ge=0；--export 待重跑。

## 3. 验证集可选（后端训练链路）

- [x] 3.1 两分区切分（time_ordered 与 repository_holdout），policy_valid 接受 0 验证、拒绝负值与总和不为 1
  - 验证环节：数据集内核单元测试。
  - 具体验证步骤：test_research_dataset.py::test_validation_partition_optional_two_way、::test_validation_optional_repository_holdout、::test_policy_accepts_zero_validation_but_not_negatives 全绿。
  - 证据：49/49 通过。
- [x] 3.2 无验证分区训练：训练区内按时间切前后两段校准定门槛，报告 calibration_partition=train、train_threshold 指标、limitations 标注，risk_items 默认只看 test
  - 验证环节：模型内核单元测试 + 应用测试。
  - 具体验证步骤：test_model_kernel.py::test_train_internal_calibration_when_validation_absent（rng=17）全绿；test_model_application.py::test_training_without_validation_partition（0.75/0/0.25 数据集→训练）断言 calibration_partition=="train"、train_threshold 指标、test 支撑集、risk_items partitions ["test"]，待 Docker 起栈后补跑。
  - 证据：内核 49/49 通过；应用测试待补。
- [x] 3.3 audit_dataset 按实际存在的分区过滤；study.py 适配训练函数新返回结构
  - 验证环节：语义/审计相关内核测试回归。
  - 具体验证步骤：packages/modeling/data.py 用 `output["files"]` 的键过滤；语义测试套件回归（既有 lr fitted_hash 失败为 HEAD 既有问题，用 worktree 对照确认非本批引入）。
  - 证据：`git worktree add` HEAD 对照复现既有失败；本批改动未新增失败。

## 4. 前端：比例自选、十四维表、口径文案

- [x] 4.1 数据集列表：训练/验证/测试比例输入（验证可为 0），总和必须为 100、train/test 不得为 0 时禁提交，提交体带三个比例
  - 验证环节：前端组件测试。
  - 具体验证步骤：DatasetsPage.test.js::create sends user-chosen ratios and allows zero validation、::ratios that do not sum to 100 disable submission with a hint 全绿。
  - 证据：前端 25/25 通过。
- [x] 4.2 数据集详情：十四维特征表（fix/lt 人话展示与「未提供」兜底），「修复标志只展示、不参与模型计算」提示，「原始记录数/去重后样本数」口径，下载按钮按分区数显示
  - 验证环节：前端组件测试。
  - 具体验证步骤：DatasetsPage.test.js::two-way dataset hides validation rows and shows auxiliary features、::ready research shows release limitations and both-class partition support、::ready dataset renders a 2D feature table with toggles and filters 全绿（14 个列开关、fix:false/lt:null 渲染）。
  - 证据：前端 25/25 通过。
- [x] 4.3 模型训练页：分区样本数与「没有验证区」说明、「验证集不是必须」文案、训练与历史风险列表职责边界文案、风险面板「没有验证区的训练默认只看测试」提示
  - 验证环节：前端组件测试。
  - 具体验证步骤：ModelsPage.test.js::chosen dataset shows partition sample counts and two-way note、::training page separates itself from the historical risk list、::model without validation shows train-internal threshold summary 全绿。
  - 证据：前端 25/25 通过。

## 5. 测试与验收

- [x] 5.1 数据集/模型内核单元测试全量
  - 验证环节：全量内核电池。
  - 具体验证步骤：`pytest tests/test_research_dataset.py tests/test_model_kernel.py -q` 49/49。
  - 证据：49 passed（含本批 +5 数据集、+1 模型测试）。
- [x] 5.2 应用测试（真实 MySQL）：两分区构建与下载 404、无验证分区训练、成员辅助字段
  - 验证环节：Docker 起栈后 test_dataset_application.py + test_model_application.py。
  - 具体验证步骤：启动 Docker Desktop → `docker compose --profile full up -d --build` → 上述两个文件全绿。
  - 证据：32/32 通过（2026-11-13，真实 MySQL）。含 test_optional_validation_build_and_member_auxiliaries（CSV 头 fix,lt、下载 validation 抛 DATA_PARTITION_ABSENT）与 test_training_without_validation_partition（calibration_partition=="train"、train_threshold、risk_items 只看 test）。首轮暴露审计重建缺 fix/lt 的漂移误报，已修复并复跑通过。
- [ ] 5.3 契约导出与全量验收电池
  - 验证环节：契约检查 + 前端全量。
  - 具体验证步骤：`scripts/check_contracts.py --export` 后 `scripts/check_contracts.py` PASS（已跑，PASS）；`cd apps/web && npx vitest --run` 全绿（已跑，25/25 相关文件）；`openspec validate --all --strict` 全绿（已跑，25 passed）。全量前端与后端电池随 5.2 复跑一起收口。
