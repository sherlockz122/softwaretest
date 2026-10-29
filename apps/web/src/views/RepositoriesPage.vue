<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { client } from "../api";
import { useLatest } from "../latest";
import { date, operationKeys } from "../task-tools";
import RequestError from "../components/RequestError.vue";
const route = useRoute();
const router = useRouter();
const page = computed(() =>
  Math.max(1, Number.parseInt(route.query.page, 10) || 1),
);
const list = useLatest((page, signal) =>
  client.request("/repositories?page=" + page + "&page_size=10", { signal }),
);
const url = ref("");
const creating = ref(false);
const failure = ref(null);
const keys = operationKeys();
const labels = {
  queued: "排队等待下载",
  cloning: "正在下载",
  cloned: "已下载",
  failed: "下载失败",
  cancelled: "下载已取消",
};
const canCreate = computed(() =>
  ["Admin", "Member"].includes(client.state.user?.role),
);
watch(page, (value) => list.load(value), { immediate: true });
const poll = setInterval(() => {
  if (!document.hidden && !list.loading.value && !client.state.busy)
    list.load(page.value);
}, 3000);
onUnmounted(() => clearInterval(poll));
async function create() {
  if (creating.value) return;
  creating.value = true;
  failure.value = null;
  const value = url.value.trim();
  try {
    const result = await client.request("/repositories", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": keys.get("repository", value),
      },
      body: JSON.stringify({ url: value }),
    });
    keys.done("repository", value);
    await router.push("/repositories/" + result.repository_id);
  } catch (error) {
    failure.value = error;
  } finally {
    creating.value = false;
  }
}
</script>
<template>
  <section>
    <div class="section-heading">
      <h1>仓库目录</h1>
      <button :disabled="list.loading.value" @click="list.load(page)">
        刷新仓库
      </button>
    </div>
    <p>
      把公开的 Git 仓库地址贴进来，系统会把它下载到本机。下载完成后点进详情页，就能一步步做后面的分析。
    </p>
    <form v-if="canCreate" class="create-task" @submit.prevent="create">
      <div>
        <label for="repository-url">仓库 HTTPS 地址</label
        ><input
          id="repository-url"
          v-model="url"
          type="url"
          maxlength="1024"
          required
          placeholder="https://github.com/owner/repo"
          :disabled="creating"
        />
      </div>
      <button class="primary" :disabled="creating">
        {{ creating ? "正在提交…" : "添加仓库" }}
      </button>
    </form>
    <p v-else class="hint">
      Viewer（只读账号）可查看仓库；添加仓库需要 Member 或 Admin
      账号（普通成员或管理员）。
    </p>
    <RequestError :error="failure" /><RequestError :error="list.error.value" />
    <p v-if="list.loading.value" role="status">正在读取仓库…</p>
    <div v-if="list.data.value && !list.error.value">
      <p v-if="!list.data.value.total">
        还没有添加过仓库。在上面填入仓库地址就能开始。
      </p>
      <ul class="repository-list">
        <li v-for="repo in list.data.value.items" :key="repo.id">
          <RouterLink :to="'/repositories/' + repo.id">{{
            repo.url
          }}</RouterLink>
          <span>
            · {{ labels[repo.status] || repo.status }} ·
            {{ date(repo.created_at) }}</span
          >
        </li>
      </ul>
      <div class="pagination">
        <button
          :disabled="page <= 1"
          @click="
            router.replace({ path: '/repositories', query: { page: page - 1 } })
          "
        >
          上一页
        </button>
        <span>第 {{ page }} 页 · 共 {{ list.data.value.total }} 个仓库</span>
        <button
          :disabled="page * 10 >= list.data.value.total"
          @click="
            router.replace({ path: '/repositories', query: { page: page + 1 } })
          "
        >
          下一页
        </button>
      </div>
    </div>
  </section>
</template>
