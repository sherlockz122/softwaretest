<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { active, operationKeys, date } from "../task-tools";
import RequestError from "../components/RequestError.vue";
const route = useRoute();
const repo = useLatest((id, signal) =>
  client.request("/repositories/" + encodeURIComponent(id), { signal }),
);
const page = ref(1);
const commits = useLatest((input, signal) =>
  client.request(
    `/repositories/${encodeURIComponent(input.id)}/commits?page=${input.page}&page_size=10`,
    { signal },
  ),
);
const parsing = ref(false);
const syncing = ref(false);
const syncFailure = ref(null);
const syncPage = ref(1);
const windows = useLatest((input, signal) =>
  client.request(
    `/repositories/${encodeURIComponent(input.id)}/sync-windows?page=${input.page}&page_size=10`,
    { signal },
  ),
);
const failure = ref(null);
const commitLimit = ref("");
const selected = ref(null);
const filePage = ref(1);
const files = useLatest((input, signal) =>
  client.request(
    `/repositories/${encodeURIComponent(input.id)}/commits/${input.sha}/files?page=${input.page}&page_size=20`,
    { signal },
  ),
);
function selectCommit(commit) {
  selected.value = commit;
  filePage.value = 1;
  files.load({ id: route.params.id, sha: commit.sha, page: filePage.value });
}
async function refresh() {
  const id = route.params.id;
  const queries = [
    repo.load(id),
    commits.load({ id, page: page.value }),
    windows.load({ id, page: syncPage.value }),
  ];
  if (selected.value)
    queries.push(
      files.load({ id, sha: selected.value.sha, page: filePage.value }),
    );
  await Promise.all(queries);
}
watch(filePage, (value) => {
  if (selected.value)
    files.load({ id: route.params.id, sha: selected.value.sha, page: value });
});
const keys = operationKeys();
const canParse = computed(() =>
  ["Member", "Admin"].includes(client.state.user?.role),
);
const parseLabels = {
  pending: "未开始",
  queued: "排队中",
  parsing: "解析中",
  parsed: "解析完成",
  failed: "解析失败",
  cancelled: "已取消",
};
const syncLabels = {
  pending: "未同步",
  queued: "同步排队中",
  syncing: "同步中",
  synced: "同步完成",
  requires_review: "历史变化待复核",
  failed: "同步失败",
  cancelled: "同步已取消",
};
const relationLabels = {
  pending: "等待新快照",
  initial: "空历史后的首次提交",
  unchanged: "HEAD 未变化",
  fast_forward: "新增可达提交",
  requires_review: "祖先关系或默认分支变化",
};
async function startSync() {
  if (syncing.value) return;
  const id = route.params.id;
  const previous = repo.data.value.sync_window?.root_task_id;
  const input = JSON.stringify([id, repo.data.value.head_sha, previous]);
  syncing.value = true;
  syncFailure.value = null;
  try {
    await client.request(`/repositories/${encodeURIComponent(id)}/sync`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": keys.get("sync", input),
      },
      body: "{}",
    });
    keys.done("sync", input);
  } catch (error) {
    if (id === route.params.id) syncFailure.value = error;
  } finally {
    if (id === route.params.id) {
      await repo.load(id);
      if (
        syncFailure.value?.code === "NETWORK_UNAVAILABLE" &&
        repo.data.value?.sync_window?.root_task_id !== previous &&
        repo.data.value?.sync_window
      )
        syncFailure.value = null;
    }
    syncing.value = false;
  }
}
const contentLabels = {
  parsed: "已解析",
  binary: "二进制内容略过",
  blob_limit: "文件超过内容上限",
  diff_limit: "diff 超过保留上限",
  commit_diff_limit: "提交超过内容预算",
  encoding: "编码不兼容",
  submodule: "子模块内容略过",
  ambiguous_patch: "跨文件 patch 略过",
  merge_skipped: "合并 diff 略过",
  message_encoding: "消息编码异常",
};
async function startParse() {
  if (parsing.value) return;
  parsing.value = true;
  failure.value = null;
  const id = route.params.id;
  const limit = commitLimit.value === "" ? null : Number(commitLimit.value);
  const input = JSON.stringify([id, limit]);
  try {
    await client.request(`/repositories/${encodeURIComponent(id)}/parse`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": keys.get("parse", input),
      },
      body: JSON.stringify({ commit_limit: limit }),
    });
    keys.done("parse", input);
    if (id === route.params.id) await repo.load(id);
  } catch (error) {
    if (id === route.params.id) failure.value = error;
  } finally {
    // A lost POST response can still have created a task. Refresh the repository
    // so polling follows its current task instead of the completed clone.
    if (id === route.params.id) await repo.load(id);
    parsing.value = false;
  }
}
watch(
  () => route.params.id,
  () => {
    page.value = 1;
    failure.value = null;
    syncFailure.value = null;
    syncPage.value = 1;
    selected.value = null;
  },
);
watch(
  () => [
    route.params.id,
    page.value,
    repo.data.value?.parse_window?.processed,
    repo.data.value?.sync_window?.processed,
  ],
  () => commits.load({ id: route.params.id, page: page.value }),
  { immediate: true },
);
watch(
  () => [
    route.params.id,
    syncPage.value,
    repo.data.value?.sync_window?.root_task_id,
    repo.data.value?.sync_window?.processed,
  ],
  () => windows.load({ id: route.params.id, page: syncPage.value }),
  { immediate: true },
);
const labels = {
  queued: "排队中",
  cloning: "克隆中",
  cloned: "已克隆",
  failed: "失败",
  cancelled: "已取消",
};
watch(
  () => route.params.id,
  (id) => repo.load(id),
  { immediate: true },
);
const poll = setInterval(() => {
  if (
    !document.hidden &&
    !repo.loading.value &&
    !client.state.busy &&
    (!repo.data.value || active.has(repo.data.value.task.status))
  )
    repo.load(route.params.id);
}, 2000);
onUnmounted(() => clearInterval(poll));
</script>
<template>
  <section>
    <RouterLink to="/repositories">← 返回仓库目录</RouterLink>
    <div class="section-heading">
      <h1>仓库详情</h1>
      <button :disabled="repo.loading.value" @click="refresh">刷新详情</button>
    </div>
    <RequestError :error="repo.error.value" />
    <p v-if="repo.loading.value" role="status">正在读取仓库…</p>
    <div
      v-if="repo.data.value && !repo.error.value"
      data-testid="repository-detail"
    >
      <p class="task-id">{{ repo.data.value.url }}</p>
      <p data-testid="repository-status">
        {{ labels[repo.data.value.status] || repo.data.value.status }}
      </p>
      <dl class="task-facts">
        <dt>默认分支</dt>
        <dd>{{ repo.data.value.default_branch ?? "—" }}</dd>
        <dt>HEAD</dt>
        <dd class="task-id" data-testid="repository-head">
          {{ repo.data.value.head_sha ?? "—" }}
        </dd>
        <dt>仓库大小</dt>
        <dd>{{ (repo.data.value.size_bytes / 1024 / 1024).toFixed(2) }} MiB</dd>
      </dl>
      <p v-if="repo.data.value.parse_status === 'pending'">
        克隆阶段保存 Git 数据；当前尚未导入提交和文件变更记录。
      </p>
      <p data-testid="parse-status">
        {{ parseLabels[repo.data.value.parse_status] }}
      </p>
      <p v-if="repo.data.value.parse_window">
        已入库 {{ repo.data.value.parse_window.processed }} /
        {{ repo.data.value.parse_window.total ?? "待扫描" }} 个提交；
        {{
          repo.data.value.parse_window.commit_limit
            ? `最近 ${repo.data.value.parse_window.commit_limit} 个提交`
            : "全部可达历史"
        }}。
        {{ repo.data.value.parse_window.parser_version }}
      </p>
      <form
        v-if="
          canParse &&
          repo.data.value.status === 'cloned' &&
          repo.data.value.parse_status === 'pending'
        "
        class="create-task"
        @submit.prevent="startParse"
      >
        <div>
          <label for="commit-limit">提交窗口（留空为全部）</label
          ><input
            id="commit-limit"
            v-model="commitLimit"
            type="number"
            min="1"
            max="10000000"
            step="1"
            :disabled="parsing"
          />
        </div>
        <button class="primary" :disabled="parsing">
          {{ parsing ? "提交中…" : "开始解析提交" }}
        </button>
      </form>
      <p v-if="!canParse" class="hint">
        Viewer 可查看解析结果；启动解析需要 Member 或 Admin。
      </p>
      <RequestError v-if="!repo.data.value.parse_window" :error="failure" />
      <h2>增量同步</h2>
      <p data-testid="sync-status">
        {{ syncLabels[repo.data.value.sync_status] }}
      </p>
      <p
        v-if="repo.data.value.history_coverage === 'recent_window'"
        class="hint"
      >
        初次解析只选取最近 N
        个提交；增量同步不会补齐此前历史，后续特征和数据集需检查覆盖度。
      </p>
      <button
        v-if="
          canParse &&
          repo.data.value.parse_status === 'parsed' &&
          ['pending', 'synced'].includes(repo.data.value.sync_status)
        "
        class="primary"
        :disabled="syncing || active.has(repo.data.value.task.status)"
        @click="startSync"
      >
        {{ syncing ? "提交中…" : "同步默认分支" }}
      </button>
      <p
        v-if="repo.data.value.sync_status === 'requires_review'"
        role="alert"
        class="error"
      >
        默认分支或历史祖先关系发生变化，已暂停同步并保留旧
        HEAD、提交和新快照。后续需制定复核与恢复方案。
      </p>
      <p
        v-if="['failed', 'cancelled'].includes(repo.data.value.sync_status)"
        class="hint"
      >
        此前同步进度已保留，请在当前任务中重试。
      </p>
      <p v-if="!canParse" class="hint">启动同步需要 Member 或 Admin。</p>
      <RequestError :error="syncFailure" />
      <RequestError
        data-testid="sync-window-error"
        :error="windows.error.value"
      />
      <ul
        v-if="windows.data.value"
        class="repository-list"
        data-testid="sync-windows"
      >
        <li
          v-for="window in windows.data.value.items"
          :key="window.root_task_id"
        >
          <p>
            {{ relationLabels[window.relation] }} · 已入库
            {{ window.processed }} / {{ window.total ?? "待扫描" }} 个新提交
          </p>
          <p class="task-id">
            {{ window.base_head_sha ?? "空历史" }} →
            {{ window.head_sha ?? "待扫描或空历史" }}
          </p>
          <small
            >{{ window.parser_version }} · {{ date(window.created_at) }}</small
          >
        </li>
      </ul>
      <div v-if="windows.data.value?.total" class="pagination">
        <button
          :disabled="syncPage <= 1 || windows.loading.value"
          @click="syncPage--"
        >
          上一页同步
        </button>
        <span
          >共 {{ windows.data.value.total }} 个同步窗口，第
          {{ syncPage }} 页</span
        >
        <button
          :disabled="
            syncPage * 10 >= windows.data.value.total || windows.loading.value
          "
          @click="syncPage++"
        >
          下一页同步
        </button>
      </div>
      <p>
        <RouterLink :to="'/tasks/' + repo.data.value.task.id"
          >查看当前任务</RouterLink
        >，可查看进度、取消或重试。
      </p>
      <p v-if="repo.data.value.task.health === 'stale'" role="status">
        执行心跳已过期，等待协调器恢复。
      </p>
      <div v-if="repo.data.value.task.error" role="alert" class="error">
        <p>{{ repo.data.value.task.error.message }}</p>
        <small>错误编号：{{ repo.data.value.task.error.code }}</small>
        <small>请求编号：{{ repo.data.value.task.error.request_id }}</small>
      </div>
      <h2>提交记录</h2>
      <RequestError :error="commits.error.value" />
      <div
        v-if="commits.data.value && !commits.error.value"
        data-testid="commit-list"
      >
        <p>
          共
          {{ commits.data.value.total }}
          个已入库提交。解析运行或中断时，这里显示已成功提交的批次。
        </p>
        <ul class="repository-list">
          <li v-for="commit in commits.data.value.items" :key="commit.id">
            <code class="task-id">{{ commit.sha }}</code>
            <p>
              {{ commit.message || "（空提交消息）"
              }}{{ commit.message_truncated ? "…" : "" }}
            </p>
            <small
              >{{ commit.author.name_alias || "未命名作者" }} ·
              {{ date(commit.committer_time) }} · {{ commit.file_count }} 个文件
              · {{ commit.parent_count }} 个父提交 ·
              {{
                contentLabels[commit.parse_status] || commit.parse_status
              }}</small
            >
            <button @click="selectCommit(commit)">查看文件变更</button>
          </li>
        </ul>
        <div class="pagination">
          <button
            :disabled="page <= 1 || commits.loading.value"
            @click="page--"
          >
            上一页
          </button>
          <span>第 {{ page }} 页</span>
          <button
            :disabled="
              page * 10 >= commits.data.value.total || commits.loading.value
            "
            @click="page++"
          >
            下一页
          </button>
        </div>
        <section v-if="selected">
          <h3>文件变更</h3>
          <p class="task-id">{{ selected.sha }}</p>
          <p v-if="selected.parse_status === 'merge_skipped'">
            合并提交保留父提交关系；当前策略跳过其 diff。
          </p>
          <RequestError :error="files.error.value" />
          <ul
            v-if="files.data.value && !files.error.value"
            class="repository-list"
            data-testid="file-list"
          >
            <li v-for="file in files.data.value.items" :key="file.ordinal">
              <p class="task-id">
                {{ file.old_path ?? "（新增）" }} →
                {{ file.new_path ?? "（删除）" }}
              </p>
              <small
                >{{ file.change_type }} · 新增 {{ file.insertions ?? "—" }} /
                删除 {{ file.deletions ?? "—" }} · 原 LOC
                {{ file.old_loc ?? "—" }} ·
                {{
                  contentLabels[file.content_status] || file.content_status
                }}</small
              >
            </li>
          </ul>
          <div v-if="files.data.value" class="pagination">
            <button
              :disabled="filePage <= 1 || files.loading.value"
              @click="filePage--"
            >
              上一页文件</button
            ><span
              >共 {{ files.data.value.total }} 个文件，第
              {{ filePage }} 页</span
            >
            <button
              :disabled="
                filePage * 20 >= files.data.value.total || files.loading.value
              "
              @click="filePage++"
            >
              下一页文件
            </button>
          </div>
        </section>
      </div>
    </div>
  </section>
</template>
