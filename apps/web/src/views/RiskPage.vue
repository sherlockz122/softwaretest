<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { active, labels, operationKeys } from "../task-tools";
import RequestError from "../components/RequestError.vue";
const route = useRoute();
const router = useRouter();
const taskId = ref(String(route.query.task || ""));
const repos = useLatest((signal) =>
  client.request("/repositories?page=1&page_size=100", { signal }),
);
const models = useLatest((signal) =>
  client.request("/models?page=1&page_size=100", { signal }),
);
repos.load(null);
models.load(null);
const repositoryId = ref("");
const modelRunId = ref("");
const baseRef = ref("");
const windowDays = ref(90);
const maxCandidates = ref(30);
const creating = ref(false);
const error = ref(null);
const keys = operationKeys();
const page = ref(1);
const level = ref("");
const oversized = ref("");
const levelNames = { high: "高", medium: "中", low: "低" };
const queue = useLatest((key, signal) => {
  const params = new URLSearchParams({
    page: String(page.value),
    page_size: "20",
  });
  if (level.value) params.set("level", level.value);
  if (oversized.value !== "") params.set("oversized", oversized.value);
  return client.request(
    `/prediction-tasks/${encodeURIComponent(taskId.value)}/results?${params}`,
    { signal },
  );
});
const canCreate = computed(() =>
  ["Member", "Admin"].includes(client.state.user?.role),
);
const registered = computed(() =>
  (models.data.value?.items || []).filter((row) => row.registered),
);
watch(
  [taskId, page, level, oversized],
  () => {
    if (taskId.value) queue.load("queue");
    else queue.clear();
  },
  { immediate: true },
);
watch(
  () => route.query.task,
  (value) => {
    taskId.value = String(value || "");
    page.value = 1;
    level.value = "";
    oversized.value = "";
  },
);
const timer = setInterval(() => {
  if (
    !document.hidden &&
    !queue.loading.value &&
    !client.state.busy &&
    taskId.value &&
    (!queue.data.value || active.has(queue.data.value.status))
  )
    queue.load("queue");
}, 3000);
onUnmounted(() => clearInterval(timer));
const explanation = useLatest((id, signal) =>
  client.request(`/predictions/${encodeURIComponent(id)}/explanation`, {
    signal,
  }),
);
const number = (value) => (value == null ? "—" : Number(value).toFixed(4));
const density = (value) => (value == null ? "—" : Number(value).toFixed(6));
function factorText(factor) {
  return (
    (factor.feature || "").toUpperCase() +
    (factor.direction === "increase" ? "↑" : "↓")
  );
}
function resetFilters() {
  page.value = 1;
  queue.load("queue");
}
async function createBatch() {
  if (creating.value) return;
  creating.value = true;
  error.value = null;
  const body = {
    repository_id: repositoryId.value,
    model_run_id: modelRunId.value,
    base_ref: baseRef.value.trim(),
    window_days: Number(windowDays.value),
    max_candidates: Number(maxCandidates.value),
  };
  try {
    const ack = await client.request("/prediction-tasks", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": keys.get("batch", body),
      },
      body: JSON.stringify(body),
    });
    keys.done("batch", body);
    await router.replace({ path: "/risk", query: { task: ack.prediction_task_id } });
  } catch (failure) {
    error.value = failure;
  } finally {
    creating.value = false;
  }
}
</script>
<template>
  <section>
    <div class="section-heading">
      <h1>候选变更风险队列</h1>
      <button
        :disabled="!taskId || queue.loading.value"
        @click="queue.load('queue')"
      >
        刷新队列
      </button>
    </div>
    <p>
      队列按概率降序、提交时间升序排列，用于安排审查顺序，不是自动合并或拒绝结论。批量预测最多覆盖 90 天、500 条候选。
    </p>
    <p>
      这里的预测结果只看「已注册模型」；训练在
      <RouterLink to="/models">模型研究</RouterLink>
      完成，训练和预测是分开的两件事：训练走任务队列，预测直接读已注册模型的结果，训练排队或中断不会影响这里已有模型的预测。
    </p>
    <RequestError
      :error="error || queue.error.value || repos.error.value || models.error.value || explanation.error.value"
    />
    <form v-if="canCreate && !taskId" @submit.prevent="createBatch">
      <label
        >仓库<select v-model="repositoryId" required :disabled="creating">
          <option value="">请选择仓库</option>
          <option
            v-for="repo in repos.data.value?.items || []"
            :key="repo.id"
            :value="repo.id"
          >
            {{ repo.url }} · {{ repo.default_branch || "—" }} · {{
              repo.parse_status || "未解析"
            }}
          </option>
        </select></label
      >
      <label
        >已注册模型<select v-model="modelRunId" required :disabled="creating">
          <option value="">请选择模型</option>
          <option
            v-for="row in registered"
            :key="row.id"
            :value="row.id"
          >
            {{ row.plugin.toUpperCase() }} · 种子{{ row.seed }} · {{
              row.id.slice(0, 8)
            }}
          </option>
        </select></label
      >
      <label
        >Base 引用<input
          v-model="baseRef"
          maxlength="255"
          placeholder="例如 main 或 v1.0"
          required
          :disabled="creating"
      /></label>
      <label
        >时间窗口（天）<input
          v-model.number="windowDays"
          type="number"
          min="1"
          max="90"
          step="1"
          required
          :disabled="creating"
      /></label>
      <label
        >候选数量上限<input
          v-model.number="maxCandidates"
          type="number"
          min="1"
          max="500"
          step="1"
          required
          :disabled="creating"
      /></label>
      <button
        :disabled="creating || !repositoryId || !modelRunId || !baseRef.trim()"
      >
        {{ creating ? "正在创建…" : "创建批量预测任务" }}
      </button>
      <p v-if="!registered.length && !models.loading.value">
        尚无已注册模型，请先在模型研究完成训练。
      </p>
    </form>
    <p v-else-if="!taskId">当前账号可查看队列；创建批量预测需要Member或Admin权限。</p>
    <template v-if="queue.data.value">
      <p class="task-id">
        预测任务：{{ queue.data.value.prediction_task_id }} · 任务状态：{{
          labels[queue.data.value.status] || queue.data.value.status
        }}
      </p>
      <p>
        Base {{ queue.data.value.base_ref }} · {{
          queue.data.value.candidate_kind === "batch"
            ? "批量窗口候选"
            : "单一候选"
        }}
        · 特征版本：{{ queue.data.value.feature_version || "—" }} · 判定阈值：{{
          number(queue.data.value.threshold)
        }}
      </p>
      <div class="actions">
        <RouterLink :to="`/tasks/${queue.data.value.task_id}`"
          >查看任务</RouterLink
        >
        <label
          >等级<select v-model="level" @change="resetFilters">
            <option value="">全部等级</option>
            <option value="high">高</option>
            <option value="medium">中</option>
            <option value="low">低</option>
          </select></label
        >
        <label
          >Effort<select v-model="oversized" @change="resetFilters">
            <option value="">全部</option>
            <option value="true">仅超大（&gt;500）</option>
            <option value="false">仅非超大</option>
          </select></label
        >
      </div>
      <p v-if="queue.loading.value" role="status">正在加载…</p>
      <p v-if="queue.data.value.empty">
        任务已完成，队列中没有候选记录。
      </p>
      <div
        v-else-if="queue.data.value.items.length"
        class="table-scroll"
        role="region"
        aria-label="风险队列"
        tabindex="0"
      >
        <table>
          <thead>
            <tr>
              <th>SHA</th>
              <th>概率</th>
              <th>等级</th>
              <th>Effort</th>
              <th>风险密度</th>
              <th>超大</th>
              <th>主要影响因素</th>
              <th>解释</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in queue.data.value.items" :key="item.prediction_id">
              <td :title="item.candidate_sha">{{
                item.candidate_sha.slice(0, 12) + "…"
              }}</td>
              <td>{{ number(item.probability) }}</td>
              <td>{{ levelNames[item.risk_level] || item.risk_level }}</td>
              <td>{{ item.effort }}</td>
              <td>{{ density(item.risk_density) }}</td>
              <td>{{ item.oversized ? "超大" : "—" }}</td>
              <td>
                <span
                  v-for="factor in item.top_factors"
                  :key="factor.feature"
                  class="factor"
                  >{{ factorText(factor) }}</span
                >
              </td>
              <td>
                <button @click="explanation.load(item.prediction_id)">
                  解释
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else>队列还没有候选记录。</p>
      <div class="actions">
        <button :disabled="page <= 1 || queue.loading.value" @click="page--">
          上一页</button
        ><span>第 {{ queue.data.value.page }} 页 · 共
          {{ queue.data.value.total }} 条</span
        ><button
          :disabled="
            page * 20 >= queue.data.value.total || queue.loading.value
          "
          @click="page++"
        >
          下一页
        </button>
      </div>
    </template>
    <template v-if="explanation.data.value">
      <h2>解释</h2>
      <p>
        SHA {{ explanation.data.value.candidate_sha }} · 概率
        {{ number(explanation.data.value.probability) }} · 方法：{{
          explanation.data.value.method
        }}
      </p>
      <div
        class="table-scroll"
        role="region"
        aria-label="因素解释"
        tabindex="0"
      >
        <table>
          <thead>
            <tr>
              <th>因素</th>
              <th>取值</th>
              <th>贡献</th>
              <th>方向</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="factor in explanation.data.value.factors"
              :key="factor.feature"
            >
              <td>{{ (factor.feature || "").toUpperCase() }}</td>
              <td>{{ factor.value }}</td>
              <td>{{ number(factor.contribution) }}</td>
              <td>
                {{ factor.direction === "increase" ? "推高风险" : "降低风险" }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="explanation.data.value.limitations.length">
        {{ explanation.data.value.limitations.join(" ") }}
      </p>
    </template>
  </section>
</template>
<style scoped>
label {
  display: block;
  min-width: 0;
  margin: 12px 0;
}
input,
select {
  width: 100%;
  max-width: 100%;
}
table {
  table-layout: fixed;
}
td,
th {
  overflow-wrap: anywhere;
}
.factor {
  display: inline-block;
  margin-right: 8px;
}
.actions {
  display: flex;
  gap: 12px;
  margin-top: 16px;
  align-items: center;
  flex-wrap: wrap;
}
.actions label {
  margin: 0;
}
</style>
