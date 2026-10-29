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
const linksPage = ref(1);
const sending = ref(false);
const failure = ref(null);
const keys = operationKeys();
const runs = useLatest((input, signal) =>
  client.request(
    `/repositories/${input.id}/szz-runs?page=${input.page}&page_size=10`,
    { signal },
  ),
);
const results = useLatest((input, signal) =>
  client.request(
    `/szz-runs/${input.run}/results?page=${input.page}&page_size=10`,
    { signal },
  ),
);
const links = useLatest((input, signal) =>
  client.request(
    `/szz-runs/${input.run}/links?page=${input.page}&page_size=10`,
    { signal },
  ),
);
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
    await Promise.all([
      results.load({ run: selected.value, page: page.value }),
      links.load({ run: selected.value, page: linksPage.value }),
    ]);
}
defineExpose({ refresh });
watch(
  () => props.repo.id,
  () => {
    selected.value = "";
    runsPage.value = page.value = linksPage.value = 1;
    failure.value = null;
    runs.clear();
    results.clear();
    links.clear();
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
  () => [selected.value, page.value, linksPage.value],
  () => {
    if (selected.value) {
      results.load({ run: selected.value, page: page.value });
      links.load({ run: selected.value, page: linksPage.value });
    }
  },
);
function select(value) {
  selected.value = value;
  page.value = linksPage.value = 1;
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
      :error="
        failure || runs.error.value || results.error.value || links.error.value
      "
    />
    <p
      v-if="runs.loading.value || results.loading.value || links.loading.value"
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
      <ul class="task-list">
        <li
          v-for="item in results.data.value?.items"
          :key="item.id"
          data-testid="szz-result"
        >
          <p class="task-id">{{ shortId(item.sha) }}</p>
          <p>
            提交当时的人工复核：{{
              label(fixReview, item.input.review_status)
            }}<span v-if="item.input.review_revision > 0"
              >（第 {{ item.input.review_revision }} 次修改后）</span
            >
          </p>
          <p>
            追溯结果：{{
              item.result ? label(szzResult, item.result.status) : "待处理"
            }}
            · {{ item.links_count }} 条代码行线索
          </p>
          <p v-if="item.result?.reasons.length">
            原因：{{ reasons(item.result.reasons) }}
          </p>
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
      <h3>代码行线索</h3>
      <ul class="task-list" data-testid="szz-links">
        <li v-for="(item, index) in links.data.value?.items" :key="index">
          <p>文件 {{ item.path }} · 删掉了第 {{ item.fixed_line }} 行</p>
          <p>
            <span class="task-id">这次修复 {{ shortId(item.fix_sha) }}</span>
            ·
            <span class="task-id"
              >最早来自提交 {{ shortId(item.blamed_sha) }} 的第
              {{ item.blamed_line }} 行</span
            >
          </p>
          <p v-if="item.origin_path">原文件：{{ item.origin_path }}</p>
          <p>
            {{ item.eligible ? "可能是引入缺陷的提交" : "无法确定" }} ·
            {{
              item.reason
                ? label(szzReason, item.reason)
                : `把握：${label(confidenceNames, item.confidence)}`
            }} · 结果可用时间 {{ date(item.label_available_at) }}
          </p>
        </li>
      </ul>
      <p v-if="!links.data.value?.total">
        这一轮没有可用的代码行线索；这种情况下不能把这些提交当成「没有问题」。
      </p>
      <div class="pagination">
        <button :disabled="linksPage <= 1" @click="linksPage--">
          上一页线索</button
        ><span>{{ linksPage }}</span
        ><button
          :disabled="linksPage * 10 >= (links.data.value?.total || 0)"
          @click="linksPage++"
        >
          下一页线索
        </button>
      </div>
    </div>
  </section>
</template>
