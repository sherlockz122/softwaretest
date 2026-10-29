<script setup>
import { ref, watch } from "vue";
import { client } from "../api";
import { useLatest } from "../latest";
import RequestError from "../components/RequestError.vue";
const page = ref(1);
const list = useLatest((p, signal) =>
  client.request(`/semantic-assets?page=${p}&page_size=10`, { signal }),
);
watch(page, (p) => list.load(p), { immediate: true });
</script>
<template>
  <section>
    <div class="section-heading">
      <h1>语义资产</h1>
      <button :disabled="list.loading.value" @click="list.load(page)">
        刷新语义资产
      </button>
    </div>
    <p>
      查看已保存的研究向量及其来源。结构可读不代表标签、许可、独立评价或模型发布资格已通过；当前不能用于历史或候选预测。
    </p>
    <RequestError :error="list.error.value" />
    <p v-if="list.loading.value" role="status">正在加载…</p>
    <ul v-if="list.data.value?.items.length" class="assets">
      <li v-for="item in list.data.value.items" :key="item.id">
        <RouterLink :to="`/semantic-assets/${item.id}`"
          >{{ item.id.slice(0, 12) }} ·
          {{ item.accepted }} 条研究向量</RouterLink
        >
        <p>
          {{ item.producer_version }} · train {{ item.partitions.train }} /
          validation {{ item.partitions.validation }}
        </p>
        <p>仅供研究查看；打开详情核验向量和成员。</p>
      </li>
    </ul>
    <p v-else-if="list.data.value && !list.error.value">
      尚无语义资产，请联系管理员完成已验证研究资产的安装。
    </p>
    <div class="actions">
      <button :disabled="page <= 1 || list.loading.value" @click="page--">
        上一页</button
      ><span>第 {{ page }} 页</span
      ><button
        :disabled="
          !list.data.value ||
          page * 10 >= list.data.value.total ||
          list.loading.value
        "
        @click="page++"
      >
        下一页
      </button>
    </div>
  </section>
</template>
<style scoped>
.assets {
  padding-left: 24px;
  overflow-wrap: anywhere;
}
.assets li {
  margin-bottom: 20px;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
}
</style>
