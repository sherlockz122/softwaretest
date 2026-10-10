<script setup>
import { watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { client } from "./api";
const route = useRoute();
const router = useRouter();
watch(
  () => client.state.user,
  (user) => {
    if (!user && route.meta.auth)
      router.replace({ path: "/login", query: { next: route.fullPath } });
  },
);
async function logout() {
  try {
    await client.logout();
  } catch {
    /* Login page explains uncertain revocation. */
  }
}
</script>
<template>
  <main>
    <header>
      <RouterLink class="brand" to="/tasks">DefectGuard</RouterLink
      ><span>即时软件缺陷预测</span>
      <nav aria-label="主要导航">
        <RouterLink to="/repositories">仓库目录</RouterLink>
        <RouterLink to="/tasks">任务中心</RouterLink
        ><RouterLink to="/health">连接检查</RouterLink>
      </nav>
      <div v-if="client.state.user" class="account">
        <span
          >{{ client.state.user.username }} · {{ client.state.user.role }}</span
        ><button :disabled="client.state.busy" @click="logout">退出登录</button>
      </div>
    </header>
    <RouterView :key="client.state.user?.id || 'anonymous'" />
    <footer>DefectGuard · 开发环境 · 任务时间按本地时区显示</footer>
  </main>
</template>
