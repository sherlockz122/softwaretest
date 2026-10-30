<script setup>
import { computed, ref, watch } from "vue";
import { client } from "../api";
import { useLatest } from "../latest";
import { operationKeys, date, shortId } from "../task-tools";
import {
  confidenceNames,
  fixReview,
  label,
  szzAlgorithms,
  szzReason,
  szzResult,
  szzStatus,
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
const sending = ref(false);
const failure = ref(null);
const keys = operationKeys();
const runs = useLatest((input, signal) =>
  client.request(
    `/repositories/${input.id}/szz-runs?page=${input.page}&page_size=10`,
    { signal },
  ),
);
// 默认只看真正追过的提交；不是疑似修复、按规则跳过的收在「全部」里。
const mode = ref("traced");
const results = useLatest((input, signal) =>
  client.request(
    `/szz-runs/${input.run}/results?page=${input.page}&page_size=10&attention=${input.attention}`,
    { signal },
  ),
);
function skippedItem(item) {
  return item.result?.status === "not_candidate";
}
// 点开某条结果的代码行线索，就地问后端要这一条的链接，不再靠下面的总列表翻。
const expanded = ref("");
const itemLinks = ref({ loading: false, items: [], error: null });
async function toggleLinks(item) {
  if (expanded.value === item.id) {
    expanded.value = "";
    return;
  }
  expanded.value = item.id;
  itemLinks.value = { loading: true, items: [], error: null };
  try {
    const data = await client.request(
      `/szz-runs/${selected.value}/links?page=1&page_size=50&item_id=${item.id}`,
    );
    if (expanded.value === item.id) itemLinks.value.items = data.items;
  } catch (error) {
    if (expanded.value === item.id) itemLinks.value.error = error;
  } finally {
    if (expanded.value === item.id) itemLinks.value.loading = false;
  }
}
function switchMode(value) {
  if (mode.value === value) return;
  mode.value = value;
  page.value = 1;
}
const run = computed(() => results.data.value?.run);
function reasons(list) {
  return (list || []).map((reason) => label(szzReason, reason)).join("；");
}
const canStart = computed(
  () =>
    props.canWrite &&
    props.repo.fix_status === "detected" &&
    props.repo.fix_run?.head_sha === props.repo.head_sha,
);
async function refresh() {
  const id = props.repo.id;
  await runs.load({ id, page: runsPage.value });
  if (id !== props.repo.id) return;
  if (!selected.value)
    selected.value =
      props.repo.szz_run?.root_task_id ||
      runs.data.value?.items[0]?.root_task_id ||
      "";
  if (selected.value)
    await results.load({
      run: selected.value,
      page: page.value,
      attention: mode.value === "traced",
    });
}
defineExpose({ refresh });
watch(
  () => props.repo.id,
  () => {
    selected.value = "";
    runsPage.value = page.value = 1;
    failure.value = null;
    runs.clear();
    results.clear();
  },
  { immediate: true },
);
watch(
  () => [
    props.repo.id,
    props.repo.szz_run?.root_task_id,
    props.repo.szz_run?.processed,
    runsPage.value,
  ],
  refresh,
  { immediate: true },
);
watch(
  () => [selected.value, page.value, mode.value],
  () => {
    if (selected.value) {
      results.load({
        run: selected.value,
        page: page.value,
        attention: mode.value === "traced",
      });
    }
  },
);
function select(value) {
  selected.value = value;
  page.value = 1;
}
async function start() {
  if (sending.value) return;
  const id = props.repo.id;
  const previous = props.repo.szz_run?.root_task_id;
  const body = {
    fix_run_id: props.repo.fix_run.root_task_id,
    algorithm_version: "baseline-szz-v1",
    as_of: null,
  };
  const input = JSON.stringify([id, props.repo.head_sha, previous, body]);
  sending.value = true;
  failure.value = null;
  try {
    const result = await client.request(`/repositories/${id}/szz-runs`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": keys.get("szz", input),
      },
      body: JSON.stringify(body),
    });
    keys.done("szz", input);
    if (id === props.repo.id) select(result.task_id);
  } catch (error) {
    if (id === props.repo.id) failure.value = error;
  } finally {
    sending.value = false;
    await props.reload();
    if (id === props.repo.id) {
      const accepted = props.repo.szz_run?.root_task_id;
      if (accepted && accepted !== previous) {
        keys.done("szz", input);
        select(accepted);
        failure.value = null;
      }
      await refresh();
    }
  }
}
</script>
<template>
  <section data-testid="szz-panel">
    <h2>第三步：追查缺陷是哪次改动引入的</h2>
    <p data-testid="szz-status">
      {{ label(szzStatus, repo.szz_status) }}
    </p>
    <p class="hint">
      从第二步找出的修复提交出发往回翻：这次被删掉或改掉的代码，最早是哪次提交写进来的。翻不到的时候会写明原因（历史太短、文件太大等），不会当成「肯定不是缺陷」。
    </p>
    <button
      v-if="canStart"
      class="primary"
      :disabled="busy || sending"
      @click="start"
    >
      {{ sending ? "提交中…" : "开始追查缺陷引入" }}
    </button>
    <p v-else-if="!canWrite" class="hint">
      Viewer（只读账号）可以查看追溯结果；开始追溯需要 Member 或
      Admin 账号（普通成员或管理员）。
    </p>
    <p v-else class="hint">
      先完成第二步：当前最新提交的疑似修复识别还没做完。
    </p>
    <RequestError
      :error="failure || runs.error.value || results.error.value"
    />
    <p
      v-if="runs.loading.value || results.loading.value"
      role="status"
    >
      正在读取追溯结果…
    </p>
    <ul
      v-if="runs.data.value?.items.length"
      class="task-list"
      data-testid="szz-runs"
    >
      <li v-for="item in runs.data.value.items" :key="item.root_task_id">
        <p class="task-id">{{ shortId(item.root_task_id) }}</p>
        <p>
          {{ date(item.created_at) }} · 已追查 {{ item.processed }} /
          {{ item.total ?? "待扫描" }} 个提交
        </p>
        <button
          :disabled="selected === item.root_task_id"
          @click="select(item.root_task_id)"
        >
          查看这一轮追溯
        </button>
      </li>
    </ul>
    <div v-if="runs.data.value?.total" class="pagination">
      <button :disabled="runsPage <= 1" @click="runsPage--">
        上一页追溯</button
      ><span>{{ runsPage }}</span
      ><button
        :disabled="runsPage * 10 >= runs.data.value.total"
        @click="runsPage++"
      >
        下一页追溯
      </button>
    </div>
    <p v-else-if="!runs.loading.value">还没有做过追溯。</p>
    <div v-if="run" data-testid="szz-results">
      <dl class="task-facts">
        <dt>追溯方法</dt>
        <dd>{{ label(szzAlgorithms, run.algorithm_version) }}</dd>
        <dt>基于哪一轮修复识别</dt>
        <dd class="task-id">{{ shortId(run.fix_root_task_id) }}</dd>
        <dt>只看到这个时间为止</dt>
        <dd>{{ date(run.as_of) }}</dd>
        <dt>覆盖的历史范围</dt>
        <dd>
          {{
            run.history_coverage === "full"
              ? "仓库全部可追溯的历史"
              : "只有最近一段历史"
          }}
        </dd>
        <dt>结果版本</dt>
        <dd class="task-id">{{ shortId(run.label_version) }}</dd>
      </dl>
      <div class="filters">
        <button
          :class="['chip', { on: mode === 'traced' }]"
          @click="switchMode('traced')"
        >
          已追溯（{{ results.data.value?.counts?.traced ?? 0 }}）
        </button>
        <button
          :class="['chip', { on: mode === 'all' }]"
          @click="switchMode('all')"
        >
          全部（{{ results.data.value?.counts?.total ?? 0 }}）
        </button>
      </div>
      <p class="hint">
        {{
          mode === "traced"
            ? "这里只列传回来的结果；不是疑似修复、按规则跳过的提交收在「全部」里。"
            : "全部提交都在这里；跳过的不展开，只显示一行。"
        }}
      </p>
      <ul class="task-list">
        <li
          v-for="item in results.data.value?.items"
          :key="item.id"
          data-testid="szz-result"
        >
          <p v-if="skippedItem(item)" class="szz-skipped">
            <span class="task-id">{{ shortId(item.sha) }}</span>
            跳过：这条提交不是疑似修复，没有可往回翻的代码
          </p>
          <template v-else>
            <p class="task-id">{{ shortId(item.sha) }}</p>
            <p v-if="item.input.review_status !== 'unreviewed'">
              追溯时的人工复核：{{
                label(fixReview, item.input.review_status)
              }}<span v-if="item.input.review_revision > 0"
                >（第 {{ item.input.review_revision }} 次修改后）</span
              >
            </p>
            <p>
              追溯结果：{{
                item.result ? label(szzResult, item.result.status) : "待处理"
              }}
              <button
                v-if="item.links_count"
                class="link-button"
                @click="toggleLinks(item)"
              >
                {{
                  expanded === item.id
                    ? "收起代码行线索"
                    : `查看 ${item.links_count} 条代码行线索`
                }}
              </button>
            </p>
            <div v-if="expanded === item.id" class="szz-links-inline">
              <p v-if="itemLinks.loading">正在读取代码行线索…</p>
              <RequestError :error="itemLinks.error" />
              <ul v-if="itemLinks.items.length">
                <li v-for="(link, i) in itemLinks.items" :key="i">
                  {{ link.path }} 第 {{ link.fixed_line }} 行 ←
                  {{ shortId(link.blamed_sha) }} 第 {{ link.blamed_line }} 行
                  <span v-if="link.eligible"
                    >（可用作「引入缺陷」的线索，把握：{{
                      label(confidenceNames, link.confidence)
                    }}）</span
                  >
                  <span v-else
                    >（不能用作标签：{{ label(szzReason, link.reason) }}）</span
                  >
                </li>
              </ul>
            </div>
            <p v-if="item.result?.reasons.length">
              原因：{{ reasons(item.result.reasons) }}
            </p>
          </template>
        </li>
      </ul>
      <p
        v-if="!results.loading.value && !results.data.value?.items.length"
        class="hint"
      >
        {{
          mode === "traced" ? "这一轮没有追溯出结果。" : "这一轮没有可显示的提交。"
        }}
      </p>
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
