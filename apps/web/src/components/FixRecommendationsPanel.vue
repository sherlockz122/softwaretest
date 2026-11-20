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
</script>
<template>
  <section class="recommendations">
    <h2>同类修复参考</h2>
    <p class="hint">
      同一个仓库里以前修过类似问题的提交，按改到的文件和改动大小排出来的。里面可能有时间在后面的提交，它们是用来看思路的，不代表这次的问题和它们一样。
    </p>
    <RequestError :error="items.error.value" />
    <p v-if="data && !data.items?.length" class="hint">
      该仓库尚无修复历史，或没有与目标提交相近的修复案例。
    </p>
    <p v-if="data" class="hint">
      目标提交：{{ shortId(data.target?.sha) }}
    </p>
    <p v-if="data && data.items?.length" class="hint">
      共 {{ data.total }} 条匹配，最多显示 {{ data.limit }} 条。
    </p>
    <table v-if="data && data.items?.length">
      <thead>
        <tr>
          <th>修复提交</th>
          <th>提交信息摘要</th>
          <th>触及文件</th>
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
