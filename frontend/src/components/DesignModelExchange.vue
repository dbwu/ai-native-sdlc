<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { request } from '../api/client'

interface ModelExchange {
  request: { model: string; messages: { role: string; content: string }[]; [key: string]: unknown }
  response: { content: string | null; finish_reason?: string | null; refusal?: string | null } | null
}

const props = defineProps<{ flowId: string; jobId: string; available: boolean; pending: boolean }>()
const expanded = ref(false)
const exchange = ref<ModelExchange | null>(null)
const error = ref('')
const loading = ref(false)
const requestParameters = computed(() => Object.fromEntries(
  Object.entries(exchange.value?.request ?? {}).filter(([key]) => key !== 'model' && key !== 'messages'),
))
let readVersion = 0

function roleLabel(role: string) {
  if (role === 'system') return '系统指令'
  if (role === 'user') return '任务内容和上下文'
  return role
}

async function loadExchange() {
  if (!expanded.value || loading.value) return
  const version = ++readVersion
  loading.value = true
  error.value = ''
  try {
    const path = `/design/flows/${encodeURIComponent(props.flowId)}/generations/${encodeURIComponent(props.jobId)}/model-exchange`
    const data = await request<ModelExchange>(path)
    if (version === readVersion) exchange.value = data
  } catch (failure) {
    if (version === readVersion) error.value = (failure as Error).message
  } finally {
    if (version === readVersion) loading.value = false
  }
}

function toggle(event: Event) {
  expanded.value = (event.currentTarget as HTMLDetailsElement).open
  if (expanded.value) {
    void loadExchange()
  } else {
    ++readVersion
    exchange.value = null
    loading.value = false
    error.value = ''
  }
}

onBeforeUnmount(() => { ++readVersion })
</script>

<template>
  <details v-if="available" class="model-exchange" @toggle="toggle">
    <summary>查看发送给模型的输入和模型回复</summary>
    <div v-if="expanded" class="exchange-content">
      <p v-if="loading" role="status">正在读取本轮模型调用内容…</p>
      <div v-else-if="error" class="info-box error" role="alert">
        <p>{{ error }}</p><button type="button" class="button secondary" @click="loadExchange">重新读取</button>
      </div>
      <template v-else-if="exchange">
        <h4>平台发送给模型</h4>
        <p>模型 {{ exchange.request.model }}</p>
        <div v-for="(message, index) in exchange.request.messages" :key="index" class="exchange-message">
          <p><strong>{{ roleLabel(message.role) }}</strong> <code>{{ message.role }}</code></p>
          <pre tabindex="0" :aria-label="roleLabel(message.role)">{{ message.content }}</pre>
        </div>
        <details v-if="Object.keys(requestParameters).length">
          <summary>查看请求参数</summary>
          <pre tabindex="0" aria-label="请求参数">{{ JSON.stringify(requestParameters, null, 2) }}</pre>
        </details>
        <h4>模型回复平台</h4>
        <template v-if="exchange.response">
          <p v-if="exchange.response.finish_reason">结束原因 <code>{{ exchange.response.finish_reason }}</code></p>
          <pre v-if="exchange.response.content" tabindex="0" aria-label="模型回复原文">{{ exchange.response.content }}</pre>
          <p v-else>模型回复没有文本内容。</p>
          <template v-if="exchange.response.refusal">
            <p><strong>拒绝说明</strong></p>
            <pre tabindex="0" aria-label="模型拒绝说明">{{ exchange.response.refusal }}</pre>
          </template>
        </template>
        <p v-else>本轮没有可查看的模型回复，请查看本轮生成状态和诊断。</p>
      </template>
    </div>
  </details>
  <p v-else-if="!pending" class="section-label">本轮没有保存模型输入和回复。</p>
</template>

<style scoped>
.exchange-content { margin-top: 12px; }
.exchange-content > h4:not(:first-child) { margin-top: 16px; }
.model-exchange pre { max-height: 22rem; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
