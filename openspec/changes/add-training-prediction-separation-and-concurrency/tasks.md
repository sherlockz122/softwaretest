# Tasks

立项阶段：以下为实施计划，每条任务含「验证环节」与「具体验证步骤」，实现后按步骤跑通再勾选。

## 1. 规格与依据

- [x] 1.1 追踪矩阵登记 US-33（训练预测分离）与 US-34（多用户并发），docs/04、docs/08 同步边界说明与职责文案
  - 证据：docs/07 US-33/US-34 行改为「已实施」并写明载体与第三十阶段条目；docs/04 第三十阶段（分离契约三句 + 并发三规则 + 错误码）；docs/08 第三十阶段（风险队列职责文案与互链）；check_contracts 597 links PASS（2026-11-18）。
  - 验证环节：文档一致性检查。
  - 具体验证步骤：docs/07 出现 US-33/US-34 行；docs/04 写明「训练走任务队列、预测在线读已注册模型」与并发三规则；docs/08 的训练页与风险列表职责文案成体系。

## 2. 分离契约（后端）

- [x] 2.1 训练请求一律 202 落任务队列、预测同步读已注册模型：预测不依赖队列状态、不获取任务租约
  - 证据：test_stuck_training_does_not_block_registered_predictions（真实 MySQL）：模型 B 训练任务租约过期卡死时，已注册模型 A 的 risk_items 照常返回；风险列表请求前后 async_task 与 task_outbox 计数逐行不变（不产生队列消息）；训练 202 与风险列表同步读工件在既有应用测试中覆盖（2026-11-18）。
  - 验证环节：模型/任务应用测试（真实 MySQL/Redis）。
  - 具体验证步骤：test_model_application.py 增补：训练任务卡死（租约过期不续）时 risk_items 照常返回已注册模型结果；预测请求不产生队列消息（broker 计数不变）。
- [x] 2.2 注册发布是训练→预测的唯一数据通道：未注册模型任何状态都不可预测
  - 证据：未注册运行 risk_items 409 MODEL_NOT_READY（test_risk_api_rejects_unregistered_drift_and_invalid_params）；已注册运行的任务不可重试（test_registered_run_has_no_retry_channel：retry 409 TASK_STATE_CONFLICT）；注册发布在同一事务（publish 事务化，既有代码路径）（2026-11-18）。
  - 验证环节：模型应用测试。
  - 具体验证步骤：训练成功但未注册的模型，risk_items 拒绝或不可见；注册后立即可预测。
- [x] 2.3 训练中间状态与预测工件零共享：预测读取密封工件，不读运行状态表
  - 证据：risk_items 路径经 artifact() 逐项校验 DB 摘要、磁盘摘要与密封清单（manifest.json 等值比较），不读进度/阶段/心跳等过程状态；test_registered_run_has_no_retry_channel 断言任务状态查询前后 risk_items 结果逐位一致（2026-11-18）。
  - 验证环节：代码审查 + 应用测试。
  - 具体验证步骤：审查 risk_items 路径无任务租约/运行状态依赖；测试断言预测结果在训练任务状态变更前后不变。

## 3. 并发契约（后端）

- [x] 3.1 幂等键并发去重：同一键并发重复提交只产生一个任务
  - 证据：test_concurrent_same_idempotency_key_creates_one_task：双线程并发提交同一幂等键，返回同一 task_id、async_task 表恰一行，且该任务可正常执行注册（2026-11-18）。
  - 验证环节：任务应用测试。
  - 具体验证步骤：线程并发（或两客户端同时）提交同幂等键构建请求，断言任务数 = 1、返回同一任务 id。
- [x] 3.2 两用户同时训练互不干扰，产物各自正确；跨用户操作 403
  - 证据：test_two_users_concurrent_training_isolated：两位 Member 在各自数据集上并发训练（双线程），两个运行均注册且 model.skops 工件哈希互不相同；第二位 Member 取消第一位任务 AUTH_FORBIDDEN 403 且任务状态保持 succeeded（2026-11-18）。
  - 验证环节：模型应用测试。
  - 具体验证步骤：两个用户并发发起训练（不同数据集），断言两个运行产物哈希与报告各归各主；A 操作 B 的任务/模型 403。
- [x] 3.3 构建与训练排队：worker 串行消费下任务状态一致，租约 reconcile 恢复
  - 证据：test_queue_serial_consumption_and_reconcile_recovery：连续提交三个训练，task_outbox 按 next_attempt_at 与提交顺序一致，按序 claim+run 三个运行全部注册；执行中崩溃与租约过期的 reconcile 重试恢复由 test_model_application 既有用例覆盖（test_subprocess_fault_retry_same_root_and_old_fence、test_expired_token_and_auto_retry_cannot_publish）（2026-11-18）。
  - 验证环节：任务应用测试。
  - 具体验证步骤：连续提交多个构建/训练，断言队列顺序执行、状态转移合法；模拟执行中崩溃（after_batch 抛错）后 reconcile 产生重试任务并最终成功。

## 4. 前端职责边界

- [x] 4.1 训练页与历史风险列表职责文案成体系（训练走队列、预测看已注册模型），路由互链不混用
  - 证据：风险队列页新增「预测只看已注册模型 + 训练在模型研究 + 训练排队或中断不影响预测」文案与 /models 互链，新增 RiskPage 测试断言文案与链接（7/7）；训练页既有「训练和预测是分开的两件事」与 /risk 互链由 ModelsPage 测试覆盖（2026-11-18，前端全套 73/73）。
  - 验证环节：前端组件测试。
  - 具体验证步骤：ModelsPage.test.js / RiskPanel 相关测试断言职责文案与 /risk、/models 路由互链。

## 5. 测试与验收

- [x] 5.1 全量验收电池
  - 验证环节：契约检查 + 后端/前端全量。
  - 具体验证步骤：`scripts/check_contracts.py --export` 后 PASS；后端全量（Docker 起栈）；`cd apps/web && npx vitest --run` 全绿；`openspec validate --all --strict` 全绿。
  - 证据：check_contracts PASS（597 local links / 36 API IDs / 99 篇变更文档）；后端全量 790 tests（768 passed / 20 skipped / 2 failed，2026-11-19）：两个失败为 feature-quality 路由装饰器错位（修复参考路由插入时遗留，本批暴露），修复后 test_feature_service/test_feature_v2 两用例单独复跑通过，fix-recommendations 与分离并发套件 6 用例复跑全绿；前端 73/73；openspec validate 25 passed。
