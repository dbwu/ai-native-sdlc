<script setup lang="ts">
import { computed } from 'vue'
import { kinds, relationshipTypes, readable } from '../lib/display'
import type { RecordData } from '../types'

export type ReviewDecision = { finding_id: string; action: string; reason: string }
const props = withDefaults(defineProps<{
  report: RecordData
  projectId?: string
  decisions?: ReviewDecision[]
  readonly?: boolean
  disabled?: boolean
}>(), { projectId: '', decisions: () => [], readonly: false, disabled: false })
const emit = defineEmits<{ 'update:decisions': [value: ReviewDecision[]]; bind: [candidateKey: string, existing: RecordData] }>()
const operations: Record<string, string> = { added: '新增', updated: '修订', evidence_only: '内容沿用，补充证据' }
const findings = computed<RecordData[]>(() => props.report.findings || [])
const changes = computed<RecordData[]>(() => props.report.changes || [])
const allDecisions = computed<ReviewDecision[]>(() => props.readonly ? props.report.decisions || props.decisions : props.decisions)
const impact = computed<RecordData>(() => props.report.impact || {})
const paths = computed<RecordData[]>(() => [...(impact.value.affected || []), ...(impact.value.model_affected || [])])
function decision(id: string) { return allDecisions.value.find(item => item.finding_id === id) }
function updateDecision(id: string, field: 'action' | 'reason', event: Event) {
  const value = (event.target as HTMLInputElement).value
  const previous = decision(id) || { finding_id: id, action: '', reason: '' }
  emit('update:decisions', [...props.decisions.filter(item => item.finding_id !== id), { ...previous, [field]: value }])
}
function actionLabel(finding: RecordData) {
  return (finding.allowed_actions || []).find((item: RecordData) => item.value === decision(finding.id)?.action)?.label || '未记录处理选择'
}
function reference(record: RecordData) {
  return { path: `/projects/${props.projectId}/knowledge`, query: { [record.kind === 'Requirement' ? 'requirement' : 'entity']: record.id, version: String(record.version) } }
}
function versionName(record: RecordData | null | undefined) { return record ? `${record.name || kinds[record.kind] || '已有知识'}${record.version ? ` · v${record.version}` : ''}` : '尚无旧知识' }
function comparisonText(record: RecordData | null | undefined) {
  if (!record) return ''
  if (Array.isArray(record)) return record.map(item => typeof item === 'string' ? item : item.text || item.name || '').filter(Boolean).join('\n')
  if (typeof record === 'string') return record
  return record.description || record.rationale || record.text || ''
}
function acceptanceItems(record: RecordData | null | undefined): RecordData[] {
  return record?.acceptance_items || record?.attributes?.extensions?.acceptance_items || []
}
function attrsText(record: RecordData | null | undefined) {
  const attrs = { ...(record?.attributes || {}) }
  delete attrs.extensions
  delete attrs.acceptance_criteria
  return Object.entries(attrs).filter(([, value]) => value != null).map(([key, value]) => {
    if (key === 'requirement_type') return `需求类别：${value === 'functional' ? '功能需求' : value === 'nonfunctional' ? '非功能需求' : readable(value)}`
    if (key === 'glossary' && value && typeof value === 'object') return `领域术语：\n${Object.entries(value).map(([term, definition]) => `${term}：${definition}`).join('\n')}`
    return `${key}：${readable(value)}`
  }).join('\n')
}
function pathTitle(item: RecordData) { return `${item.name || '受影响对象'}${item.version ? ` · v${item.version}` : ''}` }
function modelLabel(kind: string) { return ({ architecture_node: '架构节点', process: '业务流程', scenario: '时序场景' } as Record<string, string>)[kind] || kind }
function relationStatus(status: string) { return ({ proposed: '待确认关系', verified: '已验证关系', retired: '已退役关系' } as Record<string, string>)[status] || status }
</script>

<template>
  <section class="incremental-review" aria-label="本轮增量与影响分析">
    <h3>{{ readonly ? '本次提案的增量审阅记录' : '本轮增量与影响分析' }}</h3>
    <p class="review-note">分析基于本轮固定的知识版本。疑似重复、冲突与潜在影响均需复核，不代表已合并、删除或修改这些知识。</p>
    <p v-if="report.summary?.semantic_checked === false" class="info-box warning">本次任务未执行模型语义冲突检查，下面仅展示可核对的结构变化和规则提示。</p>
    <p v-for="(note, index) in report.notes || []" :key="index" class="info-box warning">{{ note }}</p>
    <div class="review-counts">
      <span>新增 {{ report.summary?.added ?? 0 }}</span><span>修订 {{ report.summary?.updated ?? 0 }}</span><span>沿用并补证据 {{ report.summary?.evidence_only ?? 0 }}</span><span>待复核事项 {{ findings.length }}</span>
    </div>
    <p v-if="report.retrieval?.complete === false" class="info-box warning">本轮仅纳入 {{ report.retrieval.included_entities }} / {{ report.retrieval.total_entities }} 项知识，另有 {{ report.retrieval.omitted_entities }} 项未进入模型上下文。当前结果不是全项目去重或冲突检查，请核对业务范围是否覆盖。</p>
    <details v-if="report.retrieval" class="review-context"><summary>本轮读取的知识范围</summary>
      <p v-if="report.retrieval.included_entities != null">纳入 {{ report.retrieval.included_entities }} 项，共 {{ report.retrieval.total_entities }} 项；{{ report.retrieval.complete ? '已纳入本次检索的全部知识' : '本轮仅分析筛选的知识子集' }}。</p>
      <p v-if="report.retrieval.message">{{ report.retrieval.message }}</p>
      <p v-for="(note, index) in report.retrieval.warnings || []" :key="index">{{ note }}</p>
      <p v-if="report.retrieval.truncated" class="info-box warning">知识范围受到上限限制，请结合本轮范围说明核对是否遗漏关联知识。</p>
      <details><summary>范围与固定版本详情</summary><pre>{{ JSON.stringify(report.retrieval, null, 2) }}</pre></details>
    </details>

    <section aria-label="知识增量比较" class="review-part">
      <h4>本轮将形成的知识变化</h4>
      <p v-if="!changes.length" class="section-label">当前没有纳入提案的知识变化。</p>
      <article v-for="(change, index) in changes" :key="`${change.table}:${change.key || index}`" class="change-card">
        <div class="change-heading"><span class="change-operation" :class="change.operation">{{ operations[change.operation] || change.operation }}</span><strong>{{ change.after?.name || change.before?.name || relationshipTypes[change.after?.type] || '知识关系' }}</strong></div>
        <p v-if="change.changed_fields?.length" class="review-note">变化内容：{{ change.changed_fields.join('、') }}</p>
        <p v-if="change.change_note" class="review-note">{{ change.change_note }}</p>
        <div class="change-comparison">
          <div class="before"><strong>原内容 · {{ versionName(change.before) }}</strong><p>{{ comparisonText(change.before) }}</p><ul v-if="acceptanceItems(change.before).length"><li v-for="item in acceptanceItems(change.before)" :key="item.key">{{ item.text }}</li></ul><p v-if="attrsText(change.before)" class="attribute-text">{{ attrsText(change.before) }}</p></div>
          <div class="after"><strong>本轮内容</strong><p>{{ comparisonText(change.after) }}</p><ul v-if="acceptanceItems(change.after).length"><li v-for="item in acceptanceItems(change.after)" :key="item.key">{{ item.text }}</li></ul><p v-if="attrsText(change.after)" class="attribute-text">{{ attrsText(change.after) }}</p></div>
        </div>
        <RouterLink v-if="projectId && change.entity_ref" :to="reference({ ...change.before, ...change.entity_ref })" class="text-button">查看此次比较的历史知识 →</RouterLink>
        <details><summary>稳定标识与比较详情</summary><pre>{{ JSON.stringify(change, null, 2) }}</pre></details>
      </article>
    </section>

    <section aria-label="重复冲突与缺失审阅" class="review-part">
      <h4>逐项处理重复、冲突和缺失提示</h4>
      <p v-if="!findings.length" class="info-box">本次分析没有发现需要逐项处理的提示，仍请核对业务含义与原文依据。</p>
      <article v-for="finding in findings" :key="finding.id" class="finding-card" :data-finding-id="finding.id">
        <h4>{{ finding.title }}</h4>
        <p>{{ finding.detail }}</p>
        <p class="review-note">{{ finding.origin === 'model' ? '模型提出的待审阅判断' : '依据固定版本的规则检查' }}</p>
        <template v-if="finding.existing">
          <RouterLink v-if="projectId" :to="reference(finding.existing)" class="text-button">{{ versionName(finding.existing) }} · 查看已有版本 →</RouterLink>
          <p v-else>{{ versionName(finding.existing) }}</p>
        </template>
        <div v-if="finding.before || finding.after" class="change-comparison">
          <div class="before"><strong>已有内容</strong><p>{{ comparisonText(finding.before) || '请查看关联的固定知识版本。' }}</p></div>
          <div class="after"><strong>本轮候选</strong><p>{{ comparisonText(finding.after) || '本轮未纳入对应候选。' }}</p></div>
        </div>
        <blockquote v-for="(quote, index) in finding.evidence || []" :key="index">{{ quote.quote }}</blockquote>
        <p v-if="finding.existing_quote" class="review-note">已有知识依据：{{ finding.existing_quote }}</p>
        <button v-if="!readonly && finding.existing && finding.candidate_keys?.length === 1 && ['possible_duplicate', 'same_name', 'similar_name', 'duplicate_existing'].includes(finding.kind)" type="button" class="text-button" :disabled="disabled" @click="emit('bind', finding.candidate_keys[0], finding.existing)">在候选中关联此已有知识并重新分析</button>
        <template v-if="readonly">
          <p class="decision-result"><strong>审阅选择：{{ actionLabel(finding) }}</strong></p><p class="decision-reason">{{ decision(finding.id)?.reason || '没有保存处理依据。' }}</p>
        </template>
        <template v-else>
          <label class="field">处理选择<select :aria-label="`处理选择：${finding.title}`" :value="decision(finding.id)?.action || ''" :disabled="disabled" :required="finding.required !== false" @change="updateDecision(finding.id, 'action', $event)"><option value="">请选择处理方式</option><option v-for="action in finding.allowed_actions || []" :key="action.value" :value="action.value">{{ action.label }}</option></select></label>
          <label class="field">处理依据<textarea :aria-label="`处理依据：${finding.title}`" :value="decision(finding.id)?.reason || ''" :disabled="disabled" :required="finding.required !== false" maxlength="4000" rows="2" placeholder="说明结合哪些原文和业务范围作出此判断。" @input="updateDecision(finding.id, 'reason', $event)"/></label>
        </template>
      </article>
      <p v-if="report.missing?.length" class="review-note">同一来源中未出现在本轮候选的 {{ report.missing.length }} 项旧知识继续保留。本次提示不会自动删除、归档或退役它们。</p>
    </section>

    <section class="review-part" aria-label="潜在影响路径">
      <h4>潜在影响与追溯路径</h4>
      <p class="review-note">沿已记录的有方向关系和显式模型引用查看潜在影响。以下对象不会由本提案自动修改；没有路径也不代表业务上没有影响。</p>
      <p v-if="impact.available === false" class="info-box warning">此历史任务没有固定的影响分析快照，无法还原当时的完整影响范围。</p>
      <p v-if="impact.message">{{ impact.message }}</p>
      <div v-if="impact.truncated" class="info-box warning"><strong>影响范围未完整展开</strong><ul><li v-for="(message, index) in impact.truncation_reasons || []" :key="index">{{ message }}</li></ul></div>
      <p v-if="!paths.length && impact.available !== false" class="section-label">当前固定快照没有找到可展开的影响路径。</p>
      <article v-for="(item, index) in paths" :key="index" class="impact-card">
        <h4>{{ pathTitle(item) }} <span class="review-note">{{ item.subject ? modelLabel(item.subject.kind) : kinds[item.kind] }} · {{ item.level === 'direct' ? '直接潜在影响' : '间接潜在影响' }}</span></h4>
        <p>{{ item.message }}</p>
        <RouterLink v-if="projectId && item.id" :to="reference(item)" class="text-button">查看固定知识版本 →</RouterLink>
        <RouterLink v-if="projectId && item.baseline_ref" :to="{ path: `/projects/${projectId}/knowledge`, query: { baseline: String(item.baseline_ref.version) } }" class="text-button">查看项目基线 v{{ item.baseline_ref.version }} →</RouterLink>
        <ol class="impact-path"><li v-for="(edge, edgeIndex) in item.path || []" :key="edgeIndex">
          <template v-if="edge.edge_kind === 'relationship'"><strong>{{ versionName(edge.from) }}</strong> → {{ relationshipTypes[edge.type] || edge.type }} → <strong>{{ versionName(edge.to) }}</strong><p class="review-note">关系 v{{ edge.relationship_version }} · {{ relationStatus(edge.status) }} · {{ edge.direction === 'reverse' ? '沿反向依赖追溯' : '按关系方向追溯' }}</p><p v-if="edge.stale_endpoints" class="version-warning">该关系引用历史端点版本（源 v{{ edge.source_version }}、目标 v{{ edge.target_version }}），与当前知识版本不同，需复核关系是否仍适用。</p><p v-if="edge.inactive_endpoints" class="version-warning">路径包含已弃用或归档的知识，需核对当前适用范围。</p></template>
          <template v-else><strong>{{ versionName(edge.from) }}</strong> → {{ modelLabel(edge.subject?.kind) }} · 项目基线 v{{ edge.baseline_ref?.version }}<p v-if="edge.stale_reference" class="version-warning">模型引用知识 v{{ edge.referenced_version }}，当前为 v{{ edge.current_version }}，需核对设计是否已同步。</p><p v-if="edge.reference_version_missing" class="version-warning">此历史模型引用没有记录知识版本，只能作为待核对线索。</p></template>
        </li></ol>
      </article>
    </section>
  </section>
</template>

<style scoped>
.incremental-review { min-width: 0; }
.incremental-review h3, .incremental-review h4 { margin-bottom: 8px; }
.review-note { color: var(--muted); font-size: 12px; }
.review-counts { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
.review-counts span, .change-operation { border-radius: 5px; background: var(--blue-soft); padding: 3px 7px; font-size: 12px; }
.review-part { margin-top: 18px; }
.change-card, .finding-card, .impact-card { border: 1px solid var(--line); border-radius: 8px; padding: 12px; margin: 9px 0; min-width: 0; overflow-wrap: anywhere; }
.finding-card { border-left: 3px solid #c59b55; }
.change-heading { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 8px; }
.change-operation.added { background: #eaf7f0; color: #2c7257; }
.change-operation.evidence_only { background: #eff2f5; color: #526b7e; }
.change-comparison { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 10px 0; }
.change-comparison > div { border-radius: 6px; padding: 9px; min-width: 0; white-space: pre-wrap; }
.change-comparison p { margin-top: 6px; }
.before { background: #f6f6f7; }.after { background: #eff7fb; }
.attribute-text { font-size: 12px; }
details { margin-top: 8px; } summary { cursor: pointer; color: var(--blue); }
pre { max-height: 20rem; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; background: var(--bg); padding: 8px; }
blockquote { margin: 10px 0; padding-left: 10px; border-left: 2px solid #b9cbd8; white-space: pre-wrap; }
.decision-reason { white-space: pre-wrap; }.decision-result { margin-top: 10px; }
.impact-path { padding-left: 22px; }.impact-path li { margin: 8px 0; }
.version-warning { color: #936c27; font-size: 12px; }.review-context { margin-bottom: 12px; }
@media (max-width: 700px) { .change-comparison { grid-template-columns: minmax(0, 1fr); } }
</style>
