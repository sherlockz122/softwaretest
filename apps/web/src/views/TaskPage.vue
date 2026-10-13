<script setup>
import { onUnmounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { active, date, labels } from "../task-tools";
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
</script>
<template>
  <section>
    <RouterLink to="/tasks">← 返回任务中心</RouterLink>
    <div class="section-heading">
      <h1>任务详情</h1>
      <button
        :disabled="task.loading.value"
        @click="task.load(route.params.id)"
      >
        刷新任务
      </button>
    </div>
    <p v-if="task.loading.value" role="status">正在读取任务状态…</p>
    <RequestError :error="task.error.value" />
    <div v-if="task.data.value && !task.error.value" data-testid="task-detail">
      <p class="task-id">任务编号：{{ task.data.value.id }}</p>
      <span
        :class="['badge', task.data.value.status]"
        data-testid="task-status"
        >{{ labels[task.data.value.status] || task.data.value.status }}</span
      >
      <p v-if="task.data.value.health === 'stale'" role="status">
        执行心跳已过期，等待协调器恢复；请勿重复创建任务。
      </p>
      <dl class="task-facts">
        <dt>类型</dt>
        <dd>{{ task.data.value.type }}</dd>
        <dt>阶段</dt>
        <dd>{{ task.data.value.stage }}</dd>
        <dt>进度</dt>
        <dd>
          <template
            v-if="
              task.data.value.type === 'repository.clone' ||
              (task.data.value.type === 'repository.sync' &&
                task.data.value.stage === 'cloning')
            "
            >当前仓库 {{ task.data.value.processed }} 字节；完成后进度为
            100%。</template
          >
          <template v-else>
            {{ task.data.value.progress }}%（{{ task.data.value.processed }} /
            {{ task.data.value.total ?? "—" }}）
          </template>
        </dd>
        <dt>最近心跳</dt>
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
        <p>{{ task.data.value.error.message }}</p>
        <small>错误编号：{{ task.data.value.error.code }}</small
        ><small>请求编号：{{ task.data.value.error.request_id }}</small>
      </div>
      <div v-if="task.data.value.result" class="result">
        <h2>执行结果</h2>
        <p
          v-if="task.data.value.result.disposition === 'requires_review'"
          role="alert"
          class="error"
        >
          历史变化待复核；本次检查已结束，仓库 HEAD 尚未推进。
        </p>
        <RouterLink
          v-if="task.data.value.result.repository_id"
          :to="'/repositories/' + task.data.value.result.repository_id"
          >查看仓库详情</RouterLink
        >
        <pre>{{ JSON.stringify(task.data.value.result, null, 2) }}</pre>
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
