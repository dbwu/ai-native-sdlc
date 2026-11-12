<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { request } from '../api/client'
import { dateTime } from '../lib/display'

type Snapshot = { id: string; version: number; sha256: string; recorded_at: string }
type Evidence = { file: string; line: number; end_line: number; snippet: string; file_sha256: string }
type InterfaceStatus = 'matched' | 'extra' | 'missing' | 'changed' | 'uncertain'
type Comparison = {
  snapshot: Snapshot
  design: { id: string; version: number; title: string; proposal_id: string | null }
  rows: {
    key: string; method: string | null; path: string | null; status: InterfaceStatus; reason: string
    designs: { key: string; name: string; contract: Record<string, unknown> }[]
    implementation: { handler: string; evidence: Evidence }[]
    fields: { name: string; expected: unknown; actual: unknown; known: boolean }[]
  }[]
  diagnostics: { message: string; file?: string; line?: number }[]
  executions: { id: string; status: string; recorded_at: string; tests_run: number | null }[]
}

const props = defineProps<{ projectId: string; workspaceId: string; latestSnapshotId: string }>()
const labels: Record<InterfaceStatus, string> = {
  matched: '一致', extra: '实现新增', missing: '设计接口缺失', changed: '契约变更', uncertain: '待核实',
}
const fieldLabels: Record<string, string> = { status_code: '声明状态码', response_model: '声明响应模型' }
const executionLabels: Record<string, string> = {
  RUNNING: '执行中', SUCCEEDED: '执行通过', FAILED: '执行失败', TEST_FAILED: '测试失败',
  TIMED_OUT: '执行超时', CANCELLED: '已取消', STALE: '版本已变化',
}
const order: InterfaceStatus[] = ['changed', 'missing', 'extra', 'uncertain', 'matched']
const snapshots = ref<Snapshot[]>([])
const selected = ref('')
const comparison = ref<Comparison | null>(null)
const snapshotsError = ref('')
const comparisonError = ref('')
const loading = ref(false)
const path = computed(() => '/engineering/workspaces/' + encodeURIComponent(props.workspaceId) + '/snapshots')
const rows = computed(() => [...(comparison.value?.rows ?? [])].sort((a, b) => order.indexOf(a.status) - order.indexOf(b.status)))
const counts = computed(() => order.map(status => ({
  status, count: rows.value.filter(row => row.status === status).length,
})))

watch(() => props.latestSnapshotId, async (latest, previous, onCleanup) => {
  if (!selected.value || selected.value === previous) selected.value = latest
  let current = true
  onCleanup(() => { current = false })
  snapshotsError.value = ''
  try {
    const versions = await request<Snapshot[]>(path.value)
    if (current) snapshots.value = versions
  } catch (failure) {
    if (current) snapshotsError.value = (failure as Error).message
  }
}, { immediate: true })

let requestVersion = 0
async function loadComparison(id: string) {
  if (!id) return
  const version = ++requestVersion
  loading.value = true
  comparisonError.value = ''
  comparison.value = null
  try {
    const result = await request<Comparison>(path.value + '/' + encodeURIComponent(id) + '/interface-comparison')
    if (requestVersion === version) comparison.value = result
  } catch (failure) {
    if (requestVersion === version) comparisonError.value = (failure as Error).message
  } finally {
    if (requestVersion === version) loading.value = false
  }
}
watch(selected, id => { void loadComparison(id) }, { immediate: true })
</script>

<template>
  <section class="panel project-panel interface-panel" aria-label="接口实现对照">
    <div class="studio-heading">
      <div><h3>接口实现对照</h3><p>从已保存的 Python 快照读取 FastAPI 路由声明，与工作区关联的已确认设计逐项对照。</p></div>
    </div>
    <div class="comparison-controls">
      <label class="field">对照代码快照
        <select v-model="selected">
          <option v-if="!snapshots.length" :value="latestSnapshotId">当前已保存快照</option>
          <option v-for="item in snapshots" :key="item.id" :value="item.id">
            快照 v{{ item.version }}{{ item.id === latestSnapshotId ? ' · 最新' : '' }} · {{ dateTime(item.recorded_at, true) }}
          </option>
        </select>
      </label>
      <button type="button" class="button secondary" :disabled="loading || !selected" @click="loadComparison(selected)">刷新对照</button>
    </div>
    <p v-if="snapshotsError || comparisonError" class="info-box error" role="alert">{{ snapshotsError || comparisonError }}</p>
    <p v-if="loading" role="status">正在读取固定快照并核对接口…</p>
    <template v-if="comparison">
      <dl class="comparison-basis">
        <div><dt>已确认设计</dt><dd><RouterLink :to="{ path: '/projects/' + projectId + '/design', query: { flow: comparison.design.id } }">{{ comparison.design.title }} · 流程版本 {{ comparison.design.version }}</RouterLink></dd></div>
        <div><dt>文件快照 v{{ comparison.snapshot.version }}</dt><dd><code>{{ comparison.snapshot.sha256 }}</code></dd></div>
      </dl>
      <div class="comparison-counts" aria-label="接口对照统计">
        <span v-for="item in counts" :key="item.status" class="comparison-status" :class="item.status">{{ labels[item.status] }} {{ item.count }}</span>
      </div>
      <details v-if="comparison.diagnostics.length" class="info-box warning">
        <summary>静态扫描有待核实的范围（{{ comparison.diagnostics.length }} 项）</summary>
        <ul><li v-for="(item, index) in comparison.diagnostics" :key="index">
          <code v-if="item.file">{{ item.file }}<template v-if="item.line">:{{ item.line }}</template> · </code>{{ item.message }}
        </li></ul>
      </details>
      <p v-if="!rows.length" class="empty-inline">当前设计与快照中没有可展示的 HTTP 接口对照项。</p>
      <article v-for="row in rows" :key="row.key" class="interface-row">
        <div class="interface-title">
          <code v-if="row.method">{{ row.method }} {{ row.path }}</code>
          <strong v-else>{{ row.designs[0]?.name }}</strong>
          <span class="comparison-status" :class="row.status">{{ labels[row.status] }}</span>
        </div>
        <p>{{ row.reason }}</p>
        <details v-for="design in row.designs" :key="design.key">
          <summary>设计契约 · {{ design.name }}</summary>
          <pre class="code-block">{{ JSON.stringify(design.contract, null, 2) }}</pre>
        </details>
        <dl v-if="row.fields.length" class="contract-fields">
          <div v-for="field in row.fields" :key="field.name">
            <dt>{{ fieldLabels[field.name] || field.name }}</dt>
            <dd>设计 <code>{{ JSON.stringify(field.expected) }}</code> · 代码 <code>{{ field.known ? JSON.stringify(field.actual) : '待核实' }}</code></dd>
          </div>
        </dl>
        <details v-for="(route, index) in row.implementation" :key="index">
          <summary>代码证据 · {{ route.evidence.file }}:{{ route.evidence.line }} · {{ route.handler }}</summary>
          <pre class="code-block">{{ route.evidence.snippet }}</pre>
          <p class="file-hash">文件 SHA-256 <code>{{ route.evidence.file_sha256 }}</code></p>
        </details>
      </article>
      <details class="comparison-tests">
        <summary>同一快照的测试记录（{{ comparison.executions.length }}）</summary>
        <p v-if="!comparison.executions.length">此快照暂无隔离测试记录。</p>
        <ul v-else>
          <li v-for="item in comparison.executions" :key="item.id">
            {{ executionLabels[item.status] || item.status }} · {{ dateTime(item.recorded_at, true) }}<template v-if="item.tests_run != null"> · {{ item.tests_run }} 项测试</template>
          </li>
        </ul>
      </details>
    </template>
  </section>
</template>

<style scoped>
.interface-panel { min-width: 0; }
.interface-panel h3 { margin: 0 0 .35rem; }
.interface-panel p { margin: .45rem 0 .7rem; }
.comparison-controls { display: flex; flex-wrap: wrap; gap: .75rem; align-items: end; margin: .6rem 0 1rem; }
.comparison-controls .field { flex: 1 1 15rem; margin: 0; }
.comparison-basis { display: grid; gap: .6rem; margin: 0 0 .9rem; }
.comparison-basis dt, .contract-fields dt { font-size: .8rem; color: #405b74; }
.comparison-basis dd, .contract-fields dd { margin: .2rem 0 0; overflow-wrap: anywhere; }
.comparison-counts { display: flex; flex-wrap: wrap; gap: .45rem; margin-bottom: .85rem; }
.comparison-status { display: inline-block; padding: .2rem .55rem; border-radius: .4rem; font-size: .8rem; font-weight: 650; white-space: nowrap; }
.matched { background: #e9f4ed; color: #215e3f; }
.extra { background: #e6f2f9; color: #1c506d; }
.missing { background: #fceaea; color: #93322f; }
.changed, .uncertain { background: #fbf0dc; color: #774910; }
.interface-row { margin: .6rem 0; border: 1px solid #dae4ec; border-radius: .6rem; padding: .8rem; min-width: 0; }
.interface-title { display: flex; flex-wrap: wrap; align-items: center; gap: .55rem; overflow-wrap: anywhere; }
.interface-title code { color: #193e5e; font-weight: 700; }
.interface-row details + details { margin-top: .4rem; }
.file-hash { font-size: .8rem; overflow-wrap: anywhere; }
.comparison-tests { margin-top: .9rem; }
</style>
