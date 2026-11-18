<script setup>
import { computed, watch } from "vue";
import { client } from "../api";
import { useLatest } from "../latest";
import { shortId } from "../task-tools";
import RequestError from "./RequestError.vue";
const props = defineProps({
  repositoryId: { type: String, required: true },
  sha: { type: String, required: true },
});
const emit = defineEmits(["select"]);
const items = useLatest((input, signal) => {
  const query = new URLSearchParams({ sha: input.sha, limit: "20" });
  return client.request(
    `/repositories/${encodeURIComponent(input.repositoryId)}/fix-recommendations?${query}`,
    { signal },
  );
});
watch(
  () => [props.repositoryId, props.sha],
  ([repositoryId, sha]) => {
    if (repositoryId && sha) items.load({ repositoryId, sha });
  },
  { immediate: true },
);
const data = computed(() => items.data.value);
const levelNames = { high: "高", medium: "中", low: "低", null: "—" };
const similarityText = (similarity) => {
  const parts = [];
  if (similarity.file_overlap > 0)
    parts.push(`触及了 ${similarity.file_overlap} 个相同文件`);
  if (similarity.file_path_overlap)
    parts.push(`路径重合 ${similarity.file_path_overlap} 个`);
  if (similarity.feature_distance != null)
    parts.push(`改动特征距离 ${similarity.feature_distance.toFixed(2)}`);
  if (parts.length === 0) return "与目标改动相似度低（没有相同文件，特征也较远）";
  return parts.join("，");
};
</script>
<template>
  <section class="recommendations">
    <h2>同类修复参考</h2>
    <p class="hint">
      这些是同一仓库里真实存在过的修复提交，按「触及文件重叠、改动特征距离、证据把握」排序，包括时间上晚于目标提交的修复。它们是同类缺陷的修法示例，不是对当前缺陷的根因结论。
    </p>
    <RequestError :error="items.error.value" />
    <p v-if="data && !data.items.length" class="hint">
      该仓库尚无修复历史，或没有与目标提交相近的修复案例。
    </p>
    <p v-if="data" class="hint">
      目标提交：{{ shortId(data.target.sha) }}
    </p>
    <p v-if="data && data.items.length" class="hint">
      共 {{ data.total }} 条匹配，最多显示 {{ data.limit }} 条；检索规则版本
      {{ data.rule_version }}。
    </p>
    <table v-if="data && data.items.length">
      <thead>
        <tr>
          <th>修复提交</th>
          <th>提交信息摘要</th>
          <th>触及文件</th>
          <th>证据把握</th>
          <th>相似理由</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in data.items" :key="item.fix_sha">
          <td>
            <button type="button" class="link" @click="emit('select', item.fix_sha)">
              {{ shortId(item.fix_sha) }}
            </button>
          </td>
          <td>{{ item.message_summary }}</td>
          <td>
            {{ item.touched_files.join("、") || "—" }}
          </td>
          <td>{{ levelNames[item.evidence_level ?? "null"] }}</td>
          <td>{{ similarityText(item.similarity) }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
<style scoped>
.recommendations table {
  margin-top: 8px;
}
button.link {
  background: none;
  border: none;
  color: #0b5fa5;
  padding: 0;
  text-decoration: underline;
  cursor: pointer;
}
</style>
