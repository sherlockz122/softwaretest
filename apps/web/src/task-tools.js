export const labels = {
  queued: "排队中",
  running: "运行中",
  cancel_requested: "取消处理中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
export const active = new Set(["queued", "running", "cancel_requested"]);
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
