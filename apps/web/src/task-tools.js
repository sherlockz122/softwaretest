export const labels = {
  queued: "排队中",
  running: "运行中",
  cancel_requested: "取消处理中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
export const active = new Set(["queued", "running", "cancel_requested"]);
// 任务类型显示成人话；原始类型只在详情页的「系统内部名称」里出现。
export const taskNames = {
  "repository.clone": "下载仓库",
  "repository.parse": "整理提交记录",
  "repository.sync": "同步新增提交",
  "repository.fix": "找出疑似修复提交",
  "repository.szz": "追查缺陷是哪次改动引入的",
  "repository.feature": "计算每个提交的统计指标",
  "dataset.build": "生成研究数据集",
  "model.train": "训练模型",
  diagnostic: "系统自检",
};
export const taskHints = {
  "repository.clone": "把公开仓库复制到本机，后面所有分析都基于这份副本。",
  "repository.parse": "读入提交记录和每次改动的文件，后面的分析都要用到这些数据。",
  "repository.sync": "仓库有新的提交时，把它们补进来；不会补更早的历史。",
  "repository.fix": "按提交说明里的关键词，找出看起来是在修 bug 的提交。",
  "repository.szz": "往回翻历史，看被修掉的代码最早是哪次提交写进来的。",
  "repository.feature": "给每个提交算一组统计指标（改了多少行、涉及多少文件、作者经验等）。",
  "dataset.build": "把公开数据按时间切成训练、验证、测试三份，供训练模型使用。",
  "model.train": "用训练区数据拟合模型，在验证区定判定门槛，最后在测试区做一次评价。",
  diagnostic: "创建一个几秒钟的空任务，用来确认后台服务能正常接收和执行任务。",
};
export const stageNames = {
  cloning: "正在下载仓库",
  parsing: "正在整理提交记录",
  detecting_fix: "正在识别疑似修复",
  tracing_szz: "正在追溯缺陷引入",
  extracting_features: "正在计算统计指标",
  building_dataset: "正在生成数据集",
  verifying_dataset: "正在核对数据",
  fitting_train: "正在训练模型",
  evaluating_test: "正在评估模型",
  verifying_artifacts: "正在校验产出文件",
  running: "正在执行",
  snapshot_ready: "快照已准备好",
  cancel_requested: "正在取消",
};
export function shortId(value) {
  return String(value || "").slice(0, 8);
}
export function taskTitle(task) {
  if (!task) return "后台任务";
  if (task.type === "model.train") {
    const plugin = task.result?.plugin;
    if (plugin === "rf") return "训练模型（随机森林）";
    if (plugin === "lr") return "训练模型（逻辑回归）";
  }
  return taskNames[task.type] || "后台任务";
}
export function fileSize(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} 字节`;
}
export function taskLine(task) {
  if (!task) return "";
  if (active.has(task.status)) {
    return stageNames[task.stage] || (task.status === "queued" ? "等待后台执行" : "");
  }
  if (task.status !== "succeeded") return "";
  const result = task.result || {};
  switch (task.type) {
    case "repository.clone":
      return result.size_bytes
        ? `仓库大小约 ${fileSize(result.size_bytes)}`
        : "";
    case "repository.parse":
      return task.total ? `已整理 ${task.processed} / ${task.total} 个提交` : "";
    case "repository.sync":
      return task.total ? `已同步 ${task.processed} / ${task.total} 个新提交` : "";
    case "repository.fix":
      return result.assessed ? `检查了 ${result.assessed} 个提交` : "";
    case "repository.szz":
      return result.assessed ? `追溯了 ${result.assessed} 个提交` : "";
    case "repository.feature":
      return result.extracted ? `计算了 ${result.extracted} 个提交的指标` : "";
    case "dataset.build":
      return result.rows ? `共 ${result.rows.toLocaleString("zh-CN")} 条样本` : "";
    case "model.train":
      return "训练完成，可以查看指标";
    case "diagnostic":
      return Number.isFinite(result.duration_seconds) ? `用时 ${result.duration_seconds} 秒` : "";
    default:
      return "";
  }
}
export function date(value) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "—";
}
export function operationKeys(uuid = () => crypto.randomUUID()) {
  const keys = new Map();
  return {
    get(scope, input) {
      const identity = JSON.stringify([scope, input]);
      if (!keys.has(identity)) keys.set(identity, uuid());
      return keys.get(identity);
    },
    done(scope, input) {
      keys.delete(JSON.stringify([scope, input]));
    },
  };
}
