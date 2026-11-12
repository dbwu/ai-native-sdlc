<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { request } from '../api/client'
import { dateTime, kinds, relationshipTypes } from '../lib/display'
import { notify, selectProject, state } from '../store/workbench'
import type { RecordData } from '../types'
import ExtractionEvidenceEditor, { type ExtractionQuote } from '../components/ExtractionEvidenceEditor.vue'
import ExtractionReview, { type ReviewDecision } from '../components/ExtractionReview.vue'
import StatusPill from '../components/StatusPill.vue'

type Existing = { id: string; version: number }
type AcceptanceItem = { key: string; text: string; evidence: ExtractionQuote[] }
type CandidateEntity = {
  key: string; kind: string; name: string; description: string; attributes: RecordData
  existing: Existing | null; evidence: ExtractionQuote[]; acceptance_items?: AcceptanceItem[] | null
}
type CandidateRelationship = {
  key: string; type: string; source_key: string; target_key: string; rationale: string; evidence: ExtractionQuote[]
}
type Candidate = { summary: string; entities: CandidateEntity[]; relationships: CandidateRelationship[]; warnings: string[]; review_hints?: RecordData[] }

const route = useRoute(), router = useRouter()
const pid = computed(() => String(route.params.projectId || state.projectId))
const jobId = computed(() => String(route.params.jobId || ''))
const jobs = ref<RecordData[]>([]), sources = ref<RecordData[]>([]), detail = ref<RecordData | null>(null)
const config = ref<RecordData>({ configured: false }), candidate = ref<Candidate | null>(null)
const selectedEntities = reactive<Record<string, boolean>>({}), selectedRelationships = reactive<Record<string, boolean>>({})
const attributeTexts = reactive<Record<string, string>>({})
const glossaryRows = reactive<Record<string, { term: string; definition: string }[]>>({})
const error = ref(''), readError = ref(''), submitting = ref(false), loading = ref(false), sourceLoading = ref(false)
const selectedSource = ref(''), showSourceForm = ref(true), reviewed = ref(false), reason = ref('')
const form = reactive({ uri: `urn:document:${crypto.randomUUID()}`, revision: 'v1', content: '', actor: state.actor || '本地维护者', guidance: '', source_scope: 'full' })
const reviewReport = ref<RecordData | null>(null), reviewDecisions = ref<ReviewDecision[]>([])
const analyzing = ref(false), analysisError = ref('')
const reviewComplete = computed(() => !!reviewReport.value?.review_digest && (reviewReport.value.findings || []).every((finding: RecordData) => {
  if (finding.required === false) return true
  const decision = reviewDecisions.value.find(item => item.finding_id === finding.id)
  return !!decision?.reason.trim() && (finding.allowed_actions || []).some((action: RecordData) => action.value === decision?.action)
}))
const contentSize = computed(() => new TextEncoder().encode(form.content).length)
const contentLength = computed(() => Array.from(form.content).length)
const selectedKeys = computed(() => new Set(candidate.value?.entities.filter(item => selectedEntities[item.key]).map(item => item.key) || []))
const selectedRelations = computed(() => candidate.value?.relationships.filter(item => selectedRelationships[item.key] && endpointsSelected(item)) || [])
const running = computed(() => ['QUEUED', 'RUNNING'].includes(detail.value?.status))
const exchange = computed(() => detail.value?.result?.model_exchange)
const sourceContent = computed(() => detail.value?.source?.content || '')
const proposals = computed<RecordData[]>(() => detail.value?.proposals || [])
const hasProposal = computed(() => proposals.value.some(item => item.status !== 'rejected'))
const existing = computed<RecordData[]>(() => detail.value?.existing_entities || [])
let disposed = false, projectRead = 0, detailRead = 0, sourceRead = 0, reviewRead = 0, loadedCandidate = '', analyzedCandidate = ''
let pollTimer: ReturnType<typeof setTimeout> | undefined

const statusNames: Record<string, string> = { QUEUED: '等待抽取', RUNNING: '模型抽取中', SUCCEEDED: '候选待审阅', COMPLETED: '候选待审阅', FAILED: '抽取失败', STALE: '输入版本已变化' }
function statusName(status: string) { return statusNames[status] || status }
function clearObject(value: RecordData) { Object.keys(value).forEach(key => delete value[key]) }
function endpointsSelected(item: CandidateRelationship) { return selectedKeys.value.has(item.source_key) && selectedKeys.value.has(item.target_key) }
function nameOf(key: string) { return candidate.value?.entities.find(item => item.key === key)?.name || key }
function jobPath(id = '') { return `/projects/${encodeURIComponent(pid.value)}/knowledge/extraction${id ? '/' + encodeURIComponent(id) : ''}` }
function invalidateReview() {
  ++reviewRead; reviewReport.value = null; reviewDecisions.value = []; analyzedCandidate = ''
  analyzing.value = false; analysisError.value = ''; reviewed.value = false
}
watch([candidate, selectedEntities, selectedRelationships, attributeTexts, glossaryRows], invalidateReview, { deep: true, flush: 'sync' })
watch(reviewDecisions, () => { reviewed.value = false }, { deep: true, flush: 'sync' })

function installCandidate(job: RecordData) {
  if (!job.result?.candidate || loadedCandidate === job.id) return
  loadedCandidate = job.id
  candidate.value = JSON.parse(JSON.stringify(job.result.candidate))
  clearObject(selectedEntities); clearObject(selectedRelationships); clearObject(attributeTexts); clearObject(glossaryRows)
  for (const item of candidate.value!.entities) {
    item.existing ??= null
    item.acceptance_items ??= null
    selectedEntities[item.key] = true
    attributeTexts[item.key] = JSON.stringify(item.attributes || {}, null, 2)
    glossaryRows[item.key] = Object.entries(item.attributes?.glossary || {}).map(([term, definition]) => ({ term, definition: String(definition) }))
  }
  for (const item of candidate.value!.relationships) selectedRelationships[item.key] = true
  reviewed.value = false
}

async function loadJobs(project = pid.value) {
  const data = await request<RecordData>(`/extractions/projects/${encodeURIComponent(project)}/jobs`)
  if (disposed || project !== pid.value) return
  jobs.value = data.items
  config.value = data.model_config
}

async function loadSources(project = pid.value) {
  const data = await request<RecordData[]>(`/operations/sources?${new URLSearchParams({ project_id: project, limit: '200' })}`)
  if (!disposed && project === pid.value) sources.value = data.filter(source => source.format === 'text')
}

async function loadDetail(id = jobId.value) {
  if (!id) return
  const token = ++detailRead, project = pid.value
  const data = await request<RecordData>(`/extractions/jobs/${encodeURIComponent(id)}`)
  if (disposed || token !== detailRead || project !== pid.value || id !== jobId.value) return
  if (data.project_id !== project) throw new Error('此抽取记录不属于当前项目。')
  // 其他会话形成提案后，切换为原始候选只读视图，不能把本地未提交草稿当成模型结果。
  if (!hasProposal.value && (data.proposals || []).some((proposal: RecordData) => proposal.status !== 'rejected')) loadedCandidate = ''
  detail.value = data
  installCandidate(data)
}

function schedulePoll() {
  clearTimeout(pollTimer)
  if (disposed || !jobs.value.some(job => ['QUEUED', 'RUNNING'].includes(job.status)) && !running.value) return
  pollTimer = setTimeout(async () => {
    try {
      await Promise.all([loadJobs(), loadDetail()])
      readError.value = ''
    } catch (failure) {
      if (!disposed) readError.value = (failure as Error).message
    }
    schedulePoll()
  }, 2500)
}

watch(() => [pid.value, state.initialized], async () => {
  if (!state.initialized || !pid.value) return
  const token = ++projectRead
  loading.value = true
  jobs.value = []; sources.value = []; detail.value = null; candidate.value = null; loadedCandidate = ''
  selectedSource.value = ''; showSourceForm.value = !jobId.value
  error.value = ''; readError.value = ''
  Object.assign(form, { uri: `urn:document:${crypto.randomUUID()}`, revision: 'v1', content: '', guidance: '', source_scope: 'full' })
  clearTimeout(pollTimer)
  try {
    if (state.projectId !== pid.value) await selectProject(pid.value)
    if (token !== projectRead || disposed) return
    await Promise.all([loadJobs(), loadSources(), loadDetail()])
    if (token === projectRead) schedulePoll()
  } catch (failure) {
    if (token === projectRead && !disposed) readError.value = (failure as Error).message
  } finally {
    if (token === projectRead && !disposed) loading.value = false
  }
}, { immediate: true })

watch(jobId, async () => {
  ++detailRead
  detail.value = null; candidate.value = null; loadedCandidate = ''; reviewed.value = false; reason.value = ''
  showSourceForm.value = !jobId.value
  if (!state.initialized || !jobId.value) return
  try { await loadDetail(); schedulePoll() }
  catch (failure) { if (!disposed) readError.value = (failure as Error).message }
})

onBeforeUnmount(() => { disposed = true; ++projectRead; ++detailRead; ++sourceRead; ++reviewRead; clearTimeout(pollTimer) })

async function refresh() {
  readError.value = ''
  try { await Promise.all([loadJobs(), loadSources(), loadDetail()]); schedulePoll() }
  catch (failure) { if (!disposed) readError.value = (failure as Error).message }
}

async function chooseSource() {
  const token = ++sourceRead, project = pid.value
  if (!selectedSource.value) return
  sourceLoading.value = true; error.value = ''
  const source = sources.value.find(item => item.id === selectedSource.value)
  try {
    if (!source) throw new Error('请重新选择来源。')
    const data = await request<RecordData>(`/operations/sources/${encodeURIComponent(source.id)}/versions/${source.version}`)
    if (disposed || token !== sourceRead || project !== pid.value) return
    Object.assign(form, { uri: source.uri, revision: data.external_revision, content: data.content })
  } catch (failure) {
    if (!disposed && token === sourceRead) error.value = (failure as Error).message
  } finally {
    if (!disposed && token === sourceRead) sourceLoading.value = false
  }
}

async function chooseLatestSource() {
  const sourceId = detail.value?.source_id
  const uri = detail.value?.source?.uri || ''
  try {
    await loadSources()
    await router.push(jobPath())
    selectedSource.value = sources.value.some(source => source.id === sourceId) ? sourceId : ''
    if (selectedSource.value) await chooseSource()
    else Object.assign(form, { uri, revision: '', content: '' })
  } catch (failure) { if (!disposed) error.value = (failure as Error).message }
}

function nextSourceVersion() {
  selectedSource.value = ''
  form.revision = ''
}

async function readFile(event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  if (!file) return
  error.value = ''
  try {
    if (!/\.(md|markdown|txt)$/i.test(file.name)) throw new Error('请选择 UTF-8 编码的 Markdown 或纯文本文件。')
    if (file.size > 1024 * 1024) throw new Error('原文文件不能超过 1 MiB。')
    const content = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer())
    if (disposed) return
    if (content.includes('\0')) throw new Error('文件包含无法登记的空字符，请检查文本编码。')
    form.content = content
  } catch (failure) {
    if (!disposed) error.value = (failure as Error).message.includes('encoded') ? '文件须使用 UTF-8 编码，请转换后重新选择。' : (failure as Error).message
  } finally { input.value = '' }
}

async function startJob(retry = false) {
  if (submitting.value) return
  const project = pid.value
  submitting.value = true; error.value = ''
  try {
    let source: { source_id: string; version: number }
    if (retry && detail.value) source = { source_id: detail.value.source_id, version: detail.value.source_version }
    else if (selectedSource.value) {
      const known = sources.value.find(item => item.id === selectedSource.value)
      if (!known || sourceLoading.value) throw new Error('请先完成原文读取。')
      source = { source_id: known.id, version: known.version }
    } else {
      if (!form.content.trim()) throw new Error('请填写需要抽取的原文。')
      source = await request('/operations/source-snapshots', {
        project_id: project, source_uri: form.uri.trim(), source_revision: form.revision.trim(), content: form.content, actor: form.actor.trim(),
      })
      await loadSources(project)
    }
    const job = await request<RecordData>(`/extractions/projects/${encodeURIComponent(project)}/jobs`, {
      source_id: source.source_id, source_version: source.version, actor: form.actor.trim(),
      guidance: retry ? detail.value?.guidance || detail.value?.context?.guidance || detail.value?.request?.context?.guidance || '' : form.guidance.trim(),
      source_scope: retry ? detail.value?.context?.source_scope || 'full' : form.source_scope,
    })
    if (disposed || project !== pid.value) return
    await loadJobs(project)
    await router.push(jobPath(job.id))
    schedulePoll()
    notify('已登记抽取任务，完成后请核对候选知识与原文。')
  } catch (failure) {
    if (!disposed && project === pid.value) error.value = (failure as Error).message
  } finally { if (!disposed) submitting.value = false }
}

function bindExisting(item: CandidateEntity, event: Event) {
  const id = (event.target as HTMLSelectElement).value
  const record = existing.value.find(entity => entity.id === id)
  item.existing = record ? { id: record.id, version: record.version } : null
  reviewed.value = false
}

function boundRecord(item: CandidateEntity) { return existing.value.find(entity => entity.id === item.existing?.id) }
function lockedBinding(item: CandidateEntity) { return detail.value?.bindings?.some((binding: RecordData) => binding.key === item.key) }
function applySuggestedBinding(key: string, reference: RecordData) {
  const item = candidate.value?.entities.find(entity => entity.key === key)
  const record = existing.value.find(entity => entity.id === reference.id && entity.version === reference.version)
  if (!item || !record || item.kind !== record.kind || lockedBinding(item) || hasProposal.value) {
    analysisError.value = '此建议不能直接改变当前候选的身份，请在候选中核对已确认的来源绑定。'
    return
  }
  item.existing = { id: record.id, version: record.version }
  notify('已关联到已有知识，请重新分析增量并核对本次修改。')
}
function addAcceptance(item: CandidateEntity) {
  item.acceptance_items ??= []
  item.acceptance_items.push({ key: `acceptance-${crypto.randomUUID()}`, text: '', evidence: [{ quote: '', start: null }] })
  reviewed.value = false
}

function buildCandidate(allowEmpty = false): Candidate {
  if (!candidate.value) throw new Error('请先完成知识抽取。')
  const entities = candidate.value.entities.filter(item => selectedEntities[item.key]).map(item => {
    let attributes: RecordData
    try { attributes = JSON.parse(attributeTexts[item.key]) }
    catch { throw new Error(`${item.name} 的高级属性不是有效 JSON。`) }
    if (!attributes || Array.isArray(attributes) || typeof attributes !== 'object') throw new Error(`${item.name} 的属性须为 JSON 对象。`)
    if (item.kind === 'Requirement') {
      if (item.attributes.requirement_type !== undefined) attributes.requirement_type = item.attributes.requirement_type
      else delete attributes.requirement_type
      delete attributes.acceptance_criteria
    }
    if (item.kind === 'Domain') {
      const terms = glossaryRows[item.key].filter(row => row.term.trim() || row.definition.trim())
      if (terms.some(row => !row.term.trim() || !row.definition.trim())) throw new Error(`${item.name} 的术语和解释须同时填写。`)
      if (new Set(terms.map(row => row.term.trim())).size !== terms.length) throw new Error(`${item.name} 存在重复术语。`)
      if (terms.length || Object.hasOwn(item.attributes, 'glossary')) attributes.glossary = Object.fromEntries(terms.map(row => [row.term.trim(), row.definition.trim()]))
      else delete attributes.glossary
    }
    return { ...item, attributes }
  })
  if (!allowEmpty && !entities.length) throw new Error('请至少选择一项知识。只有移除建议时，请到项目知识中提出生命周期修订提案。')
  return { ...candidate.value, entities, relationships: selectedRelations.value }
}

async function propose() {
  if (submitting.value || !detail.value || !reviewed.value || hasProposal.value || !reviewComplete.value) return
  const project = pid.value, id = detail.value.id
  submitting.value = true; error.value = ''
  try {
    const currentCandidate = buildCandidate()
    if (JSON.stringify(currentCandidate) !== analyzedCandidate) { invalidateReview(); throw new Error('候选内容已变化，请重新分析本轮增量与影响。') }
    const proposal = await request<RecordData>(`/extractions/jobs/${encodeURIComponent(id)}/proposals`, {
      actor: form.actor.trim(), reason: reason.value.trim(), candidate: currentCandidate,
      review_digest: reviewReport.value!.review_digest, decisions: reviewDecisions.value,
    })
    if (disposed || project !== pid.value || id !== jobId.value) return
    notify('抽取结果已形成待审提案，请继续校验并确认提交。')
    await router.push(`/projects/${encodeURIComponent(project)}/proposals/${encodeURIComponent(proposal.id)}`)
  } catch (failure) {
    if (!disposed && project === pid.value && id === jobId.value) error.value = (failure as Error).message
  } finally { if (!disposed) submitting.value = false }
}

async function analyzeReview() {
  if (!detail.value || hasProposal.value || submitting.value) return
  invalidateReview()
  const token = reviewRead, project = pid.value, id = detail.value.id
  analyzing.value = true
  try {
    const body = buildCandidate(true), fingerprint = JSON.stringify(body)
    const report = await request<RecordData>(`/extractions/jobs/${encodeURIComponent(id)}/review`, { candidate: body })
    if (disposed || token !== reviewRead || project !== pid.value || id !== jobId.value || fingerprint !== JSON.stringify(buildCandidate(true))) return
    reviewReport.value = report; analyzedCandidate = fingerprint
  } catch (failure) {
    if (!disposed && token === reviewRead && project === pid.value) analysisError.value = (failure as Error).message
  } finally { if (!disposed && token === reviewRead) analyzing.value = false }
}
</script>

<template>
  <section class="panel project-panel extraction-workspace">
    <div class="extraction-heading">
      <div><RouterLink :to="`/projects/${pid}/knowledge`" class="text-button">← 返回项目知识</RouterLink><h2>文档知识抽取</h2></div>
      <button type="button" class="button secondary" :disabled="submitting || loading" @click="refresh">刷新记录</button>
    </div>
    <p>从原文提取需求、业务规则和领域概念，逐项核对证据与已有知识，再提交提案审阅。</p>
    <p v-if="readError" class="info-box error" role="alert">{{ readError }}</p>
    <p v-if="error" class="info-box error" role="alert">{{ error }}</p>
    <p v-if="loading" role="status">正在读取抽取记录…</p>
    <div v-if="!loading && !config.configured" class="info-box warning">模型服务尚未配置完成，请按服务端模型配置说明设置后再发起抽取。{{ config.errors?.join(' / ') }}</div>

    <div class="extraction-layout">
      <aside class="extraction-history" aria-label="知识抽取历史">
        <RouterLink :to="jobPath()" class="button secondary">登记原文并抽取</RouterLink>
        <h3>抽取记录</h3>
        <p v-if="!jobs.length && !loading" class="section-label">此项目还没有模型抽取记录。</p>
        <RouterLink v-for="job in jobs" :key="job.id" :to="jobPath(job.id)" class="extraction-job" :class="{ selected: job.id === jobId }">
          <strong>{{ statusName(job.status) }}</strong>
          <span>{{ job.source_uri || job.source?.uri || '原文快照' }} · v{{ job.source_version }}</span>
          <small>{{ dateTime(job.created_at) }}</small>
        </RouterLink>
      </aside>

      <div class="extraction-main">
        <section v-if="!jobId || showSourceForm" class="extraction-section" aria-label="登记抽取原文">
          <h3>原文与抽取范围</h3>
          <form @submit.prevent="startJob()">
            <label class="field">选择原文来源
              <select v-model="selectedSource" :disabled="submitting" @change="chooseSource">
                <option value="">登记新原文或新版本</option>
                <option v-for="source in sources" :key="source.id" :value="source.id">{{ source.uri }} · {{ source.external_revision }} · 快照 v{{ source.version }}</option>
              </select>
            </label>
            <div class="two-columns">
              <label class="field">来源标识<input v-model="form.uri" required :readonly="!!selectedSource" :disabled="submitting" maxlength="4000"></label>
              <label class="field">文档版本<input v-model="form.revision" required :readonly="!!selectedSource" :disabled="submitting" placeholder="例如 v1、v1.1" maxlength="4000"></label>
            </div>
            <p class="section-label">同一文档更新时保持来源标识，填写新的文档版本。每版原文独立保存。</p>
            <button v-if="selectedSource" type="button" class="text-button" :disabled="submitting || sourceLoading" @click="nextSourceVersion">以这份原文登记新版本</button>
            <label v-else class="field">读取本地文档<input type="file" accept=".md,.markdown,.txt,text/plain,text/markdown" :disabled="submitting" @change="readFile"></label>
            <label class="field">原文内容<textarea v-model="form.content" rows="12" required :readonly="!!selectedSource" :disabled="submitting || sourceLoading" placeholder="粘贴需求或业务说明，保留原文的否定、例外和边界条件。"/></label>
            <p class="section-label">{{ contentLength }} / 40000 个码点 · {{ (contentSize / 1024).toFixed(1) }} KiB · 支持 UTF-8 Markdown 和纯文本。单次模型抽取最多 40000 个码点，请将更长文档按业务范围拆分。</p>
            <label class="field">原文范围<select v-model="form.source_scope" aria-label="原文范围" :disabled="submitting"><option value="full">完整文档</option><option value="delta">增量补充</option></select></label>
            <p class="section-label">完整文档会提示同一来源中本轮缺失的旧知识，供你确认是否继续保留；增量补充只处理这次新增或修改的内容，不将未出现的旧知识视为移除。两种方式都不会自动删除知识。</p>
            <label class="field">本轮抽取说明（可选）<textarea v-model="form.guidance" rows="2" maxlength="4000" :disabled="submitting" placeholder="例如重点识别容量限制及重复提交的例外规则。"/></label>
            <label class="field">操作署名<input v-model="form.actor" required maxlength="4000" :disabled="submitting"></label>
            <p class="section-label">发起抽取会将原文和本项目相关知识发送给已配置的模型服务。</p>
            <button class="button primary" :disabled="submitting || sourceLoading || !config.configured || !form.actor.trim() || !form.content.trim() || contentSize > 1024 * 1024 || contentLength > 40000">{{ submitting ? '正在登记任务…' : '登记原文并开始抽取' }}</button>
          </form>
        </section>

        <template v-if="detail">
          <section class="extraction-section" aria-label="当前抽取记录">
            <div class="extraction-heading"><h3>{{ statusName(detail.status) }}</h3><small>{{ dateTime(detail.created_at) }}</small></div>
            <p>{{ detail.source?.uri }} · 文档 {{ detail.source?.external_revision }} · 固定快照 v{{ detail.source_version }}</p>
            <p class="section-label">本轮范围：{{ detail.context?.source_scope === 'delta' ? '增量补充，仅处理本次变更' : '完整文档，复核同来源缺失项' }}</p>
            <p v-if="running" role="status">正在等待模型结果，页面会自动更新。原文版本与已有知识版本已固定。</p>
            <p v-if="detail.result?.diagnostic" :class="['info-box', { error: detail.status === 'FAILED' }]" role="status">{{ detail.result.diagnostic }}</p>
            <details><summary>查看本轮固定原文</summary><pre class="raw-text" tabindex="0">{{ sourceContent }}</pre></details>
            <div v-if="detail.status === 'FAILED'" class="retry-panel">
              <label class="field">操作署名<input v-model="form.actor" required></label>
              <button class="button secondary" :disabled="submitting || !config.configured || !form.actor.trim()" @click="startJob(true)">使用同一原文重新抽取</button>
              <p class="section-label">重试会新增记录，保留本次失败的原因和模型回复。</p>
            </div>
            <div v-if="detail.status === 'STALE'" class="retry-panel">
              <button type="button" class="button secondary" :disabled="submitting" @click="chooseLatestSource">选择最新原文重新抽取</button>
              <p class="section-label">本轮输入已过期，请基于最新原文和知识版本建立新任务。历史记录继续保留。</p>
            </div>
            <details v-if="exchange" class="model-exchange">
              <summary>查看平台发送给模型的输入和模型回复</summary>
              <h4>平台发送给模型</h4>
              <p>模型 {{ exchange.request?.model || detail.result?.trace?.model }}</p>
              <div v-for="(message, index) in exchange.request?.messages || []" :key="index">
                <strong>{{ message.role === 'system' ? '系统指令' : message.role === 'user' ? '任务内容和上下文' : message.role }}</strong>
                <pre class="raw-text" tabindex="0">{{ message.content }}</pre>
              </div>
              <h4>模型回复平台</h4>
              <pre v-if="exchange.response?.content" class="raw-text" tabindex="0" aria-label="模型回复原文">{{ exchange.response.content }}</pre>
              <p v-else>尚无可查看的回复文本。</p>
              <p v-if="exchange.response?.finish_reason">结束原因 {{ exchange.response.finish_reason }}</p>
              <p v-if="exchange.response?.refusal">{{ exchange.response.refusal }}</p>
            </details>
            <details v-if="detail.result?.trace"><summary>模型与版本追踪</summary><pre class="raw-text">{{ JSON.stringify(detail.result.trace, null, 2) }}</pre></details>
          </section>

          <section v-if="proposals.length" class="extraction-section" aria-label="本次抽取的提案">
            <h3>已建立的审阅提案</h3>
            <p v-for="proposal in proposals" :key="proposal.id">
              <RouterLink :to="`/projects/${pid}/proposals/${proposal.id}`">{{ proposal.title }}</RouterLink> · <StatusPill :status="proposal.status"/>
            </p>
            <RouterLink :to="`/projects/${pid}/knowledge`">返回项目知识查看已提交知识与历史版本 →</RouterLink>
            <p v-if="hasProposal" class="section-label">本轮已有待审或已提交提案。下方保留模型原始候选，人工修改后的内容请在对应提案中查看。提案被拒绝后可重新编辑候选。</p>
          </section>

          <form v-if="candidate" class="candidate-review" @submit.prevent="propose">
            <section class="extraction-section">
              <h3>审阅候选知识</h3>
              <p>{{ candidate.summary }}</p>
              <div class="info-box">引文匹配表示能够定位原文。请逐项确认原文是否支持候选的业务含义，特别是例外、否定和边界条件。候选暂不写入知识库，确认提案后才生效。</div>
              <div v-if="candidate.warnings.length" class="info-box warning"><strong>模型提示的待复核事项</strong><ul><li v-for="(warning, index) in candidate.warnings" :key="index">{{ warning }}</li></ul></div>
              <p v-if="!candidate.entities.length">本轮没有可新增或修订的知识候选。仍可分析已有知识缺失项和模型提出的移除建议，请结合原文复核。</p>
            </section>

            <article v-for="item in candidate.entities" :key="item.key" class="candidate-card" :class="{ excluded: !selectedEntities[item.key] }">
              <div class="candidate-heading"><label class="check-field"><input v-model="selectedEntities[item.key]" type="checkbox" :disabled="submitting || hasProposal" @change="reviewed = false"><strong>纳入 {{ kinds[item.kind] }} · {{ item.name }}</strong></label></div>
              <p class="stable-key">稳定标识 <code>{{ item.key }}</code></p>
              <fieldset :disabled="!selectedEntities[item.key] || submitting" @input="reviewed = false">
                <label class="field">知识名称<input v-model="item.name" required maxlength="300" :readonly="hasProposal" @input="reviewed = false"></label>
                <label class="field">知识说明<textarea v-model="item.description" rows="3" maxlength="12000" :readonly="hasProposal" @input="reviewed = false"/></label>
                <label class="field">关联已有知识
                  <select :value="item.existing?.id || ''" :disabled="lockedBinding(item) || hasProposal" @change="bindExisting(item, $event)">
                    <option value="">作为新知识提出</option>
                    <option v-for="record in existing.filter(row => row.kind === item.kind && row.status !== 'archived')" :key="record.id" :value="record.id">{{ record.name }} · v{{ record.version }}</option>
                  </select>
                </label>
                <p v-if="lockedBinding(item)" class="section-label">同一来源的此标识已绑定已有知识，后续版本沿用此身份。</p>
                <template v-if="item.existing">
                  <p class="section-label">本次修订基于固定知识版本 v{{ item.existing.version }}。修改名称会保留同一知识身份。</p>
                  <details><summary>查看修改前的知识</summary><p>{{ boundRecord(item)?.description }}</p><pre class="raw-text">{{ JSON.stringify(boundRecord(item)?.attributes, null, 2) }}</pre><RouterLink :to="{ path: `/projects/${pid}/knowledge`, query: { [item.kind === 'Requirement' ? 'requirement' : 'entity']: item.existing.id, version: String(item.existing.version) } }">打开此历史知识版本 →</RouterLink></details>
                </template>
                <label v-if="item.kind === 'Requirement'" class="field">需求类别<select v-model="item.attributes.requirement_type" :disabled="hasProposal" @change="reviewed = false"><option :value="undefined">沿用已有值</option><option :value="null">未指定</option><option value="functional">功能需求</option><option value="nonfunctional">非功能需求</option></select></label>
                <section v-if="item.kind === 'Requirement'" class="acceptance-items" aria-label="验收条件">
                  <h4>验收条件</h4>
                  <template v-if="item.acceptance_items == null">
                    <p class="section-label">本轮保留已有验收条件。需要替换时，请完整填写本轮条件及证据。</p>
                    <ul v-if="(boundRecord(item)?.attributes.acceptance_criteria || item.attributes.acceptance_criteria || []).length"><li v-for="(text, index) in boundRecord(item)?.attributes.acceptance_criteria || item.attributes.acceptance_criteria || []" :key="index">{{ text }}</li></ul>
                    <button v-if="!hasProposal" type="button" class="text-button" @click="item.acceptance_items = []; reviewed = false">本轮重新指定验收条件</button>
                  </template>
                  <template v-else>
                    <p v-if="!item.acceptance_items.length" class="info-box warning">当前条件列表为空，提交将明确清空本轮知识的验收条件。</p>
                    <button v-if="!hasProposal" type="button" class="text-button" @click="item.acceptance_items = null; reviewed = false">恢复保留已有条件</button>
                  </template>
                  <article v-for="(condition, conditionIndex) in item.acceptance_items || []" :key="condition.key" class="acceptance-item">
                    <label class="field">验收条件<textarea v-model="condition.text" required rows="2" maxlength="4000" :readonly="hasProposal" @input="reviewed = false"/></label>
                    <p class="stable-key">条件标识 <code>{{ condition.key }}</code></p>
                    <ExtractionEvidenceEditor v-model="condition.evidence" :content="sourceContent" :readonly="hasProposal" @update:model-value="reviewed = false"/>
                    <button v-if="!hasProposal" type="button" class="text-button" @click="item.acceptance_items!.splice(conditionIndex, 1); reviewed = false">移除此验收条件</button>
                  </article>
                  <button v-if="item.acceptance_items != null && !hasProposal" type="button" class="text-button" @click="addAcceptance(item)">增加验收条件及证据</button>
                </section>
                <section v-if="item.kind === 'Domain'" class="glossary-editor" aria-label="领域术语">
                  <h4>领域术语</h4>
                  <div v-for="(term, index) in glossaryRows[item.key]" :key="index" class="glossary-row">
                    <label class="field">术语<input v-model="term.term" :readonly="hasProposal" @input="reviewed = false"></label>
                    <label class="field">解释<textarea v-model="term.definition" rows="2" :readonly="hasProposal" @input="reviewed = false"/></label>
                    <button v-if="!hasProposal" type="button" class="text-button" @click="glossaryRows[item.key].splice(index, 1); reviewed = false">移除</button>
                  </div>
                  <button v-if="!hasProposal" type="button" class="text-button" @click="glossaryRows[item.key].push({ term: '', definition: '' }); reviewed = false">增加术语</button>
                </section>
                <ExtractionEvidenceEditor v-model="item.evidence" :content="sourceContent" :readonly="hasProposal" @update:model-value="reviewed = false"/>
                <details class="advanced-attributes"><summary>其他类型属性（高级编辑）</summary><label class="field">属性 JSON<textarea v-model="attributeTexts[item.key]" rows="7" class="json-editor" :readonly="hasProposal" @input="reviewed = false"/></label><p class="section-label">需求类别、验收条件和领域术语以页面上方填写的内容为准。请保留与本次变更无关的属性。</p></details>
              </fieldset>
            </article>

            <section v-if="candidate.relationships.length" class="extraction-section" aria-label="候选知识关系">
              <h3>审阅候选关系</h3>
              <article v-for="item in candidate.relationships" :key="item.key" class="relationship-card">
                <label class="check-field"><input v-model="selectedRelationships[item.key]" type="checkbox" :disabled="!endpointsSelected(item) || submitting || hasProposal" @change="reviewed = false"><strong>{{ nameOf(item.source_key) }} → {{ relationshipTypes[item.type] }} → {{ nameOf(item.target_key) }}</strong></label>
                <p v-if="!endpointsSelected(item)" class="section-label">有端点知识未纳入，这条关系将一并排除。</p>
                <p class="stable-key">关系标识 <code>{{ item.key }}</code></p>
                <fieldset :disabled="!selectedRelationships[item.key] || !endpointsSelected(item) || submitting" @input="reviewed = false">
                  <label class="field">关系依据<textarea v-model="item.rationale" required rows="2" maxlength="4000" :readonly="hasProposal" @input="reviewed = false"/></label>
                  <ExtractionEvidenceEditor v-model="item.evidence" :content="sourceContent" :readonly="hasProposal" @update:model-value="reviewed = false"/>
                </fieldset>
              </article>
            </section>

            <section v-if="!hasProposal" class="extraction-section" aria-label="增量分析操作">
              <h3>分析本轮增量与影响</h3>
              <p>核对新增、修订、重复或冲突提示，再检查可能受影响的知识与设计。修改候选或选择范围后需重新分析。</p>
              <button type="button" class="button secondary" :disabled="submitting || analyzing" @click="analyzeReview">{{ analyzing ? '正在分析固定版本…' : reviewReport ? '重新分析本轮增量与影响' : '分析本轮增量与影响' }}</button>
              <p v-if="analysisError" class="info-box error" role="alert">{{ analysisError }}</p>
              <p v-if="!detail.context?.review_contract_version" class="section-label">这是升级前的抽取记录。本次分析可比较固定内容，但不会补造当时的模型语义判断和影响快照。</p>
              <ExtractionReview v-if="reviewReport" v-model:decisions="reviewDecisions" :report="reviewReport" :project-id="pid" :disabled="submitting" @bind="applySuggestedBinding"/>
            </section>

            <section v-if="!hasProposal" class="extraction-section" aria-label="建立抽取提案">
              <h3>建立待审提案</h3>
              <p>本次纳入 {{ selectedKeys.size }} 项知识、{{ selectedRelations.length }} 条关系。未纳入的候选保留在模型原始结果中。</p>
              <p v-if="!selectedKeys.size" class="info-box warning">当前没有实际知识写入，不能从这里建立提案。只有移除建议时，请到<RouterLink :to="`/projects/${pid}/knowledge`">项目知识</RouterLink>选中已有对象，另提生命周期修订提案。此处不会删除或归档知识。</p>
              <label class="field">操作署名<input v-model="form.actor" required :disabled="submitting"></label>
              <label class="field">审阅与修改依据<textarea v-model="reason" required rows="3" maxlength="4000" :disabled="submitting" placeholder="说明确认的范围、绑定已有知识的依据，以及对模型候选的修改。"/></label>
              <p v-if="!reviewReport" class="info-box warning">请先分析本轮增量与影响，再形成提案。</p>
              <p v-else-if="!reviewComplete" class="info-box warning">请为每条必需复核事项选择处理方式并填写依据。</p>
              <label class="check-field"><input v-model="reviewed" type="checkbox" :disabled="submitting || !reviewComplete"><span>我已核对选中知识、原文证据、已有版本绑定和增量分析，确认形成待审提案。</span></label>
              <p v-if="error" class="info-box error" role="alert">{{ error }}</p>
              <button class="button primary" :disabled="submitting || !reviewed || !reviewComplete || !selectedKeys.size || !form.actor.trim() || !reason.trim()">{{ submitting ? '正在建立提案…' : '建立提案并进入审阅' }}</button>
              <p class="section-label">下一步在项目提案页查看修改前后内容，校验通过后由你确认提交。</p>
            </section>
          </form>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.extraction-workspace { min-width: 0; }
.extraction-heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.extraction-heading h2 { margin-top: 10px; }
.extraction-layout { display: grid; grid-template-columns: minmax(185px, 235px) minmax(0, 1fr); gap: 18px; margin-top: 20px; align-items: start; }
.extraction-history { position: sticky; top: 12px; max-height: 80vh; overflow: auto; padding-inline-end: 5px; }
.extraction-history h3 { margin: 18px 0 10px; }
.extraction-job { display: grid; gap: 5px; padding: 11px; margin: 6px 0; border: 1px solid var(--line); border-radius: 7px; text-decoration: none; color: var(--ink); overflow-wrap: anywhere; }
.extraction-job.selected { background: var(--blue-soft); border-color: #80aac5; }
.extraction-job span, .extraction-job small { color: var(--muted); font-size: 12px; }
.extraction-main, .candidate-card, .extraction-section { min-width: 0; }
.extraction-section, .candidate-card { padding: 17px; border: 1px solid var(--line); border-radius: 9px; margin-bottom: 15px; }
.extraction-section h3, .candidate-card h4 { margin: 0 0 9px; }
.two-columns { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 12px; }
.candidate-card.excluded { background: #f6f7f8; }
.candidate-card.excluded fieldset { opacity: .65; }
fieldset { border: 0; padding: 0; margin: 0; min-width: 0; }
.stable-key { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; margin: 6px 0; }
.raw-text { max-height: 23rem; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 10px; background: var(--bg); font: inherit; }
.model-exchange, .advanced-attributes, .retry-panel { margin-top: 14px; }
.acceptance-items, .glossary-editor { margin-top: 14px; }
.acceptance-item { padding: 12px; border: 1px solid var(--line); border-radius: 7px; margin: 8px 0; }
.glossary-row { display: grid; grid-template-columns: minmax(90px, 1fr) minmax(140px, 2fr) auto; gap: 9px; align-items: center; }
.relationship-card { border-top: 1px solid var(--line); padding: 13px 0; }
.candidate-review .info-box ul { margin-bottom: 0; }
details > summary { cursor: pointer; color: var(--blue); }
.extraction-section > form > button { margin-top: 12px; }
@media (max-width: 950px) {
  .extraction-layout { grid-template-columns: minmax(0, 1fr); }
  .extraction-history { position: static; max-height: 17rem; }
}
@media (max-width: 600px) {
  .two-columns, .glossary-row { grid-template-columns: minmax(0, 1fr); gap: 0; }
  .extraction-heading { align-items: start; }
  .extraction-section, .candidate-card { padding: 12px; }
}
</style>
