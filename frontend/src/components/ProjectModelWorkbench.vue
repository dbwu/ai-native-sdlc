<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { request } from '../api/client'
import { dateTime } from '../lib/display'
import { notify, state } from '../store/workbench'
import DiagramPreview, { type DesignDiagram } from './DiagramPreview.vue'
import ProjectArchitectureTree, { type ArchitectureNode } from './ProjectArchitectureTree.vue'
import ProjectModelChanges, { type ProjectModelChange } from './ProjectModelChanges.vue'
import ProjectDiagramComparison from './ProjectDiagramComparison.vue'

type Reference = { id: string; version: number; name?: string }
type Process = { key: string; name: string; description: string; diagrams: DesignDiagram[]; requirement_refs?: Reference[]; rule_refs?: Reference[] }
type Version = { version: number; title: string; confirmed_at: string; proposal_id: string }
type Baseline = Version & {
  summary: string
  model: {
    title: string
    summary: string
    nodes: ArchitectureNode[]
    dependencies: { source: string; target: string; label: string }[]
    processes: { key: string; requirement_refs: Reference[]; rule_refs?: Reference[] }[]
  }
  node_versions?: Record<string, number>
  architecture: DesignDiagram
  processes: Process[]
  changes: ProjectModelChange[]
  download_url: string
}

const example = {
  title: '示例项目基线',
  summary: '记录业务领域、模块职责和主要业务流程。',
  nodes: [
    { key: 'Business', name: '示例业务领域', kind: 'domain', parent_key: null },
    { key: 'Intake', name: '申请办理', kind: 'module', parent_key: 'Business' },
  ],
  dependencies: [],
  processes: [{
    key: 'Submit', name: '提交申请', description: '受理并返回提交结果。',
    participants: [{ key: 'Applicant', name: '申请人' }],
    steps: [
      { key: 'Start', name: '申请人提交申请', kind: 'start', owner: 'Applicant' },
      { key: 'Record', name: '登记申请', kind: 'task', owner: 'Intake' },
      { key: 'Finish', name: '返回受理结果', kind: 'end', owner: 'Applicant' },
    ],
    transitions: [{ source: 'Start', target: 'Record' }, { source: 'Record', target: 'Finish' }],
    scenarios: [{ key: 'Success', name: '提交成功', messages: [
      { source: 'Applicant', target: 'Intake', label: '提交申请' },
      { source: 'Intake', target: 'Applicant', label: '返回受理结果' },
    ] }],
    requirement_refs: [],
  }],
}

const props = defineProps<{ projectId: string; refreshKey: number; openByDefault: boolean }>()
const route = useRoute()
const router = useRouter()
const path = computed(() => `/workbench/projects/${encodeURIComponent(props.projectId)}/project-model`)
const expanded = ref(props.openByDefault)
const tab = ref<'architecture' | 'processes' | 'changes' | 'comparison'>(
  typeof route.query.compare === 'string' ? 'comparison' : 'architecture',
)
const initialBefore = computed(() => typeof route.query.compare === 'string' ? Number(route.query.compare) : undefined)
const versionQuery = computed({
  get: () => typeof route.query.baseline === 'string' ? route.query.baseline : '',
  set: version => {
    void router.replace({ path: route.path, query: { ...route.query, baseline: version || undefined } })
  },
})
const versions = ref<Version[]>([])
const baseline = ref<Baseline | null>(null)
const loading = ref(false)
const loadError = ref('')
const processKey = ref('')
const scenarioIndex = ref(0)
const actor = ref(state.actor || '本地维护者')
const release = ref('v1')
const busy = ref(false)
const submitError = ref('')
const draft = ref('')
const draftBase = ref(0)
const latestVersion = computed(() => Math.max(0, ...versions.value.map(item => item.version)))
const editingCurrent = computed(() => !loading.value && !loadError.value && !versionQuery.value)
const currentProcess = computed(() => baseline.value?.processes.find(item => item.key === processKey.value) ?? baseline.value?.processes[0])
const activity = computed(() => currentProcess.value?.diagrams.find(item => item.kind === 'activity'))
const sequences = computed(() => currentProcess.value?.diagrams.filter(item => item.kind === 'sequence') ?? [])
const processDefinition = computed(() => baseline.value?.model.processes.find(item => item.key === currentProcess.value?.key))
const requirements = computed(() => currentProcess.value?.requirement_refs ?? processDefinition.value?.requirement_refs ?? [])
const rules = computed(() => currentProcess.value?.rule_refs ?? processDefinition.value?.rule_refs ?? [])
const nodeNames = computed(() => new Map(baseline.value?.model.nodes.map(node => [node.key, node.name])))
const childrenByParent = computed(() => {
  const children = new Map<string | null, ArchitectureNode[]>()
  for (const node of baseline.value?.model.nodes ?? []) {
    const siblings = children.get(node.parent_key) ?? []
    siblings.push(node)
    children.set(node.parent_key, siblings)
  }
  return children
})

let generation = 0
watch(() => props.openByDefault, open => { expanded.value = open })
watch(() => [path.value, props.refreshKey, versionQuery.value], async () => {
  const token = ++generation
  loading.value = true
  loadError.value = ''
  try {
    const query = versionQuery.value ? `?version=${encodeURIComponent(versionQuery.value)}` : ''
    const data = await request<{ versions: Version[]; baseline: Baseline | null }>(path.value + query)
    if (token === generation) {
      versions.value = data.versions
      baseline.value = data.baseline
    }
  } catch (failure) {
    if (token === generation) loadError.value = (failure as Error).message
  } finally {
    if (token === generation) loading.value = false
  }
}, { immediate: true })

watch(() => [baseline.value?.version, currentProcess.value?.key], () => { scenarioIndex.value = 0 })

function setExpanded(event: Event) {
  expanded.value = (event.currentTarget as HTMLDetailsElement).open
}

function loadDraft() {
  draftBase.value = latestVersion.value
  draft.value = JSON.stringify(baseline.value?.model ?? example, null, 2)
}

async function sendProposal(endpoint: string, body: Record<string, unknown>) {
  submitError.value = ''
  busy.value = true
  try {
    const proposal = await request<{ id: string }>(path.value + endpoint, { actor: actor.value.trim(), ...body })
    notify('项目基线提案已建立。校验并确认提交后，图表才会更新。')
    await router.push(`/projects/${props.projectId}/proposals/${proposal.id}`)
  } catch (failure) {
    submitError.value = (failure as Error).message
  } finally {
    busy.value = false
  }
}

function submitDraft() {
  let model: unknown
  try {
    model = JSON.parse(draft.value)
  } catch {
    submitError.value = '项目模型 JSON 无法读取，请检查括号、引号和逗号。'
    return
  }
  void sendProposal('', { expected_version: draftBase.value, model })
}
</script>

<template>
  <details class="project-model" :open="expanded" @toggle="setExpanded">
    <summary>项目架构与业务流程</summary>
    <div v-if="expanded" class="model-body">
      <p>从项目基线查看领域、模块和主要业务流程。图表随项目基线确认发布，保留历史版本。</p>
      <div v-if="versions.length" class="model-toolbar">
        <label class="field">项目基线版本
          <select v-model="versionQuery">
            <option value="">最新（基线 v{{ latestVersion }}）</option>
            <option v-for="item in versions" :key="item.version" :value="String(item.version)">
              基线 v{{ item.version }} · {{ item.title }}
            </option>
          </select>
        </label>
        <template v-if="baseline && !loading && !loadError">
          <RouterLink class="button secondary" :to="`/projects/${projectId}/proposals/${baseline.proposal_id}`">查看本轮提案</RouterLink>
          <a class="button secondary" :href="baseline.download_url" download>下载项目基线 Markdown</a>
        </template>
      </div>
      <p v-if="loading" class="section-label" role="status">正在读取项目基线…</p>
      <p v-else-if="loadError" class="info-box error" role="alert">{{ loadError }}</p>
      <p v-else-if="!baseline" class="info-box">项目还没有已确认的架构和业务流程。可以从选课案例建立基线，或提交自己的项目模型。</p>
      <template v-else>
        <h3 class="baseline-title">{{ baseline.title }}</h3>
        <p class="section-label">项目基线 v{{ baseline.version }} · {{ dateTime(baseline.confirmed_at) }} 确认</p>
        <p v-if="baseline.summary" class="baseline-summary">{{ baseline.summary }}</p>
        <div class="model-tabs" aria-label="项目基线视图">
          <button type="button" class="button" :class="tab === 'architecture' ? 'primary' : 'secondary'" :aria-pressed="tab === 'architecture'" @click="tab = 'architecture'">架构总览</button>
          <button type="button" class="button" :class="tab === 'processes' ? 'primary' : 'secondary'" :aria-pressed="tab === 'processes'" @click="tab = 'processes'">业务流程目录</button>
          <button type="button" class="button" :class="tab === 'changes' ? 'primary' : 'secondary'" :aria-pressed="tab === 'changes'" @click="tab = 'changes'">本轮变更</button>
          <button type="button" class="button" :class="tab === 'comparison' ? 'primary' : 'secondary'" :aria-pressed="tab === 'comparison'" @click="tab = 'comparison'">版本对照</button>
        </div>

        <section v-if="tab === 'architecture'" class="model-grid" aria-label="架构总览">
          <section class="model-directory" aria-label="领域与模块目录">
            <h4>领域与模块目录</h4>
            <p class="section-label">点击条目逐级展开。</p>
            <ProjectArchitectureTree :key="baseline.version" :children-by-parent="childrenByParent"
              :node-versions="baseline.node_versions ?? {}" :project-id="projectId" :parent-key="null"/>
          </section>
          <div class="model-detail">
            <DiagramPreview :diagram="baseline.architecture"/>
            <h4>模块依赖</h4>
            <ul v-if="baseline.model.dependencies.length" class="dependency-list">
              <li v-for="dependency in baseline.model.dependencies" :key="`${dependency.source}:${dependency.target}:${dependency.label}`">
                <strong>{{ nodeNames.get(dependency.source) }}</strong> → <strong>{{ nodeNames.get(dependency.target) }}</strong>
                <span>{{ dependency.label }}</span>
              </li>
            </ul>
            <p v-else class="section-label">这版基线没有记录模块依赖。</p>
          </div>
        </section>

        <section v-else-if="tab === 'processes'" class="model-grid" aria-label="业务流程目录">
          <section class="model-directory" aria-label="主要业务流程">
            <h4>主要业务流程</h4>
            <ul v-if="baseline.processes.length" class="process-list">
              <li v-for="process in baseline.processes" :key="process.key">
                <button type="button" class="process-select" :aria-pressed="currentProcess?.key === process.key" @click="processKey = process.key">{{ process.name }}</button>
              </li>
            </ul>
            <p v-else class="section-label">这版基线尚未记录主要业务流程。</p>
          </section>
          <div v-if="currentProcess" class="model-detail">
            <h4>{{ currentProcess.name }}</h4>
            <p v-if="currentProcess.description" class="baseline-summary">{{ currentProcess.description }}</p>
            <ul v-if="requirements.length || rules.length" class="reference-list" aria-label="流程依据">
              <li v-for="item in requirements" :key="`requirement:${item.id}:${item.version}`">
                <RouterLink :to="{ path: `/projects/${projectId}/knowledge`, query: { requirement: item.id, version: String(item.version) } }">
                  {{ item.name || '关联需求' }} · v{{ item.version }}
                </RouterLink>
              </li>
              <li v-for="item in rules" :key="`rule:${item.id}:${item.version}`">
                <RouterLink :to="{ path: `/projects/${projectId}/knowledge`, query: { entity: item.id, version: String(item.version) } }">
                  {{ item.name || '业务规则' }} · v{{ item.version }}
                </RouterLink>
              </li>
            </ul>
            <DiagramPreview v-if="activity" :diagram="activity"/>
            <label v-if="sequences.length" class="field">时序场景
              <select v-model="scenarioIndex">
                <option v-for="(sequence, index) in sequences" :key="index" :value="index">{{ sequence.title }}</option>
              </select>
            </label>
            <DiagramPreview v-if="sequences[scenarioIndex]" :diagram="sequences[scenarioIndex]"/>
            <p v-else class="section-label">这条业务流程尚未记录时序场景。</p>
          </div>
        </section>

        <ProjectDiagramComparison v-else-if="tab === 'comparison'" :key="baseline.version" :path="path"
          :versions="versions" :current-version="baseline.version" :initial-before="initialBefore"/>
        <section v-else class="changes-pane" aria-label="本轮变更">
          <h4>{{ baseline.version === 1 ? '首次发布的内容' : `与上一轮基线相比 · v${baseline.version - 1} → v${baseline.version}` }}</h4>
          <ProjectModelChanges :changes="baseline.changes"/>
        </section>
      </template>

      <details class="model-editor">
        <summary>建立项目基线提案</summary>
        <p v-if="versionQuery" class="info-box">切回最新项目基线后可建立新提案。</p>
        <template v-if="editingCurrent">
          <label class="field model-input">操作署名<input v-model="actor" maxlength="100" required :disabled="busy"></label>
          <form class="model-edit-section" @submit.prevent="sendProposal('/course-example', { expected_version: latestVersion, release })">
            <h4>选课教学案例</h4>
            <p>先在本页下方的「选课教学案例：导入一轮知识」导入相同版本，并在项目提案中校验和确认提交。</p>
            <label class="field model-input">选课架构和流程版本
              <select v-model="release" :disabled="busy">
                <option value="v1">v1 · 基础选课</option>
                <option value="v1.1">v1.1 · 容量限制</option>
                <option value="v1.2">v1.2 · 时间冲突</option>
              </select>
            </label>
            <button type="submit" class="button secondary" :disabled="busy || !actor.trim()">提出选课项目基线</button>
          </form>
          <details class="model-edit-section">
            <summary>自定义项目基线（JSON）</summary>
            <p>读取示例或当前项目基线后再编辑。保留已有领域、模块和流程的标识。提交后在项目提案页审阅。</p>
            <button type="button" class="button secondary" :disabled="busy" @click="loadDraft">{{ baseline ? '载入最新基线' : '载入示例' }}</button>
            <form v-if="draft" @submit.prevent="submitDraft">
              <label class="field">项目模型 JSON <textarea v-model="draft" class="json-editor" rows="16" spellcheck="false" :disabled="busy" required/></label>
              <p v-if="draftBase !== latestVersion" class="info-box warning">此草稿基线已过期，请载入最新基线后再编辑。</p>
              <button type="submit" class="button primary" :disabled="busy || !actor.trim() || draftBase !== latestVersion">提出自定义项目基线</button>
            </form>
          </details>
          <p v-if="submitError" class="info-box error" role="alert">{{ submitError }}</p>
        </template>
      </details>
    </div>
  </details>
</template>

<style scoped>
.project-model { margin-top: 16px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 10px; }
.project-model summary { cursor: pointer; font-weight: 650; }
.model-body { margin-top: 8px; min-width: 0; }
.model-toolbar, .model-tabs { display: flex; flex-wrap: wrap; align-items: end; gap: 8px; margin-top: 12px; }
.model-toolbar .field { flex: 0 1 25rem; min-width: 0; }
.model-toolbar .field, .model-edit-section .field { margin-top: 0; }
.project-model select { max-width: 100%; min-width: 0; padding: 7px 9px; border: 1px solid var(--line); border-radius: 7px; background: #fcfdfe; color: var(--ink); }
.baseline-title { margin-top: 12px; }
.baseline-summary { white-space: pre-wrap; overflow-wrap: anywhere; margin-top: 3px; }
.model-grid { display: grid; grid-template-columns: minmax(190px, 255px) minmax(0, 1fr); gap: 12px; align-items: start; margin-top: 10px; }
.model-directory, .model-detail, .changes-pane { min-width: 0; padding: 10px; border: 1px solid var(--line); border-radius: 7px; }
.dependency-list, .process-list, .reference-list { list-style: none; margin: 7px 0; padding: 0; overflow-wrap: anywhere; }
.dependency-list li + li { margin-top: 5px; }
.dependency-list span { color: var(--muted); margin-inline-start: 8px; }
.process-select { display: block; width: 100%; padding: 6px 8px; border: 1px solid transparent; border-radius: 6px; background: #fff; text-align: start; color: var(--ink); overflow-wrap: anywhere; }
.process-select:hover, .process-select[aria-pressed='true'] { background: var(--blue-soft); border-color: #c0d5e3; }
.reference-list { display: flex; flex-wrap: wrap; gap: 5px 12px; font-size: 12px; }
.changes-pane, .model-editor { margin-top: 10px; }
.model-editor { padding: 10px; background: #f8fafc; border: 1px solid var(--line); border-radius: 7px; }
.model-edit-section { padding: 9px 0; border-bottom: 1px solid var(--line); }
.model-edit-section > p { margin: 4px 0 8px; }
.model-edit-section .button { margin-top: 8px; }
.model-input { max-width: 20rem; }
@media (max-width: 830px) {
  .model-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
