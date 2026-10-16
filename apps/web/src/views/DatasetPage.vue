<script setup>
import { onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { labels, date } from "../task-tools";
import RequestError from "../components/RequestError.vue";
const route = useRoute();
const offset = ref(0);
const downloadError = ref(null);
const downloading = ref(false);
const data = useLatest((id, signal) =>
  client.request(
    `/datasets/${encodeURIComponent(id)}?offset=${offset.value}&limit=20`,
    { signal },
  ),
);
watch(
  () => route.params.id,
  (id) => {
    offset.value = 0;
    data.load(id);
  },
  { immediate: true },
);
const timer = setInterval(() => {
  if (
    !document.hidden &&
    !data.loading.value &&
    !client.state.busy &&
    !data.data.value?.ready
  )
    data.load(route.params.id);
}, 3000);
onUnmounted(() => clearInterval(timer));
function paginate(delta) {
  offset.value += delta;
  data.load(route.params.id);
}
async function download(partition) {
  downloading.value = true;
  downloadError.value = null;
  try {
    const blob = await client.request(
      `/datasets/${encodeURIComponent(route.params.id)}?format=csv&partition=${partition}`,
      { responseType: "blob" },
    );
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${route.params.id}-${partition}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch (error) {
    downloadError.value = error;
  } finally {
    downloading.value = false;
  }
}
</script>
<template>
  <section>
    <div class="section-heading">
      <h1>数据集详情</h1>
      <button
        :disabled="data.loading.value"
        @click="data.load(route.params.id)"
      >
        刷新详情
      </button>
    </div>
    <RouterLink to="/datasets">返回数据集</RouterLink>
    <RequestError :error="data.error.value || downloadError" />
    <p v-if="data.loading.value" role="status">正在加载…</p>
    <template v-if="data.data.value">
      <h2>{{ data.data.value.name }}</h2>
      <dl>
        <dt>用途</dt>
        <dd>公开发布弱监督二分类研究</dd>
        <dt>状态</dt>
        <dd>{{ labels[data.data.value.status] || data.data.value.status }}</dd>
        <dt>成员进度</dt>
        <dd>
          {{ data.data.value.processed }} / {{ data.data.value.total ?? "—" }}
        </dd>
        <dt>创建时间</dt>
        <dd>{{ date(data.data.value.created_at) }}</dd>
        <dt>数据来源</dt>
        <dd class="break-anywhere">{{ data.data.value.asset_id }}</dd>
      </dl>
      <RouterLink :to="`/tasks/${data.data.value.task_id}`"
        >查看任务、取消或重试</RouterLink
      >
      <p v-if="!data.data.value.ready">
        数据集尚未完成；半成品不能导出或用于训练。失败、取消和重试可在任务详情中处理。
      </p>
      <template v-else>
        <h3>限定研究就绪</h3>
        <p>
          来源与分区结构已经验证，真实标签/负例覆盖尚未独立认证。本版本不支持历史在线回放、候选发布、语义、类型或定位用途；尚未训练模型。
        </p>
        <p>
          切分：{{
            data.data.value.policy.split_strategy === "time_ordered"
              ? "按时间同组"
              : "按独立仓库留出"
          }}
          · 12项数值，原发布fix标志不作为特征。
        </p>
        <p class="hint">窄屏下可左右滚动表格，查看全部列与下载操作。</p>
        <div
          class="table-scroll"
          role="region"
          aria-label="分区与导出"
          tabindex="0"
        >
          <table>
            <thead>
              <tr>
                <th>分区</th>
                <th>clean</th>
                <th>buggy</th>
                <th>导出</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(count, partition) in data.data.value.summary.partitions"
                :key="partition"
              >
                <td>{{ partition }}</td>
                <td>{{ count.clean }}</td>
                <td>{{ count.buggy }}</td>
                <td>
                  <button :disabled="downloading" @click="download(partition)">
                    下载 {{ partition }} CSV
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <details>
          <summary>来源、分区和能力审计</summary>
          <dl>
            <dt>原数据取得时间</dt>
            <dd>
              {{
                date(data.data.value.summary.source_acquisition?.acquired_at)
              }}
            </dd>
            <dt>源首次保存时间</dt>
            <dd>{{ date(data.data.value.summary.source_first_stored_at) }}</dd>
            <dt>数据许可</dt>
            <dd>
              {{
                data.data.value.summary.source_acquisition?.apachejit_license ||
                "待核验"
              }}
            </dd>
            <dt>原始行 / 去重成员</dt>
            <dd>
              {{ data.data.value.summary.original_rows }} /
              {{ data.data.value.summary.rows }}
            </dd>
            <dt>合并重复行</dt>
            <dd>{{ data.data.value.summary.duplicate_rows }}</dd>
            <dt>事件时间</dt>
            <dd>原发布作者时间；用于后验研究，不代表历史当时已知标签。</dd>
          </dl>
          <p
            v-if="
              data.data.value.summary.source_acquisition
                ?.fixture_not_real_acquisition
            "
          >
            工程测试数据，不代表真实标签或模型效果。
          </p>
          <div
            class="table-scroll"
            role="region"
            aria-label="独立仓库支持数"
            tabindex="0"
          >
            <table>
              <thead>
                <tr>
                  <th>独立仓库</th>
                  <th>clean</th>
                  <th>buggy</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(count, repository) in data.data.value.summary
                    .repositories"
                  :key="repository"
                >
                  <td>{{ repository }}</td>
                  <td>{{ count.clean }}</td>
                  <td>{{ count.buggy }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>
        <p class="break-anywhere">
          不可变清单摘要：{{ data.data.value.manifest_hash }}
        </p>
        <h3>成员预览</h3>
        <div
          class="table-scroll"
          role="region"
          aria-label="数据集成员"
          tabindex="0"
        >
          <table>
            <thead>
              <tr>
                <th>仓库</th>
                <th>提交</th>
                <th>分区</th>
                <th>原发布标签</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in data.data.value.members" :key="row.ordinal">
                <td>{{ row.canonical_repository }}</td>
                <td class="break-anywhere">{{ row.commit_id }}</td>
                <td>{{ row.partition }}</td>
                <td>{{ row.label ? "buggy" : "clean" }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="actions">
          <button
            :disabled="offset <= 0 || data.loading.value"
            @click="paginate(-20)"
          >
            上一页</button
          ><button
            :disabled="
              offset + 20 >= data.data.value.total || data.loading.value
            "
            @click="paginate(20)"
          >
            下一页
          </button>
        </div>
      </template>
    </template>
  </section>
</template>
<style scoped>
.break-anywhere,
dl,
h2 {
  overflow-wrap: anywhere;
}
table {
  table-layout: fixed;
}
td {
  overflow-wrap: anywhere;
}
.actions {
  display: flex;
  gap: 12px;
  margin-top: 16px;
}
</style>
