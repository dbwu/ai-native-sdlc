<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { request } from '../api/client'
import { state, selectProject, notify } from '../store/workbench'
import { fieldName, kinds, readable, relationshipTypes } from '../lib/display'
import type { RecordData } from '../types'
import ProjectDesignDiagrams from '../components/ProjectDesignDiagrams.vue'
import ProjectModelWorkbench from '../components/ProjectModelWorkbench.vue'
import SourceEvidence from '../components/SourceEvidence.vue'
import StatusPill from '../components/StatusPill.vue'
import RequirementDeliveries from '../components/RequirementDeliveries.vue'

const route = useRoute()
const router = useRouter()
const pid = computed(() => String(route.params.projectId || state.projectId))
function queryValue(name: string) {
  const value = route.query[name]
  return typeof value === 'string' ? value : ''
}
const requirementId = computed(() => queryValue('requirement'))
const entityId = computed(() => queryValue('entity'))
const versionQuery = computed(() => queryValue('version'))
const designId = computed(() => queryValue('design'))
const selectedId = computed(() => requirementId.value || entityId.value)

const batch = ref<RecordData>({ entities: [], relationships: [], keys: {} })
const error = ref('')
const busy = ref(false)
const editor = ref(false)
const editorSection = ref<HTMLDetailsElement>()
const editing = ref<RecordData | null>(null)
const history = ref<RecordData[]>([])
const impact = ref<RecordData | null>(null)
const form = reactive({
  kind: 'Requirement', name: '', description: '', criteria: '', attributes: '{}',
  status: 'draft', reason: '', actor: state.actor || '本地维护者',
})
const relation = reactive({ source: '', target: '', type: 'DEPENDS_ON', reason: '' })
const chosenEndpoints = new Map<string, RecordData>()
const release = ref('v1')

const pageSize = 25
const rows = ref<RecordData[]>([])
const browserKind = ref('Requirement')
const q = ref('')
const activeSearch = ref('')
const offset = ref(0)
const hasNext = ref(false)
const browsing = ref(false)
const browserError = ref('')
const deliveryRefresh = ref(0)
const selectedEntity = ref<RecordData | null>(null)
const entityError = ref('')
const entityLoading = ref(false)
const entityOptions = computed(() => new Map<string, RecordData>([
  ...chosenEndpoints.entries(),
  ...[...batch.value.entities, ...rows.value].map((record: RecordData): [string, RecordData] => [record.id, record]),
]))
let batchRead = 0
let browserRead = 0
let entityRead = 0

async function loadBatch() {
  const token = ++batchRead
  const project = pid.value
  try {
    const data = await request<RecordData>('/workbench/projects/' + encodeURIComponent(project) + '/knowledge')
    if (token === batchRead && project === pid.value) batch.value = data
  } catch (failure) {
    if (token === batchRead && project === pid.value) error.value = (failure as Error).message
  }
}

async function browse() {
  const token = ++browserRead
  const project = pid.value
  const params = new URLSearchParams({ project_id: project, limit: String(pageSize + 1), offset: String(offset.value) })
  if (browserKind.value) params.set('kind', browserKind.value)
  if (activeSearch.value) params.set('q', activeSearch.value)
  browsing.value = true
  browserError.value = ''
  try {
    const result = await request<RecordData[]>('/entities?' + params)
    if (token !== browserRead || project !== pid.value) return
    rows.value = result.slice(0, pageSize)
    hasNext.value = result.length > pageSize
  } catch (failure) {
    if (token === browserRead && project === pid.value) {
      browserError.value = (failure as Error).message
      rows.value = []
      hasNext.value = false
    }
  } finally {
    if (token === browserRead && project === pid.value) browsing.value = false
  }
}

function search() {
  offset.value = 0
  activeSearch.value = q.value.trim()
  void browse()
}

function movePage(delta: number) {
  offset.value += delta * pageSize
  void browse()
}

async function refresh() {
  error.value = ''
  deliveryRefresh.value++
  await Promise.all([loadBatch(), browse()])
}

watch(() => [pid.value, state.initialized], async () => {
  if (!state.initialized || !pid.value) return
  const project = pid.value
  browserKind.value = 'Requirement'
  q.value = ''
  activeSearch.value = ''
  offset.value = 0
  rows.value = []
  hasNext.value = false
  batch.value = { entities: [], relationships: [], keys: {} }
  chosenEndpoints.clear()
  relation.source = ''
  relation.target = ''
  editor.value = false
  impact.value = null
  try {
    if (state.projectId !== project) await selectProject(project)
    if (project === pid.value) await refresh()
  } catch (failure) {
    if (project === pid.value) error.value = (failure as Error).message
  }
}, { immediate: true })

watch(() => [pid.value, entityId.value, entityId.value ? versionQuery.value : '', deliveryRefresh.value], async () => {
  const token = ++entityRead
  selectedEntity.value = null
  entityError.value = ''
  entityLoading.value = false
  if (!entityId.value) return
  const project = pid.value
  entityLoading.value = true
  try {
    const version = versionQuery.value ? '?' + new URLSearchParams({ version: versionQuery.value }) : ''
    const item = await request<RecordData>('/entities/' + encodeURIComponent(entityId.value) + version)
    if (token !== entityRead) return
    if (item.project_id !== project) throw new Error('此知识不属于当前项目。')
    selectedEntity.value = item
  } catch (failure) {
    if (token === entityRead) entityError.value = (failure as Error).message
  } finally {
    if (token === entityRead) entityLoading.value = false
  }
}, { immediate: true })

function selectKnowledge(item: RecordData) {
  const query = item.kind === 'Requirement' ? { requirement: item.id, version: String(item.version) } : { entity: item.id }
  void router.push({ path: route.path, query })
}

function selectDelivery(version: number, design?: string) {
  void router.push({ path: route.path, query: { requirement: requirementId.value, version: String(version), ...(design ? { design } : {}) } })
}

async function proposed(proposal: RecordData) {
  notify('提案已建立，尚未提交知识。')
  await router.push('/projects/' + pid.value + '/proposals/' + proposal.id)
}

async function importCourse() {
  busy.value = true
  try {
    const result = await request<RecordData>('/workbench/projects/' + pid.value + '/course-import', {
      release: release.value, actor: form.actor,
    })
    await proposed(result.proposal)
  } catch (failure) {
    error.value = (failure as Error).message
  } finally {
    busy.value = false
  }
}

function edit(item: RecordData | null) {
  editing.value = item
  Object.assign(form, {
    kind: item?.kind || 'Requirement', name: item?.name || '', description: item?.description || '',
    criteria: (item?.attributes?.acceptance_criteria || []).join('\n'),
    attributes: JSON.stringify(item?.attributes || {}, null, 2), status: item?.status || 'draft', reason: '',
  })
  history.value = []
  impact.value = null
  editor.value = true
  void nextTick(() => {
    editorSection.value?.focus({ preventScroll: true })
    editorSection.value?.scrollIntoView({ block: 'nearest' })
  })
}

async function save() {
  busy.value = true
  error.value = ''
  try {
    const attributes = JSON.parse(form.attributes)
    if (form.kind === 'Requirement') attributes.acceptance_criteria = form.criteria.split('\n').map(line => line.trim()).filter(Boolean)
    let proposal: RecordData
    if (editing.value) {
      const item = editing.value
      proposal = await request('/operations/proposals', {
        project_id: pid.value, title: '修订 ' + form.name, actor: form.actor,
        changes: [{ op: 'update_entity', id: item.id, data: {
          expected_version: item.version, name: form.name, description: form.description,
          status: form.status, attributes, evidence: item.evidence,
          provenance: { actor: form.actor, source_uri: 'urn:workbench:manual', source_revision: String(item.version + 1),
            change_reason: form.reason, nature: 'human_confirmed' },
        } }],
      })
    } else {
      const result = await request<RecordData>('/operations/ingestions', {
        project_id: pid.value, source_uri: 'urn:workbench:manual:' + crypto.randomUUID(),
        source_revision: 'v1', format: 'json', actor: form.actor, title: '新建 ' + form.name,
        content: JSON.stringify({ entities: [{ key: 'item', kind: form.kind, name: form.name, description: form.description, attributes }], relationships: [] }),
      })
      proposal = result.proposal
    }
    editor.value = false
    await proposed(proposal)
  } catch (failure) {
    error.value = (failure as Error).message
  } finally {
    busy.value = false
  }
}

async function viewHistory(item: RecordData) {
  edit(item)
  const project = pid.value
  try {
    const [historyResult, impactResult] = await Promise.all([
      request<RecordData[]>('/entities/' + item.id + '/history'),
      request<RecordData>('/operations/impact?' + new URLSearchParams({ project_id: project, entity_id: item.id })),
    ])
    if (pid.value !== project || editing.value?.id !== item.id) return
    history.value = historyResult
    impact.value = impactResult
  } catch (failure) {
    if (pid.value === project && editing.value?.id === item.id) error.value = (failure as Error).message
  }
}

function rememberRelation(id: string) {
  const item = entityOptions.value.get(id)
  if (item) chosenEndpoints.set(id, item)
}

async function addRelation() {
  busy.value = true
  try {
    const source = entityOptions.value.get(relation.source)
    const target = entityOptions.value.get(relation.target)
    if (!source || !target) throw new Error('请先选择两个关联的知识。')
    const proposal = await request<RecordData>('/workbench/projects/' + pid.value + '/relationships', {
      type: relation.type, source_id: source.id, target_id: target.id, source_version: source.version,
      target_version: target.version, rationale: relation.reason,
      provenance: { actor: form.actor, source_uri: 'urn:workbench:manual', source_revision: 'v1',
        change_reason: relation.reason, nature: 'inference' },
    })
    await proposed(proposal)
  } catch (failure) {
    error.value = (failure as Error).message
  } finally {
    busy.value = false
  }
}

async function check() {
  try { impact.value = await request('/operations/consistency?project_id=' + pid.value) }
  catch (failure) { error.value = (failure as Error).message }
}
</script>

<template>
  <section class="panel project-panel">
    <h2>项目知识工作台</h2>
    <p>查看项目架构和主要业务流程，或从单条需求追溯设计产物与交付文档。知识创建与修订须提交提案审阅。</p>
    <div v-if="error" class="info-box error" role="alert">{{ error }}</div>
    <div class="inline-actions">
      <button type="button" class="button primary" @click="edit(null)">新建知识</button>
      <RouterLink class="button primary" :to="`/projects/${pid}/knowledge/extraction`">文档知识抽取</RouterLink>
      <button type="button" class="button secondary" @click="refresh">刷新知识</button>
      <button type="button" class="button secondary" @click="check">检查一致性</button>
      <a class="button secondary" href="/metamodel/registry" target="_blank" rel="noopener">类型注册表</a>
    </div>
    <ProjectModelWorkbench v-if="state.initialized && pid" :key="`model:${pid}`" :project-id="pid"
      :refresh-key="deliveryRefresh" :open-by-default="!selectedId"/>
    <ProjectDesignDiagrams v-if="state.initialized && pid" :key="pid" :project-id="pid"
      :refresh-key="deliveryRefresh" :open-by-default="!selectedId"/>
    <div class="knowledge-workspace">
      <section class="knowledge-browser" aria-label="项目知识目录">
        <h3>知识目录</h3>
        <form @submit.prevent="search">
          <label class="field">知识类型
            <select v-model="browserKind" @change="search">
              <option value="">全部知识</option>
              <option v-for="(label, key) in kinds" v-show="key !== 'Relationship'" :key="key" :value="key">{{ label }}</option>
            </select>
          </label>
          <label class="field">搜索知识
            <input v-model="q" type="search" maxlength="300" placeholder="在当前项目按名称或说明搜索">
          </label>
          <button class="button secondary" type="submit">搜索</button>
        </form>
        <p v-if="browserError" class="info-box error" role="alert">{{ browserError }}</p>
        <p v-if="browsing" class="section-label" role="status">正在读取知识…</p>
        <p v-else-if="!browserError && !rows.length" class="section-label">没有找到符合条件的知识。</p>
        <ul v-if="rows.length" class="knowledge-items">
          <li v-for="item in rows" :key="item.id">
            <button type="button" class="knowledge-item" :class="{ selected: selectedId === item.id }"
              :aria-pressed="selectedId === item.id" @click="selectKnowledge(item)">
              <strong>{{ item.name }}</strong>
              <small>{{ kinds[item.kind] }} · v{{ item.version }}<template v-if="batch.keys[item.id]"> · {{ batch.keys[item.id] }}</template></small>
            </button>
          </li>
        </ul>
        <div v-if="offset || hasNext" class="inline-actions">
          <button type="button" class="button secondary" :disabled="browsing || offset === 0" @click="movePage(-1)">上一页</button>
          <button type="button" class="button secondary" :disabled="browsing || !hasNext" @click="movePage(1)">下一页</button>
        </div>
      </section>
      <section class="knowledge-detail" aria-label="当前知识">
        <RequirementDeliveries v-if="requirementId" :key="pid + ':' + requirementId + ':' + deliveryRefresh"
          :project-id="pid" :requirement-id="requirementId" :version="versionQuery" :design-id="designId"
          @select="selectDelivery" @revise="edit" @history="viewHistory"/>
        <template v-else-if="entityId">
          <p v-if="entityLoading" class="section-label" role="status">正在读取知识详情…</p>
          <p v-if="entityError" class="info-box error" role="alert">{{ entityError }}</p>
          <template v-if="selectedEntity">
            <h3>{{ selectedEntity.name }}</h3>
            <p>{{ kinds[selectedEntity.kind] }} · v{{ selectedEntity.version }} <StatusPill :status="selectedEntity.status"/></p>
            <p class="entity-description">{{ selectedEntity.description || '暂无说明' }}</p>
            <RouterLink v-if="versionQuery" :to="{ path: route.path, query: { entity: entityId } }">查看此知识的当前版本</RouterLink>
            <div v-else class="inline-actions">
              <button v-if="selectedEntity.kind !== 'Project' && selectedEntity.status !== 'archived'" type="button"
                class="button secondary" @click="edit(selectedEntity)">修订此知识</button>
              <button type="button" class="button secondary" @click="viewHistory(selectedEntity)">历史与影响</button>
            </div>
            <SourceEvidence :key="`${pid}:${selectedEntity.id}:${selectedEntity.version}`" :project-id="pid"
              :entity-id="selectedEntity.id" :version="selectedEntity.version"
              :editable="!versionQuery && selectedEntity.kind !== 'Project' && selectedEntity.status !== 'archived'"/>
            <dl class="entity-fields">
              <div v-for="(value, field) in selectedEntity.attributes" :key="field">
                <dt>{{ fieldName('attributes.' + field) }}</dt><dd><pre>{{ readable(value) }}</pre></dd>
              </div>
            </dl>
          </template>
        </template>
        <div v-else class="info-box">在知识目录选择一条需求，即可查看需求版本与交付文档。</div>
      </section>
    </div>
    <details class="scenario-card"><summary>选课教学案例：导入一轮知识</summary><p>先完成本轮提案与流程，再导入下一轮。导入只建立待审提案，后续创建流程时绑定已有知识。</p><label>教学版本 <select v-model="release"><option>v1</option><option>v1.1</option><option>v1.2</option></select></label><label>操作署名 <input v-model="form.actor" required></label><button class="button secondary" :disabled="busy||!form.actor.trim()" @click="importCourse">提出选课知识提案</button></details>
<details v-if="editor" ref="editorSection" open tabindex="-1" class="editor-box"><summary>{{ editing ? '修订知识与查看历史' : '新建知识草稿' }}</summary><form @submit.prevent="save"><label class="field">类型<select v-model="form.kind" :disabled="!!editing"><option v-for="(label,key) in kinds" v-show="!['Project','Relationship'].includes(key)" :key="key" :value="key">{{ label }}</option></select></label><label class="field">名称<input v-model="form.name" required></label><label class="field">说明<textarea v-model="form.description" rows="3"></textarea></label><label v-if="form.kind==='Requirement'" class="field">验收条件（每行一条）<textarea v-model="form.criteria" rows="4"></textarea></label><label v-if="editing" class="field">生命周期<select v-model="form.status"><option value="draft">草稿</option><option value="active">生效</option><option value="deprecated">弃用</option><option value="archived">归档</option></select></label><details><summary>类型属性（高级 JSON 编辑）</summary><textarea v-model="form.attributes" rows="10" class="json-editor"></textarea><p>组件职责用 responsibility，接口协议用 protocol，模型结构用 schema_definition；服务端仍按类型校验。</p></details><label class="field">操作署名<input v-model="form.actor" required></label><label class="field">变更依据<textarea v-model="form.reason" required></textarea></label><button class="button primary" :disabled="busy||editing?.kind==='Project'||editing?.status==='archived'">建立待审提案</button> <button type="button" class="button secondary" @click="editor=false">收起</button></form><h3 v-if="history.length">不可变修订历史</h3><details v-for="h in history" :key="h.version"><summary>v{{ h.version }} · {{ h.name }} · {{ h.provenance.change_reason }}</summary><pre>{{ JSON.stringify(h,null,2) }}</pre></details></details>
<details v-if="impact" open class="editor-box"><summary>影响或一致性检查结果（待人工复核）</summary><pre>{{ JSON.stringify(impact,null,2) }}</pre></details>
<details class="editor-box"><summary>项目关系与新增关系提案</summary><table class="knowledge-table"><tr v-for="r in batch.relationships" :key="r.id"><td>{{ entityOptions.get(r.source_id)?.name || r.source_id }}</td><td>{{ relationshipTypes[r.type] }}</td><td>{{ entityOptions.get(r.target_id)?.name || r.target_id }}</td><td><StatusPill :status="r.status"/></td></tr></table><form @submit.prevent="addRelation"><label class="field">源知识<select v-model="relation.source" required @change="rememberRelation(relation.source)"><option value="">请选择</option><option v-for="e in entityOptions.values()" :key="e.id" :value="e.id">{{ e.name }}</option></select></label><label class="field">关系类型<select v-model="relation.type"><option v-for="(label,key) in relationshipTypes" :value="key" :key="key">{{ label }}</option></select></label><label class="field">目标知识<select v-model="relation.target" required @change="rememberRelation(relation.target)"><option value="">请选择</option><option v-for="e in entityOptions.values()" :key="e.id" :value="e.id">{{ e.name }}</option></select></label><label class="field">关系依据<textarea v-model="relation.reason" required></textarea></label><button class="button primary" :disabled="busy||!form.actor.trim()">建立关系提案</button></form></details>
  </section>
</template>

<style scoped>
.knowledge-workspace { display: grid; grid-template-columns: minmax(220px, 270px) minmax(0, 1fr); align-items: start; gap: 16px; margin: 16px 0; }
.knowledge-browser, .knowledge-detail { min-width: 0; padding: 14px; border: 1px solid var(--line); border-radius: 10px; }
.knowledge-browser .field { margin-top: 9px; }
.knowledge-browser form > button { margin: 8px 0; }
.knowledge-items { margin: 8px 0 0; padding: 0; list-style: none; max-height: 33rem; overflow-y: auto; }
.knowledge-item { display: flex; flex-direction: column; width: 100%; gap: 3px; padding: 8px 9px; border: 1px solid transparent; border-radius: 6px; text-align: start; background: transparent; color: var(--ink); }
.knowledge-item:hover, .knowledge-item.selected { border-color: #c0d5e3; background: var(--blue-soft); }
.knowledge-item strong, .knowledge-item small, .entity-description { overflow-wrap: anywhere; }
.knowledge-item small { color: var(--muted); }
.entity-description { margin: 8px 0; white-space: pre-wrap; }
.entity-fields dd { margin: 0; }
.entity-fields pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 14rem; overflow: auto; }
@media (max-width: 830px) {
  .knowledge-workspace { grid-template-columns: minmax(0, 1fr); }
  .knowledge-items { max-height: 16rem; }
}
</style>
