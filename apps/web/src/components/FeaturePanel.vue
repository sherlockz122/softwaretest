<script setup>
import { computed, ref, watch } from "vue";
import { client } from "../api";
import { useLatest } from "../latest";
import { operationKeys, date, shortId } from "../task-tools";
import {
  featureNames,
  featureReason,
  featureStatus,
  label,
  ruleVersions,
} from "../terms";
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
// 只保留最新的指标规则；旧版结果仍然可以在历史轮次里查看，但不再提供选择。
const FEATURE_VERSION = "kamei14-dg-message-v2";
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
function featureLabel(name) {
  return label(featureNames, name);
}
function rounded(value) {
  if (value == null) return "未知";
  if (typeof value !== "number") return value;
  return Number.isInteger(value) ? value : Math.round(value * 1000) / 1000;
}
function featureValue(name, value) {
  if (name === "FIX") {
    if (value === 1) return "是";
    if (value === 0) return "否";
  }
  return rounded(value);
}
// 14 个指标按含义分成几组，一行一组；算不出来的单独归到一句里，
// 不再把“未知（只整理了部分历史）”重复六遍。
const groups = [
  { title: "改动规模", names: ["NF", "NS", "ND", "LA", "LD", "LT"] },
  { title: "改动分散程度", names: ["Entropy"] },
  { title: "历史背景", names: ["NUC", "AGE", "NDEV"] },
  { title: "作者经验", names: ["EXP", "REXP", "SEXP"] },
  { title: "是否疑似修复", names: ["FIX"] },
];
function itemGroups(item) {
  return groups
    .filter((group) => !feature.value || group.names.includes(feature.value))
    .map((group) => {
      const known = [];
      const missing = new Map();
      for (const name of group.names) {
        const raw = item.result.values[name];
        if (raw == null || raw === undefined) {
          const reason = item.result.reasons[name] ?? "";
          missing.set(reason, [...(missing.get(reason) ?? []), featureLabel(name)]);
        } else {
          // 只有一个指标的组，标题已经说清楚了，不再重复一遍指标名。
          known.push(
            group.names.length === 1
              ? `${featureValue(name, raw)}`
              : `${featureLabel(name)} ${featureValue(name, raw)}`,
          );
        }
      }
      const missingText = [...missing.entries()]
        .map(
          ([reason, list]) =>
            `${list.join("、")} 未计算${reason ? `（${label(featureReason, reason)}）` : ""}`,
        )
        .join("；");
      return {
        title: group.title,
        values: known.join(" · "),
        missing: missingText,
      };
    })
    .filter((group) => group.values || group.missing);
}
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
    feature_version: FEATURE_VERSION,
    purpose: purpose.value,
    fix_run_id: null,
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
    <h2>第四步：计算每个提交的统计指标</h2>
    <p data-testid="feature-status">
      {{ label(featureStatus, repo.feature_status) }}
    </p>
    <p class="hint">
      每个提交会算一组统计指标：改了多少行、涉及多少文件、作者对项目有多熟等。算不出来的指标会留空并写明原因，不会被填成 0；指标本身不含「是否有缺陷」的结论。
    </p>
    <div v-if="canStart">
      <p class="hint">
        指标一律按最新规则计算：核对 Git 里的原始记录，历史提交和待预测提交用同一套规则。FIX=0
        只表示提交说明里没看到修复迹象，不代表这条提交不是修复，更不代表它没有问题；说明读不出内容时会留空。一次最多处理 1000 个提交。
      </p>
      <label
        >算哪些提交
        <select v-model="purpose" :disabled="busy || sending">
          <option value="historical">历史提交</option>
          <option value="candidate">待预测的提交（同样规则）</option>
        </select></label
      >
      <button class="primary" :disabled="busy || sending" @click="start">
        {{ sending ? "提交中…" : "开始计算统计指标" }}
      </button>
    </div>
    <p v-else-if="!canWrite" class="hint">
      Viewer（只读账号）可以查看指标质量；开始计算需要 Member 或
      Admin 账号（普通成员或管理员）。
    </p>
    <p v-else class="hint">先完成第一步：当前最新提交的提交记录还没整理好。</p>
    <RequestError :error="failure || runs.error.value || results.error.value" />
    <p v-if="runs.loading.value || results.loading.value" role="status">
      正在读取指标结果…
    </p>
    <ul class="task-list" data-testid="feature-runs">
      <li v-for="item in runs.data.value?.items" :key="item.root_task_id">
        <span class="task-id">{{ shortId(item.root_task_id) }}</span
        ><span
          >{{ date(item.created_at) }} · 已计算 {{ item.processed }} /
          {{ item.total ?? "待扫描" }} 个提交 ·
          {{ item.complete ? "完整" : "还没算完" }} ·
          {{ item.feature_version }}</span
        ><button
          :disabled="selected === item.root_task_id"
          @click="select(item.root_task_id)"
        >
          查看这一轮指标
        </button>
      </li>
    </ul>
    <div v-if="runs.data.value?.total" class="pagination">
      <button :disabled="runsPage <= 1" @click="runsPage--">
        上一页指标</button
      ><span>{{ runsPage }}</span
      ><button
        :disabled="runsPage * 10 >= runs.data.value.total"
        @click="runsPage++"
      >
        下一页指标
      </button>
    </div>
    <p v-else-if="!runs.loading.value">还没有计算过指标。</p>
    <div v-if="run" data-testid="feature-results">
      <dl class="task-facts">
        <dt>指标版本</dt>
        <dd>{{ label(ruleVersions, run.feature_version) }}</dd>
        <dt>基于哪个最新提交</dt>
        <dd class="task-id">{{ shortId(run.head_sha) }}</dd>
        <dt>算哪些提交</dt>
        <dd>
          {{
            run.purpose === "historical"
              ? "历史提交"
              : run.feature_version === "kamei14-dg-message-v2"
                ? "待预测的提交（同样规则）"
                : "待预测的提交（FIX 默认 0）"
          }}
        </dd>
        <dt>覆盖的历史范围</dt>
        <dd>
          {{
            run.history_coverage === "full"
              ? "仓库全部可追溯的历史"
              : "只有最近一段历史"
          }}
        </dd>
        <dt>结果指纹（用来确认结果没被改过）</dt>
        <dd class="task-id" :title="run.source_digest || ''">
          {{ shortId(run.source_digest) || "待生成" }}
        </dd>
      </dl>
      <p
        v-if="run.feature_version === 'kamei14-dg-message-v2'"
        class="hint"
        data-testid="feature-version-semantics"
      >
        这一轮的 FIX 是「提交说明里有没有修复迹象」的粗略判断：0
        不代表这条提交不是修复，也不代表它没有问题。核对过 Git
        记录，只说明指标算得对，不说明缺陷标签已经齐全，也不说明可以直接拿去训练模型。
      </p>
      <p
        v-if="quality?.source_validation"
        data-testid="feature-source-validation"
      >
        已核对指标和 Git 原始记录的对应关系（版本
        {{ label(ruleVersions, quality.source_validation.version) }}）；历史范围：{{
          quality.source_validation.feature_population_complete
            ? "完整"
            : "只有最近一段"
        }}；缺陷标签是否覆盖完整、能不能直接用来训练模型，目前都还没有确认。
      </p>
      <p v-if="!quality">
        这一轮还没算完，暂时不发布总体统计。已经算好的部分可以看，但不代表完整结果。
      </p>
      <div v-else data-testid="feature-quality" class="feature-table-wrap">
        <p class="hint">
          表里的 25 / 50 / 75
          分位，是把数值从小到大排后这些位置上的值，用来大致看出分布；中位数就是
          50 分位。
        </p>
        <table class="feature-table">
          <caption>
            这一轮算了
            {{
              quality.count
            }}
            个提交；下表是每个指标的分布（最小值、四分位数、最大值）
          </caption>
          <thead>
            <tr>
              <th>指标</th>
              <th>有值 / 缺失</th>
              <th>最小</th>
              <th>25 分位</th>
              <th>中位数</th>
              <th>75 分位</th>
              <th>95 分位</th>
              <th>最大</th>
              <th>缺失原因</th>
              <th>查看</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="name in names" :key="name">
              <th>{{ featureLabel(name) }}<small>（{{ name }}）</small></th>
              <td>
                {{ quality.features[name].valid }} /
                {{ quality.features[name].missing }}
              </td>
              <td
                v-for="q in ['min', 'p25', 'median', 'p75', 'p95', 'max']"
                :key="q"
              >
                {{ rounded(quality.features[name][q]) }}
              </td>
              <td>
                {{
                  Object.entries(quality.features[name].missing_reasons)
                    .map(
                      ([reason, count]) =>
                        `${label(featureReason, reason)}：${count} 个`,
                    )
                    .join("，") || "无"
                }}
              </td>
              <td>
                <button @click="drill(name, true)">看缺失的提交</button
                ><button @click="drill(name, false)">数值从高到低</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p>
        正在看：{{ feature ? featureLabel(feature) : "全部指标" }} ·
        {{
          mode === "missing"
            ? "只看缺失的提交"
            : mode === "valid"
              ? "按数值从高到低"
              : "全部提交"
        }}
        <button
          @click="
            feature = '';
            mode = 'all';
            page = 1;
          "
        >
          取消筛选
        </button>
      </p>
      <ul class="task-list">
        <li
          v-for="item in results.data.value?.items"
          :key="item.ordinal"
          data-testid="feature-result"
        >
          <p class="task-id">{{ shortId(item.sha) }}</p>
          <p v-if="!item.result">待处理</p>
          <template v-else>
            <p>{{ date(item.result.event_time) }}</p>
            <div class="feature-groups">
              <p v-for="group in itemGroups(item)" :key="group.title">
                <strong>{{ group.title }}</strong>
                <span v-if="group.values">{{ group.values }}</span>
                <span v-if="group.missing" class="muted">{{
                  group.missing
                }}</span>
              </p>
            </div>
          </template>
        </li>
      </ul>
      <div class="pagination">
        <button :disabled="page <= 1" @click="page--">上一页结果</button
        ><span>{{ page }}</span
        ><button
          :disabled="page * 10 >= (results.data.value?.total || 0)"
          @click="page++"
        >
          下一页结果
        </button>
      </div>
    </div>
  </section>
</template>
