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
const runsPage = ref(1);
const evidencePage = ref(1);
const selectedRun = ref("");
const includeMedium = ref(true);
const sending = ref(false);
const reviewing = ref("");
const failure = ref(null);
const notes = ref({});
const keys = operationKeys();
const runs = useLatest((input, signal) =>
  client.request(
    `/repositories/${input.id}/fix-runs?page=${input.page}&page_size=10`,
    { signal },
  ),
);
const evidence = useLatest((input, signal) =>
  client.request(
    `/repositories/${input.id}/fix-runs/${input.run}/evidence?page=${input.page}&page_size=10`,
    { signal },
  ),
);
const labels = {
  pending: "未识别",
  queued: "识别排队中",
  detecting: "识别中",
  detected: "识别完成",
  failed: "识别失败",
  cancelled: "识别已取消",
};
const reviewLabels = {
  unreviewed: "待复核",
  confirmed: "人工确认",
  rejected: "人工拒绝",
};
const confidenceLabels = { high: "高", medium: "中", low: "低" };
const canStart = computed(
  () =>
    props.canWrite &&
    props.repo.parse_status === "parsed" &&
    ["pending", "synced", "requires_review"].includes(props.repo.sync_status),
);
async function refresh() {
  const id = props.repo.id;
  await runs.load({ id, page: runsPage.value });
  if (id !== props.repo.id) return;
  if (!selectedRun.value)
    selectedRun.value =
      props.repo.fix_run?.root_task_id ||
      runs.data.value?.items[0]?.root_task_id ||
      "";
  if (selectedRun.value)
    await evidence.load({
      id,
      run: selectedRun.value,
      page: evidencePage.value,
    });
}
defineExpose({ refresh });
watch(
  () => props.repo.id,
  () => {
    selectedRun.value = "";
    runsPage.value = evidencePage.value = 1;
    failure.value = null;
    notes.value = {};
  },
  { immediate: true },
);
watch(
  () => [
    props.repo.id,
    props.repo.fix_run?.root_task_id,
    props.repo.fix_run?.processed,
    runsPage.value,
  ],
  refresh,
  { immediate: true },
);
watch(
  () => [selectedRun.value, evidencePage.value],
  () => {
    if (selectedRun.value)
      evidence.load({
        id: props.repo.id,
        run: selectedRun.value,
        page: evidencePage.value,
      });
  },
);
function select(run) {
  selectedRun.value = run.root_task_id;
  evidencePage.value = 1;
  failure.value = null;
}
async function start() {
  if (sending.value) return;
  const id = props.repo.id;
  const previous = props.repo.fix_run?.root_task_id;
  const body = {
    rule_version: "fix-evidence-v1",
    include_medium: includeMedium.value,
  };
  const input = JSON.stringify([id, props.repo.head_sha, previous, body]);
  sending.value = true;
  failure.value = null;
  try {
    const result = await client.request(`/repositories/${id}/fix-detection`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": keys.get("fix", input),
      },
      body: JSON.stringify(body),
    });
    keys.done("fix", input);
    if (id === props.repo.id) selectedRun.value = result.task_id;
  } catch (error) {
    if (id === props.repo.id) failure.value = error;
  } finally {
    if (id === props.repo.id) {
      await props.reload();
      if (id === props.repo.id) {
        if (
          props.repo.fix_run?.root_task_id !== previous &&
          props.repo.fix_run
        ) {
          selectedRun.value = props.repo.fix_run.root_task_id;
          if (failure.value?.code === "NETWORK_UNAVAILABLE")
            failure.value = null;
        }
        runsPage.value = evidencePage.value = 1;
        await refresh();
      }
    }
    sending.value = false;
  }
}
async function review(row, status) {
  if (reviewing.value) return;
  const note = notes.value[row.id]?.trim();
  if (!note) {
    failure.value = { message: "请填写复核理由。" };
    return;
  }
  const id = props.repo.id;
  const run = selectedRun.value;
  reviewing.value = row.id;
  failure.value = null;
  try {
    await client.request(`/repositories/${id}/fix-evidence/${row.id}/review`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        status,
        expected_revision: row.review_revision,
        note,
      }),
    });
  } catch (error) {
    if (id === props.repo.id && run === selectedRun.value)
      failure.value = error;
  } finally {
    if (id === props.repo.id && run === selectedRun.value) await refresh();
    reviewing.value = "";
  }
}
</script>
<template>
  <section data-testid="fix-panel">
    <h2>Fix 修复证据</h2>
    <p data-testid="fix-status">{{ labels[repo.fix_status] }}</p>
    <p class="hint">
      证据与人工复核用于识别候选修复；没有证据不代表干净提交。SZZ 标签尚未生成。
    </p>
    <form v-if="canStart" class="create-task" @submit.prevent="start">
      <label
        ><input
          v-model="includeMedium"
          type="checkbox"
          :disabled="sending || busy"
        />纳入中等级关键词候选</label
      >
      <button class="primary" :disabled="sending || busy">
        {{ sending ? "提交中…" : "运行 Fix 识别" }}
      </button>
    </form>
    <p v-if="!canWrite" class="hint">
      Viewer 可查看证据；运行与复核需要 Member 或 Admin。
    </p>
    <p v-if="['failed', 'cancelled'].includes(repo.fix_status)" class="hint">
      已提交证据保留，可在当前任务重试；新分析将保留此前结果。
    </p>
    <RequestError :error="failure" />
    <RequestError :error="runs.error.value" />
    <ul
      v-if="runs.data.value && !runs.error.value"
      class="repository-list"
      data-testid="fix-runs"
    >
      <li v-for="run in runs.data.value.items" :key="run.root_task_id">
        <p class="task-id">{{ run.head_sha ?? "空历史" }}</p>
        <small
          >{{ run.rule_version }} · 中等级{{
            run.include_medium ? "纳入" : "不纳入"
          }}
          · {{ run.processed }} / {{ run.total ?? "待扫描" }} ·
          {{ date(run.created_at) }}</small
        >
        <button @click="select(run)">
          {{ selectedRun === run.root_task_id ? "当前分析" : "查看此轮证据" }}
        </button>
      </li>
    </ul>
    <div v-if="runs.data.value?.total" class="pagination">
      <button
        :disabled="runsPage <= 1 || runs.loading.value"
        @click="runsPage--"
      >
        上一页分析
      </button>
      <span>共 {{ runs.data.value.total }} 轮分析</span>
      <button
        :disabled="runsPage * 10 >= runs.data.value.total || runs.loading.value"
        @click="runsPage++"
      >
        下一页分析
      </button>
    </div>
    <RequestError :error="evidence.error.value" />
    <div
      v-if="evidence.data.value && !evidence.error.value"
      data-testid="fix-evidence"
    >
      <p>
        {{ evidence.data.value.run.rule_version }} ·
        {{ evidence.data.value.total }} 个已判定提交；仅展示成功提交的批次。
      </p>
      <p
        v-if="evidence.data.value.run.history_coverage === 'recent_window'"
        class="hint"
      >
        此轮包含有限历史，不能代表完整仓库历史。
      </p>
      <ul class="repository-list">
        <li
          v-for="row in evidence.data.value.items"
          :key="row.id"
          data-testid="fix-assessment"
        >
          <code class="task-id">{{ row.sha }}</code>
          <p>
            {{ row.candidate ? "候选修复" : "未纳入候选" }} ·
            {{ reviewLabels[row.review_status] }}
          </p>
          <p v-if="!row.content_complete" class="hint">
            存在略过或不可用内容，后续行级分析需要检查。
          </p>
          <p v-if="!row.evidence.length">未发现证据，不能据此判定干净提交。</p>
          <ul>
            <li v-for="(item, index) in row.evidence" :key="index">
              {{ confidenceLabels[item.confidence] }}等级 · {{ item.type }}：{{
                item.value
              }}<small v-if="item.source.status"
                >Issue 观测：{{ item.source.status }} ·
                {{ item.source.observed_at }}</small
              >
            </li>
          </ul>
          <p v-if="row.review_note">复核理由：{{ row.review_note }}</p>
          <details v-if="row.review_history.length">
            <summary>复核记录（最近20次）</summary>
            <p v-for="item in row.review_history" :key="item.revision">
              第 {{ item.revision }} 次 · {{ reviewLabels[item.status] }}：{{
                item.note
              }}
            </p>
          </details>
          <div v-if="canWrite">
            <label :for="'fix-note-' + row.id">复核理由</label>
            <input
              :id="'fix-note-' + row.id"
              v-model="notes[row.id]"
              maxlength="300"
              :disabled="!!reviewing"
            />
            <div class="pagination">
              <button :disabled="!!reviewing" @click="review(row, 'confirmed')">
                确认修复
              </button>
              <button :disabled="!!reviewing" @click="review(row, 'rejected')">
                拒绝候选
              </button>
              <button
                :disabled="!!reviewing"
                @click="review(row, 'unreviewed')"
              >
                恢复待复核
              </button>
            </div>
          </div>
        </li>
      </ul>
      <div class="pagination">
        <button
          :disabled="evidencePage <= 1 || evidence.loading.value"
          @click="evidencePage--"
        >
          上一页证据
        </button>
        <span>第 {{ evidencePage }} 页</span>
        <button
          :disabled="
            evidencePage * 10 >= evidence.data.value.total ||
            evidence.loading.value
          "
          @click="evidencePage++"
        >
          下一页证据
        </button>
      </div>
    </div>
  </section>
</template>
