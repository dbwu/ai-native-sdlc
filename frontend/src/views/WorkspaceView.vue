<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ApiError, request } from '../api/client'
import { notify, selectProject, state } from '../store/workbench'
import { dateTime } from '../lib/display'
import type { RecordData } from '../types'
import WorkspaceCodeGeneration from '../components/WorkspaceCodeGeneration.vue'
import WorkspaceInterfaceComparison from '../components/WorkspaceInterfaceComparison.vue'

const route = useRoute(), pid = computed(() => String(route.params.projectId || ''))
const workspaces = ref<RecordData[]>([]), designs = ref<RecordData[]>([]), executor = ref<RecordData>({ configured: false, available: false })
const detail = ref<RecordData | null>(null), selected = ref(''), error = ref(''), busy = ref(false), loading = ref(false), executing = ref(false), cancelling = ref(false)
const codeBusy = ref(false)
const actor = ref(state.actor || ''), reason = ref(''), showCreate = ref(false)
const languageLabels = { python: 'Python', c: 'C', cpp: 'C++', java: 'Java' }
type WorkspaceLanguage = keyof typeof languageLabels
const executionProfiles = { python: 'python_unittest', c: 'c_native', cpp: 'cpp_native', java: 'java_native' } as const
const profileLabels: Record<string, string> = {
  python_unittest: 'Python unittest', c_native: 'C 编译与测试', cpp_native: 'C++ 编译与测试', java_native: 'Java 编译与测试',
}
const form = reactive({ name: '', design: '', language: 'python' as WorkspaceLanguage, sourceSnapshot: '' })
const files = ref<{path: string; content: string}[]>([])
const labels: Record<string, string> = {
  RUNNING: '执行中', SUCCEEDED: '执行通过', FAILED: '执行失败', COMPILE_FAILED: '编译失败', TEST_FAILED: '测试失败',
  TIMED_OUT: '执行超时', CANCELLED: '已取消', STALE: '版本已变化',
}
const phaseLabels: Record<string, string> = { passed: '通过', failed: '失败', not_run: '未运行' }
const workspace = computed(() => detail.value?.workspace)
const workspaceLanguage = computed<WorkspaceLanguage>(() => workspace.value?.language ?? 'python')
const executionProfile = computed(() => executionProfiles[workspaceLanguage.value])
const usesNativeRunner = computed(() => executionProfile.value !== 'python_unittest')
const currentExecutor = computed(() => executor.value.profiles?.[executionProfile.value] ?? (usesNativeRunner.value
  ? { configured: false, available: false, message: executor.value.loadError }
  : executor.value))
const snapshot = computed(() => detail.value?.latest_snapshot)
const snapshotSources = computed(() => workspaces.value.filter(item => item.language === form.language && item.latest_snapshot_id))
const controlsLocked = computed(() => busy.value || executing.value || codeBusy.value || loading.value)
const canWrite = computed(() => !controlsLocked.value && !!actor.value.trim() && !!reason.value.trim())
const activeExecution = computed(() => (detail.value?.executions || []).find((e: RecordData) => e.status === 'RUNNING'))
const savedFiles = computed(() => (snapshot.value?.files || []).map((f: RecordData) => ({ path: f.path, content: f.content })))
const hasUnsavedFiles = computed(() => JSON.stringify(files.value) !== JSON.stringify(savedFiles.value))
let generation = 0, detailGeneration = 0, timer: ReturnType<typeof setTimeout> | undefined

function stopPoll() { if (timer) clearTimeout(timer); timer = undefined }
function schedulePoll() { stopPoll(); if (executing.value || activeExecution.value) timer = setTimeout(() => { void loadDetail(selected.value, false) }, 2000) }
function designTitle(id: string) { return designs.value.find(item => item.id === id)?.title || (id ? '已关联确认设计' : '未关联设计') }
function languageLabel(language: WorkspaceLanguage = 'python') { return languageLabels[language] ?? '未知语言' }
function restoreFiles() { files.value = savedFiles.value.map((file: {path: string; content: string}) => ({ ...file })) }
function setCodeBusy(value: boolean) { codeBusy.value = value; state.busy = value }

function updateDetail(next: RecordData, resetFiles: boolean) {
  detail.value = next
  workspaces.value = workspaces.value.map(w => w.id === next.workspace.id ? {
    ...next.workspace, latest_snapshot_id: next.latest_snapshot?.id, latest_snapshot_version: next.latest_snapshot?.version,
  } : w)
  if (resetFiles) restoreFiles()
  schedulePoll()
}

function applyCodeSnapshot(next: RecordData) {
  if (next.workspace.id !== selected.value || String(next.workspace.project_id) !== pid.value) return
  ++detailGeneration
  updateDetail(next, true)
}

async function loadDetail(id: string, resetFiles = true) {
  if (!id) return
  const token = ++detailGeneration, project = pid.value
  try {
    const next = await request<RecordData>(`/engineering/workspaces/${id}`)
    if (token !== detailGeneration || id !== selected.value || project !== pid.value) return
    if (String(next.workspace.project_id) !== project) throw Error('工程工作区不属于当前项目。')
    updateDetail(next, resetFiles)
  } catch (failure) { if (token === detailGeneration) { error.value = (failure as Error).message; stopPoll() } }
}

async function choose(id: string) {
  if (controlsLocked.value) return
  stopPoll(); ++detailGeneration; selected.value = id; detail.value = null; files.value = []; reason.value = ''; error.value = ''
  await loadDetail(id)
}

async function loadProject() {
  const token = ++generation, project = pid.value
  const requestedDesign = typeof route.query.design === 'string' ? route.query.design : ''
  const requestedWorkspace = typeof route.query.workspace === 'string' ? route.query.workspace : ''
  const previousSelection = selected.value
  loading.value = true; stopPoll(); ++detailGeneration; error.value = ''
  try {
    if (state.projectId !== project) await selectProject(project)
    if (token !== generation || project !== pid.value) return
    const [items, flows, configuration] = await Promise.all([
      request<RecordData[]>(`/engineering/projects/${project}/workspaces`),
      request<RecordData[]>(`/design/projects/${project}/flows`),
      request<RecordData>('/engineering/executor').catch((failure: Error) => ({
        configured: false, available: false, message: failure.message, loadError: failure.message,
      })),
    ])
    if (token !== generation || project !== pid.value) return
    workspaces.value = items; designs.value = flows.filter(f => f.status === 'COMPLETED'); executor.value = configuration
    if (!snapshotSources.value.some(item => item.latest_snapshot_id === form.sourceSnapshot)) form.sourceSnapshot = ''
    const linkedWorkspace = items.find(item => item.id === requestedWorkspace)
    if (linkedWorkspace) {
      selected.value = linkedWorkspace.id
      form.design = linkedWorkspace.design_flow_id || ''
      showCreate.value = false
    } else if (requestedDesign && designs.value.some(d => d.id === requestedDesign)) {
      form.design = requestedDesign
      selected.value = items.find(w => w.id === previousSelection && w.design_flow_id === requestedDesign)?.id
        || items.find(w => w.design_flow_id === requestedDesign)?.id || ''
      showCreate.value = !selected.value
    } else {
      if (!designs.value.some(d => d.id === form.design)) form.design = ''
      selected.value = items.some(w => w.id === previousSelection) ? previousSelection : items[0]?.id || ''
    }
    if (selected.value) await loadDetail(selected.value, selected.value !== previousSelection || !hasUnsavedFiles.value)
    else { detail.value = null; files.value = []; showCreate.value = true }
  } catch (failure) { if (token === generation) error.value = (failure as Error).message }
  finally { if (token === generation) loading.value = false }
}

watch(() => [route.params.projectId, route.query.design, route.query.workspace, state.initialized, state.loading], () => {
  if (!state.initialized || state.loading || !pid.value) return
  detail.value = null; files.value = []; void loadProject()
}, { immediate: true })

watch(() => form.language, () => { form.sourceSnapshot = '' })

async function create() {
  if (controlsLocked.value) return
  busy.value = true; state.busy = true; error.value = ''
  try {
    const created = await request<RecordData>(`/engineering/projects/${pid.value}/workspaces`, {
      name: form.name.trim(), actor: actor.value.trim(), language: form.language,
      ...(form.design ? { design_flow_id: form.design } : {}),
      ...(form.sourceSnapshot ? { source_snapshot_id: form.sourceSnapshot } : {}),
    })
    workspaces.value = [created, ...workspaces.value]; selected.value = created.id; showCreate.value = false
    await loadDetail(created.id)
    notify(created.source_snapshot_id ? '已从所选快照建立工程工作区，可以生成本轮代码变更。' : '已创建空的工程工作区。', 'success')
    form.sourceSnapshot = ''
  } catch (failure) { error.value = (failure as Error).message }
  finally { busy.value = false; state.busy = false }
}

async function saveSnapshot() {
  if (!workspace.value || !canWrite.value) return
  busy.value = true; state.busy = true; error.value = ''
  try {
    await request(`/engineering/workspaces/${selected.value}/snapshots`, {
      expected_version: workspace.value.version, actor: actor.value.trim(), reason: reason.value.trim(), files: files.value,
    })
    await loadDetail(selected.value); notify('文件快照已保存，尚未执行测试。', 'success')
  } catch (failure) {
    if (failure instanceof ApiError && failure.status === 409) {
      await loadDetail(selected.value, false); error.value = '工作区版本已变化，已刷新状态并保留编辑内容，请重新核对差异后保存。未自动重试。'
    } else error.value = (failure as Error).message
  } finally { busy.value = false; state.busy = false }
}

async function execute() {
  if (!workspace.value || !snapshot.value || !canWrite.value || !currentExecutor.value.available || activeExecution.value) return
  executing.value = true; state.busy = true; error.value = ''; schedulePoll()
  try {
    const result = await request<RecordData>(`/engineering/workspaces/${selected.value}/executions`, {
      expected_version: workspace.value.version, snapshot_id: snapshot.value.id, profile: executionProfile.value,
      actor: actor.value.trim(), reason: reason.value.trim(),
    })
    await loadDetail(selected.value, false)
    const passed = result.status === 'SUCCEEDED' && result.result?.passed
    const message = passed ? '该文件快照的测试已通过。' : `${labels[result.status] || '执行未通过'}，请查看执行记录。`
    notify(message, passed ? 'success' : 'warning')
  } catch (failure) {
    if (failure instanceof ApiError && failure.status === 409) { await loadDetail(selected.value, false); error.value = '工作区或执行版本已变化，请重新核对。没有自动重试执行。' }
    else error.value = (failure as Error).message
  } finally { executing.value = false; state.busy = false; schedulePoll() }
}

async function cancelExecution() {
  if (!activeExecution.value || cancelling.value || !actor.value.trim() || !reason.value.trim()) return
  cancelling.value = true; error.value = ''
  try {
    await request(`/engineering/executions/${activeExecution.value.id}/cancel`, { actor: actor.value.trim(), reason: reason.value.trim() })
    await loadDetail(selected.value, false); notify('取消请求已记录，取消状态与操作历史会保留。', 'success')
  } catch (failure) { error.value = (failure as Error).message }
  finally { cancelling.value = false }
}

onBeforeUnmount(() => { ++generation; ++detailGeneration; stopPoll() })
</script>

<template>
  <div class="project-studio">
    <section class="panel project-panel">
      <div class="studio-heading"><div><p class="eyebrow">代码生成 · 文件快照 · 隔离测试</p><h2>工程工作区</h2></div><button class="button primary" :disabled="controlsLocked" @click="showCreate = !showCreate">{{ showCreate ? '收起新建表单' : '新建工程工作区' }}</button></div>
      <p>关联已确认的设计，按工作区语言生成并审阅代码。确认后保存为文件快照，再对快照运行隔离测试。</p>
      <div v-if="error" class="info-box error" role="alert">{{ error }}</div>
      <div v-if="workspace" class="model-config-box" :class="{ warning: !currentExecutor.available }">
        <strong>{{ currentExecutor.available ? '隔离执行器可用' : currentExecutor.configured ? '隔离执行器当前不可用' : '隔离执行器未配置' }} · {{ profileLabels[executionProfile] }}</strong>
        <p>{{ currentExecutor.message || '请在服务端配置当前语言的隔离执行器。' }}</p>
        <dl v-if="currentExecutor.configured" class="studio-meta">
          <div><dt>执行后端</dt><dd>{{ currentExecutor.backend }}</dd></div>
          <div><dt>固定镜像</dt><dd>{{ currentExecutor.image }}</dd></div>
        </dl>
      </div>
      <form v-if="showCreate" class="editor-box" @submit.prevent="create">
        <h3>创建工程工作区</h3>
        <label class="field">工作区名称<input v-model="form.name" required maxlength="200" :disabled="controlsLocked" placeholder="例如：选课系统工程"></label>
        <label class="field">代码语言
          <select v-model="form.language" :disabled="controlsLocked">
            <option v-for="(label, language) in languageLabels" :key="language" :value="language">{{ label }}</option>
          </select>
          <small>语言在创建时确定。要为同一设计生成另一种语言的代码，请新建工作区。</small>
        </label>
        <label class="field">关联已确认设计（可选）<select v-model="form.design" :disabled="controlsLocked"><option value="">暂不关联</option><option v-for="item in designs" :key="item.id" :value="item.id">{{ item.title }}</option></select><small>仅列出当前项目已确认完成的设计流程。使用代码生成时请选择设计。</small></label>
        <label class="field">代码起点
          <select v-model="form.sourceSnapshot" :disabled="controlsLocked">
            <option value="">从空工作区开始</option>
            <option v-for="item in snapshotSources" :key="item.latest_snapshot_id" :value="item.latest_snapshot_id">
              {{ item.name }} · 快照 v{{ item.latest_snapshot_version }}
            </option>
          </select>
          <small>新增需求时，可沿用本项目同语言工作区的已保存快照，在新一轮设计上继续生成代码变更。</small>
        </label>
        <label class="field">操作署名<input v-model="actor" required maxlength="100" :disabled="controlsLocked"></label>
        <div class="studio-actions"><button class="button primary" :disabled="controlsLocked || !form.name.trim() || !actor.trim()">创建工程工作区</button><RouterLink v-if="!loading && !designs.length" class="button secondary" :to="`/projects/${pid}/design`">去确认一份设计</RouterLink></div>
      </form>
    </section>
    <div class="studio-grid">
      <aside class="panel studio-flow-list" aria-label="工程工作区列表"><div class="studio-list-heading"><h3>工程工作区 <small>{{ workspaces.length }}</small></h3><button class="text-button" :disabled="controlsLocked" @click="loadProject">刷新列表</button></div><p v-if="loading" class="list-loading">正在读取…</p><button v-for="item in workspaces" :key="item.id" class="studio-flow-button" :class="{ active: item.id === selected }" :disabled="controlsLocked" @click="choose(item.id)"><strong>{{ item.name }}</strong><span>{{ languageLabel(item.language ?? 'python') }} · 工作区 v{{ item.version }}</span><small>{{ dateTime(item.created_at, true) }}</small></button><p v-if="!loading && !workspaces.length" class="empty-inline">尚无工程工作区。</p></aside>
      <div v-if="workspace" class="studio-detail section-stack">
        <section class="panel project-panel">
          <div class="studio-heading"><h2>{{ workspace.name }}</h2><span class="studio-badge">工作区 v{{ workspace.version }}</span></div>
          <p>设计来源 · {{ designTitle(workspace.design_flow_id) }}</p>
          <p>代码语言 · {{ languageLabel(workspaceLanguage) }}</p>
          <p v-if="workspace.source_snapshot_id">继承的代码快照 <code>{{ workspace.source_snapshot_id }}</code></p>
          <div v-if="snapshot" class="info-box"><strong>当前文件快照 v{{ snapshot.version }}</strong><p class="studio-hash">{{ snapshot.sha256 }}</p><p>{{ snapshot.files?.length || 0 }} 个文件 · {{ dateTime(snapshot.recorded_at) }}</p></div>
          <p v-else class="info-box">尚无文件快照。请确认保存代码候选，或手工保存工程文件。</p>
          <label class="field">操作署名<input v-model="actor" required maxlength="100" :disabled="controlsLocked"></label>
          <label class="field">操作依据<textarea v-model="reason" required maxlength="2000" rows="3" :disabled="controlsLocked" placeholder="说明本次生成、审阅、保存或测试的目的"></textarea></label>
        </section>
        <WorkspaceInterfaceComparison v-if="workspace.design_flow_id && workspaceLanguage === 'python' && snapshot" :key="'interfaces-' + workspace.id"
          :project-id="pid" :workspace-id="workspace.id" :latest-snapshot-id="snapshot.id" />
        <WorkspaceCodeGeneration v-if="workspace.design_flow_id" :key="workspace.id" :workspace-id="workspace.id" :language-label="languageLabel(workspaceLanguage)"
          :workspace-version="workspace.version" :snapshot-id="snapshot?.id || null" :has-unsaved-files="hasUnsavedFiles"
          :disabled="busy || executing || loading" :actor="actor" :reason="reason"
          @busy="setCodeBusy" @applied="applyCodeSnapshot" @refresh-workspace="loadDetail(selected, false)" />
        <section v-else class="panel project-panel">
          <h3>代码生成与审阅</h3><p>请新建工作区并选择已确认的设计来生成代码。</p>
        </section>
        <section class="panel project-panel">
          <h3>{{ usesNativeRunner ? '隔离编译与测试' : '运行隔离测试' }}</h3>
          <p v-if="snapshot">本次执行使用文件快照 v{{ snapshot.version }}。</p>
          <p v-else>先保存工程文件快照，再执行。</p>
          <p v-if="hasUnsavedFiles" class="info-box warning">当前还有未保存的文件编辑，本次执行使用已保存的快照。</p>
          <div class="studio-actions">
            <button type="button" class="button primary" :disabled="!canWrite || !snapshot || !currentExecutor.available || !!activeExecution" @click="execute">{{ executing ? '隔离执行中…' : '执行当前已保存快照' }}</button>
            <button type="button" class="button secondary" :disabled="busy || codeBusy" @click="loadDetail(selected, false)">刷新执行状态</button>
            <button v-if="activeExecution" type="button" class="button danger" :disabled="cancelling || codeBusy || !actor.trim() || !reason.trim()" @click="cancelExecution">取消本次执行</button>
          </div>
        </section>
        <section class="panel project-panel">
          <details>
            <summary>工程文件与手工编辑（{{ files.length }} 个文件）</summary>
            <div class="studio-actions">
              <button type="button" class="button secondary" :disabled="controlsLocked" @click="files.push({path:'',content:''})">添加文本文件</button>
              <button v-if="hasUnsavedFiles" type="button" class="button secondary" :disabled="controlsLocked" @click="restoreFiles">恢复当前已保存文件</button>
            </div>
            <p>请填写工程内相对路径。每次保存形成新的文件快照。</p>
            <form @submit.prevent="saveSnapshot">
              <article v-for="(file, index) in files" :key="index" class="editor-box">
                <label class="field">文件路径<input v-model="file.path" required maxlength="240" :disabled="controlsLocked" placeholder="工程内相对路径"></label>
                <label class="field">文件内容<textarea v-model="file.content" rows="10" class="json-editor" :disabled="controlsLocked" spellcheck="false"></textarea></label>
                <button type="button" class="text-button" :disabled="controlsLocked" @click="files.splice(index,1)">移除此文件</button>
              </article>
              <p v-if="!files.length" class="empty-inline">工作区目前没有文本文件。</p>
              <div class="studio-actions"><button class="button primary" :disabled="!canWrite || !files.length || files.some(f => !f.path.trim())">保存新的文件快照</button></div>
            </form>
          </details>
        </section>
        <section class="panel project-panel">
          <h3>真实执行记录</h3>
          <p v-if="!detail?.executions?.length" class="info-box">尚无执行记录。</p>
          <article v-for="item in detail?.executions" :key="item.id" class="execution-card studio-execution">
            <div class="execution-top">
              <span class="studio-badge" :class="{ complete: item.status === 'SUCCEEDED' && item.result?.passed }">{{ labels[item.status] || item.status }}</span>
              <strong>{{ profileLabels[item.profile || 'python_unittest'] || item.profile }}</strong>
              <small>{{ dateTime(item.created_at || item.recorded_at, true) }}</small>
            </div>
            <div v-if="item.profile && item.profile !== 'python_unittest'" class="execution-grid">
              <span><b>{{ phaseLabels[item.result?.compile_status] || '—' }}</b>编译结果</span>
              <span><b>{{ phaseLabels[item.result?.test_status] || '—' }}</b>测试结果</span>
              <span><b>{{ item.result?.compile_returncode ?? '—' }}</b>编译退出码</span>
              <span><b>{{ item.result?.test_returncode ?? '—' }}</b>测试退出码</span>
            </div>
            <div class="execution-grid">
              <span><b>{{ item.result?.tests_run ?? '—' }}</b>{{ item.profile && item.profile !== 'python_unittest' ? '实际运行测试程序' : '实际运行测试' }}</span>
              <span><b>{{ item.result?.returncode ?? '—' }}</b>进程退出码</span>
              <span><b>{{ item.result?.duration_ms == null ? '—' : `${item.result.duration_ms} ms` }}</b>执行耗时</span>
            </div>
            <div class="hash-row">文件快照摘要 {{ item.snapshot_sha256 }}</div>
            <p v-if="item.result?.error" class="info-box warning">{{ item.result.error }}</p>
            <details>
              <summary>查看真实执行日志</summary>
              <template v-if="item.result?.stdout"><h4>标准输出</h4><pre class="code-block log-block">{{ item.result.stdout }}</pre></template>
              <template v-if="item.result?.stderr"><h4>{{ item.profile && item.profile !== 'python_unittest' ? '编译与测试日志' : '错误输出' }}</h4><pre class="code-block log-block">{{ item.result.stderr }}</pre></template>
              <p v-if="!item.result?.stdout && !item.result?.stderr" class="section-label">暂无日志。</p>
              <p v-if="item.result?.output_truncated" class="section-label">日志达到保留上限，以上为截断内容。</p>
            </details>
          </article>
        </section>
      </div>
      <section v-else class="panel empty-state"><h3>{{ loading ? '正在读取工程工作区…' : '选择或创建一个工程工作区' }}</h3><p>保存文本文件、固定快照版本，并查看真实执行结果。</p></section>
    </div>
  </div>
</template>
