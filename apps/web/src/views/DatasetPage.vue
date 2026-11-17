<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { labels, date, shortId } from "../task-tools";
import {
  datasetAuxiliaryFeatures,
  datasetAuxiliaryHints,
  datasetAuxiliaryNames,
  datasetFeatureHints,
  datasetFeatureNames,
  label,
  partitions,
} from "../terms";
import RequestError from "../components/RequestError.vue";
const route = useRoute();
const offset = ref(0);
const hidden = ref(new Set());
const filterPartition = ref("");
const filterLabel = ref("");
const downloadError = ref(null);
const downloading = ref(false);
const data = useLatest((id, signal) => {
  const filters = [
    filterPartition.value && `filter_partition=${filterPartition.value}`,
    filterLabel.value !== "" && `filter_label=${filterLabel.value}`,
  ]
    .filter(Boolean)
    .join("&");
  return client.request(
    `/datasets/${encodeURIComponent(id)}?offset=${offset.value}&limit=20${
      filters ? `&${filters}` : ""
    }`,
    { signal },
  );
});
// 十四维特征：12 项数值指标（summary.features）+ 修复标志 + 改动前文件总行数
const featureColumns = computed(() => {
  const features = data.data.value?.summary?.features;
  return features ? [...features, ...datasetAuxiliaryFeatures] : [];
});
function featureName(name) {
  return datasetFeatureNames[name] || datasetAuxiliaryNames[name] || name;
}
function featureHint(name) {
  return datasetFeatureHints[name] || datasetAuxiliaryHints[name] || name;
}
const pageTotal = computed(
  () => data.data.value?.member_total ?? data.data.value?.total ?? 0,
);
watch(
  () => route.params.id,
  (id) => {
    offset.value = 0;
    data.load(id);
  },
  { immediate: true },
);
watch([filterPartition, filterLabel], () => {
  offset.value = 0;
  data.load(route.params.id);
});
const timer = setInterval(() => {
  if (
    !document.hidden &&
    !data.loading.value &&
    !client.state.busy &&
    !data.data.value?.ready
  )
    data.load(route.params.id);
}, 3000);
onUnmounted(() => clearInterval(timer));
function paginate(delta) {
  offset.value += delta;
  data.load(route.params.id);
}
function toggleFeature(name) {
  const next = new Set(hidden.value);
  if (next.has(name)) next.delete(name);
  else next.add(name);
  hidden.value = next;
}
function featureValue(value) {
  if (value == null || !Number.isFinite(value)) return "—";
  return Math.abs(value) >= 100
    ? String(Math.round(value))
    : String(Number(value.toFixed(2)));
}
function featureCell(row, name) {
  if (name === "fix") {
    if (row.fix == null) return "—";
    return row.fix ? "是修复" : "非修复";
  }
  if (name === "lt" && row.lt == null) return "未提供";
  return featureValue(row[name] ?? row.numeric12?.[name]);
}
function featureTitle(row, name) {
  if (name === "lt" && row.lt == null)
    return `上游源数据没提供改动前文件总行数：${row.lt_missing_reason || "未提供"}`;
  if (name === "fix" && row.fix == null)
    return `导入文件没提供修复标志：${row.fix_missing_reason || "未提供"}`;
  return undefined;
}
const isImport = computed(
  () => data.data.value?.summary?.source_acquisition?.source_kind === "user_import",
);
function sourceColumns(acquisition) {
  const columns = acquisition?.import_columns;
  if (!columns) return null;
  return Object.entries(columns)
    .map(([field, column]) => `${field} ← ${column}`)
    .join("；");
}
async function download(partition) {
  downloading.value = true;
  downloadError.value = null;
  try {
    const blob = await client.request(
      `/datasets/${encodeURIComponent(route.params.id)}?format=csv&partition=${partition}`,
      { responseType: "blob" },
    );
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${route.params.id}-${partition}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch (error) {
    downloadError.value = error;
  } finally {
    downloading.value = false;
  }
}
</script>
<template>
  <section>
    <div class="section-heading">
      <h1>数据集详情</h1>
      <button
        :disabled="data.loading.value"
        @click="data.load(route.params.id)"
      >
        刷新详情
      </button>
    </div>
    <RouterLink to="/datasets">返回数据集列表</RouterLink>
    <RequestError :error="data.error.value || downloadError" />
    <p v-if="data.loading.value" role="status">正在读取…</p>
    <template v-if="data.data.value">
      <h2>{{ data.data.value.name }}</h2>
      <dl>
        <dt>用途</dt>
        <dd>
          {{
            isImport
              ? "研究用二分类数据（标签来自导入文件里的声明）"
              : "研究用二分类数据（标签来自原公开数据的规则判断）"
          }}
        </dd>
        <dt>状态</dt>
        <dd>{{ labels[data.data.value.status] || data.data.value.status }}</dd>
        <dt>进度（已处理 / 总数）</dt>
        <dd>
          {{ data.data.value.processed }} / {{ data.data.value.total ?? "—" }}
        </dd>
        <dt>创建时间</dt>
        <dd>{{ date(data.data.value.created_at) }}</dd>
        <dt>数据来源编号</dt>
        <dd class="break-anywhere">{{ shortId(data.data.value.asset_id) }}</dd>
      </dl>
      <RouterLink :to="`/tasks/${data.data.value.task_id}`"
        >查看任务，可取消或重试</RouterLink
      >
      <p v-if="!data.data.value.ready">
        数据集还没生成完；没完成的不能下载，也不能用于训练。如果失败了，可以到任务详情里取消或重试。
      </p>
      <template v-else>
        <h3>数据集已可用</h3>
        <p>
          数据来源和划分结构已经核对过。标签是原公开数据自带的规则判断，准不准、两类样本够不够还没有单独确认；这个版本也不支持按历史时间点回放、发布模型或判断缺陷类型。
        </p>
        <p>
          划分方式：{{
            data.data.value.policy.split_strategy === "time_ordered"
              ? "按时间先后（同一时间段的提交不拆开）"
              : "按仓库划分（留出独立仓库做测试）"
          }}
          · 共 14 项特征：12 项数值指标 + 修复标志 + 改动前文件总行数；改动前总行数上游源数据没提供，按未提供展示；修复标志只展示、不参与模型计算，避免提前泄露答案。
        </p>
        <p class="hint">窄屏下可左右滚动表格，查看全部列与下载操作。</p>
        <div
          class="table-scroll"
          role="region"
          aria-label="分区与导出"
          tabindex="0"
        >
          <table>
            <thead>
              <tr>
                <th>分区</th>
                <th>正常提交</th>
                <th>有缺陷提交</th>
                <th>下载</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(count, partition) in data.data.value.summary.partitions"
                :key="partition"
              >
                <td>{{ label(partitions, partition) }}</td>
                <td>{{ count.clean }}</td>
                <td>{{ count.buggy }}</td>
                <td>
                  <button :disabled="downloading" @click="download(partition)">
                    下载{{ label(partitions, partition) }}数据（CSV）
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <details>
          <summary>数据来源和划分详情</summary>
          <dl>
            <dt>数据来源类型</dt>
            <dd>{{ isImport ? "导入文件" : "内置公开资产" }}</dd>
            <template v-if="isImport">
              <dt>列映射</dt>
              <dd class="break-anywhere">
                {{
                  sourceColumns(
                    data.data.value.summary.source_acquisition,
                  ) || "未记录"
                }}
              </dd>
            </template>
            <dt>原数据获取时间</dt>
            <dd>
              {{
                data.data.value.summary.source_acquisition?.acquired_at
                  ? date(data.data.value.summary.source_acquisition.acquired_at)
                  : "与首次保存时间一致（导入即获取）"
              }}
            </dd>
            <dt>数据首次保存时间</dt>
            <dd>{{ date(data.data.value.summary.source_first_stored_at) }}</dd>
            <dt>使用许可</dt>
            <dd>
              {{
                data.data.value.summary.source_acquisition
                  ?.import_license_declaration ||
                data.data.value.summary.source_acquisition?.apachejit_license ||
                "待核验"
              }}
            </dd>
            <dt>原始记录数 / 去重后样本数</dt>
            <dd>
              {{ data.data.value.summary.original_rows }} /
              {{ data.data.value.summary.rows }}
            </dd>
            <dt>合并的重复行</dt>
            <dd>{{ data.data.value.summary.duplicate_rows }}</dd>
            <dt>提交时间</dt>
            <dd>
              {{
                isImport
                  ? "用的是导入文件里声明的事件时间。这是事后研究，不表示当时就能知道标签。"
                  : "用的是原作者提交时间。这是事后研究，不表示当时就能知道标签。"
              }}
            </dd>
          </dl>
          <p
            v-if="
              data.data.value.summary.source_acquisition
                ?.fixture_not_real_acquisition
            "
          >
            工程测试数据，不代表真实标签或模型效果。
          </p>
          <div
            class="table-scroll"
            role="region"
            aria-label="独立仓库支持数"
            tabindex="0"
          >
            <table>
              <thead>
                <tr>
                  <th>独立仓库</th>
                  <th>正常提交</th>
                  <th>有缺陷提交</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(count, repository) in data.data.value.summary
                    .repositories"
                  :key="repository"
                >
                  <td>{{ repository }}</td>
                  <td>{{ count.clean }}</td>
                  <td>{{ count.buggy }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>
        <p class="break-anywhere">
          内容指纹（用来确认文件没有被改过）：<span
            :title="data.data.value.manifest_hash"
            >{{ shortId(data.data.value.manifest_hash) }}</span
          >
        </p>
        <h3>数据表</h3>
        <p>
          每行是一个样本（一次提交），列是它的各项特征。想看清某项特征是什么意思，把鼠标停在表头上；没有勾选「显示哪些列」里的特征不会出现在表里。
        </p>
        <div class="filters">
          <label
            >只看分区<select v-model="filterPartition">
              <option value="">全部分区</option>
              <option
                v-for="(name, key) in partitions"
                :key="key"
                :value="key"
              >
                {{ name }}
              </option>
            </select></label
          >
          <label
            >只看标签<select v-model="filterLabel">
              <option value="">全部标签</option>
              <option value="1">有缺陷</option>
              <option value="0">正常</option>
            </select></label
          >
          <details class="column-toggles">
            <summary>显示哪些列</summary>
            <label
              v-for="f in featureColumns"
              :key="f"
              :title="featureHint(f)"
            >
              <input
                type="checkbox"
                :checked="!hidden.has(f)"
                @change="toggleFeature(f)"
              />{{ featureName(f) }}
            </label>
          </details>
        </div>
        <p v-if="filterPartition || filterLabel !== ''">
          当前按筛选条件显示，共 {{ pageTotal }} 条；下载不受筛选影响，仍是该分区的完整数据。
        </p>
        <div
          class="table-scroll"
          role="region"
          aria-label="数据集成员"
          tabindex="0"
        >
          <table>
            <thead>
              <tr>
                <th>仓库</th>
                <th>提交</th>
                <th>分区</th>
                <th>标签</th>
                <th
                  v-for="f in featureColumns"
                  v-show="!hidden.has(f)"
                  :key="f"
                  :title="featureHint(f)"
                >
                  {{ featureName(f) }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in data.data.value.members" :key="row.ordinal">
                <td>{{ row.canonical_repository }}</td>
                <td class="break-anywhere" :title="row.commit_id">
                  {{ shortId(row.commit_id) }}
                </td>
                <td>{{ label(partitions, row.partition) }}</td>
                <td>{{ row.label ? "有缺陷" : "正常" }}</td>
                <td
                  v-for="f in featureColumns"
                  v-show="!hidden.has(f)"
                  :key="f"
                  :title="featureTitle(row, f)"
                >
                  {{ featureCell(row, f) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="actions">
          <button
            :disabled="offset <= 0 || data.loading.value"
            @click="paginate(-20)"
          >
            上一页</button
          ><button
            :disabled="offset + 20 >= pageTotal || data.loading.value"
            @click="paginate(20)"
          >
            下一页
          </button>
        </div>
      </template>
    </template>
  </section>
</template>
<style scoped>
.break-anywhere,
dl,
h2 {
  overflow-wrap: anywhere;
}
table {
  table-layout: fixed;
}
td {
  overflow-wrap: anywhere;
}
.actions {
  display: flex;
  gap: 12px;
  margin-top: 16px;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: flex-start;
}
.column-toggles {
  border: 1px solid #ccc;
  border-radius: 4px;
  padding: 6px 10px;
}
.column-toggles summary {
  cursor: pointer;
}
.column-toggles label {
  display: inline-block;
  margin-right: 10px;
  white-space: nowrap;
}
</style>
