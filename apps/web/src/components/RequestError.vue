<script setup>
defineProps({ error: { type: Object, default: null } });
</script>
<template>
  <div v-if="error" role="alert" class="error">
    <p>{{ error.message }}</p>
    <small v-if="error.code">错误编号：{{ error.code }}</small
    ><small v-if="error.requestId">请求编号：{{ error.requestId }}</small>
  </div>
</template>
