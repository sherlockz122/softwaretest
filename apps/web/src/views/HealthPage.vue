<script setup>
import { onMounted } from "vue";
import { useLatest } from "../latest";
import { ApiError } from "../api";
const check = useLatest(async (_, signal) => {
  const response = await fetch("/api/v1/health/ready", {
    signal: AbortSignal.any([signal, AbortSignal.timeout(8000)]),
  });
  if (!response.ok)
    throw new ApiError(
      response.status,
      "SYSTEM_DEPENDENCY_UNAVAILABLE",
      response.headers.get("X-Request-ID") || "",
    );
  return response.json();
});
onMounted(() => check.load());
</script>
<template>
  <section>
    <p class="eyebrow">工程基础</p>
    <h1>开发环境连接检查</h1>
    <p>检查 API、MySQL、Redis 和数据库迁移。任务执行状态可在任务中心查看。</p>
    <div role="status">
      <p v-if="check.loading.value">正在检查连接…</p>
      <p v-else-if="check.error.value">
        基础服务暂不可用，请确认服务已启动。请求编号：{{
          check.error.value.requestId || "—"
        }}
      </p>
      <p v-else-if="check.data.value?.status === 'ok'">基础服务连接正常</p>
    </div>
    <button :disabled="check.loading.value" @click="check.load()">
      重新检查
    </button>
  </section>
</template>
