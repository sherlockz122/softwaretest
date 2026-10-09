<script setup>
import { ref, onMounted } from 'vue'
const state = ref('loading')
const requestId = ref('')
async function check() {
  state.value = 'loading'
  requestId.value = ''
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 8000)
  try {
    const response = await fetch('/api/v1/health/ready', { signal: controller.signal })
    requestId.value = response.headers.get('X-Request-ID') || ''
    const data = await response.json()
    state.value = response.ok && data.status === 'ok' ? 'ready' : 'unavailable'
  } catch {
    state.value = 'unavailable'
  } finally {
    clearTimeout(timer)
  }
}
onMounted(check)
</script>

<template>
  <main>
    <header><span class="brand">DefectGuard</span><span>即时软件缺陷预测</span></header>
    <section>
      <p class="eyebrow">开发环境 · 第二阶段</p>
      <h1>开发环境连接检查</h1>
      <p>本页检查 API、MySQL 和 Redis 的基础连接，帮助开始后续开发。</p>
      <div role="status" aria-live="polite" class="status">
        <el-tag v-if="state === 'loading'" type="info">正在检查连接</el-tag>
        <el-tag v-else-if="state === 'ready'" type="success">基础服务连接正常</el-tag>
        <el-tag v-else type="danger">基础服务暂不可用</el-tag>
        <el-button :loading="state === 'loading'" @click="check">重新检查</el-button>
      </div>
      <p v-if="state === 'unavailable'">请确认 API、MySQL 和 Redis 已启动，配置与运行说明一致。</p>
      <p v-if="requestId" class="request">请求编号：{{ requestId }}</p>
      <hr>
      <h2>下一步：认证与可靠任务</h2>
      <p>登录、数据库业务表、任务创建和预测功能尚未实现。当前连接检查不代表业务验收完成。</p>
    </section>
  </main>
</template>
