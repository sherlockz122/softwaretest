<script setup>
import { computed, ref, watch } from "vue";
import { client } from "../api";
import { useLatest } from "../latest";
import { operationKeys, date } from "../task-tools";
import RequestError from "./RequestError.vue";
const props = defineProps({
  repo: { type: Object, required: true },
  canWrite: Boolean,
  busy: Boolean,
  reload: { type: Function, required: true },
});
const selected = ref("");
const runsPage = ref(1);
const page = ref(1);
const feature = ref("");
const mode = ref("all");
const purpose = ref("historical");
const sending = ref(false);
const failure = ref(null);
const keys = operationKeys();
const runs = useLatest((input, signal) =>
  client.request(
    `/repositories/${input.id}/feature-runs?page=${input.page}&page_size=10`,
    { signal },
  ),
);
const results = useLatest((input, signal) => {
  const query = new URLSearchParams({
    run_id: input.run,
    page: input.page,
    page_size: 10,
  });
  if (input.feature) {
    query.set("feature", input.feature);
    if (input.mode !== "all") query.set("missing", input.mode === "missing");
  }
  return client.request(`/repositories/${input.id}/feature-quality?${query}`, {
    signal,
  });
});
const run = computed(() => results.data.value?.run);
const quality = computed(() => results.data.value?.quality);
const names = [
  "NS",
  "ND",
  "NF",
  "Entropy",
  "LA",
  "LD",
  "LT",
  "FIX",
  "NDEV",
  "AGE",
  "NUC",
  "EXP",
  "REXP",
  "SEXP",
];
const statuses = {
  pending: "未提取",
  queued: "提取排队中",
  extracting: "提取中",
  extracted: "提取完成",
  failed: "提取失败",
  cancelled: "提取已取消",
};
const canStart = computed(
  () =>
    props.canWrite &&
    props.repo.parse_status === "parsed" &&
    props.repo.head_sha,
);
function loadResults() {
  if (selected.value)
    return results.load({
      id: props.repo.id,
      run: selected.value,
      page: page.value,
      feature: feature.value,
      mode: mode.value,
    });
}
async function refresh() {
  const id = props.repo.id;
  await runs.load({ id, page: runsPage.value });
  if (id !== props.repo.id) return;
  if (!selected.value)
    selected.value =
      props.repo.feature_run?.root_task_id ||
      runs.data.value?.items[0]?.root_task_id ||
      "";
  await loadResults();
}
defineExpose({ refresh });
watch(
  () => props.repo.id,
  () => {
    selected.value = "";
    runsPage.value = page.value = 1;
    feature.value = "";
    mode.value = "all";
    failure.value = null;
    runs.clear();
    results.clear();
  },
  { immediate: true },
);
watch(
  () => [
    props.repo.id,
    props.repo.feature_run?.root_task_id,
    props.repo.feature_run?.processed,
    runsPage.value,
  ],
  refresh,
  { immediate: true },
);
watch(
  () => [selected.value, page.value, feature.value, mode.value],
  loadResults,
);
function select(value) {
  selected.value = value;
  page.value = 1;
  results.clear();
}
function drill(name, missing) {
  feature.value = name;
  mode.value = missing ? "missing" : "valid";
  page.value = 1;
}
async function start() {
  if (sending.value) return;
  const id = props.repo.id;
  const previous = props.repo.feature_run?.root_task_id;
  const body = {
    feature_version: "kamei14-dg-v1",
    purpose: purpose.value,
    fix_run_id:
      props.repo.fix_run?.head_sha === props.repo.head_sha &&
      props.repo.fix_run?.processed === props.repo.fix_run?.total
        ? props.repo.fix_run.root_task_id
        : null,
  };
  const input = JSON.stringify([id, props.repo.head_sha, previous, body]);
  sending.value = true;
  failure.value = null;
  try {
    const result = await client.request(`/repositories/${id}/feature-runs`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": keys.get("feature", input),
      },
      body: JSON.stringify(body),
    });
    keys.done("feature", input);
    if (id === props.repo.id) select(result.task_id);
  } catch (error) {
    if (id === props.repo.id) failure.value = error;
  } finally {
    sending.value = false;
    await props.reload();
    if (id === props.repo.id) {
      const accepted = props.repo.feature_run?.root_task_id;
      if (accepted && accepted !== previous) {
        keys.done("feature", input);
        select(accepted);
        failure.value = null;
      }
      await refresh();
    }
  }
}
</script>
<template>
  <section data-testid="feature-panel">
    <h2>Kamei14 原始特征与质量</h2>
    <p data-testid="feature-status">
      {{ statuses[repo.feature_status] || "未提取" }}
    </p>
    <p class="hint">
      冻结接受 HEAD 和 Fix 复核版本，缺失保留原因，不填充或生成缺陷标签。历史
      FIX 仅使用事件发生时已可用的证据；候选模式明确默认 FIX=0。
    </p>
    <div v-if="canStart">
      <label
        >特征用途
        <select v-model="purpose" :disabled="busy || sending">
          <option value="historical">历史事件</option>
          <option value="candidate">候选提交（FIX 默认 0）</option>
        </select></label
      >
      <button class="primary" :disabled="busy || sending" @click="start">
        {{ sending ? "提交中…" : "提取 Kamei14 特征" }}
      </button>
    </div>
    <p v-else-if="!canWrite" class="hint">
      Viewer 可查看特征质量；提取需要 Member 或 Admin。
    </p>
    <p v-else class="hint">请先完成接受 HEAD 的提交解析。</p>
    <RequestError :error="failure || runs.error.value || results.error.value" />
    <p v-if="runs.loading.value || results.loading.value" role="status">
      正在读取特征结果…
    </p>
    <ul class="task-list" data-testid="feature-runs">
      <li v-for="item in runs.data.value?.items" :key="item.root_task_id">
        <span class="task-id">{{ item.root_task_id }}</span
        ><span
          >{{ date(item.created_at) }} · {{ item.processed }} /
          {{ item.total ?? "待扫描" }} ·
          {{ item.complete ? "完整" : "未完成" }}</span
        ><button
          :disabled="selected === item.root_task_id"
          @click="select(item.root_task_id)"
        >
          查看特征轮次
        </button>
      </li>
    </ul>
    <div v-if="runs.data.value?.total" class="pagination">
      <button :disabled="runsPage <= 1" @click="runsPage--">
        特征轮次上一页</button
      ><span>{{ runsPage }}</span
      ><button
        :disabled="runsPage * 10 >= runs.data.value.total"
        @click="runsPage++"
      >
        特征轮次下一页
      </button>
    </div>
    <p v-else-if="!runs.loading.value">尚无特征轮次。</p>
    <div v-if="run" data-testid="feature-results">
      <dl class="task-facts">
        <dt>特征版本</dt>
        <dd>{{ run.feature_version }}</dd>
        <dt>接受 HEAD</dt>
        <dd class="task-id">{{ run.head_sha }}</dd>
        <dt>用途</dt>
        <dd>
          {{
            run.purpose === "historical" ? "历史事件" : "候选提交（FIX 默认 0）"
          }}
        </dd>
        <dt>历史覆盖</dt>
        <dd>
          {{
            run.history_coverage === "full" ? "全部可达历史" : "有限历史窗口"
          }}
        </dd>
        <dt>源摘要</dt>
        <dd class="task-id">{{ run.source_digest || "待冻结" }}</dd>
      </dl>
      <p v-if="!quality">
        本轮未完成，暂不发布总体质量统计。已提交批次可查看，但不表示完整结果。
      </p>
      <div v-else data-testid="feature-quality" class="feature-table-wrap">
        <table class="feature-table">
          <caption>
            完整轮次
            {{
              quality.count
            }}
            个提交；原始值分位数
          </caption>
          <thead>
            <tr>
              <th>特征</th>
              <th>有效 / 缺失</th>
              <th>min</th>
              <th>p25</th>
              <th>median</th>
              <th>p75</th>
              <th>p95</th>
              <th>max</th>
              <th>缺失原因</th>
              <th>下钻</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="name in names" :key="name">
              <th>{{ name }}</th>
              <td>
                {{ quality.features[name].valid }} /
                {{ quality.features[name].missing }}
              </td>
              <td
                v-for="q in ['min', 'p25', 'median', 'p75', 'p95', 'max']"
                :key="q"
              >
                {{ quality.features[name][q] ?? "未知" }}
              </td>
              <td>
                {{
                  Object.entries(quality.features[name].missing_reasons)
                    .map(([reason, count]) => `${reason}: ${count}`)
                    .join("，") || "无"
                }}
              </td>
              <td>
                <button @click="drill(name, true)">{{ name }} 缺失</button
                ><button @click="drill(name, false)">
                  {{ name }} 数值降序
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p>
        结果筛选：{{ feature || "全部特征" }} ·
        {{
          mode === "missing"
            ? "仅缺失"
            : mode === "valid"
              ? "有效值降序"
              : "全部"
        }}
        <button
          @click="
            feature = '';
            mode = 'all';
            page = 1;
          "
        >
          清除特征筛选
        </button>
      </p>
      <ul class="task-list">
        <li
          v-for="item in results.data.value?.items"
          :key="item.ordinal"
          data-testid="feature-result"
        >
          <span class="task-id">{{ item.sha }}</span
          ><span v-if="!item.result">待处理</span
          ><template v-else
            ><span>{{ date(item.result.event_time) }}</span
            ><span v-for="name in feature ? [feature] : names" :key="name"
              >{{ name }}={{ item.result.values[name] ?? "未知"
              }}<span v-if="item.result.reasons[name]"
                >（{{ item.result.reasons[name] }}）</span
              ></span
            ></template
          >
        </li>
      </ul>
      <div class="pagination">
        <button :disabled="page <= 1" @click="page--">特征结果上一页</button
        ><span>{{ page }}</span
        ><button
          :disabled="page * 10 >= (results.data.value?.total || 0)"
          @click="page++"
        >
          特征结果下一页
        </button>
      </div>
    </div>
  </section>
</template>
