# Tasks

## 1. 查询与排序
- [x] 1.1 实现相似修复检索：以目标提交或候选改动的触及文件为起点，取同仓库的修复提交，按文件重叠数、特征距离、证据等级排序，输出提交编号、信息摘要、触及文件、证据等级和相似理由
  - 证据：packages/mining/fix_recommendations.py（候选口径与 SZZ 冻结一致、目标自身排除、晚于目标的修复包含在内、排序键 = 文件重叠降序 → 路径重合降序 → 特征距离升序 → 证据把握降序 → sha 升序）；test_fix_recommendations.py 5 用例（真实 MySQL）断言排序稳定与字段齐备（2026-11-18）。
- [x] 1.2 落实查询保护：候选扫描行数和返回条数上限、超限时在响应中说明总数与上限、空历史与低相似度的明确状态
  - 证据：limit 默认 20、上限 100（越界 FIX_RECOMMEND_LIMIT_INVALID），响应含 total/limit 说明匹配总数与返回上限；无修复历史返回明确空状态（items=[]、total=0），无相同文件且特征较远时相似理由写「相似度低」；触及文件展示截断到 5 个（FILES_SHOWN）。test_recommend_limit_errors_and_empty_history 与组件空态/低相似断言（2026-11-18）。
- [x] 1.3 用手算夹具验证排序：构造文件重叠、特征距离和证据等级各不相同的修复提交，断言排序结果与相似理由稳定一致
  - 证据：test_recommend_uses_feature_distance_when_feature_run_exists 手插特征轮次夹具（目标与第一个候选特征距离 0、与其余候选距离大）断言首个参考恰为该候选；test_recommend_ranks_by_overlap_and_evidence 断言文件重叠降序与证据等级字段；vector_distance 单元断言共享维度均方根距离与缺失回退（2026-11-18）。

## 2. 接口与权限
- [x] 2.1 新增修复参考只读接口，请求携带仓库与提交编号，响应携带修复证据规则版本号；提交不存在、未登录分别返回明确错误码
  - 证据：A37 GET /repositories/{id}/fix-recommendations?sha=&limit=，响应含 rule_version；test_recommend_api_readonly_and_requires_login：未登录 401、Viewer 200、未知提交 404 FIX_RECOMMEND_COMMIT_NOT_FOUND、limit 越界 422（2026-11-18）。
- [x] 2.2 通过接口契约漂移检查，验证只读角色可以查询且接口不触发写入
  - 证据：check_contracts --export 后 PASS（597 local links / 36 API IDs）；接口实现只有 SELECT（无任务创建、无表写入），Viewer 查询 200 断言在 2.1 同用例（2026-11-18）。

## 3. 页面
- [x] 3.1 仓库详情页增加提交修复参考面板：参考列表、相似理由、证据等级和详情跳转；组件测试覆盖正常、空态和错误态
  - 证据：FixRecommendationsPanel.vue 接入 RepositoryPage（选中提交即出面板，点击参考编号跳转该提交文件视图）；组件测试 3 用例（列表与相似理由、空态、错误态）全绿，前端全套 71/71（2026-11-18）。
- [x] 3.2 候选预测结果页增加同类修复面板：与预测使用同一个检索对象，页面说明参考不是根因结论；组件测试覆盖展示与空态
  - 证据：ReviewPage 预测结果下方接入同一面板，检索 sha 与预测候选引用一致（mode=ref 时用 candidateRef），点击参考编号回填候选引用框；面板文案声明「修法示例，不是根因结论」；组件测试含空态断言，ReviewPage 原有 6 用例保持全绿（2026-11-18）。
- [ ] 3.3 浏览器实测两个面板：在真实仓库上核对列表、相似理由、跳转和空态提示

## 4. 端到端与文档
- [ ] 4.1 端到端走通：演示环境里对一个真实仓库完成缺陷来源追溯后查看修复参考，展示顺序与手算排序一致
- [x] 4.2 同步文档：产品需求文档新增用户故事、追踪矩阵登记、API 与数据库规格补充接口清单、界面与交互规格更新页面清单；契约检查脚本通过
  - 证据：docs/07 US-24 两行更新为「已实施」（A37 载体与阶段行）并新增第二十九阶段条目；docs/04 第二十九阶段（A37 端点、排序键、错误码、scope）；docs/08 第二十九阶段（双面板与免责声明）；OpenAPI 重导出，check_contracts PASS（2026-11-18）。
