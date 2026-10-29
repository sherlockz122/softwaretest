<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { active, operationKeys, date, shortId } from "../task-tools";
import {
  changeType,
  contentStatus,
  label,
  parseStatus,
  ruleVersions,
  syncRelation,
  syncStatus,
} from "../terms";
import RequestError from "../components/RequestError.vue";
import FixPanel from "../components/FixPanel.vue";
import SZZPanel from "../components/SZZPanel.vue";
import FeaturePanel from "../components/FeaturePanel.vue";
const fixPanel = ref(null);
const szzPanel = ref(null);
const featurePanel = ref(null);
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
// 提交窗口必须显式给出：默认填上限值，想解析全历史要另外勾选，不能让留空悄悄变成全量。
const commitLimit = ref("700");
const fullHistory = ref(false);
const scopeReady = computed(() => {
  if (fullHistory.value) return true;
  const value = Number(commitLimit.value);
  return Number.isInteger(value) && value >= 1 && value <= 700;
});
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
  await fixPanel.value?.refresh();
  await szzPanel.value?.refresh();
  await featurePanel.value?.refresh();
}
watch(filePage, (value) => {
  if (selected.value)
    files.load({ id: route.params.id, sha: selected.value.sha, page: value });
});
const keys = operationKeys();
const canParse = computed(() =>
  ["Member", "Admin"].includes(client.state.user?.role),
);
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
async function startParse() {
  if (parsing.value) return;
  parsing.value = true;
  failure.value = null;
  const id = route.params.id;
  const body = fullHistory.value
    ? { full_history: true }
    : { commit_limit: Number(commitLimit.value) };
  const input = JSON.stringify([id, body]);
  try {
    await client.request(`/repositories/${encodeURIComponent(id)}/parse`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": keys.get("parse", input),
      },
      body: JSON.stringify(body),
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
const repoStatus = {
  queued: "排队等待下载",
  cloning: "正在下载仓库",
  cloned: "仓库已下载",
  failed: "下载失败",
  cancelled: "下载已取消",
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
        <span :class="['badge', repo.data.value.status]">{{
          label(repoStatus, repo.data.value.status)
        }}</span>
      </p>
      <p class="hint">
        这个仓库的分析分四步，按下面的顺序做：先整理提交记录，再找出疑似修复的提交，然后追查缺陷是哪次改动引入的，最后计算每个提交的统计指标。
      </p>
      <dl class="task-facts">
        <dt>默认分支</dt>
        <dd>
          {{ repo.data.value.default_branch ?? "—"
          }}<small>下载和分析都以这个分支为准</small>
        </dd>
        <dt>最新提交</dt>
        <dd>
          <span
            class="task-id"
            data-testid="repository-head"
            :title="repo.data.value.head_sha ?? ''"
            >{{ shortId(repo.data.value.head_sha) || "—" }}</span
          >
          <small>仓库当前最新的那次提交，后面所有分析都以它为准</small>
        </dd>
        <dt>仓库大小</dt>
        <dd>{{ (repo.data.value.size_bytes / 1024 / 1024).toFixed(2) }} MB</dd>
      </dl>
      <h2>第一步：整理提交记录</h2>
      <p data-testid="parse-status">
        {{ label(parseStatus, repo.data.value.parse_status) }}
      </p>
      <p v-if="repo.data.value.parse_status === 'pending'" class="hint">
        下载只是把代码复制过来；还要把每次提交改了什么文件读进数据库，后面的分析才能用。
      </p>
      <p v-if="repo.data.value.parse_window">
        已整理 {{ repo.data.value.parse_window.processed }} /
        {{ repo.data.value.parse_window.total ?? "待扫描" }} 个提交{{
          repo.data.value.parse_window.commit_limit
            ? `（最多取最近的 ${repo.data.value.parse_window.commit_limit} 个）`
            : "（包括全部历史）"
        }}。<small
          >整理这些提交时用的规则版本：{{
            label(ruleVersions, repo.data.value.parse_window.parser_version)
          }}</small
        >
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
          <label for="commit-limit">先整理最近多少个提交</label
          ><input
            id="commit-limit"
            v-model="commitLimit"
            type="number"
            min="1"
            max="700"
            step="1"
            :disabled="parsing || fullHistory"
          />
        </div>
        <label class="scope"
          ><input v-model="fullHistory" type="checkbox" :disabled="parsing" />
          整理全部历史（大仓库可能要跑几个小时）</label
        >
        <button class="primary" :disabled="parsing || !scopeReady">
          {{ parsing ? "提交中…" : "开始整理提交记录" }}
        </button>
        <small>数量越少整理越快；选少了以后也能同步新的提交，但补不回更早的历史。</small>
      </form>
      <p v-if="!canParse" class="hint">
        Viewer（只读账号）可查看整理结果；开始整理需要 Member 或
        Admin 账号（普通成员或管理员）。
      </p>
      <RequestError v-if="!repo.data.value.parse_window" :error="failure" />
      <h2>同步新提交</h2>
      <p data-testid="sync-status">
        {{ label(syncStatus, repo.data.value.sync_status) }}
      </p>
      <p class="hint">
        仓库有了新提交时，点这里把它们补进来；已经整理过的提交不会重复处理。
      </p>
      <p
        v-if="repo.data.value.history_coverage === 'recent_window'"
        class="hint"
      >
        这次只整理了最近一段历史。同步只补新提交，不会补更早的，后面的指标和数据集会沿用这个范围。
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
        {{ syncing ? "提交中…" : "同步最新提交" }}
      </button>
      <p
        v-if="repo.data.value.sync_status === 'requires_review'"
        role="alert"
        class="error"
      >
        仓库的历史被改写过，或者默认分支换了，自动同步已经暂停。原来的提交、最新版本和快照都保留着，需要人工确认后再继续。
      </p>
      <p
        v-if="['failed', 'cancelled'].includes(repo.data.value.sync_status)"
        class="hint"
      >
        此前同步进度已保留，请在当前任务中重试。
      </p>
      <p v-if="!canParse" class="hint">
        启动同步需要 Member 或 Admin 账号（普通成员或管理员）。
      </p>
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
            {{ label(syncRelation, window.relation) }} · 已处理
            {{ window.processed }} / {{ window.total ?? "待扫描" }} 个新提交
          </p>
          <p>
            <small
              >从
              {{ window.base_head_sha ? shortId(window.base_head_sha) : "空历史" }}
              到
              {{ window.head_sha ? shortId(window.head_sha) : "待扫描" }}</small
            >
          </p>
          <small
            >{{ date(window.created_at) }} · 规则版本
            {{ label(ruleVersions, window.parser_version) }}</small
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
          >共 {{ windows.data.value.total }} 条同步记录，第
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
        >，可以看进度、取消或重试。
      </p>
      <p v-if="repo.data.value.task.health === 'stale'" role="status">
        这个任务很久没有更新了，后台会自动尝试恢复；这段时间请不要重复提交。
      </p>
      <div v-if="repo.data.value.task.error" role="alert" class="error">
        <p><strong>这次任务没有完成。</strong>{{ repo.data.value.task.error.message }}</p>
        <small
          >如果反复失败，把下面两行发给开发可以定位问题：错误编号
          {{ repo.data.value.task.error.code }}，请求编号
          {{ repo.data.value.task.error.request_id }}</small
        >
      </div>
      <FixPanel
        ref="fixPanel"
        :repo="repo.data.value"
        :can-write="canParse"
        :busy="active.has(repo.data.value.task.status)"
        :reload="() => repo.load(route.params.id)"
      />
      <SZZPanel
        ref="szzPanel"
        :repo="repo.data.value"
        :can-write="canParse"
        :busy="active.has(repo.data.value.task.status)"
        :reload="refresh"
      />
      <FeaturePanel
        ref="featurePanel"
        :repo="repo.data.value"
        :can-write="canParse"
        :busy="active.has(repo.data.value.task.status)"
        :reload="refresh"
      />
      <h2>提交记录</h2>
      <p class="hint">
        每次提交就是一次代码改动。点「看改动了哪些文件」可以展开这次提交改过的文件。
      </p>
      <RequestError :error="commits.error.value" />
      <div
        v-if="commits.data.value && !commits.error.value"
        data-testid="commit-list"
      >
        <p>
          共
          {{ commits.data.value.total }}
          个已整理的提交。整理还在进行或中途中断时，这里只显示已经处理完的部分。
        </p>
        <ul class="repository-list">
          <li v-for="commit in commits.data.value.items" :key="commit.id">
            <p class="task-id" :title="commit.sha">{{ shortId(commit.sha) }}</p>
            <p>
              {{ commit.message || "（空提交消息）"
              }}{{ commit.message_truncated ? "…" : "" }}
            </p>
            <small
              >{{ commit.author.name_alias || "未命名作者" }} ·
              {{ date(commit.committer_time) }} · {{ commit.file_count }} 个文件{{
                commit.parent_count > 1 ? " · 合并提交" : ""
              }}
              · {{ label(contentStatus, commit.parse_status) }}</small
            >
            <button @click="selectCommit(commit)">看改动了哪些文件</button>
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
          <h3>这次改了哪些文件</h3>
          <p class="task-id" :title="selected.sha">{{ shortId(selected.sha) }}</p>
          <p v-if="selected.parse_status === 'merge_skipped'">
            合并提交只保留「哪次提交合并进来」这个关系，不展开它改了什么。
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
                >{{ label(changeType, file.change_type) }} · 新增
                {{ file.insertions ?? "—" }} / 删除
                {{ file.deletions ?? "—" }} · 改前文件行数
                {{ file.old_loc ?? "—" }} ·
                {{ label(contentStatus, file.content_status) }}</small
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
