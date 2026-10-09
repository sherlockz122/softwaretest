<script setup>
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { client } from "../api";
import RequestError from "../components/RequestError.vue";
const route = useRoute();
const router = useRouter();
const username = ref("");
const password = ref("");
const failure = ref(null);
async function submit() {
  if (client.state.busy) return;
  failure.value = null;
  try {
    await client.login(username.value, password.value);
    const next =
      typeof route.query.next === "string" &&
      /^\/tasks(?:\/|\?|$)/.test(route.query.next)
        ? route.query.next
        : "/tasks";
    await router.replace(next);
  } catch (error) {
    failure.value = error;
  } finally {
    password.value = "";
  }
}
</script>
<template>
  <section class="login-panel">
    <p class="eyebrow">DefectGuard · 开发工作台</p>
    <h1>登录</h1>
    <p>登录后查看任务进度和执行结果。</p>
    <p v-if="client.state.reason" role="status">{{ client.state.reason }}</p>
    <form @submit.prevent="submit">
      <label for="username">用户名</label
      ><input
        id="username"
        v-model="username"
        name="username"
        autocomplete="username"
        maxlength="64"
        required
        :disabled="client.state.busy"
      />
      <label for="password">密码</label
      ><input
        id="password"
        v-model="password"
        name="password"
        type="password"
        autocomplete="current-password"
        maxlength="1024"
        required
        :disabled="client.state.busy"
      />
      <RequestError :error="failure" /><button
        class="primary"
        type="submit"
        :disabled="client.state.busy"
      >
        {{ client.state.busy ? "登录中…" : "登录" }}
      </button>
    </form>
    <p class="hint">
      使用已初始化的项目账号。刷新页面后需重新登录，任务记录会保留。
    </p>
  </section>
</template>
