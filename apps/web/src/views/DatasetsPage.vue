<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { date, labels, operationKeys } from "../task-tools";
import RequestError from "../components/RequestError.vue";
const router = useRouter();
const page = ref(1);
const list = useLatest((p, signal) =>
  client.request(`/datasets?page=${p}&page_size=10`, { signal }),
);
const name = ref("");
const assetId = ref("");
const strategy = ref("time_ordered");
const creating = ref(false);
const error = ref(null);
const keys = operationKeys();
const canCreate = computed(() =>
  ["Member", "Admin"].includes(client.state.user?.role),
);
const assets = computed(() => list.data.value?.assets || []);
watch(page, (p) => list.load(p), { immediate: true });
const timer = setInterval(() => {
  if (!document.hidden && !list.loading.value && !client.state.busy)
    list.load(page.value);
}, 5000);
onUnmounted(() => clearInterval(timer));
async function create() {
  const selected = assets.value.find((a) => a.asset_id === assetId.value);
  if (!selected || creating.value) return;
  creating.value = true;
  error.value = null;
  const body = {
    name: name.value.trim(),
    asset_id: selected.asset_id,
    source_hash: selected.source_hash,
    purpose: "published_binary_12_v1",
    split_strategy: strategy.value,
  };
  try {
    const result = await client.request("/datasets", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": keys.get("dataset", body),
      },
      body: JSON.stringify(body),
    });
    keys.done("dataset", body);
    await router.push(`/datasets/${result.dataset_id}`);
  } catch (failure) {
    error.value = failure;
  } finally {
    creating.value = false;
  }
}
</script>
<template>
  <section>
    <div class="section-heading">
      <h1>研究数据集</h1>
      <button :disabled="list.loading.value" @click="list.load(page)">
        刷新
      </button>
    </div>
    <p>
      这里保存每次生成的数据集，之后可以回来对照。每个数据集都记录了数据从哪来、按什么规则切开；标签用的是原公开数据自带的判断（由规则生成，不是人工逐个确认）。
    </p>
    <RequestError :error="list.error.value || error" />
    <form v-if="canCreate" @submit.prevent="create">
      <label
        >起个名字<input
          v-model="name"
          maxlength="80"
          required
          :disabled="creating"
      /></label>
      <label
        >用哪份公开数据<select v-model="assetId" required :disabled="creating">
          <option value="">请选择已保存的数据</option>
          <option
            v-for="asset in assets"
            :key="asset.asset_id"
            :value="asset.asset_id"
          >
            {{ asset.rows }} 行数据 · 编号 {{ asset.asset_id.slice(0, 8) }}
          </option>
        </select></label
      >
      <label
        >怎么划分训练 / 验证 / 测试<select
          v-model="strategy"
          :disabled="creating"
        >
          <option value="time_ordered">
            按时间先后（同一时间段的提交不拆开）
          </option>
          <option value="repository_holdout">
            按仓库划分（留出独立仓库做测试）
          </option>
        </select></label
      >
      <p>
        每个数据集用 12 项数值指标，默认按 70% / 15% / 15%
        分成训练、验证、测试三份。后台会核对数据来源和两类的数量，全部完成后才能下载。
      </p>
      <button :disabled="creating || !assetId || !name.trim()">
        {{ creating ? "正在提交…" : "生成研究数据集" }}
      </button>
      <p v-if="!assets.length && !list.loading.value">
        目前没有可用的公开数据，需要先把数据导入。
      </p>
    </form>
    <p v-else>
      当前账号可以查看数据集；生成数据集需要 Member 或 Admin
      账号（普通成员或管理员）。
    </p>
    <p v-if="list.loading.value" role="status">正在读取…</p>
    <p v-if="list.data.value?.items.length" class="hint">
      窄屏下可左右滚动表格，查看全部列；键盘用户可聚焦表格后使用方向键。
    </p>
    <div
      v-if="list.data.value?.items.length"
      class="table-scroll"
      role="region"
      aria-label="数据集列表"
      tabindex="0"
    >
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>用途</th>
            <th>状态</th>
            <th>进度（已处理 / 总数）</th>
            <th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in list.data.value.items" :key="item.id">
            <td>
              <RouterLink :to="`/datasets/${item.id}`">{{
                item.name
              }}</RouterLink>
            </td>
            <td>公开二分类研究</td>
            <td>{{ labels[item.status] || item.status }}</td>
            <td>{{ item.processed }} / {{ item.total ?? "—" }}</td>
            <td>{{ date(item.created_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else-if="list.data.value">还没有生成过数据集。</p>
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
label {
  display: block;
  min-width: 0;
  margin: 12px 0;
}
input,
select {
  width: 100%;
  max-width: 100%;
}
table {
  table-layout: fixed;
}
td,
th {
  overflow-wrap: anywhere;
}
.actions {
  display: flex;
  gap: 12px;
  margin-top: 16px;
}
</style>
