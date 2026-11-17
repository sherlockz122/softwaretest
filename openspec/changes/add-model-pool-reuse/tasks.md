# Tasks

立项阶段：以下为实施计划，每条任务含「验证环节」与「具体验证步骤」，实现后按步骤跑通再勾选。

## 1. 规格与依据

- [x] 1.1 追踪矩阵登记 US-32（模型池复用），docs/04、docs/08 同步池接口与界面
  - 验证环节：文档一致性检查。
  - 具体验证步骤：docs/07 出现 US-32 行；docs/04 的池注册/列表/复用请求与错误码（MODEL_POOL_CONTRACT_MISMATCH）齐备；docs/08 的训练页复用选择与模型详情复用来源齐备。
  - 证据：docs/07 US-32 行改为「已实施」并写明 model_pool 行与 runtime/model-pool 密封目录载体；docs/04 第二十八阶段（model_pool 表、三个池端点、reuse_from/reuse_policy 语义、九个池错误码）；docs/08 第二十八阶段（三选一复用表单与详情来源段）。`check_contracts.py --export` 后 614 local links PASS（2026-11-17）。

## 2. 模型池（后端）

- [x] 2.1 池注册：预置基线随版本注册 + 用户把成功运行加入池；工件哈希与特征合同校验，坏工件/缺合同拒绝
  - 验证环节：模型内核单元测试。
  - 具体验证步骤：新增 tests/test_model_pool.py 覆盖注册校验、重复注册幂等、工件篡改拒绝；`pytest tests/test_model_pool.py -q` 全绿。
  - 证据：tests/test_model_pool.py 7/7：四元组逐维匹配、密封/加载回环（coef 一致）、篡改与摘要不符拒绝（MODEL_POOL_ARTIFACT_DRIFT）、运行工件提取（缺 model 拒绝 MODEL_POOL_SOURCE_INVALID）、内置资产挑选、预置训练产出 LR/RF 估计器；应用测试另覆盖 ensure_presets 幂等（第二次注册 0 条）与同一运行重复入池返回原条目（2026-11-17）。
- [x] 2.2 四元组匹配（插件 + 特征集 + 形式 + 超参）：显式 reuse_from 契约不匹配报 MODEL_POOL_CONTRACT_MISMATCH；reuse_policy=auto 零命中回退从零训练并提示
  - 验证环节：模型内核单元测试。
  - 具体验证步骤：测试覆盖精确命中、多命中取最新、零命中回退、显式指定不匹配报错。
  - 证据：内核 7 用例断言四元组逐维不匹配；应用测试断言显式契约不一致（params c=2.0）422 MODEL_POOL_CONTRACT_MISMATCH、不存在的条目 MODEL_POOL_NOT_FOUND、auto 零命中回退（报告 limitations 含 pool_auto_match_none_trained_from_scratch 且无 reused_from）、auto 命中取最新条目（2026-11-17）。
- [x] 2.3 复用训练路径：跳过估计器拟合，标准化统计、校准器与门槛在新数据集上重做，报告 reused_from 与 limitations
  - 验证环节：模型应用测试（真实 MySQL）。
  - 具体验证步骤：test_model_application.py 增补用例：从零训练 → 入池 → 新数据集复用，断言工件估计器一致、校准/门槛为新建、报告字段正确、风险列表同构。
  - 证据：test_pool_add_and_reuse_full_chain（真实 MySQL）：从零训练 → 入池（Viewer 403、重复入池幂等）→ 新数据集复用训练：model.skops 估计器 coef 与来源一致、calibration_rows 与来源运行不同（校准在新数据重做）、reused_from.pool_entry_id/source_kind 正确、limitations 含 estimator_parameters_reused_from_pool_calibration_threshold_refit、风险列表可读；移除权限（非本人 403、Admin 可移除）与移除后 MODEL_POOL_NOT_FOUND（2026-11-17）。

## 3. 前端

- [x] 3.1 训练页复用选择：池模型列表（契约摘要）或自动匹配开关，零命中回退提示
  - 验证环节：前端组件测试。
  - 具体验证步骤：ModelsPage.test.js 增补：选择池条目提交体带 reuse_from、自动匹配回退提示渲染。
  - 证据：新增 2 用例：池条目按当前契约实时过滤（3 条只留 1 条匹配、选项来源文案）且提交体带 reuse_from；空池下 entry 模式显示「池中没有匹配条目」并禁用提交，auto 模式提交体带 reuse_policy 且无 reuse_from（2026-11-17，全套 68/68）。
- [x] 3.2 模型详情展示复用来源（池条目、来源运行、训练契约）
  - 验证环节：前端组件测试。
  - 具体验证步骤：ModelsPage.test.js 增补复用运行详情的 reused_from 段渲染。
  - 证据：新增 1 用例：详情渲染「模型参数复用自模型池」、来源类型「训练运行」、「查看来源运行」链接与自动匹配零命中说明「已从零训练」（2026-11-17）。

## 4. 测试与验收

- [x] 4.1 全量验收电池
  - 验证环节：契约检查 + 后端/前端全量。
  - 具体验证步骤：`scripts/check_contracts.py --export` 后 PASS；后端全量（Docker 起栈）；`cd apps/web && npx vitest --run` 全绿；`openspec validate --all --strict` 全绿。
  - 证据：check_contracts PASS（614 local links / 36 API IDs / 99 篇变更文档）；后端全量 758 passed / 20 skipped（2026-11-17，真实 MySQL/Redis 栈，测试临时目录指 D 盘）；其中 test_connections 因演示主库迁移版本停在 0012 与 SCHEMA_HEAD 不一致而失败，对主库执行 `alembic upgrade head` 升到 0013 后单独复跑通过；前端 68/68；openspec validate 25 passed。
