<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { active, labels, shortId } from "../task-tools";
import {
  effortMetrics,
  effortNames,
  metricNames,
  metrics,
} from "../terms";
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
        ["验证区（定好判定门槛后）", report.value.metrics.validation_threshold],
        ["测试区（最终评价）", report.value.metrics.test],
        ["对照：哪类多就全猜哪类（测试区）", report.value.baseline.metrics],
      ]
    : [],
);
const modelNames = { lr: "逻辑回归", rf: "随机森林" };
const artifactNames = {
  "report.json": "评估报告（report.json）",
  "probabilities.csv": "每条提交的风险概率（probabilities.csv）",
  "model.skops": "模型文件（model.skops）",
};
const number = (value) => (value == null ? "—" : Number(value).toFixed(4));
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
      <h1>模型训练结果</h1>
      <button
        :disabled="data.loading.value"
        @click="data.load(route.params.id)"
      >
        刷新
      </button>
    </div>
    <RouterLink to="/models">返回模型列表</RouterLink>
    <RequestError :error="error || data.error.value" />
    <p v-if="data.loading.value" role="status">正在读取…</p>
    <template v-if="data.data.value">
      <p class="task-id">编号：{{ shortId(data.data.value.id) }}</p>
      <p>
        {{ modelNames[data.data.value.plugin] || data.data.value.plugin }} ·
        随机种子 {{ data.data.value.seed }} ·
        {{ labels[data.data.value.status] || data.data.value.status }}
      </p>
      <div class="actions">
        <RouterLink :to="`/datasets/${data.data.value.dataset_id}`"
          >查看用的数据集</RouterLink
        ><RouterLink :to="`/tasks/${data.data.value.task_id}`"
          >查看训练任务，可取消或重试</RouterLink
        >
      </div>
      <p>
        这是事后训练出来的研究模型：没有在真实历史时间点验证过，没有区分缺陷类型的把握，也不能直接发布用来预警。
      </p>
      <h2 v-if="data.data.value.registered">模型已生成</h2>
      <p v-else>
        还没有可用的模型。失败的或取消的任务，可以到任务页看原因、重试。
      </p>
      <template v-if="report">
        <p class="task-id">
          训练规则：{{ report.provenance.version }} · 代码指纹（用来确认训练时用的代码没被改过）：<span
            :title="report.provenance.code_sha"
            >{{ shortId(report.provenance.code_sha) }}</span
          >
        </p>
        <p>
          判定门槛：{{ number(report.threshold) }}（模型给出的概率超过它，就算「可能引入缺陷」）。验证区前
          {{ report.calibration.calibration_rows }} 条用来校准概率（把分数调得更接近真实比例），后
          {{ report.calibration.threshold_rows }}
          条用来定判定门槛，同一时间段的提交不拆开。
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
                <th>正常 / 有缺陷</th>
                <td v-for="[summary, score] in summaries" :key="summary">
                  {{ score.support.clean }} / {{ score.support.buggy }}
                </td>
              </tr>
              <tr v-for="(name, key) in metricNames" :key="key">
                <th>{{ name }}</th>
                <td v-for="[summary, score] in summaries" :key="summary">
                  {{ number(score[key]) }}
                </td>
              </tr>
              <tr v-for="(name, key) in effortNames" :key="key">
                <th>{{ name }}</th>
                <td v-for="[summary, score] in summaries" :key="summary">
                  {{ number(score.effort[key]) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <details>
          <summary>这些指标是什么意思？</summary>
          <ul>
            <li v-for="text in metrics" :key="text">{{ text }}</li>
            <li v-for="text in effortMetrics" :key="text">{{ text }}</li>
          </ul>
        </details>
        <p>
          验证区的结果会受判定门槛高低影响，测试区只做最后评价。表格里的区间是把数据按时间段重新抽样算出来的，只能说明结果有多不稳定，不能证明标签就是对的。工作量按新增和删除的行数估算；风险高也不等于找到了缺陷的根因。
        </p>
        <div class="actions">
          <button
            v-for="kind in ['report.json', 'probabilities.csv', 'model.skops']"
            :key="kind"
            :disabled="downloading"
            @click="download(kind)"
          >
            下载 {{ artifactNames[kind] }}
          </button>
        </div>
        <RiskPanel :run="String(route.params.id)" />
        <details>
          <summary>完整参数与复现信息（JSON）</summary>
          <pre>{{ JSON.stringify(report, null, 2) }}</pre>
        </details>
      </template>
      <details>
        <summary>数据来源与划分记录（JSON）</summary>
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
