<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { date, labels, operationKeys } from "../task-tools";
import RequestError from "../components/RequestError.vue";
const route = useRoute();
const router = useRouter();
const status = computed(() =>
  Object.hasOwn(labels, route.query.status) ? route.query.status : "",
);
const page = computed(() =>
  Math.max(1, Number.parseInt(route.query.page, 10) || 1),
);
const list = useLatest((query, signal) =>
  client.request("/tasks?" + query, { signal }),
);
const duration = ref(3);
const creating = ref(false);
const failure = ref(null);
const keys = operationKeys();
const canCreate = computed(() =>
  ["Admin", "Member"].includes(client.state.user?.role),
);
const query = computed(() =>
  new URLSearchParams({
    ...(status.value ? { status: status.value } : {}),
    page: page.value,
    page_size: 10,
  }).toString(),
);
watch(query, (value) => list.load(value), { immediate: true });
const poll = setInterval(() => {
  if (!document.hidden && !list.loading.value && !client.state.busy)
    list.load(query.value);
}, 3000);
onUnmounted(() => clearInterval(poll));
function filter(value) {
  router.replace({ path: "/tasks", query: value ? { status: value } : {} });
}
function paginate(value) {
  router.replace({
    path: "/tasks",
    query: { status: status.value || undefined, page: value },
  });
}
async function create() {
  if (creating.value) return;
  creating.value = true;
  failure.value = null;
  const value = Number(duration.value);
  if (!Number.isInteger(value) || value < 0 || value > 30) {
    failure.value = { message: "诊断时长必须是 0～30 秒的整数。" };
    creating.value = false;
    return;
  }
  const key = keys.get("create", value);
  try {
    const result = await client.request("/tasks/diagnostic", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify({ duration_seconds: value }),
    });
    keys.done("create", value);
    await router.push("/tasks/" + result.task_id);
  } catch (error) {
    failure.value = error;
  } finally {
    creating.value = false;
  }
}
</script>
<template>
  <section>
    <div class="section-heading">
      <div>
        <p class="eyebrow">执行与恢复</p>
        <h1>任务中心</h1>
      </div>
      <button :disabled="list.loading.value" @click="list.load(query)">
        刷新列表
      </button>
    </div>
    <form v-if="canCreate" class="create-task" @submit.prevent="create">
      <div>
        <label for="duration">诊断时长（秒）</label
        ><input
          id="duration"
          v-model="duration"
          type="number"
          min="0"
          max="30"
          step="1"
          required
          :disabled="creating"
        />
      </div>
      <button class="primary" :disabled="creating">
        {{ creating ? "提交中…" : "创建诊断任务" }}</button
      ><small>0～30 秒，用于检查任务执行链路。</small>
    </form>
    <p v-else class="hint">Viewer 仅可查看任务，创建需要 Member 或 Admin。</p>
    <RequestError :error="failure" />
    <div class="filters">
      <label for="status">状态筛选</label
      ><select
        id="status"
        :value="status"
        @change="filter($event.target.value)"
      >
        <option value="">全部状态</option>
        <option v-for="(label, key) in labels" :key="key" :value="key">
          {{ label }}
        </option>
      </select>
    </div>
    <p v-if="list.loading.value" role="status">正在加载任务…</p>
    <RequestError :error="list.error.value" />
    <div v-if="list.data.value && !list.error.value">
      <p v-if="!list.data.value.items.length" class="empty">
        暂无符合条件的任务。{{
          canCreate
            ? "可以创建诊断任务或更改筛选。"
            : "请更改筛选或联系任务创建者。"
        }}
      </p>
      <div v-else class="table-scroll">
        <table>
          <caption class="sr-only">
            任务列表
          </caption>
          <thead>
            <tr>
              <th>任务 / 类型</th>
              <th>状态</th>
              <th>进度</th>
              <th>最近心跳</th>
              <th>创建时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="task in list.data.value.items" :key="task.id">
              <td>
                <RouterLink :to="'/tasks/' + task.id">{{ task.id }}</RouterLink
                ><small>{{ task.type }}</small>
              </td>
              <td>
                <span :class="['badge', task.status]">{{
                  labels[task.status] || task.status
                }}</span
                ><small v-if="task.health === 'stale'"
                  >心跳过期，等待恢复</small
                >
              </td>
              <td>{{ task.progress }}%</td>
              <td>{{ date(task.heartbeat_at) }}</td>
              <td>{{ date(task.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pagination">
        <button
          :disabled="page === 1 || list.loading.value"
          @click="paginate(page - 1)"
        >
          上一页</button
        ><span>第 {{ page }} 页 · 共 {{ list.data.value.total }} 项</span
        ><button
          :disabled="page * 10 >= list.data.value.total || list.loading.value"
          @click="paginate(page + 1)"
        >
          下一页
        </button>
      </div>
    </div>
  </section>
</template>
