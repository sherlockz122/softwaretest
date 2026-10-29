// 界面上的术语统一在这里翻译成人话；原始代码值仍保留在 Map 的键上，
// 查不到时原样显示，方便对照后端日志。
export const parseStatus = {
  pending: "还没有整理提交记录",
  queued: "排队等待整理",
  parsing: "正在整理提交记录",
  parsed: "提交记录已整理",
  failed: "整理失败",
  cancelled: "整理已取消",
};
export const syncStatus = {
  pending: "还没有同步过新提交",
  queued: "排队等待同步",
  syncing: "正在同步新提交",
  synced: "已同步",
  requires_review: "历史发生变化，需要人工确认",
  failed: "同步失败",
  cancelled: "同步已取消",
};
export const syncRelation = {
  pending: "等待生成快照",
  initial: "第一次同步",
  unchanged: "没有新提交",
  fast_forward: "新增了提交",
  requires_review: "历史被改写或换了默认分支",
};
export const contentStatus = {
  parsed: "内容已读取",
  binary: "二进制文件，内容略过",
  blob_limit: "文件太大，内容略过",
  diff_limit: "改动太大，只保留一部分",
  commit_diff_limit: "提交太大，超出处理上限",
  encoding: "编码不支持，内容略过",
  submodule: "子模块，内容略过",
  ambiguous_patch: "改动格式无法解析，已略过",
  merge_skipped: "合并提交，不展开改动",
  message_encoding: "提交说明编码异常",
};
export const changeType = {
  A: "新增文件",
  M: "修改",
  D: "删除",
  R: "重命名",
  C: "复制",
  T: "权限或链接变化",
};
export const fixStatus = {
  pending: "还没做过疑似修复识别",
  queued: "排队等待识别",
  detecting: "正在识别",
  detected: "识别完成",
  failed: "识别失败",
  cancelled: "识别已取消",
};
export const fixReview = {
  unreviewed: "还没有人工复核",
  confirmed: "已确认是修复",
  rejected: "已否定这条候选",
};
export const fixEvidenceType = {
  keyword: "提交说明里出现了修复关键词",
  bug_issue: "关联并关闭了一个缺陷单",
  issue_reference: "提到了一个问题单",
  exclusion: "按规则排除了这条提交",
  unavailable: "内容不可用，无法判断",
};
export const fixExclusion = {
  revert: "回滚提交",
  negated_fix: "说明里明确说不是修复",
  spelling: "只改拼写或错别字",
  documentation_only: "只改了文档",
  documentation_context: "文档相关，缺少文件信息",
  message_encoding: "提交说明编码异常",
};
export const ruleVersions = {
  "native-pydriller-v1": "Git 原生读取（第 1 版）",
  "native-record-binding-v1": "Git 原始记录核对（第 1 版）",
  "fix-evidence-v1": "按提交说明关键词判断（第 1 版）",
  "kamei14-dg-v1": "Kamei 14 项指标（第 1 版）",
  "kamei14-dg-message-v2": "Kamei 14 项指标（第 2 版，含提交说明）",
};
export const szzAlgorithms = {
  "baseline-szz-v1": "基础 SZZ（第 1 版）",
};
export const confidenceNames = {
  high: "高",
  medium: "中",
  low: "低",
  reviewed: "已人工复核",
};
export const szzStatus = {
  pending: "还没做过缺陷引入追溯",
  queued: "排队等待追溯",
  tracing: "正在追溯",
  traced: "追溯完成",
  failed: "追溯失败",
  cancelled: "追溯已取消",
};
export const szzResult = {
  unknown: "无法确定",
  partial: "只追溯了一部分",
  traced: "已追溯",
  no_traceable_lines: "没有可追溯的旧代码",
  not_candidate: "这条不是疑似修复",
};
export const szzReason = {
  evidence_after_cutoff: "修复证据出现在观察截止之后",
  frozen_non_candidate: "这条提交不在疑似修复名单里",
  merge_parent: "合并提交不单独追溯",
  root_parent: "仓库最早的提交没有历史可追",
  new_file: "文件是这次新增的，没有旧代码可追",
  documentation: "只涉及文档，已跳过",
  unsupported_mode: "这次改动的类型暂不支持追溯",
  parent_blob_conflict: "与上一版本对不上，无法定位",
  blob_limit: "文件太大，超过处理上限",
  filter_unavailable: "当时的过滤条件不可用",
  line_limit: "行数超过处理上限",
  no_deleted_code: "这次改动没有删掉代码行",
  blame_output_limit: "追溯输出超过处理上限",
  blame_invalid: "追溯结果无法解析",
  outside_parsed_history: "来源提交不在已整理的提交记录里",
  event_time_inversion: "来源提交的时间反而晚于这次修复",
  not_parent_ancestor: "来源提交不在这条提交的历史里",
};
export const featureStatus = {
  pending: "还没计算",
  queued: "排队等待计算",
  extracting: "正在计算",
  extracted: "计算完成",
  failed: "计算失败",
  cancelled: "计算已取消",
};
export const featureNames = {
  NS: "改动的子系统数",
  ND: "改动的目录数",
  NF: "改动的文件数",
  Entropy: "改动分散程度",
  LA: "新增行数",
  LD: "删除行数",
  LT: "改动前文件总行数",
  FIX: "是否疑似修复",
  NDEV: "改过这些文件的人数",
  AGE: "这些文件距上次改动（天）",
  NUC: "这些文件被改过几次",
  EXP: "作者此前提交次数",
  REXP: "作者近期活跃程度",
  SEXP: "作者在这个子系统的提交次数",
};
export const featureReason = {
  current_content_unsupported: "这次改动的内容不完整",
  history_incomplete: "只整理了部分历史",
  ancestor_time_inversion: "历史提交的时间顺序异常",
  ancestor_content_unsupported: "历史提交的内容不完整",
  zero_churn: "没有代码行变化",
  first_file_change: "这些文件以前没有改动记录",
  candidate_fix_default: "新提交默认按「不是修复」处理",
  fix_unavailable: "没有可用的修复识别结果",
  fix_not_yet_available: "修复证据当时还没有产生",
  message_purpose: "根据提交说明判断",
  message_encoding: "提交说明编码异常，无法判断",
  message_unverified: "提交说明无法核对",
};
export const partitions = { train: "训练", validation: "验证", test: "测试" };
export const metricNames = {
  precision: "精确率",
  recall: "召回率",
  f1: "F1",
  pr_auc: "PR-AUC",
  roc_auc: "ROC-AUC",
  mcc: "MCC",
  brier: "Brier",
};
export const metrics = {
  precision: "精确率（判为有缺陷的提交里，真有缺陷的比例）",
  recall: "召回率（真有缺陷的提交里，被找出来的比例）",
  f1: "F1（精确率与召回率的综合）",
  pr_auc: "PR-AUC（各阈值下精确率与召回率的整体表现）",
  roc_auc: "ROC-AUC（模型区分两类提交的整体能力）",
  mcc: "MCC（综合四类判断的平衡指标）",
  brier: "Brier（概率预测误差，越小越好）",
};
export const effortNames = {
  pofb20: "PofB@20",
  ifa: "IFA",
  popt: "Popt",
};
export const effortMetrics = {
  pofb20: "PofB@20（只检查前 20% 改动量时，能找到多少比例的缺陷提交）",
  ifa: "IFA（按风险排序后，第几个提交才发现第一个缺陷）",
  popt: "Popt（排序接近最理想排序的程度）",
};
export function label(map, key) {
  return map[key] ?? key;
}
