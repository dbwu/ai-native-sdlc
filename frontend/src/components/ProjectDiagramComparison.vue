<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { request } from '../api/client'
import DiagramPreview, { type DesignDiagram } from './DiagramPreview.vue'
import ProjectModelChanges, { type ProjectModelChange } from './ProjectModelChanges.vue'

type Kind = 'architecture' | 'activity' | 'sequence'
type View = 'overlay' | 'split' | 'before' | 'after'
type Version = { version: number; title: string }
type Choice = { key: string; name: string }
type Comparison = {
  before: DesignDiagram | null
  after: DesignDiagram | null
  overlay: DesignDiagram | null
  before_ref: Version | null
  after_ref: Version
  catalog: (Choice & { scenarios: Choice[] })[]
  selection: { process_key: string | null; scenario_key: string | null }
  notice: string
  changes: ProjectModelChange[]
}

const props = defineProps<{ path: string; versions: Version[]; currentVersion: number; initialBefore?: number }>()
const kind = ref<Kind>('architecture')
const view = ref<View>('overlay')
const after = ref(props.currentVersion)
const previous = computed(() => props.versions.filter(item => item.version < after.value))
const before = ref(previous.value.some(item => item.version === props.initialBefore) || props.initialBefore === 0
  ? props.initialBefore! : Math.max(0, ...previous.value.map(item => item.version)))
const processKey = ref('')
const scenarioKey = ref('')
const report = ref<Comparison | null>(null)
const loading = ref(false)
const error = ref('')
const refresh = ref(0)
const modeOptions: { key: View; name: string }[] = [
  { key: 'overlay', name: '变化叠加' }, { key: 'split', name: '左右对照' },
  { key: 'before', name: '仅旧版' }, { key: 'after', name: '仅新版' },
]
const processOptions = computed(() => report.value?.catalog.filter(item => kind.value !== 'sequence' || item.scenarios.length) ?? [])
const selectedProcess = computed(() => processKey.value || report.value?.selection.process_key || '')
const scenarios = computed(() => processOptions.value.find(item => item.key === selectedProcess.value)?.scenarios ?? [])
const query = computed(() => {
  const params = new URLSearchParams({ before_version: String(before.value), after_version: String(after.value), kind: kind.value })
  if (kind.value !== 'architecture' && processKey.value) params.set('process_key', processKey.value)
  if (kind.value === 'sequence' && scenarioKey.value) params.set('scenario_key', scenarioKey.value)
  return params.toString()
})
const downloadUrl = computed(() => {
  const params = new URLSearchParams(query.value)
  if (report.value?.selection.process_key) params.set('process_key', report.value.selection.process_key)
  if (report.value?.selection.scenario_key) params.set('scenario_key', report.value.selection.scenario_key)
  return `${props.path}/comparison/markdown?${params}`
})
let generation = 0

function resetSelection() {
  processKey.value = ''
  scenarioKey.value = ''
}

function changeAfter() {
  if (before.value >= after.value) before.value = Math.max(0, ...previous.value.map(item => item.version))
  resetSelection()
}

watch(() => [props.path, query.value, refresh.value], async () => {
  const token = ++generation
  loading.value = true
  error.value = ''
  try {
    const result = await request<Comparison>(`${props.path}/comparison?${query.value}`)
    if (token === generation) report.value = result
  } catch (failure) {
    if (token === generation) {
      report.value = null
      error.value = (failure as Error).message
    }
  } finally {
    if (token === generation) loading.value = false
  }
}, { immediate: true })
onBeforeUnmount(() => { ++generation })
</script>

<template>
  <section class="comparison" aria-label="图形版本对照">
    <div class="comparison-controls">
      <label class="field">旧版本
        <select v-model.number="before" aria-label="旧版本" @change="resetSelection">
          <option :value="0">首次发布前</option>
          <option v-for="item in previous" :key="item.version" :value="item.version">v{{ item.version }} · {{ item.title }}</option>
        </select>
      </label>
      <label class="field">新版本
        <select v-model.number="after" aria-label="新版本" @change="changeAfter">
          <option v-for="item in versions" :key="item.version" :value="item.version">v{{ item.version }} · {{ item.title }}</option>
        </select>
      </label>
      <label class="field">图表
        <select v-model="kind" aria-label="图表" @change="resetSelection">
          <option value="architecture">项目架构图</option>
          <option value="activity">业务流程泳道图</option>
          <option value="sequence">业务时序图</option>
        </select>
      </label>
      <label v-if="kind !== 'architecture' && processOptions.length" class="field">业务流程
        <select :value="selectedProcess" aria-label="对照业务流程" :disabled="loading"
          @change="processKey = ($event.target as HTMLSelectElement).value; scenarioKey = ''">
          <option v-for="item in processOptions" :key="item.key" :value="item.key">{{ item.name }}</option>
        </select>
      </label>
      <label v-if="kind === 'sequence' && scenarios.length" class="field">时序场景
        <select :value="scenarioKey || report?.selection.scenario_key || ''" aria-label="对照时序场景"
          :disabled="loading" @change="scenarioKey = ($event.target as HTMLSelectElement).value">
          <option v-for="item in scenarios" :key="item.key" :value="item.key">{{ item.name }}</option>
        </select>
      </label>
    </div>
    <p v-if="loading" role="status">正在读取固定版本的图表…</p>
    <div v-else-if="error" class="info-box error" role="alert">
      <p>{{ error }}</p><button type="button" class="text-button" @click="refresh++">重试</button>
    </div>
    <template v-else-if="report">
      <div class="comparison-actions">
        <div class="comparison-modes" aria-label="图表对照方式">
          <button v-for="item in modeOptions" :key="item.key" type="button" class="button"
            :class="view === item.key ? 'primary' : 'secondary'" :aria-pressed="view === item.key"
            @click="view = item.key">{{ item.name }}</button>
        </div>
        <a class="button secondary" :href="downloadUrl" download>下载对照 Markdown</a>
      </div>
      <p v-if="report.notice" class="info-box warning">{{ report.notice }}</p>
      <template v-if="view === 'overlay'">
        <div class="legend" aria-label="图内变化图例">
          <span class="added">新增</span><span class="updated">修改</span><span class="removed">删除</span>
          <span>删除项显示旧版内容。</span>
        </div>
        <DiagramPreview v-if="report.overlay" :diagram="report.overlay" compact/>
        <p v-else class="info-box">两个版本均没有所选图表。</p>
      </template>
      <div v-else class="comparison-sides" :class="{ split: view === 'split' }">
        <section v-if="view !== 'after'" aria-label="旧版图表">
          <h4>{{ report.before_ref ? `旧版 · 基线 v${report.before_ref.version}` : '旧版 · 首次发布前' }}</h4>
          <DiagramPreview v-if="report.before" :diagram="report.before" compact/>
          <p v-else class="info-box">旧版本没有所选图表。</p>
        </section>
        <section v-if="view !== 'before'" aria-label="新版图表">
          <h4>新版 · 基线 v{{ report.after_ref.version }}</h4>
          <DiagramPreview v-if="report.after" :diagram="report.after" compact/>
          <p v-else class="info-box">新版本没有所选图表。</p>
        </section>
      </div>
      <details class="comparison-details">
        <summary>查看变更说明（{{ report.changes.length }} 项）</summary>
        <ProjectModelChanges :changes="report.changes"/>
      </details>
    </template>
  </section>
</template>

<style scoped>
.comparison { margin-top: 12px; min-width: 0; }
.comparison-controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 14rem), 1fr)); gap: 8px 12px; }
.comparison-controls select { width: 100%; padding: 7px 8px; border: 1px solid var(--line); border-radius: 7px; background: #fcfdfe; color: var(--ink); }
.comparison-actions, .comparison-modes, .legend { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.comparison-actions { justify-content: space-between; margin-block: 12px; }
.legend { color: var(--muted); font-size: 13px; }
.legend span { padding: 2px 8px; border-radius: 5px; }
.legend .added { color: #26734c; background: #e7f6ed; }
.legend .updated { color: #285ca1; background: #e8f1ff; }
.legend .removed { color: #a43746; background: #fcebed; border: 1px dashed currentColor; }
.comparison-sides { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; }
.comparison-sides.split { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.comparison-sides > section { min-width: 0; }
.comparison-details { margin-block: 10px; }
.comparison-details summary { cursor: pointer; }
@media (max-width: 760px) {
  .comparison-sides.split { grid-template-columns: minmax(0, 1fr); }
}
</style>
