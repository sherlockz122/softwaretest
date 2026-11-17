# Tasks

立项阶段：以下为实施计划，每条任务含「验证环节」与「具体验证步骤」，实现后按步骤跑通再勾选。

## 1. 规格与依据

- [x] 1.1 追踪矩阵登记 US-31（加载已有公开数据集），docs/04、docs/08 同步导入接口与界面
  - 验证环节：文档一致性检查。
  - 具体验证步骤：docs/07 出现 US-31 行；docs/04 的导入请求体（columns 映射、两种来源通道）与错误码齐备；docs/08 的导入表单与详情页来源展示齐备。
  - 证据：docs/07 US-31 行改为「已实施」并写明资产 identity 与摘要载体；docs/04 第二十七阶段（三端点、columns/license 请求体、六个导入错误码、导入语义）；docs/08 第二十七阶段（导入表单双通道与详情页来源段）。`check_contracts.py` 612 local links PASS（2026-11-13）。

## 2. 导入归一化（后端）

- [x] 2.1 导入请求校验与列映射解析：四个身份列 + 12 个数值特征列必填（缺列列出缺口）、fix 可选、未知列名/重复映射拒绝，错误码 `DATASET_IMPORT_COLUMNS_INVALID`
  - 验证环节：数据集内核单元测试。
  - 具体验证步骤：新增 tests/test_dataset_import.py 覆盖缺身份列、缺特征列（断言错误含缺列清单）、未知列名、重复映射、fix 未映射的归一化记录生成；`pytest tests/test_dataset_import.py -q` 全绿。
  - 证据：tests/test_dataset_import.py 20/20 通过（含 5 个无效映射参数化：缺身份列、缺特征列断言含缺列清单、未知列名、重复映射、列名类型）；fix 未映射的归一化记录在 test_fix_unmapped 中带 fix_missing_reason（2026-11-13）。
- [x] 2.2 文件归一化与密封：导入文件落资产目录，生成清单与内容哈希，非法行/篡改文件按既有错误码拒绝，密封资产可被 prepare 消费
  - 验证环节：数据集内核单元测试。
  - 具体验证步骤：测试构造含坏行、篡改后的 CSV，断言 DATA_SOURCE_INVALID/DATA_SOURCE_DRIFT 类错误；合法导入的资产经 prepare 产出 members 与 metadata（含 source_kind、import_license_declaration、fix_missing_reason）。
  - 证据：坏行（label/epoch/负数特征/fix）DATASET_IMPORT_ROW_INVALID、篡改 normalized.jsonl → ASSET_FILE_DRIFT；prepare 产出 80 members 且 source_kind="user_import"、target="user_declared_import_label"、event_clock 与 limitations 断言通过（2026-11-13）。
- [x] 2.3 幂等键含源内容与列映射：同文件同映射同许可返回同一资产（asset_id 相同），不同映射或不同许可为新资产
  - 验证环节：数据集内核单元测试。
  - 具体验证步骤：test_dataset_import.py 断言两次同参导入 asset_id 相等且目录只有一个，换映射后 asset_id 不同。
  - 证据：test_idempotent_same_asset 两次同参导入 asset_id 相等且目录仅一个；test_different_mapping_new_asset 换映射后 asset_id 不同；应用测试另断言上传/路径双通道同内容同映射同许可 → 同一 asset_id（2026-11-13）。

## 3. 构建与展示链路

- [x] 3.1 摘要记录 source_kind/来源描述/缺失特征清单，详情页展示来源与映射
  - 验证环节：数据集应用测试 + 前端组件测试。
  - 具体验证步骤：应用测试断言摘要字段；DatasetsPage.test.js 增补详情页来源段渲染。
  - 证据：test_imported_csv_full_chain_build_download_train 断言 summary.source_acquisition（source_kind/import_license_declaration/import_columns/fix_column_mapped）与 target/event_clock；前端 imported dataset detail 测试断言「导入文件」「CC0-1.0」「canonical_repository ← repo」与获取时间回退文案（2026-11-13）。
- [x] 3.2 导入表单：文件/路径双通道、列映射（读文件表头自动匹配 + 下拉调整）、许可声明、缺映射禁提交
  - 验证环节：前端组件测试。
  - 具体验证步骤：DatasetsPage.test.js 增补表单校验（缺映射禁提交、自动匹配、提交体字段）全绿。
  - 证据：前端 4 个新用例：别名自动匹配 + 原始字节上传（columns/license 查询参数、body 为原始字节）、缺映射禁提交、Admin 路径通道（读列 → JSON 提交 path/license/columns）（2026-11-13，全套 65/65）。
- [x] 3.3 导入的数据集全链路：构建→切分（含验证集可选）→下载→训练
  - 验证环节：数据集/模型应用测试。
  - 具体验证步骤：真实 MySQL 上导入小 CSV 构建两分区数据集并训练成功，CSV 工件与报告断言。
  - 证据：test_imported_csv_full_chain_build_download_train（真实 MySQL）：API 导入 60 行（Viewer 403、缺特征列 422 带缺口、路径通道 403/Admin 201 且与上传通道同 asset_id）→ 两分区构建 ready、CSV 头 `...,fix,lt` 46 行、validation 下载抛 DATA_PARTITION_ABSENT → lr 训练 registered、report calibration_partition="train"、test support {"clean": 7, "buggy": 8}（2026-11-13）。

## 4. 测试与验收

- [x] 4.1 全量验收电池
  - 验证环节：契约检查 + 后端/前端全量。
  - 具体验证步骤：`scripts/check_contracts.py --export` 后 PASS；后端全量（Docker 起栈）；`cd apps/web && npx vitest --run` 全绿；`openspec validate --all --strict` 全绿。
  - 证据：check_contracts PASS（612 local links / 36 API IDs / 99 篇变更文档）；后端全量 750 passed / 20 skipped（2026-11-17，真实 MySQL/Redis 栈）；前端 65/65；openspec validate 25 passed。电池过程修掉两处与导入无关的既有问题：语义 fitted_hash 对裸 LR 估计器误用随机森林分支（study.py 按 coef/intercept 分支计算，25/25）；仓库/同步套件因 C 盘剩余空间低于存储预检下限（6 GiB）失败，测试临时目录指到 D 盘后 25/25。
