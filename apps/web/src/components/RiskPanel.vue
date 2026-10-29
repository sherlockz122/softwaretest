<script setup>
import { computed, ref, watch } from "vue";
import { client } from "../api";
import { useLatest } from "../latest";
import { date } from "../task-tools";
import { partitions } from "../terms";
import RequestError from "./RequestError.vue";
const props = defineProps({ run: { type: String, required: true } });
const page = ref(1);
const partition = ref("");
const items = useLatest((input, signal) => {
  const query = new URLSearchParams({ page: input.page, page_size: 20 });
  if (input.partition) query.set("partition", input.partition);
  return client.request(
    `/model-runs/${encodeURIComponent(input.run)}/risk-items?${query}`,
    { signal },
  );
});
watch(
  () => props.run,
  () => {
    page.value = 1;
    partition.value = "";
  },
);
watch(
  [() => props.run, page, partition],
  ([run]) => {
    if (run) items.load({ run, page: page.value, partition: partition.value });
  },
  { immediate: true },
);
const data = computed(() => items.data.value);
const total = computed(() => data.value?.total ?? 0);
const pages = computed(() => Math.max(1, Math.ceil(total.value / 20)));
const levels = { high: "高", medium: "中", low: "低" };
const percent = (value) => `${(value * 100).toFixed(1)}%`;
</script>

<template>
  <h2>每条提交的风险排序</h2>
  <p class="hint">
    按模型给出的概率从高到低排列，概率相同的按提交编号排。默认只看验证和测试两份数据；训练区是模型见过的数据，分数会偏高，要看的话需要单独选。
  </p>
  <div>
    <label for="risk-partition">看哪份数据</label>
    <select
      id="risk-partition"
      v-model="partition"
      :disabled="items.loading.value"
    >
      <option value="">验证与测试（默认）</option>
      <option value="validation">只看验证</option>
      <option value="test">只看测试</option>
      <option value="train">训练（模型见过的数据）</option>
    </select>
  </div>
  <RequestError :error="items.error.value" />
  <p v-if="data && data.in_sample" class="hint">
    当前包含训练分区的提交，这些分数是样本内的，不能当作在新数据上的表现。
  </p>
  <table v-if="data && data.items.length">
    <thead>
      <tr>
        <th>风险等级</th>
        <th>概率</th>
        <th>提交</th>
        <th>提交时间</th>
        <th>分区</th>
      </tr>
    </thead>
    <tbody>
      <tr
        v-for="item in data.items"
        :key="`${item.partition}-${item.commit_id}`"
      >
        <td>{{ levels[item.risk_level] ?? "—" }}</td>
        <td>{{ percent(item.probability) }}</td>
        <td class="sha">{{ item.commit_id.slice(0, 12) }}</td>
        <td>{{ date(item.event_epoch * 1000) }}</td>
        <td>{{ partitions[item.partition] ?? item.partition }}</td>
      </tr>
    </tbody>
  </table>
  <p v-else-if="data" class="hint">该运行在这个分区里没有可列出的提交。</p>
  <p v-if="data">
    共 {{ total }} 条，第 {{ data.page }} / {{ pages }} 页；判定门槛
    {{ data.threshold === null ? "未登记" : percent(data.threshold)
    }}（概率超过它就算「可能引入缺陷」）。
  </p>
  <div class="actions">
    <button :disabled="page <= 1 || items.loading.value" @click="page -= 1">
      上一页
    </button>
    <button :disabled="page >= pages || items.loading.value" @click="page += 1">
      下一页
    </button>
  </div>
</template>
