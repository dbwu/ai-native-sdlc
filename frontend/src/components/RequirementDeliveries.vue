<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { request } from '../api/client'
import { dateTime, kinds } from '../lib/display'
import type { RecordData } from '../types'
import StatusPill from './StatusPill.vue'

type DocumentKind = 'requirements_analysis' | 'design'
type DeliveryDocument = { title: string; filename: string; markdown: string; download_url: string }
type DesignVersion = { id: string; title: string; version: number; confirmed_at: string }
type DesignArtifact = { id: string; kind: string; name: string; version: number; status: string; description: string }
type Delivery = {
  requirement: RecordData
  designs: DesignVersion[]
  design: (DesignVersion & { shared_requirement_count: number; artifacts: DesignArtifact[] }) | null
  documents: { requirements_analysis: DeliveryDocument; design: DeliveryDocument | null }
}

const props = defineProps<{ projectId: string; requirementId: string; version: string; designId: string }>()
const emit = defineEmits<{
  select: [version: number, designId?: string]
  revise: [requirement: RecordData]
  history: [requirement: RecordData]
}>()
const versions = ref<RecordData[]>([])
const delivery = ref<Delivery | null>(null)
const previewKind = ref<DocumentKind>('requirements_analysis')
const loadingVersions = ref(false)
const loadingDelivery = ref(false)
const versionError = ref('')
const deliveryError = ref('')
let versionRead = 0
let deliveryRead = 0

const entityPath = computed(() => '/entities/' + encodeURIComponent(props.requirementId))
const version = computed(() => {
  if (!props.version) return versions.value[0]?.version
  const number = Number(props.version)
  return /^\d+$/.test(props.version) && Number.isSafeInteger(number) && number > 0 ? number : undefined
})
const versionOptions = computed(() => {
  const options = versions.value
  if (!version.value || options.some(item => item.version === version.value)) return options
  return [...options, { version: version.value }]
})
const document = computed(() => delivery.value?.documents[previewKind.value])
const currentRequirement = computed(() => {
  const latest = versions.value[0]
  return latest && latest.version === delivery.value?.requirement.version ? latest : null
})

watch(() => [props.projectId, props.requirementId], async () => {
  const token = ++versionRead
  versions.value = []
  versionError.value = ''
  loadingVersions.value = true
  try {
    const result = await request<RecordData[]>(entityPath.value + '/history?limit=200')
    if (token !== versionRead) return
    if (!result.length || result[0].project_id !== props.projectId || result[0].kind !== 'Requirement') {
      throw new Error('当前项目中没有这条需求。')
    }
    versions.value = result
  } catch (failure) {
    if (token === versionRead) versionError.value = (failure as Error).message
  } finally {
    if (token === versionRead) loadingVersions.value = false
  }
}, { immediate: true })

watch(() => [props.projectId, props.requirementId, version.value, props.designId], async () => {
  const token = ++deliveryRead
  delivery.value = null
  deliveryError.value = props.version && !version.value ? '链接中的需求版本无效。' : ''
  loadingDelivery.value = false
  previewKind.value = props.designId ? 'design' : 'requirements_analysis'
  const selectedVersion = version.value
  if (!selectedVersion) return
  loadingDelivery.value = true
  try {
    const params = new URLSearchParams({ version: String(selectedVersion) })
    if (props.designId) params.set('design_id', props.designId)
    const path = '/workbench/projects/' + encodeURIComponent(props.projectId) +
      '/requirements/' + encodeURIComponent(props.requirementId) + '/deliveries?' + params
    const result = await request<Delivery>(path)
    if (token !== deliveryRead) return
    delivery.value = result
    if (!result.documents.design) previewKind.value = 'requirements_analysis'
  } catch (failure) {
    if (token === deliveryRead) deliveryError.value = (failure as Error).message
  } finally {
    if (token === deliveryRead) loadingDelivery.value = false
  }
}, { immediate: true })

function changeVersion(event: Event) {
  emit('select', Number((event.target as HTMLSelectElement).value))
}

function changeDesign(event: Event) {
  if (version.value) emit('select', version.value, (event.target as HTMLSelectElement).value)
}
</script>

<template>
  <section class="delivery" aria-label="需求交付视图">
    <div class="delivery-title">
      <div>
        <p class="delivery-kicker">需求交付视图</p>
        <h3>{{ delivery?.requirement.name || versions[0]?.name || '需求版本与交付文档' }}</h3>
      </div>
      <div class="inline-actions" v-if="currentRequirement">
        <button v-if="currentRequirement.status !== 'archived'" type="button" class="button secondary" @click="emit('revise', currentRequirement)">修订此知识</button>
        <button type="button" class="button secondary" @click="emit('history', currentRequirement)">历史与影响</button>
      </div>
    </div>
    <div class="delivery-options">
      <label v-if="versionOptions.length" class="field">需求版本
        <select :value="version" @change="changeVersion">
          <option v-for="item in versionOptions" :key="item.version" :value="item.version">
            v{{ item.version }}{{ item.version === versions[0]?.version ? '（最新）' : '' }}
          </option>
        </select>
      </label>
      <label v-if="delivery?.designs.length" class="field">已确认设计
        <select :value="delivery.design?.id" @change="changeDesign">
          <option v-for="item in delivery.designs" :key="item.id" :value="item.id">
            {{ item.title }} · v{{ item.version }} · {{ dateTime(item.confirmed_at, true) }}
          </option>
        </select>
      </label>
    </div>
    <p v-if="versionError" class="info-box warning" role="status">{{ versionError }}</p>
    <p v-if="loadingDelivery || loadingVersions" class="section-label" role="status">正在读取需求版本与交付文档…</p>
    <div v-if="deliveryError" class="info-box error" role="alert">
      <p>{{ deliveryError }}</p>
      <button v-if="designId && version" type="button" class="button secondary" @click="emit('select', version)">查看此需求版本的可用设计</button>
    </div>
    <template v-if="delivery">
      <p class="delivery-note">
        <StatusPill :status="delivery.requirement.status"/> 需求 v{{ delivery.requirement.version }}
        <template v-if="delivery.design"> · 设计确认于 {{ dateTime(delivery.design.confirmed_at) }}</template>
      </p>
      <p v-if="!delivery.design" class="info-box">此需求版本暂无已确认设计，可以先预览和下载需求分析文档。</p>
      <section v-if="delivery.design" class="delivery-artifacts" aria-label="本次设计产物">
        <h4>本次设计产物</h4>
        <p v-if="delivery.design.shared_requirement_count > 1" class="section-label">
          本次设计涉及 {{ delivery.design.shared_requirement_count }} 条需求，以下显示关联到当前需求的产物。
        </p>
        <p v-if="!delivery.design.artifacts.length" class="section-label">本次设计没有可列出的关联产物。</p>
        <ul v-else class="artifact-list">
          <li v-for="item in delivery.design.artifacts" :key="item.id">
            <details>
              <summary><strong>{{ item.name }}</strong><span>{{ kinds[item.kind] || item.kind }} · v{{ item.version }}</span></summary>
              <p><StatusPill :status="item.status"/> {{ item.description || '暂无说明' }}</p>
            </details>
          </li>
        </ul>
      </section>
      <div class="delivery-documents">
        <div class="inline-actions" aria-label="文档预览">
          <button type="button" class="button" :class="previewKind === 'requirements_analysis' ? 'primary' : 'secondary'"
            :aria-pressed="previewKind === 'requirements_analysis'" @click="previewKind = 'requirements_analysis'">需求分析文档</button>
          <button type="button" class="button" :class="previewKind === 'design' ? 'primary' : 'secondary'"
            :aria-pressed="previewKind === 'design'" :disabled="!delivery.documents.design" @click="previewKind = 'design'">设计文档</button>
          <a v-if="document" class="button secondary delivery-download" :href="document.download_url" :download="document.filename">
            下载 Markdown
          </a>
        </div>
        <pre v-if="document" class="markdown-preview" tabindex="0" :aria-label="document.title">{{ document.markdown }}</pre>
      </div>
    </template>
  </section>
</template>

<style scoped>
.delivery { min-width: 0; }
.delivery-title { display: flex; justify-content: space-between; align-items: start; gap: 12px; flex-wrap: wrap; }
.delivery-title h3 { font-size: 18px; }
.delivery-kicker, .delivery-note { font-size: 12px; color: var(--muted); }
.delivery-options { display: grid; grid-template-columns: minmax(0, .7fr) minmax(0, 1.3fr); gap: 12px; }
.delivery-options select { min-width: 0; width: 100%; }
.delivery-note { margin: 12px 0; }
.delivery-artifacts { margin: 14px 0 18px; }
.artifact-list { list-style: none; padding: 0; margin: 8px 0 0; }
.artifact-list li { border-bottom: 1px solid var(--line); padding: 7px 0; }
.artifact-list summary { cursor: pointer; }
.artifact-list summary span { margin-inline-start: 8px; font-size: 12px; color: var(--muted); }
.artifact-list p { margin-top: 6px; }
.delivery-download { margin-inline-start: auto; }
.markdown-preview { white-space: pre-wrap; overflow-wrap: anywhere; overflow: auto; max-height: 34rem; margin: 10px 0 0; padding: 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); font: inherit; font-size: 13px; }
@media (max-width: 700px) { .delivery-options { grid-template-columns: 1fr; gap: 0; } }
</style>
