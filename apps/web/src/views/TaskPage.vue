<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import {
  active,
  date,
  labels,
  shortId,
  stageNames,
  taskHints,
  taskTitle,
} from "../task-tools";
import RequestError from "../components/RequestError.vue";
import TaskActions from "../components/TaskActions.vue";
const route = useRoute();
const router = useRouter();
const task = useLatest((id, signal) =>
  client.request("/tasks/" + encodeURIComponent(id), { signal }),
);
watch(
  () => route.params.id,
  (id) => task.load(id),
  { immediate: true },
);
const poll = setInterval(() => {
  if (
    !document.hidden &&
    !task.loading.value &&
    !client.state.busy &&
    (!task.data.value || active.has(task.data.value.status))
  )
    task.load(route.params.id);
}, 2000);
onUnmounted(() => clearInterval(poll));
const copied = ref(false);
async function copyId() {
  try {
    await navigator.clipboard.writeText(task.data.value.id);
    copied.value = true;
    setTimeout(() => (copied.value = false), 1500);
  } catch {
    copied.value = false;
  }
}
const stage = computed(() => {
  const value = task.data.value;
  if (!value) return "";
  if (stageNames[value.stage]) return stageNames[value.stage];
  return active.has(value.status) ? "尚未开始" : "已结束";
});
const stageKnown = computed(() => Boolean(stageNames[task.data.value?.stage]));
function number(value) {
  return typeof value === "number" ? value.toLocaleString("zh-CN") : "—";
}
const progressText = computed(() => {
  const value = task.data.value;
  if (!value) return "";
  if (
    value.type === "repository.clone" ||
    (value.type === "repository.sync" && value.stage === "cloning")
  )
    return value.progress >= 100
      ? `已下载完成，仓库共 ${number(value.processed)} 字节。`
      : `已下载 ${number(value.processed)} 字节，进度按下载量计算。`;
  if (value.total == null) return `${value.progress}%`;
  return `${value.progress}%（${number(value.processed)} / ${number(value.total)}）`;
});
function elapsed(value) {
  const start = Date.parse(value?.started_at);
  const end = Date.parse(value?.finished_at);
  return start && end && end >= start
    ? Math.round((end - start) / 1000)
    : Number.NaN;
}
const result = computed(() => task.data.value?.result || null);
const resultFacts = computed(() => {
  const value = result.value;
  if (!value) return [];
  const facts = [];
  if (value.assessed) facts.push(`检查了 ${value.assessed} 个提交`);
  if (value.extracted) facts.push(`计算了 ${value.extracted} 个提交的指标`);
  if (value.rows) facts.push(`共 ${value.rows.toLocaleString("zh-CN")} 条样本`);
  if (value.default_branch) facts.push(`默认分支 ${value.default_branch}`);
  if (value.head_sha) facts.push(`对应仓库版本 ${shortId(value.head_sha)}`);
  const seconds = Number.isFinite(value.duration_seconds)
    ? value.duration_seconds
    : elapsed(task.data.value);
  if (Number.isFinite(seconds) && seconds >= 0) facts.push(`用时 ${seconds} 秒`);
  return facts;
});
</script>
<template>
  <section>
    <p class="task-links">
      <RouterLink to="/tasks">← 返回任务中心</RouterLink>
      <RouterLink
        v-if="task.data.value?.dataset_id"
        :to="`/datasets/${task.data.value.dataset_id}`"
        >查看用的数据集</RouterLink
      >
      <RouterLink
        v-if="task.data.value?.model_run_id"
        :to="`/model-runs/${task.data.value.model_run_id}`"
        >查看训练结果</RouterLink
      >
    </p>
    <div class="section-heading">
      <h1>{{ task.data.value ? taskTitle(task.data.value) : "任务详情" }}</h1>
      <button
        :disabled="task.loading.value"
        @click="task.load(route.params.id)"
      >
        刷新任务
      </button>
    </div>
    <p v-if="taskHints[task.data.value?.type]" class="hint">
      {{ taskHints[task.data.value.type] }}
    </p>
    <p v-if="task.loading.value" role="status">正在读取任务状态…</p>
    <RequestError :error="task.error.value" />
    <div v-if="task.data.value && !task.error.value" data-testid="task-detail">
      <p class="task-id">
        编号 {{ shortId(task.data.value.id) }}
        <button class="link-button" @click="copyId">
          {{ copied ? "已复制" : "复制完整编号" }}
        </button>
      </p>
      <span
        :class="['badge', task.data.value.status]"
        data-testid="task-status"
        >{{ labels[task.data.value.status] || task.data.value.status }}</span
      >
      <p v-if="task.data.value.health === 'stale'" role="status">
        这个任务很久没有更新了，后台会自动尝试恢复；这段时间请不要重复创建同一个任务。
      </p>
      <dl class="task-facts">
        <dt>任务类型</dt>
        <dd>
          {{ taskTitle(task.data.value)
          }}<small>（系统内部名称：{{ task.data.value.type }}）</small>
        </dd>
        <dt>当前阶段</dt>
        <dd>
          {{ stage
          }}<small v-if="stageKnown"
            >（系统内部名称：{{ task.data.value.stage }}）</small
          >
        </dd>
        <dt>进度</dt>
        <dd>{{ progressText }}</dd>
        <dt>最近更新</dt>
        <dd>{{ date(task.data.value.heartbeat_at) }}</dd>
        <dt>创建时间</dt>
        <dd>{{ date(task.data.value.created_at) }}</dd>
        <dt>开始时间</dt>
        <dd>{{ date(task.data.value.started_at) }}</dd>
        <dt>结束时间</dt>
        <dd>{{ date(task.data.value.finished_at) }}</dd>
      </dl>
      <progress
        :value="task.data.value.progress"
        max="100"
        aria-label="任务进度"
      ></progress>
      <p v-if="task.data.value.retry_of">
        原任务：<RouterLink :to="'/tasks/' + task.data.value.retry_of">{{
          task.data.value.retry_of
        }}</RouterLink>
      </p>
      <div v-if="task.data.value.error" role="alert" class="error">
        <p><strong>这次任务没有完成。</strong>{{ task.data.value.error.message }}</p>
        <small
          >如果反复失败，把下面两行发给开发可以定位问题：错误编号
          {{ task.data.value.error.code }}，请求编号
          {{ task.data.value.error.request_id }}</small
        >
      </div>
      <div v-if="result" class="result">
        <h2>执行结果</h2>
        <p v-if="result.disposition === 'requires_review'" role="alert" class="error">
          仓库的历史发生了变化，需要人工确认；这次检查已经结束，仓库的最新提交暂时没有更新。
        </p>
        <ul v-if="resultFacts.length" class="result-facts">
          <li v-for="fact in resultFacts" :key="fact">{{ fact }}</li>
        </ul>
        <RouterLink
          v-if="result.repository_id"
          :to="'/repositories/' + result.repository_id"
          >查看仓库详情</RouterLink
        >
        <details>
          <summary>查看原始结果数据</summary>
          <pre>{{ JSON.stringify(result, null, 2) }}</pre>
        </details>
      </div>
      <TaskActions
        :key="task.data.value.id"
        :task="task.data.value"
        @changed="task.load(route.params.id)"
        @successor="(id) => router.push('/tasks/' + id)"
      />
    </div>
  </section>
</template>
