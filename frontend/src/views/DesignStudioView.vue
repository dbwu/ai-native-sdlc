<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, request } from '../api/client'
import { notify, selectProject, state } from '../store/workbench'
import { dateTime } from '../lib/display'
import type { RecordData } from '../types'
import DesignModelExchange from '../components/DesignModelExchange.vue'
import DiagramPreview, { type DesignDiagram } from '../components/DiagramPreview.vue'
import ProposalDiff from '../components/ProposalDiff.vue'
import StatusPill from '../components/StatusPill.vue'

const route = useRoute(), router = useRouter()
const pid = computed(() => String(route.params.projectId || ''))
const flows = ref<RecordData[]>([]), catalog = ref<RecordData[]>([]), config = ref<RecordData>({ configured: false })
const detail = ref<RecordData | null>(null), diff = ref<RecordData | null>(null)
const loading = ref(false), busy = ref(false), error = ref(''), flowId = ref(''), confirmed = ref(false)
const confirmedScope = ref(false)
const showCreate = ref(false), actor = ref(state.actor || ''), reason = ref('')
const form = reactive({ title: '', goal: '', requirements: [] as string[], inputs: [] as string[] })
const answers = reactive<Record<string, string>>(Object.create(null))
const labels: Record<string, string> = { DRAFT: '待确认需求', READY: '可以生成设计', GENERATING: '设计生成中', NEEDS_CLARIFICATION: '等待补充说明', REVIEW_PENDING: '设计待审阅', DESIGN_COMMITTED: '设计已提交', COMPLETED: '本轮设计已确认', BLOCKED: '需要处理', CANCELLED: '已取消', QUEUED: '排队中', RUNNING: '生成中', SUCCEEDED: '生成完成', FAILED: '生成失败', pending: '待校验', validated: '校验通过', committed: '已提交', rejected: '已拒绝' }
const flow = computed(() => detail.value?.flow)
const proposal = computed(() => detail.value?.proposal)
const requirements = computed(() => catalog.value.filter(e => e.kind === 'Requirement' && e.status !== 'archived'))
const inputs = computed(() => catalog.value.filter(e => ['BusinessRule', 'Constraint', 'Decision', 'Domain'].includes(e.kind) && e.status !== 'archived'))
const questions = computed<RecordData[]>(() => detail.value?.questions || [])
const design = computed(() => detail.value?.design)
const diagrams = computed<DesignDiagram[]>(() => detail.value?.diagrams ?? [])
const hasSequence = computed(() => diagrams.value.some(diagram => diagram.kind === 'sequence'))
const canAct = computed(() => !busy.value && !!actor.value.trim() && !!reason.value.trim())
const terminal = computed(() => ['COMPLETED', 'CANCELLED'].includes(flow.value?.status))
const canGenerate = computed(() => ['READY', 'BLOCKED'].includes(flow.value?.status) || (flow.value?.status === 'REVIEW_PENDING' && proposal.value?.status === 'rejected'))
const clarifying = computed(() => flow.value?.status === 'NEEDS_CLARIFICATION')
const hasUnsavedAnswers = computed(() => questions.value.some(question => !!answers[question.key]?.trim()))
const canGenerateConfirmed = computed(() => canGenerate.value || clarifying.value)
const canSubmitConfirmed = computed(() => canGenerateConfirmed.value && canAct.value && config.value.configured
  && (!clarifying.value || (confirmedScope.value && !hasUnsavedAnswers.value)))
const designConfirmed = computed(() => flow.value?.status === 'COMPLETED')
const fixedRequirements = computed<RecordData[]>(() => detail.value?.requirements || [])
const requirementNames = computed(() => new Map(fixedRequirements.value.map(item => [String(item.id), item.name])))
let generation = 0, detailGeneration = 0, timer: ReturnType<typeof setTimeout> | undefined
let answerScope = ''

function nameOf(ids: string[] = []) { return ids.map(id => requirementNames.value.get(id) || '关联需求').join('、') }
function deliveryLocation(requirement: RecordData, designId: string) {
  return {
    path: `/projects/${pid.value}/knowledge`,
    query: { requirement: String(requirement.id), version: String(requirement.version), design: designId },
  }
}
function statusName(value: string) { return value === 'STALE' ? '输入版本已变化' : labels[value] || value || '—' }
function recordedAnswer(job: RecordData, key: string) {
  return (job.answers || []).find((answer: RecordData) => answer.key === key)?.answer
}
function stopPoll() { if (timer) clearTimeout(timer); timer = undefined }
function clearAnswers() { Object.keys(answers).forEach(key => delete answers[key]); answerScope = ''; confirmedScope.value = false }
function schedulePoll() {
  stopPoll()
  if (flow.value?.status === 'GENERATING') timer = setTimeout(() => { void loadDetail(flowId.value, true) }, 2000)
}

async function loadDetail(id: string, quiet = false) {
  if (!id) return
  const token = ++detailGeneration, project = pid.value
  try {
    const next = await request<RecordData>(`/design/flows/${encodeURIComponent(id)}`)
    if (token !== detailGeneration || project !== pid.value || id !== flowId.value) return
    if (String(next.flow.project_id) !== project) throw Error('设计流程不属于当前项目。')
    const oldProposal = proposal.value
    if (flow.value?.id !== next.flow.id || flow.value?.version !== next.flow.version || oldProposal?.id !== next.proposal?.id || oldProposal?.version !== next.proposal?.version) {
      confirmed.value = false
      confirmedScope.value = false
    }
    // 同名问题在下一次模型生成中可能含义已变，历史答复不能自动当作本轮答案。
    const nextAnswerScope = JSON.stringify([next.flow.id, next.flow.state?.active_job_id, next.questions || []])
    if (nextAnswerScope !== answerScope) { clearAnswers(); answerScope = nextAnswerScope }
    detail.value = next
    config.value = next.model_config || config.value
    flows.value = flows.value.map(item => item.id === id ? next.flow : item)
    if (!next.proposal) diff.value = null
    else if (!diff.value || oldProposal?.id !== next.proposal.id || oldProposal?.version !== next.proposal.version) {
      const nextDiff = await request<RecordData>(`/workbench/proposals/${next.proposal.id}/diff`)
      if (token !== detailGeneration || project !== pid.value || id !== flowId.value) return
      diff.value = nextDiff
    }
    if (!quiet) error.value = ''
    schedulePoll()
  } catch (failure) {
    if (token !== detailGeneration || project !== pid.value) return
    error.value = (failure as Error).message
    // 读取失败不重复生成，保留手动刷新入口。
    stopPoll()
  }
}

async function choose(id: string) {
  if (busy.value) return
  stopPoll(); ++detailGeneration
  flowId.value = id; detail.value = null; diff.value = null; confirmed.value = false; reason.value = ''
  clearAnswers()
  await router.replace({ path: `/projects/${pid.value}/design`, query: { flow: id } })
  await loadDetail(id)
}

async function loadProject() {
  const token = ++generation, project = pid.value
  stopPoll(); ++detailGeneration; loading.value = true; error.value = ''; confirmed.value = false
  try {
    if (state.projectId !== project) await selectProject(project)
    if (token !== generation || project !== pid.value) return
    const [knowledge, items, model] = await Promise.all([
      request<RecordData>(`/workbench/projects/${project}/knowledge`),
      request<RecordData[]>(`/design/projects/${project}/flows`), request<RecordData>('/design/config'),
    ])
    if (token !== generation || project !== pid.value) return
    catalog.value = knowledge.entities; flows.value = items; config.value = model
    form.requirements = form.requirements.filter(id => requirements.value.some(e => e.id === id))
    form.inputs = form.inputs.filter(id => inputs.value.some(e => e.id === id))
    const requested = String(route.query.flow || '')
    flowId.value = items.some(item => item.id === requested) ? requested : items.some(item => item.id === flowId.value) ? flowId.value : items[0]?.id || ''
    if (flowId.value) await loadDetail(flowId.value)
    else { detail.value = null; diff.value = null; showCreate.value = true }
  } catch (failure) { if (token === generation) error.value = (failure as Error).message }
  finally { if (token === generation) loading.value = false }
}

watch(() => [route.params.projectId, state.initialized, state.loading], () => {
  if (!state.initialized || state.loading || !pid.value) return
  form.requirements = []; form.inputs = []; detail.value = null; diff.value = null; confirmed.value = false; reason.value = ''; clearAnswers()
  void loadProject()
}, { immediate: true })

async function create() {
  if (busy.value) return
  busy.value = true; state.busy = true; error.value = ''
  const project = pid.value
  let createdId = ''
  try {
    const refOf = (id: string) => ({ id, version: catalog.value.find(e => e.id === id)!.version })
    const created = await request<RecordData>(`/design/projects/${project}/flows`, {
      title: form.title.trim(), goal: form.goal.trim(), actor: actor.value.trim(),
      requirements: form.requirements.map(refOf), input_knowledge: form.inputs.map(refOf),
    })
    flows.value = [created, ...flows.value]; flowId.value = created.id
    createdId = created.id
    detail.value = null; diff.value = null; confirmed.value = false; reason.value = ''; showCreate.value = false
    notify('设计流程已创建，先审阅并确认需求范围。', 'success')
  } catch (failure) { error.value = (failure as Error).message }
  finally { busy.value = false; state.busy = false }
  if (createdId && project === pid.value) await choose(createdId)
}

async function action(id: string) {
  if (!flow.value || !canAct.value) return
  if (['commit', 'reject', 'cancel', 'complete'].includes(id) && !confirmed.value) return
  if (id === 'generate_confirmed' && !canSubmitConfirmed.value) return
  busy.value = true; state.busy = true; error.value = ''; stopPoll()
  try {
    await request(`/design/flows/${flowId.value}/actions/${id}`, {
      expected_version: flow.value.version, proposal_version: proposal.value?.version ?? null,
      actor: actor.value.trim(), reason: reason.value.trim(),
    })
    confirmed.value = false
    confirmedScope.value = false
    await loadDetail(flowId.value)
    const failedValidation = id === 'validate' && proposal.value?.result?.valid === false
    const generating = id === 'generate' || id === 'generate_confirmed'
    notify(failedValidation ? '设计提案未通过校验，请查看错误。' : generating ? '生成任务已提交，请在此查看进度。' : '操作已记录。', failedValidation ? 'warning' : 'success')
  } catch (failure) {
    if (failure instanceof ApiError && failure.status === 409) {
      await loadDetail(flowId.value); confirmed.value = false
      error.value = '版本或状态已变化，页面已刷新。请重新审阅，本次操作未自动重试。'
    } else error.value = (failure as Error).message
  } finally { busy.value = false; state.busy = false; schedulePoll() }
}

async function submitAnswers() {
  if (!flow.value || !canAct.value || questions.value.some(q => !answers[q.key]?.trim())) return
  busy.value = true; state.busy = true; error.value = ''
  try {
    await request(`/design/flows/${flowId.value}/answers`, { expected_version: flow.value.version,
      actor: actor.value.trim(), reason: reason.value.trim(),
      answers: questions.value.map(q => ({ key: q.key, answer: answers[q.key].trim() })),
    })
    await loadDetail(flowId.value); notify('补充说明已保存，请明确点击生成设计继续。', 'success')
  } catch (failure) {
    if (failure instanceof ApiError && failure.status === 409) { await loadDetail(flowId.value); error.value = '流程版本已变化，请重新核对问题和答案。未自动重试。' }
    else error.value = (failure as Error).message
  } finally { busy.value = false; state.busy = false }
}

onBeforeUnmount(() => { ++generation; ++detailGeneration; stopPoll() })
</script>

<template>
  <div class="project-studio">
    <section class="panel project-panel">
      <div class="studio-heading"><div><p class="eyebrow">需求到设计</p><h2>需求设计工作区</h2></div><button class="button primary" :disabled="busy" @click="showCreate = !showCreate">{{ showCreate ? '收起新建表单' : '新建需求设计流程' }}</button></div>
      <p>从已确认的需求启动，模型先提出问题或形成设计候选，再由你审阅和确认。本里程碑完成需求与设计，不代表代码已实现或测试已运行。</p>
      <div v-if="error" class="info-box error" role="alert">{{ error }}</div>
      <div class="model-config-box" :class="{ warning: !config.configured }">
        <strong>{{ config.configured ? '模型服务已配置' : '模型服务尚未配置' }}</strong>
        <p>{{ config.message || '模型连接由服务端环境配置，页面不保存密钥。' }}</p>
        <dl v-if="config.configured" class="studio-meta"><div><dt>提供方</dt><dd>{{ config.provider }}</dd></div><div><dt>模型</dt><dd>{{ config.model }}</dd></div><div><dt>服务地址</dt><dd>{{ config.base_url }}</dd></div></dl>
        <details><summary>如何配置模型服务</summary><p>在服务端配置 KNOWLEDGE_MODEL_PROVIDER、KNOWLEDGE_MODEL_BASE_URL、KNOWLEDGE_MODEL_MODEL 及密钥等环境变量后重启服务。云端及本地兼容服务均通过服务端连接。此页仅显示脱敏状态，配置详情见项目中文文档。</p></details>
      </div>
      <form v-if="showCreate" class="editor-box" @submit.prevent="create">
        <h3>只选择需求即可开始</h3>
        <label class="field">流程名称<input v-model="form.title" required maxlength="200" placeholder="例如：选课数量上限设计" :disabled="busy"></label>
        <label class="field">设计目标<textarea v-model="form.goal" required maxlength="4000" rows="3" placeholder="说明本轮希望解决的问题及范围" :disabled="busy"></textarea></label>
        <fieldset class="studio-choices"><legend>选择已有需求及当前版本</legend><label v-for="item in requirements" :key="item.id" class="check-field"><input v-model="form.requirements" type="checkbox" :value="item.id" :disabled="busy"><span>{{ item.name }} <small>知识版本 v{{ item.version }}</small></span></label><p v-if="!requirements.length">尚无可选需求。先到“项目知识”新建需求并明确提交提案。</p></fieldset>
        <details v-if="inputs.length" class="studio-optional"><summary>补充业务规则与工程约束（可选）</summary><label v-for="item in inputs" :key="item.id" class="check-field"><input v-model="form.inputs" type="checkbox" :value="item.id" :disabled="busy"><span>{{ item.name }} · v{{ item.version }}</span></label></details>
        <label class="field">操作署名<input v-model="actor" required maxlength="100" :disabled="busy" placeholder="你的姓名或本地署名"></label>
        <div class="studio-actions"><button class="button primary" :disabled="busy || !form.title.trim() || !form.goal.trim() || !form.requirements.length || !actor.trim()">{{ busy ? '正在保存…' : '创建需求设计流程' }}</button><RouterLink v-if="!loading && !requirements.length" class="button secondary" :to="`/projects/${pid}/knowledge`">去新建需求</RouterLink></div>
      </form>
    </section>

    <div class="studio-grid">
      <aside class="panel studio-flow-list" aria-label="需求设计流程列表"><div class="studio-list-heading"><h3>设计流程 <small>{{ flows.length }}</small></h3><button class="text-button" :disabled="busy || loading" @click="loadProject">刷新列表</button></div><p v-if="loading" class="list-loading">正在读取…</p><button v-for="item in flows" :key="item.id" class="studio-flow-button" :class="{ active: item.id === flowId }" :disabled="busy" @click="choose(item.id)"><strong>{{ item.title }}</strong><span>{{ statusName(item.status) }}</span><small>{{ dateTime(item.created_at, true) }}</small></button><p v-if="!loading && !flows.length" class="empty-inline">尚无设计流程，从上方创建。</p></aside>
      <div v-if="flow" class="studio-detail section-stack">
        <section class="panel project-panel">
          <div class="studio-heading"><h2>{{ flow.title }}</h2><span class="studio-badge" :class="{ complete: designConfirmed }">{{ statusName(flow.status) }}</span></div>
          <p>{{ flow.definition?.goal || flow.goal }}</p>
          <div class="design-stage-track" aria-label="需求设计两阶段进度"><div :class="{ done: flow.status !== 'DRAFT' }"><b>1</b><span>需求确认<small>{{ flow.status === 'DRAFT' ? '待确认' : '需求基线已记录' }}</small></span></div><div :class="{ done: designConfirmed }"><b>2</b><span>设计生成与审阅<small>{{ designConfirmed ? '已确认设计' : statusName(flow.status) }}</small></span></div></div>
          <p class="section-label">流程事件版本 {{ flow.version }}。本轮设计确认后，可在工程工作区关联设计；目前不会自动生成或执行代码。</p>
          <div v-if="flow.status === 'GENERATING'" class="info-box" role="status">模型正在生成设计，页面会更新状态。可取消流程；不会自动校验或提交生成的提案。</div>
          <div v-if="flow.state?.last_error || flow.state?.diagnostic" class="info-box warning">{{ flow.state.last_error || flow.state.diagnostic }}</div>
          <div v-if="designConfirmed" class="info-box">
            <strong>本轮需求与设计已确认。</strong>
            <div class="studio-actions">
              <RouterLink v-if="fixedRequirements.length === 1" class="button secondary"
                :to="deliveryLocation(fixedRequirements[0], flow.id)">查看本次设计产物</RouterLink>
              <RouterLink class="button secondary" :to="{ path: `/projects/${pid}/engineering`, query: { design: flow.id } }">进入工程工作区生成代码 →</RouterLink>
            </div>
            <details v-if="fixedRequirements.length > 1">
              <summary>查看本次设计产物</summary>
              <ul><li v-for="item in fixedRequirements" :key="item.id">
                <RouterLink :to="deliveryLocation(item, flow.id)">{{ item.name }} · v{{ item.version }}</RouterLink>
              </li></ul>
            </details>
          </div>
          <details class="editor-box"><summary>本轮固定的需求版本</summary><article v-for="item in fixedRequirements" :key="item.id" class="studio-requirement"><h4>{{ item.name }} · v{{ item.version }}</h4><p>{{ item.description }}</p><ul><li v-for="criterion in item.attributes?.acceptance_criteria" :key="criterion">{{ criterion }}</li></ul></article></details>
        </section>

        <section v-if="questions.length && clarifying" class="panel project-panel">
          <h3>请补充需求说明</h3><p>可以逐项保存答复，也可以按已确认的范围和已保存答复继续生成。</p>
          <form @submit.prevent="submitAnswers">
            <label v-for="question in questions" :key="question.key" class="field">
              {{ question.question }}
              <small>关联需求 {{ nameOf(question.requirement_ids) }}</small>
              <small v-if="question.acceptance_criterion">验收条件 {{ question.acceptance_criterion }}</small>
              <small v-if="question.acceptance_impact">对验收的影响 {{ question.acceptance_impact }}</small>
              <small v-if="question.options?.length">备选规则 {{ question.options.join(' / ') }}</small>
              <textarea v-model="answers[question.key]" required maxlength="4000" rows="3" :disabled="busy"></textarea>
            </label>
            <p class="section-label">操作署名及依据请在下方“当前流程操作”填写。</p>
            <button class="button primary" :disabled="!canAct || questions.some(q => !answers[q.key]?.trim())">保存补充说明</button>
          </form>
          <div class="info-box">
            <p>按已确认范围生成时，模型只追问会改变本轮验收结果的规则。其余问题作为待审阅假设或后续事项。</p>
            <p v-if="hasUnsavedAnswers" class="section-label">你还有未保存的答复。请补齐并保存，或清空未保存的输入后继续。</p>
            <label class="check-field"><input v-model="confirmedScope" type="checkbox" :disabled="busy || hasUnsavedAnswers"><span>我确认按当前已保存的范围和答复继续生成。</span></label>
            <button type="button" class="button secondary" :disabled="!canSubmitConfirmed" @click="action('generate_confirmed')">按已确认范围生成设计</button>
          </div>
        </section>

        <section v-if="design" class="panel project-panel">
          <h3>模型生成的候选设计</h3><p>{{ design.summary }}</p>
          <section v-if="diagrams.length" aria-label="设计图示">
            <h4 class="studio-section-title">设计图示</h4>
            <p v-if="!hasSequence" class="section-label">本次设计没有记录业务时序图。</p>
            <DiagramPreview v-for="(diagram, index) in diagrams" :key="index" :diagram="diagram"/>
          </section>
          <div v-if="(detail?.assumptions || design.assumptions || []).length" class="info-box warning"><strong>需要审阅的设计假设</strong><ul><li v-for="item in detail?.assumptions || design.assumptions" :key="item">{{ item }}</li></ul><p>这些是假设，需随设计一并确认，不能视作已验证业务事实。</p></div>
          <div v-if="design.deferred_items?.length" class="info-box">
            <strong>后续事项</strong>
            <ul><li v-for="item in design.deferred_items" :key="item">{{ item }}</li></ul>
          </div>
          <h4 class="studio-section-title">组件职责</h4><div class="design-card-grid"><article v-for="item in design.components" :key="item.key" class="artifact-card"><h4>{{ item.name }}</h4><p>{{ item.description }}</p><p>{{ item.responsibility }}</p><small>关联需求：{{ nameOf(item.requirement_ids) }}</small></article></div>
          <h4 class="studio-section-title">接口设计</h4><article v-for="item in design.interfaces" :key="item.key" class="editor-box"><h4>{{ item.name }} <small>{{ item.protocol }}</small></h4><p>{{ item.description }}</p><details><summary>查看接口契约</summary><pre>{{ JSON.stringify(item.contract, null, 2) }}</pre></details><small>关联需求：{{ nameOf(item.requirement_ids) }}</small></article>
          <h4 class="studio-section-title">数据模型</h4><article v-for="item in design.data_models" :key="item.key" class="editor-box"><h4>{{ item.name }}</h4><p>{{ item.description }}</p><details><summary>查看数据结构</summary><pre>{{ JSON.stringify(item.schema_definition, null, 2) }}</pre></details><small>关联需求：{{ nameOf(item.requirement_ids) }}</small></article>
          <h4 class="studio-section-title">测试计划（尚未执行）</h4><article v-for="item in design.test_plan" :key="item.key" class="editor-box"><h4>{{ item.name }}</h4><ol><li v-for="step in item.steps" :key="step">{{ step }}</li></ol><p>预期结果：{{ item.expected_result }}</p><small>关联需求：{{ nameOf(item.requirement_ids) }}</small></article>
        </section>

        <section v-if="proposal" class="panel project-panel">
          <div class="studio-heading"><h3>设计知识提案与差异</h3><StatusPill :status="proposal.status"/></div><p>{{ proposal.title }} · 提案事件版本 {{ proposal.version }}</p><RouterLink class="text-button" :to="`/projects/${pid}/proposals/${proposal.id}`">在项目提案页查看 →</RouterLink>
          <div v-if="diff?.stale" class="info-box warning">输入知识或原基线已变化，请重新协调后生成设计，不能沿用旧提案提交。</div>
          <div v-if="proposal.result?.valid === false" class="info-box warning" role="alert">校验未通过：{{ JSON.stringify(proposal.result.errors) }}</div>
          <details class="editor-box"><summary>展开修改前与候选内容</summary><ProposalDiff :diff="diff"/><p v-if="!diff">正在读取差异…</p></details>
        </section>

        <section class="panel project-panel">
          <h3>当前流程操作</h3><p>每一步单独记录。模型生成、提案校验、确认提交和完成设计互相独立。</p>
          <template v-if="!terminal"><label class="field">操作署名<input v-model="actor" required maxlength="100" :disabled="busy"></label><label class="field">操作依据<textarea v-model="reason" required maxlength="2000" rows="3" :disabled="busy" placeholder="说明本次生成或审核所依据的内容"></textarea></label><label class="check-field"><input v-model="confirmed" type="checkbox" :disabled="busy"><span>我已核对当前需求、设计假设及提案差异，确认本次提交、完成或终止操作。</span></label>
            <p v-if="canGenerateConfirmed && !config.configured" class="info-box warning">请先在服务端配置模型服务。当前需求可以保存，模型未配置时不会生成设计或使用教学模板代替。</p>
            <div class="studio-actions">
              <button v-if="flow.status === 'DRAFT'" class="button primary" :disabled="!canAct" @click="action('start')">确认需求并启动</button>
              <button v-if="canGenerate" class="button primary" :disabled="!canAct || !config.configured" @click="action('generate')">{{ busy ? '正在处理…' : '生成设计提案' }}</button>
              <button v-if="canGenerate" class="button secondary" :disabled="!canSubmitConfirmed" @click="action('generate_confirmed')">按已确认范围生成设计</button>
              <button v-if="proposal?.status === 'pending'" class="button secondary" :disabled="!canAct" @click="action('validate')">校验设计提案</button>
              <button v-if="proposal?.status === 'validated'" class="button primary" :disabled="!canAct || !confirmed || diff?.stale" @click="action('commit')">确认提交设计提案</button>
              <button v-if="proposal?.status === 'committed'" class="button primary" :disabled="!canAct || !confirmed" @click="action('complete')">确认本轮设计完成</button>
              <button v-if="proposal && ['pending', 'validated'].includes(proposal.status)" class="button danger" :disabled="!canAct || !confirmed" @click="action('reject')">拒绝设计提案</button>
              <button class="button secondary" :disabled="!canAct || !confirmed" @click="action('cancel')">取消设计流程</button>
            </div>
          </template>
          <button class="button secondary" :disabled="busy" @click="loadDetail(flowId)">刷新当前流程</button>
        </section>

        <section v-if="detail?.jobs?.length" class="panel project-panel">
          <h3>模型生成记录</h3>
          <article v-for="job in detail.jobs" :key="job.id" class="editor-box">
            <div class="studio-heading"><strong>{{ statusName(job.status) }}</strong><small>{{ dateTime(job.created_at) }}</small></div>
            <p v-if="job.generation_mode === 'confirmed_scope'" class="section-label">按已确认范围生成</p>
            <p v-if="job.diagnostic">{{ job.diagnostic }}</p>
            <details v-if="job.questions?.length">
              <summary>查看原始问题和答复（{{ job.questions.length }} 项）</summary>
              <ol>
                <li v-for="question in job.questions" :key="question.key">
                  <p><strong>模型原始问题</strong> {{ question.question }}</p>
                  <p v-if="question.acceptance_criterion"><strong>验收条件</strong> {{ question.acceptance_criterion }}</p>
                  <p v-if="question.acceptance_impact"><strong>对验收的影响</strong> {{ question.acceptance_impact }}</p>
                  <p v-if="question.options?.length"><strong>备选规则</strong> {{ question.options.join(' / ') }}</p>
                  <p><strong>人工答复</strong> {{ recordedAnswer(job, question.key) || '未提交答复' }}</p>
                </li>
              </ol>
            </details>
            <DesignModelExchange :flow-id="flow.id" :job-id="job.id" :available="job.has_model_exchange === true"
              :pending="['QUEUED', 'RUNNING'].includes(job.status)" />
            <dl v-if="job.trace" class="studio-meta"><div><dt>模型</dt><dd>{{ job.trace.model || '—' }}</dd></div><div><dt>提供方</dt><dd>{{ job.trace.provider || '—' }}</dd></div><div><dt>提示版本</dt><dd>{{ job.trace.prompt_version || '—' }}</dd></div></dl>
            <details v-if="job.trace"><summary>查看调用来源与摘要</summary><pre>{{ JSON.stringify(job.trace, null, 2) }}</pre></details>
          </article>
        </section>
        <section v-if="detail?.history?.length" class="panel project-panel"><details><summary>流程不可变操作历史</summary><article v-for="event in detail.history" :key="event.version" class="editor-box"><strong>v{{ event.version }} · {{ statusName(event.status) }}</strong><p>{{ event.reason }}</p><small>{{ event.actor }} · {{ dateTime(event.recorded_at || event.created_at) }}</small></article></details></section>
      </div>
      <section v-else class="panel empty-state"><h3>{{ loading ? '正在读取需求设计流程…' : '选择或创建一个设计流程' }}</h3><p>已有需求即可开始，无需预先建立组件、接口、代码或测试。</p></section>
    </div>
  </div>
</template>
