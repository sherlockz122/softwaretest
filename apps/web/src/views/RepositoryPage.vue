<script setup>
import { onUnmounted, watch } from "vue";
import { useRoute } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { active } from "../task-tools";
import RequestError from "../components/RequestError.vue";
const route = useRoute();
const repo = useLatest((id, signal) =>
  client.request("/repositories/" + encodeURIComponent(id), { signal }),
);
const labels = {
  queued: "排队中",
  cloning: "克隆中",
  cloned: "已克隆",
  failed: "失败",
  cancelled: "已取消",
};
watch(
  () => route.params.id,
  (id) => repo.load(id),
  { immediate: true },
);
const poll = setInterval(() => {
  if (
    !document.hidden &&
    !repo.loading.value &&
    !client.state.busy &&
    (!repo.data.value || active.has(repo.data.value.task.status))
  )
    repo.load(route.params.id);
}, 2000);
onUnmounted(() => clearInterval(poll));
</script>
<template>
  <section>
    <RouterLink to="/repositories">← 返回仓库目录</RouterLink>
    <div class="section-heading">
      <h1>仓库详情</h1>
      <button
        :disabled="repo.loading.value"
        @click="repo.load(route.params.id)"
      >
        刷新详情
      </button>
    </div>
    <RequestError :error="repo.error.value" />
    <p v-if="repo.loading.value" role="status">正在读取仓库…</p>
    <div
      v-if="repo.data.value && !repo.error.value"
      data-testid="repository-detail"
    >
      <p class="task-id">{{ repo.data.value.url }}</p>
      <p data-testid="repository-status">
        {{ labels[repo.data.value.status] || repo.data.value.status }}
      </p>
      <dl class="task-facts">
        <dt>默认分支</dt>
        <dd>{{ repo.data.value.default_branch ?? "—" }}</dd>
        <dt>HEAD</dt>
        <dd class="task-id" data-testid="repository-head">
          {{ repo.data.value.head_sha ?? "—" }}
        </dd>
        <dt>仓库大小</dt>
        <dd>{{ (repo.data.value.size_bytes / 1024 / 1024).toFixed(2) }} MiB</dd>
      </dl>
      <p>克隆阶段保存 Git 数据；当前尚未导入提交和文件变更记录。</p>
      <p>
        <RouterLink :to="'/tasks/' + repo.data.value.task.id"
          >查看克隆任务</RouterLink
        >，可查看进度、取消或重试。
      </p>
      <p v-if="repo.data.value.task.health === 'stale'" role="status">
        执行心跳已过期，等待协调器恢复。
      </p>
      <div v-if="repo.data.value.task.error" role="alert" class="error">
        <p>{{ repo.data.value.task.error.message }}</p>
        <small>错误编号：{{ repo.data.value.task.error.code }}</small>
        <small>请求编号：{{ repo.data.value.task.error.request_id }}</small>
      </div>
    </div>
  </section>
</template>
