<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { labels, operationKeys } from "../task-tools";
import RequestError from "../components/RequestError.vue";
const router = useRouter();
const page = ref(1);
const datasetPage = ref(1);
const list = useLatest((p, signal) =>
  client.request(`/models?page=${p}&page_size=10`, { signal }),
);
const datasets = useLatest((p, signal) =>
  client.request(`/datasets?page=${p}&page_size=20`, { signal }),
);
const dataset = ref("");
const plugin = ref("lr");
const seed = ref(42);
const c = ref(1);
const iterations = ref(500);
const trees = ref(100);
const depth = ref(16);
const leaf = ref(2);
const creating = ref(false);
const error = ref(null);
const keys = operationKeys();
const canCreate = computed(() =>
  ["Member", "Admin"].includes(client.state.user?.role),
);
watch(page, (p) => list.load(p), { immediate: true });
watch(
  datasetPage,
  (p) => {
    dataset.value = "";
    datasets.load(p);
  },
  { immediate: true },
);
const timer = setInterval(() => {
  if (!document.hidden && !list.loading.value && !client.state.busy)
    list.load(page.value);
}, 5000);
onUnmounted(() => clearInterval(timer));
async function create() {
  if (creating.value) return;
  creating.value = true;
  error.value = null;
  const body = {
    dataset_id: dataset.value,
    plugin: plugin.value,
    seed: Number(seed.value),
    params:
      plugin.value === "lr"
        ? { c: Number(c.value), max_iter: Number(iterations.value) }
        : {
            n_estimators: Number(trees.value),
            max_depth: Number(depth.value),
            min_samples_leaf: Number(leaf.value),
          },
  };
  try {
    const ack = await client.request("/model-runs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": keys.get("train", body),
      },
      body: JSON.stringify(body),
    });
    keys.done("train", body);
    await router.push(`/model-runs/${ack.model_run_id}`);
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
      <h1>模型研究</h1>
      <button :disabled="list.loading.value" @click="list.load(page)">
        刷新模型
      </button>
    </div>
    <p>
      使用固定公开数据训练LR/RF二分类基线。研究标签未经金标认证，结果不能直接用于候选变更预警。
    </p>
    <RequestError :error="error || list.error.value || datasets.error.value" />
    <form v-if="canCreate" @submit.prevent="create">
      <label
        >已发布数据集<select v-model="dataset" required :disabled="creating">
          <option value="">请选择数据集</option>
          <option
            v-for="row in datasets.data.value?.items || []"
            :key="row.id"
            :value="row.id"
            :disabled="!row.ready"
          >
            {{ row.name }} · {{ row.total ?? "—" }}条{{
              row.ready ? "" : "（未就绪）"
            }}
          </option>
        </select></label
      >
      <div class="actions">
        <button
          type="button"
          :disabled="datasetPage === 1 || creating"
          @click="datasetPage--"
        >
          上一页数据集
        </button>
        <button
          type="button"
          :disabled="
            datasetPage * 20 >= (datasets.data.value?.total || 0) || creating
          "
          @click="datasetPage++"
        >
          下一页数据集
        </button>
      </div>
      <label
        >算法<select v-model="plugin" :disabled="creating">
          <option value="lr">Logistic Regression</option>
          <option value="rf">Random Forest</option>
        </select></label
      >
      <label
        >随机种子<input
          v-model.number="seed"
          type="number"
          min="0"
          max="2147483647"
          step="1"
          required
          :disabled="creating"
      /></label>
      <template v-if="plugin === 'lr'">
        <label
          >正则化参数 C<input
            v-model.number="c"
            type="number"
            min="0.001"
            max="1000"
            step="any"
            required
            :disabled="creating"
        /></label>
        <label
          >最大迭代次数<input
            v-model.number="iterations"
            type="number"
            min="50"
            max="1000"
            step="1"
            required
            :disabled="creating"
        /></label>
      </template>
      <template v-else>
        <label
          >树数量<input
            v-model.number="trees"
            type="number"
            min="10"
            max="256"
            step="1"
            required
            :disabled="creating"
        /></label>
        <label
          >最大树深度<input
            v-model.number="depth"
            type="number"
            min="2"
            max="32"
            step="1"
            required
            :disabled="creating"
        /></label>
        <label
          >叶子最少样本<input
            v-model.number="leaf"
            type="number"
            min="1"
            max="100"
            step="1"
            required
            :disabled="creating"
        /></label>
      </template>
      <p>
        沿用数据集原分区；仅训练区拟合模型，验证区分别校准概率、选择阈值，测试区只做最终评价。任务中断后从固定输入重训。
      </p>
      <button :disabled="creating || !dataset">
        {{ creating ? "正在提交…" : "开始研究训练" }}
      </button>
    </form>
    <p v-else>当前账号可查看研究结果；训练需要Member或Admin权限。</p>
    <p v-if="list.loading.value" role="status">正在加载…</p>
    <p v-if="list.data.value && !list.data.value.items.length">
      尚无模型运行。
    </p>
    <div
      v-if="list.data.value?.items.length"
      class="table-scroll"
      role="region"
      aria-label="模型运行列表"
      tabindex="0"
    >
      <table>
        <thead>
          <tr>
            <th>运行编号</th>
            <th>算法</th>
            <th>种子</th>
            <th>状态</th>
            <th>用途</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in list.data.value.items" :key="row.id">
            <td>
              <RouterLink :to="`/model-runs/${row.id}`">{{
                row.id
              }}</RouterLink>
            </td>
            <td>{{ row.plugin.toUpperCase() }}</td>
            <td>{{ row.seed }}</td>
            <td>{{ labels[row.status] || row.status }}</td>
            <td>限定二分类研究</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="actions">
      <button :disabled="page === 1" @click="page--">上一页模型</button
      ><span>第{{ page }}页</span
      ><button
        :disabled="page * 10 >= (list.data.value?.total || 0)"
        @click="page++"
      >
        下一页模型
      </button>
    </div>
  </section>
</template>
