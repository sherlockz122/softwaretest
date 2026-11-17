<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { date, labels, operationKeys } from "../task-tools";
import { datasetFeatureNames } from "../terms";
import RequestError from "../components/RequestError.vue";
const router = useRouter();
const page = ref(1);
const list = useLatest((p, signal) =>
  client.request(`/datasets?page=${p}&page_size=10`, { signal }),
);
const name = ref("");
const assetId = ref("");
const strategy = ref("time_ordered");
const trainRatio = ref(70);
const validationRatio = ref(15);
const testRatio = ref(15);
const creating = ref(false);
const error = ref(null);
const importFile = ref(null);
const importPath = ref("");
const importLicense = ref("CC-BY-4.0");
const importColumns = ref({});
const importColumnNames = ref([]);
const importing = ref(false);
const importError = ref(null);
const importDone = ref(null);
const isAdmin = computed(() => client.state.user?.role === "Admin");
const importFields = [
  { name: "canonical_repository", label: "仓库名", required: true },
  { name: "commit_id", label: "提交编号", required: true },
  { name: "event_epoch", label: "事件时间（整数秒）", required: true },
  { name: "label", label: "标签（0/1 或 True/False）", required: true },
  ...Object.keys(datasetFeatureNames).map((name) => ({
    name,
    label: `${name}（${datasetFeatureNames[name]}）`,
    required: true,
  })),
  { name: "fix", label: "修复标志（True/False，可选）", required: false },
];
const importMissing = computed(() =>
  importFields
    .filter((field) => field.required && !importColumns.value[field.name])
    .map((field) => field.name),
);
// 身份列按常见别名预匹配，结果仍展示在映射表里由用户核对调整。
const identityAliases = {
  canonical_repository: ["canonical_repository", "repo", "repository", "project"],
  commit_id: ["commit_id", "sha", "commit", "hash"],
  event_epoch: ["event_epoch", "time", "epoch", "author_date"],
  label: ["label", "bug", "buggy"],
};
function autoMatch() {
  importColumns.value = {};
  for (const field of importFields) {
    const candidates = [field.name, ...(identityAliases[field.name] || [])];
    const column = candidates.find((name) => importColumnNames.value.includes(name));
    if (column) importColumns.value[field.name] = column;
  }
}
const canImport = computed(
  () =>
    importMissing.value.length === 0 &&
    (importFile.value || importPath.value.trim()) &&
    !importing.value,
);
const keys = operationKeys();
const canCreate = computed(() =>
  ["Member", "Admin"].includes(client.state.user?.role),
);
const ratiosInvalid = computed(() => {
  const ratios = [
    Number(trainRatio.value),
    Number(validationRatio.value),
    Number(testRatio.value),
  ];
  if (ratios.some((r) => !Number.isFinite(r) || r < 0)) return true;
  if (ratios[0] === 0 || ratios[2] === 0) return true;
  return Math.abs(ratios.reduce((a, b) => a + b, 0) - 100) > 1e-6;
});
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
    train_ratio: Number(trainRatio.value) / 100,
    validation_ratio: Number(validationRatio.value) / 100,
    test_ratio: Number(testRatio.value) / 100,
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
async function onFileChosen(event) {
  const file = event.target.files?.[0] || null;
  importFile.value = file;
  importColumns.value = {};
  importColumnNames.value = [];
  importDone.value = null;
  if (!file) return;
  const head = ((await file.slice(0, 65536).text()).split(/\r?\n/)[0] || "").trim();
  let names = [];
  if (head.startsWith("{")) {
    try {
      names = Object.keys(JSON.parse(head));
    } catch {
      names = [];
    }
  } else {
    names = head.split(",");
  }
  importColumnNames.value = [...new Set(names)].filter(Boolean);
  autoMatch();
}
async function onPathChosen() {
  if (!importPath.value.trim() || !isAdmin.value) return;
  try {
    const result = await client.request(
      `/assets/import-columns?path=${encodeURIComponent(importPath.value.trim())}`,
    );
    importColumnNames.value = result.columns || [];
    autoMatch();
  } catch (failure) {
    importError.value = failure;
  }
}
function columnsPayload() {
  const columns = {};
  for (const field of importFields) {
    if (importColumns.value[field.name])
      columns[field.name] = importColumns.value[field.name];
  }
  return columns;
}
async function submitImport() {
  if (!canImport.value || importing.value) return;
  importing.value = true;
  importError.value = null;
  importDone.value = null;
  try {
    const columns = columnsPayload();
    let result;
    if (importPath.value.trim()) {
      result = await client.request("/assets/import-from-path", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: importPath.value.trim(),
          columns,
          license: importLicense.value,
        }),
      });
    } else {
      const query = new URLSearchParams({
        columns: JSON.stringify(columns),
        license: importLicense.value,
      });
      result = await client.request(`/assets/import?${query}`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: await importFile.value.arrayBuffer(),
        timeoutMs: 120000,
      });
    }
    importDone.value = result;
    assetId.value = result.asset_id;
    await list.load(page.value);
  } catch (failure) {
    importError.value = failure;
  } finally {
    importing.value = false;
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
            {{ asset.rows }} 条样本 ·
            {{ asset.source_kind === "user_import" ? "导入" : "内置" }} · 编号
            {{ asset.asset_id.slice(0, 8) }}
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
      <label
        >划分比例（训练 / 验证 / 测试，加起来要等于 100%）<span
          class="ratio-row"
        >
          <input
            v-model.number="trainRatio"
            type="number"
            min="1"
            max="99"
            required
            aria-label="训练比例"
            :disabled="creating"
          />
          <input
            v-model.number="validationRatio"
            type="number"
            min="0"
            max="99"
            required
            aria-label="验证比例"
            :disabled="creating"
          />
          <input
            v-model.number="testRatio"
            type="number"
            min="1"
            max="99"
            required
            aria-label="测试比例"
            :disabled="creating"
          />
        </span>
        <span class="hint">验证比例填 0 表示不切验证集，只分训练和测试两份。</span>
      </label>
      <p>
        每个数据集有 14 项特征：12 项数值指标 + 修复标志 + 改动前文件总行数。后台会核对数据来源和两类的数量，全部完成后才能下载。
      </p>
      <button
        :disabled="creating || !assetId || !name.trim() || ratiosInvalid"
      >
        {{ creating ? "正在提交…" : "生成研究数据集" }}
      </button>
      <p v-if="ratiosInvalid" class="hint">
        训练和测试比例要大于 0，验证比例可以为 0，三者加起来等于 100。
      </p>
      <p v-if="!assets.length && !list.loading.value">
        目前没有可用的公开数据，需要先把数据导入。
      </p>
    </form>
    <p v-else>
      当前账号可以查看数据集；生成数据集需要 Member 或 Admin
      账号（普通成员或管理员）。
    </p>
    <details v-if="canCreate">
      <summary>导入外部数据（CSV / JSONL）</summary>
      <p class="hint">
        导入的文件需要包含：仓库名、提交编号、事件时间（整数秒）、标签（0/1 或
        True/False）四列，以及 12
        项数值特征列（la、ld、nf、nd、ns、ent、ndev、age、nuc、aexp、arexp、asexp）；
        修复标志列（True/False）可选。选择文件后会自动按列名匹配，也可以手动调整；
        许可声明会随数据集一起记录。
      </p>
      <RequestError :error="importError" />
      <form @submit.prevent="submitImport">
        <label
          >许可声明<select v-model="importLicense" :disabled="importing">
            <option
              v-for="license in [
                'CC-BY-4.0',
                'CC0-1.0',
                'Apache-2.0',
                'MIT',
                'OTHER',
              ]"
              :key="license"
              :value="license"
            >
              {{ license }}
            </option>
          </select></label
        >
        <label
          >选择文件（CSV 或 JSONL，UTF-8）<input
            type="file"
            accept=".csv,.jsonl,.ndjson,text/csv"
            :disabled="importing"
            @change="onFileChosen"
        /></label>
        <label v-if="isAdmin"
          >或服务器本地路径<input
            v-model="importPath"
            :disabled="importing"
            placeholder="D:\\data\\public.csv"
          /><button
            type="button"
            :disabled="importing || !importPath.trim()"
            @click="onPathChosen"
          >
            读取文件列
          </button></label
        >
        <template v-if="importColumnNames.length">
          <p>
            文件列：{{ importColumnNames.join("、") }}。请核对下面每项的映射：
          </p>
          <label v-for="field in importFields" :key="field.name">
            {{ field.label }}<select
              v-model="importColumns[field.name]"
              :disabled="importing"
            >
              <option v-if="!field.required" value="">不映射（按未提供展示）</option>
              <option
                v-for="column in importColumnNames"
                :key="column"
                :value="column"
              >
                {{ column }}
              </option>
            </select></label
          >
          <p v-if="importMissing.length" class="hint">
            还缺映射：{{ importMissing.join("、") }}
          </p>
        </template>
        <p v-else-if="importFile">
          读不到文件表头，请检查文件是不是 CSV 或 JSONL。
        </p>
        <button :disabled="!canImport">
          {{ importing ? "正在导入…" : "导入为可用数据" }}
        </button>
        <p v-if="importDone">
          导入成功：{{ importDone.rows }} 条样本 · 编号
          {{ importDone.asset_id.slice(0, 8) }}，已在上方「用哪份公开数据」选中。
        </p>
      </form>
    </details>
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
.ratio-row {
  display: flex;
  gap: 8px;
}
.ratio-row input {
  width: 64px;
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
