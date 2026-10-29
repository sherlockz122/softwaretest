<script setup>
import { ref, watch } from "vue";
import { useRoute } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import RequestError from "../components/RequestError.vue";
const route = useRoute();
const page = ref(1);
const partition = ref("");
const detail = useLatest(({ id, page, partition }, signal) =>
  client.request(
    `/semantic-assets/${encodeURIComponent(id)}?offset=${(page - 1) * 20}&limit=20${partition ? `&partition=${partition}` : ""}`,
    { signal },
  ),
);
function load() {
  return detail.load({
    id: route.params.id,
    page: page.value,
    partition: partition.value,
  });
}
watch(
  () => route.params.id,
  () => {
    page.value = 1;
    partition.value = "";
    load();
  },
  { immediate: true },
);
watch(partition, () => {
  page.value = 1;
  load();
});
watch(page, load);
</script>
<template>
  <section>
    <RouterLink to="/semantic-assets">返回语义资产</RouterLink>
    <div class="section-heading">
      <h1>语义资产详情</h1>
      <button :disabled="detail.loading.value" @click="load">
        刷新资产详情
      </button>
    </div>
    <RequestError :error="detail.error.value" />
    <p v-if="detail.loading.value" role="status">正在核验并加载…</p>
    <template
      v-if="detail.data.value && !detail.loading.value && !detail.error.value"
    >
      <p class="identity">资产：{{ detail.data.value.id }}</p>
      <p>
        版本：{{ detail.data.value.producer_version }} ·
        {{ detail.data.value.accepted }} 条 ×
        {{ detail.data.value.shape[1] }} 维
      </p>
      <p>
        train {{ detail.data.value.partitions.train }} / validation
        {{
          detail.data.value.partitions.validation
        }}；成员和向量完整性核验通过。
      </p>
      <p class="identity">来源工件：{{ detail.data.value.origin_artifact }}</p>
      <p class="identity">
        输入：{{ detail.data.value.input_hash }}<br />权重：{{
          detail.data.value.checkpoint_hash
        }}
      </p>
      <ul>
        <li v-for="note in detail.data.value.limitations" :key="note">
          {{ note }}
        </li>
      </ul>
      <p>
        当前仅供研究查看，不能据此训练或发布产品模型。结构核验不证明变更已完整覆盖或模型有效。
      </p>
    </template>
    <label
      >成员分区
      <select v-model="partition">
        <option value="">全部</option>
        <option value="train">train</option>
        <option value="validation">validation</option>
      </select></label
    >
    <div
      v-if="detail.data.value && !detail.loading.value && !detail.error.value"
      class="table-scroll"
      role="region"
      aria-label="语义成员列表"
      tabindex="0"
    >
      <table>
        <thead>
          <tr>
            <th>序号</th>
            <th>仓库 / 提交</th>
            <th>分区</th>
            <th>所选 token（消息/新增/删除/base）</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in detail.data.value.items"
            :key="item.population_index"
          >
            <td>{{ item.population_index + 1 }}</td>
            <td>
              {{ item.member.canonical_repository }}<br />{{
                item.member.commit_id
              }}
            </td>
            <td>{{ item.member.partition }}</td>
            <td>
              {{ item.coverage.message.tokens }} /
              {{ item.coverage.added.tokens }} /
              {{ item.coverage.deleted.tokens }} /
              {{ item.coverage.base.tokens }}
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!detail.data.value.items.length">此分区当前页没有成员。</p>
    </div>
    <div class="actions">
      <button :disabled="page <= 1 || detail.loading.value" @click="page--">
        上一页</button
      ><span>第 {{ page }} 页</span
      ><button
        :disabled="
          !detail.data.value ||
          page * 20 >= detail.data.value.total ||
          detail.loading.value ||
          !!detail.error.value
        "
        @click="page++"
      >
        下一页
      </button>
    </div>
  </section>
</template>
<style scoped>
.identity,
td,
th {
  overflow-wrap: anywhere;
}
table {
  table-layout: fixed;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
}
</style>
