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
const statusLabels = {
  pending: "未追溯",
  queued: "追溯排队中",
  tracing: "追溯中",
  traced: "追溯完成",
  failed: "追溯失败",
  cancelled: "追溯已取消",
};
const resultLabels = {
  unknown: "未知",
  partial: "部分追溯",
  traced: "已追溯",
  no_traceable_lines: "无可追溯旧代码行",
  not_candidate: "冻结输入非候选",
};
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
    <h2>Baseline SZZ 缺陷引入追溯</h2>
    <p data-testid="szz-status">
      {{ statusLabels[repo.szz_status] || "未追溯" }}
    </p>
    <p class="hint">
      冻结当前 Fix
      轮次与复核版本，在唯一父提交追溯删除或替换行。候选缺陷引入证据保留可用时间；未知或无证据均不表示无缺陷。
    </p>
    <button
      v-if="canStart"
      class="primary"
      :disabled="busy || sending"
      @click="start"
    >
      {{ sending ? "提交中…" : "运行 SZZ 追溯" }}
    </button>
    <p v-else-if="!canWrite" class="hint">
      Viewer 可查看 SZZ 证据；启动需要 Member 或 Admin。
    </p>
    <p v-else class="hint">请先完成当前接受 HEAD 的 Fix 识别。</p>
    <RequestError
      :error="
        failure || runs.error.value || results.error.value || links.error.value
      "
    />
    <p
      v-if="runs.loading.value || results.loading.value || links.loading.value"
      role="status"
    >
      正在读取 SZZ 结果…
    </p>
    <ul
      v-if="runs.data.value?.items.length"
      class="task-list"
      data-testid="szz-runs"
    >
      <li v-for="item in runs.data.value.items" :key="item.root_task_id">
        <span class="task-id">{{ item.root_task_id }}</span
        ><span
          >{{ date(item.created_at) }} · {{ item.processed }} /
          {{ item.total ?? "待扫描" }}</span
        ><button
          :disabled="selected === item.root_task_id"
          @click="select(item.root_task_id)"
        >
          查看 SZZ 轮次
        </button>
      </li>
    </ul>
    <div v-if="runs.data.value?.total" class="pagination">
      <button :disabled="runsPage <= 1" @click="runsPage--">
        SZZ 轮次上一页</button
      ><span>{{ runsPage }}</span
      ><button
        :disabled="runsPage * 10 >= runs.data.value.total"
        @click="runsPage++"
      >
        SZZ 轮次下一页
      </button>
    </div>
    <p v-else-if="!runs.loading.value">尚无 SZZ 轮次。</p>
    <div v-if="run" data-testid="szz-results">
      <dl class="task-facts">
        <dt>算法</dt>
        <dd>{{ run.algorithm_version }}</dd>
        <dt>Fix 轮次</dt>
        <dd class="task-id">{{ run.fix_root_task_id }}</dd>
        <dt>观察截止</dt>
        <dd>{{ date(run.as_of) }}</dd>
        <dt>历史覆盖</dt>
        <dd>
          {{
            run.history_coverage === "full" ? "全部可达历史" : "有限历史窗口"
          }}
        </dd>
        <dt>标签版本</dt>
        <dd class="task-id">{{ run.label_version }}</dd>
      </dl>
      <ul class="task-list">
        <li
          v-for="item in results.data.value?.items"
          :key="item.id"
          data-testid="szz-result"
        >
          <span class="task-id">{{ item.sha }}</span
          ><span
            >复核版本 {{ item.input.review_revision }} ·
            {{ item.input.review_status }}</span
          ><span
            >{{ item.result ? resultLabels[item.result.status] : "待处理" }} ·
            {{ item.links_count }} 条行证据</span
          ><span v-if="item.result?.reasons.length">{{
            item.result.reasons.join("，")
          }}</span>
        </li>
      </ul>
      <div class="pagination">
        <button :disabled="page <= 1" @click="page--">SZZ 结果上一页</button
        ><span>{{ page }}</span
        ><button
          :disabled="page * 10 >= (results.data.value?.total || 0)"
          @click="page++"
        >
          SZZ 结果下一页
        </button>
      </div>
      <h3>行级证据</h3>
      <ul class="task-list" data-testid="szz-links">
        <li v-for="(item, index) in links.data.value?.items" :key="index">
          <span>旧路径 {{ item.path }} · 删除行 {{ item.fixed_line }}</span
          ><span class="task-id">Fix {{ item.fix_sha }}</span
          ><span class="task-id"
            >来源 {{ item.blamed_sha }} · 原始行 {{ item.blamed_line }}</span
          ><span>来源路径 {{ item.origin_path }}</span
          ><span
            >{{ item.eligible ? "候选缺陷引入" : "未知" }} ·
            {{ item.reason || item.confidence }}</span
          ><span>标签可用时间 {{ date(item.label_available_at) }}</span>
        </li>
      </ul>
      <p v-if="!links.data.value?.total">
        本轮没有已发布的行级证据；不能据此生成 clean 标签。
      </p>
      <div class="pagination">
        <button :disabled="linksPage <= 1" @click="linksPage--">
          SZZ 证据上一页</button
        ><span>{{ linksPage }}</span
        ><button
          :disabled="linksPage * 10 >= (links.data.value?.total || 0)"
          @click="linksPage++"
        >
          SZZ 证据下一页
        </button>
      </div>
    </div>
  </section>
</template>
