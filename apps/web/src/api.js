import { reactive } from "vue";

const messages = {
  SEMANTIC_TASK_BINDING_CONFLICT:
    "固定资产或校验程序版本发生变化，请核对后创建新的校验任务。",
  SEMANTIC_TASK_INVALID_INPUT:
    "校验任务不接收文件或额外内容，请刷新资产页面后重试。",
  SEMANTIC_ASSET_DRIFT: "语义资产内容与固定摘要不符，请联系管理员核对来源。",
  SEMANTIC_ASSET_UNAVAILABLE: "语义资产暂不可用，请核对部署和存储后重试。",
  SEMANTIC_ASSET_NOT_FOUND: "语义资产不存在，请返回资产列表。",
  SEMANTIC_ASSET_DEADLINE: "校验达到时限，请核对存储和资产后重试。",
  MODEL_DATASET_NOT_READY: "数据集尚未发布，请等待其完成后训练。",
  MODEL_DATASET_INCOMPATIBLE:
    "数据集用途或特征不兼容，请选择已发布的研究十二指标数据。",
  MODEL_DATA_DRIFT: "固定数据或分区摘要不符，已停止训练。",
  MODEL_INVALID_INPUT: "模型参数超出支持范围，请核对算法、种子和参数。",
  MODEL_CALIBRATION_UNUSABLE:
    "验证区不能形成两类校准和阈值子集，请核对预声明数据分区。",
  MODEL_PURPOSE_INCOMPATIBLE: "研究模型尚不具备候选产品发布资格。",
  MODEL_ARTIFACT_DRIFT: "模型工件校验失败，已停止下载。",
  MODEL_NOT_READY: "研究模型尚未完成，暂不能下载。",
  MODEL_NOT_FOUND: "模型运行不存在，请返回列表。",
  MODEL_MEMORY_LIMIT: "训练达到内存限制，请核对模型规模后重试。",
  MODEL_DEADLINE: "训练达到当前时限，请核对模型规模后重试。",
  MODEL_NOT_CONVERGED: "模型未收敛，请调整训练参数并创建新的研究运行。",
  DATA_SOURCE_DRIFT: "数据源版本发生变化，请刷新来源后重新创建。",
  DATA_PURPOSE_UNSUPPORTED: "当前数据尚不支持该用途，请使用公开二分类研究。",
  DATA_SPLIT_UNUSABLE: "数据不足以形成完整分区，请调整策略或选择其它数据。",
  DATA_SPLIT_SINGLE_CLASS: "某分区只有一类标签，请核对数据和预声明分区策略。",
  DATA_DUPLICATE_CONFLICT: "同一提交存在冲突记录，请先核验来源。",
  DATA_NORMALIZATION_DRIFT: "派生数据与原始来源不一致，请重新核验数据。",
  DATA_SOURCE_OR_STORAGE_INVALID:
    "数据源或工件暂不可用，请核对来源或存储后重试。",
  DATA_ARTIFACT_DRIFT: "导出工件校验失败，已停止下载。",
  DATA_LICENSE_UNVERIFIED: "数据许可尚未核实，不能用于当前研究导出。",
  DATA_NOT_READY: "数据集尚未完成，暂不能导出。",
  DATA_NOT_FOUND: "数据集不存在，请返回列表。",
  DATA_BUILD_DEADLINE: "构建达到当前时限，请检查任务和规模后重试。",
  SZZ_STATE_CONFLICT: "请完成当前 Fix 识别，并等待仓库活动任务结束。",
  SZZ_SOURCE_CONFLICT: "Fix 轮次与接受快照不一致，请选择当前可用轮次。",
  SZZ_VERSION_CONFLICT: "追溯策略、Git 或接受快照发生变化，请检查本轮记录。",
  SZZ_CUTOFF_FUTURE: "观察截止时间不能晚于当前时间。",
  SZZ_RUN_NOT_FOUND: "此轮 SZZ 不存在，请刷新轮次列表。",
  FIX_STATE_CONFLICT: "请先完成解析，并等待当前任务结束后再运行 Fix 识别。",
  FIX_VERSION_CONFLICT: "分析规则或基线发生变化，请检查本轮记录。",
  FIX_PLAN_CONFLICT: "分析计划与已保存数据不一致，已停止写入。",
  FIX_REVIEW_CONFLICT: "复核记录已被更新，请刷新后重新确认。",
  FIX_RULE_UNSUPPORTED: "此规则版本尚未支持，请使用当前可用规则。",
  FIX_RUN_NOT_FOUND: "此轮分析不存在，请刷新分析列表。",
  REPOSITORY_SYNC_STATE_CONFLICT:
    "仓库需先完成解析；已有同步、失败或待复核窗口时，请查看当前任务。",
  REPOSITORY_SYNC_BASE_CONFLICT:
    "同步基线发生变化，已停止写入，请检查当前窗口。",
  REPOSITORY_PARSE_STATE_CONFLICT:
    "仓库尚未克隆完成或已有解析窗口，请刷新详情或在当前任务重试。",
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
  async function raw(path, { signal, responseType = "json", ...options } = {}) {
    const timeout = AbortSignal.timeout(
      responseType === "blob" ? 60000 : 10000,
    );
    try {
      const response = await fetcher("/api/v1" + path, {
        credentials: "same-origin",
        cache: "no-store",
        ...options,
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      });
      const data =
        response.ok && responseType === "blob"
          ? await response.blob()
          : response.status === 204
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
