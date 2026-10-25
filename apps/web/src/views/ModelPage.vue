<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { active, labels } from "../task-tools";
import RequestError from "../components/RequestError.vue";
import RiskPanel from "../components/RiskPanel.vue";
const route = useRoute();
const data = useLatest((id, signal) =>
  client.request(`/model-runs/${encodeURIComponent(id)}`, { signal }),
);
const error = ref(null);
const downloading = ref(false);
watch(
  () => route.params.id,
  (id) => data.load(id),
  { immediate: true },
);
const timer = setInterval(() => {
  if (
    !document.hidden &&
    !data.loading.value &&
    !client.state.busy &&
    (!data.data.value || active.has(data.data.value.status))
  )
    data.load(route.params.id);
}, 2000);
onUnmounted(() => clearInterval(timer));
const report = computed(() => data.data.value?.report);
const summaries = computed(() =>
  report.value
    ? [
        ["验证阈值子集", report.value.metrics.validation_threshold],
        ["测试集", report.value.metrics.test],
        ["训练先验常数基线（测试）", report.value.baseline.metrics],
      ]
    : [],
);
const fields = {
  precision: "Precision",
  recall: "Recall",
  f1: "F1",
  pr_auc: "PR-AUC（AP）",
  roc_auc: "ROC-AUC",
  mcc: "MCC",
  brier: "Brier",
};
const number = (value) => (value == null ? "未定义" : Number(value).toFixed(4));
async function download(kind) {
  downloading.value = true;
  error.value = null;
  try {
    const blob = await client.request(
      `/model-runs/${encodeURIComponent(route.params.id)}?artifact=${kind}`,
      { responseType: "blob" },
    );
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${route.params.id}-${kind}`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch (failure) {
    error.value = failure;
  } finally {
    downloading.value = false;
  }
}
</script>
<template>
  <section>
    <div class="section-heading">
      <h1>模型运行详情</h1>
      <button
        :disabled="data.loading.value"
        @click="data.load(route.params.id)"
      >
        刷新结果
      </button>
    </div>
    <RouterLink to="/models">返回模型研究</RouterLink>
    <RequestError :error="error || data.error.value" />
    <p v-if="data.loading.value" role="status">正在加载…</p>
    <template v-if="data.data.value">
      <p class="task-id">运行编号：{{ data.data.value.id }}</p>
      <p>
        {{ data.data.value.plugin.toUpperCase() }} · 种子{{
          data.data.value.seed
        }}
        · {{ labels[data.data.value.status] || data.data.value.status }}
      </p>
      <div class="actions">
        <RouterLink :to="`/datasets/${data.data.value.dataset_id}`"
          >查看固定数据集</RouterLink
        ><RouterLink :to="`/tasks/${data.data.value.task_id}`"
          >查看训练任务／取消重试</RouterLink
        >
      </div>
      <p>
        限定后验弱监督二分类研究。未认证历史在线效果、缺陷类型、跨语言泛化或候选发布资格。
      </p>
      <h2 v-if="data.data.value.registered">研究工件已注册</h2>
      <p v-else>尚无可用研究模型。失败与取消任务可在任务页查看原因和重试。</p>
      <template v-if="report">
        <p class="task-id">
          训练规则：{{ report.provenance.version }} · 源码摘要：{{
            report.provenance.code_sha
          }}
        </p>
        <p>
          概率阈值：{{ number(report.threshold) }}。验证区前{{
            report.calibration.calibration_rows
          }}条校准，后{{
            report.calibration.threshold_rows
          }}条选择阈值；同时间组不拆分。
        </p>
        <div
          class="table-scroll"
          role="region"
          aria-label="模型评估指标"
          tabindex="0"
        >
          <table>
            <thead>
              <tr>
                <th>指标</th>
                <th v-for="[name] in summaries" :key="name">{{ name }}</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <th>clean / buggy</th>
                <td v-for="[name, score] in summaries" :key="name">
                  {{ score.support.clean }} / {{ score.support.buggy }}
                </td>
              </tr>
              <tr v-for="(label, key) in fields" :key="key">
                <th>{{ label }}</th>
                <td v-for="[name, score] in summaries" :key="name">
                  {{ number(score[key]) }}
                </td>
              </tr>
              <tr v-for="key in ['pofb20', 'ifa', 'popt']" :key="key">
                <th>{{ key.toUpperCase() }}</th>
                <td v-for="[name, score] in summaries" :key="name">
                  {{ number(score.effort[key]) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p>
          验证结果受阈值选择影响；测试集仅最终评价。置信区间来自固定时间块重采样，不能证明标签真实性。成本按LA+LD估算，解释不等于缺陷根因。
        </p>
        <div class="actions">
          <button
            v-for="kind in ['report.json', 'probabilities.csv', 'model.skops']"
            :key="kind"
            :disabled="downloading"
            @click="download(kind)"
          >
            下载 {{ kind }}
          </button>
        </div>
        <RiskPanel :run="String(route.params.id)" />
        <details>
          <summary>参数、校准、区间与复现信息</summary>
          <pre>{{ JSON.stringify(report, null, 2) }}</pre>
        </details>
      </template>
      <details>
        <summary>固定来源与分区审计</summary>
        <pre>{{ JSON.stringify(data.data.value.snapshot, null, 2) }}</pre>
      </details>
    </template>
  </section>
</template>
<style scoped>
pre {
  max-width: 100%;
  overflow: auto;
  padding: 12px;
  background: #f4f7f9;
}
summary {
  overflow-wrap: anywhere;
}
</style>
