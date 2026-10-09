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
      <p class="eyebrow">开发环境 · 工程基础</p>
      <h1>开发环境连接检查</h1>
      <p>本页检查 API、MySQL、Redis 和数据库迁移版本，帮助开始后续开发。</p>
      <div role="status" aria-live="polite" class="status">
        <el-tag v-if="state === 'loading'" type="info">正在检查连接</el-tag>
        <el-tag v-else-if="state === 'ready'" type="success">基础服务连接正常</el-tag>
        <el-tag v-else type="danger">基础服务暂不可用</el-tag>
        <el-button :loading="state === 'loading'" @click="check">重新检查</el-button>
      </div>
      <p v-if="state === 'unavailable'">请确认 API、MySQL 和 Redis 已启动，配置与运行说明一致。</p>
      <p v-if="requestId" class="request">请求编号：{{ requestId }}</p>
      <hr>
      <h2>下一步：登录与任务操作页面</h2>
      <p>认证与可靠任务接口已实现；登录和任务操作页面、预测功能待后续开发。当前连接检查不代表业务验收完成。</p>
    </section>
  </main>
</template>
