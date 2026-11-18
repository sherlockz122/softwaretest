<script setup>
import { computed, ref } from "vue";
import { client } from "../api";
import { useLatest } from "../latest";
import { operationKeys } from "../task-tools";
import RequestError from "../components/RequestError.vue";
import FixRecommendationsPanel from "../components/FixRecommendationsPanel.vue";
const repos = useLatest((signal) =>
  client.request("/repositories?page=1&page_size=100", { signal }),
);
const models = useLatest((signal) =>
  client.request("/models?page=1&page_size=100", { signal }),
);
repos.load(null);
models.load(null);
const repositoryId = ref("");
const modelRunId = ref("");
const baseRef = ref("");
const mode = ref("ref");
const candidateRef = ref("");
const candidateDiff = ref("");
const submitting = ref(false);
const error = ref(null);
const result = ref(null);
const ack = ref(null);
const keys = operationKeys();
const canCreate = computed(() =>
  ["Member", "Admin"].includes(client.state.user?.role),
);
const levelNames = { high: "高", medium: "中", low: "低" };
const registered = computed(() =>
  (models.data.value?.items || []).filter((row) => row.registered),
);
const explanation = useLatest((id, signal) =>
  client.request(`/predictions/${encodeURIComponent(id)}/explanation`, {
    signal,
  }),
);
const number = (value) => (value == null ? "—" : Number(value).toFixed(4));
const density = (value) => (value == null ? "—" : Number(value).toFixed(6));
function factorText(factor) {
  return (
    (factor.feature || "").toUpperCase() +
    (factor.direction === "increase" ? "↑" : "↓")
  );
}
async function submit() {
  if (submitting.value) return;
  submitting.value = true;
  error.value = null;
  result.value = ack.value = null;
  explanation.clear();
  const body = {
    repository_id: repositoryId.value,
    model_run_id: modelRunId.value,
    base_ref: baseRef.value.trim(),
    candidate_ref: mode.value === "ref" ? candidateRef.value.trim() : null,
    candidate_diff: mode.value === "diff" ? candidateDiff.value : null,
  };
  try {
    const response = await client.request("/predictions/candidate", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": keys.get("review", body),
      },
      body: JSON.stringify(body),
    });
    keys.done("review", body);
    if (response.prediction_id) result.value = response;
    else ack.value = response;
  } catch (failure) {
    error.value = failure;
  } finally {
    submitting.value = false;
  }
}
</script>
<template>
  <section>
    <div class="section-heading">
      <h1>合入前评审</h1>
    </div>
    <p>
      对合入前的候选变更运行缺陷预测，按风险概率安排审查顺序。模型结果用于安排审查顺序，不是自动合并或拒绝结论。
    </p>
    <RequestError
      :error="error || repos.error.value || models.error.value || explanation.error.value"
    />
    <form v-if="canCreate" @submit.prevent="submit">
      <label
        >仓库<select v-model="repositoryId" required :disabled="submitting">
          <option value="">请选择仓库</option>
          <option
            v-for="repo in repos.data.value?.items || []"
            :key="repo.id"
            :value="repo.id"
          >
            {{ repo.url }} · {{ repo.default_branch || "—" }} · {{
              repo.parse_status || "未解析"
            }}
          </option>
        </select></label
      >
      <label
        >已注册模型<select v-model="modelRunId" required :disabled="submitting">
          <option value="">请选择模型</option>
          <option
            v-for="row in registered"
            :key="row.id"
            :value="row.id"
          >
            {{ row.plugin.toUpperCase() }} · 种子{{ row.seed }} · {{
              row.id.slice(0, 8)
            }}
          </option>
        </select></label
      >
      <label
        >Base 引用<input
          v-model="baseRef"
          maxlength="255"
          placeholder="例如 main 或 v1.0"
          required
          :disabled="submitting"
      /></label>
      <fieldset>
        <legend>Candidate 输入方式</legend>
        <label
          ><input
            v-model="mode"
            type="radio"
            value="ref"
            :disabled="submitting"
          />引用分支或标签</label
        ><label
          ><input
            v-model="mode"
            type="radio"
            value="diff"
            :disabled="submitting"
          />直接提交改动内容</label
        >
      </fieldset>
      <label v-if="mode === 'ref'"
        >Candidate 引用<input
          v-model="candidateRef"
          maxlength="255"
          placeholder="例如 feature/x"
          required
          :disabled="submitting"
      /></label>
      <label v-else
        >候选改动（unified diff）<textarea
          v-model="candidateDiff"
          rows="8"
          maxlength="2097152"
          placeholder="diff --git …"
          required
          :disabled="submitting"
        ></textarea></label
      >
      <p>
        候选单次最多 200 个文件、20,000 行改动，直接提交差异不超过 2
        MB。预测在 1 秒内同步返回，超时自动转入后台任务并给出任务编号。
      </p>
      <button
        :disabled="
          submitting ||
          !repositoryId ||
          !modelRunId ||
          !baseRef.trim() ||
          (mode === 'ref' ? !candidateRef.trim() : !candidateDiff.trim())
        "
      >
        {{ submitting ? "正在预测…" : "运行预测" }}
      </button>
      <p v-if="!registered.length && !models.loading.value">
        尚无已注册模型，请先在模型研究完成训练。
      </p>
    </form>
    <p v-else>当前账号可查看评审结果；提交预测需要Member或Admin权限。</p>
    <template v-if="ack">
      <p role="status">预测已转入后台任务。</p>
      <div class="actions">
        <RouterLink :to="`/tasks/${ack.task_id}`">查看任务</RouterLink
        ><RouterLink :to="`/risk?task=${ack.prediction_task_id}`"
          >查看风险队列</RouterLink
        >
      </div>
    </template>
    <template v-else-if="result">
      <h2>预测结果</h2>
      <p class="task-id">预测编号：{{ result.prediction_id }}</p>
      <p>
        概率 {{ number(result.probability) }} · 等级 {{
          levelNames[result.risk_level] || result.risk_level
        }}
        · Effort {{ result.effort }} · 风险密度
        {{ density(result.risk_density) }} · 判定阈值
        {{ number(result.threshold) }}
      </p>
      <p>
        特征版本：{{ result.feature_version }} · 校准方式：{{
          result.calibration
        }}
      </p>
      <p v-if="result.top_factors.length">
        主要影响因素：<span
          v-for="factor in result.top_factors"
          :key="factor.feature"
          class="factor"
          >{{ factorText(factor) }}</span
        >
      </p>
      <p v-if="result.limitations.length">
        {{ result.limitations.join(" ") }}
      </p>
      <div class="actions">
        <button @click="explanation.load(result.prediction_id)">
          查看完整解释
        </button>
        <RouterLink :to="`/model-runs/${result.model_run_id}`"
          >查看模型</RouterLink
        >
      </div>
      <FixRecommendationsPanel
        v-if="mode === 'ref' && candidateRef.trim()"
        :repository-id="repositoryId"
        :sha="candidateRef.trim()"
        @select="(sha) => candidateRef = sha"
      />
    </template>
    <template v-if="explanation.data.value">
      <h3>解释</h3>
      <p>
        方法：{{ explanation.data.value.method }} · 特征版本：{{
          explanation.data.value.feature_version
        }}
        · 判定阈值：{{ number(explanation.data.value.threshold) }}
      </p>
      <div
        class="table-scroll"
        role="region"
        aria-label="因素解释"
        tabindex="0"
      >
        <table>
          <thead>
            <tr>
              <th>因素</th>
              <th>取值</th>
              <th>贡献</th>
              <th>方向</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="factor in explanation.data.value.factors"
              :key="factor.feature"
            >
              <td>{{ (factor.feature || "").toUpperCase() }}</td>
              <td>{{ factor.value }}</td>
              <td>{{ number(factor.contribution) }}</td>
              <td>
                {{ factor.direction === "increase" ? "推高风险" : "降低风险" }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="explanation.data.value.limitations.length">
        {{ explanation.data.value.limitations.join(" ") }}
      </p>
    </template>
    <p v-if="models.loading.value" role="status">正在加载…</p>
  </section>
</template>
<style scoped>
label {
  display: block;
  min-width: 0;
  margin: 12px 0;
}
input,
select,
textarea {
  width: 100%;
  max-width: 100%;
}
fieldset {
  margin: 12px 0;
}
fieldset label {
  display: inline-block;
  margin-right: 16px;
}
.factor {
  display: inline-block;
  margin-right: 8px;
}
.actions {
  display: flex;
  gap: 12px;
  margin-top: 16px;
}
</style>
