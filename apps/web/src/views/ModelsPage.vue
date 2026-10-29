<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { labels, operationKeys, shortId } from "../task-tools";
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
const modelNames = { lr: "逻辑回归", rf: "随机森林" };
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
      <h1>训练模型</h1>
      <button :disabled="list.loading.value" @click="list.load(page)">
        刷新
      </button>
    </div>
    <p>
      用选定的研究数据集训练一个二分类模型，判断「这次提交会不会引入缺陷」。标签来自原公开数据的规则判断，没有人工逐条确认，所以结果只适合研究对照，不能直接拿来预警。
    </p>
    <RequestError :error="error || list.error.value || datasets.error.value" />
    <form v-if="canCreate" @submit.prevent="create">
      <label
        >用哪个数据集<select v-model="dataset" required :disabled="creating">
          <option value="">请选择数据集</option>
          <option
            v-for="row in datasets.data.value?.items || []"
            :key="row.id"
            :value="row.id"
            :disabled="!row.ready"
          >
            {{ row.name }} · {{ row.total ?? "—" }} 条{{
              row.ready ? "" : "（还没生成完，不能选）"
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
          上一页
        </button>
        <button
          type="button"
          :disabled="
            datasetPage * 20 >= (datasets.data.value?.total || 0) || creating
          "
          @click="datasetPage++"
        >
          下一页
        </button>
      </div>
      <label
        >用哪种模型<select v-model="plugin" :disabled="creating">
          <option value="lr">逻辑回归（Logistic Regression）</option>
          <option value="rf">随机森林（Random Forest）</option>
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
          >正则化参数 C（防止模型死记训练数据；越大越贴合训练数据）<input
            v-model.number="c"
            type="number"
            min="0.001"
            max="1000"
            step="any"
            required
            :disabled="creating"
        /></label>
        <label
          >最多训练多少轮（越大越慢，通常保持默认）<input
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
          >树的数量（越多越稳，也越慢）<input
            v-model.number="trees"
            type="number"
            min="10"
            max="256"
            step="1"
            required
            :disabled="creating"
        /></label>
        <label
          >每棵树的最大深度（越深越容易死记训练数据）<input
            v-model.number="depth"
            type="number"
            min="2"
            max="32"
            step="1"
            required
            :disabled="creating"
        /></label>
        <label
          >每片叶子至少要放的样本数（越大模型越简单）<input
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
        三份数据各司其职：训练区用来训练模型，验证区用来调整概率、定判定门槛，测试区只做最后评价、不参与调整。任务中断后会用同样的输入重新训练。
      </p>
      <button :disabled="creating || !dataset">
        {{ creating ? "正在提交…" : "开始训练" }}
      </button>
    </form>
    <p v-else>
      当前账号可以查看训练结果；训练模型需要 Member 或 Admin
      账号（普通成员或管理员）。
    </p>
    <p v-if="list.loading.value" role="status">正在读取…</p>
    <p v-if="list.data.value && !list.data.value.items.length">
      还没有训练过模型。
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
            <th>编号</th>
            <th>模型</th>
            <th>随机种子</th>
            <th>状态</th>
            <th>用途</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in list.data.value.items" :key="row.id">
            <td>
              <RouterLink :to="`/model-runs/${row.id}`">{{
                shortId(row.id)
              }}</RouterLink>
            </td>
            <td>{{ modelNames[row.plugin] || row.plugin }}</td>
            <td>{{ row.seed }}</td>
            <td>{{ labels[row.status] || row.status }}</td>
            <td>研究用二分类模型</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="actions">
      <button :disabled="page === 1" @click="page--">上一页</button
      ><span>第 {{ page }} 页</span
      ><button
        :disabled="page * 10 >= (list.data.value?.total || 0)"
        @click="page++"
      >
        下一页
      </button>
    </div>
  </section>
</template>
