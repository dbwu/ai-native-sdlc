<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ApiError, request } from '../api/client'
import { notify, selectProject, state } from '../store/workbench'
import { dateTime } from '../lib/display'
import type { RecordData } from '../types'

const route = useRoute(), pid = computed(() => String(route.params.projectId || ''))
const workspaces = ref<RecordData[]>([]), designs = ref<RecordData[]>([]), executor = ref<RecordData>({ configured: false, available: false })
const detail = ref<RecordData | null>(null), selected = ref(''), error = ref(''), busy = ref(false), loading = ref(false), executing = ref(false), cancelling = ref(false)
const actor = ref(state.actor || ''), reason = ref(''), showCreate = ref(false)
const form = reactive({ name: '', design: '' }), files = ref<{path: string; content: string}[]>([])
const labels: Record<string, string> = { RUNNING: '执行中', SUCCEEDED: '执行通过', FAILED: '执行失败', TIMED_OUT: '执行超时', CANCELLED: '已取消', STALE: '版本已变化' }
const workspace = computed(() => detail.value?.workspace)
const snapshot = computed(() => detail.value?.latest_snapshot)
const canWrite = computed(() => !busy.value && !executing.value && !!actor.value.trim() && !!reason.value.trim())
const activeExecution = computed(() => (detail.value?.executions || []).find((e: RecordData) => e.status === 'RUNNING'))
let generation = 0, detailGeneration = 0, timer: ReturnType<typeof setTimeout> | undefined

function stopPoll() { if (timer) clearTimeout(timer); timer = undefined }
function schedulePoll() { stopPoll(); if (executing.value || activeExecution.value) timer = setTimeout(() => { void loadDetail(selected.value, false) }, 2000) }
function designTitle(id: string) { return designs.value.find(item => item.id === id)?.title || (id ? '已关联确认设计' : '未关联设计') }

async function loadDetail(id: string, resetFiles = true) {
  if (!id) return
  const token = ++detailGeneration, project = pid.value
  try {
    const next = await request<RecordData>(`/engineering/workspaces/${id}`)
    if (token !== detailGeneration || id !== selected.value || project !== pid.value) return
    if (String(next.workspace.project_id) !== project) throw Error('工程工作区不属于当前项目。')
    detail.value = next
    workspaces.value = workspaces.value.map(w => w.id === id ? next.workspace : w)
    if (resetFiles) files.value = (next.latest_snapshot?.files || []).map((f: RecordData) => ({ path: f.path, content: f.content }))
    schedulePoll()
  } catch (failure) { if (token === detailGeneration) { error.value = (failure as Error).message; stopPoll() } }
}

async function choose(id: string) {
  if (busy.value || executing.value) return
  stopPoll(); ++detailGeneration; selected.value = id; detail.value = null; files.value = []; reason.value = ''; error.value = ''
  await loadDetail(id)
}

async function loadProject() {
  const token = ++generation, project = pid.value
  loading.value = true; stopPoll(); ++detailGeneration; error.value = ''
  try {
    if (state.projectId !== project) await selectProject(project)
    if (token !== generation || project !== pid.value) return
    const [items, flows, configuration] = await Promise.all([
      request<RecordData[]>(`/engineering/projects/${project}/workspaces`),
      request<RecordData[]>(`/design/projects/${project}/flows`), request<RecordData>('/engineering/executor'),
    ])
    if (token !== generation || project !== pid.value) return
    workspaces.value = items; designs.value = flows.filter(f => f.status === 'COMPLETED'); executor.value = configuration
    if (!designs.value.some(d => d.id === form.design)) form.design = ''
    selected.value = items.some(w => w.id === selected.value) ? selected.value : items[0]?.id || ''
    if (selected.value) await loadDetail(selected.value)
    else { detail.value = null; files.value = []; showCreate.value = true }
  } catch (failure) { if (token === generation) error.value = (failure as Error).message }
  finally { if (token === generation) loading.value = false }
}

watch(() => [route.params.projectId, state.initialized, state.loading], () => {
  if (!state.initialized || state.loading || !pid.value) return
  detail.value = null; files.value = []; void loadProject()
}, { immediate: true })

async function create() {
  if (busy.value || executing.value) return
  busy.value = true; state.busy = true; error.value = ''
  try {
    const created = await request<RecordData>(`/engineering/projects/${pid.value}/workspaces`, {
      name: form.name.trim(), actor: actor.value.trim(), ...(form.design ? { design_flow_id: form.design } : {}),
    })
    workspaces.value = [created, ...workspaces.value]; selected.value = created.id; showCreate.value = false
    await loadDetail(created.id); notify('工程工作区已创建，目前为空，不代表已生成代码。', 'success')
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
  if (!workspace.value || !snapshot.value || !canWrite.value || !executor.value.available) return
  executing.value = true; state.busy = true; error.value = ''; schedulePoll()
  try {
    const result = await request<RecordData>(`/engineering/workspaces/${selected.value}/executions`, {
      expected_version: workspace.value.version, snapshot_id: snapshot.value.id, profile: 'python_unittest',
      actor: actor.value.trim(), reason: reason.value.trim(),
    })
    await loadDetail(selected.value, false)
    notify(result.status === 'SUCCEEDED' && result.result?.passed ? '该文件快照的真实测试已通过。' : '本次执行未通过，请查看真实日志。', result.status === 'SUCCEEDED' && result.result?.passed ? 'success' : 'warning')
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
      <div class="studio-heading"><div><p class="eyebrow">工程快照与执行基础</p><h2>工程工作区</h2></div><button class="button primary" :disabled="busy || executing" @click="showCreate = !showCreate">{{ showCreate ? '收起新建表单' : '新建工程工作区' }}</button></div>
      <p>保存工程文件的不可变快照，连接已确认设计，并通过服务端固定测试配置执行。当前里程碑尚未自动生成业务代码；可在此维护文本文件和验证执行接口。</p>
      <div v-if="error" class="info-box error" role="alert">{{ error }}</div>
      <div class="model-config-box" :class="{ warning: !executor.available }"><strong>{{ executor.available ? '隔离执行器可用' : executor.configured ? '隔离执行器当前不可用' : '隔离执行器未配置' }}</strong><p>{{ executor.message || '默认不执行工程代码，不会退回宿主机运行。' }}</p><dl v-if="executor.configured" class="studio-meta"><div><dt>执行后端</dt><dd>{{ executor.backend }}</dd></div><div><dt>固定镜像</dt><dd>{{ executor.image }}</dd></div></dl><p class="section-label">只接受服务端固定的 Python unittest 配置，不接受页面传入命令或宿主路径。测试不通过或没有发现测试时，不会显示执行成功。</p></div>
      <form v-if="showCreate" class="editor-box" @submit.prevent="create"><h3>创建空工作区</h3><label class="field">工作区名称<input v-model="form.name" required maxlength="200" :disabled="busy || executing" placeholder="例如：选课系统工程"></label><label class="field">关联已确认设计（可选）<select v-model="form.design" :disabled="busy || executing"><option value="">暂不关联</option><option v-for="item in designs" :key="item.id" :value="item.id">{{ item.title }}</option></select><small>仅列出当前项目已确认完成的设计流程。</small></label><label class="field">操作署名<input v-model="actor" required maxlength="100" :disabled="busy || executing"></label><div class="studio-actions"><button class="button primary" :disabled="busy || executing || !form.name.trim() || !actor.trim()">创建工程工作区</button><RouterLink class="button secondary" :to="`/projects/${pid}/design`">查看需求设计</RouterLink></div></form>
    </section>
    <div class="studio-grid">
      <aside class="panel studio-flow-list" aria-label="工程工作区列表"><div class="studio-list-heading"><h3>工程工作区 <small>{{ workspaces.length }}</small></h3><button class="text-button" :disabled="busy || executing || loading" @click="loadProject">刷新列表</button></div><p v-if="loading" class="list-loading">正在读取…</p><button v-for="item in workspaces" :key="item.id" class="studio-flow-button" :class="{ active: item.id === selected }" :disabled="busy || executing" @click="choose(item.id)"><strong>{{ item.name }}</strong><span>工作区版本 v{{ item.version }}</span><small>{{ dateTime(item.created_at, true) }}</small></button><p v-if="!loading && !workspaces.length" class="empty-inline">尚无工程工作区。</p></aside>
      <div v-if="workspace" class="studio-detail section-stack">
        <section class="panel project-panel"><div class="studio-heading"><h2>{{ workspace.name }}</h2><span class="studio-badge">工作区 v{{ workspace.version }}</span></div><p>{{ designTitle(workspace.design_flow_id) }}</p><div v-if="snapshot" class="info-box"><strong>当前文件快照 v{{ snapshot.version }}</strong><p class="studio-hash">{{ snapshot.sha256 }}</p><p>{{ snapshot.files?.length || 0 }} 个文件 · {{ dateTime(snapshot.recorded_at) }}</p></div><p v-else class="info-box">尚无文件快照。添加文件并保存后，才能请求执行。</p></section>
        <section class="panel project-panel"><div class="studio-heading"><h3>工程文件</h3><button class="button secondary" :disabled="busy || executing" @click="files.push({path:'',content:''})">添加文本文件</button></div><p>路径填写工程内相对路径，例如 app.py 或 tests/test_app.py。保存会形成新快照，原快照继续保留。</p><form @submit.prevent="saveSnapshot"><article v-for="(file, index) in files" :key="index" class="editor-box"><label class="field">文件路径<input v-model="file.path" required maxlength="240" :disabled="busy || executing" placeholder="例如 tests/test_app.py"></label><label class="field">文件内容<textarea v-model="file.content" rows="10" class="json-editor" :disabled="busy || executing" spellcheck="false"></textarea></label><button type="button" class="text-button" :disabled="busy || executing" @click="files.splice(index,1)">移除此文件</button></article><p v-if="!files.length" class="empty-inline">工作区目前没有文本文件，尚未生成代码。</p><label class="field">操作署名<input v-model="actor" required maxlength="100" :disabled="busy || executing"></label><label class="field">变更或执行依据<textarea v-model="reason" required maxlength="2000" rows="3" :disabled="busy || executing" placeholder="说明本次文件变更或执行验证的依据"></textarea></label><div class="studio-actions"><button class="button primary" :disabled="!canWrite || !files.length || files.some(f => !f.path.trim())">保存新的文件快照</button><button type="button" class="button secondary" :disabled="!canWrite || !snapshot || !executor.available" @click="execute">{{ executing ? '隔离测试执行中…' : '执行当前已保存快照' }}</button><button type="button" class="button secondary" :disabled="busy" @click="loadDetail(selected, false)">刷新执行状态</button><button v-if="activeExecution" type="button" class="button danger" :disabled="cancelling || !actor.trim() || !reason.trim()" @click="cancelExecution">取消本次执行</button></div><p class="section-label">执行只使用上方已保存的文件快照，未保存的编辑不会被执行。</p></form></section>
        <section class="panel project-panel"><h3>真实执行记录</h3><p v-if="!detail?.executions?.length" class="info-box">尚无执行记录。未配置执行器或只保存文件都不代表测试通过。</p><article v-for="item in detail?.executions" :key="item.id" class="execution-card studio-execution"><div class="execution-top"><span class="studio-badge" :class="{ complete: item.status === 'SUCCEEDED' && item.result?.passed }">{{ labels[item.status] || item.status }}</span><strong>Python unittest</strong><small>{{ dateTime(item.created_at || item.recorded_at, true) }}</small></div><div class="execution-grid"><span><b>{{ item.result?.tests_run ?? '—' }}</b>实际运行测试</span><span><b>{{ item.result?.returncode ?? '—' }}</b>进程退出码</span><span><b>{{ item.result?.duration_ms ?? '—' }} ms</b>执行耗时</span></div><div class="hash-row">文件快照摘要 {{ item.snapshot_sha256 }}</div><p v-if="item.result?.error" class="info-box warning">{{ item.result.error }}</p><details><summary>查看真实执行日志</summary><pre class="code-block log-block">{{ item.result?.stdout || '' }}{{ item.result?.stderr || '' }}</pre><p v-if="item.result?.output_truncated" class="section-label">日志达到保留上限，以上为截断内容。</p></details></article></section>
      </div>
      <section v-else class="panel empty-state"><h3>{{ loading ? '正在读取工程工作区…' : '选择或创建一个工程工作区' }}</h3><p>保存文本文件、固定快照版本，并查看真实执行结果。</p></section>
    </div>
  </div>
</template>
