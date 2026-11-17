<script setup>
import { computed, onUnmounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { labels, operationKeys, shortId } from "../task-tools";
import {
  datasetFeatureHints,
  datasetFeatureNames,
  featureFormHints,
  featureForms,
  label,
  partitions,
} from "../terms";
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
const poolList = useLatest((signal) =>
  client.request("/model-pool?page=1&page_size=100", { signal }),
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
const reuseMode = ref("none");
const reuseEntry = ref("");
const reuseBody = computed(() => {
  const selected = featureList.filter((name) => chosen.value.has(name));
  return {
    plugin: plugin.value,
    params:
      plugin.value === "lr"
        ? { c: Number(c.value), max_iter: Number(iterations.value) }
        : {
            n_estimators: Number(trees.value),
            max_depth: Number(depth.value),
            min_samples_leaf: Number(leaf.value),
          },
    features: selected,
    forms: Object.fromEntries(selected.map((name) => [name, forms[name]])),
  };
});
const sameMap = (a, b) => {
  const ka = Object.keys(a || {}).sort();
  const kb = Object.keys(b || {}).sort();
  return (
    JSON.stringify(ka) === JSON.stringify(kb) &&
    ka.every((k) => String(a[k]) === String(b[k]))
  );
};
const matchingPool = computed(() =>
  (poolList.data.value?.items || []).filter(
    (entry) =>
      entry.plugin === reuseBody.value.plugin &&
      sameMap(entry.params, reuseBody.value.params) &&
      JSON.stringify(entry.features) === JSON.stringify(reuseBody.value.features) &&
      sameMap(entry.forms, reuseBody.value.forms),
  ),
);
const keys = operationKeys();
const modelNames = { lr: "逻辑回归", rf: "随机森林" };
const featureList = Object.keys(datasetFeatureNames);
const chosen = ref(new Set(featureList));
const forms = reactive({});
function defaultForm() {
  return plugin.value === "lr" ? "standardize" : "raw";
}
function resetForms() {
  for (const name of featureList) forms[name] = defaultForm();
}
resetForms();
watch(plugin, resetForms);
function toggleFeature(name) {
  const next = new Set(chosen.value);
  if (next.has(name)) next.delete(name);
  else next.add(name);
  chosen.value = next;
}
const canCreate = computed(() =>
  ["Member", "Admin"].includes(client.state.user?.role),
);
const canSubmit = computed(
  () =>
    chosen.value.size > 0 &&
    (reuseMode.value !== "entry" || Boolean(reuseEntry.value)),
);
const chosenDataset = computed(
  () => datasets.data.value?.items.find((row) => row.id === dataset.value) || null,
);
const chosenPartitions = computed(
  () => chosenDataset.value?.summary?.partitions || {},
);
watch(page, (p) => list.load(p), { immediate: true });
poolList.load();
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
  const selected = featureList.filter((name) => chosen.value.has(name));
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
    features: selected,
    forms: Object.fromEntries(selected.map((name) => [name, forms[name]])),
  };
  if (reuseMode.value === "entry" && reuseEntry.value)
    body.reuse_from = reuseEntry.value;
  if (reuseMode.value === "auto") body.reuse_policy = "auto";
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
    <p>
      训练和预测是分开的两件事：这里只做训练；用训练好的模型对提交做批量预测，结果会进
      <RouterLink to="/risk">历史风险列表</RouterLink>，不会出现在本页。这里的训练记录与历史风险列表里的预测记录是各自独立的，互不混用。
    </p>
    <RequestError
      :error="error || list.error.value || datasets.error.value || poolList.error.value"
    />
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
      <p
        v-if="
          chosenDataset &&
          chosenDataset.ready &&
          Object.keys(chosenPartitions).length
        "
        class="hint"
      >
        这份数据集的样本划分：
        <span v-for="(count, name) in chosenPartitions" :key="name">
          {{ label(partitions, name) }}区 {{ count.clean + count.buggy }} 条样本（正常
          {{ count.clean }} / 有缺陷 {{ count.buggy }}）·
        </span>
        <span v-if="!('validation' in chosenPartitions)"
          >没有验证区，说明当时只分了训练和测试两份。</span
        >
      </p>
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
      <fieldset class="feature-choice">
        <legend>用哪些指标、以什么形式</legend>
        <p>
          默认全部指标、按模型推荐的形式；取消勾选的指标不参与训练。「取对数」适合偶尔出现超大值的指标，「标准化」的均值和标准差只在训练数据上计算，不会用到验证区和测试区。
        </p>
        <div
          class="table-scroll"
          role="region"
          aria-label="训练指标选择"
          tabindex="0"
        >
          <table>
            <thead>
              <tr>
                <th>是否使用</th>
                <th>指标</th>
                <th>形式</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="name in featureList" :key="name">
                <td>
                  <input
                    type="checkbox"
                    :checked="chosen.has(name)"
                    :disabled="creating"
                    @change="toggleFeature(name)"
                  />
                </td>
                <td :title="datasetFeatureHints[name]">
                  {{ datasetFeatureNames[name] || name }}
                </td>
                <td>
                  <select
                    v-model="forms[name]"
                    :disabled="creating || !chosen.has(name)"
                  >
                    <option
                      v-for="(shown, key) in featureForms"
                      :key="key"
                      :value="key"
                    >
                      {{ shown }}
                    </option>
                  </select>
                  <span class="hint">{{ featureFormHints[forms[name]] }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="!canSubmit" class="hint">
          至少要勾选一个指标才能训练。
        </p>
      </fieldset>
      <fieldset class="reuse-choice">
        <legend>模型参数从哪里来</legend>
        <p>
          模型池里放的是已经训练好的模型参数（预置基线，或把某次训练结果加入池）。复用只跳过训练这一步：标准化统计、概率校准、判定门槛都会在这次的训练数据上重新计算，评估口径与从零训练完全一致。
        </p>
        <label
          ><input
            type="radio"
            value="none"
            v-model="reuseMode"
            :disabled="creating"
          />从零训练（每次重新拟合模型参数）</label
        >
        <label
          ><input
            type="radio"
            value="entry"
            v-model="reuseMode"
            :disabled="creating"
          />复用池中模型（当前插件、参数、指标与形式下有
          {{ matchingPool.length }} 个匹配条目，换配置会重新匹配）</label
        >
        <template v-if="reuseMode === 'entry'">
          <label v-if="matchingPool.length"
            >选择池条目<select v-model="reuseEntry" :disabled="creating">
              <option
                v-for="entry in matchingPool"
                :key="entry.id"
                :value="entry.id"
              >
                {{
                  entry.source_kind === "preset"
                    ? "预置基线"
                    : `训练运行 ${shortId(entry.source_run_id)} 的模型`
                }}
                · {{ modelNames[entry.plugin] }} ·
                {{ entry.created_at.slice(0, 10) }}
              </option>
            </select></label
          >
          <p v-if="!matchingPool.length" class="hint">
            当前配置下池中没有匹配条目：要么改回从零训练，要么改用自动匹配，让系统按同样规则找一次。
          </p>
        </template>
        <label
          ><input
            type="radio"
            value="auto"
            v-model="reuseMode"
            :disabled="creating"
          />自动匹配（按插件、参数、指标与形式在池里找；找不到就从零训练，训练记录里会注明）</label
        >
      </fieldset>
      <p>
        分区各司其职：训练区用来训练模型，验证区（如果切了）用来调整概率、定判定门槛，测试区只做最后评价、不参与调整。验证区不是必须的，也可以只分训练和测试两份。任务中断后会用同样的输入重新训练。
      </p>
      <button :disabled="creating || !dataset || !canSubmit">
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
<style scoped>
.feature-choice {
  margin: 16px 0;
}
.feature-choice table {
  table-layout: fixed;
}
.reuse-choice {
  margin: 16px 0;
}
.reuse-choice label {
  display: block;
  margin: 8px 0;
}
.hint {
  color: #666;
  font-size: 12px;
}
</style>
