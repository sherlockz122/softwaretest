<script setup>
import { computed, ref, watch } from "vue";
import { client } from "../api";
import { useLatest } from "../latest";
import { date, shortId } from "../task-tools";
import RequestError from "../components/RequestError.vue";

const repos = useLatest((signal) =>
  client.request("/repositories?page=1&page_size=100", { signal }),
);
const commits = useLatest((input, signal) =>
  input.id
    ? client.request(
        `/repositories/${encodeURIComponent(input.id)}/commits?page=1&page_size=20`,
        { signal },
      )
    : Promise.resolve({ items: [], total: 0 }),
);
const models = useLatest((signal) =>
  client.request("/models?page=1&page_size=100", { signal }),
);
const llmProviders = useLatest((signal) =>
  client.request("/predictions/llm/providers", { signal }),
);
repos.load(null);
models.load(null);
llmProviders.load(null);

const repositoryId = ref("");
const sha = ref("");
const chosen = ref([]);
const running = ref(false);
const localResults = ref([]);
const llm = ref(null);
const llmError = ref(null);
const llmBusy = ref(false);
const failure = ref(null);
const provider = ref("deepseek");

const readyModels = computed(() =>
  (models.data.value?.items || []).filter((row) => row.registered),
);
const providerList = computed(() => llmProviders.data.value?.items || []);
const activeProvider = computed(
  () => providerList.value.find((item) => item.id === provider.value) || null,
);
const llmReady = computed(() => Boolean(activeProvider.value?.configured));
const selectedCommit = computed(
  () => (commits.data.value?.items || []).find((row) => row.sha === sha.value) || null,
);
const canRun = computed(
  () => Boolean(repositoryId.value && sha.value && chosen.value.length) && !running.value,
);

watch(repositoryId, (id) => {
  sha.value = "";
  localResults.value = [];
  llm.value = null;
  llmError.value = null;
  commits.load({ id });
});

const levels = { high: "高", medium: "中", low: "低" };
const verdicts = {
  likely_defect: "可能引入缺陷",
  likely_clean: "大概率没问题",
  uncertain: "拿不准",
};

function factorText(factor) {
  const arrow = factor.direction === "increase" ? "↑" : "↓";
  return `${factor.feature}${arrow}`;
}

const wait = (ms) => new Promise((done) => setTimeout(done, ms));

async function predictOne(model) {
  const body = {
    repository_id: repositoryId.value,
    model_run_id: model.id,
    base_ref: `${sha.value}^`,
    candidate_ref: sha.value,
  };
  // 超过同步窗口的预测会转成后台任务；用同一个幂等键重发，任务完成后就能拿回结果。
  const key = crypto.randomUUID();
  const deadline = Date.now() + 60000;
  try {
    while (Date.now() < deadline) {
      const response = await client.request("/predictions/candidate", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": key,
        },
        body: JSON.stringify(body),
      });
      if (response.prediction_id) {
        return {
          id: model.id,
          plugin: model.plugin,
          status: "ok",
          probability: response.probability,
          level: response.risk_level,
          factors: (response.top_factors || []).slice(0, 3).map(factorText),
          threshold: response.threshold,
        };
      }
      await wait(1500);
    }
    return {
      id: model.id,
      plugin: model.plugin,
      status: "error",
      message: "等了 60 秒还没算完，可以稍后在任务中心查看",
    };
  } catch (error) {
    return {
      id: model.id,
      plugin: model.plugin,
      status: "error",
      message: error.message || error.code || "预测失败",
      code: error.code,
    };
  }
}

async function runModels() {
  if (!canRun.value) return;
  running.value = true;
  failure.value = null;
  localResults.value = [];
  const picked = readyModels.value.filter((row) => chosen.value.includes(row.id));
  try {
    localResults.value = await Promise.all(picked.map(predictOne));
  } finally {
    running.value = false;
  }
}

async function runLlm() {
  if (!repositoryId.value || !sha.value || llmBusy.value) return;
  llmBusy.value = true;
  llm.value = null;
  llmError.value = null;
  try {
    llm.value = await client.request("/predictions/llm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        repository_id: repositoryId.value,
        sha: sha.value,
        provider: provider.value,
      }),
    });
  } catch (error) {
    llmError.value = error;
  } finally {
    llmBusy.value = false;
  }
}

function toggleModel(id) {
  chosen.value = chosen.value.includes(id)
    ? chosen.value.filter((item) => item !== id)
    : [...chosen.value, id];
}
</script>
<template>
  <section>
    <div class="section-heading">
      <h1>模型预测</h1>
    </div>
    <p>
      选一条提交，让训练好的模型判断它是否会引入缺陷；也可以让外部大模型单独判断一次，两边结果放在一起对照。
    </p>

    <label
      >选仓库<select v-model="repositoryId">
        <option value="">请选择仓库</option>
        <option
          v-for="repo in repos.data.value?.items || []"
          :key="repo.id"
          :value="repo.id"
        >
          {{ repo.url }} · {{ repo.default_branch || "—" }} ·
          {{ repo.parse_status || "未解析" }}
        </option>
      </select></label
    >

    <p v-if="repositoryId && !commits.loading.value" class="hint">
      选一条提交（只列最近 20 条）：
    </p>
    <ul v-if="repositoryId" class="predict-commits">
      <li v-for="item in commits.data.value?.items || []" :key="item.sha">
        <label>
          <input
            type="radio"
            :value="item.sha"
            :checked="sha === item.sha"
            @change="sha = item.sha"
          />
          <span class="task-id">{{ shortId(item.sha) }}</span>
          <span>{{ item.message || "（空提交消息）" }}</span>
          <small>{{ date(item.committer_time) }} · {{ item.file_count }} 个文件</small>
        </label>
      </li>
    </ul>
    <p v-else-if="commits.loading.value" class="hint">正在读取提交记录…</p>

    <fieldset v-if="sha" class="predict-models">
      <legend>用哪些模型预测</legend>
      <label v-for="model in readyModels" :key="model.id" class="check-row">
        <input
          type="checkbox"
          :checked="chosen.includes(model.id)"
          @change="toggleModel(model.id)"
        />
        {{ model.plugin.toUpperCase() }} · 种子 {{ model.seed }} ·
        {{ shortId(model.id) }}
      </label>
      <p v-if="!readyModels.length" class="hint">
        还没有训练完成的模型，先去「模型研究」训练一个。
      </p>
      <button class="primary" :disabled="!canRun" @click="runModels">
        {{ running ? "预测中…" : "用选中的模型预测" }}
      </button>
      <p class="hint">
        本地模型要求仓库有全历史特征；不满足时会明确提示，不会硬算。
      </p>
    </fieldset>

    <RequestError :error="failure" />
    <RequestError :error="commits.error.value" />

    <div v-if="selectedCommit" class="predict-target">
      <h2>预测对象</h2>
      <p class="task-id">{{ shortId(selectedCommit.sha) }}</p>
      <p>{{ selectedCommit.message || "（空提交消息）" }}</p>
      <p class="hint">
        {{ date(selectedCommit.committer_time) }} · {{ selectedCommit.file_count }} 个文件
      </p>
    </div>

    <div v-if="localResults.length">
      <h2>本地模型结果</h2>
      <table>
        <thead>
          <tr>
            <th>模型</th>
            <th>概率</th>
            <th>风险等级</th>
            <th>主要因素</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in localResults" :key="row.id">
            <td>{{ row.plugin.toUpperCase() }} · {{ shortId(row.id) }}</td>
            <td v-if="row.status === 'ok'">
              {{ row.probability == null ? "—" : (row.probability * 100).toFixed(1) + "%" }}
            </td>
            <td v-else colspan="3">
              {{
                row.status === "queued"
                  ? "模型判断较慢，已转后台任务 " + shortId(row.task)
                  : `没能预测：${row.message}`
              }}
            </td>
            <td v-if="row.status === 'ok'">{{ levels[row.level] || row.level }}</td>
            <td v-if="row.status === 'ok'">{{ row.factors.join(" ") || "—" }}</td>
          </tr>
        </tbody>
      </table>
      <p class="hint">
        概率超过判定门槛就算「可能引入缺陷」。这只是研究模型的判断，用来安排审查顺序，不是结论。
      </p>
    </div>

    <div v-if="sha" class="predict-llm">
      <h2>大模型判断</h2>
      <p class="hint">
        会把这条提交的说明、改动的文件路径和增删行数发给所选的模型；不发送源码内容。
      </p>
      <label
        >用哪个大模型<select v-model="provider">
          <option
            v-for="item in providerList"
            :key="item.id"
            :value="item.id"
            :disabled="!item.configured"
          >
            {{ item.label }} · {{ item.model
            }}{{ item.configured ? "" : "（未配置）" }}
          </option>
        </select></label
      >
      <p v-if="activeProvider && !activeProvider.configured" class="hint">
        这个入口还没配置：DeepSeek 需要在 .env 填 DG_LLM_API_KEY；本地
        Ollama 需要先在本机装好并下载对应模型；自定义服务需要填三项
        DG_LLM_CUSTOM_*。
      </p>
      <button :disabled="llmBusy || !sha || !llmReady" @click="runLlm">
        {{ llmBusy ? "正在询问…" : "让大模型判断一次" }}
      </button>
      <RequestError :error="llmError" />
      <div v-if="llm">
        <p>
          <strong>{{ verdicts[llm.verdict] || llm.verdict }}</strong>
          <span v-if="llm.risk != null">
            · 风险分 {{ (llm.risk * 100).toFixed(0) }}%
          </span>
          <span class="hint">
            · {{ llm.model }} · 用时 {{ llm.elapsed_ms }}ms · 看了
            {{ llm.files_sent }} 个文件
          </span>
        </p>
        <p v-if="llm.reason">{{ llm.reason }}</p>
      </div>
    </div>
  </section>
</template>
<style scoped>
.predict-commits {
  max-height: 320px;
  overflow-y: auto;
  padding-left: 0;
  list-style: none;
}
.predict-commits li {
  padding: 6px 0;
  border-bottom: 1px solid #eef3f6;
}
.predict-commits label {
  display: flex;
  gap: 10px;
  align-items: baseline;
  cursor: pointer;
}
.predict-models {
  margin-top: 16px;
  border: 1px solid #dce6ed;
  border-radius: 12px;
  padding: 14px 18px;
}
.check-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin: 6px 0;
}
.predict-target,
.predict-llm {
  margin-top: 20px;
}
</style>
