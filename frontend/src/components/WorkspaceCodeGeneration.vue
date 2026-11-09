<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ApiError, request } from '../api/client'
import { dateTime } from '../lib/display'
import { notify } from '../store/workbench'
import type { RecordData } from '../types'
import CodeChangeReview from './CodeChangeReview.vue'

interface Generation {
  id: string
  status: 'QUEUED' | 'RUNNING' | 'REVIEW_PENDING' | 'APPLIED' | 'FAILED' | 'STALE'
  diagnostic: string
  created_at: string
  base_snapshot_id: string | null
  applied_snapshot_id: string | null
  summary?: string
  changes?: { path: string; operation: 'add' | 'modify'; before: string | null; after: string }[]
}

const props = defineProps<{
  workspaceId: string
  languageLabel: string
  workspaceVersion: number
  snapshotId: string | null
  hasUnsavedFiles: boolean
  disabled: boolean
  actor: string
  reason: string
}>()
const emit = defineEmits<{ busy: [value: boolean]; applied: [workspace: RecordData]; refreshWorkspace: [] }>()
const labels: Record<Generation['status'], string> = {
  QUEUED: '排队中', RUNNING: '生成中', REVIEW_PENDING: '待审阅',
  APPLIED: '已保存', FAILED: '生成失败', STALE: '基线已变化',
}
const jobs = ref<Generation[]>([])
const review = ref<Generation | null>(null)
const model = ref<{ configured: boolean; model?: string; message?: string } | null>(null)
const error = ref('')
const loading = ref(false)
const reviewing = ref(false)
const busy = ref(false)
const confirmed = ref(false)
const baseChanged = computed(() => review.value !== null && review.value.base_snapshot_id !== props.snapshotId)
const isGenerating = computed(() => jobs.value.some(job => job.status === 'QUEUED' || job.status === 'RUNNING'))
const canWrite = computed(() => !busy.value && !props.disabled && !!props.actor.trim() && !!props.reason.trim())
const canGenerate = computed(() => canWrite.value && !isGenerating.value && !!model.value?.configured)
const canApply = computed(() => canWrite.value && confirmed.value && review.value?.status === 'REVIEW_PENDING'
  && !baseChanged.value && !props.hasUnsavedFiles && !!review.value.changes?.length)
const endpoint = `/engineering/workspaces/${props.workspaceId}/code-generations`
let timer: ReturnType<typeof setTimeout> | undefined
let loadVersion = 0
let reviewVersion = 0

watch(() => props.snapshotId, () => { confirmed.value = false })

function stopPoll() {
  if (timer) clearTimeout(timer)
  timer = undefined
}

function schedulePoll() {
  stopPoll()
  if (isGenerating.value && !busy.value) timer = setTimeout(() => { void loadJobs() }, 2000)
}

async function loadJobs(refreshModel = false) {
  const version = ++loadVersion
  stopPoll()
  if (refreshModel) { loading.value = true; error.value = '' }
  try {
    const [current, config] = await Promise.all([
      request<Generation[]>(endpoint),
      refreshModel ? request<NonNullable<typeof model.value>>('/design/config') : Promise.resolve(model.value),
    ])
    if (version !== loadVersion) return
    jobs.value = current
    model.value = config
    if (review.value) {
      const job = current.find(item => item.id === review.value?.id)
      if (job && job.status !== review.value.status) {
        review.value = { ...review.value, ...job }
        confirmed.value = false
      }
    }
  } catch (failure) {
    if (version === loadVersion) error.value = (failure as Error).message
  } finally {
    if (version === loadVersion) { loading.value = false; schedulePoll() }
  }
}

async function openReview(jobId: string) {
  const version = ++reviewVersion
  reviewing.value = true; confirmed.value = false; error.value = ''; review.value = null
  try {
    const result = await request<Generation>(`${endpoint}/${jobId}`)
    if (version === reviewVersion) review.value = result
  } catch (failure) {
    if (version === reviewVersion) error.value = (failure as Error).message
  } finally {
    if (version === reviewVersion) reviewing.value = false
  }
}

function operation() {
  return { expected_version: props.workspaceVersion, actor: props.actor.trim(), reason: props.reason.trim() }
}

async function generate() {
  if (!canGenerate.value) return
  busy.value = true; emit('busy', true); stopPoll(); ++loadVersion; error.value = ''
  try {
    const job = await request<Generation>(endpoint, operation())
    jobs.value = [job, ...jobs.value.filter(item => item.id !== job.id)]
    notify('已提交代码生成任务，可在下方查看进度。', 'success')
    await loadJobs()
  } catch (failure) {
    if (failure instanceof ApiError && failure.status === 409) {
      emit('refreshWorkspace')
      await loadJobs()
    }
    error.value = (failure as Error).message
  } finally {
    busy.value = false; emit('busy', false); schedulePoll()
  }
}

async function apply() {
  if (!canApply.value || !review.value) return
  busy.value = true; emit('busy', true); stopPoll(); ++loadVersion; error.value = ''
  try {
    const result = await request<RecordData>(`${endpoint}/${review.value.id}/apply`, operation())
    emit('applied', result)
    review.value = null
    notify('代码变更已保存为新的工作区快照。', 'success')
    await loadJobs()
  } catch (failure) {
    if (failure instanceof ApiError && failure.status === 409) {
      emit('refreshWorkspace')
      await loadJobs()
    }
    error.value = (failure as Error).message
  } finally {
    confirmed.value = false; busy.value = false; emit('busy', false); schedulePoll()
  }
}

onMounted(() => { void loadJobs(true) })
onBeforeUnmount(() => { ++loadVersion; ++reviewVersion; stopPoll() })
</script>

<template>
  <section class="panel project-panel">
    <div class="studio-heading">
      <h3>代码生成与审阅</h3>
      <button type="button" class="text-button" :disabled="loading || busy" @click="loadJobs(true)">刷新生成状态</button>
    </div>
    <p>本次生成语言为 {{ languageLabel }}。发起生成会将已确认的设计、当前已保存的文件和本次操作依据发送给配置的模型服务。生成完成后，请审阅文件变更，再确认保存。</p>
    <p v-if="model" class="info-box" :class="{ warning: !model.configured }">
      {{ model.configured ? `模型服务已配置 · ${model.model}` : model.message || '请先配置模型服务。' }}
    </p>
    <p v-if="error" class="info-box error" role="alert">{{ error }}</p>
    <div class="studio-actions">
      <button type="button" class="button primary" :disabled="!canGenerate" @click="generate">生成代码候选</button>
      <span v-if="isGenerating" role="status">正在等待代码候选，页面会更新生成状态。</span>
    </div>
    <div v-if="jobs.length" class="code-job-list" aria-label="代码生成记录">
      <article v-for="job in jobs" :key="job.id" class="editor-box">
        <div class="studio-heading">
          <strong>{{ labels[job.status] || job.status }}</strong>
          <small>{{ dateTime(job.created_at, true) }}</small>
        </div>
        <p v-if="job.summary">{{ job.summary }}</p>
        <p v-if="job.diagnostic">{{ job.diagnostic }}</p>
        <button v-if="['REVIEW_PENDING', 'APPLIED'].includes(job.status)" type="button" class="button secondary"
          :disabled="busy || reviewing" @click="openReview(job.id)">审阅代码变更</button>
      </article>
    </div>
    <p v-if="reviewing" role="status">正在读取文件变更…</p>
    <article v-if="review" class="editor-box" aria-label="候选代码审阅">
      <div class="studio-heading"><h4>候选代码审阅</h4><span class="studio-badge">{{ labels[review.status] }}</span></div>
      <p v-if="review.summary">{{ review.summary }}</p>
      <CodeChangeReview :changes="review.changes || []" />
      <template v-if="review.status === 'REVIEW_PENDING'">
        <p v-if="baseChanged" class="info-box warning">工作区已保存新版本。请基于当前快照重新生成代码候选。</p>
        <p v-else-if="hasUnsavedFiles" class="info-box warning">工程文件还有未保存的编辑。请先保存编辑或恢复已保存文件，再确认代码变更。</p>
        <label class="check-field">
          <input v-model="confirmed" type="checkbox" :disabled="busy || disabled || baseChanged || hasUnsavedFiles">
          我已审阅全部候选文件及测试变更，确认保存为新的工作区快照。
        </label>
        <div class="studio-actions">
          <button type="button" class="button primary" :disabled="!canApply" @click="apply">确认保存到工程工作区</button>
        </div>
      </template>
    </article>
  </section>
</template>

<style scoped>
.code-job-list { max-height: 320px; overflow-y: auto; margin-top: 12px; }
</style>
