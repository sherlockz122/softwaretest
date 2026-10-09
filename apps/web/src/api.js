import { reactive } from "vue";

const messages = {
  REPOSITORY_UNSAFE_URL:
    "地址格式未通过，请使用无凭据的公开 HTTPS 仓库地址（443 端口）。",
  REPOSITORY_UNSAFE_ADDRESS: "仓库地址解析到不允许访问的网络位置。",
  REPOSITORY_REDIRECT_REJECTED:
    "仓库地址发生重定向，请填写最终仓库 HTTPS 地址。",
  REPOSITORY_DNS_UNAVAILABLE: "仓库域名解析暂不可用，请稍后重试。",
  REPOSITORY_NETWORK_UNAVAILABLE:
    "仓库 HTTPS 连接或证书校验未通过，请检查连接后重试。",
  REPOSITORY_NOT_PUBLIC_GIT: "该地址未提供可匿名访问的 Git 仓库。",
  REPOSITORY_ALREADY_EXISTS: "此仓库已经添加，请在仓库目录查看已有记录。",
  REPOSITORY_STORAGE_LOW:
    "项目磁盘无法容纳预计增长，请先补充空间；任务不会转移到其他盘。",
  REPOSITORY_STORAGE_UNAVAILABLE: "仓库存储目录不可用，请检查项目启动配置。",
  REPOSITORY_NOT_FOUND: "仓库不存在，请返回仓库目录。",
  AUTH_INVALID_CREDENTIALS: "用户名或密码不正确，请重新输入。",
  AUTH_RATE_LIMITED: "登录尝试过于频繁，请稍后再试。",
  AUTH_FORBIDDEN: "当前账号没有此操作权限。",
  TASK_STATE_CONFLICT: "任务状态已变化，请刷新后再操作。",
  TASK_RETRY_EXISTS: "此任务已有后继，请查看最新任务。",
  TASK_IDEMPOTENCY_CONFLICT: "操作输入发生冲突，请确认后重新创建。",
  TASK_NOT_FOUND: "任务不存在，请返回任务列表。",
  TASK_INVALID_INPUT: "任务输入无效，请检查时长和筛选条件。",
  SYSTEM_DEPENDENCY_UNAVAILABLE: "服务暂不可用，请稍后重试。",
  NETWORK_UNAVAILABLE: "连接失败，操作结果可能尚未返回；重试将沿用同一次操作。",
  AUTH_REQUIRED: "会话已失效，请重新登录。",
  SESSION_CHANGED: "会话已变化，请重新登录。",
};
export class ApiError extends Error {
  constructor(status, code, requestId = "") {
    super(messages[code] || "请求未完成，请刷新或稍后重试。");
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}
// Credentials stay in this closure; nothing is written to browser storage.
export function createClient(fetcher = (...args) => fetch(...args)) {
  const state = reactive({ user: null, busy: false, reason: "" });
  let token = "";
  let csrf = "";
  let epoch = 0;
  let refreshing = null;
  function clear(reason = "") {
    epoch += 1;
    token = csrf = "";
    state.user = null;
    state.reason = reason;
  }
  async function raw(path, { signal, ...options } = {}) {
    const timeout = AbortSignal.timeout(10000);
    try {
      const response = await fetcher("/api/v1" + path, {
        credentials: "same-origin",
        cache: "no-store",
        ...options,
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      });
      const data =
        response.status === 204
          ? null
          : await response.json().catch(() => null);
      if (!response.ok)
        throw new ApiError(
          response.status,
          data?.code || "SYSTEM_DEPENDENCY_UNAVAILABLE",
          data?.request_id || response.headers.get("X-Request-ID") || "",
        );
      return data;
    } catch (error) {
      if (error instanceof ApiError || signal?.aborted) throw error;
      throw new ApiError(0, "NETWORK_UNAVAILABLE");
    }
  }
  function accept(data, expected) {
    if (epoch !== expected) throw new ApiError(401, "SESSION_CHANGED");
    token = data.access_token;
    csrf = data.csrf_token;
    state.user = data.user;
    state.reason = "";
  }
  async function login(username, password) {
    if (state.busy) return;
    clear();
    const expected = epoch;
    state.busy = true;
    try {
      const data = await raw("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      accept(data, expected);
    } finally {
      state.busy = false;
    }
  }
  function refresh() {
    if (refreshing) return refreshing;
    const expected = epoch;
    refreshing = (async () => {
      try {
        const data = await raw("/auth/refresh", {
          method: "POST",
          headers: { "X-CSRF-Token": csrf },
        });
        accept(data, expected);
      } catch (error) {
        if (epoch === expected && [401, 403].includes(error.status))
          clear("会话已失效，请重新登录。");
        throw error;
      } finally {
        refreshing = null;
      }
    })();
    return refreshing;
  }
  async function request(path, options = {}) {
    if (!token || state.busy) throw new ApiError(401, "AUTH_REQUIRED");
    const expected = epoch;
    const usedToken = token;
    const send = () =>
      raw(path, {
        ...options,
        headers: { ...options.headers, Authorization: "Bearer " + token },
      });
    try {
      const data = await send();
      if (epoch !== expected) throw new ApiError(401, "SESSION_CHANGED");
      return data;
    } catch (error) {
      if (error.status !== 401 || epoch !== expected || options.signal?.aborted)
        throw error;
      if (token === usedToken) await refresh();
      if (epoch !== expected) throw new ApiError(401, "SESSION_CHANGED");
      try {
        const data = await send();
        if (epoch !== expected) throw new ApiError(401, "SESSION_CHANGED");
        return data;
      } catch (retryError) {
        if (epoch === expected && retryError.status === 401)
          clear("会话已失效，请重新登录。");
        throw retryError;
      }
    }
  }
  async function logout() {
    if (state.busy) return;
    state.busy = true;
    try {
      if (refreshing) await refreshing.catch(() => {});
      const currentCsrf = csrf;
      clear();
      if (currentCsrf) {
        try {
          await raw("/auth/logout", {
            method: "POST",
            headers: { "X-CSRF-Token": currentCsrf },
          });
        } catch (error) {
          if (error.status !== 401) {
            state.reason =
              "已退出本页；服务未确认会话撤销。请恢复连接后重新登录并退出。";
            throw error;
          }
        }
      }
    } finally {
      state.busy = false;
    }
  }
  return { state, login, logout, request };
}
export const client = createClient();
