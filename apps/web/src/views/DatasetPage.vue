<script setup>
import { onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { labels, date, shortId } from "../task-tools";
import { label, partitions } from "../terms";
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
    <RouterLink to="/datasets">返回数据集列表</RouterLink>
    <RequestError :error="data.error.value || downloadError" />
    <p v-if="data.loading.value" role="status">正在读取…</p>
    <template v-if="data.data.value">
      <h2>{{ data.data.value.name }}</h2>
      <dl>
        <dt>用途</dt>
        <dd>研究用二分类数据（标签来自原公开数据的规则判断）</dd>
        <dt>状态</dt>
        <dd>{{ labels[data.data.value.status] || data.data.value.status }}</dd>
        <dt>进度（已处理 / 总数）</dt>
        <dd>
          {{ data.data.value.processed }} / {{ data.data.value.total ?? "—" }}
        </dd>
        <dt>创建时间</dt>
        <dd>{{ date(data.data.value.created_at) }}</dd>
        <dt>数据来源编号</dt>
        <dd class="break-anywhere">{{ shortId(data.data.value.asset_id) }}</dd>
      </dl>
      <RouterLink :to="`/tasks/${data.data.value.task_id}`"
        >查看任务，可取消或重试</RouterLink
      >
      <p v-if="!data.data.value.ready">
        数据集还没生成完；没完成的不能下载，也不能用于训练。如果失败了，可以到任务详情里取消或重试。
      </p>
      <template v-else>
        <h3>数据集已可用</h3>
        <p>
          数据来源和划分结构已经核对过。标签是原公开数据自带的规则判断，准不准、两类样本够不够还没有单独确认；这个版本也不支持按历史时间点回放、发布模型或判断缺陷类型。
        </p>
        <p>
          划分方式：{{
            data.data.value.policy.split_strategy === "time_ordered"
              ? "按时间先后（同一时间段的提交不拆开）"
              : "按仓库划分（留出独立仓库做测试）"
          }}
          · 共 12 项数值指标；原数据里的 fix 标志不参与计算，避免提前泄露答案。
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
                <th>正常提交</th>
                <th>有缺陷提交</th>
                <th>下载</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(count, partition) in data.data.value.summary.partitions"
                :key="partition"
              >
                <td>{{ label(partitions, partition) }}</td>
                <td>{{ count.clean }}</td>
                <td>{{ count.buggy }}</td>
                <td>
                  <button :disabled="downloading" @click="download(partition)">
                    下载{{ label(partitions, partition) }}数据（CSV）
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <details>
          <summary>数据来源和划分详情</summary>
          <dl>
            <dt>原数据获取时间</dt>
            <dd>
              {{
                date(data.data.value.summary.source_acquisition?.acquired_at)
              }}
            </dd>
            <dt>数据首次保存时间</dt>
            <dd>{{ date(data.data.value.summary.source_first_stored_at) }}</dd>
            <dt>使用许可</dt>
            <dd>
              {{
                data.data.value.summary.source_acquisition?.apachejit_license ||
                "待核验"
              }}
            </dd>
            <dt>原始行数 / 去重后提交数</dt>
            <dd>
              {{ data.data.value.summary.original_rows }} /
              {{ data.data.value.summary.rows }}
            </dd>
            <dt>合并的重复行</dt>
            <dd>{{ data.data.value.summary.duplicate_rows }}</dd>
            <dt>提交时间</dt>
            <dd>用的是原作者提交时间。这是事后研究，不表示当时就能知道标签。</dd>
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
                  <th>正常提交</th>
                  <th>有缺陷提交</th>
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
          内容指纹（用来确认文件没有被改过）：<span
            :title="data.data.value.manifest_hash"
            >{{ shortId(data.data.value.manifest_hash) }}</span
          >
        </p>
        <h3>数据样例</h3>
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
                <th>标签</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in data.data.value.members" :key="row.ordinal">
                <td>{{ row.canonical_repository }}</td>
                <td class="break-anywhere" :title="row.commit_id">
                  {{ shortId(row.commit_id) }}
                </td>
                <td>{{ label(partitions, row.partition) }}</td>
                <td>{{ row.label ? "有缺陷" : "正常" }}</td>
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
