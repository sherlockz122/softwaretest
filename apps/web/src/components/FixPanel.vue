<script setup>
import { computed, ref, watch } from "vue";
import { client } from "../api";
import { useLatest } from "../latest";
import { operationKeys, date, shortId } from "../task-tools";
import {
  changeType,
  confidenceNames,
  fixEvidenceType,
  fixExclusion,
  fixReview,
  fixStatus,
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
const runsPage = ref(1);
const evidencePage = ref(1);
const selectedRun = ref("");
const includeMedium = ref(true);
const sending = ref(false);
const reviewing = ref("");
const failure = ref(null);
const notes = ref({});
// 列表默认只看「还需要人看」的行，避免在几百条排除项里翻页。
const mode = ref("attention");
const detail = ref(null);
const dialog = ref(null);
const keys = operationKeys();
const runs = useLatest((input, signal) =>
  client.request(
    `/repositories/${input.id}/fix-runs?page=${input.page}&page_size=10`,
    { signal },
  ),
);
const evidence = useLatest((input, signal) =>
  client.request(
    `/repositories/${input.id}/fix-runs/${input.run}/evidence?page=${input.page}&page_size=10&attention=${input.attention}`,
    { signal },
  ),
);
// 排除类证据没有「把握」可言：它说的是这条提交按规则不进入候选，
// 不是「我们不确定」。所以判定和证据两处都要分开表述。
function verdict(row) {
  if (row.review_status === "confirmed") return "已确认是修复";
  if (row.review_status === "rejected") return "已否定这条候选";
  if (row.candidate) return "疑似是修复";
  if (row.disposition === "excluded") return "已按规则排除，不算疑似修复";
  if (row.disposition === "unknown") return "内容读不出来，无法判断";
  return "看起来不是修复";
}
async function openDetail(row) {
  detail.value = { sha: row.sha, loading: true, items: [], total: 0, error: null };
  dialog.value?.showModal();
  try {
    const data = await client.request(
      `/repositories/${props.repo.id}/commits/${row.sha}/files?page=1&page_size=50`,
    );
    if (detail.value?.sha !== row.sha) return;
    detail.value.items = data.items;
    detail.value.total = data.total;
  } catch (error) {
    if (detail.value?.sha === row.sha) detail.value.error = error;
  } finally {
    if (detail.value?.sha === row.sha) detail.value.loading = false;
  }
}
function closeDetail() {
  dialog.value?.close();
  detail.value = null;
}
function evidenceText(item) {
  const value =
    item.type === "exclusion"
      ? label(fixExclusion, item.value)
      : item.value ?? "";
  return `${label(fixEvidenceType, item.type)}：${value}`;
}
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
      attention: mode.value === "attention",
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
  () => [selectedRun.value, evidencePage.value, mode.value],
  () => {
    if (selectedRun.value)
      evidence.load({
        id: props.repo.id,
        run: selectedRun.value,
        page: evidencePage.value,
        attention: mode.value === "attention",
      });
  },
);
function select(run) {
  selectedRun.value = run.root_task_id;
  evidencePage.value = 1;
  failure.value = null;
}
function switchMode(value) {
  if (mode.value === value) return;
  mode.value = value;
  evidencePage.value = 1;
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
    <h2>第二步：找出疑似修复的提交</h2>
    <p data-testid="fix-status">{{ label(fixStatus, repo.fix_status) }}</p>
    <p class="hint">
      这一步按提交说明里的关键词、以及关联的缺陷单，找出看起来是在修 bug 的提交，并为每条判断留下依据。没有找到依据，只说明这次没找到证据，不能断定这条提交没有问题。
    </p>
    <form v-if="canStart" class="create-task" @submit.prevent="start">
      <label
        ><input
          v-model="includeMedium"
          type="checkbox"
          :disabled="sending || busy"
        />中等把握的提交也算作疑似修复</label
      >
      <button class="primary" :disabled="sending || busy">
        {{ sending ? "提交中…" : "开始找出疑似修复的提交" }}
      </button>
    </form>
    <p v-if="!canWrite" class="hint">
      Viewer（只读账号）可以查看判断依据；开始识别和人工复核需要 Member 或
      Admin 账号（普通成员或管理员）。
    </p>
    <p v-if="['failed', 'cancelled'].includes(repo.fix_status)" class="hint">
      这次没跑完，已有的识别结果还留着；可以直接重试，新一轮会保留之前的结果。
    </p>
    <RequestError :error="failure" />
    <RequestError :error="runs.error.value" />
    <ul
      v-if="runs.data.value && !runs.error.value"
      class="repository-list"
      data-testid="fix-runs"
    >
      <li v-for="run in runs.data.value.items" :key="run.root_task_id">
        <p class="task-id">{{ shortId(run.head_sha) || "空历史" }}</p>
        <small
          >规则版本 {{ label(ruleVersions, run.rule_version) }} · {{
            run.include_medium ? "中等把握也算" : "只算有把握的"
          }}
          · 已检查 {{ run.processed }} / {{ run.total ?? "待扫描" }} 个提交 ·
          {{ date(run.created_at) }}</small
        >
        <button @click="select(run)">
          {{
            selectedRun === run.root_task_id
              ? "正在查看这一轮"
              : "查看这一轮的判断"
          }}
        </button>
      </li>
    </ul>
    <div v-if="runs.data.value?.total" class="pagination">
      <button
        :disabled="runsPage <= 1 || runs.loading.value"
        @click="runsPage--"
      >
        上一页识别
      </button>
      <span>共 {{ runs.data.value.total }} 轮识别</span>
      <button
        :disabled="runsPage * 10 >= runs.data.value.total || runs.loading.value"
        @click="runsPage++"
      >
        下一页识别
      </button>
    </div>
    <RequestError :error="evidence.error.value" />
    <div
      v-if="evidence.data.value && !evidence.error.value"
      data-testid="fix-evidence"
    >
      <p>
        规则版本 {{ evidence.data.value.run.rule_version }} · 这一轮判定了
        {{ evidence.data.value.counts?.total ?? evidence.data.value.total }}
        个提交；识别还在进行时，这里只显示已经判完的部分。
      </p>
      <p
        v-if="evidence.data.value.run.history_coverage === 'recent_window'"
        class="hint"
      >
        这一轮只覆盖了最近一段历史，不代表整个仓库的完整历史。
      </p>
      <div class="filters">
        <button
          :class="['chip', { on: mode === 'attention' }]"
          @click="switchMode('attention')"
        >
          待复核（{{ evidence.data.value.counts?.needs_review ?? 0 }}）
        </button>
        <button
          :class="['chip', { on: mode === 'all' }]"
          @click="switchMode('all')"
        >
          全部（{{ evidence.data.value.counts?.total ?? 0 }}）
        </button>
      </div>
      <p class="hint">
        {{ mode === "attention"
          ? "这里只列还需要人工看一眼的提交；按规则排除的和已经复核过的在「全部」里。"
          : "全部判定都在这里；待复核的排在最前面。" }}
      </p>
      <ul class="repository-list">
        <li
          v-for="row in evidence.data.value.items"
          :key="row.id"
          data-testid="fix-assessment"
        >
          <code class="task-id" :title="row.sha">{{ shortId(row.sha) }}</code>
          <p>
            {{ verdict(row) }}
            <button class="link-button" @click="openDetail(row)">
              查看这次提交改了什么
            </button>
          </p>
          <p v-if="!row.content_complete" class="hint">
            这条提交里有内容没读到（文件太大或格式不支持），后面的追溯可能不完整。
          </p>
          <p v-if="!row.evidence.length">
            没有找到修复依据；但没找到不等于这条提交没有问题。
          </p>
          <ul>
            <li v-for="(item, index) in row.evidence" :key="index">
              <template v-if="item.type === 'exclusion'">{{
                evidenceText(item)
              }}</template>
              <template v-else
                >{{ label(confidenceNames, item.confidence) }}把握 ·
                {{ evidenceText(item) }}</template
              ><small v-if="item.source.status"
                >缺陷单状态：{{ item.source.status }} · 记录时间
                {{ item.source.observed_at }}</small
              >
            </li>
          </ul>
          <p v-if="row.review_note">复核理由：{{ row.review_note }}</p>
          <details v-if="row.review_history.length">
            <summary>复核历史（最近 20 次）</summary>
            <p v-for="item in row.review_history" :key="item.revision">
              第 {{ item.revision }} 次 · {{ label(fixReview, item.status) }}：{{
                item.note
              }}
            </p>
          </details>
          <div v-if="canWrite">
            <p v-if="row.disposition === 'excluded'" class="hint">
              这类提交按规则排除，通常不需要复核。规则判断错了才需要改。
            </p>
            <details :open="row.disposition !== 'excluded'">
              <summary>
                {{
                  row.disposition === "excluded"
                    ? "这条判断不对？手动纠正"
                    : "人工复核"
                }}
              </summary>
              <label :for="'fix-note-' + row.id">复核理由</label>
              <input
                :id="'fix-note-' + row.id"
                v-model="notes[row.id]"
                maxlength="300"
                :disabled="!!reviewing"
              />
              <div class="pagination">
                <button :disabled="!!reviewing" @click="review(row, 'confirmed')">
                  确认是修复
                </button>
                <button :disabled="!!reviewing" @click="review(row, 'rejected')">
                  否定这条候选
                </button>
                <button
                  :disabled="!!reviewing"
                  @click="review(row, 'unreviewed')"
                >
                  改回未复核
                </button>
              </div>
            </details>
          </div>
        </li>
      </ul>
      <p
        v-if="!evidence.loading.value && !evidence.data.value.items.length"
        class="hint"
      >
        {{
          mode === "attention"
            ? "没有待复核的提交了。"
            : "这一轮没有可显示的提交。"
        }}
      </p>
      <div v-if="evidence.data.value.total > 10" class="pagination">
        <button
          :disabled="evidencePage <= 1 || evidence.loading.value"
          @click="evidencePage--"
        >
          上一页
        </button>
        <span>第 {{ evidencePage }} 页</span>
        <button
          :disabled="
            evidencePage * 10 >= evidence.data.value.total ||
            evidence.loading.value
          "
          @click="evidencePage++"
        >
          下一页
        </button>
      </div>
    </div>
    <dialog ref="dialog" class="fix-detail" @close="detail = null">
      <div v-if="detail">
        <div class="dialog-head">
          <strong>提交 {{ shortId(detail.sha) }} 改了什么</strong>
          <button @click="closeDetail">关闭</button>
        </div>
        <p v-if="detail.loading" role="status">正在读取文件变更…</p>
        <RequestError :error="detail.error" />
        <ul v-if="detail.items.length" class="repository-list">
          <li v-for="file in detail.items" :key="file.ordinal">
            <p class="task-id">
              {{ file.old_path ?? "（新增）" }} →
              {{ file.new_path ?? "（删除）" }}
            </p>
            <small
              >{{ label(changeType, file.change_type) }} · 新增
              {{ file.insertions ?? "—" }} / 删除
              {{ file.deletions ?? "—" }} · 改前文件行数
              {{ file.old_loc ?? "—" }}</small
            >
          </li>
        </ul>
        <p v-else-if="!detail.loading && !detail.error">
          这条提交没有可显示的文件变更，或者内容没有读进来。
        </p>
        <p v-if="detail.total > detail.items.length" class="hint">
          共 {{ detail.total }} 个文件，这里只显示前 {{ detail.items.length }} 个。
        </p>
      </div>
    </dialog>
  </section>
</template>
