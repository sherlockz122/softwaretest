<script setup>
import { computed, ref } from "vue";
import { client } from "../api";
import { operationKeys } from "../task-tools";
import RequestError from "./RequestError.vue";
const props = defineProps({ task: { type: Object, required: true } });
const emit = defineEmits(["changed", "successor"]);
const busy = ref(false);
const failure = ref(null);
const notice = ref("");
const keys = operationKeys();
const allowed = computed(() =>
  ["Admin", "Member"].includes(client.state.user?.role),
);
const canCancel = computed(
  () => allowed.value && ["queued", "running"].includes(props.task.status),
);
const canRetry = computed(
  () => allowed.value && ["failed", "cancelled"].includes(props.task.status),
);
async function action(kind) {
  if (busy.value) return;
  busy.value = true;
  failure.value = null;
  notice.value = "";
  const id = props.task.id;
  const key = keys.get(kind, id);
  try {
    const result = await client.request(`/tasks/${id}/${kind}`, {
      method: "POST",
      headers: kind === "retry" ? { "Idempotency-Key": key } : {},
    });
    keys.done(kind, id);
    if (kind === "retry") emit("successor", result.task_id);
    else {
      notice.value = result
        ? "已请求取消，等待 Worker 检查点。"
        : "任务已取消。";
      emit("changed");
    }
  } catch (error) {
    failure.value = error;
    if (error.status === 409) emit("changed");
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <div class="actions">
    <button v-if="canCancel" :disabled="busy" @click="action('cancel')">
      {{ busy ? "提交中…" : "取消任务" }}
    </button>
    <button v-if="canRetry" :disabled="busy" @click="action('retry')">
      {{ busy ? "提交中…" : "重试任务" }}
    </button>
    <small v-if="!allowed"
      >Viewer 可以查看任务，发起操作需要 Member 或 Admin。</small
    >
    <small v-else-if="!canCancel && !canRetry">当前状态没有可用操作。</small>
    <small v-else>Member 只能操作自己的任务，Admin 可操作全部任务。</small>
    <p v-if="notice" role="status">{{ notice }}</p>
    <RequestError :error="failure" />
  </div>
</template>
