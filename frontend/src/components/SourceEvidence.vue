<script setup lang="ts">
import { nextTick, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { request } from '../api/client'
import { notify, state } from '../store/workbench'

type SourceSpan = {
  source_id: string
  source_version: number
  sha256: string
  start: number
  end: number
  quote: string
}

type SourceEvidence = {
  id: string
  version: number
  name: string
  span: SourceSpan
  source: { uri: string; external_revision: string }
}

const props = defineProps<{ projectId: string; entityId: string; version: number; editable: boolean }>()
const router = useRouter()
const items = ref<SourceEvidence[]>([])
const loading = ref(false)
const loadError = ref('')
const openEvidence = ref('')
const preview = ref<{ before: string; quote: string; after: string } | null>(null)
const previewLoading = ref(false)
const previewError = ref('')
const highlight = ref<HTMLElement>()
const submitting = ref(false)
const submitError = ref('')
const form = reactive({ uri: '', revision: '', content: '', quote: '', actor: state.actor || '本地维护者', reason: '' })
let previewRead = 0
let disposed = false

onUnmounted(() => { disposed = true })

watch(() => [props.projectId, props.entityId, props.version], async (_, __, onCleanup) => {
  let current = true
  onCleanup(() => { current = false })
  ++previewRead
  openEvidence.value = ''
  preview.value = null
  items.value = []
  loading.value = true
  loadError.value = ''
  try {
    const params = new URLSearchParams({ table: 'entity', id: props.entityId, version: String(props.version) })
    const result = await request<{ items: SourceEvidence[] }>(
      `/operations/projects/${encodeURIComponent(props.projectId)}/source-evidence?${params}`,
    )
    if (current) items.value = result.items
  } catch (failure) {
    if (current) loadError.value = (failure as Error).message
  } finally {
    if (current) loading.value = false
  }
}, { immediate: true })

function evidenceKey(item: SourceEvidence) {
  return `${item.id}:${item.version}`
}

function setHighlight(element: unknown) {
  highlight.value = element instanceof HTMLElement ? element : undefined
}

async function showOriginal(item: SourceEvidence) {
  const token = ++previewRead
  preview.value = null
  previewError.value = ''
  previewLoading.value = false
  const key = evidenceKey(item)
  if (openEvidence.value === key) {
    openEvidence.value = ''
    return
  }
  openEvidence.value = key
  previewLoading.value = true
  const span = item.span
  try {
    const path = `/operations/sources/${encodeURIComponent(span.source_id)}/versions/${span.source_version}`
    const source = await request<{ content: string; sha256: string }>(path)
    if (disposed || token !== previewRead) return
    const characters = Array.from(source.content)
    const quote = characters.slice(span.start, span.end).join('')
    if (source.sha256 !== span.sha256 || quote !== span.quote) {
      throw new Error('当前原文与保存的证据位置不一致。')
    }
    preview.value = { before: characters.slice(0, span.start).join(''), quote, after: characters.slice(span.end).join('') }
    await nextTick()
    if (token === previewRead) highlight.value?.scrollIntoView({ block: 'center' })
  } catch (failure) {
    if (!disposed && token === previewRead) previewError.value = (failure as Error).message
  } finally {
    if (!disposed && token === previewRead) previewLoading.value = false
  }
}

function quoteRange(content: string, quote: string) {
  const offset = content.indexOf(quote)
  if (!quote.trim() || offset === -1) throw new Error('请从原文复制完整引文，保留原有标点和空格。')
  if (content.indexOf(quote, offset + 1) !== -1) throw new Error('引文在原文中出现多次，请复制更多相邻原文以确定位置。')
  const start = Array.from(content.slice(0, offset)).length
  return { start, end: start + Array.from(quote).length }
}

async function proposeEvidence() {
  if (submitting.value || !props.editable) return
  const { projectId, entityId, version } = props
  const { content, quote } = form
  const actor = form.actor.trim(), reason = form.reason.trim()
  submitError.value = ''
  submitting.value = true
  try {
    const range = quoteRange(content, quote)
    const source = await request<{ source_id: string; version: number; sha256: string }>('/operations/source-snapshots', {
      project_id: projectId, source_uri: form.uri.trim(), source_revision: form.revision.trim(), content, actor,
    })
    const proposal = await request<{ id: string }>('/operations/source-evidence/proposals', {
      project_id: projectId, target: { table: 'entity', id: entityId, version },
      span: { source_id: source.source_id, source_version: source.version, sha256: source.sha256, ...range, quote },
      actor, reason,
    })
    if (disposed) return
    notify('已建立证据提案，请校验并确认提交。')
    await router.push(`/projects/${encodeURIComponent(projectId)}/proposals/${encodeURIComponent(proposal.id)}`)
  } catch (failure) {
    if (!disposed) submitError.value = (failure as Error).message
  } finally {
    if (!disposed) submitting.value = false
  }
}
</script>

<template>
  <section class="source-evidence" aria-label="原文证据">
    <h4>原文证据</h4>
    <p v-if="loading" class="section-label" role="status">正在读取证据…</p>
    <p v-else-if="loadError" class="info-box error" role="alert">{{ loadError }}</p>
    <p v-else-if="!items.length" class="section-label">此知识版本没有可定位的原文证据。</p>
    <ul v-else class="evidence-items">
      <li v-for="item in items" :key="evidenceKey(item)">
        <strong>{{ item.name }}</strong>
        <p class="section-label">{{ item.source.uri }} · {{ item.source.external_revision }} · 快照 v{{ item.span.source_version }}</p>
        <blockquote>{{ item.span.quote }}</blockquote>
        <button type="button" class="text-button" :aria-expanded="openEvidence === evidenceKey(item)" @click="showOriginal(item)">
          {{ openEvidence === evidenceKey(item) ? '收起原文' : '查看原文' }}
        </button>
        <template v-if="openEvidence === evidenceKey(item)">
          <p v-if="previewLoading" class="section-label" role="status">正在定位原文…</p>
          <p v-else-if="previewError" class="info-box error" role="alert">{{ previewError }}</p>
          <pre v-else-if="preview" class="source-preview" tabindex="0" aria-label="固定版本原文">{{ preview.before }}<mark :ref="setHighlight">{{ preview.quote }}</mark>{{ preview.after }}</pre>
        </template>
      </li>
    </ul>

    <details v-if="editable" class="evidence-editor">
      <summary>登记原文并提出证据</summary>
      <form @submit.prevent="proposeEvidence">
        <div class="source-identity">
          <label class="field">来源 URI<input v-model="form.uri" placeholder="例如 urn:course:requirements" required></label>
          <label class="field">来源版本<input v-model="form.revision" placeholder="例如 v1" required></label>
        </div>
        <label class="field">原文<textarea v-model="form.content" rows="5" required></textarea></label>
        <label class="field">证据引文<textarea v-model="form.quote" rows="2" required></textarea></label>
        <p class="section-label">从原文复制一段仅出现一次的文字，包括原有标点和空格。</p>
        <label class="field">操作署名<input v-model="form.actor" required></label>
        <label class="field">提案依据<textarea v-model="form.reason" rows="2" required></textarea></label>
        <p v-if="submitError" class="info-box error" role="alert">{{ submitError }}</p>
        <button type="submit" class="button primary" :disabled="submitting || !form.actor.trim() || !form.reason.trim()">
          {{ submitting ? '正在建立提案…' : '提出证据提案' }}
        </button>
      </form>
    </details>
  </section>
</template>

<style scoped>
.source-evidence { margin: 14px 0; min-width: 0; }
.evidence-items { list-style: none; margin: 6px 0; padding: 0; }
.evidence-items li { border-top: 1px solid var(--line); padding: 7px 0; overflow-wrap: anywhere; }
.evidence-items blockquote { margin: 4px 0; padding: 4px 8px; border-inline-start: 2px solid var(--line); white-space: pre-wrap; }
.source-preview { margin: 6px 0; padding: 10px; max-height: 20rem; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; background: var(--bg); font: inherit; }
.source-preview mark { background: #ffe99b; color: var(--ink); }
.evidence-editor { margin-top: 8px; }
.evidence-editor summary { cursor: pointer; color: var(--blue); }
.evidence-editor button { margin-top: 8px; }
.source-identity { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 10px; }
@media (max-width: 600px) { .source-identity { grid-template-columns: 1fr; gap: 0; } }
</style>
